#!/usr/bin/env python3
"""send.py — Datei, URL oder Markdown an die reMarkable-Cloud senden.

    send.py bericht.pdf                       # PDF/EPUB direkt
    send.py notiz.md                          # Markdown -> bildschirm-PDF
    send.py notiz.md --dest /Lesestoff        # in einen (auch verschachtelten) Cloud-Ordner
    send.py https://example.com/x --name "X"  # Web-Artikel -> bildschirm-PDF
    send.py notiz.md --dest /HERMES --json    # strukturierte Ausgabe fuer Agenten/Cron

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
    # JSON-Modus: rmapi-"put"-Chatter ("uploading: … OK") vom JSON-Kanal fernhalten.
    out = sys.stderr if rmlib.json_mode() else None
    return subprocess.run(cmd, env=rmlib.rm_env(), stdout=out).returncode


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="An reMarkable senden (Datei/URL/Markdown)")
    ap.add_argument("source", help="Datei-Pfad oder http(s)-URL")
    ap.add_argument("--name", help="Anzeigename auf dem Gerät (optional)")
    ap.add_argument("--dest", default="/", help="Zielordner in der Cloud (Default: Wurzel)")
    ap.add_argument("--keep", metavar="PFAD", help="erzeugtes PDF zusätzlich hier ablegen")
    ap.add_argument("--json", action="store_true",
                    help="strukturierte JSON-Ausgabe (für Agenten/Cron)")
    args = ap.parse_args(argv)
    rmlib.set_json_mode(args.json)

    tmp = tempfile.mkdtemp(prefix="rm-send-")
    try:
        src = args.source
        if src.startswith(("http://", "https://")):
            rmlib.progress("→ Web-Artikel extrahieren & rendern …")
            md, title = rmlib.extract_article(src, args.name)
            stem = rmlib.safe_name(args.name or title, "Artikel")
            target = os.path.join(tmp, f"{stem}.pdf")
            rmlib.md_to_pdf(md, target)
            doc_type = "article"
        elif os.path.isfile(src):
            ext = os.path.splitext(src)[1].lower()
            stem = rmlib.safe_name(args.name or os.path.splitext(os.path.basename(src))[0])
            if ext in PASSTHROUGH:
                target = os.path.join(tmp, f"{stem}{ext}")
                shutil.copy(src, target)
                doc_type = ext[1:]
                rmlib.progress(f"→ {ext.upper()[1:]} direkt durchreichen …")
            elif ext in MARKDOWN_EXT:
                rmlib.progress("→ Markdown → bildschirm-PDF …")
                target = os.path.join(tmp, f"{stem}.pdf")
                with open(src, encoding="utf-8") as f:
                    rmlib.md_to_pdf(f.read(), target)
                doc_type = "markdown"
            else:
                return rmlib.fail(
                    "unsupported_format", code=2, detail=ext,
                    human=f"FEHLER: Format '{ext}' wird nicht unterstützt "
                          "(pdf, epub, md, markdown, txt oder URL).")
        else:
            return rmlib.fail("not_found", code=2, detail=src,
                              human=f"FEHLER: weder existierende Datei noch URL: {src}")

        if args.keep:
            shutil.copy(target, args.keep)
            rmlib.progress(f"  Kopie abgelegt: {args.keep}")

        rmlib.progress(f"→ Upload '{os.path.basename(target)}' → {args.dest}")
        rc = upload(target, args.dest)
        if rc == 0:
            rmlib.emit({"ok": True, "uploaded": True, "display_name": stem,
                        "dest": args.dest, "doc_type": doc_type},
                       human="✓ Erfolgreich gesendet.")
            return 0
        # P1: rc≠0 wird wie bisher als Auth-Fall behandelt; feine Klassifizierung
        # (Auth vs. Offline vs. transient) ist Roadmap P4 (classify_rmapi_failure).
        return rmlib.fail(
            "auth_required", hint=rmlib.AUTH_HINT_URL, code=rc, uploaded=False,
            human=f"✗ Upload-Fehler (rc={rc}). {rmlib.AUTH_HINT_HUMAN}")
    except SystemExit as e:
        # require() o. Ä. mit Meldung → strukturierter Fehler statt leerem stdout.
        if rmlib.json_mode() and not isinstance(e.code, int):
            return rmlib.fail("precondition_failed", detail=str(e.code))
        raise
    except Exception as e:
        if rmlib.json_mode():
            return rmlib.fail("send_failed", detail=str(e))
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
