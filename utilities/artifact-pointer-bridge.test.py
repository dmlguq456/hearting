#!/usr/bin/env python3
import ast
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
MEM_PY = REPO_ROOT / "tools" / "memory" / "mem.py"
# Mirrors tools/memory/mem.py RECORD_COLS: the physical `records` column order.
RECORD_FIELDS = ("id", "tier", "scope", "type", "cwd_origin", "created", "updated",
                 "expires", "source", "tags", "links", "body", "strength", "last_accessed",
                 "injection_flag", "delivery_state", "headline", "aliases", "entities", "topics",
                 "artifact_refs", "status", "canonical_id", "superseded_by", "capsule_version")
spec = importlib.util.spec_from_file_location("pointer_bridge", HERE / "artifact-pointer-bridge.py")
bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)

CAIRN_CHECKOUT = Path("/home/nas/user/Uihyeop/personal/cairn")
REQUIRED_CAIRN_COMMIT = "1fa0d99e4b714b5ce305f78c8f7c7773255e8f87"
RETRYABLE_CODES = {"TIMEOUT", "STALE_SNAPSHOT", "STALE_PROJECTION", "UNAVAILABLE_PROJECTION"}
D54_REQUEST_KEYS = frozenset((
    "artifact_root_id", "resolve_active", "target_ids", "retrieval_mode",
    "verify", "order", "page_size", "deadline_ms",
))


def _cairn_ready():
    """Only the pinned detached W3a commit is an acceptable fixture transport."""
    tsx = CAIRN_CHECKOUT / "node_modules" / ".bin" / "tsx"
    if not tsx.exists():
        return False
    proc = subprocess.run(
        ["git", "-C", str(CAIRN_CHECKOUT), "merge-base", "--is-ancestor", REQUIRED_CAIRN_COMMIT, "HEAD"],
        capture_output=True,
    )
    return proc.returncode == 0


class _W5FixtureHandler(BaseHTTPRequestHandler):
    """Isolated HTTP read fixture: never live Cairn, never a real credential."""

    def do_POST(self):
        size = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(size))
        self.server.captured.append(request)
        auth = self.headers.get("authorization")
        code = detail = None
        status = 200
        if auth == "Bearer w5-ingest-scope-token":
            code, detail, status = "FORBIDDEN", "ingest-scope", 403
        elif auth != f"Bearer {self.server.token}":
            code, detail, status = "AUTH_FAILED", "fixture-auth", 401
        else:
            targets = request.get("target_ids") or []
            trigger = request.get("query", "")
            if not trigger and targets and str(targets[0]).startswith("error:"):
                trigger = str(targets[0])
            if trigger.startswith("error:"):
                code, detail, status = trigger.split(":", 1)[1], "fixture", 400
        if code:
            body = {"code": code, "detail": detail, "retryable": code in RETRYABLE_CODES,
                     "observed_at": "2026-08-21T00:00:00Z"}
            self.server.responses.append(body)
            payload = json.dumps(body).encode()
            self.send_response(status)
        else:
            targets = request.get("target_ids") or []
            empty = request.get("query") == "empty-rows" or targets == ["empty-rows"]
            rows = [] if empty or not targets else [{"stable_id": targets[0]}]
            body = {"rows": rows}
            self.server.responses.append(body)
            payload = json.dumps(body).encode()
            self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


def _start_fixture_server(token="w5-fixture-read-token"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _W5FixtureHandler)
    server.token = token
    server.captured = []
    server.responses = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


_SEED_CONFLICT_SCRIPT = """
import hashlib
import json
import sqlite3
import sys

sys.path.insert(0, sys.argv[1])
import mem
import protocol_v2

rid = mem.write_record("durable", "global", "note",
                        "w5 synthetic conflict fixture baseline body content", quiet=True)
if rid is None:
    raise SystemExit("baseline write failed")
op_a = hashlib.sha256(b"w5-synthetic-conflict-op-a").hexdigest()
op_b = hashlib.sha256(b"w5-synthetic-conflict-op-b").hexdigest()
con = mem.get_con()
try:
    con.execute("BEGIN IMMEDIATE")
    base = mem._record_state(con, rid)
    variant_a = dict(base); variant_a["headline"] = "w5 variant a"
    variant_b = dict(base); variant_b["headline"] = "w5 variant b"
    con.execute("DELETE FROM sync_frontier WHERE record_id=?", (rid,))
    seed_rows = (
        (op_a, variant_a, 0, "replica-w5-a", "1", "synthetic/w5/op-a"),
        (op_b, variant_b, 1, "replica-w5-b", "1", "synthetic/w5/op-b"),
    )
    for op_id, variant, provisional, replica, counter, path in seed_rows:
        con.execute(
            "INSERT INTO sync_objects(op_id,replica_id,counter,project_key,kind,"
            "object_path,payload_bytes,classification) VALUES(?,?,?,?,?,?,?,?)",
            (op_id, replica, counter, "global", "record", path,
             sqlite3.Binary(protocol_v2.canonical_bytes({"synthetic": True})), "local"),
        )
        con.execute(
            "INSERT INTO sync_conflicts(project_key,record_id,op_id,diagnostic_id,"
            "provisional,variant_bytes) VALUES(?,?,?,?,?,?)",
            ("global", rid, op_id, "concurrent-record-variants", provisional,
             sqlite3.Binary(protocol_v2.canonical_bytes(variant))),
        )
        con.execute(
            "INSERT INTO sync_frontier(project_key,record_id,op_id,source) "
            "VALUES(?,?,?,?)", ("global", rid, op_id, "synthetic"),
        )
    con.commit()
finally:
    con.close()
print(json.dumps({"rid": rid, "parent_a": op_a, "parent_b": op_b}))
""".lstrip()


