"""File-generation reuse contract for parsed registry rows."""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet.collectors import dispatch


class ParsedRegistryRowsTest(unittest.TestCase):
    def test_attempt_record_shares_only_one_pass_and_one_authoritative_root(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory)/'first', Path(directory)/'second'
            for root, value in ((first,'old'), (second,'other-registry')):
                (root/'watchdog').mkdir(parents=True)
                (root/'watchdog'/'same.json').write_text(dispatch.json.dumps({'nested': {'value': value}}))
            a, b = SimpleNamespace(attempt_id='same', root=first), SimpleNamespace(attempt_id='same', root=second)
            with mock.patch.object(dispatch,'_row_state_roots',side_effect=lambda job:(job.root,)):
                with dispatch.collection_read_scope():
                    observed = dispatch._attempt_state_record(a,'watchdog')
                    observed['nested']['value'] = 'caller mutation'
                    (first/'watchdog'/'same.json').write_text('{"nested":{"value":"new"}}')
                    self.assertEqual(dispatch._attempt_state_record(a,'watchdog')['nested']['value'],'old')
                    self.assertEqual(dispatch._attempt_state_record(b,'watchdog')['nested']['value'],'other-registry')
                with dispatch.collection_read_scope():
                    self.assertEqual(dispatch._attempt_state_record(a,'watchdog')['nested']['value'],'new')
                (first/'watchdog'/'same.json').write_text('{"nested":{"value":"outside"}}')
                self.assertEqual(dispatch._attempt_state_record(a,'watchdog')['nested']['value'],'outside')

    def test_merged_projection_reuses_producers_but_returns_owned_outer_maps(self):
        from runtime_observation import ProjectionCache
        meta = ('attempt_schema_version=2,dispatch_depth=1,transport=headless,'
                'execution_surface=registered-headless,registered_worker=1,'
                'fallback_hop=same-harness-headless,route_id=rt,route_node=work,attempt_id=one')
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(dispatch, '_REGISTRY_MERGED_CACHE', ProjectionCache(max_entries=1,max_bytes=65536)):
            first, second = Path(directory)/'first', Path(directory)/'second'
            first.write_text('t\tdone\t/r\t/w\tone\t'+meta+'\n')
            second.write_text('t\tdone\t/r\t/w\ttwo\t'+meta.replace('route_node=work','route_node=test')
                              .replace('attempt_id=one','attempt_id=two')+'\n')
            def read(paths):
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    return dispatch._scan_registry_evidence(paths)
            original = read([first,second])
            again = read([first,second])
            self.assertEqual(again, original)
            self.assertEqual(dispatch._REGISTRY_MERGED_CACHE.admissions, 1)
            self.assertEqual(set(again[0]['rt']), {'work','test'})
            again[0].clear();again[1].clear()
            self.assertEqual(read([first,second]), original)
            first.write_text(first.read_text().replace('\tdone\t','\trunning\t'))
            current = read([first,second])
            self.assertNotIn('one',current[1])
            self.assertIn('two',current[1])
            with mock.patch.object(dispatch, '_REGISTRY_MERGED_CACHE', ProjectionCache(max_bytes=1)):
                self.assertEqual(read([first,second]), current)
                self.assertFalse(dispatch._REGISTRY_MERGED_CACHE)
            second.unlink()
            self.assertNotIn('test', read([first,second])[0]['rt'])

    def test_source_eviction_releases_borrowed_native_projections_and_metadata(self):
        import gc
        import weakref
        class Metadata(dict):
            pass
        references = []
        def convert(record):
            value = Metadata(dispatch._parse_pipe_meta(record.fields[5]))
            references.append(weakref.ref(value))
            return value
        registry = dispatch._orphan_registry_module()
        raw = 't\tdone\t/r\t/w\tone\tattempt_id=one,route_id=rt,route_node=work,delivery_intent=1\n'
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(dispatch, '_registry_record_metadata', side_effect=convert), \
                mock.patch.object(dispatch, '_PARSED_REGISTRY_RAW_BYTES_LIMIT', len(raw)+1):
            first, second = Path(directory)/'first', Path(directory)/'second'
            first.write_text(raw)
            second.write_text(raw.replace('one', 'two'))
            with dispatch._dispatch_registry_cache.registry_read_scope():
                dispatch._orphan_registry_rows(first, registry, read_only=True)
                dispatch._scan_registry_evidence([first])
                dispatch._delivery_intents(first, dispatch.parsed_registry_rows(first))
            self.assertIn(str(first), dispatch._ORPHAN_ROW_PROJECTIONS)
            dispatch.parsed_registry_rows(second)
            gc.collect()
            self.assertNotIn(str(first), dispatch._ORPHAN_ROW_PROJECTIONS)
            self.assertNotIn(str(first), dispatch._DELIVERY_INTENTS)
            self.assertFalse(any(os.path.abspath(key[1]) == str(first)
                                 for key in dispatch._REGISTRY_EVIDENCE_CACHE))
            self.assertIsNone(references[0]())

    def test_source_replacement_charges_removed_rows_before_reusing_the_cache(self):
        from runtime_observation import ProjectionCache
        registry = dispatch._orphan_registry_module()
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(dispatch, '_ORPHAN_ROW_PROJECTIONS', ProjectionCache(max_bytes=65536)):
            path = Path(directory)/'jobs.log'
            path.write_text('t\tdone\t/r\t/w\tone\tattempt_id=one,note=' + 'x'*100000 + '\n')
            with dispatch._dispatch_registry_cache.registry_read_scope():
                dispatch._orphan_registry_rows(path, registry, read_only=True)
            self.assertIn(str(path), dispatch._ORPHAN_ROW_PROJECTIONS)
            path.write_text('')
            with dispatch._dispatch_registry_cache.registry_read_scope():
                dispatch.parsed_registry_rows(path)
            self.assertNotIn(str(path), dispatch._ORPHAN_ROW_PROJECTIONS)
            self.assertEqual(dispatch._ORPHAN_ROW_PROJECTIONS.rejections, 1)

    def test_fingerprint_budget_does_not_change_a_published_history(self):
        from runtime_observation import ProjectionCache
        evidence = dispatch.RegistryRouteEvidence(work={'note': '한글' * 100})
        expected = dispatch.json.dumps(evidence, sort_keys=True, default=str)
        with mock.patch.object(dispatch, '_REGISTRY_FINGERPRINT_CACHE', ProjectionCache(max_bytes=1)):
            self.assertEqual(evidence.fingerprint(), expected)
            self.assertEqual(evidence.fingerprint(), expected)
            self.assertFalse(dispatch._REGISTRY_FINGERPRINT_CACHE)
            self.assertEqual(dispatch._REGISTRY_FINGERPRINT_CACHE.rejections, 2)

    def test_passive_rows_defer_wide_raw_text_without_changing_the_mapping(self):
        registry = dispatch._orphan_registry_module()
        fields = ('t', 'done', '/repo', '/work', '한글🙂',
                  'note=' + 'x' * 100000 + ',attempt_id=att-one')
        eager = registry._row_from_fields(3, fields)
        passive = registry._row_from_fields(3, fields, defer_raw=True)
        self.assertIs(type(eager), dict)
        self.assertNotIn('raw', passive._values)
        self.assertEqual(list(passive), list(eager))
        self.assertEqual(dict(passive), eager)
        self.assertEqual(passive.get('raw'), eager['raw'])
        self.assertNotIn('raw', passive._values)  # reads do not grow the cache
        self.assertEqual(registry.classify(passive, None, {}),
                         registry.classify(eager, None, {}))
        from runtime_observation import retained_size
        self.assertLess(retained_size(passive), retained_size(eager) // 2)
        with self.assertRaises(TypeError):
            passive['status'] = 'running'

    def test_native_passive_adapter_borrows_rows_but_public_adapter_returns_dicts(self):
        registry = dispatch._orphan_registry_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.log'
            path.write_text('t\tdone\t/r\t/w\t한글\tattempt_id=one,note=hello\n')
            with dispatch._dispatch_registry_cache.registry_read_scope():
                passive = dispatch._orphan_registry_rows(path, registry, read_only=True)
                public = dispatch._orphan_registry_rows(path, registry)
            self.assertNotIn('raw', passive[0]._values)
            self.assertIs(type(public[0]), dict)
            self.assertEqual(dict(passive[0]), public[0])
            public[0]['meta']['note'] = 'changed by caller'
            self.assertEqual(passive[0]['meta']['note'], 'hello')

    def test_native_adapter_shares_equal_text_but_preserves_raw_whitespace(self):
        registry = dispatch._orphan_registry_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.log'
            path.write_text('t\tdone\t/r\t/w\tone\tattempt_id=one,note=single spaces\n'
                            't\tdone\t/r\t/w\ttwo\tattempt_id=two,note=double  spaces\n')
            with dispatch._dispatch_registry_cache.registry_read_scope():
                parsed = dispatch.parsed_registry_rows(path)
                passive = dispatch._orphan_registry_rows(path, registry, read_only=True)
            self.assertIs(passive[0]['meta']['note'], parsed[0][2]['note'])
            self.assertEqual(parsed[1][2]['note'], 'double spaces')
            self.assertEqual(passive[1]['meta']['note'], 'double  spaces')
            self.assertEqual([dict(row) for row in passive], registry.read_rows(path))

    def test_oversized_reuse_entries_leave_the_current_observation_usable(self):
        from types import SimpleNamespace
        from runtime_observation import ProjectionCache
        def convert(order, fields, **kwargs):
            return {'order': order, 'meta': dict(kwargs.get('meta') or {})}
        raw = 't\tdone\t/r\t/w\ta\tattempt_id=one,route_id=rt-one,route_node=work\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'jobs.log';path.write_text(raw)
            with mock.patch.object(dispatch, '_ORPHAN_ROW_PROJECTIONS', ProjectionCache(max_bytes=1)):
                first = dispatch._orphan_registry_rows(path, SimpleNamespace(_row_from_fields=convert))
                second = dispatch._orphan_registry_rows(path, SimpleNamespace(_row_from_fields=convert))
                self.assertEqual(first, second)
                self.assertEqual(len(first), 1)
                self.assertFalse(dispatch._ORPHAN_ROW_PROJECTIONS)
            with mock.patch.object(dispatch, '_REGISTRY_EVIDENCE_CACHE', ProjectionCache(max_bytes=1)):
                rows = dispatch.parsed_registry_rows(path)
                first = dispatch._registry_evidence_for_path(path, rows)
                second = dispatch._registry_evidence_for_path(path, rows)
                self.assertEqual(first, second)
                self.assertIn('rt-one', first[0])
                self.assertFalse(dispatch._REGISTRY_EVIDENCE_CACHE)

    def test_source_byte_limit_evicts_all_references_of_the_old_path(self):
        import tempfile
        from unittest import mock
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory)/'first.log', Path(directory)/'second.log'
            raw = 't\tdone\t/r\t/w\ta\tattempt_id=one\n'
            first.write_text(raw); second.write_text(raw.replace('one', 'two'))
            with mock.patch.object(dispatch, '_PARSED_REGISTRY_RAW_BYTES_LIMIT', len(raw)+1):
                dispatch.parsed_registry_rows(first)
                dispatch.parsed_registry_rows(second)
                for cache in (dispatch._PARSED_REGISTRY_ROWS, dispatch._PARSED_REGISTRY_SEGMENTS,
                              dispatch._PARSED_REGISTRY_INDEXES):
                    self.assertFalse(any(key[0] == str(first) for key in cache))
                self.assertTrue(any(key[0] == str(second) for key in dispatch._PARSED_REGISTRY_ROWS))

    def test_metadata_converter_change_invalidates_an_unchanged_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.log'
            path.write_text('t\topen\t/r\t/w\tjob\tattempt_id=original\n')
            with dispatch._dispatch_registry_cache.registry_read_scope():
                original = dispatch.parsed_registry_rows(path)
            with mock.patch.object(dispatch, '_registry_record_metadata',
                                   return_value={'attempt_id': 'new-policy'}) as convert:
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    updated = dispatch.parsed_registry_rows(path)
                self.assertEqual(convert.call_count, 1)
                self.assertEqual(updated[0][2]['attempt_id'], 'new-policy')
                self.assertEqual(original[0][2]['attempt_id'], 'original')

    def test_atomic_row_replacement_reuses_unchanged_metadata_conversions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.log'
            lines = [f't\topen\t/r\t/w\tjob-{i}\tattempt_id=att-{i}\n' for i in range(100)]
            path.write_text(''.join(lines))
            with mock.patch.object(dispatch, '_registry_record_metadata',
                                   wraps=dispatch._registry_record_metadata) as convert:
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    original = dispatch.parsed_registry_rows(path)
                self.assertEqual(convert.call_count, 100)
                replacement = path.with_suffix('.new')
                lines[-1] = lines[-1].replace('\topen\t', '\tdone\t')
                replacement.write_text(''.join(lines))
                replacement.replace(path)
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    current = dispatch.parsed_registry_rows(path)
                self.assertEqual(convert.call_count, 101)
                self.assertIs(current[0][2], original[0][2])
                self.assertEqual(current[-1][1][1], 'done')
                self.assertEqual(original[-1][1][1], 'open')

    def test_unchanged_pinned_rows_need_no_historical_text_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.log'
            row = 't\topen\t/repo\t/work\tslug\tattempt_id=att-one\n'
            path.write_text(row)
            with dispatch._dispatch_registry_cache.registry_read_scope():
                initial = dispatch.parsed_registry_rows(path)
            with mock.patch.object(dispatch._dispatch_registry_cache.RegistryRecord, 'raw',
                                   new_callable=mock.PropertyMock,
                                   side_effect=AssertionError('rebuilt historical raw row')):
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    self.assertIs(dispatch.parsed_registry_rows(path), initial)
                    self.assertEqual(dispatch._registry_diagnostics(path), (None, 0))
            with path.open('a') as handle:
                handle.write(row.replace('att-one', 'att-two'))
            with dispatch._dispatch_registry_cache.registry_read_scope():
                appended = dispatch.parsed_registry_rows(path)
            self.assertEqual([meta['attempt_id'] for _order, _fields, meta in appended],
                             ['att-one', 'att-two'])

    def test_route_evidence_reuses_generation_and_folds_append_without_mutating_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.log'
            metadata = ('attempt_schema_version=2,dispatch_depth=1,transport=headless,'
                        'execution_surface=registered-headless,registered_worker=1,'
                        'fallback_hop=same-harness-headless,attempt_id=att-1,'
                        'route_id=rt-1,route_node=execute,harness=codex')
            row = 't\trunning\t/repo\t/work\tjob\t' + metadata + '\n'
            path.write_text(row)
            with mock.patch.object(dispatch, '_attempt_contract', wraps=dispatch._attempt_contract) as convert:
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    first, _ = dispatch._scan_registry_evidence([path])
                calls = convert.call_count
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    unchanged, _ = dispatch._scan_registry_evidence([path])
                self.assertEqual(convert.call_count, calls)
                self.assertIs(first['rt-1'], unchanged['rt-1'])
                fingerprint = first['rt-1'].fingerprint()
                with path.open('a') as handle:
                    handle.write(row.replace('\trunning\t', '\tdone\t'))
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    appended, terminals = dispatch._scan_registry_evidence([path])
                self.assertEqual(convert.call_count, calls * 2)
                self.assertEqual(first['rt-1']['execute']['status'], 'running')
                self.assertEqual(len(first['rt-1']['execute']['attempt_history']), 1)
                self.assertEqual(len(appended['rt-1']['execute']['attempt_history']), 2)
                self.assertEqual(first['rt-1'].fingerprint(), fingerprint)
                self.assertNotEqual(appended['rt-1'].fingerprint(), fingerprint)
                self.assertIn('att-1', terminals)
                path.write_text(row.replace('rt-1', 'rt-2'))
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    replaced, _ = dispatch._scan_registry_evidence([path])
                self.assertEqual(set(replaced), {'rt-2'})
                self.assertEqual(appended['rt-1']['execute']['status'], 'done')

    def test_orphan_conversions_reuse_unchanged_rows_without_exposing_cached_mutation(self):
        registry = dispatch._orphan_registry_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            first = "t\topen\t/repo\t/work\tone\tattempt_id=att-1\n"
            second = "t\tdone\t/repo\t/work\ttwo\tattempt_id=att-2\n"
            path.write_text(first)
            with mock.patch.object(registry, "_row_from_fields", wraps=registry._row_from_fields) as convert:
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    initial = dispatch._orphan_registry_rows(path, registry)
                initial[0]["meta"]["attempt_id"] = "caller mutation"
                initial[0]["status"] = "caller mutation"
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    unchanged = dispatch._orphan_registry_rows(path, registry)
                self.assertEqual(convert.call_count, 1)
                self.assertEqual(unchanged[0]["meta"]["attempt_id"], "att-1")
                self.assertEqual(unchanged[0]["status"], "open")
                with path.open("a") as handle:
                    handle.write(second)
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    appended = dispatch._orphan_registry_rows(path, registry)
                self.assertEqual(convert.call_count, 2)
                self.assertEqual(len(appended), 2)
                replacement = path.with_suffix(".replacement")
                replacement.write_text(first.replace("open", "done"))
                os.replace(replacement, path)
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    replaced = dispatch._orphan_registry_rows(path, registry)
                self.assertEqual(convert.call_count, 3)
                self.assertEqual(len(replaced), 1)
                self.assertEqual(replaced[0]["status"], "done")
                self.assertEqual(replaced, registry.read_rows(path))
                path.unlink()
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    self.assertEqual(dispatch._orphan_registry_rows(path, registry), [])

    def test_process_backfill_selects_the_same_slug_and_exact_attempt_as_full_indexes(self):
        with tempfile.TemporaryDirectory() as temp:
            current, legacy = Path(temp) / "current", Path(temp) / "legacy"
            current.write_text(
                "t\topen\t/repo\t/wt\tshared\tattempt_id=old,mode=old\n"
                "t\topen\t/repo\t/wt\tshared\tattempt_id=new,mode=new\n"
                "t\tdone\t/repo\t/wt\tother\tattempt_id=other,mode=other\n")
            legacy.write_text("t\topen\t/repo\t/wt\tshared\tattempt_id=old,mode=foreign\n")
            paths = [str(current), str(legacy)]
            all_slugs, all_attempts = dispatch._jobs_log_fields(paths)
            slugs, attempts = dispatch._jobs_log_fields(paths, slugs={"shared"}, attempts={"old"})
            self.assertEqual(slugs, {"shared": all_slugs["shared"]})
            self.assertEqual(attempts, {"old": all_attempts["old"]})
            self.assertEqual(slugs["shared"]["mode"], "new")
            self.assertEqual(attempts["old"]["mode"], "old")

    def test_parent_selection_preserves_duplicates_siblings_and_registry_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            current, legacy = Path(temp) / "current", Path(temp) / "legacy"
            current.write_text(
                "t\topen\t/repo\t/wt\tp\tattempt_id=parent\n"
                "t\tdone\t/repo\t/wt\tp\tattempt_id=parent\n"
                "t\topen\t/repo\t/wt\ts\tattempt_id=sibling,parent_attempt_id=parent\n"
                "t\topen\t/repo\t/wt\tu\tattempt_id=unrelated,parent_attempt_id=other\n")
            legacy.write_text(
                "t\topen\t/repo\t/wt\tu\tattempt_id=unrelated,parent_attempt_id=parent\n")
            paths = [str(current), str(legacy)]
            full = dispatch._parent_extinction_rows(paths)
            selected = dispatch._parent_extinction_rows(paths, {"parent"})
            expected = tuple(row for row in full if row[1].get("attempt_id") == "parent"
                             or row[1].get("parent_attempt_id") == "parent")
            self.assertEqual(selected, expected)
            self.assertEqual(len(selected), 3)
            with mock.patch.object(dispatch, "parsed_registry_rows",
                                   side_effect=AssertionError("no child needs parent evidence")):
                self.assertEqual(dispatch._parent_extinction_rows(paths, set()), ())

    def test_long_metadata_values_scan_field_boundaries_once(self):
        class CountedText(str):
            scans = 0

            def find(self, *args):
                type(self).scans += 1
                return super().find(*args)

            def rfind(self, *args):
                type(self).scans += 1
                return super().rfind(*args)

            def split(self, *args):
                return [type(self)(part) for part in super().split(*args)]

        raw = CountedText("capability=code,note=" + "word " * 2000 + "end,mode=dev")
        legacy, keys = dispatch._registry_metadata_analysis(raw)
        self.assertFalse(legacy)
        self.assertEqual(set(keys), {"note"})
        self.assertLess(raw.scans, 20)  # depends on the three fields, not 2,000 words

    def test_fast_spacing_guard_matches_the_legacy_regex(self):
        legacy_pattern = re.compile(r"(?:^|,)[^=,]*\s[^=,]*=|=\s|\s+[^\s,]*=")
        examples = (
            "capability=code,mode=dev",
            "model_role=deep  maker,mode=dev",
            "model_role=deep maker=fast",
            "model_role=deep\t maker",
            "capability=code, model_role=deep maker",
            "capability=code,note=foo,bar",
            "capability:code(x)",
            "a=wide\u2003b=value",
            "a=wide\u2003value",
            "a=\u2003wide",
            "capability=code,,mode=dev",
            "capability=code=alias,missing",
            "capability=code,=mode",
            "capability=code,mode=dev,",
        )
        for raw in examples:
            eq_pos, colon_pos = raw.find("="), raw.find(":")
            has_whitespace = bool(re.search(r"\s", raw))
            expected = ("(" in raw or eq_pos < 0
                        or colon_pos >= 0 and colon_pos < eq_pos
                        or any("=" not in part for part in raw.split(","))
                        or bool(legacy_pattern.search(raw)))
            self.assertEqual(dispatch._registry_metadata_needs_legacy_parser(raw),
                             expected, raw)
            self.assertEqual(dispatch._registry_metadata_shape(raw),
                             (expected, has_whitespace), raw)

    def test_single_scan_analysis_matches_split_and_regex_oracle(self):
        legacy_pattern = re.compile(r"(?:^|,)[^=,]*\s[^=,]*=|=\s|\s+[^\s,]*=")
        value_pattern = re.compile(r"(?:^|,)([^=,]*)=([^,]*\s[^,]*)(?=,|$)")
        examples = [
            "capability=code,,mode=dev", "capability=code,mode=dev,",
            "capability=code, model_role=deep maker",
            "capability=code model_role=deep maker",
            "capability=code,note=foo =bar", "capability=code,note=foo,bar",
            "capability:code(x)", "a=wide\u2003b=value", "a=wide\u2003value",
            "a=\u2003wide", "a=foo bar=baz", "a=foo bar baz=baz",
            "a=foo, b=bar", "a=foo, b", "a=foo,=bar",
        ]
        keys = ("a", "a b", " a", "_name", "")
        values = ("", "plain", "two words", " two", "two ", "two  words",
                  "wide\u2003value", "foo=bar", "foo bar=baz", "foo =bar")
        examples.extend(f"{key}={value},mode=dev" for key in keys for value in values)
        for raw in examples:
            eq_pos, colon_pos = raw.find("="), raw.find(":")
            needs_legacy = ("(" in raw or eq_pos < 0
                            or colon_pos >= 0 and colon_pos < eq_pos
                            or any("=" not in part for part in raw.split(","))
                            or bool(re.search(r"\s", raw)
                                    and legacy_pattern.search(raw)))
            keys_with_whitespace = (() if needs_legacy else tuple(
                match.group(1) for match in value_pattern.finditer(raw)))
            expected = (needs_legacy, keys_with_whitespace)
            actual = dispatch._registry_metadata_analysis(raw)
            self.assertEqual(actual[0], expected[0], raw)
            self.assertEqual(set(actual[1]), set(expected[1]), raw)

    def test_canonical_metadata_normalizes_only_values_with_whitespace(self):
        class CountingWhitespace:
            def __init__(self):
                self.calls = 0

            def search(self, value):
                self.calls += 1
                return re.search(r"\s", value)

        pairs = {"key%d" % index: "value%d" % index for index in range(120)}
        pairs["note"] = "first\t  value"
        raw = ",".join("%s=%s" % (key, value) for key, value in pairs.items())
        record = mock.Mock(fields=("t", "open", "/repo", "/work", "owner", raw))
        record.completion_metadata.return_value = pairs
        whitespace = CountingWhitespace()
        with mock.patch.object(dispatch, "_REGISTRY_METADATA_WHITESPACE_RE", whitespace):
            metadata = dispatch._registry_record_metadata(record)
        self.assertEqual(whitespace.calls, 0)
        self.assertEqual(metadata["key119"], "value119")
        self.assertEqual(metadata["note"], "first value")
        self.assertEqual(metadata["_name"], None)

    def test_whitespace_normalization_matches_legacy_parser_oracle(self):
        examples = (
            "capability=autopilot-code,model_role=deep  maker,mode=dev",
            "capability=code,note=wide\u2003value,mode=dev",
            "capability=code,note=tail\t  ,mode=dev",
            "capability=code,note=pre=post  value,mode=dev",
            "capability=code,note=first  value,note=last",
            "capability=code,note=first,note=last  value",
            "capability=code model_role=deep maker",
            "capability:code(x)",
        )
        for raw in examples:
            pairs = {}
            for part in raw.split(","):
                if "=" in part:
                    key, value = part.split("=", 1)
                    pairs[key] = value
            record = mock.Mock(fields=("t", "open", "/repo", "/work", "owner", raw))
            record.completion_metadata.return_value = pairs
            self.assertEqual(dispatch._registry_record_metadata(record),
                             dispatch._parse_pipe_meta_uncached(raw), raw)

    def test_nonlegacy_whitespace_values_keep_normalization(self):
        raw = "capability=autopilot-code,model_role=deep\t maker"
        record = mock.Mock(fields=("t", "open", "/repo", "/work", "owner", raw))
        record.completion_metadata.return_value = {
            "capability": "autopilot-code", "model_role": "deep\t maker"}
        self.assertEqual(dispatch._registry_record_metadata(record), {
            "capability": "autopilot-code", "model_role": "deep maker",
            "_name": "code"})

    def test_comma_delimited_role_values_reuse_the_existing_generation_parse(self):
        raw = "capability=autopilot-code,model_role=deep  maker,mode=dev"
        expected = dispatch._parse_pipe_meta_uncached(raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("t\topen\t/repo\t/work\towner\t" + raw + "\n")
            with dispatch._dispatch_registry_cache.registry_read_scope(), \
                 mock.patch.object(dispatch, "_parse_pipe_meta", side_effect=AssertionError("reparsed")):
                rows = dispatch.parsed_registry_rows(path)
        self.assertEqual(dict(rows[0][2]), expected)

    def test_noncanonical_metadata_keeps_the_legacy_parser_semantics(self):
        examples = ["capability=autopilot-code model_role=deep maker",
                    "capability=autopilot-code,model_role= deep maker",
                    "capability=autopilot-code,note=foo =bar",
                    "capability=autopilot-code,note=foo,bar",
                    "capability=autopilot-code, model_role=deep maker"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("".join("t\topen\t/repo\t/work\towner\t" + raw + "\n"
                                    for raw in examples))
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
        self.assertEqual([dict(row[2]) for row in rows],
                         [dispatch._parse_pipe_meta_uncached(raw) for raw in examples])

    def test_collection_adapts_registry_records_without_reparsing_common_metadata(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text(
                "open\tslug\t/repo\t/work\tstamp\tcapability=autopilot-code,mode=dev\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual(rows[0][2]["capability"], "autopilot-code")
            self.assertEqual(rows[0][2]["_name"], "code")
            self.assertEqual(rows[0][2]["mode"], "dev")
            self.assertEqual(dispatch._parse_pipe_meta_cached.cache_info().misses, 0)

    def test_canonical_metadata_skips_legacy_whitespace_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            raw = "capability=autopilot-code,mode=dev,attempt_id=att-1"
            path.write_text("t\topen\t/repo\t/work\towner\t" + raw + "\n")
            with (dispatch._dispatch_registry_cache.registry_read_scope(),
                  mock.patch.object(dispatch, "_parse_pipe_meta",
                                    side_effect=AssertionError("canonical metadata reparsed"))):
                rows = dispatch.parsed_registry_rows(path)
        self.assertEqual(dict(rows[0][2]), dispatch._parse_pipe_meta_uncached(raw))

    def test_attempt_index_preserves_first_duplicate_registry_row(self):
        first = {"order": 1, "meta": {"attempt_id": "att-1", "value": "first"}}
        duplicate = {"order": 2, "meta": {"attempt_id": "att-1", "value": "later"}}
        missing = {"order": 3, "meta": {"value": "missing-attempt"}}
        index = dispatch._index_registry_rows_by_attempt([first, duplicate, missing])
        self.assertIs(index["att-1"], first)
        self.assertIs(index[None], missing)

    def test_orphan_row_adapter_matches_dispatch_registry_contract(self):
        registry = dispatch._orphan_registry_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text(
                "t\topen\t/repo\t/work\towner\tcapability=code,attempt_id=att-1\n"
                "malformed\trow\n"
                "t\tdone\t/repo\t/work\tlegacy\tcapability:code(x)\n"
                "t\topen\t/repo\t/work\tworker\tmodel_role=deep  maker,attempt_id=att-2\n"
                "t\topen\t/repo\t/work\tlegacy-name\tcapability=code,_name=explicit\n",
                encoding="utf-8")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                parsed = dispatch.parsed_registry_rows(path)
                expected_lines = ["\t".join(fields) for _order, fields, _meta in parsed]
                expected = registry.read_rows(path, lines=expected_lines)
                for row, (physical_order, fields, _meta) in zip(expected, parsed):
                    row["order"] = physical_order
                    row["raw"] = "\t".join(fields)
                actual = dispatch._orphan_registry_rows(path, registry)
        self.assertEqual(actual, expected)

    def test_owned_row_metadata_keeps_contract_validation_without_copy(self):
        registry = dispatch._orphan_registry_module()
        metadata = {
            "_name": "code", "attempt_schema_version": "2", "dispatch_depth": "99",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
        }
        fields = ("t", "open", "/repo", "/work", "owner",
                  "attempt_schema_version=2,dispatch_depth=99,transport=headless,"
                  "execution_surface=registered-headless,registered_worker=1,"
                  "fallback_hop=same-harness-headless")
        row = registry._row_from_fields(0, fields, meta=metadata, meta_owned=True)
        self.assertIs(row["meta"], metadata)
        self.assertEqual(row["attempt_contract_status"], "invalid:invalid-dispatch-depth")
        self.assertEqual(metadata["_name"], "code")

    def test_orphan_rows_are_shared_only_through_explicit_pass_cache(self):
        registry = object()
        rows = [{"order": 0, "meta": {"attempt_id": "att-1"}}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            cache = {}
            with mock.patch.object(dispatch, "_orphan_registry_rows",
                                   return_value=rows) as reader:
                first = dispatch._orphan_registry_rows_for_pass(path, registry, cache)
                second = dispatch._orphan_registry_rows_for_pass(
                    os.path.abspath(path), registry, cache)
                self.assertIs(first, second)
                self.assertEqual(reader.call_count, 1)
                dispatch._orphan_registry_rows_for_pass(path, registry, None)
                dispatch._orphan_registry_rows_for_pass(path, registry, None)
                self.assertEqual(reader.call_count, 3)

    def test_orphan_rows_cache_is_bounded_and_overflow_reads_without_eviction(self):
        registry = object()
        cache = {
            f"/registry/{index}": [{"order": index, "meta": {"attempt_id": str(index)}}]
            for index in range(dispatch._ORPHAN_ROWS_PASS_CACHE_LIMIT)
        }
        retained = dict(cache)
        fresh = [{"order": 999, "meta": {"attempt_id": "fresh"}}]
        with mock.patch.object(dispatch, "_orphan_registry_rows", return_value=fresh) as reader:
            first = dispatch._orphan_registry_rows_for_pass("/registry/overflow", registry, cache)
            second = dispatch._orphan_registry_rows_for_pass("/registry/overflow", registry, cache)
        self.assertIs(first, fresh)
        self.assertIs(second, fresh)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(len(cache), dispatch._ORPHAN_ROWS_PASS_CACHE_LIMIT)
        self.assertEqual(cache, retained)

    def test_orphan_rows_cache_is_fresh_for_each_pinned_pass(self):
        registry = object()
        path = "/registry/shared"
        first_rows = [{"order": 0, "meta": {"attempt_id": "first"}}]
        second_rows = [{"order": 0, "meta": {"attempt_id": "second"}}]
        with mock.patch.object(dispatch, "_orphan_registry_rows",
                               side_effect=[first_rows, second_rows]) as reader:
            first = dispatch._orphan_registry_rows_for_pass(path, registry, {})
            second = dispatch._orphan_registry_rows_for_pass(path, registry, {})
        self.assertIs(first, first_rows)
        self.assertIs(second, second_rows)
        self.assertEqual(reader.call_count, 2)

    def test_collection_falls_back_for_legacy_and_whitespace_continuation_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            legacy = "open\tlegacy\t/repo\t/work\tstamp\tautopilot-code:dev(x)\n"
            continuation = (
                "open\tworker\t/repo\t/work\tstamp\t"
                "capability=autopilot-code model_role=deep maker\n")
            path.write_text(legacy + continuation)
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual(rows[0][2]["_name"], "code")
            self.assertEqual(rows[0][2]["mode"], "dev")
            self.assertEqual(rows[1][2]["model_role"], "deep maker")

    def test_reuses_immutable_rows_until_file_generation_changes(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("open\tslug\t/repo\t/work\t2026-01-01\tcapability=code\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                first = dispatch.parsed_registry_rows(path)
                first_misses = dispatch._parse_pipe_meta_cached.cache_info().misses
                second = dispatch.parsed_registry_rows(path)
            self.assertIs(first, second)
            self.assertEqual(dispatch._parse_pipe_meta_cached.cache_info().misses, first_misses)
            self.assertEqual(first[0][0], 0)
            self.assertEqual(len(first[0][1]), 6)
            with self.assertRaises(TypeError):
                first[0][2]["mode"] = "changed"

            with path.open("a") as stream:
                stream.write("\n")
                stream.write("bad\trow\n")
            with path.open("a") as stream:
                stream.write("open\tslug2\t/repo\t/work\t2026-01-02\tmode=dev\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                third = dispatch.parsed_registry_rows(path)
            self.assertIsNot(first, third)
            self.assertEqual(
                dispatch._parse_pipe_meta_cached.cache_info().misses, first_misses)
            self.assertEqual(len(third), 2)
            self.assertEqual(third[-1][0], 3)
            self.assertIs(third[0], first[0])

    def test_registry_order_keeps_physical_zero_based_line_positions(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            valid = "open\tslug\t/repo\t/work\t2026-01-01\tcapability=code\n"
            path.write_text("\nmalformed\n" + valid)
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[0] for row in rows], [2])

            with path.open("a") as stream:
                stream.write("\n")
                stream.write("also\tmalformed\n")
                stream.write("open\tslug2\t/repo\t/work\t2026-01-02\tmode=dev\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                appended = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[0] for row in appended], [2, 5])

    def test_multiple_appends_reuse_each_prefix_without_dropping_or_repeating_rows(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("\nmalformed\ndate\topen\t/repo\t/work\tslug0\tcapability=cap0\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[0] for row in rows], [2])
            misses = dispatch._parse_pipe_meta_cached.cache_info().misses

            expected_orders = [2]
            for index in range(1, 4):
                with path.open("a") as stream:
                    stream.write(f"date\topen\t/repo\t/work\tslug{index}\tcapability=cap{index}\n")
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    rows = dispatch.parsed_registry_rows(path)
                expected_orders.append(index + 2)
                self.assertEqual([row[0] for row in rows], expected_orders)
                self.assertEqual(
                    [row[1][4] for row in rows], [f"slug{i}" for i in range(index + 1)])
                self.assertEqual(
                    dispatch._parse_pipe_meta_cached.cache_info().misses, misses)

    def test_cache_is_isolated_by_path_and_file_generation(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.log"
            second_path = Path(directory) / "second.log"
            first_path.write_text("date\topen\t/repo\t/work\tfirst\tcapability=first\n")
            second_path.write_text("date\topen\t/repo\t/work\tsecond\tcapability=second\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                first = dispatch.parsed_registry_rows(first_path)
                second = dispatch.parsed_registry_rows(second_path)
            self.assertEqual(first[0][1][4], "first")
            self.assertEqual(second[0][1][4], "second")

            first_path.write_text(
                "date\topen\t/repo\t/work\treplaced\tcapability=replaced\n"
                "date\topen\t/repo\t/work\tnew\tcapability=new\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                replaced = dispatch.parsed_registry_rows(first_path)
                unchanged_second = dispatch.parsed_registry_rows(second_path)
            self.assertEqual([row[1][4] for row in replaced], ["replaced", "new"])
            self.assertIs(second, unchanged_second)

    def test_missing_registry_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(dispatch.parsed_registry_rows(Path(directory) / "missing"), ())

    def test_large_generation_keeps_every_unique_row_after_parser_lru_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            count = 8205
            path.write_text("".join(
                f"open\tslug-{index}\t/repo\t/work\tstamp\tkey=value-{index}\n"
                for index in range(count)))
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual(len(rows), count)
            self.assertEqual(rows[0][0], 0)
            self.assertEqual(rows[-1][0], count - 1)
            self.assertEqual(rows[0][2]["key"], "value-0")
            self.assertEqual(rows[-1][2]["key"], f"value-{count - 1}")

    def test_incomplete_tail_is_omitted_until_its_newline_arrives(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            first = "stamp\topen\t/repo\t/work\tfirst\tkey=first\n"
            tail = "stamp\topen\t/repo\t/work\tsecond\tkey=second"
            path.write_text(first + tail)
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[1][4] for row in rows], ["first"])

            with path.open("a") as stream:
                stream.write("\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                completed = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[1][4] for row in completed], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
