#!/usr/bin/env python3
"""Behaviour scenario: a design MCP tool call in a noninteractive run (PG-23).

Loaded by ``harness_behavior_scenarios.py`` (``_load_extra``).

    ``design-mcp-exec``  headless (Claude ``-p``, Codex ``exec``, OpenCode
                         ``run``): the model calls the ``preview`` tool of an
                         MCP server registered as ``design``; the oracle is the
                         server's own call log and the tool result reaching the
                         model.

The server is a loopback stdio MCP server written into the fixture root with
the same registration shape as ``tools/design-mcp/server.js`` (server id
``design``, tool ``preview``), so the run needs no Node packages or browser; it
measures the approval path, not rendering. Codex gets the config keys release
activation sets (``_merge_codex_managed_keys``); a source without them measures
as it is. Everything stays in the fixture root.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_scenarios as base  # noqa: E402
from harness_behavior_scenarios import Reply, Scenario  # noqa: E402
import harness_behavior_cases_b as cb  # noqa: E402
import harness_behavior_cases_e as ce  # noqa: E402
from harness_behavior_cases_subagent_model import MANAGED_KEYS, _source  # noqa: E402

PG = {"design-mcp-exec": "PG-23"}

SERVER = r'''
import json, sys
log = sys.argv[1]
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "design", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
            "name": "preview", "description": "Load an HTML file for rendering",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                            "required": ["path"]}}]}})
    elif method == "tools/call":
        with open(log, "a") as fh:
            fh.write(json.dumps(msg.get("params", {}).get("arguments")) + "\n")
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": "DESIGN-MCP-RENDERED"}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
'''


def preview_call(path: str) -> cb.NativeCall:
    def build(protocol, names):
        if protocol == "anthropic":
            return "mcp__design__preview", {"path": path}
        if protocol == "responses":
            return f"mcp__design{ce.NAMESPACE_SEP}preview", {"path": path}
        # OpenCode V2 exposes MCP tools through its Code Mode `execute` tool.
        return "execute", {"code": f"return await tools.design.preview({{path: {json.dumps(path)}}})"}
    return cb.NativeCall("design-preview", build)


class DesignMcpExecScenario(Scenario):
    name = "design-mcp-exec"
    summary = ("Headless: the model calls the design MCP server's preview tool; the call must run "
               "and its result reach the model.")
    needs_tui = False

    def __init__(self):
        self.marker = f"DESIGN-MCP-{uuid.uuid4().hex[:8]}"

    def prepare(self, run):
        root = run.home.root
        server = root / "design-mcp-probe.py"
        server.write_text(SERVER, encoding="utf-8")
        self.log = root / "design-mcp-calls.log"
        command = [sys.executable, str(server), str(self.log)]
        run.evidence["registration"] = getattr(self, f"_register_{run.harness}")(run, command)

    def _register_claude(self, run, command):
        config = run.home.root / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {"design": {
            "command": command[0], "args": command[1:]}}}), encoding="utf-8")
        self.extra = ["--mcp-config", str(config)]
        return {"mcp_config": str(config)}

    def _register_codex(self, run, command):
        self.extra = []
        config = Path(run.home.env["CODEX_HOME"]) / "config.toml"
        # The fixture's `danger-full-access` runs every MCP call unasked; a normal
        # `codex exec` runs sandboxed, where `approval_policy = "never"` rejects a
        # call that needs approval. Measure that posture.
        text = config.read_text(encoding="utf-8").replace(
            'sandbox_mode = "danger-full-access"', 'sandbox_mode = "workspace-write"')
        text += ("\n[mcp_servers.design]\ncommand = " + json.dumps(command[0])
                 + "\nargs = " + json.dumps(command[1:]) + "\n")
        config.write_text(text, encoding="utf-8")
        done = subprocess.run([sys.executable, "-B", "-c", MANAGED_KEYS, str(_source()), str(config)],
                              env=run.home.env, text=True, capture_output=True, timeout=120)
        merged = config.read_text(encoding="utf-8")
        return {"managed_keys": done.stdout.strip()[-300:] or done.stderr[-300:],
                "approval_line": next((line for line in merged.splitlines()
                                       if line.startswith("default_tools_approval_mode")), None)}

    def _register_opencode(self, run, command):
        self.extra = []
        path = Path(run.home.env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["mcp"] = {"design": {"type": "local", "command": command, "enabled": True}}
        path.write_text(json.dumps(config, indent=1), encoding="utf-8")
        return {"opencode_json": str(path)}

    def script(self, run):
        def answer(record, earlier):
            if self.marker not in record["text"]:
                return Reply("OK")
            if record["last_kind"] == "tool":
                return Reply("design loop done")
            called = any(str(r["reply"].get("shell") or "") == "design-preview" for r in earlier)
            # OpenCode lists an MCP server's tools in its Code Mode catalog once the server
            # has connected; a short shell step lets that listing arrive first.
            waits = sum(1 for r in earlier if self.marker in r["text"]
                        and str(r["reply"].get("shell") or "").startswith("sleep"))
            if not called and record["protocol"] == "chat" and "tools.design" not in record["text"] \
                    and waits < 5:
                return Reply(shell="sleep 3")
            if not called:
                return Reply(shell=preview_call(str(run.home.workspace / "index.html")))
            return Reply("design loop done")
        return answer

    def drive(self, run):
        (run.home.workspace / "index.html").write_text("<h1>probe</h1>\n", encoding="utf-8")
        # `--mcp-config` takes several values: keep it ahead of the other flags.
        argv = {"claude": [run.binary, *self.extra, "-p", "--model", base.FIXTURE_MODEL,
                           "--permission-mode", "bypassPermissions"],
                "codex": [run.binary, "exec", "--skip-git-repo-check"],
                "opencode": [run.binary, "run"]}[run.harness]
        done = subprocess.run([*argv, f"{self.marker} render index.html"], env=run.home.env,
                              cwd=str(run.home.workspace), text=True, capture_output=True,
                              timeout=240, stdin=subprocess.DEVNULL)
        time.sleep(1)
        calls = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        mine = [r for r in run.main_requests() if self.marker in r["text"]]
        # The tool result is the request right after the one answered with the preview call.
        results = [after["last_text"][-200:] for before, after in zip(mine, mine[1:])
                   if str(before["reply"].get("shell") or "") == "design-preview"
                   and after["last_kind"] == "tool"]
        reached = any("DESIGN-MCP-RENDERED" in text for text in results)
        ev = run.evidence
        ev.update({"exit": done.returncode, "server_calls": len(calls), "result_reached_model": reached,
                   "tool_results": results[:2], "stderr_tail": done.stderr[-300:]})
        run.note(f"server_calls={len(calls)} result_reached_model={reached}"
                 + (f"; tool result: {results[0][-90:]!r}" if results and not reached else ""))
        if not results:
            run.note("the model never got a tool result")
            return "fail"
        return "pass" if calls and reached else "gap"


SCENARIOS = [DesignMcpExecScenario]
