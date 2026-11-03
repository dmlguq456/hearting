"""Fleet's helper processes never share the viewer's keyboard (GH#840).

The live viewer reads keys from its terminal on stdin. A child that inherits that
stdin competes for the same bytes: the compute-host probe's ssh forwarded the
keys typed into Fleet, including ``q``, to the remote shell, so Fleet missed
navigation keys and kept running after ``q``. Every child Fleet starts therefore
names its own stdin.
"""
import ast
import unittest
from pathlib import Path


FLEET = Path(__file__).resolve().parents[1]
SPAWNS = {"run", "Popen", "check_output", "call", "check_call"}


def _spawns(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if ((isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
             and func.value.id == "subprocess" and func.attr in SPAWNS)
                or (isinstance(func, ast.Name) and func.id == "runner")):
            yield node


class ChildStdinTest(unittest.TestCase):
    def test_every_child_names_its_own_stdin(self):
        inherited = []
        for path in sorted(FLEET.rglob("*.py")):
            if "tests" in path.relative_to(FLEET).parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for call in _spawns(tree):
                # ``input=`` gives the child a pipe; ``**kwargs`` is the caller's choice.
                if not any(keyword.arg in ("stdin", "input", None)
                           for keyword in call.keywords):
                    inherited.append("%s:%d" % (path.relative_to(FLEET), call.lineno))
        self.assertEqual(inherited, [],
                         "these children would read the keys typed into Fleet")


if __name__ == "__main__":
    unittest.main()
