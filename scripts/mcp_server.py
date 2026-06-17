#!/usr/bin/env python3
"""mcp_server.py — MCP-stdio-Frontend für das reMarkable-Toolkit.

Spricht Model Context Protocol (Spec 2025-11-25) über JSON-RPC 2.0, newline-
delimited über stdin/stdout. Jedes Tool shellt die bestehende CLI (send.py /
pull.py) mit ``--json`` über den venv-Python und reicht deren EIN-JSON-Objekt
als Text-Content zurück. So bekommt ein MCP-Client (Claude Code, Tom/Hermes)
die geprüften, strukturierten Resultate des P1-Vertrags — ohne dass diese
Datei irgendeine Abhängigkeit braucht (NUR stdlib: sys/json/subprocess/os).

ARCHITEKTUR-INVARIANTEN
-----------------------
* **stdout des Servers ist AUSSCHLIESSLICH JSON-RPC.** Keine prints/Logs auf
  stdout. Diagnose ausnahmslos auf stderr. Der Kindprozess (send/pull) wird mit
  ``capture_output=True`` aufgerufen: dessen stdout (genau ein JSON-Objekt)
  geht in den Text-Content, dessen stderr (Fortschritt/rmapi-Chatter) wird auf
  unser stderr durchgereicht — niemals vermischt.
* **Fehler = Exit-Code.** ``isError = (rc != 0)``. Guard-Denials erscheinen als
  rc=1 mit ``error:"precondition_failed"`` (send.py-SystemExit-Handler), sind
  also automatisch abgedeckt; kein Sonderfall nötig.
* **Env wird UNVERÄNDERT durchgereicht** (``os.environ``). So wirken
  ``RM_ALLOWED_PREFIX`` (Schreib-Confinement, fail-closed) und ``RMAPI_CONFIG``
  (Per-Agent-Token) im Kind. Dieser Server injiziert NIEMALS ``ALL`` — ein
  unconfiguriertes ``rm_send`` schlägt fail-closed im Kind fehl. (Nur der
  interaktive Shim ``bin/remarkable`` setzt ALL, wenn unset — bewusst die
  gegenteilige Policy; nicht vermischen.)
* **Notifications bekommen KEINE Antwort.** JSON-RPC-Nachrichten ohne ``id``
  (z. B. ``notifications/initialized``) werden verarbeitet und dann
  geschwiegen. Nur Requests (mit ``id``) bekommen genau eine Antwort.
* **Die Schleife crasht nie an einer einzelnen Nachricht.** Unlesbares JSON →
  Parse-Error-Antwort; Tool-Fehler → isError-Content; alles andere wird
  gefangen und als JSON-RPC-Fehler zurückgegeben.

Lauf (stdio):  <repo>/.venv/bin/python <repo>/scripts/mcp_server.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

# --- Pfade (Projekt-Wurzel = ein Verzeichnis über scripts/, dynamisch) ------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
SEND_PY = os.path.join(ROOT, "scripts", "send.py")
PULL_PY = os.path.join(ROOT, "scripts", "pull.py")

PROTOCOL_VERSION = "2025-11-25"  # aus Spec verifiziert (context7, 2026-06-17)
SERVER_NAME = "remarkable"
SERVER_VERSION = "0.1.0"

# JSON-RPC 2.0 Fehlercodes (nur die hier genutzten)
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603


# --- Tool-Definitionen ------------------------------------------------------
# Jedes Tool kennt: das Ziel-Skript, eine Funktion die (args-dict) -> argv[]
# baut (OHNE python/script-Präfix, OHNE --json — das hängt der Dispatcher an),
# sowie sein inputSchema (JSON-Schema). So bleibt die Liste single-source für
# tools/list UND tools/call.

def _send_argv(a: dict) -> list[str]:
    """send.py: positional <source>, optional --name/--dest. (Schreib-Tool —
    erbt RM_ALLOWED_PREFIX aus der Server-Env; Fail-closed greift im Kind.)"""
    argv = [str(a["source"])]
    if a.get("name"):
        argv += ["--name", str(a["name"])]
    # dest hat in send.py Default "/"; nur anhängen wenn explizit gesetzt,
    # damit das CLI-Default-Verhalten (und sein Guard) unverändert greift.
    if a.get("dest"):
        argv += ["--dest", str(a["dest"])]
    return argv


def _pull_argv(verb: str):
    """Erzeugt einen argv-Builder für ein pull.py-Unterkommando."""
    def build(a: dict) -> list[str]:
        argv = [verb]
        if verb == "list":
            argv.append(str(a.get("path", "/")))
        elif verb == "get":
            argv.append(str(a["path"]))
            if a.get("out"):
                argv += ["-o", str(a["out"])]
        elif verb == "render":
            argv.append(str(a["path"]))
            if a.get("out"):
                argv += ["-o", str(a["out"])]
        elif verb == "backup":
            if a.get("dir"):
                argv.append(str(a["dir"]))
        return argv
    return build


# Reihenfolge = Anzeigereihenfolge in tools/list.
TOOLS: dict[str, dict] = {
    "rm_list": {
        "script": PULL_PY,
        "build": _pull_argv("list"),
        "title": "reMarkable: Cloud-Inhalt auflisten",
        "description": (
            "Listet den Inhalt eines reMarkable-Cloud-Ordners "
            "(Default: Wurzel '/'). Read-only."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Cloud-Pfad (Default '/').",
                    "default": "/",
                },
            },
            "additionalProperties": False,
        },
    },
    "rm_get": {
        "script": PULL_PY,
        "build": _pull_argv("get"),
        "title": "reMarkable: Dokument holen",
        "description": (
            "Holt ein Dokument als .rmdoc in ein Zielverzeichnis. Read-only."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Cloud-Pfad/Name des Dokuments.",
                },
                "out": {
                    "type": "string",
                    "description": "Zielverzeichnis (Default: CWD).",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "rm_render": {
        "script": PULL_PY,
        "build": _pull_argv("render"),
        "title": "reMarkable: Notizbuch → PDF rendern",
        "description": (
            "Rendert ein handschriftliches Notizbuch zu PDF (Handschrift als "
            "Bild, kein Text). Read-only."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Cloud-Pfad/Name des Notizbuchs.",
                },
                "out": {
                    "type": "string",
                    "description": "Ausgabe-PDF-Pfad (optional).",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "rm_backup": {
        "script": PULL_PY,
        "build": _pull_argv("backup"),
        "title": "reMarkable: Vollbackup",
        "description": (
            "Sichert den GESAMTEN Account rekursiv in ein Verzeichnis "
            "(Default: <repo>/backup). Read-only."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dir": {
                    "type": "string",
                    "description": "Zielverzeichnis (Default: <repo>/backup).",
                },
            },
            "additionalProperties": False,
        },
    },
    "rm_send": {
        "script": SEND_PY,
        "build": _send_argv,
        "title": "reMarkable: Datei/URL/Markdown senden",
        "description": (
            "Sendet eine Datei (pdf/epub/md/txt) oder einen Web-Artikel "
            "(http[s]-URL) an die reMarkable-Cloud. SCHREIB-Tool: unterliegt "
            "RM_ALLOWED_PREFIX (fail-closed, wenn unkonfiguriert)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Lokaler Datei-Pfad oder http(s)-URL.",
                },
                "name": {
                    "type": "string",
                    "description": "Anzeigename auf dem Gerät (optional).",
                },
                "dest": {
                    "type": "string",
                    "description": "Cloud-Zielordner (Default Wurzel '/').",
                    "default": "/",
                },
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    },
}


# --- JSON-RPC-Bausteine -----------------------------------------------------

def _result(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tools_list() -> dict:
    """tools/list-Result: name/title/description/inputSchema je Tool."""
    return {
        "tools": [
            {
                "name": name,
                "title": spec["title"],
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            }
            for name, spec in TOOLS.items()
        ]
    }


def _run_tool(name: str, arguments: dict) -> dict:
    """Shellt die CLI mit --json und baut das tools/call-Result.

    Kind-stdout (genau ein JSON-Objekt) → Text-Content. Kind-stderr (Progress/
    rmapi-Chatter) → unser stderr. ``isError = (rc != 0)``. Lässt sich das
    Kind-stdout nicht als JSON parsen, wird ein synthetisches Fehlerobjekt als
    Text-Content gesetzt (isError=True) — der Server bleibt JSON-RPC-konform.
    """
    spec = TOOLS[name]
    try:
        cli_args = spec["build"](dict(arguments or {}))
    except KeyError as e:
        # Pflichtfeld fehlt (z. B. 'path'/'source') — sauberer Tool-Fehler,
        # kein Crash. (additionalProperties/required im Schema ist Client-Hint;
        # wir validieren defensiv hier nochmal.)
        payload = json.dumps(
            {"ok": False, "error": "invalid_arguments",
             "detail": f"Pflichtfeld fehlt: {e.args[0]}"},
            ensure_ascii=False)
        return {"content": [{"type": "text", "text": payload}], "isError": True}

    argv = [VENV_PYTHON, spec["script"], *cli_args, "--json"]
    # Env UNVERÄNDERT durchreichen → RM_ALLOWED_PREFIX + RMAPI_CONFIG wirken im
    # Kind. capture_output trennt stdout (JSON) von stderr (Chatter) sauber.
    # encoding="utf-8" explizit: die Kinder geben non-ASCII aus (ensure_ascii=
    # False → '→', Umlaute). So bleibt das Dekodieren locale-unabhängig, falls
    # Tom den Server unter C/POSIX-Locale startet (sonst UnicodeDecodeError).
    proc = subprocess.run(
        argv, env=os.environ.copy(), capture_output=True,
        text=True, encoding="utf-8")

    # Kind-Diagnose auf UNSER stderr spiegeln (nie auf stdout).
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()

    out = (proc.stdout or "").strip()
    if out:
        # Genau ein JSON-Objekt erwartet — validieren, sonst synthetisch fehlern.
        try:
            json.loads(out)
            text = out
        except ValueError:
            text = json.dumps(
                {"ok": False, "error": "bad_cli_output",
                 "detail": "CLI lieferte kein lesbares JSON auf stdout.",
                 "raw": out[:500]},
                ensure_ascii=False)
            return {"content": [{"type": "text", "text": text}], "isError": True}
    else:
        # Kein stdout (z. B. require()-Abbruch ohne JSON-Modus-Handler) — Exit-
        # Code in ein JSON-Objekt heben, damit der Client etwas Strukturiertes
        # bekommt statt leerem Content.
        text = json.dumps(
            {"ok": proc.returncode == 0, "error": "no_output",
             "detail": f"CLI ohne stdout beendet (rc={proc.returncode})."},
            ensure_ascii=False)

    return {"content": [{"type": "text", "text": text}],
            "isError": proc.returncode != 0}


# --- Dispatch ---------------------------------------------------------------

def handle(message: dict):
    """Verarbeitet EINE JSON-RPC-Nachricht. Rückgabe = Antwort-dict ODER None
    (None = keine Antwort senden, z. B. bei Notifications)."""
    req_id = message.get("id")
    method = message.get("method")
    is_notification = "id" not in message

    # Notifications (kein 'id') werden verarbeitet und bekommen NIE eine
    # Antwort — sonst Protokoll-Verletzung. notifications/initialized fällt
    # hierunter; wir nehmen sie still entgegen.
    if is_notification:
        return None

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": "reMarkable Toolkit",
                "version": SERVER_VERSION,
            },
        })

    if method == "tools/list":
        return _result(req_id, _tools_list())

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in TOOLS:
            # Unbekanntes Tool → JSON-RPC-Fehler (Methode existiert, Ziel nicht).
            return _error(req_id, _METHOD_NOT_FOUND, f"Unbekanntes Tool: {name!r}")
        try:
            return _result(req_id, _run_tool(name, arguments))
        except Exception as e:  # noqa: BLE001 — Loop darf nie crashen
            return _error(req_id, _INTERNAL_ERROR, f"Tool-Ausführung fehlgeschlagen: {e}")

    # ping ist im Spec ein leeres Result — billig zu unterstützen.
    if method == "ping":
        return _result(req_id, {})

    return _error(req_id, _METHOD_NOT_FOUND, f"Unbekannte Methode: {method!r}")


def _write(obj: dict) -> None:
    """Genau eine JSON-RPC-Zeile auf stdout schreiben + flushen (newline-
    delimited). stdout bleibt reines JSON-RPC."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    # Zeilenweise von stdin lesen, bis EOF (Pipe-Close → sauberes Ende).
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            # Unlesbares JSON → Parse-Error (id unbekannt → null), nie crashen.
            _write(_error(None, _PARSE_ERROR, "Parse error: kein gültiges JSON."))
            continue
        if not isinstance(message, dict):
            _write(_error(None, _INVALID_REQUEST, "Request muss ein JSON-Objekt sein."))
            continue
        try:
            response = handle(message)
        except Exception as e:  # noqa: BLE001 — letzte Verteidigung
            response = _error(message.get("id"), _INTERNAL_ERROR, f"Interner Fehler: {e}")
        if response is not None:
            _write(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
