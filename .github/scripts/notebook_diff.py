#!/usr/bin/env python3
"""
Render every notebook changed in a PR as readable cell source, diff it against
the merge base, and write the result to .notebook-diffs/pr-<N>.diff so
reviewers can leave line comments on it. Removes the file once the PR has the
review-done label, or when no notebook changes remain.

Standard library only. Configured by env vars set in the workflow:
  PR_NUMBER, BASE_REF, REVIEW_DONE ("true"/"false"), INCLUDE_OUTPUTS ("true"/"false")
"""

from __future__ import annotations  # allows "str | None" hints on Python 3.7-3.9

import difflib
import json
import os
import subprocess
from pathlib import Path

DIFF_DIR = Path(".notebook-diffs")


# ------------------------------------------------------------------------ git

def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def show(ref: str, path: str) -> str | None:
    r = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, encoding="utf-8"
    )
    return r.stdout if r.returncode == 0 else None


def changed_notebooks(base: str) -> list[tuple[str, str, str]]:
    """(status, old_path, new_path) for each .ipynb changed since base."""
    parts = git("diff", "--name-status", "-M", "-z", base, "HEAD", "--", "*.ipynb").split("\0")
    out, i = [], 0
    while i < len(parts) and parts[i]:
        status = parts[i][0]
        if status in "RC":
            out.append((status, parts[i + 1], parts[i + 2]))
            i += 3
        else:
            out.append((status, parts[i + 1], parts[i + 1]))
            i += 2
    return out


# ------------------------------------------------------- notebook -> text lines

def _join(src) -> str:
    return "".join(src) if isinstance(src, list) else (src or "")


def _output_text(out: dict) -> str:
    kind = out.get("output_type")
    if kind == "stream":
        return _join(out.get("text"))
    if kind in ("execute_result", "display_data"):
        data = out.get("data", {})
        if "text/plain" in data:
            return _join(data["text/plain"])
        return "[" + ", ".join(sorted(data)) + "]"  # e.g. [image/png]
    if kind == "error":
        return f"{out.get('ename')}: {out.get('evalue')}"
    return ""


def notebook_lines(raw: str | None, include_outputs: bool) -> list[str]:
    """Jupytext-style percent format. Cells are deliberately not numbered:
    inserting one cell would otherwise change every header below it."""
    if raw is None:
        return []
    try:
        nb = json.loads(raw)
    except json.JSONDecodeError:
        return ["# (not valid notebook JSON, showing raw file)"] + raw.splitlines()

    lines: list[str] = []
    for cell in nb.get("cells", []):
        kind = cell.get("cell_type", "code")
        lines.append("# %%" if kind == "code" else f"# %% [{kind}]")
        lines.extend(_join(cell.get("source")).splitlines())
        if include_outputs and kind == "code":
            for out in cell.get("outputs", []):
                text = _output_text(out)
                if text:
                    lines.append("# --- output ---")
                    lines.extend("# " + ln for ln in text.splitlines())
        lines.append("")
    return lines


# ----------------------------------------------------------------------- main

def remove(path: Path, reason: str) -> None:
    if path.exists():
        path.unlink()
        print(f"Removed {path}: {reason}")
    if DIFF_DIR.exists() and not any(DIFF_DIR.iterdir()):
        DIFF_DIR.rmdir()


def main() -> None:
    pr = os.environ["PR_NUMBER"]
    out_path = DIFF_DIR / f"pr-{pr}.diff"

    if os.environ.get("REVIEW_DONE") == "true":
        remove(out_path, "review marked done")
        return

    include_outputs = os.environ.get("INCLUDE_OUTPUTS") == "true"
    merge_base = git("merge-base", f"origin/{os.environ['BASE_REF']}", "HEAD").strip()

    # No commit SHA in the header on purpose: the file must be byte-identical
    # when notebooks haven't changed, or the bot's own commit would trigger an
    # endless regenerate-and-push loop.
    lines = [
        f"# Readable notebook diff for PR #{pr}. Comment on any line below.",
        "# When review is done, add the 'notebook-review-done' label and this file",
        "# will be removed automatically. The PR can't merge until it's gone.",
        "",
    ]
    changed = 0
    for status, old_path, new_path in changed_notebooks(merge_base):
        old = None if status == "A" else show(merge_base, old_path)
        new = None if status == "D" else show("HEAD", new_path)
        diff = list(difflib.unified_diff(
            notebook_lines(old, include_outputs),
            notebook_lines(new, include_outputs),
            fromfile=f"a/{old_path}" if old is not None else "/dev/null",
            tofile=f"b/{new_path}" if new is not None else "/dev/null",
            lineterm="",
        ))
        if diff:
            changed += 1
            lines.extend(diff)
            lines.append("")

    if not changed:
        remove(out_path, "no notebook source changes in this PR")
        return

    content = "\n".join(lines) + "\n"
    if out_path.exists() and out_path.read_text(encoding="utf-8") == content:
        print(f"{out_path} already up to date")
        return

    DIFF_DIR.mkdir(exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"Wrote {out_path} covering {changed} notebook(s)")


if __name__ == "__main__":
    main()
