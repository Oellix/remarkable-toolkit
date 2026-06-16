#!/usr/bin/env python3
"""send.py — Datei, URL oder Markdown an die reMarkable-Cloud senden.

    send.py bericht.pdf                       # PDF/EPUB direkt
    send.py notiz.md                          # Markdown -> bildschirm-PDF
    send.py notiz.md --dest /Lesestoff        # in einen (auch verschachtelten) Cloud-Ordner
    send.py https://example.com/x --name "X"  # Web-Artikel -> bildschirm-PDF

Regeln:
  * .pdf / .epub          -> Passthrough (direkt hochgeladen)
  * .md / .markdown / .txt-> Markdown -> bildschirmoptimiertes PDF
  * http(s)-URL           -> Artikel-Extraktion -> Markdown -> PDF
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

import rmlib

PASSTHROUGH = {".pdf", ".epub"}
MARKDOWN_EXT = {".md", ".markdown", ".txt"}


def ensure_dest(dest: str) -> None:
    """Legt den Cloud-Zielordner an — auch verschachtelt (/A/B/C), Segment für Segment.
    mkdir auf bereits existierende Ordner schlägt fehl; das ist erwartet und unschädlich."""
    path = ""
    for part in [p for p in dest.strip("/").split("/") if p]:
        path += "/" + part
        subprocess.run([rmlib.RMAPI, "mkdir", path], env=rmlib.rm_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def upload(path: str, dest: str) -> int:
    rmlib.require(rmlib.RMAPI, "Erst 'bash scripts/setup.sh' ausführen.")
    cmd = [rmlib.RMAPI, "put", path]
    if dest and dest != "/":
        ensure_dest(dest)
        cmd.append(dest)
    return subprocess.run(cmd, env=rmlib.rm_env()).returncode


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="An reMarkable senden (Datei/URL/Markdown)")
    ap.add_argument("source", help="Datei-Pfad oder http(s)-URL")
    ap.add_argument("--name", help="Anzeigename auf dem Gerät (optional)")
    ap.add_argument("--dest", default="/", help="Zielordner in der Cloud (Default: Wurzel)")
    ap.add_argument("--keep", metavar="PFAD", help="erzeugtes PDF zusätzlich hier ablegen")
    args = ap.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="rm-send-")
    try:
        src = args.source
        if src.startswith(("http://", "https://")):
            print("→ Web-Artikel extrahieren & rendern …")
            md, title = rmlib.extract_article(src, args.name)
            stem = rmlib.safe_name(args.name or title, "Artikel")
            target = os.path.join(tmp, f"{stem}.pdf")
            rmlib.md_to_pdf(md, target)
        elif os.path.isfile(src):
            ext = os.path.splitext(src)[1].lower()
            stem = rmlib.safe_name(args.name or os.path.splitext(os.path.basename(src))[0])
            if ext in PASSTHROUGH:
                target = os.path.join(tmp, f"{stem}{ext}")
                shutil.copy(src, target)
                print(f"→ {ext.upper()[1:]} direkt durchreichen …")
            elif ext in MARKDOWN_EXT:
                print("→ Markdown → bildschirm-PDF …")
                target = os.path.join(tmp, f"{stem}.pdf")
                with open(src, encoding="utf-8") as f:
                    rmlib.md_to_pdf(f.read(), target)
            else:
                print(f"FEHLER: Format '{ext}' wird nicht unterstützt "
                      "(pdf, epub, md, markdown, txt oder URL).", file=sys.stderr)
                return 2
        else:
            print(f"FEHLER: weder existierende Datei noch URL: {src}", file=sys.stderr)
            return 2

        if args.keep:
            shutil.copy(target, args.keep)
            print(f"  Kopie abgelegt: {args.keep}")

        print(f"→ Upload '{os.path.basename(target)}' → {args.dest}")
        rc = upload(target, args.dest)
        if rc == 0:
            print("✓ Erfolgreich gesendet.")
        else:
            print(f"✗ Upload-Fehler (rc={rc}). Token abgelaufen? Neu anmelden (siehe SKILL.md).",
                  file=sys.stderr)
        return rc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
