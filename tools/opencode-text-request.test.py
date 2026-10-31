"""Native OpenCode single-request fixture for Fleet text workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fleet import refresh_title as rt  # noqa: E402


class OpenCodeTextRequestTest(unittest.TestCase):
    def test_fleet_title_request_uses_one_loopback_model_call_with_fixed_title(self):
        binary = shutil.which("opencode")
        if binary is None:
            self.skipTest("OpenCode CLI unavailable; native request count not measured")
        if rt.opencode_runtime.is_v2(binary):
            # This suite asserts the V1 argv spelling (--pure/--dir/--variant and
            # --log-level). The V2 argv contract is covered by
            # tools/fleet/tests/test_text_provider_context.py.
            self.skipTest("V1 native argv fixture; V2 has a different argv contract")

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                self.server.requests.append((self.path, payload))
                model = payload.get("model", "fixture")
                chunks = [
                    {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1,
                     "model": model, "choices": [{"index": 0, "delta": {
                         "role": "assistant", "content": "TITLE: fixture topic\nNOW: one request"},
                         "finish_reason": None}]},
                    {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1,
                     "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ]
                response = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                            + "data: [DONE]\n\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="opencode-text-request-") as td:
                root = Path(td)
                home, xdg, state, workdir = (root / "home", root / "config", root / "state", root / "work")
                for path in (home, xdg, state, workdir):
                    path.mkdir(parents=True, exist_ok=True)
                for name in ("data", "cache", "state"):
                    (state / name).mkdir(parents=True, exist_ok=True)
                source_config = xdg / "opencode"
                source_config.mkdir()
                (source_config / "opencode.json").write_text(json.dumps({
                    "$schema": "https://opencode.ai/config.json",
                    "provider": {"fixture": {
                        "npm": "@ai-sdk/openai-compatible", "name": "loopback fixture",
                        "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                                    "apiKey": "fixture-only"},
                        "models": {"fixture": {"name": "fixture"}},
                    }},
                }), encoding="utf-8")
                env = os.environ.copy()
                env.update({
                    "HOME": str(home), "XDG_CONFIG_HOME": str(xdg),
                    "XDG_DATA_HOME": str(state / "data"), "XDG_CACHE_HOME": str(state / "cache"),
                    "XDG_STATE_HOME": str(state / "state"), "FLEET_TITLE_STATE_DIR": str(workdir),
                    "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                })
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                    env.pop(key, None)
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch.object(rt, "provider_settings",
                                          return_value={"model": "fixture/fixture", "budget": "low"}):
                    command = rt.provider_command("opencode", "single native title prompt",
                                                  home=home, workdir=workdir, profile="mini")
                self.assertIsNotNone(command)
                argv, stdin_text, _output = command
                self.assertEqual(stdin_text, "single native title prompt")
                self.assertEqual(argv[argv.index("--title") + 1], "fleet-titler")
                self.assertEqual(argv[argv.index("--log-level") + 1], "WARN")
                child_env = env.copy()
                for key, value in command.env.items():
                    if value is None:
                        child_env.pop(key, None)
                    else:
                        child_env[key] = value
                result = subprocess.run(argv, cwd=command.cwd, env=child_env, input=stdin_text,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertIn("TITLE: fixture topic", result.stdout)
                self.assertEqual(len(server.requests), 1)
                path, payload = server.requests[0]
                self.assertEqual(path, "/v1/chat/completions")
                self.assertEqual(payload["model"], "fixture")
                self.assertTrue(payload["stream"])
                serialized = json.dumps(payload)
                self.assertIn("single native title prompt", serialized)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
