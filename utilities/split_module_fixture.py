"""Test support for modules split into `<module>_*.py` parts.

`artifact_producer.py` and `dispatch_contract.py` re-export every name their
parts define, but each part looks names up in its own globals, so
`mock.patch.object(module, name)` no longer reaches code that moved.
`patch_split(module, name, ...)` and `split_patch("module.name", ...)` take the
same arguments as `mock.patch.object` and `mock.patch` and install the one
replacement in the module and in every part that binds the same object --
what a single patch did before the split. They work as context managers,
decorators, and with `start()`/`stop()`, and nest like ordinary patches.
`set_split(module, name, value)` is the plain assignment `module.name = value`
with the same reach.
"""
from __future__ import annotations

import sys
from unittest import mock

_MISSING = object()


def split_parts(module):
    """The loaded parts of `module` (whatever name `module` was loaded under).

    A part is a loaded module named `<stem>_*` (`-` in the stem read as `_`;
    for a package module, the last name component) whose docstring says
    "Split out of `<stem>.py`"; other modules with a common prefix (for example
    `artifact_locator_amendment` beside `artifact_locator`) are not parts.
    Only names and docstrings are read -- never the filesystem, which a test
    may have patched (for example `os.readlink`) while it enters a patch.
    """
    file = getattr(module, "__file__", None)
    if not isinstance(file, str) or not file:
        return []
    name = file.replace("\\", "/").rpartition("/")[2]
    stem = name[:-3] if name.endswith(".py") else name
    prefix, marker = stem.replace("-", "_") + "_", f"Split out of `{name}`"
    found = []
    for loaded in list(sys.modules.values()):
        if loaded is None or loaded is module:
            continue
        short = str(getattr(loaded, "__name__", "")).rpartition(".")[2]
        doc = getattr(loaded, "__doc__", None)
        if short.startswith(prefix) and isinstance(doc, str) and marker in doc:
            found.append(loaded)
    return sorted(found, key=lambda loaded: loaded.__name__)


class _SplitPatch(mock._patch):
    def __enter__(self):
        result = super().__enter__()
        replacement = getattr(self.target, self.attribute)
        self._split_restore = []
        for part in split_parts(self.target):
            current = vars(part).get(self.attribute, _MISSING)
            if current is not _MISSING and current is self.temp_original:
                self._split_restore.append((part, current))
                setattr(part, self.attribute, replacement)
        return result

    def __exit__(self, *exc_info):
        for part, original in reversed(getattr(self, "_split_restore", [])):
            setattr(part, self.attribute, original)
        self._split_restore = []
        return super().__exit__(*exc_info)


def patch_split(module, name, *args, **kwargs):
    """`mock.patch.object(module, name, ...)` applied to `module` and its parts."""
    patcher = mock.patch.object(module, name, *args, **kwargs)
    patcher.__class__ = _SplitPatch
    return patcher


def split_patch(target, *args, **kwargs):
    """`mock.patch("module.name", ...)` applied to the module and its parts."""
    patcher = mock.patch(target, *args, **kwargs)
    patcher.__class__ = _SplitPatch
    return patcher


def set_split(module, name, value):
    """`module.name = value`, also in every part that binds the current value."""
    current = getattr(module, name, _MISSING)
    if current is not _MISSING:
        for part in split_parts(module):
            if vars(part).get(name, _MISSING) is current:
                setattr(part, name, value)
    setattr(module, name, value)
