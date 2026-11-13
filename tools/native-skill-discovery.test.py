"""Exercise native skill discovery in isolated installed runtimes."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import os
import base64
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


def git_env(home: Path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull})
    return env


def make_repo(path: Path, env):
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"],
                   env=env, check=True, capture_output=True, text=True)


def skill_text(name: str, description: str, body: str, *, explicit_name=True):
    name_line = f"name: {name}\n" if explicit_name else ""
    return f"---\n{name_line}description: {description}\n---\n{body}\n"


class NativeSkillDiscoveryTest(unittest.TestCase):
    def test_opencode_resolves_native_copy_and_keeps_user_skill_without_compat_disable(self):
        binary = shutil.which("opencode")
        if binary is None:
            self.skipTest("OpenCode CLI unavailable; native discovery not measured")
        with tempfile.TemporaryDirectory(prefix="opencode-native-skills-") as td:
            root = Path(td)
            home = root / "home"
            xdg = home / ".config"
            repo = root / "independent-repo"
            env = git_env(home)
            for path in (home, xdg):
                path.mkdir(parents=True, exist_ok=True)
            make_repo(repo, env)

            version = subprocess.run([binary, "--version"], cwd=repo, env=env,
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(version.returncode, 0, version.stderr)
            major = re.search(r"(\d+)\.\d+\.\d+", version.stdout)
            self.assertIsNotNone(major, version.stdout)
            modern = int(major.group(1)) >= 2
            native = repo / (".opencode/skills" if modern else "native-skills") / "code-execute" / "SKILL.md"
            compat = repo / (".claude/skills" if modern else "claude-compat-skills") / "code-execute" / "SKILL.md"
            user = xdg / "opencode" / "skills" / "user-supplied" / "SKILL.md"
            for path in (native, compat, user):
                path.parent.mkdir(parents=True, exist_ok=True)
            native_text = (ROOT / "adapters" / "opencode" / "skills" / "code-execute" / "SKILL.md").read_text(
                encoding="utf-8")
            compat_text = (ROOT / "skills" / "code-execute" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("name: code-execute", native_text.split("---", 2)[1])
            self.assertNotIn("name:", compat_text.split("---", 2)[1])
            native.write_text(native_text, encoding="utf-8")
            # Rebuild the audited pre-fix representation in an isolated fixture:
            # both trees had the same explicit OpenCode name and the parser chose
            # the Claude compatibility body from the later configured source.
            compat.write_text(compat_text.replace("---\n", "---\nname: code-execute\n", 1),
                              encoding="utf-8")
            user.write_text(skill_text("user-supplied", "user skill sentinel", "USER-SKILL"),
                            encoding="utf-8")
            env.update({
                "XDG_CONFIG_HOME": str(xdg),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_STATE_HOME": str(home / ".local" / "state"),
                "OPENCODE_CONFIG_CONTENT": json.dumps({"skills": (
                    [str(native.parent.parent), str(compat.parent.parent)] if modern else
                    {"paths": [str(native.parent.parent), str(compat.parent.parent)]})}),
            })
            env.pop("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", None)

            def discover():
                if not modern:
                    result = subprocess.run([binary, "debug", "skill"], cwd=repo, env=env,
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                    return json.loads(result.stdout)
                # A standalone one-request API snapshot can precede skill-plugin
                # settlement. Keep one private fixture server for the same 30s
                # deadline; never attach to the user's managed service.
                password = secrets.token_urlsafe(32)
                server_env = {**env, "OPENCODE_SERVER_PASSWORD": password,
                              "OPENCODE_SERVER_USERNAME": "opencode"}
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                with (root / "fixture-server.log").open("w") as log:
                    server = subprocess.Popen(
                        [binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                        cwd=repo, env=server_env, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True)
                    try:
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline:
                            request = urllib.request.Request(
                                f"http://127.0.0.1:{port}/api/skill",
                                headers={"Authorization": "Basic " + base64.b64encode(
                                    ("opencode:" + password).encode()).decode(),
                                    "x-opencode-directory": str(repo)})
                            try:
                                with urllib.request.urlopen(request, timeout=2) as response:
                                    payload = json.load(response)
                            except (urllib.error.URLError, TimeoutError):
                                time.sleep(0.1)
                                continue
                            self.assertEqual(Path(payload["location"]["directory"]).resolve(), repo.resolve())
                            entries = payload["data"]
                            if {"code-execute", "user-supplied"} <= {item["id"] for item in entries}:
                                return [{**item, "location": item["path"]} for item in entries]
                            time.sleep(0.1)
                        self.fail("OpenCode V2 did not discover both fixture skills within 30s")
                    finally:
                        if server.poll() is None:
                            server.terminate()
                        try:
                            server.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            server.kill()
                            server.wait(timeout=5)

            found = discover()
            duplicate_entries = [item for item in found if item.get("name") == "code-execute"]
            user_entries = [item for item in found if item.get("name") == "user-supplied"]
            self.assertEqual(len(duplicate_entries), 1)
            self.assertEqual(Path(duplicate_entries[0]["location"]).resolve(), compat.resolve())

            # V1 requires the explicit OpenCode name. V2 uses path-derived IDs
            # and its documented native-versus-compatibility source precedence.
            compat.write_text(compat_text, encoding="utf-8")
            if modern:
                # V2 uses path-derived IDs even without name frontmatter. Its
                # normal native source outranks automatic Claude compatibility;
                # the explicit bad override above is no longer configured.
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"skills": []})
            found = discover()
            native_entries = [item for item in found if item.get("name") == "code-execute"]
            user_entries = [item for item in found if item.get("name") == "user-supplied"]
            self.assertEqual(len(native_entries), 1)
            self.assertEqual(Path(native_entries[0]["location"]).resolve(), native.resolve())
            self.assertIn("Portable Contract", native_entries[0]["content"])
            self.assertNotIn("Claude compatibility copy", native_entries[0]["content"])
            self.assertEqual(len(user_entries), 1)
            self.assertEqual(Path(user_entries[0]["location"]).resolve(), user.resolve())

    def test_codex_debug_prompt_uses_its_native_name_and_path(self):
        binary = shutil.which("codex")
        if binary is None:
            self.skipTest("Codex CLI unavailable; native discovery not measured")
        with tempfile.TemporaryDirectory(prefix="codex-native-skills-") as td:
            root = Path(td)
            home = root / "home"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            repo = root / "independent-repo"
            env = git_env(home)
            env.update({"CODEX_HOME": str(codex_home), "XDG_CONFIG_HOME": str(home / ".config")})
            make_repo(repo, env)

            native = repo / ".agents" / "skills" / "code-execute" / "SKILL.md"
            decoy = repo / ".claude" / "skills" / "code-execute" / "SKILL.md"
            user = codex_home / "skills" / "user-supplied" / "SKILL.md"
            for path in (native, decoy, user):
                path.parent.mkdir(parents=True, exist_ok=True)
            native_text = (ROOT / "adapters" / "codex" / "skills" / "code-execute" / "SKILL.md").read_text(
                encoding="utf-8")
            native.write_text(native_text, encoding="utf-8")
            decoy.write_text(skill_text("code-execute", "Claude decoy", "CLAUDE-DECOY",
                                        explicit_name=False), encoding="utf-8")
            user.write_text(skill_text("user-supplied", "user skill sentinel", "CODEX-USER"),
                            encoding="utf-8")

            result = subprocess.run([binary, "debug", "prompt-input", "inspect skills"],
                                    cwd=repo, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            messages = json.loads(result.stdout)
            prompt = "\n".join(part.get("text", "") for message in messages
                                 for part in message.get("content", []) if isinstance(part, dict))
            description = re.search(r'^description:\s*"?([^"\n]+)', native_text, re.MULTILINE).group(1)
            self.assertIn("- code-execute: " + description, prompt)
            self.assertIn("- user-supplied: user skill sentinel", prompt)
            self.assertNotIn("CLAUDE-DECOY", prompt)
            self.assertIn(".agents/skills", prompt)
            self.assertIn(str(repo / ".agents" / "skills"), prompt)

    def test_claude_folder_name_invokes_skill_on_loopback_without_api_access(self):
        binary = shutil.which("claude")
        if binary is None:
            self.skipTest("Claude Code CLI unavailable; native invocation not measured")

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(length)
                self.server.requests.append((self.path, payload))
                response = json.dumps({
                    "id": "msg_fixture", "type": "message", "role": "assistant",
                    "model": "fixture", "content": [{"type": "text", "text": "FIXTURE_OK"}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            with tempfile.TemporaryDirectory(prefix="claude-native-skills-") as td:
                root = Path(td)
                home, config, repo = root / "home", root / "config", root / "independent-repo"
                home.mkdir(); config.mkdir()
                env = git_env(home)
                env.update({
                    "XDG_CONFIG_HOME": str(config),
                    "CLAUDE_CONFIG_DIR": str(home / "claude-config"),
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "ANTHROPIC_API_KEY": "loopback-fixture",
                    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                })
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                    env.pop(key, None)
                make_repo(repo, env)
                skill = repo / ".claude" / "skills" / "code-execute" / "SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text(skill_text("code-execute", "Claude fixture skill", "CLAUDE-BODY-SENTINEL",
                                            explicit_name=False), encoding="utf-8")
                result = subprocess.run(
                    [binary, "--print", "--model", "claude-3-5-haiku-latest",
                     "--setting-sources", "project", "--no-session-persistence",
                     "--tools", "", "--output-format", "json", "/code-execute"],
                    cwd=repo, env=env, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                self.assertGreaterEqual(len(server.requests), 1)
                sent = b"\n".join(payload for _path, payload in server.requests)
                self.assertIn(b"CLAUDE-BODY-SENTINEL", sent)
                self.assertIn(b"/code-execute", sent)
                self.assertIn("FIXTURE_OK", result.stdout)
                self.assertTrue(all(path.startswith("/v1/messages") for path, _ in server.requests))
        finally:
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