def _leak_scan(value, forbidden_substrings=(), max_len=240):
    """Independent recursive scanner over an already-serialized value.

    Deliberately not implemented in terms of the bridge's own helpers, so a
    bug shared between the scanner and the bridge cannot hide a real leak.
    """
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, sub in node.items():
                if key in bridge.FORBIDDEN_BODY_KEYS:
                    hits.append(f"{path}.{key}")
                walk(sub, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            if len(node) > max_len:
                hits.append(f"{path}:long:{len(node)}")
            for needle in forbidden_substrings:
                if needle and needle in node:
                    hits.append(f"{path}:substring")

    walk(value, "$")
    return hits


class IsolationMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["XDG_DATA_HOME"] = str(self.root / "data")
        os.environ["XDG_STATE_HOME"] = str(self.root / "state")
        os.environ["MEM_STORE"] = str(self.root / "data" / "hearting" / "memory")
        self.assertTrue(Path(os.environ["MEM_STORE"]).resolve().is_relative_to(self.root.resolve()))

    def tearDown(self):
        for key in ("XDG_DATA_HOME", "XDG_STATE_HOME", "MEM_STORE"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _db_path(self):
        return Path(os.environ["MEM_STORE"]) / "memory.db"

    def _telemetry_path(self):
        return Path(os.environ["XDG_STATE_HOME"]) / "hearting" / "artifact-projection" / "read-telemetry.jsonl"

    def _recall_events_path(self):
        return Path(os.environ["XDG_STATE_HOME"]) / "agent-memory" / "recall-events.jsonl"

    def _mem_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MEM_PY), *args],
            env=os.environ.copy(), cwd=str(REPO_ROOT),
            capture_output=True, text=True,
        )

    def _seed_conflict(self):
        proc = subprocess.run(
            [sys.executable, "-c", _SEED_CONFLICT_SCRIPT, str(MEM_PY.parent)],
            env=os.environ.copy(), cwd=str(REPO_ROOT),
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fixture = json.loads(proc.stdout.strip().splitlines()[-1])
        # Prove the fixture through the real CLI before any resolve test runs.
        proof = self._mem_cli("show-conflict", fixture["rid"], "--json")
        self.assertEqual(proof.returncode, 0, proof.stderr)
        proof_ops = sorted(item["op_id"] for item in json.loads(proof.stdout)["variants"])
        self.assertEqual(proof_ops, sorted((fixture["parent_a"], fixture["parent_b"])))
        return fixture

    def _snapshot(self, rid):
        con = sqlite3.connect(self._db_path())
        try:
            record = con.execute(
                f"SELECT {','.join(RECORD_FIELDS)} FROM records WHERE id=?", (rid,)).fetchone()
            conflicts = con.execute(
                "SELECT project_key,record_id,op_id,diagnostic_id,provisional,"
                "variant_bytes,resolved_by FROM sync_conflicts WHERE record_id=? "
                "ORDER BY op_id", (rid,)).fetchall()
            frontier = con.execute(
                "SELECT project_key,record_id,op_id FROM sync_frontier "
                "WHERE record_id=? ORDER BY op_id", (rid,)).fetchall()
            objects = con.execute("SELECT op_id FROM sync_objects ORDER BY op_id").fetchall()
        finally:
            con.close()
        return (record, conflicts, frontier, objects)


class CairnFixtureMixin(IsolationMixin):
    """Shared isolated-HTTP-fixture wiring for real cairn-artifact-read.sh tests."""

    @classmethod
    def setUpClass(cls):
        if not _cairn_ready():
            raise unittest.SkipTest(
                "detached Cairn W3a checkout is unavailable at the pinned commit")
        cls.fixture_server, cls.fixture_thread = _start_fixture_server()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_server.shutdown()
        cls.fixture_thread.join(timeout=5)

    def setUp(self):
        super().setUp()
        self.fixture_server.captured.clear()
        self.fixture_server.responses.clear()
        os.environ["CAIRN_ROOT"] = str(CAIRN_CHECKOUT)
        os.environ["CAIRN_READ_ENDPOINT"] = f"http://127.0.0.1:{self.fixture_server.server_address[1]}/read"
        os.environ["CAIRN_READ_TOKEN"] = self.fixture_server.token

    def tearDown(self):
        for key in ("CAIRN_ROOT", "CAIRN_READ_ENDPOINT", "CAIRN_READ_TOKEN"):
            os.environ.pop(key, None)
        super().tearDown()

    def _base_request(self, target="a1"):
        return {"artifact_root_id": "root-a", "resolve_active": True, "target_ids": [target],
                "retrieval_mode": "path-only", "verify": "metadata", "order": "stable-id",
                "page_size": 20, "deadline_ms": 3000}

    def _bridge_cli(self, record):
        return subprocess.run(
            [sys.executable, str(HERE / "artifact-pointer-bridge.py"),
             "resolve", json.dumps(record)],
            cwd=str(REPO_ROOT), env=os.environ.copy(), capture_output=True, text=True,
        )


class Channel(IsolationMixin, unittest.TestCase):
    def test_legacy_is_not_attempted(self):
        result = bridge.parse_record({"id": "legacy", "entities": ["git:abc"], "artifact_refs": ["old/path"]})
        self.assertEqual(result["disposition"], "legacy-locator-only")
        self.assertIsNone(result["integrity"]); self.assertEqual(result["fallback_locators"], ["old/path"])

    def test_channels_are_separate(self):
        result = bridge.parse_record({"id": "p", "entities": ["apid:root:r", "apid:artifact:a"], "artifact_refs": ["x"]})
        self.assertEqual({x["slot"] for x in result["identity"]}, {"root", "artifact"})
        with self.assertRaisesRegex(bridge.BridgeError, "apid-in-artifact-refs"):
            bridge.parse_record({"entities": ["apid:root:r"], "artifact_refs": ["apid:artifact:a"]})


class ChannelNegative(Channel):
    def test_unknown_prefix_is_not_identity(self):
        self.assertEqual(bridge.parse_record({"entities": ["commit:abc"]})["disposition"], "legacy-locator-only")

    def test_resolve_rejects_apid_in_artifact_refs(self):
        fixture = self._seed_conflict()
        rid = fixture["rid"]
        before = self._snapshot(rid)
        proc = self._mem_cli(
            "resolve", rid, "--parents", fixture["parent_a"], fixture["parent_b"],
            "--artifact-ref", "apid:artifact:x",
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr, "[apid-capacity] apid-in-artifact-refs\n")
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self._snapshot(rid), before)


class Identity(IsolationMixin, unittest.TestCase):
    def test_closed_slots_and_class(self):
        result = bridge.parse_record({"entities": ["apid:root:r", "apid:cycle:migration:c"]})
        self.assertEqual({x["slot"] for x in result["identity"]}, {"root", "cycle"})


class IdentityNegative(Identity):
    def test_shaped_values_and_reserved_prefixes_are_not_identity(self):
        result = bridge.parse_record({
            "entities": ["<value>", "apid:artifact-revision:z", "apid:manifest:m", "apid:legacy:l"],
        })
        self.assertIsNone(result["identity"])
        self.assertEqual(result["disposition"], "legacy-locator-only")

    def test_unrecognized_slot_is_unrecognized_not_error(self):
        # Must not raise: an unrecognized slot is simply not identity.
        result = bridge.parse_record({"entities": ["apid:unknown-slot:v"]})
        self.assertIsNone(result["identity"])
        self.assertEqual(result["disposition"], "legacy-locator-only")


