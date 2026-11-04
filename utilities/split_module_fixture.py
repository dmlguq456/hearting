"""Test support for modules split into `<module>_*.py` parts.

`artifact_producer.py` and `dispatch_contract.py` re-export every name their
parts define, but each part looks names up in its own globals, so
`mock.patch.object(module, name)` no longer reaches code that moved.
`patch_split(module, name, ...)` and `split_patch("module.name", ...)` take the
same arguments as `mock.patch.object` and `mock.patch` and install the one
replacement in the module and in every part that binds the same object --
what a single patch did before the split. They work as context managers,
decorators, and with `start()`/`stop()`, and nest like ordinary patches.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_MISSING = object()


def split_parts(module):
    """The loaded parts of `module` (whatever name `module` was loaded under).

    A part is a `<stem>_*.py` module beside it (`-` in the stem read as `_`) whose docstring says
    "Split out of `<stem>.py`"; other modules with a common prefix (for example
    `artifact_locator_amendment.py` beside `artifact_locator.py`) are not parts.
    """
    file = getattr(module, "__file__", None)
    if not file:
        return []
    path = Path(file).resolve()
    # `capability-route.py` has parts named `capability_route_*.py`.
    prefix, marker = path.stem.replace("-", "_") + "_", f"Split out of `{path.name}`"
    found = []
    for loaded in list(sys.modules.values()):
        other_file = getattr(loaded, "__file__", None)
        if loaded is module or not other_file:
            continue
        other = Path(other_file).resolve()
        if other.parent == path.parent and other.stem.startswith(prefix) and marker in (loaded.__doc__ or ""):
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
