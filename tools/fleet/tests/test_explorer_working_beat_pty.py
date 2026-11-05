"""Working accents keep changing in the real loop when the model is unchanged."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[3]


def pulse_model(model):
    """One open owner, one finished owner, and one owner without routes."""
    values = model.to_mapping()
    template = values["sessions"][0]
    values["sessions"] = [dict(template, tag=tag, title="A long session title " * 8,
                               branch=None, steward=False)
                          for tag in ("live", "closed", "none")]
    route = values["routes"][0]
    values["routes"] = [dict(route, id=tag, session_tag=tag, current=True, state=state,
                             stages=[dict(name="execute", st="active", r=1)])
                        for tag, state in (("live", "working"), ("closed", "done"))]
    values.update(questions=[], seats=[], ordering={})
    return type(model).from_mapping(values)


def styled_lines(capture):
    """Read the foreground and intensity runs emitted by capture-pane -e."""
    foreground, bold, dim = None, False, False
    lines, text, attributes = [], [], []
    for token in re.finditer(r"\x1b\[([0-9;]*)m|([^\x1b]+)", capture):
        if token.group(1) is not None:
            codes = [int(value or 0) for value in token.group(1).split(";")]
            index = 0
            while index < len(codes):
                code = codes[index]
                if code == 0:
                    foreground, bold, dim = None, False, False
                elif code == 1:
                    bold = True
                elif code == 2:
                    dim = True
                elif code == 22:
                    bold, dim = False, False
                elif code == 39:
                    foreground = None
                elif code in (38, 48) and index + 2 < len(codes) and codes[index + 1] == 5:
                    if code == 38:
                        foreground = ("indexed", codes[index + 2])
                    index += 2
                elif code in (38, 48) and index + 4 < len(codes) and codes[index + 1] == 2:
                    if code == 38:
                        foreground = ("rgb", *codes[index + 2:index + 5])
                    index += 4
                index += 1
        else:
            for character in token.group(2):
                if character == "\n":
                    lines.append(("".join(text), attributes))
                    text, attributes = [], []
                else:
                    text.append(character)
                    attributes.append((foreground, bold, dim))
    if text:
        lines.append(("".join(text), attributes))
    return lines


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for terminal attribute coverage")
class ExplorerWorkingBeatPtyTest(unittest.TestCase):
    def test_same_model_keeps_session_route_count_and_working_accents_pulsing(self):
        with tempfile.TemporaryDirectory(prefix="fleet-beat-") as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            evidence.mkdir()
            for name in ("state", "cache", "config", "tmp", "tmux", "bin"):
                (root / name).mkdir()
            herdr = root / "bin" / "herdr"
            herdr.write_text("#!/bin/sh\nexit 1\n")
            herdr.chmod(0o755)
            env = dict(os.environ, AGENT_HOME=str(ROOT), TERM="xterm-256color",
                       TMUX_TMPDIR=str(root / "tmux"), TMPDIR=str(root / "tmp"),
                       XDG_STATE_HOME=str(root / "state"), XDG_CACHE_HOME=str(root / "cache"),
                       XDG_CONFIG_HOME=str(root / "config"), FLEET_DEMO="1", FLEET_MOUSE="0",
                       FLEET_NO_INSTALL_FOLLOW="1", HERDR_SOCKET_PATH=str(root / "no-herdr.sock"),
                       PYTHONDONTWRITEBYTECODE="1", PATH=str(root / "bin") + os.pathsep + os.environ["PATH"])
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            command = ["tmux", "-L", "codex-1104"]

            def tmux(*args, check=True):
                return subprocess.run([*command, *args], env=env, capture_output=True,
                                      text=True, timeout=10, check=check).stdout

            # The existing fixture supplies a fixed, redacted view model and
            # synthetic basic observations. Keep its detail backend synthetic too.
            child = (
                "import sys;sys.path[:0]=[sys.argv[1]+'/tools',sys.argv[1]+'/utilities',"
                "sys.argv[1]+'/tools/fleet/tests'];"
                "from fleet import details;"
                "details.enrich=lambda snapshot,**kw:details.DetailSnapshot(details.state_key(snapshot),snapshot);"
                "from fleet.explorer import view_model;"
                "from test_explorer_working_beat_pty import pulse_model;"
                "load=view_model.load_fixture;"
                "view_model.load_fixture=lambda path:pulse_model(load(path));"
                "import pty_repo_viewport as fixture;"
                "fixture._child_main(sys.argv[2],explorer=True,refresh_interval=2.0)"
            )
            launch = shlex.join([sys.executable, "-c", child, str(ROOT), str(evidence)])
            targets = {}
            try:
                tmux("-f", "/dev/null", "new-session", "-d", "-s", "beat",
                     "-x", "140", "-y", "45", launch)
                socket = tmux("display-message", "-p", "#{socket_path}").strip()
                self.assertTrue(socket.startswith(str(root / "tmux")), socket)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    capture = tmux("capture-pane", "-p", "-e", "-t", "beat")
                    lines = styled_lines(capture)
                    for y, (text, attributes) in enumerate(lines):
                        count = re.search(r"ROUTES\s+(\d+ open)", text)
                        if count:
                            targets[(y, count.start(1))] = ("count", count.group(1), set())
                        if "[live]" in text or "[closed]" in text:
                            x = text.find("route 1")
                            if x >= 0:
                                kind = "live" if "[live]" in text else "closed"
                                targets[(y, x)] = (kind, "route 1", set())
                    if {value[0] for value in targets.values()} == {"count", "live", "closed"}:
                        break
                    time.sleep(0.05)
                self.assertEqual({value[0] for value in targets.values()}, {"count", "live", "closed"}, capture)
                for _sample in range(20):
                    capture = tmux("capture-pane", "-p", "-e", "-t", "beat")
                    lines = styled_lines(capture)
                    for (y, x), (_kind, label, states) in targets.items():
                        text, attributes = lines[y]
                        shown = text[x:x + len(label)]
                        if _kind == "live":
                            self.assertIn(shown, (label, " " * len(label)))
                            states.add(shown)
                        else:
                            self.assertEqual(shown, label)
                            states.add(attributes[x])
                    time.sleep(0.15)
                for kind in ("count", "live"):
                    observed = [states for value_kind, _label, states in targets.values() if value_kind == kind]
                    self.assertTrue(any(len(states) >= 2 for states in observed), (kind, observed))
                closed = [states for kind, _label, states in targets.values() if kind == "closed"]
                self.assertTrue(all(len(states) == 1 for states in closed), closed)
                state = json.loads((evidence / "state.json").read_text())
                self.assertGreaterEqual(state["collects"], 2)
                self.assertNotIn("blocked-side-effect", (evidence / "events.jsonl").read_text())
            finally:
                tmux("send-keys", "-t", "beat", "q", check=False)
                tmux("kill-server", check=False)


if __name__ == "__main__":
    unittest.main()