class Capacity(IsolationMixin, unittest.TestCase):
    def test_entity_capacity_is_pre_normalization_and_transactional(self):
        initial = [f"entity-{i}" for i in range(22)]
        first_args = ["add", "durable", "note", "existing entity capacity fixture body", "--scope", "global", "--source", "capacity-source"]
        for item in initial: first_args += ["--entity", item]
        first = self._mem_cli(*first_args)
        self.assertEqual(first.returncode, 0, first.stderr)
        con = sqlite3.connect(self._db_path()); rid = con.execute("SELECT id FROM records").fetchone()[0]; con.close()
        before = self._snapshot(rid)
        args = ["add", "durable", "note", "new entity capacity fixture body", "--scope", "global", "--source", "capacity-source"]
        for i in range(25): args += ["--entity", f"entity-{i}"]
        proc = self._mem_cli(*args)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("[apid-capacity] apid-entity-capacity", proc.stderr)
        self.assertEqual(self._snapshot(rid), before)
    def test_target_capacity(self):
        entities = ["apid:root:r"] + [f"apid:artifact:a{i}" for i in range(5)]
        with self.assertRaisesRegex(bridge.BridgeError, "apid-target-capacity"):
            bridge.parse_record({"entities": entities})

    def test_length_capacity(self):
        with self.assertRaisesRegex(bridge.BridgeError, "apid-token-too-long"):
            bridge.parse_record({"entities": ["apid:artifact:" + "x" * 160]})

    def test_resolve_rejects_target_capacity_overflow(self):
        fixture = self._seed_conflict()
        rid = fixture["rid"]
        before = self._snapshot(rid)
        entity_args = []
        for token in ["apid:root:r"] + [f"apid:artifact:a{i}" for i in range(5)]:
            entity_args += ["--entity", token]
        proc = self._mem_cli(
            "resolve", rid, "--parents", fixture["parent_a"], fixture["parent_b"],
            *entity_args,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr, "[apid-capacity] apid-target-capacity\n")
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self._snapshot(rid), before)

    def test_resolve_positive_normal_range(self):
        fixture = self._seed_conflict()
        rid = fixture["rid"]
        before = self._snapshot(rid)
        proc = self._mem_cli(
            "resolve", rid, "--parents", fixture["parent_a"], fixture["parent_b"],
            "--entity", "apid:root:r1", "--entity", "apid:artifact:a1",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("[resolve] "), proc.stdout)
        after = self._snapshot(rid)
        self.assertEqual(len(after[3]) - len(before[3]), 1)
        stored_entities = json.loads(after[0][RECORD_FIELDS.index("entities")])
        stored_apid = {item for item in stored_entities if item.startswith("apid:")}
        self.assertEqual(stored_apid, {"apid:root:r1", "apid:artifact:a1"})


class CapacityNegative(Capacity):
    def test_empty_is_not_successful_pointer(self):
        self.assertEqual(bridge.parse_record({"entities": []})["disposition"], "legacy-locator-only")

    def test_resolve_rejects_token_too_long(self):
        fixture = self._seed_conflict()
        rid = fixture["rid"]
        before = self._snapshot(rid)
        proc = self._mem_cli(
            "resolve", rid, "--parents", fixture["parent_a"], fixture["parent_b"],
            "--entity", "apid:artifact:" + "x" * 160,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr, "[apid-capacity] apid-token-too-long\n")
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self._snapshot(rid), before)

    def test_guard_is_not_only_at_write_record(self):
        # resolve_conflict never calls write_record; a positive capacity
        # rejection through `resolve` alone proves the guard is duplicated
        # on the resolver path rather than relying on write_record's guard.
        fixture = self._seed_conflict()
        rid = fixture["rid"]
        before = self._snapshot(rid)
        proc = self._mem_cli(
            "resolve", rid, "--parents", fixture["parent_a"], fixture["parent_b"],
            "--entity", "apid:artifact:" + "y" * 161,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr, "[apid-capacity] apid-token-too-long\n")
        self.assertEqual(self._snapshot(rid), before)


class Duplication(IsolationMixin, unittest.TestCase):
    def test_freeze_and_zero_model_calls(self):
        freeze_path = HERE / "fixtures/w5-duplication-freeze.json"
        corpus_path = HERE / "fixtures/w5-duplication-labeled.json"
        freeze_before = hashlib.sha256(freeze_path.read_bytes()).hexdigest()
        freeze = json.loads(freeze_path.read_text())
        corpus = json.loads(corpus_path.read_text())
        result = bridge.duplication_check(corpus, freeze)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["denominator"], "memory-side")
        self.assertEqual(result["false_positive"], freeze["false_positive"])
        self.assertEqual(result["false_negative"], freeze["false_negative"])
        self.assertLessEqual(result["false_positive"], freeze["false_positive_upper_bound"])
        self.assertLessEqual(result["false_negative"], freeze["false_negative_upper_bound"])
        self.assertEqual(result["undecidable_count"], freeze["undecidable_count"])
        self.assertGreater(result["undecidable_count"], 0)
        self.assertEqual(result["corpus_count"], len(corpus))
        self.assertEqual(result["corpus_sha256"], hashlib.sha256(corpus_path.read_bytes()).hexdigest())
        self.assertEqual(result["normalize_source_digest"], bridge.NORMALIZE_SOURCE_DIGEST)
        self.assertEqual(result["rule_source_digest"], bridge.RULE_SOURCE_DIGEST)
        self.assertEqual(result["ngram_denominator_count"],
                         sum(item["ngram_denominator_count"] for item in result["items"]))
        by_id = {item["id"]: item for item in result["items"]}
        self.assertEqual(by_id["u1"]["verdict"], "undecidable")
        positives = [row for row in corpus if row["label"] == "positive"]
        negatives = [row for row in corpus if row["label"] == "negative"]
        self.assertGreaterEqual(len(positives), 10); self.assertGreaterEqual(len(negatives), 10)
        self.assertTrue(all(by_id[row["id"]]["verdict"] == "violation" for row in positives))
        self.assertTrue(all(by_id[row["id"]]["verdict"] == "ok" for row in negatives))
        self.assertEqual(bridge._word_ngrams("alpha beta gamma", 2),
                         (("alpha", "beta"), ("beta", "gamma")))
        self.assertEqual(hashlib.sha256(freeze_path.read_bytes()).hexdigest(), freeze_before)

    def test_sealed_cli_uses_repository_corpus_and_freeze(self):
        corpus_path = HERE / "fixtures/w5-duplication-labeled.json"
        freeze_path = HERE / "fixtures/w5-duplication-freeze.json"
        before = hashlib.sha256(freeze_path.read_bytes()).hexdigest()
        proc = subprocess.run(
            [sys.executable, str(HERE / "artifact-pointer-bridge.py"), "duplication-check"],
            input=corpus_path.read_text(), capture_output=True, text=True, cwd=str(REPO_ROOT),
            env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["corpus_sha256"], hashlib.sha256(corpus_path.read_bytes()).hexdigest())
        self.assertEqual(hashlib.sha256(freeze_path.read_bytes()).hexdigest(), before)
        imports = set()
        for node in ast.walk(ast.parse((HERE / "artifact-pointer-bridge.py").read_text())):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue({"requests", "httpx", "openai", "anthropic"}.isdisjoint(imports))


