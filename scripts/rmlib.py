#!/usr/bin/env python3
"""Gemeinsame Bausteine der reMarkable-Skill.

Enthält die Pfade zu den Engines (rmapi, rmc, Chrome) und die Render-Kette
Markdown/HTML -> bildschirmoptimiertes PDF. send.py und pull.py bauen darauf auf.

WICHTIG: Mit dem venv-Python ausführen, z. B.
    <repo>/.venv/bin/python <repo>/scripts/send.py ...
da hier markdown/trafilatura/rmscene gebraucht werden.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time

# --- Pfade (Projekt-Wurzel = ein Verzeichnis über scripts/, dynamisch) ------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RMAPI = os.path.join(ROOT, "bin", "rmapi")
RMAPI_CONFIG = os.path.join(ROOT, ".rmapi.conf")
RMC = os.path.join(ROOT, ".venv", "bin", "rmc")

# Seitenformat des erzeugten PDFs. Default passt zum reMarkable Paper Pro Move
# (7,3"). Für andere Modelle per Umgebungsvariable überschreiben, z. B.:
#   RM_PAGE_SIZE="157mm 210mm"  (rM / rM2, 10,3")
#   RM_PAGE_SIZE="179mm 239mm"  (reMarkable Paper Pro, 11,8")
PAGE_SIZE = os.environ.get("RM_PAGE_SIZE", "100mm 178mm")

HTML_TEMPLATE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><style>
@page {{ size: {size}; margin: 8mm 8mm 10mm 8mm; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
  font-size: 9.5pt; line-height: 1.5; color: #1a1a1a;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
h1 {{ font-size: 16pt; color: #0b3d5c; border-bottom: 2px solid #0b3d5c;
  padding-bottom: 3px; margin: 0 0 8px; }}
h2 {{ font-size: 12.5pt; color: #0b3d5c; margin: 14px 0 5px; }}
h3 {{ font-size: 10.5pt; color: #15506e; margin: 11px 0 4px; }}
p {{ margin: 0 0 7px; }}
ul, ol {{ margin: 0 0 7px; padding-left: 18px; }}
li {{ margin: 2px 0; }}
code {{ font-family: "SF Mono", Menlo, monospace; font-size: 8.4pt;
  background: #eef2f5; padding: 0.5px 3px; border-radius: 3px; }}
pre {{ background: #f4f6f8; border: 1px solid #d4dde3; border-left: 3px solid #0b3d5c;
  border-radius: 4px; padding: 7px 9px; white-space: pre-wrap; word-wrap: break-word; }}
pre code {{ background: none; padding: 0; font-size: 8pt; line-height: 1.4; }}
blockquote {{ margin: 7px 0; padding: 3px 9px; border-left: 3px solid #c0683a;
  background: #faf2ed; color: #5b3a22; }}
table {{ border-collapse: collapse; width: 100%; font-size: 8.4pt; margin: 7px 0; }}
th, td {{ border: 1px solid #c4ced4; padding: 3px 6px; text-align: left; }}
th {{ background: #0b3d5c; color: #fff; }}
a {{ color: #0b66c2; text-decoration: none; }}
hr {{ border: none; border-top: 1px solid #ccc; margin: 9px 0; }}
</style></head><body>
{body}
</body></html>"""


def rm_env() -> dict:
    """Umgebung für rmapi-Aufrufe (zeigt auf das projekt-lokale Token)."""
    return dict(os.environ, RMAPI_CONFIG=RMAPI_CONFIG)


def find_chrome() -> str:
    """Findet eine Chrome/Chromium-Binary plattformübergreifend (macOS & Linux).
    Override per Umgebungsvariable CHROME_BIN. Bricht mit klarer Meldung ab,
    wenn nichts gefunden wird."""
    env = os.environ.get("CHROME_BIN")
    if env and os.path.isfile(env):
        return env
    mac_apps = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ]
    for path in mac_apps:
        if os.path.isfile(path):
            return path
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "brave-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("FEHLER: keine Chrome/Chromium-Binary gefunden.\n"
                     "       → Chrome/Chromium installieren oder CHROME_BIN=<pfad> setzen.")


