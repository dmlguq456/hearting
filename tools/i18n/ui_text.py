#!/usr/bin/env python3
"""Shared user-facing text: one catalog, English by default, Korean on request.

Language comes from the preference path Hearting already has
(`tools/fleet/config.py declared_language()`: `FLEET_NOW_LANG`, then
`fleet.json` `title_language`, then Claude's native `language` setting). With no
declaration the text is English; a Korean declaration (`ko`, `Korean`, `한국어`)
selects Korean; any other declared language also gets English because the
catalog carries only those two. No new setting exists.

Python callers use `translator()`; shell and JavaScript read the same data with
`python3 ui_text.py get <key> [name=value ...]` or by loading `messages.json`
directly (`{"messages": {key: {"en": ..., "ko": ...}}}`, `{name}` placeholders).
"""
from __future__ import annotations

import functools
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "messages.json"
DEFAULT_LANGUAGE = "en"
LANGUAGES = ("en", "ko")
_KOREAN_NAMES = {"ko", "kor", "korean", "한국어", "한글"}


def _declared_language() -> str:
    """The operator's declared language through Fleet's single reader, or ''."""
    try:
        spec = importlib.util.spec_from_file_location(
            "_hearting_ui_fleet_config", HERE.parent / "fleet" / "config.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.declared_language() or "")
    except Exception:  # a missing or broken preference never blocks a screen
        return ""


def normalize(declared: str | None) -> str:
    name = (declared or "").strip().lower().replace("_", "-")
    if name in _KOREAN_NAMES or name.startswith("ko-"):
        return "ko"
    return DEFAULT_LANGUAGE


def language(declared: str | None = None) -> str:
    """`en` or `ko`; resolves the declared preference unless one is passed."""
    return normalize(_declared_language() if declared is None else declared)


@functools.lru_cache(maxsize=1)
def _messages() -> dict:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    return data["messages"]


def text(message: str, lang: str = DEFAULT_LANGUAGE, /, **fields) -> str:
    """One message in `lang`; a missing Korean entry falls back to English."""
    entry = _messages()[message]
    template = entry.get(lang) or entry[DEFAULT_LANGUAGE]
    return template.format(**fields) if fields else template


def translator(declared: str | None = None):
    """`t(key, **fields)` bound to the resolved language (resolve once per screen)."""
    lang = language(declared)

    def t(message: str, /, **fields) -> str:
        return text(message, lang, **fields)

    t.lang = lang
    return t


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    lang_override = None
    if "--lang" in args:
        at = args.index("--lang")
        lang_override = args[at + 1]
        del args[at:at + 2]
    if args[:1] == ["lang"]:
        print(language(lang_override))
        return 0
    if args[:1] == ["get"] and len(args) >= 2:
        fields = dict(item.split("=", 1) for item in args[2:] if "=" in item)
        try:
            print(text(args[1], language(lang_override), **fields))
        except KeyError as exc:
            print(f"ui_text: unknown key or field {exc}", file=sys.stderr)
            return 2
        return 0
    print("usage: ui_text.py [--lang en|ko] lang | get <key> [name=value ...]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
