#!/usr/bin/env python3
"""pull.py — Daten von der reMarkable-Cloud abziehen.

    pull.py list [PFAD]                 # Cloud-Inhalt auflisten
    pull.py get  "Schnellnotiz" -o ./   # ein Dokument als .rmdoc holen
    pull.py backup [DIR]                # ALLES rekursiv sichern
    pull.py render "Schnellnotiz" -o note.pdf   # Notizbuch -> PDF (Handschrift als Bild)

Hinweis: 'render' gibt Handschrift als BILD wieder (kein Text). Farbe kann
fehlen, solange rmscene das neueste Paper-Pro-Move-Format nicht voll abdeckt.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

import rmlib

AUTH_HINT = "  (Fehlgeschlagen — Token abgelaufen? Neu anmelden, siehe SKILL.md)"


def rmapi(args: list[str], cwd: str | None = None) -> int:
    rmlib.require(rmlib.RMAPI, "Erst 'bash scripts/setup.sh' ausführen.")
    return subprocess.run([rmlib.RMAPI, *args], env=rmlib.rm_env(), cwd=cwd).returncode


def _ordered_pages(extract: str) -> list[str]:
    """Seitenreihenfolge aus der .content-Datei (cPages.pages) statt nach UUID-Dateiname.
    Filtert gelöschte Seiten. Fallback: sortierte .rm-Dateien (sehr altes Format)."""
    rm_glob = glob.glob(os.path.join(extract, "*", "*.rm"))
    content_files = glob.glob(os.path.join(extract, "*.content"))
    if content_files:
        notebook = os.path.splitext(os.path.basename(content_files[0]))[0]
        try:
            with open(content_files[0], encoding="utf-8") as f:
                content = json.load(f)
            order = [p["id"] for p in content.get("cPages", {}).get("pages", [])
                     if isinstance(p, dict) and p.get("id") and not p.get("deleted")]
            ordered = [os.path.join(extract, notebook, f"{pid}.rm") for pid in order]
            ordered = [p for p in ordered if os.path.exists(p)]
            if ordered:
                return ordered
        except (OSError, ValueError, KeyError):
            pass
    return sorted(rm_glob)


def cmd_list(a) -> int:
    rc = rmapi(["ls", a.path])
    if rc != 0:
        print(AUTH_HINT, file=sys.stderr)
    return rc


def cmd_get(a) -> int:
    out = a.out or os.getcwd()
    os.makedirs(out, exist_ok=True)
    print(f"→ Hole '{a.path}' nach {out} …")
    rc = rmapi(["get", a.path], cwd=out)
    if rc != 0:
        print(AUTH_HINT, file=sys.stderr)
    return rc


def cmd_backup(a) -> int:
    out = a.dir or os.path.join(rmlib.ROOT, "backup")
    os.makedirs(out, exist_ok=True)
    print(f"→ Vollbackup (rekursiv) nach {out} …")
    rc = rmapi(["mget", "/"], cwd=out)
    if rc != 0:
        print(AUTH_HINT, file=sys.stderr)
    return rc


def cmd_render(a) -> int:
    rmlib.require(rmlib.RMC, "rmc fehlt — 'bash scripts/setup.sh' ausführen.")
    tmp = tempfile.mkdtemp(prefix="rm-render-")
    try:
        print(f"→ Lade '{a.path}' …")
        if rmapi(["get", a.path], cwd=tmp) != 0:
            print("FEHLER: Download fehlgeschlagen." + AUTH_HINT, file=sys.stderr)
            return 1
        docs = sorted(glob.glob(os.path.join(tmp, "*.rmdoc")) + glob.glob(os.path.join(tmp, "*.zip")))
        if not docs:
            print("FEHLER: kein .rmdoc/.zip erhalten.", file=sys.stderr)
            return 1
        extract = os.path.join(tmp, "extract")
        with zipfile.ZipFile(docs[0]) as z:
            z.extractall(extract)

        pages = _ordered_pages(extract)
        if not pages:
            print("Keine handschriftlichen .rm-Seiten — bei reinen PDF/EPUB-Dokumenten "
                  "nutze 'get' oder 'backup'.", file=sys.stderr)
            return 1

        svgs, skipped = [], 0
        for i, rmf in enumerate(pages):
            svg = os.path.join(tmp, f"page{i:03d}.svg")
            r = subprocess.run([rmlib.RMC, "-t", "svg", "-o", svg, rmf],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and os.path.exists(svg) and os.path.getsize(svg) > 0:
                svgs.append(svg)
            else:
                skipped += 1
                print(f"  ⚠ Seite {i + 1}/{len(pages)} konnte nicht gerendert werden.",
                      file=sys.stderr)
        if not svgs:
            print("FEHLER: rmc konnte keine Seite rendern.", file=sys.stderr)
            return 1

        out = a.out or os.path.join(
            rmlib.ROOT, f"{rmlib.safe_name(os.path.basename(a.path.rstrip('/')))}.pdf")
        html = os.path.join(tmp, "all.html")
        with open(html, "w", encoding="utf-8") as f:
            f.write('<!doctype html><meta charset="utf-8"><style>'
                    '@page{margin:6mm} html,body{margin:0;padding:0}'
                    '.pg{page-break-after:always;text-align:center}'
                    '.pg:last-child{page-break-after:auto}'
                    'svg{max-width:100%;height:auto}</style>')
            for svg in svgs:
                with open(svg, encoding="utf-8") as s:
                    f.write(f'<div class="pg">{rmlib.strip_scripts(s.read())}</div>')
        rmlib.html_to_pdf(html, out, timeout=max(40, 8 * len(svgs)))

        msg = f"✓ {len(svgs)} Seite(n) gerendert → {out}"
        if skipped:
            msg += f"  ({skipped} übersprungen)"
        print(msg)
        print("  Hinweis: Handschrift als Bild (kein Text); Farbe evtl. nicht enthalten.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Von reMarkable abziehen")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="Cloud-Inhalt auflisten")
    p.add_argument("path", nargs="?", default="/")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("get", help="ein Dokument als .rmdoc holen")
    p.add_argument("path")
    p.add_argument("-o", "--out", help="Zielverzeichnis")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("backup", help="ALLES rekursiv sichern")
    p.add_argument("dir", nargs="?", help="Zielverzeichnis (Default: <projekt>/backup)")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("render", help="Notizbuch → PDF (Handschrift als Bild)")
    p.add_argument("path")
    p.add_argument("-o", "--out", help="Ausgabe-PDF")
    p.set_defaults(fn=cmd_render)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