def require(path: str, hint: str) -> None:
    """Bricht mit klarer Meldung ab, wenn ein benötigtes Programm fehlt (statt Traceback)."""
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise SystemExit(f"FEHLER: '{os.path.basename(path)}' nicht gefunden/ausführbar "
                         f"({path}).\n       → {hint}")


def safe_name(raw: str, fallback: str = "Dokument") -> str:
    """Macht einen Anzeige-/Dateinamen sicher: keine Slashes, keine Steuerzeichen,
    keine Pfad-Traversal. Schützt Schreibpfade und den Anzeigenamen auf dem Gerät."""
    cleaned = re.sub(r"[^\w\-. ()]+", "_", (raw or "")).strip(" .")
    return cleaned[:120] or fallback


def strip_scripts(html: str) -> str:
    """Entfernt <script>-Blöcke und inline on*=-Eventhandler. Verhindert, dass
    untrusted HTML/SVG (rohe HTML-Blöcke in .md, SVG aus der Cloud) beim
    Chrome-Druck aktiven Code ausführt — engine-unabhängige Defense-in-Depth."""
    html = re.sub(r"(?is)<script.*?</script\s*>", "", html)
    html = re.sub(r"(?is)<script[^>]*>", "", html)
    html = re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html)
    return html


def md_to_html(md_text: str) -> str:
    """Markdown -> vollständiges HTML mit bildschirmoptimiertem Stylesheet."""
    import markdown
    body = markdown.markdown(
        md_text,
        extensions=["fenced_code", "codehilite", "tables", "sane_lists"],
        extension_configs={"codehilite": {"noclasses": True}},
    )
    return HTML_TEMPLATE.format(size=PAGE_SIZE, body=strip_scripts(body))


def extract_article(url: str, forced_title: str | None = None):
    """URL -> (Markdown des Hauptartikels, Titel). Nutzt trafilatura."""
    import trafilatura
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise RuntimeError(f"URL konnte nicht geladen werden: {url}")
    md = trafilatura.extract(
        downloaded, output_format="markdown",
        include_links=True, include_images=False,
    )
    if not md:
        raise RuntimeError("Keine Artikel-Inhalte extrahiert (Paywall/JS-Seite?).")
    title = forced_title
    if not title:
        try:
            meta = trafilatura.extract_metadata(downloaded)
            title = meta.title if meta and meta.title else None
        except Exception:
            title = None
    title = title or "Artikel"
    return f"# {title}\n\n{md}", title


def _pdf_complete(path: str) -> bool:
    """True, wenn die Datei existiert und mit dem PDF-Endmarker %%EOF endet.
    Verhindert, dass ein noch nicht fertig geschriebenes PDF als 'fertig' gilt."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size <= 0:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - 1024))
            return b"%%EOF" in f.read()
    except OSError:
        return False


def html_to_pdf(html_path: str, pdf_path: str, timeout: int = 40) -> None:
    """Druckt HTML via Headless-Chrome zu PDF.

    Robust gegen das bekannte Problem, dass --headless=new sich nach dem Druck nicht
    beendet: wir pollen auf ein VOLLSTÄNDIGES PDF (Endmarker %%EOF) und beenden danach
    die Chrome-Prozessgruppe hart. So dauert ein Render ~2-3 s statt bis zum Timeout.
    (Aktiven Code im HTML neutralisiert strip_scripts() vorab — siehe md_to_html/pull.)
    """
    chrome = find_chrome()
    profile = tempfile.mkdtemp(prefix="rm-chrome-")
    if os.path.exists(pdf_path):
        os.remove(pdf_path)
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
         f"--user-data-dir={profile}", f"--print-to-pdf={pdf_path}", html_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if proc.poll() is not None:        # Chrome hat sich selbst beendet
                break
            if _pdf_complete(pdf_path):        # vollständiges PDF liegt vor
                break
            time.sleep(0.3)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)               # Zombie vermeiden
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
    if not _pdf_complete(pdf_path):
        raise RuntimeError("Chrome hat kein vollständiges PDF erzeugt.")


def md_to_pdf(md_text: str, pdf_path: str) -> None:
    """Markdown-Text -> bildschirmoptimiertes PDF."""
    work = tempfile.mkdtemp(prefix="rm-md-")
    try:
        html_path = os.path.join(work, "page.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(md_to_html(md_text))
        html_to_pdf(html_path, pdf_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