class DuplicationNegative(Duplication):
    def test_empty_corpus_is_typed_failure(self):
        with self.assertRaisesRegex(bridge.BridgeError, "no-corpus"):
            bridge.duplication_check([], {})

    def test_missing_freeze_field_and_count_drift_are_typed(self):
        corpus = json.loads((HERE / "fixtures/w5-duplication-labeled.json").read_text())
        freeze = json.loads((HERE / "fixtures/w5-duplication-freeze.json").read_text())
        incomplete = dict(freeze); incomplete.pop("calibration_source_digest")
        with self.assertRaisesRegex(bridge.BridgeError, "no-freeze"):
            bridge.duplication_check(corpus, incomplete)
        drifted = dict(freeze); drifted["false_negative"] += 1
        with self.assertRaisesRegex(bridge.BridgeError, "freeze-count-mismatch"):
            bridge.duplication_check(corpus, drifted)

    def test_cli_rejects_rule_and_path_overrides(self):
        for payload, code in (({"freeze": "/tmp/alternate-freeze.json"}, "freeze-path-forbidden"),
                              ({"corpus": "/tmp/alternate-corpus.json"}, "corpus-path-forbidden")):
            proc = subprocess.run(
                [sys.executable, str(HERE / "artifact-pointer-bridge.py"),
                 "duplication-check", json.dumps(payload)],
                capture_output=True, text=True, cwd=str(REPO_ROOT), env=os.environ.copy(),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(json.loads(proc.stdout)["error"]["code"], code)


class Mapping(CairnFixtureMixin, unittest.TestCase):
    def test_runtime_tuple_and_request_contract_from_tsx(self):
        tsx = CAIRN_CHECKOUT / "node_modules/.bin/tsx"
        self.assertTrue(tsx.exists())
        proc = subprocess.run([str(tsx), "-e", "import {READ_ERROR_CODES} from './lib/artifact-projection/read/errors.ts'; console.log(JSON.stringify(READ_ERROR_CODES))"], cwd=CAIRN_CHECKOUT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        runtime = tuple(json.loads(proc.stdout.strip().splitlines()[-1]))
        self.assertEqual(runtime, tuple(bridge.ERROR_TO_INTEGRITY))
        self.assertEqual(bridge._validate_error_domain(), runtime)
        request = bridge._request({"value": "root"}, [{"value": "a"}])
        self.assertEqual(request, {
            "artifact_root_id": "root", "resolve_active": True,
            "target_ids": ["a"], "retrieval_mode": "path-only",
            "verify": "metadata", "order": "stable-id", "page_size": 20,
            "deadline_ms": 3000,
        })
        self.assertEqual(set(request), D54_REQUEST_KEYS)

    def test_nineteen_input_runtime_mapping_over_real_transport(self):
        """A-12.5: all 17 READ_ERROR_CODEs plus rows=[target] and rows=[] over
        the real utilities/cairn-artifact-read.sh contract and isolated HTTP
        fixture -- not a monkeypatch loop over the bridge's own table."""
        runtime_codes = bridge._validate_error_domain()
        self.assertEqual(len(runtime_codes), 17)
        expected_exit = {code: index + 2 for index, code in enumerate(runtime_codes)}
        explicit_table = {
            "MISSING_TARGET": ("missing", 6), "STALE_SNAPSHOT": ("stale", 8),
            "STALE_PROJECTION": ("stale", 9), "VERSION_MISMATCH": ("version-mismatch", 10),
            "DIGEST_MISMATCH": ("version-mismatch", 11),
            "LEGACY_MAPPING_MISSING": ("missing", None),
            "LEGACY_MAPPING_MISMATCH": ("version-mismatch", None),
        }
        for code, (integrity, exit_code) in explicit_table.items():
            self.assertEqual(bridge.ERROR_TO_INTEGRITY[code], integrity)
            if exit_code is not None:
                self.assertEqual(expected_exit[code], exit_code)
        for code in runtime_codes:
            with self.subTest(code=code):
                request = dict(self._base_request(), query=f"error:{code}")
                returncode, payload = bridge._read_cairn(request)
                self.assertEqual(returncode, expected_exit[code])
                self.assertEqual(payload["error"]["code"], code)
                self.assertEqual(payload["error"]["retryable"], code in RETRYABLE_CODES)
                sent = self.fixture_server.captured[-1]
                self.assertEqual(set(sent) & D54_REQUEST_KEYS, D54_REQUEST_KEYS)
                item = {"slot": "artifact", "identity_class": "stable", "value": "a1"}
                result = bridge._result_for_target(item, payload, code)
                self.assertEqual(result["integrity"], bridge.ERROR_TO_INTEGRITY[code])
                self.assertEqual(result["read_error_code"], code)
                self.assertEqual(result["detail"], payload["error"]["detail"])
                self.assertEqual(result["retryable"], payload["error"]["retryable"])
                cli = self._bridge_cli({
                    "id": f"record-{code}",
                    "entities": ["apid:root:root-a", f"apid:artifact:error:{code}"],
                })
                self.assertEqual(cli.returncode, 0, cli.stderr)
                cli_result = json.loads(cli.stdout)[0]
                self.assertEqual(cli_result["integrity"], bridge.ERROR_TO_INTEGRITY[code])
                self.assertEqual(cli_result["read_error_code"], code)
                self.assertEqual(cli_result["detail"], "Cairn read rejected the request")
                self.assertEqual(cli_result["retryable"], code in RETRYABLE_CODES)
                sent = self.fixture_server.captured[-1]
                expected_http = bridge._request(
                    {"value": "root-a"},
                    [{"value": f"error:{code}"}],
                )
                # The bridge hands the exact D-54 shape to the shell. Cairn's
                # inherited request normalizer then adds only the closed empty
                # `query` field before the isolated HTTP fixture observes it.
                self.assertEqual(sent, {**expected_http, "query": ""})
        returncode, payload = bridge._read_cairn(self._base_request())
        self.assertEqual(returncode, 0)
        present = bridge._result_for_target({"slot": "artifact", "identity_class": "stable", "value": "a1"}, payload)
        self.assertEqual(present["integrity"], "ok")
        returncode, payload = bridge._read_cairn(dict(self._base_request(), query="empty-rows"))
        self.assertEqual(returncode, 0)
        missing = bridge._result_for_target({"slot": "artifact", "identity_class": "stable", "value": "a1"}, payload)
        self.assertEqual(missing["integrity"], "missing")
        self.assertIsNone(missing["read_error_code"])

        for target, expected in (("present-a", "ok"), ("empty-rows", "missing")):
            with self.subTest(success_target=target):
                cli = self._bridge_cli({
                    "id": f"record-{target}",
                    "entities": ["apid:root:root-a", f"apid:artifact:{target}"],
                })
                self.assertEqual(cli.returncode, 0, cli.stderr)
                cli_result = json.loads(cli.stdout)[0]
                self.assertEqual(cli_result["integrity"], expected)
                self.assertIsNone(cli_result["read_error_code"])
                telemetry = json.loads(self._telemetry_path().read_text().splitlines()[-1])
                self.assertEqual(telemetry["zero_result"], expected == "missing")

    def test_four_targets_two_rows_produces_four_results(self):
        original = bridge._read_cairn
        bridge._read_cairn = lambda request: (0, {"rows": [
            {"stable_id": "a0"}, {"stable_id": "a2"},
        ]})
        try:
            results = bridge.resolve({
                "id": "four-target-record",
                "entities": ["apid:root:r"] + [f"apid:artifact:a{i}" for i in range(4)],
            })
        finally:
            bridge._read_cairn = original
        self.assertEqual(len(results), 4)
        self.assertEqual([row["integrity"] for row in results],
                         ["ok", "missing", "ok", "missing"])

    def test_all_runtime_codes_via_resolve_end_to_end(self):
        original = bridge._read_cairn
        try:
            for code in bridge.ERROR_TO_INTEGRITY:
                bridge._read_cairn = lambda request, code=code: (2, {"error": {"code": code, "detail": "fixture", "retryable": code in RETRYABLE_CODES}})
                result = bridge.resolve({"id": "r", "entities": ["apid:root:root", "apid:artifact:a"]})[0]
                self.assertEqual(result["integrity"], bridge.ERROR_TO_INTEGRITY[code])
                self.assertEqual(result["read_error_code"], code)
            bridge._read_cairn = lambda request: (0, {"rows": [{"stable_id": "a", "canonical_raw_locator": None}]})
            self.assertEqual(bridge.resolve({"entities": ["apid:root:r", "apid:artifact:a"]})[0]["integrity"], "ok")
            bridge._read_cairn = lambda request: (0, {"rows": []})
            empty = bridge.resolve({"entities": ["apid:root:r", "apid:artifact:a"]})[0]
            self.assertEqual((empty["integrity"], empty["read_error_code"]), ("missing", None))
        finally:
            bridge._read_cairn = original


class MappingNegative(Mapping):
    def test_unknown_code_is_closed_failure(self):
        original = bridge._read_cairn
        try:
            bridge._read_cairn = lambda request: (9, {"error": {"code": "NOT_A_RUNTIME_CODE", "detail": "fixture"}})
            result = bridge.resolve({"entities": ["apid:root:r", "apid:artifact:a"]})[0]
            self.assertEqual(result["integrity"], "unknown-failure")
        finally:
            bridge._read_cairn = original

    def test_success_empty_is_never_ok(self):
        returncode, payload = bridge._read_cairn(dict(self._base_request(), query="empty-rows"))
        result = bridge._result_for_target({"slot": "artifact", "identity_class": "stable", "value": "a1"}, payload)
        self.assertNotEqual(result["integrity"], "ok")
        self.assertEqual(result["integrity"], "missing")

    def test_error_input_never_maps_to_ok(self):
        for code in bridge._validate_error_domain():
            returncode, payload = bridge._read_cairn(dict(self._base_request(), query=f"error:{code}"))
            result = bridge._result_for_target({"slot": "artifact", "identity_class": "stable", "value": "a1"}, payload)
            self.assertNotEqual(result["integrity"], "ok")

    def test_returncode_does_not_select_integrity(self):
        source = inspect.getsource(bridge._result_for_target)
        self.assertNotIn("returncode", source)
        self.assertNotIn("code == 0", source)

    def test_missing_cairn_root_is_unknown_failure_not_missing(self):
        prior = os.environ.pop("CAIRN_ROOT")
        try:
            result = bridge.resolve({
                "id": "no-cairn-root",
                "entities": ["apid:root:r", "apid:artifact:a"],
            })[0]
        finally:
            os.environ["CAIRN_ROOT"] = prior
        self.assertEqual(result["integrity"], "unknown-failure")
        self.assertEqual(result["read_error_code"], "INTERNAL_FAILURE")


class NoBody(IsolationMixin, unittest.TestCase):
    def test_serialized_resolve_results_carry_no_body_leak(self):
        secret_body = "S3CRET-BODY-GUARD " + ("body-leak-guard-fixture " * 20)
        original = bridge._read_cairn
        bridge._read_cairn = lambda request: (0, {"rows": [{"stable_id": "a1", "canonical_raw_locator": None}]})
        try:
            results = bridge.resolve({"id": "record-with-secret-body", "body": secret_body,
                                      "entities": ["apid:root:r", "apid:artifact:a1"]})
            legacy = bridge.parse_record({"id": "legacy", "entities": ["git:abc"], "artifact_refs": ["old/path"]})
            collected = results + [legacy]
            self.assertGreater(len(collected), 0, "empty result set proves nothing about leakage")
            serialized = json.dumps(collected)
            hits = _leak_scan(json.loads(serialized), forbidden_substrings=(secret_body,))
            self.assertEqual(hits, [])
            for result in collected:
                if result["canonical_raw_locator"] is not None:
                    self.assertNotEqual(result["locator_source"], "none")
        finally:
            bridge._read_cairn = original

    def test_outside_locator_not_presented_as_read_target(self):
        root = self.root / "artifact-root"; root.mkdir()
        outside = self.root / "outside-secret"; outside.write_text("outside")
        os.environ["ARTIFACT_ROOT"] = str(root)
        original = bridge._read_cairn
        bridge._read_cairn = lambda request: (0, {"rows": [{"stable_id": "a1"}]})
        try:
            result = bridge.resolve({"id": "r", "entities": ["apid:root:r", "apid:artifact:a1"],
                                      "artifact_refs": [str(outside)]})[0]
            self.assertFalse(result["containment_ok"])
            self.assertEqual(result["integrity"], "broken-locator")
            self.assertIsNotNone(result["locator_source"])
        finally:
            bridge._read_cairn = original; os.environ.pop("ARTIFACT_ROOT", None)


class NoBodyNegative(NoBody):
    def test_scanner_positive_control_detects_seeded_leaks(self):
        original = bridge._read_cairn
        bridge._read_cairn = lambda request: (0, {"rows": [{"stable_id": "a1"}]})
        try:
            produced = bridge.resolve({
                "id": "positive-control", "entities": ["apid:root:r", "apid:artifact:a1"],
            })[0]
        finally:
            bridge._read_cairn = original
        contaminated = dict(produced)
        contaminated["body"] = "sentinel-leak"
        self.assertEqual(_leak_scan(contaminated), ["$.body"])
        contaminated = dict(produced); contaminated["detail"] = "x" * 241
        self.assertEqual(_leak_scan(contaminated), ["$.detail:long:241"])
        contaminated = dict(produced); contaminated["detail"] = "prefix S3CRET-NEEDLE suffix"
        self.assertEqual(_leak_scan(
            contaminated, forbidden_substrings=("S3CRET-NEEDLE",)), ["$.detail:substring"])

    def test_empty_result_scan_is_not_evidence(self):
        # A scan over nothing trivially returns no hits; the primary test's
        # `assertGreater(len(collected), 0)` is what turns "0 hits" into
        # real evidence rather than a vacuous pass.
        self.assertEqual(_leak_scan([]), [])


class Containment(IsolationMixin, unittest.TestCase):
    def test_serialized_resolve_marks_memory_hint_without_transport_use(self):
        root = self.root / "artifact-root"; root.mkdir(); outside = self.root / "outside"; outside.write_text("x")
        os.environ["ARTIFACT_ROOT"] = str(root)
        original = bridge._read_cairn
        calls = []
        bridge._read_cairn = lambda request: (calls.append(request) or (0, {"rows": [{"stable_id": "a"}]}))
        try:
            result = bridge.resolve({"id": "r", "entities": ["apid:root:r", "apid:artifact:a"], "artifact_refs": [str(outside)]})[0]
            self.assertEqual((result["canonical_raw_locator"], result["locator_source"], result["containment_ok"], result["integrity"]), (str(outside), "memory-hint", False, "broken-locator"))
            self.assertEqual(result["fallback_locators"], [str(outside)])
            self.assertEqual(calls[0]["target_ids"], ["a"])
        finally:
            bridge._read_cairn = original; os.environ.pop("ARTIFACT_ROOT", None)

    def test_positive_missing_outside_and_symlink(self):
        root = self.root / "artifact-root"; root.mkdir()
        inside = root / "inside"; inside.write_text("fixture")
        outside = self.root / "outside"; outside.write_text("fixture")
        link = root / "escape"; link.symlink_to(outside)
        self.assertEqual(bridge.valid_fallback_locator(root, "inside"), (True, True))
        self.assertEqual(bridge.valid_fallback_locator(root, "missing"), (True, False))
        self.assertEqual(bridge.valid_fallback_locator(root, str(outside))[0], False)
        self.assertEqual(bridge.valid_fallback_locator(root, "escape"), (False, False))

    def test_produced_results_cover_valid_missing_outside_and_symlink_hints(self):
        root = self.root / "artifact-root"; root.mkdir()
        inside = root / "inside"; inside.write_text("fixture")
        outside = self.root / "outside"; outside.write_text("fixture")
        link = root / "escape"; link.symlink_to(outside)
        os.environ["ARTIFACT_ROOT"] = str(root)
        original = bridge._read_cairn
        requests = []
        bridge._read_cairn = lambda request: (
            requests.append(request) or (0, {"rows": [{"stable_id": "a"}]})
        )
        cases = (
            ("inside", True, "ok"),
            ("missing", True, "broken-locator"),
            (str(outside), False, "broken-locator"),
            ("escape", False, "broken-locator"),
        )
        try:
            for ref, contained, integrity in cases:
                with self.subTest(ref=ref):
                    result = bridge.resolve({
                        "id": "r", "entities": ["apid:root:r", "apid:artifact:a"],
                        "artifact_refs": [ref],
                    })[0]
                    self.assertEqual(result["canonical_raw_locator"], ref)
                    self.assertEqual(result["locator_source"], "memory-hint")
                    self.assertEqual(result["containment_ok"], contained)
                    self.assertEqual(result["integrity"], integrity)
                    self.assertNotIn(ref, requests[-1].get("target_ids", []))
        finally:
            bridge._read_cairn = original; os.environ.pop("ARTIFACT_ROOT", None)


class Telemetry(IsolationMixin, unittest.TestCase):
    def test_exact_25_keys_and_legacy_no_write(self):
        original = bridge._read_cairn
        try:
            bridge._read_cairn = lambda request: (0, {"rows": [{"stable_id": "a"}]})
            bridge.resolve({"id": "memory-secret", "entities": ["apid:root:r", "apid:artifact:a"]})
            path = self._telemetry_path()
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 1); self.assertEqual(set(rows[0]), set(bridge.TELEMETRY_KEYS))
            self.assertEqual(path, Path(os.environ["XDG_STATE_HOME"]) /
                             "hearting/artifact-projection/read-telemetry.jsonl")
            before = path.read_text()
            bridge.resolve({"id": "legacy", "entities": ["note"], "artifact_refs": ["secret-body"]})
            self.assertEqual(path.read_text(), before)
        finally:
            bridge._read_cairn = original

    def test_n_present_n_missing_exact_cardinality_and_recall_isolation(self):
        original = bridge._read_cairn
        n = 3
        present_ids = [f"present-{i}" for i in range(n)]
        missing_ids = [f"missing-{i}" for i in range(n)]
        record_ids = [f"rec-p{i}" for i in range(n)] + [f"rec-m{i}" for i in range(n)]
        secret_bodies = [f"secret-body-for-{pid}" for pid in present_ids]
        secret_refs = [f"secret-ref-for-{i}" for i in range(2 * n)]
        artifact_root = self.root / "artifact-root"; artifact_root.mkdir()
        for ref in secret_refs:
            (artifact_root / ref).write_text("synthetic")
        os.environ["ARTIFACT_ROOT"] = str(artifact_root)
        credential = "w5-secret-credential-value"
        endpoint = "http://127.0.0.1:9/w5-secret-endpoint"
        database_url = "libsql://w5-secret-db.invalid"
        os.environ["CAIRN_READ_TOKEN"] = credential
        os.environ["CAIRN_READ_ENDPOINT"] = endpoint
        os.environ["TURSO_DATABASE_URL"] = database_url

        def fake_read(request):
            targets = request["target_ids"]
            rows = [{"stable_id": t} for t in targets if t in present_ids]
            return 0, {"rows": rows}

        bridge._read_cairn = fake_read
        recall_path = self._recall_events_path()
        recall_path.parent.mkdir(parents=True, exist_ok=True)
        recall_path.write_text("")
        try:
            for i, pid in enumerate(present_ids):
                bridge.resolve({"id": f"rec-p{i}", "body": secret_bodies[i],
                                "artifact_refs": [secret_refs[i]],
                                "entities": ["apid:root:r", f"apid:artifact:{pid}"]})
            for i, mid in enumerate(missing_ids):
                bridge.resolve({"id": f"rec-m{i}", "body": f"missing-secret-body-{i}",
                                "artifact_refs": [secret_refs[n + i]],
                                "entities": ["apid:root:r", f"apid:artifact:{mid}"]})
            serialized = self._telemetry_path().read_text()
            rows = [json.loads(line) for line in serialized.splitlines()]
            self.assertEqual(len(rows), 2 * n)
            present_rows = [r for r in rows if r["pointer_integrity_state"] == "ok"]
            missing_rows = [r for r in rows if r["pointer_integrity_state"] == "missing"]
            self.assertEqual(len(present_rows), n)
            self.assertEqual(len(missing_rows), n)
            self.assertTrue(all(r["zero_result"] is False for r in present_rows))
            self.assertTrue(all(r["zero_result"] is True and r["pointer_bridge"] is True for r in missing_rows))
            self.assertEqual(recall_path.read_text(), "", "resolve() must never append recall-events rows")
            hits = _leak_scan(rows, forbidden_substrings=(
                tuple(secret_bodies) + tuple(f"missing-secret-body-{i}" for i in range(n)) +
                tuple(secret_refs) + tuple(present_ids) + tuple(missing_ids) + tuple(record_ids) +
                (credential, endpoint, database_url)
            ))
            self.assertEqual(hits, [])
            self.assertEqual(rows[0]["query_digest"], hashlib.sha256(b"").hexdigest())
            self.assertEqual(rows[0]["query_token_count"], 0)
        finally:
            bridge._read_cairn = original
            for key in ("ARTIFACT_ROOT", "CAIRN_READ_TOKEN", "CAIRN_READ_ENDPOINT",
                        "TURSO_DATABASE_URL"):
                os.environ.pop(key, None)

    def test_multi_slot_rows_and_declared_worst_state(self):
        original = bridge._read_cairn
        calls = []

        def fake_read(request):
            calls.append(request)
            return 0, {"rows": [{"stable_id": target} for target in request["target_ids"]
                                if target in ("artifact-ok", "cycle-ok")]}

        bridge._read_cairn = fake_read
        try:
            results = bridge.resolve({
                "id": "multi-slot-record",
                "entities": ["apid:root:r", "apid:artifact:artifact-ok",
                             "apid:artifact:artifact-missing", "apid:cycle:cycle-ok"],
            })
        finally:
            bridge._read_cairn = original
        self.assertEqual(len(results), 3)
        rows = [json.loads(line) for line in self._telemetry_path().read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        by_slot = {row["pointer_target_slot"]: row for row in rows}
        self.assertEqual(set(by_slot), {"artifact", "cycle"})
        self.assertEqual(by_slot["artifact"]["pointer_integrity_state"], "missing")
        self.assertEqual(by_slot["cycle"]["pointer_integrity_state"], "ok")
        self.assertEqual(len(calls), 2)

    def test_writer_has_one_callsite_and_legacy_never_calls_it(self):
        source = inspect.getsource(bridge.resolve)
        self.assertEqual(source.count("_telemetry("), 1)
        original = bridge._telemetry
        calls = []
        bridge._telemetry = lambda *args, **kwargs: calls.append((args, kwargs))
        try:
            for index in range(4):
                bridge.resolve({"id": f"legacy-{index}", "entities": ["git:abc"],
                                "artifact_refs": [f"legacy-ref-{index}"]})
        finally:
            bridge._telemetry = original
        self.assertEqual(calls, [])
        source = (HERE / "artifact-pointer-bridge.py").read_text()
        self.assertNotIn("RECALL_EVENTS", source)
        self.assertNotIn("recall-events.jsonl", source)

    def test_legacy_only_records_write_zero_rows(self):
        for i in range(3):
            bridge.resolve({"id": f"legacy-{i}", "entities": ["git:abc"], "artifact_refs": [f"ref-{i}"]})
        path = self._telemetry_path()
        self.assertFalse(path.exists() and path.read_text().strip())


class TelemetryNegative(Telemetry):
    def test_writer_failure_is_typed(self):
        original = bridge._read_cairn
        try:
            bridge._read_cairn = lambda request: (0, {"rows": []})
            state = Path(os.environ["XDG_STATE_HOME"]); state.mkdir(parents=True, exist_ok=True)
            # A directory at the writer path makes the append failure observable.
            (state / "hearting/artifact-projection").mkdir(parents=True)
            (state / "hearting/artifact-projection/read-telemetry.jsonl").mkdir()
            with self.assertRaises(bridge.BridgeError):
                bridge.resolve({"entities": ["apid:root:r", "apid:artifact:a"]})
        finally:
            bridge._read_cairn = original

    def test_scanner_positive_control_for_telemetry_rows(self):
        original = bridge._read_cairn
        bridge._read_cairn = lambda request: (0, {"rows": []})
        try:
            bridge.resolve({"id": "telemetry-control", "entities": ["apid:root:r", "apid:artifact:a"]})
        finally:
            bridge._read_cairn = original
        produced = json.loads(self._telemetry_path().read_text().splitlines()[-1])
        contaminated = dict(produced); contaminated["query_language_hint"] = "S3CRET-ROW-NEEDLE"
        self.assertEqual(_leak_scan(
            [contaminated], forbidden_substrings=("S3CRET-ROW-NEEDLE",)),
            ["$[0].query_language_hint:substring"])


class WriteBoundary(CairnFixtureMixin, unittest.TestCase):
    def test_audit_hook_observes_zero_canonical_writes(self):
        canonical_root = self.root / "canonical-artifact-root"
        canonical_root.mkdir()
        attempted = []
        denied = []
        armed = [False]

        def is_write(event, args):
            if event == "open":
                mode = args[1] if len(args) > 1 else ""
                flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
                return (isinstance(mode, str) and any(char in mode for char in "wax+")) or bool(
                    flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            return event in ("os.rename", "os.replace", "os.remove", "os.unlink", "os.mkdir")

        def hook(event, args):
            if not args or not is_write(event, args):
                return
            try:
                path = Path(args[0]).resolve(strict=False)
                path.relative_to(canonical_root.resolve())
            except (TypeError, ValueError, OSError):
                return
            attempted.append((event, args))
            if armed[0]:
                denied.append((event, args))
                raise PermissionError("canonical fixture root is write-denied")

        sys.addaudithook(hook)
        # Positive control: prove the hook actually observes an in-scope write
        # before trusting a later "0 events" result.
        probe = canonical_root / "probe.txt"
        with open(probe, "a", encoding="utf-8") as handle:
            handle.write("x")
        self.assertTrue(attempted, "audit hook did not observe the positive-control write")
        attempted.clear(); armed[0] = True
        canonical_root.chmod(0o555)

        os.environ["ARTIFACT_ROOT"] = str(canonical_root)
        original = bridge._read_cairn
        bridge._read_cairn = lambda request: (0, {"rows": [{"stable_id": "a1"}]})
        try:
            bridge.resolve({"id": "r", "entities": ["apid:root:r", "apid:artifact:a1"]})
        finally:
            bridge._read_cairn = original
            os.environ.pop("ARTIFACT_ROOT", None)
            canonical_root.chmod(0o755)
        self.assertEqual(attempted, [])
        self.assertEqual(denied, [])

    def test_isolated_store_row_count_digest_and_last_accessed_unchanged(self):
        proc = self._mem_cli("add", "durable", "note", "write boundary fixture body",
                              "--scope", "global", "--source", "write-boundary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        con = sqlite3.connect(self._db_path())
        rid = con.execute("SELECT id FROM records").fetchone()[0]
        con.close()
        before = self._snapshot(rid)
        ro = sqlite3.connect(f"file:{self._db_path()}?mode=ro", uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("UPDATE records SET last_accessed='forbidden' WHERE id=?", (rid,))
        finally:
            ro.close()
        original = bridge._read_cairn
        bridge._read_cairn = lambda request: (0, {"rows": [{"stable_id": "a1"}]})
        try:
            bridge.resolve({"id": rid, "entities": ["apid:root:r", "apid:artifact:a1"]})
        finally:
            bridge._read_cairn = original
        self.assertEqual(self._snapshot(rid), before)
        self.assertEqual(hashlib.sha256(repr(self._snapshot(rid)).encode()).hexdigest(),
                         hashlib.sha256(repr(before).encode()).hexdigest())

    def test_ingest_scoped_credential_is_forbidden(self):
        # The real transport (cairn-artifact-read.ts) intentionally discards
        # the upstream response body/detail on a non-2xx status and replaces
        # it with a fixed local string -- the same privacy suppression that
        # keeps `canonical_raw_locator` out of rejected bodies. Only `code`
        # survives to the bridge; that is the closed oracle here.
        os.environ["CAIRN_READ_TOKEN"] = "w5-ingest-scope-token"
        try:
            returncode, payload = bridge._read_cairn(self._base_request())
            self.assertEqual(self.fixture_server.responses[-1]["detail"], "ingest-scope")
            self.assertEqual(returncode, 3)
            self.assertEqual(payload["error"]["code"], "FORBIDDEN")
            self.assertEqual(payload["error"]["detail"], "Cairn read rejected the request")
            result = bridge._result_for_target(
                {"slot": "artifact", "identity_class": "stable", "value": "a1"}, payload)
            self.assertEqual((result["disposition"], result["read_error_code"],
                              result["integrity"], result["detail"]),
                             ("attempted", "FORBIDDEN", "forbidden",
                              "Cairn read rejected the request"))
        finally:
            os.environ["CAIRN_READ_TOKEN"] = self.fixture_server.token

    def test_mutation_keys_are_rejected_invalid_request(self):
        for key in ("canonical_write", "switch_namespace", "activate", "ingest_scope", "db_url"):
            with self.subTest(key=key):
                request = dict(self._base_request(), **{key: True})
                returncode, payload = bridge._read_cairn(request)
                self.assertEqual(returncode, 4, payload)
                self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")

    def test_extra_argv_is_rejected(self):
        command = REPO_ROOT / "utilities" / "cairn-artifact-read.sh"
        proc = subprocess.run([str(command), "--apply"], input="{}", text=True,
                               capture_output=True, env=os.environ.copy())
        self.assertEqual(proc.returncode, 4)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")

    def test_no_credential_or_endpoint_leak_in_stdio(self):
        artifact_root = self.root / "artifact-root"; artifact_root.mkdir()
        ref = "W5-SECRET-REF"; (artifact_root / ref).write_text("synthetic")
        os.environ["ARTIFACT_ROOT"] = str(artifact_root)
        os.environ["TURSO_DATABASE_URL"] = "libsql://W5-SECRET-DB.invalid"
        record_id = "W5-SECRET-RECORD-ID"
        body = "W5-SECRET-MEMORY-BODY"
        proc = self._bridge_cli({
            "id": record_id, "body": body, "artifact_refs": [ref],
            "entities": ["apid:root:root-a", "apid:artifact:a1"],
        })
        try:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            sensitive = (body, self.fixture_server.token,
                         os.environ["CAIRN_READ_ENDPOINT"], os.environ["TURSO_DATABASE_URL"])
            self.assertEqual(_leak_scan(result, forbidden_substrings=sensitive), [])
            telemetry = [json.loads(line) for line in self._telemetry_path().read_text().splitlines()]
            self.assertTrue(telemetry)
            self.assertEqual(_leak_scan(
                telemetry, forbidden_substrings=sensitive + (record_id, ref, "a1")), [])
            self.assertNotIn("db_url", (proc.stdout + proc.stderr).lower())
            # D-57 intentionally allows these two values only in their closed,
            # typed result fields; telemetry must still exclude both.
            self.assertEqual(result[0]["memory_record_id"], record_id)
            self.assertEqual(result[0]["fallback_locators"], [ref])
        finally:
            os.environ.pop("ARTIFACT_ROOT", None)
            os.environ.pop("TURSO_DATABASE_URL", None)

    def test_no_mutation_keys_and_closed_result(self):
        source = (HERE / "cairn-artifact-read.ts").read_text() if (HERE / "cairn-artifact-read.ts").exists() else ""
        self.assertTrue(bridge.FORBIDDEN_BODY_KEYS.isdisjoint(bridge._RESULT_FIELDS))
        self.assertNotIn("recall-events.jsonl", (HERE / "artifact-pointer-bridge.py").read_text())
        self.assertNotIn("_write", source)


class WriteBoundaryNegative(WriteBoundary):
    def test_forbidden_result_key_is_detected(self):
        original = bridge._read_cairn
        bridge._read_cairn = lambda request: (0, {"rows": [{"stable_id": "a1"}]})
        try:
            produced = bridge.resolve({
                "id": "write-boundary-control",
                "entities": ["apid:root:r", "apid:artifact:a1"],
            })[0]
        finally:
            bridge._read_cairn = original
        contaminated = dict(produced); contaminated["body"] = "sentinel"
        self.assertEqual(_leak_scan(contaminated), ["$.body"])

    def test_unaudited_execution_is_not_a_write_zero_proof(self):
        # Running resolve() with no audit hook installed must not itself be
        # treated as evidence of zero canonical writes; only the hook-backed
        # assertion in WriteBoundary is that oracle.
        original = bridge._read_cairn
        bridge._read_cairn = lambda request: (0, {"rows": [{"stable_id": "a1"}]})
        try:
            bridge.resolve({"id": "r", "entities": ["apid:root:r", "apid:artifact:a1"]})
        finally:
            bridge._read_cairn = original
        with self.assertRaises(AssertionError):
            self.assertTrue(False, "an unaudited run proves nothing about write count")


class Legacy(IsolationMixin, unittest.TestCase):
    def test_subcommands_are_closed_and_legacy_is_read_only(self):
        self.assertEqual(bridge.SUBCOMMANDS, {"parse", "resolve", "duplication-check"})
        self.assertEqual(bridge.parse_record({"artifact_refs": []})["disposition"], "legacy-locator-only")


class LegacyNegative(Legacy):
    def test_empty_duplication_corpus_fails(self):
        with self.assertRaises(bridge.BridgeError):
            bridge.duplication_check([], {})


class ExtractedToken(IsolationMixin, unittest.TestCase):
    def test_quoted_overlong_and_capacity_filling_tokens_are_discarded(self):
        script = """
import json, os, sys
sys.path.insert(0, sys.argv[1]); import mem
entities = [f'ordinary-{i}' for i in range(24)]
body = ' '.join('`apid:artifact:' + ('x' * 60) + '`' for _ in range(12))
rid = mem.write_record('durable', 'global', 'note', body, entities=entities, quiet=True)
con = mem.get_con(); row = con.execute('SELECT entities FROM records WHERE id=?', (rid,)).fetchone(); con.close()
stored = json.loads(row[0]); assert all(not item.startswith('apid:') for item in stored)
assert len(stored) == 24
"""
        proc = subprocess.run([sys.executable, "-c", script, str(MEM_PY.parent)],
                              env=os.environ.copy(), cwd=str(REPO_ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class ExtractedTokenNegative(ExtractedToken):
    def test_candidate_state_is_explicit_only(self):
        extracted = bridge.parse_record({
            "entities": ["ordinary"], "artifact_refs": ["legacy/ref"],
        })
        self.assertEqual(extracted["disposition"], "legacy-locator-only")
        self.assertIsNone(extracted["identity"])


def main():
    import sys
    selector = None
    if "-k" in sys.argv:
        index = sys.argv.index("-k"); selector = sys.argv[index + 1]
        del sys.argv[index:index + 2]
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    if selector:
        suite = unittest.TestSuite(test for test in _flatten(suite) if selector in test.id())
    elif not all(any(selector in test.id() for test in _flatten(suite)) for selector in (
        "Channel", "ChannelNegative", "Identity", "IdentityNegative", "Capacity",
        "CapacityNegative", "Duplication", "DuplicationNegative", "Mapping",
        "MappingNegative", "NoBody", "NoBodyNegative", "Legacy", "LegacyNegative",
        "WriteBoundary", "WriteBoundaryNegative", "Telemetry", "TelemetryNegative",
        "ExtractedToken", "ExtractedTokenNegative")):
        raise SystemExit("selector discovery failure")
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite): yield from _flatten(item)
        else: yield item


if __name__ == "__main__": raise SystemExit(0 if main() else 1)
