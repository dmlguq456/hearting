"""Optional, create-once operator preferences shared by all Fleet producers."""
import json
import os
from pathlib import Path
import time

_UI_RECHECK_SEC = 5.0      # a changed language preference reaches the screen within this long
_ui = {}                   # "module": tools/i18n/ui_text.py; "lang": (environment key, language, when)


def config_path():
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    if not root.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be absolute")
    return root / "hearting" / "fleet.json"


def _read():
    path = config_path()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Fleet config must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Fleet config must contain a JSON object") from exc
    language = value.get("title_language", "auto") if isinstance(value, dict) else None
    if (not isinstance(language, str) or not language.strip() or len(language) > 40
            or not all(ch.isalpha() or ch in " _-" for ch in language)):
        raise ValueError("title_language must be auto or a language name/code")
    return language.strip()


def title_language():
    try:
        return _read()
    except ValueError:
        return "auto"


def declared_language():
    """One operator preference for all producers; native settings stay read-only."""
    explicit = (os.environ.get("FLEET_NOW_LANG") or "").strip()
    if explicit and explicit.lower() != "auto":
        return explicit
    language = title_language()
    if language.lower() != "auto":
        return language
    # Claude currently exposes a native reply-language preference. It belongs
    # to the operator, so Codex/OpenCode Fleet rows use it too. Their native
    # config schemas expose no equivalent key; do not invent one.
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    try:
        path = root / "settings.json"
        if path.stat().st_size > 1 << 20:
            return ""
        value = json.loads(path.read_text(encoding="utf-8"))
        language = value.get("language") if isinstance(value, dict) else None
        if (isinstance(language, str) and 0 < len(language.strip()) <= 40
                and all(ch.isalpha() or ch in " _-" for ch in language)
                and language.strip().lower() != "auto"):
            return language.strip()
    except (OSError, UnicodeError, ValueError):
        pass
    return ""


def _ui_catalog():
    """The shared catalog reader ``tools/i18n/ui_text.py``, loaded once per process."""
    module = _ui.get("module")
    if module is None:
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "i18n" / "ui_text.py"
        spec = importlib.util.spec_from_file_location("_hearting_fleet_ui_text", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ui["module"] = module
    return module


def ui_language():
    """``en`` or ``ko`` for screen text, from ``declared_language()``.

    A paint asks for many strings, so the answer is kept for a few seconds instead of reading
    the preference files for each one; a different environment (a test, another HOME) asks again.
    """
    key = tuple(os.environ.get(name) for name in
                ("FLEET_NOW_LANG", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "HOME"))
    now = time.monotonic()
    held = _ui.get("lang")
    if held is None or held[0] != key or now - held[2] >= _UI_RECHECK_SEC:
        held = (key, _ui_catalog().normalize(declared_language()), now)
        _ui["lang"] = held
    return held[1]


def ui_text(message, /, **fields):
    """One screen string from ``tools/i18n/messages.json``: English unless Korean is declared.

    A screen never fails over its words: without a readable catalog the string names its fields.
    """
    try:
        return _ui_catalog().text(message, ui_language(), **fields)
    except Exception:  # noqa: BLE001 -- a missing or broken catalog never blocks a screen
        return " ".join([message.rsplit(".", 1)[-1].replace("_", " ")]
                        + [str(value) for value in fields.values()])


def ensure(*, dry_run=False):
    path = config_path()
    if path.exists() or path.is_symlink():
        return {"status": "preserved", "path": str(path)}
    if dry_run:
        return {"status": "would-create", "path": str(path)}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {"status": "preserved", "path": str(path)}
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write('{"title_language": "auto"}\n')
    return {"status": "created", "path": str(path)}


def validate():
    path = config_path()
    if not path.exists() and not path.is_symlink():
        return {"status": "absent", "ok": True, "path": str(path),
                "detail": "title uses NOW's operator-language selection"}
    try:
        language = _read()
    except ValueError as exc:
        return {"status": "invalid", "ok": False, "path": str(path), "detail": str(exc)}
    return {"status": "valid", "ok": True, "path": str(path), "detail": "title_language=" + language}
