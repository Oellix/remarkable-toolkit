#!/usr/bin/env python3
"""Gemeinsame Bausteine der reMarkable-Skill.

Enthält die Pfade zu den Engines (rmapi, rmc, Chrome) und die Render-Kette
Markdown/HTML -> bildschirmoptimiertes PDF. send.py und pull.py bauen darauf auf.

WICHTIG: Mit dem venv-Python ausführen, z. B.
    <repo>/.venv/bin/python <repo>/scripts/send.py ...
da hier markdown/trafilatura/rmscene gebraucht werden.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# --- Pfade (Projekt-Wurzel = ein Verzeichnis über scripts/, dynamisch) ------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RMAPI = os.path.join(ROOT, "bin", "rmapi")
RMAPI_CONFIG = os.path.join(ROOT, ".rmapi.conf")
RMC = os.path.join(ROOT, ".venv", "bin", "rmc")

# --- Strukturierte Ausgabe (Agent-Vertrag, P1) -----------------------------
# Im --json-Modus ist stdout GENAU EIN JSON-Objekt; alle Fortschritts- und
# Diagnosezeilen gehen nach stderr. So kann ein Agent (Claude Code, Tom, Cron)
# die Rueckgabe maschinell auswerten. Ohne --json bleibt die menschenlesbare
# Prosa exakt wie bisher. send.py/pull.py reichen diesen Vertrag nur durch.
AUTH_HINT_URL = "https://my.remarkable.com/device/browser/connect"
AUTH_HINT_HUMAN = "Token abgelaufen? Neu anmelden — siehe SKILL.md (Connect-Code)."

_JSON_MODE = False


def set_json_mode(on: bool) -> None:
    """Schaltet die strukturierte Ausgabe global fuer diesen Prozess."""
    global _JSON_MODE
    _JSON_MODE = bool(on)


def json_mode() -> bool:
    return _JSON_MODE


def progress(msg: str) -> None:
    """Fortschritts-/Diagnosezeile. JSON-Modus → stderr (stdout bleibt reines
    JSON), sonst → stdout wie bisher."""
    print(msg, file=sys.stderr if _JSON_MODE else sys.stdout)


def emit(result: dict, human: str = "") -> None:
    """Erfolgs-Resultat ausgeben: im JSON-Modus genau ein JSON-Objekt auf
    stdout, sonst die menschenlesbare Zeile (falls angegeben)."""
    if _JSON_MODE:
        print(json.dumps(result, ensure_ascii=False))
    elif human:
        print(human)


def fail(error: str, *, hint: str = "", human: str = "", code: int = 1, **extra) -> int:
    """Fehler strukturiert ausgeben und den Exit-Code zurueckgeben. JSON-Modus →
    {"ok": false, "error": ...} auf stdout (Bash sieht zusaetzlich den
    Exit-Code). Sonst → Prosa auf stderr wie bisher. Rueckgabe = code, damit
    Commands `return rmlib.fail(...)` schreiben koennen."""
    if _JSON_MODE:
        obj = {"ok": False, "error": error}
        if hint:
            obj["hint"] = hint
        obj.update(extra)
        print(json.dumps(obj, ensure_ascii=False))
    else:
        line = human or f"FEHLER: {error}"
        if hint:
            line += f"\n  {hint}"
        print(line, file=sys.stderr)
    return code


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
    """Umgebung für rmapi-Aufrufe (zeigt auf das Token).

    Per-Agent-Token (P2): Ein extern gesetztes ``RMAPI_CONFIG`` wird RESPEKTIERT
    (``setdefault``) — so bekommt Tom über seine eigene Env eine eigene
    ``.rmapi.conf`` und damit ein unabhängig widerrufbares Device-Token. Nur wenn
    nichts gesetzt ist, fällt es auf das projekt-lokale ``<repo>/.rmapi.conf``
    zurück (Alex' interaktiver Default)."""
    env = dict(os.environ)
    env.setdefault("RMAPI_CONFIG", RMAPI_CONFIG)
    return env


# --- Schreib-Confinement (Guard, P2) ---------------------------------------
# RM_ALLOWED_PREFIX begrenzt JEDEN mutierenden Cloud-Pfad (put/mkdir/mv/rm/...)
# auf einen Ordner-Teilbaum. FAIL-CLOSED: unset/"" => DENY. Der einzige Weg zu
# Vollzugriff ist das Sentinel "ALL" (case-sensitive). Ein Prefix, das zu "/"
# normalisiert, => DENY (für unbeschränkt explizit ALL nutzen).
#
# EHRLICHE GRENZE (M1): Dieser Guard schützt vor VERSEHENTLICHEN
# out-of-prefix-Writes eines korrekt konfigurierten Wrappers — er hat ~null
# Adversary-Resistance. Wer das Token (.rmapi.conf) oder das nackte rmapi-Binary
# erreicht, hat trotzdem Vollzugriff (reMarkable-Device-Tokens sind unscoped).
# ECHTES Confinement = Per-Agent-Token (eigene .rmapi.conf via RMAPI_CONFIG,
# chmod 600, NICHT im Agent-CWD) + Tom darf NUR den fixen, geguardeten
# Wrapper-Befehl aufrufen (Hermes-Tool-Allowlist), niemals raw rmapi/Token.
RM_ALLOWED_PREFIX_ENV = "RM_ALLOWED_PREFIX"
_GUARD_SENTINEL_ALL = "ALL"  # case-sensitive: nur exakt "ALL" hebt Confinement auf

# Steuerzeichen sind in keinem legitimen Cloud-Pfad erlaubt — sie könnten
# Argumente/Logs verfälschen oder rmapi verwirren. Deckt C0 (\x00-\x1f inkl.
# \n\r\t\0), DEL + C1 (\x7f-\x9f, u. a. NEL U+0085) sowie die Unicode-Line/
# Paragraph-Separatoren U+2028/U+2029 ab (Log-/Argument-Hygiene, Hardening).
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

# Alt-Separatoren / Slash-Confusables: Backslash + Unicode-Slash-Look-alikes.
# reMarkable splittet Cloud-Pfade NUR an '/', daher ist keines dieser Zeichen je
# ein legitimer Separator — aber sie sind klassische Traversal-/Confusable-
# Vektoren (z. B. '/HERMES/..\..\evil', '/HERMES/..／evil'), die sonst als EIN
# Segment am '..'-Check vorbeischlüpfen. Defense-in-Depth: in Schreibpfaden
# ablehnen. (Betrifft nur Write — pull.py/Read nutzt _norm_cloud_path nicht.)
_ALT_SEP_RE = re.compile(r"[\\\u2044\u2215\u29f8\uff0f]")


def _norm_cloud_path(p: str) -> str:
    """Normalisiert einen Cloud-Pfad VALUE-AUTHORITATIV, ohne Auflösung.

    * Lehnt '..'-Segmente und Steuerzeichen/Newlines hart ab (raise SystemExit) —
      niemals stilles Droppen (C1). os.path.normpath wird NICHT benutzt, da es
      '..' still kollabieren würde.
    * Kollabiert mehrfache/führende/abschließende Slashes ('//A//B/' -> '/A/B').
    * Reiner String-Vorgang: keine Symlink-/CWD-Auflösung, kein Filesystem.

    Rückgabe ist immer absolut ('/...') bzw. '/' für die Wurzel.
    """
    raw = p or ""
    if _CTRL_CHARS_RE.search(raw):
        raise SystemExit(
            "FEHLER: Cloud-Pfad enthält Steuerzeichen/Newlines — abgelehnt.")
    if _ALT_SEP_RE.search(raw):
        raise SystemExit(
            "FEHLER: Cloud-Pfad enthält Backslash/Slash-Confusable — abgelehnt "
            "(nur '/' ist Separator; Defense-in-Depth gegen Traversal).")
    segments = [s for s in raw.split("/") if s != ""]
    for s in segments:
        if s == "..":
            raise SystemExit(
                f"FEHLER: '..'-Segment in Cloud-Pfad '{raw}' — abgelehnt "
                "(keine Pfad-Traversal).")
    return "/" + "/".join(segments) if segments else "/"


def _guard_base() -> str | None:
    """Liest RM_ALLOWED_PREFIX zur AUFRUFZEIT (nicht beim Import) und liefert die
    Confinement-Basis.

    * Sentinel exakt "ALL"  -> None  (kein Confinement, Vollzugriff).
    * unset oder ""         -> DENY  (raise; fail-closed).
    * normalisiert zu "/"   -> DENY  (raise; für unbeschränkt ALL nutzen).
    * sonst                 -> normalisierte Basis (z. B. "/HERMES").
    """
    prefix = os.environ.get(RM_ALLOWED_PREFIX_ENV)
    if prefix == _GUARD_SENTINEL_ALL:
        return None
    if not prefix:
        raise SystemExit(
            "FEHLER: RM_ALLOWED_PREFIX ist nicht gesetzt — Schreibzugriff "
            "verweigert (fail-closed).\n"
            "       → RM_ALLOWED_PREFIX=ALL für Vollzugriff (interaktiv) "
            "oder einen Ordner (Agent) setzen.")
    base = _norm_cloud_path(prefix)
    if base == "/":
        raise SystemExit(
            "FEHLER: RM_ALLOWED_PREFIX normalisiert zu '/' (gesamter Account) — "
            "abgelehnt.\n       → Für unbeschränkten Zugriff RM_ALLOWED_PREFIX=ALL "
            "nutzen, sonst einen konkreten Ordner.")
    return base


def guard_write(dest: str) -> str:
    """Erzwingt, dass ``dest`` AT-OR-BELOW RM_ALLOWED_PREFIX liegt und liefert den
    geprüften, normalisierten Cloud-Pfad zurück.

    Der RÜCKGABEWERT ist value-authoritativ (C1): Aufrufer geben AUSSCHLIESSLICH
    diesen String an rmapi weiter, niemals das rohe Argument.

    Regeln:
      * '..'/Steuerzeichen        -> raise (über _norm_cloud_path).
      * Prefix unset/""           -> raise (fail-closed, über _guard_base).
      * Sentinel "ALL"            -> kein Confinement: normalisierter dest zurück.
      * dest == base ODER dest.startswith(base + "/") -> erlaubt.
        (Substring-Schutz: base '/HERMES' lehnt '/HERMESX' ab, da kein '/'-Grenz.)
      * sonst                     -> raise (außerhalb des Prefix).
    """
    d = _norm_cloud_path(dest or "/")
    base = _guard_base()
    if base is None:               # Sentinel ALL → kein Confinement
        return d
    if d == base or d.startswith(base + "/"):
        return d
    raise SystemExit(
        f"FEHLER: Schreibziel '{d}' liegt außerhalb von "
        f"{RM_ALLOWED_PREFIX_ENV}='{base}' — abgelehnt.")


def rmapi_write(verb: str, cloud_paths: list[str] | None = None,
                local_first: str | None = None,
                extra: list[str] | None = None) -> int:
    """EINZIGER Chokepoint für MUTIERENDE rmapi-Verben (put/mkdir/mv/rm/restore).

    JEDES Cloud-Pfad-Argument läuft durch ``guard_write()``, BEVOR ein Subprozess
    startet — und nur der geguardete RÜCKGABEWERT geht an rmapi (C1/C2).

    Args:
      verb:        rmapi-Verb (z. B. "put", "mkdir", "mv", "rm").
      cloud_paths: Cloud-Pfade, die geguarded werden. Bei 'mv' BEIDE Pfade.
      local_first: Bei 'put' die LOKALE Quelldatei — wird NICHT geguarded und
                   steht als erstes Argument vor dem (geguardeten) Cloud-Ziel.
      extra:       zusätzliche rohe Flags (z. B. "-coverpage=1"); KEINE Cloud-Pfade.

    Returns: rmapi-Returncode.
    """
    require(RMAPI, "Erst 'bash scripts/setup.sh' ausführen.")
    guarded = [guard_write(p) for p in (cloud_paths or [])]
    argv = [RMAPI, verb]
    if extra:
        argv.extend(extra)
    if local_first is not None:    # 'put': lokale Datei roh, NICHT guarden
        argv.append(local_first)
    argv.extend(guarded)
    # JSON-Modus: rmapi-Chatter ("uploading/creating … OK") vom JSON-stdout
    # fernhalten (dort lebt genau ein JSON-Objekt) → nach stderr.
    out = sys.stderr if _JSON_MODE else None
    return subprocess.run(argv, env=rm_env(), stdout=out).returncode


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
