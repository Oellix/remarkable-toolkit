#!/usr/bin/env python3
"""Markdown-Datei -> bildschirmoptimiertes HTML (Standalone-CLI, nutzt rmlib).

    .venv/bin/python scripts/md2html.py eingabe.md ausgabe.html
"""
from __future__ import annotations

import sys

import rmlib


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: md2html.py <input.md> <output.html>", file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        html = rmlib.md_to_html(f.read())
    with open(argv[1], "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML geschrieben: {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
