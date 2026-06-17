#!/usr/bin/env python3
"""Out-of-band Verifikations-Harness fuer mcp_server.py (stdio, end-to-end).

NICHT Teil der unittest-Suite (Dateiname mit '_'-Praefix => kein discover-Match,
und wird ohnehin als Skript via main()/argv gefahren, nicht von unittest).
Treibt den ECHTEN Server-Prozess ueber stdin/stdout-Pipes, exakt wie ein
MCP-Client. Schreibt alle Nachrichten (eine JSON-Zeile pro Message) auf stdin,
schliesst stdin (=> EOF beendet die Server-Schleife sauber), und liest stdout
und stderr GETRENNT (kein Deadlock, keine Vermischung).

Usage:
    PYTHONPATH=scripts .venv/bin/python tests/_mcp_harness.py <mode>
      mode = handshake   # Offline: initialize + notif + tools/list
      mode = live_list   # tools/call rm_list path=/HERMES (braucht Env/Token)
      mode = deny_send   # tools/call rm_send dest=/Secret (Guard-Deny)

Exit-Code 0 = alle Assertions erfuellt, sonst 1. Maschinenlesbarer Report
(genau ein JSON-Objekt) geht auf stdout des Harness selbst.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
SERVER = os.path.join(ROOT, "scripts", "mcp_server.py")
EXPECTED_PROTOCOL = "2025-11-25"
EXPECTED_TOOLS = ["rm_list", "rm_get", "rm_render", "rm_backup", "rm_send"]


def run_server(messages: list[dict]) -> tuple[str, str, int]:
    """Feed messages (one JSON line each) to the server, close stdin, capture
    stdout+stderr separately. Returns (stdout, stderr, returncode)."""
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [VENV_PYTHON, SERVER],
        input=payload,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=120,
    )
    return proc.stdout, proc.stderr, proc.returncode


def parse_jsonrpc_lines(stdout: str) -> tuple[list[dict], list[str]]:
    """Parse each non-empty stdout line as JSON. Returns (objs, bad_lines).
    bad_lines is non-empty if ANY stdout line is not valid JSON (= stray log)."""
    objs, bad = [], []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            objs.append(json.loads(line))
        except ValueError:
            bad.append(line)
    return objs, bad


# --- Modes ------------------------------------------------------------------

def mode_handshake() -> dict:
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": EXPECTED_PROTOCOL,
                    "capabilities": {}, "clientInfo": {"name": "harness", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    out, err, rc = run_server(msgs)
    objs, bad = parse_jsonrpc_lines(out)

    checks: dict[str, object] = {}
    checks["server_exit_0"] = (rc == 0)
    checks["stdout_pure_jsonrpc"] = (len(bad) == 0)
    checks["bad_stdout_lines"] = bad
    # 3 messages in, exactly 2 responses out (notification = NO response).
    checks["response_count_is_2"] = (len(objs) == 2)
    checks["num_responses"] = len(objs)

    init = next((o for o in objs if o.get("id") == 1), None)
    tl = next((o for o in objs if o.get("id") == 2), None)

    checks["init_present"] = init is not None
    checks["tools_list_present"] = tl is not None

    if init is not None:
        checks["init_jsonrpc_2_0"] = (init.get("jsonrpc") == "2.0")
        checks["init_no_error"] = ("error" not in init)
        res = init.get("result", {})
        checks["init_protocolVersion_correct"] = (
            res.get("protocolVersion") == EXPECTED_PROTOCOL)
        checks["init_protocolVersion_value"] = res.get("protocolVersion")
        checks["init_has_capabilities"] = ("capabilities" in res)
        checks["init_has_serverInfo"] = (
            isinstance(res.get("serverInfo"), dict)
            and bool(res["serverInfo"].get("name")))

    if tl is not None:
        checks["tl_jsonrpc_2_0"] = (tl.get("jsonrpc") == "2.0")
        checks["tl_no_error"] = ("error" not in tl)
        tools = tl.get("result", {}).get("tools", [])
        names = [t.get("name") for t in tools]
        checks["tool_names"] = names
        checks["all_expected_tools_present"] = (
            sorted(names) == sorted(EXPECTED_TOOLS))
        # Every tool has a non-trivial object inputSchema.
        ok_schema = all(
            isinstance(t.get("inputSchema"), dict)
            and t["inputSchema"].get("type") == "object"
            and isinstance(t["inputSchema"].get("properties"), dict)
            for t in tools)
        checks["all_tools_have_object_inputSchema"] = ok_schema
        checks["all_tools_have_title_desc"] = all(
            bool(t.get("title")) and bool(t.get("description")) for t in tools)

    passed = all(v is True for k, v in checks.items()
                 if k not in ("bad_stdout_lines", "num_responses",
                              "init_protocolVersion_value", "tool_names"))
    return {"mode": "handshake", "passed": passed, "checks": checks,
            "stderr_len": len(err), "stdout_raw": out}


def _call_msgs(tool: str, arguments: dict) -> list[dict]:
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": EXPECTED_PROTOCOL, "capabilities": {},
                    "clientInfo": {"name": "harness", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool, "arguments": arguments}},
    ]


def mode_live_list() -> dict:
    out, err, rc = run_server(_call_msgs("rm_list", {"path": "/HERMES"}))
    objs, bad = parse_jsonrpc_lines(out)
    checks: dict[str, object] = {}
    checks["stdout_pure_jsonrpc"] = (len(bad) == 0)
    checks["bad_stdout_lines"] = bad
    checks["response_count_is_2"] = (len(objs) == 2)

    call = next((o for o in objs if o.get("id") == 2), None)
    checks["call_present"] = call is not None
    inner = None
    if call is not None:
        checks["call_no_jsonrpc_error"] = ("error" not in call)
        result = call.get("result", {})
        content = result.get("content", [])
        checks["has_text_content"] = (
            len(content) == 1 and content[0].get("type") == "text")
        checks["isError_field"] = result.get("isError")
        if content:
            try:
                inner = json.loads(content[0]["text"])
            except ValueError:
                inner = {"_unparsable": content[0].get("text")}
            checks["inner_payload"] = inner

    # Structured success = ok:true + entries list. auth_required = token caveat.
    inner = inner or {}
    checks["inner_ok_true"] = (inner.get("ok") is True)
    checks["inner_has_entries_list"] = isinstance(inner.get("entries"), list)
    checks["inner_error"] = inner.get("error")
    # stderr is where rmapi chatter / progress lives; stdout must stay clean.
    checks["stderr_present_ok"] = True  # informational; not a fail condition
    checks["stderr_excerpt"] = err[-400:] if err else ""

    structured_ok = (
        checks["stdout_pure_jsonrpc"] and checks["response_count_is_2"]
        and checks.get("has_text_content") is True
        and checks["inner_ok_true"] and checks["inner_has_entries_list"])
    auth_caveat = (inner.get("error") == "auth_required")
    return {"mode": "live_list", "passed": bool(structured_ok),
            "auth_caveat": auth_caveat, "checks": checks, "stdout_raw": out}


def mode_deny_send() -> dict:
    # Dummy .pdf so send.py reaches upload()->ensure_dest()->guard_write().
    # A missing source would short-circuit to fail("not_found") and NEVER
    # exercise the guard (false pass). Passthrough = shutil.copy, no Chrome.
    dummy = os.path.join(os.environ.get("TMPDIR", "/tmp"), "rm_guard_dummy.pdf")
    with open(dummy, "wb") as f:
        f.write(b"%PDF-1.4\n%%EOF\n")

    out, err, rc = run_server(
        _call_msgs("rm_send", {"source": dummy, "dest": "/Secret"}))
    objs, bad = parse_jsonrpc_lines(out)
    checks: dict[str, object] = {}
    checks["stdout_pure_jsonrpc"] = (len(bad) == 0)
    checks["bad_stdout_lines"] = bad
    checks["response_count_is_2"] = (len(objs) == 2)

    call = next((o for o in objs if o.get("id") == 2), None)
    checks["call_present"] = call is not None
    inner = {}
    if call is not None:
        checks["call_no_jsonrpc_error"] = ("error" not in call)
        result = call.get("result", {})
        checks["isError_true"] = (result.get("isError") is True)
        content = result.get("content", [])
        checks["has_text_content"] = (
            len(content) == 1 and content[0].get("type") == "text")
        if content:
            try:
                inner = json.loads(content[0]["text"])
            except ValueError:
                inner = {"_unparsable": content[0].get("text")}
            checks["inner_payload"] = inner

    # Discriminator: the guard fired => error == "precondition_failed" with a
    # detail mentioning the deny. error == "not_found" => guard NOT tested.
    err_code = inner.get("error")
    checks["inner_error"] = err_code
    checks["is_precondition_failed"] = (err_code == "precondition_failed")
    checks["is_NOT_not_found"] = (err_code != "not_found")
    detail = str(inner.get("detail", ""))
    checks["detail_mentions_deny"] = (
        ("außerhalb" in detail) or ("abgelehnt" in detail)
        or ("RM_ALLOWED_PREFIX" in detail))
    checks["detail"] = detail
    # No upload happened: rmapi never ran => no "uploading...OK" on stderr.
    low = (err or "").lower()
    checks["no_upload_chatter"] = not (
        ("uploading" in low) or ("creating" in low and "ok" in low))
    checks["stderr_excerpt"] = err[-400:] if err else ""

    passed = (
        checks["stdout_pure_jsonrpc"]
        and checks.get("isError_true") is True
        and checks["is_precondition_failed"]
        and checks["is_NOT_not_found"]
        and checks["detail_mentions_deny"]
        and checks["no_upload_chatter"])
    return {"mode": "deny_send", "passed": bool(passed), "checks": checks,
            "stdout_raw": out}


MODES = {"handshake": mode_handshake, "live_list": mode_live_list,
         "deny_send": mode_deny_send}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in MODES:
        sys.stderr.write(f"usage: _mcp_harness.py {{{'|'.join(MODES)}}}\n")
        return 2
    report = MODES[argv[0]]()
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
