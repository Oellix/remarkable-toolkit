#!/usr/bin/env python3
"""pull.py — Daten von der reMarkable-Cloud abziehen.

    pull.py list [PFAD]                 # Cloud-Inhalt auflisten
    pull.py get  "Schnellnotiz" -o ./   # ein Dokument als .rmdoc holen
    pull.py backup [DIR]                # ALLES rekursiv sichern
    pull.py render "Schnellnotiz" -o note.pdf   # Notizbuch -> PDF (Handschrift als Bild)

Jedes Unterkommando akzeptiert --json fuer strukturierte Ausgabe (Agenten/Cron):
    pull.py list /HERMES --json
    pull.py render "Schnellnotiz" --json

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

# READ-CONFINEMENT (P2 / Review H3 — OFFENE ENTSCHEIDUNG):
# pull.py ist bewusst READ-ONLY und läuft daher OHNE guard_write. Das heißt
# aber: 'backup' (mget /) und 'get'/'render'/'list' sehen den GANZEN Account,
# nicht nur den Schreib-Prefix eines Agenten (RM_ALLOWED_PREFIX). Für den
# Tom-Rollout ist NOCH NICHT entschieden, ob Lesen ebenfalls auf einen Prefix
# beschränkt werden soll (z. B. nur '/HERMES' listen/sichern). reMarkable-
# Device-Tokens sind nicht ordner-scopebar, ein Read-Confinement müsste also
# — analog zum Write-Guard — clientseitig in diesen Befehlen erzwungen werden
# (z. B. Pfad-Prefix-Filter auf list/get, prefix-beschränktes mget). Bis diese
# Entscheidung fällt, bleibt Lesen voller-Account-Scope. Schreiben/Löschen ist
# über rmlib.guard_write (send.py) bereits confined.
AUTH_HINT = "  (Fehlgeschlagen — Token abgelaufen? Neu anmelden, siehe SKILL.md)"
COLOR_NOTE = "Handschrift als Bild (kein Text); Farbe evtl. nicht enthalten."
_TYPE_MAP = {"DocumentType": "document", "CollectionType": "collection"}


def rmapi(args: list[str], cwd: str | None = None) -> int:
    rmlib.require(rmlib.RMAPI, "Erst 'bash scripts/setup.sh' ausführen.")
    # JSON-Modus: rmapis eigene Fortschrittsausgabe ("downloading: … OK") darf
    # NICHT auf unser stdout (dort lebt genau ein JSON-Objekt) → nach stderr.
    out = sys.stderr if rmlib.json_mode() else None
    return subprocess.run([rmlib.RMAPI, *args], env=rmlib.rm_env(), cwd=cwd,
                          stdout=out).returncode


def rmapi_capture(args: list[str], cwd: str | None = None) -> tuple[int, str]:
    """rmapi aufrufen und (rc, stdout) zurueckgeben — fuer -json-Auswertung."""
    rmlib.require(rmlib.RMAPI, "Erst 'bash scripts/setup.sh' ausführen.")
    p = subprocess.run([rmlib.RMAPI, *args], env=rmlib.rm_env(), cwd=cwd,
                       capture_output=True, text=True)
    return p.returncode, p.stdout


def _shape_entries(raw: list, path: str) -> list[dict]:
    """rmapi `-json ls`-Rohausgabe → stabiler Vertrag [{name,type,path,...}].
    Entkoppelt die Agent-Schnittstelle von rmapis internem Schema."""
    base = "/" + path.strip("/") if path.strip("/") else ""
    out = []
    for e in raw:
        name = e.get("name")
        out.append({
            "name": name,
            "type": _TYPE_MAP.get(e.get("type"), e.get("type")),
            "path": f"{base}/{name}",
            "id": e.get("id"),
            "modified": e.get("modifiedClient"),
            "starred": e.get("starred", False),
        })
    return out


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
    if rmlib.json_mode():
        rc, out = rmapi_capture(["-ni", "-json", "ls", a.path])
        if rc != 0:
            return rmlib.fail("auth_required", hint=rmlib.AUTH_HINT_URL, code=rc, path=a.path)
        try:
            raw = json.loads(out) if out.strip() else []
        except ValueError:
            return rmlib.fail("parse_error", code=1,
                              detail="rmapi -json ls lieferte kein lesbares JSON")
        rmlib.emit({"ok": True, "path": a.path, "entries": _shape_entries(raw, a.path)})
        return 0
    rc = rmapi(["ls", a.path])
    if rc != 0:
        print(AUTH_HINT, file=sys.stderr)
    return rc


def cmd_get(a) -> int:
    out = a.out or os.getcwd()
    os.makedirs(out, exist_ok=True)
    rmlib.progress(f"→ Hole '{a.path}' nach {out} …")
    before = set(os.listdir(out))
    rc = rmapi(["get", a.path], cwd=out)
    if rc != 0:
        if rmlib.json_mode():
            return rmlib.fail("auth_required", hint=rmlib.AUTH_HINT_URL, code=rc)
        print(AUTH_HINT, file=sys.stderr)
        return rc
    new = [os.path.join(out, f) for f in os.listdir(out) if f not in before]
    target = max(new, key=os.path.getmtime) if new else None
    size = os.path.getsize(target) if target and os.path.exists(target) else 0
    rmlib.emit({"ok": True, "output_path": target, "bytes": size},
               human=f"✓ Geholt: {target or out}")
    return 0


def cmd_backup(a) -> int:
    out = a.dir or os.path.join(rmlib.ROOT, "backup")
    os.makedirs(out, exist_ok=True)
    rmlib.progress(f"→ Vollbackup (rekursiv) nach {out} …")
    rc = rmapi(["mget", "/"], cwd=out)
    if rc != 0:
        if rmlib.json_mode():
            return rmlib.fail("auth_required", hint=rmlib.AUTH_HINT_URL, code=rc)
        print(AUTH_HINT, file=sys.stderr)
        return rc
    # Plausibilitaetscheck: Doc-Count ≠ 0 (sonst stilles Leerbackup, Roadmap §6).
    count = (len(glob.glob(os.path.join(out, "**", "*.rmdoc"), recursive=True))
             + len(glob.glob(os.path.join(out, "**", "*.zip"), recursive=True)))
    rmlib.emit({"ok": True, "output_dir": out, "item_count": count},
               human=f"✓ Backup nach {out} ({count} Dokument(e)).")
    return 0


def cmd_render(a) -> int:
    rmlib.require(rmlib.RMC, "rmc fehlt — 'bash scripts/setup.sh' ausführen.")
    tmp = tempfile.mkdtemp(prefix="rm-render-")
    try:
        rmlib.progress(f"→ Lade '{a.path}' …")
        if rmapi(["get", a.path], cwd=tmp) != 0:
            return rmlib.fail("auth_required", hint=rmlib.AUTH_HINT_URL, code=1,
                              human="FEHLER: Download fehlgeschlagen." + AUTH_HINT)
        docs = sorted(glob.glob(os.path.join(tmp, "*.rmdoc")) + glob.glob(os.path.join(tmp, "*.zip")))
        if not docs:
            return rmlib.fail("no_document", code=1,
                              human="FEHLER: kein .rmdoc/.zip erhalten.")
        extract = os.path.join(tmp, "extract")
        with zipfile.ZipFile(docs[0]) as z:
            z.extractall(extract)

        pages = _ordered_pages(extract)
        if not pages:
            return rmlib.fail(
                "no_handwriting", code=1,
                human="Keine handschriftlichen .rm-Seiten — bei reinen PDF/EPUB-"
                      "Dokumenten nutze 'get' oder 'backup'.")

        svgs, skipped = [], 0
        for i, rmf in enumerate(pages):
            svg = os.path.join(tmp, f"page{i:03d}.svg")
            r = subprocess.run([rmlib.RMC, "-t", "svg", "-o", svg, rmf],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and os.path.exists(svg) and os.path.getsize(svg) > 0:
                svgs.append(svg)
            else:
                skipped += 1
                rmlib.progress(f"  ⚠ Seite {i + 1}/{len(pages)} konnte nicht gerendert werden.")
        if not svgs:
            return rmlib.fail("render_failed", code=1, pages_skipped=skipped,
                              human="FEHLER: rmc konnte keine Seite rendern.")

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

        human = f"✓ {len(svgs)} Seite(n) gerendert → {out}"
        if skipped:
            human += f"  ({skipped} übersprungen)"
        human += f"\n  Hinweis: {COLOR_NOTE}"
        rmlib.emit({"ok": True, "output_path": out, "pages_rendered": len(svgs),
                    "pages_skipped": skipped, "color_note": COLOR_NOTE}, human=human)
        return 0
    except Exception as e:
        if rmlib.json_mode():
            return rmlib.fail("render_failed", detail=str(e))
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        help="strukturierte JSON-Ausgabe (für Agenten/Cron)")

    ap = argparse.ArgumentParser(description="Von reMarkable abziehen")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="Cloud-Inhalt auflisten", parents=[common])
    p.add_argument("path", nargs="?", default="/")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("get", help="ein Dokument als .rmdoc holen", parents=[common])
    p.add_argument("path")
    p.add_argument("-o", "--out", help="Zielverzeichnis")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("backup", help="ALLES rekursiv sichern", parents=[common])
    p.add_argument("dir", nargs="?", help="Zielverzeichnis (Default: <projekt>/backup)")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("render", help="Notizbuch → PDF (Handschrift als Bild)", parents=[common])
    p.add_argument("path")
    p.add_argument("-o", "--out", help="Ausgabe-PDF")
    p.set_defaults(fn=cmd_render)

    args = ap.parse_args(argv)
    rmlib.set_json_mode(args.json)
    try:
        return args.fn(args)
    except SystemExit as e:
        if rmlib.json_mode() and not isinstance(e.code, int):
            return rmlib.fail("precondition_failed", detail=str(e.code))
        raise
    except Exception as e:
        if rmlib.json_mode():
            return rmlib.fail("unexpected_error", detail=str(e))
        raise


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
