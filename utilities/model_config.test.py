#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_model_config", ROOT / "utilities" / "model_config.py"
)
assert SPEC and SPEC.loader
config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = config
SPEC.loader.exec_module(config)


BASE = (
    'CFG_MODEL_PROFILE_DEEP=deep:xhigh\n'
    'CFG_TIER_DEEP_MODEL=shipped-model\n'
    'CFG_TIER_DEEP_EFFORT=xhigh\n'
)


class ModelConfigTest(unittest.TestCase):
    def test_capacity_probe_defaults_do_not_replace_existing_user_policy(self):
        root = self.make_root(shipped=BASE + 'CFG_CAPACITY_PROBE_MODELS="all:haiku opus:opus sonnet:sonnet"\n')
        with tempfile.TemporaryDirectory() as home:
            user = Path(home) / "agent-config/models.conf"
            user.parent.mkdir()
            user.write_text(BASE.replace("shipped-model", "user-model"))
            values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
            self.assertEqual(receipt.source, "user")
            self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "user-model")

    def make_root(self, adapter="claude", shipped=BASE):
        root = Path(tempfile.mkdtemp())
        shipped_path = root / "adapters" / adapter / "config" / "models.conf"
        shipped_path.parent.mkdir(parents=True)
        shipped_path.write_text(shipped, encoding="utf-8")
        return root

    def test_each_runtime_home_is_independent(self):
        for adapter, variable, suffix in (
            ("claude", "CLAUDE_CONFIG_DIR", "claude"),
            ("codex", "CODEX_HOME", "codex"),
            ("opencode", "XDG_CONFIG_HOME", "opencode"),
        ):
            with self.subTest(adapter=adapter):
                environ = {"HOME": "/tmp/isolated-home", variable: f"/tmp/{suffix}"}
                expected = Path(f"/tmp/{suffix}")
                if adapter == "opencode":
                    expected /= "opencode"
                self.assertEqual(config.user_path(adapter, environ=environ), expected / "agent-config/models.conf")

    def test_masked_worker_home_resolves_the_canonical_user_model_policy(self):
        for adapter, variable in (
            ("claude", "CLAUDE_CONFIG_DIR"),
            ("codex", "CODEX_HOME"),
            ("opencode", "XDG_CONFIG_HOME"),
        ):
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                shipped = source / "adapters" / adapter / "config" / "models.conf"
                shipped.parent.mkdir(parents=True)
                shipped.write_text(BASE + "CFG_MODEL_CONTEXT_WINDOW=800000\n", encoding="utf-8")
                canonical = root / "canonical"
                masked = root / "masked"
                canonical_user = canonical / "agent-config/models.conf"
                masked_user = masked / "agent-config/models.conf"
                canonical_user.parent.mkdir(parents=True)
                masked_user.parent.mkdir(parents=True)
                canonical_user.write_text(
                    BASE.replace("shipped-model", "canonical-user-model")
                    + "CFG_MODEL_CONTEXT_WINDOW=600000\n", encoding="utf-8")
                masked_user.write_text(
                    BASE.replace("shipped-model", "masked-home-model")
                    + "CFG_MODEL_CONTEXT_WINDOW=300000\n", encoding="utf-8")
                env = {"HOME": str(root / "home"), variable: str(masked),
                       "HEARTING_WORKER_HOME": str(masked),
                       "HEARTING_MODEL_CONFIG_HOME": str(canonical)}
                values, receipt = config.resolve_config(
                    adapter, environ=env, source_root=source)
                self.assertEqual(receipt.source, "user")
                self.assertEqual(Path(receipt.selected_path), canonical_user)
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "canonical-user-model")
                self.assertEqual(config.resolve_context_window(values), ("600000", "configured"))

    def test_valid_user_file_wins_as_a_complete_file(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(BASE + 'CFG_USER_EXTRA="literal"\n', encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "user")
        self.assertEqual(receipt.reason, "user-valid")
        self.assertEqual(values["CFG_USER_EXTRA"], "literal")

    def test_context_window_is_optional_common_and_strictly_validated(self):
        for adapter in config.ADAPTERS:
            with self.subTest(adapter=adapter):
                root = self.make_root(adapter, BASE + "CFG_MODEL_CONTEXT_WINDOW=800000\n")
                home = root / "home"
                user = home / "agent-config" / "models.conf"
                user.parent.mkdir(parents=True)

                # A complete pre-existing file without the new scalar remains
                # selected whole and resolves its independent default in memory.
                user.write_text(BASE, encoding="utf-8")
                values, receipt = config.resolve_config(adapter, runtime=home, source_root=root)
                self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
                self.assertNotIn(config.CONTEXT_WINDOW_KEY, values)
                self.assertEqual(config.resolve_context_window(values), ("800000", "default"))

                for number in (100000, 1000000):
                    user.write_text(BASE + f"{config.CONTEXT_WINDOW_KEY}={number}\n", encoding="utf-8")
                    values, receipt = config.resolve_config(adapter, runtime=home, source_root=root)
                    self.assertEqual(receipt.source, "user")
                    self.assertEqual(config.resolve_context_window(values), (str(number), "configured"))

                user.write_text(BASE + f"{config.CONTEXT_WINDOW_KEY}=\"0600000\" # retained comment\n", encoding="utf-8")
                values, receipt = config.resolve_config(adapter, runtime=home, source_root=root)
                self.assertEqual(receipt.source, "user")
                self.assertEqual(config.resolve_context_window(values), ("600000", "configured"))

                for invalid in ("99999", "1000001", "800k", "1m", "+800000", "-1",
                                "800000.0", "1e6", "true", "", " 800000", "800000 ",
                                '" 800000 "'):
                    user.write_text(BASE + f"{config.CONTEXT_WINDOW_KEY}={invalid}\n", encoding="utf-8")
                    values, receipt = config.resolve_config(adapter, runtime=home, source_root=root)
                    self.assertEqual((receipt.source, receipt.reason),
                                     ("shipped", "user-invalid-context-window"), invalid)
                    self.assertEqual(config.resolve_context_window(values), ("800000", "configured"))

        for invalid in ("99999", "1000001", "800k", "+800000", "true"):
            root = self.make_root(shipped=BASE + f"{config.CONTEXT_WINDOW_KEY}={invalid}\n")
            home = root / "home"
            user = home / "agent-config" / "models.conf"
            user.parent.mkdir(parents=True)
            user.write_text(BASE, encoding="utf-8")
            with self.subTest(shipped=invalid), self.assertRaises(config.ShippedConfigError):
                config.resolve_config("claude", runtime=home, source_root=root)

    def test_a_tier_the_adapter_wrappers_read_by_name_stays_required(self):
        # Review R2-B1: the role mappers read CFG_TIER_DEEP_MODEL/EFFORT (and
        # light/mini) directly after matching CFG_ROLES_*, so a copy that routes
        # every profile through model/<id>:effort still needs them.
        shipped = (
            'CFG_MODEL_PROFILE_DEEP=deep:high\n'
            'CFG_TIER_DEEP_MODEL=shipped-deep\n'
            'CFG_TIER_DEEP_EFFORT=high\n'
        )
        root = self.make_root(shipped=shipped)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        # Explicit-model profile: nothing in the file references the deep tier…
        user.write_text('CFG_MODEL_PROFILE_DEEP=model/user-model:high\nCFG_TIER_DEEP_EFFORT=high\n', encoding="utf-8")
        _values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("shipped", "user-incomplete"))
        # …and the same copy is complete once it declares the key the mapper reads.
        user.write_text('CFG_MODEL_PROFILE_DEEP=model/user-model:high\nCFG_TIER_DEEP_EFFORT=high\nCFG_TIER_DEEP_MODEL=user-deep\n', encoding="utf-8")
        _values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))

    def test_declared_wrapper_tiers_cover_every_literal_key_the_wrappers_read(self):
        """Drift guard for required tiers and declared optional fallbacks.

        Scope (deliberately narrow, review R4-M1): it reads `.sh` and `.py` files
        directly under each adapter's `bin/` and matches fully written key names.
        A wrapper that assembles the key at runtime (`CFG_TIER_${tier}_MODEL`),
        lives under another extension, or sits outside `bin/` is NOT covered —
        such a consumer must be added to the table by hand. It also does not tell
        a real read from a mention in a comment, which is the safe direction.
        """
        literal = re.compile(r"CFG_TIER_([A-Z0-9_]+)_(?:MODEL|EFFORT|VARIANT)\b")
        for adapter, declared in config.WRAPPER_REQUIRED_TIERS.items():
            with self.subTest(adapter=adapter):
                found = set()
                fallbacks = config.WRAPPER_FALLBACK_TIERS.get(adapter, {})
                self.assertFalse(declared & fallbacks.keys())
                bin_dir = ROOT / "adapters" / adapter / "bin"
                self.assertTrue(bin_dir.is_dir(), bin_dir)
                for path in sorted(bin_dir.iterdir()):
                    if path.is_file() and path.suffix in (".sh", ".py"):
                        contents = path.read_text(encoding="utf-8", errors="replace")
                        found.update(literal.findall(contents))
                        for tier, fallback in fallbacks.items():
                            self.assertIn(fallback, declared)
                            for suffix in ("MODEL", "EFFORT", "VARIANT"):
                                key = f"CFG_TIER_{tier}_{suffix}"
                                guarded = "${" + key + ":-$CFG_TIER_" + fallback + "_" + suffix + "}"
                                self.assertNotRegex(contents.replace(guarded, ""), rf"\b{key}\b",
                                                    f"{path.name} reads optional {key} without its required fallback")
                self.assertEqual(found - declared - fallbacks.keys(), set(),
                                 f"{adapter} wrappers read an undeclared tier")
                # Each declared tier must ship the exact keys a wrapper reads —
                # a failover/cascade-only mention is not enough.
                shipped = config.parse_config(config.shipped_path(adapter, source_root=ROOT))
                budget = "VARIANT" if adapter == "opencode" else "EFFORT"
                for tier in declared:
                    required = {f"CFG_TIER_{tier}_MODEL", f"CFG_TIER_{tier}_{budget}"}
                    self.assertLessEqual(required, shipped.keys(), f"{adapter} tier {tier} is incomplete")

    def test_legacy_complete_user_copy_survives_a_shipped_tier_extension(self):
        # A release adds a tier (balanced-deep) with two new CFG_TIER_* keys. An
        # older user copy that never references that tier is still a complete
        # policy and stays selected whole-file (its main-only list included).
        shipped = (
            BASE
            + 'CFG_TIER_BALANCED_DEEP_MODEL=shipped-bd\n'
            + 'CFG_TIER_BALANCED_DEEP_EFFORT=high\n'
            + 'CFG_MODEL_PROFILE_BALANCED_DEEP=balanced-deep:high\n'
            + 'CFG_MAIN_SESSION_ONLY_MODELS=" "\n'
        )
        legacy_user = BASE + 'CFG_MODEL_PROFILE_BALANCED_DEEP=deep:high\nCFG_MAIN_SESSION_ONLY_MODELS="fable"\n'
        root = self.make_root(shipped=shipped)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(legacy_user, encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
        self.assertEqual(receipt.unreferenced_tier_keys, "CFG_TIER_BALANCED_DEEP_EFFORT,CFG_TIER_BALANCED_DEEP_MODEL")
        self.assertEqual(values["CFG_MAIN_SESSION_ONLY_MODELS"], "fable")
        self.assertNotIn("CFG_TIER_BALANCED_DEEP_MODEL", values)  # whole-file, never merged
        # The same copy that *references* the new tier without declaring it is incomplete.
        user.write_text(legacy_user.replace("balanced-deep:high", "balanced-deep:high").replace(
            "CFG_MODEL_PROFILE_BALANCED_DEEP=deep:high", "CFG_MODEL_PROFILE_BALANCED_DEEP=balanced-deep:high"), encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("shipped", "user-incomplete"))
        # A scalar tier selector counts as a reference too.
        user.write_text(legacy_user + "CFG_TIER_DEEP_FAILOVER=balanced-deep\n", encoding="utf-8")
        _values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.reason, "user-incomplete")
        # Missing non-tier keys are still incomplete (the exception is tier-shaped only).
        user.write_text(legacy_user.replace("CFG_MODEL_PROFILE_DEEP=deep:xhigh\n", ""), encoding="utf-8")
        _values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.reason, "user-incomplete")

    def test_missing_incomplete_malformed_and_unsafe_user_files_fallback_whole_file(self):
        for text, reason in ((None, "user-missing"), ("CFG_TIER_DEEP_MODEL=user-only\n", "user-incomplete"),
                             ("CFG_MODEL_PROFILE_DEEP=bad+syntax\n", "user-malformed"),
                             ("$(touch sentinel)\n" + BASE, "user-malformed")):
            with self.subTest(reason=reason):
                root = self.make_root()
                home = root / "home"
                user = home / "agent-config" / "models.conf"
                if text is not None:
                    user.parent.mkdir(parents=True)
                    user.write_text(text, encoding="utf-8")
                values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
                self.assertEqual(receipt.reason, reason)
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "shipped-model")

    def test_quoted_comments_extra_keys_and_no_merge(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text('CFG_MODEL_PROFILE_DEEP="deep:xhigh" # comment\nCFG_TIER_DEEP_MODEL="user-model ; $(touch sentinel) `echo nope` O\'Brien"\nCFG_TIER_DEEP_EFFORT=xhigh\n', encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "user")
        self.assertIn("$(touch sentinel)", values["CFG_TIER_DEEP_MODEL"])
        self.assertNotIn("CFG_UNDECLARED", values)

    def test_shipped_failure_is_fatal(self):
        root = self.make_root(shipped="$(bad)\n")
        with self.assertRaises(config.ShippedConfigError):
            config.resolve_config("claude", runtime=root / "home", source_root=root)

    def test_user_symlink_is_rejected_and_falls_back(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.symlink_to(root / "adapters" / "claude" / "config" / "models.conf")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "shipped")
        self.assertEqual(receipt.reason, "user-unreadable")
        self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "shipped-model")

    def test_bridge_quotes_metacharacters_and_receipt(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(BASE.replace("shipped-model", '"$(touch sentinel); `echo bad` O\'Brien"'), encoding="utf-8")
        receipt = root / "receipt.json"
        command = [str(ROOT / "utilities" / "model-config.sh"), "--adapter", "claude", "--runtime-home", str(home), "--source-root", str(root), "--receipt-fd", "3"]
        result = subprocess.run(shlex.join(command) + f" 3>{shlex.quote(str(receipt))}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CFG_TIER_DEEP_MODEL='$(touch sentinel); `echo bad` O'\"'\"'Brien'", result.stdout)
        self.assertEqual(json.loads(receipt.read_text())["source"], "user")
        self.assertFalse((root / "sentinel").exists())


class TopProfileOptionalityTest(unittest.TestCase):
    def test_opencode_omitted_top_preserves_selected_file_bytes_and_permissions(self):
        from model_profile import resolve_runtime_profile
        with tempfile.TemporaryDirectory(prefix="opencode-top-legacy-") as root:
            runtime = Path(root)
            user = runtime / "agent-config/models.conf"
            user.parent.mkdir()
            shipped = config.shipped_path("opencode").read_bytes()
            original = b"".join(line for line in shipped.splitlines(keepends=True)
                                if not line.startswith((b"CFG_MODEL_PROFILE_TOP=",
                                                        b"CFG_MODEL_PROFILE_GRANULARITY_TOP=")))
            user.write_bytes(original)
            user.chmod(0o640)
            resolved, receipt = resolve_runtime_profile("opencode", "top", runtime=runtime)
            self.assertEqual(receipt.source, "user")
            self.assertTrue(resolved["granularity"].startswith("collapsed-top-to-"))
            self.assertEqual(user.read_bytes(), original)
            self.assertEqual(user.stat().st_mode & 0o777, 0o640)

    def make_root(self, adapter="claude", shipped=BASE):
        root = Path(tempfile.mkdtemp())
        shipped_path = root / "adapters" / adapter / "config" / "models.conf"
        shipped_path.parent.mkdir(parents=True)
        shipped_path.write_text(shipped, encoding="utf-8")
        return root

    def test_a_user_copy_without_the_top_profile_stays_valid_and_has_no_top(self):
        shipped = (BASE + 'CFG_TIER_TOP_MODEL=shipped-top\nCFG_TIER_TOP_EFFORT=max\n'
                   'CFG_MODEL_PROFILE_TOP=top:max\nCFG_MODEL_PROFILE_GRANULARITY_TOP=full\n'
                   'CFG_MAIN_SESSION_ONLY_MODELS="shipped-top"\n')
        legacy_user = BASE + 'CFG_MAIN_SESSION_ONLY_MODELS="fable"\n'
        root = self.make_root(shipped=shipped)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(legacy_user, encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
        self.assertEqual(receipt.unreferenced_tier_keys, "CFG_TIER_TOP_EFFORT,CFG_TIER_TOP_MODEL")
        self.assertNotIn("CFG_MODEL_PROFILE_TOP", values)  # never derived
        # a copy that opts in without declaring the tier stays selected whole-file,
        # and resolving `top` on it refuses typed instead (top review B2 (iii))
        user.write_text(legacy_user + "CFG_MODEL_PROFILE_TOP=top:max\n", encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
        from model_profile import ModelProfileError, resolve_profile_values
        with self.assertRaises(ModelProfileError) as refused:
            resolve_profile_values("claude", values, "top")
        self.assertEqual(refused.exception.reason, "profile-top-undeclared")

    def test_an_opencode_copy_without_the_deep_granularity_key_stays_selected(self):
        # 2026-09-30: the shipped OpenCode file started naming its deep collapse
        # (`CFG_MODEL_PROFILE_GRANULARITY_DEEP`). That key is receipt metadata, so a
        # complete copy seeded before it must keep being the user's selected file
        # instead of being replaced whole by the shipped one.
        shipped_text = (ROOT / "adapters" / "opencode" / "config" / "models.conf").read_text(encoding="utf-8")
        self.assertIn("CFG_MODEL_PROFILE_GRANULARITY_DEEP=", shipped_text)
        legacy = "\n".join(
            line for line in shipped_text.splitlines()
            if not line.startswith("CFG_MODEL_PROFILE_GRANULARITY_DEEP=")
        ) + "\n"
        root = self.make_root("opencode", shipped_text)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(legacy, encoding="utf-8")
        values, receipt = config.resolve_config("opencode", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
        from model_profile import resolve_profile_values
        self.assertEqual(resolve_profile_values("opencode", values, "deep")["granularity"], "full")
        # Any other missing shipped key still means an incomplete copy.
        user.write_text(legacy.replace("CFG_MODEL_PROFILE_MINI=", "#CFG_MODEL_PROFILE_MINI="), encoding="utf-8")
        _values, receipt = config.resolve_config("opencode", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("shipped", "user-incomplete"))

    def test_a_codex_copy_without_the_main_only_key_stays_selected_whole_file(self):
        # top review B2: a policy key a release added must not turn an older
        # complete codex copy into `user-incomplete` (a silent whole-file
        # replacement of the user's policy).
        shipped = BASE + 'CFG_MAIN_SESSION_ONLY_MODELS="shipped-top"\n'
        legacy_user = BASE.replace("CFG_TIER_DEEP_MODEL=", "CFG_TIER_DEEP_MODEL=user-custom-") if "CFG_TIER_DEEP_MODEL=" in BASE else BASE
        root = self.make_root(adapter="codex", shipped=shipped)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(legacy_user, encoding="utf-8")
        values, receipt = config.resolve_config("codex", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
        self.assertNotIn("CFG_MAIN_SESSION_ONLY_MODELS", values)   # absent, never merged
        # the same omission on claude is still incomplete (the key is required there)
        root2 = self.make_root(adapter="claude", shipped=shipped)
        home2 = root2 / "home"
        (home2 / "agent-config").mkdir(parents=True)
        (home2 / "agent-config" / "models.conf").write_text(legacy_user, encoding="utf-8")
        _v, receipt2 = config.resolve_config("claude", runtime=home2, source_root=root2)
        self.assertEqual(receipt2.reason, "user-incomplete")

    def test_every_optional_policy_key_is_shipped_and_registered_by_hand(self):
        # combined review m1: the optional-key registration is hand-written,
        # so pin what it must satisfy -- the key exists in that adapter's
        # shipped file (otherwise the entry is dead), and a user copy without
        # it stays selected whole-file with the key absent (the consumer must
        # tolerate that, which is why Claude's main-session-only key is absent
        # from this map: its wrapper raises when the key is missing).
        # Absence *tolerance* itself is asserted at the consumer, not here:
        # `codex_dispatch_model_eligibility.test.py`
        # ::test_a_user_copy_without_the_key_carries_no_restriction
        # (guard review m5).
        root = config.repository_root()
        for adapter, keys in config.OPTIONAL_POLICY_KEYS.items():
            shipped = config.parse_config(config.shipped_path(adapter, source_root=root))
            for key in keys:
                with self.subTest(adapter=adapter, key=key):
                    self.assertIn(key, shipped)
        self.assertNotIn("CFG_MAIN_SESSION_ONLY_MODELS", config.OPTIONAL_POLICY_KEYS.get("claude", ()))
        # and a shipped key that is registered nowhere still makes a copy
        # incomplete -- the deliberate default this map is an exception to
        shipped_text = BASE + 'CFG_SOME_NEW_POLICY_KEY=value\n'
        root2 = self.make_root(shipped=shipped_text)
        home = root2 / "home"
        (home / "agent-config").mkdir(parents=True)
        (home / "agent-config" / "models.conf").write_text(BASE, encoding="utf-8")
        _values, receipt = config.resolve_config("claude", runtime=home, source_root=root2)
        self.assertEqual(receipt.reason, "user-incomplete")

    def test_inheritance_is_refused_exactly_when_a_main_only_model_is_declared(self):
        # guard review m1 pinned that opencode declares no main-session-only
        # list, because its wrapper allowed inheritance on that ground alone.
        # The rule now reads the list itself on every adapter, so declaring
        # the key closes inheritance with no wrapper edit.
        for adapter in config.ADAPTERS:
            shipped = config.parse_config(config.shipped_path(adapter, source_root=config.repository_root()))
            with self.subTest(adapter=adapter):
                refusal = config.inheritance_refusal(shipped, adapter)
                self.assertEqual(refusal is None, not config.main_session_only_models(shipped))

    def test_restricted_model_matches_whole_ids_and_alias_tokens(self):
        self.assertTrue(config.restricted_model("claude-fable-5-1", "fable"))
        self.assertTrue(config.restricted_model("fable", ["fable"]))
        self.assertTrue(config.restricted_model("gpt-6-astra", "gpt-6-astra"))
        self.assertTrue(config.restricted_model("GPT-6-Astra", ["gpt-6-astra"]))
        self.assertFalse(config.restricted_model("gpt-6-astra-mini", "gpt-6-astra"))  # whole id only
        self.assertFalse(config.restricted_model("gpt-5.6-sol", "gpt-6-astra"))
        self.assertFalse(config.restricted_model("opus", " "))
        self.assertFalse(config.restricted_model("claude-opus-5", ["fable"]))


if __name__ == "__main__":
    unittest.main()
