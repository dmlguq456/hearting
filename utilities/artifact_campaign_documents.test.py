"""Campaign document ownership and legacy-root aliases, using private fixtures only."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
from datetime import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from urllib.parse import quote
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import artifact_campaign_documents as documents
import artifact_entrance as entrance
import artifact_lifecycle as lifecycle
import artifact_producer as producer

CAMPAIGN_PAGE = (
    '<!-- hearting:campaign-document v1 -->\n# Demo campaign\n\n갱신 2026-11-19 18:16\n\n'
    '**한 줄**: Demo one line.\n\n## 결정 기다리는 것\n지금 없음.\n\n## 하는 중\n- a route\n\n'
    '## 끝난 일\n- a done route · #1\n\n## 예정\n- a planned item\n')
PAGE = '# Demo status\n\nUpdated 2026-11-01 09:00\n\n## Next\n- keep this decision\n'


class CampaignDocumentsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='campaign-documents-')
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.root = self.project / '.agent_reports'
        self.campaign = self.root / 'campaigns/2026-11-01_demo'
        self.campaign.mkdir(parents=True)
        self.campaign_id = 'camp_' + '1' * 32
        (self.campaign / 'campaign.json').write_text(json.dumps({
            'campaign_id': self.campaign_id, 'key': 'demo', 'title': 'Demo',
            'goal': 'g', 'state': 'active', 'cycles': [], 'created_on': '2026-11-01T00:00:00Z'}))
        self.route = {'route_id': 'rt-' + '1' * 16, 'artifact_root': str(self.root),
                      'campaign_key': 'demo', 'slug': 'complete-work', 'capability': 'autopilot-code'}

    def legacy(self):
        path = self.campaign / '2026-11-02_old/artifacts/STATUS.md'
        path.parent.mkdir(parents=True)
        path.write_text(PAGE)
        return path

    def status(self):
        return documents.campaign_status(self.campaign)

    def test_campaign_page_wins_over_a_newer_legacy_page_without_moving_it(self):
        canonical = self.campaign / '현황.md'; canonical.write_text(PAGE)
        old = self.legacy()
        os.utime(canonical, (1, 1)); os.utime(old, (2000000000, 2000000000))
        self.assertEqual(self.status()['path'], canonical)
        self.assertEqual(canonical.read_text(), PAGE)
        self.assertEqual(old.read_text(), PAGE)

    def test_campaign_md_is_opened_by_name_before_the_older_names(self):
        page = self.campaign / 'campaign.md'; page.write_text(CAMPAIGN_PAGE)
        (self.campaign / '현황.md').write_text(PAGE); (self.campaign / 'status.md').write_text('# Other\n')
        self.legacy()
        chosen = documents.campaign_status(self.campaign)
        self.assertEqual((chosen['status'], chosen['path']), ('known', page))
        self.assertEqual(documents.select_status(self.campaign)['status'], 'ambiguous')  # the old rule is unchanged
        from tools.fleet.collectors import project_status
        project_status.clear_cache(); self.addCleanup(project_status.clear_cache)
        self.assertEqual(project_status.find_documents(str(self.root)), [str(page)])
        record = project_status.newest(str(self.root))
        self.assertEqual((record['title'], record['line']), ('Demo campaign', 'Demo one line.'))
        self.assertEqual((record['decisions'], record['next']), ([], ['a planned item']))

    def test_campaign_md_is_found_in_a_folder_longer_than_the_list_limit(self):
        page = self.campaign / 'campaign.md'; page.write_text(CAMPAIGN_PAGE)
        (self.campaign / '현황.md').write_text(PAGE)
        for number in range(documents.LIST_LIMIT + 5):
            (self.campaign / ('x%04d.txt' % number)).write_text('')
        self.assertEqual(documents.campaign_status(self.campaign)['path'], page)
        from tools.fleet.collectors import project_status
        project_status.clear_cache(); self.addCleanup(project_status.clear_cache)
        self.assertEqual(project_status.find_documents(str(self.root)), [str(page)])

    def test_unusable_campaign_md_is_unknown_and_no_older_name_stands_in(self):
        old = self.campaign / '현황.md'; old.write_text(PAGE)
        self.legacy()
        from tools.fleet.collectors import project_status
        page = self.campaign / 'campaign.md'
        outside = self.project / 'outside.md'; outside.write_text(CAMPAIGN_PAGE)
        for make in (lambda: page.write_text('# Large\n' + 'x' * 70000), lambda: page.write_bytes(b'\xff\xfe'),
                     lambda: page.symlink_to(outside), lambda: page.mkdir()):
            with self.subTest(make=make):
                project_status.clear_cache()
                make()
                chosen = documents.campaign_status(self.campaign)
                self.assertEqual((chosen['status'], chosen['path']), ('unknown', None))
                self.assertEqual(project_status.find_documents(str(self.root)), [])
                if page.is_dir():
                    page.rmdir()
                else:
                    page.unlink()
        project_status.clear_cache()
        self.assertEqual(documents.campaign_status(self.campaign)['path'], old)  # nothing left: the old path as before

    def test_a_missing_campaign_md_costs_one_stat_and_no_safe_open(self):
        old = self.campaign / '현황.md'; old.write_text(PAGE)
        with mock.patch.object(documents, 'read_document', side_effect=AssertionError('opened')) as opened:
            self.assertIsNone(documents.campaign_page(self.campaign))
        opened.assert_not_called()
        self.assertEqual(documents.campaign_status(self.campaign)['path'], old)

    def test_campaign_md_is_looked_for_only_in_the_campaign_folder(self):
        old = self.legacy()
        (old.parent / 'campaign.md').write_text(CAMPAIGN_PAGE)
        self.assertEqual(documents.campaign_status(self.campaign)['path'], old)
        from tools.fleet.collectors import project_status
        project_status.clear_cache(); self.addCleanup(project_status.clear_cache)
        self.assertEqual(project_status.find_documents(str(self.root)), [str(old)])

    def test_conflicting_campaign_pages_are_unknown_and_do_not_select_a_legacy_page(self):
        a = self.campaign / '현황.md'; b = self.campaign / 'STATUS.md'
        a.write_text(PAGE); b.write_text(PAGE.replace('Demo', 'Another'))
        old = self.legacy(); before = {p: p.read_bytes() for p in (a, b, old)}
        self.assertEqual((self.status()['status'], self.status()['path']), ('ambiguous', None))
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_campaign_status_handoff_and_topic_join_the_common_entrance_without_cycles(self):
        names = ['현황.md', 'HANDOFF-seat.md', 'topic.md']
        for name in names:(self.campaign / name).write_text('# '+name+'\n\nKnown notes.\n')
        result = entrance.scan(self.root)
        rows = [r for r in result['recent'] if r.get('kind') == 'campaign-document']
        self.assertEqual({Path(r['doc_path']).name for r in rows}, set(names))
        self.assertEqual(result['totals']['cycles'], 0)
        self.assertTrue(all(not r.get('cycle_id') for r in rows))

    def test_the_campaigns_own_page_is_no_route_report_row_and_hides_the_hand_written_status_names(self):
        (self.campaign / 'campaign.md').write_text(CAMPAIGN_PAGE)
        (self.campaign / '현황.md').write_text(PAGE)
        (self.campaign / 'status.md').write_text(PAGE)
        (self.campaign / 'HANDOFF-seat.md').write_text('# Handoff\n')
        rows = [r for r in entrance.scan(self.root)['recent'] if r.get('kind') == 'campaign-document']
        self.assertEqual({Path(r['doc_path']).name for r in rows}, {'HANDOFF-seat.md'})
        self.assertEqual(documents.campaign_status(self.campaign)['path'], self.campaign / 'campaign.md')
        self.assertEqual(PAGE, (self.campaign / '현황.md').read_text())   # hidden, never touched

    def test_a_campaign_md_of_another_case_is_not_the_page_and_hides_nothing(self):
        """Round 3: the readers open the exact name `campaign.md`; `Campaign.md` is an ordinary document."""
        (self.campaign / 'Campaign.md').write_text(CAMPAIGN_PAGE)
        (self.campaign / '현황.md').write_text(PAGE)
        rows = [r for r in entrance.scan(self.root)['recent'] if r.get('kind') == 'campaign-document']
        self.assertEqual({Path(r['doc_path']).name for r in rows}, {'Campaign.md', '현황.md'})
        self.assertEqual(documents.campaign_status(self.campaign)['path'], self.campaign / '현황.md')

    def test_a_campaign_without_campaign_md_still_lists_its_hand_written_status_page(self):
        (self.campaign / '현황.md').write_text(PAGE)
        (self.campaign / 'HANDOFF-seat.md').write_text('# Handoff\n')
        rows = [r for r in entrance.scan(self.root)['recent'] if r.get('kind') == 'campaign-document']
        self.assertEqual({Path(r['doc_path']).name for r in rows}, {'현황.md', 'HANDOFF-seat.md'})

    def test_a_document_replaced_while_it_is_read_is_read_once_more(self):
        (self.campaign / 'campaign.md').write_text(CAMPAIGN_PAGE)
        real, calls = documents._read_once, []

        def once(folder, name):
            calls.append(name)
            if len(calls) == 1:
                raise ValueError('document-changed')
            return real(folder, name)
        with mock.patch.object(documents, '_read_once', once):
            raw, _info, coverage = documents.read_document(self.campaign, 'campaign.md')
        self.assertEqual((len(calls), coverage, raw.decode()), (2, 'complete', CAMPAIGN_PAGE))
        calls.clear()
        with mock.patch.object(documents, '_read_once', side_effect=ValueError('document-changed')) as always:
            with self.assertRaises(ValueError):
                documents.read_document(self.campaign, 'campaign.md')
        self.assertEqual(always.call_count, 2)   # one more read, not a loop

    def test_normal_writer_links_the_legacy_root_but_read_only_resolution_does_not(self):
        project = self.project / 'legacy-project'; root = project / '.claude_reports'
        root.mkdir(parents=True)
        env = {k:v for k,v in os.environ.items() if k != 'AGENT_ARTIFACT_ROOT'}
        found = subprocess.check_output(['sh', str(HERE / 'artifact-root.sh'), str(project)], env=env, text=True).strip()
        self.assertEqual(Path(found), root)
        alias = project / '.agent_reports'; self.assertFalse(alias.exists())
        producer.activate(root, repository_id='repo_'+'a'*32, artifact_root_id='root_'+'b'*32)
        original = lifecycle.read_root_identity(root)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.resolve(), root.resolve())
        self.assertEqual(lifecycle.read_root_identity(alias), original)

    def test_already_active_legacy_root_keeps_its_identity_and_open_route_bytes(self):
        project = self.project / 'active-legacy'; root = project / '.claude_reports'
        root.mkdir(parents=True)
        ids = {'repository_id': 'repo_'+'a'*32, 'artifact_root_id': 'root_'+'b'*32}
        producer.activate(root, **ids)
        alias = project / '.agent_reports'
        if alias.is_symlink():alias.unlink()  # Fixture of a pre-alias active root.
        route = root / '.runtime/routes/rt-1111111111111111.json'
        route.parent.mkdir(parents=True, exist_ok=True)
        original_route = json.dumps({'route_id': 'rt-'+'1'*16, 'artifact_root': str(root),
                                     'artifact_root_id': ids['artifact_root_id'], 'state': 'open'}).encode()
        route.write_bytes(original_route)
        identity = lifecycle.read_root_identity(root)
        producer.activate(root, **ids)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(route.read_bytes(), original_route)
        self.assertEqual(lifecycle.read_root_identity(alias), identity)

    def test_distinct_existing_roots_are_never_joined_or_overwritten(self):
        project = self.project / 'two-roots'
        legacy, modern = project / '.claude_reports', project / '.agent_reports'
        legacy.mkdir(parents=True); modern.mkdir()
        (modern / 'sentinel').write_text('separate root')
        producer.activate(modern, repository_id='repo_'+'c'*32, artifact_root_id='root_'+'d'*32)
        original = lifecycle.read_root_identity(modern)
        producer.activate(legacy, repository_id='repo_'+'a'*32, artifact_root_id='root_'+'b'*32)
        self.assertFalse(modern.is_symlink())
        self.assertEqual((modern / 'sentinel').read_text(), 'separate root')
        self.assertEqual(lifecycle.read_root_identity(modern), original)
        self.assertNotEqual(lifecycle.read_root_identity(legacy), original)

    def test_dangling_and_cyclic_aliases_are_preserved_and_legacy_work_still_runs(self):
        for target in ('missing', '.agent_reports'):
            with self.subTest(target=target):
                project = self.project / ('broken-'+str(len(target)))
                root = project / '.claude_reports'; root.mkdir(parents=True)
                alias = project / '.agent_reports'; alias.symlink_to(target)
                before = alias.lstat()
                result = producer.activate(root, repository_id='repo_'+'a'*32, artifact_root_id='root_'+'b'*32)
                self.assertEqual(result['status'], 'activated')
                self.assertEqual(os.readlink(alias), target)
                self.assertEqual(alias.lstat().st_ino, before.st_ino)

    def test_alias_is_made_in_the_primary_checkout_without_touching_a_linked_worktree(self):
        project = self.project / 'git-project'; project.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(project), *args], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git('init', '-q'); git('config', 'user.name', 'fixture'); git('config', 'user.email', 'fixture@example.invalid')
        (project / 'seed').write_text('source')
        git('add', 'seed'); git('commit', '-qm', 'fixture')
        linked = self.project / 'linked'; git('worktree', 'add', '-qb', 'fixture-linked', str(linked))
        root = project / '.claude_reports'; root.mkdir()
        env = {k:v for k,v in os.environ.items() if k != 'AGENT_ARTIFACT_ROOT'}
        command = ['sh', str(HERE / 'artifact-root.sh'), str(linked)]
        self.assertEqual(Path(subprocess.check_output(command, env=env, text=True).strip()), root)
        producer.activate(root, repository_id='repo_'+'a'*32, artifact_root_id='root_'+'b'*32)
        self.assertTrue((project / '.agent_reports').is_symlink())
        self.assertFalse((linked / '.agent_reports').exists())
        self.assertEqual(Path(subprocess.check_output(command, env=env, text=True).strip()), root)

    def test_document_symlinks_cannot_borrow_content_from_outside_the_root(self):
        outside = self.project / 'outside.md'; outside.write_text('# outside\n')
        (self.campaign / 'topic.md').symlink_to(outside)
        before = outside.read_bytes()
        result = entrance.scan(self.root)
        self.assertFalse(any(Path(r['doc_path']).name == 'topic.md' for r in result['recent']))
        self.assertEqual(outside.read_bytes(), before)

    def test_legacy_only_status_is_still_read_and_no_page_is_created(self):
        self.assertIsNone(self.status()['path'])
        self.assertFalse((self.campaign / '현황.md').exists())
        old = self.legacy()
        self.assertEqual(self.status()['path'], old)
        self.assertEqual(old.read_text(), PAGE)

    def test_another_campaigns_newer_status_is_not_the_routes_page(self):
        own = self.campaign / '현황.md'; own.write_text(PAGE)
        other = self.root / 'campaigns/2026-11-02_other/현황.md'
        other.parent.mkdir(); other.write_text(PAGE.replace('Demo', 'Other'))
        os.utime(own, (1, 1)); os.utime(other, (2000000000, 2000000000))
        before = other.read_bytes()
        self.assertEqual(self.status()['path'], own)
        self.assertEqual(other.read_bytes(), before)

    def test_conflicting_status_rows_keep_both_candidates_and_report_uncertainty(self):
        for name, text in [('현황.md', PAGE), ('STATUS.md', '# A different judgment\n')]:
            (self.campaign / name).write_text(text)
        result = entrance.scan(self.root)
        rows = [r for r in result['recent'] if r.get('kind') == 'campaign-document']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['status'] == 'ambiguous' for r in rows))
        self.assertTrue(all(r['campaign_id'] == self.campaign_id and r.get('artifact_id') is None for r in rows))
        source = next(s for s in result['source_status'] if s['kind'] == 'campaign-document')
        self.assertEqual(source['coverage'], 'partial')
        self.assertTrue(source['errors'])

    def test_unsafe_canonical_status_is_not_replaced_by_a_legacy_copy(self):
        outside = self.project / 'outside-status.md'; outside.write_text(PAGE)
        canonical = self.campaign / '현황.md'; canonical.symlink_to(outside)
        old = self.legacy()
        self.assertEqual((self.status()['status'], self.status()['path']), ('unknown', None))
        self.assertEqual(old.read_text(), PAGE)
        self.assertEqual(outside.read_text(), PAGE)

    def test_identity_rejection_cannot_create_a_legacy_alias(self):
        project = self.project / 'rejected-identity'; root = project / '.claude_reports'
        root.mkdir(parents=True)
        producer.activate(root, repository_id='repo_'+'a'*32, artifact_root_id='root_'+'b'*32)
        alias = project / '.agent_reports'
        if alias.is_symlink():alias.unlink()
        with self.assertRaises(producer.ProducerError):
            producer.activate(root, repository_id='repo_'+'c'*32, artifact_root_id='root_'+'d'*32)
        self.assertFalse(alias.exists())

    def test_oversized_canonical_status_keeps_the_legacy_copy_and_partial_coverage(self):
        canonical = self.campaign / '현황.md'; canonical.write_text('# Large\n' + 'x' * 70000)
        old = self.legacy()
        self.assertEqual((self.status()['status'], self.status()['path']), ('unknown', None))
        self.assertEqual(old.read_text(), PAGE)
        result = entrance.scan(self.root)
        row = next(r for r in result['recent'] if r.get('kind') == 'campaign-document')
        self.assertEqual((row['status'], row['coverage']), ('unknown', 'partial'))
        self.assertEqual(row['timestamp_source'], 'file-mtime')

    def test_fleet_uses_the_same_conflict_rule_and_rechecks_content_with_cached_names(self):
        sys.path.insert(0, str(HERE.parent))
        from tools.fleet.collectors import project_status
        project_status.clear_cache(); self.addCleanup(project_status.clear_cache)
        a, b = self.campaign / '현황.md', self.campaign / 'STATUS.md'
        a.write_text(PAGE); b.write_text(PAGE)
        self.legacy()
        self.assertEqual(project_status.find_documents(str(self.root)), [str(a)])
        b.write_text('# Conflicting judgment\n')  # Parent directory mtime need not change.
        self.assertEqual(project_status.find_documents(str(self.root)), [])
        self.assertEqual(self.status()['status'], 'ambiguous')

    def test_conflicts_remain_visible_in_korean_and_english_entrances(self):
        (self.campaign / '현황.md').write_text(PAGE)
        (self.campaign / 'STATUS.md').write_text('# Conflicting\n')
        result = entrance.scan(self.root)
        for language, label in [('ko', '현황 문서 상충'), ('en', 'conflicting status pages')]:
            with self.subTest(language=language):
                t = entrance.ui_text.translator(language)
                self.assertIn(label, entrance.render_recent_html(result, t))
                self.assertIn(label, '\n'.join(entrance._markdown_recent(result, t)))

    def test_alias_creation_detects_root_replacement_without_touching_the_replacement(self):
        import artifact_root_alias
        project = self.project / 'swapped-root'; root = project / '.claude_reports'
        root.mkdir(parents=True)
        producer.activate(root, repository_id='repo_'+'a'*32, artifact_root_id='root_'+'b'*32)
        (project / '.agent_reports').unlink()
        identity = lifecycle.read_root_identity(root)
        symlink = os.symlink
        def replace_before_link(*args, **kwargs):
            root.rename(project / 'retained-original')
            root.mkdir(); (root / 'sentinel').write_text('keep replacement')
            return symlink(*args, **kwargs)
        with mock.patch.object(artifact_root_alias.os, 'symlink', side_effect=replace_before_link):
            result = artifact_root_alias.ensure_legacy_alias(root, identity)
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse((project / '.agent_reports').exists())
        self.assertEqual((root / 'sentinel').read_text(), 'keep replacement')
        self.assertEqual(lifecycle.read_root_identity(project / 'retained-original'), identity)

    def test_existing_unicode_locator_and_hash_percent_links_are_preserved(self):
        names = [unicodedata.normalize('NFD', '현황.md'), 'HANDOFF #1 100%.md']
        for name in names:
            (self.campaign / name).write_text('# Document\n')
        result = entrance.scan(self.root)
        rows = [r for r in result['recent'] if r.get('kind') == 'campaign-document']
        self.assertEqual({Path(r['doc_path']).name for r in rows}, set(names))
        t = entrance.ui_text.translator('en')
        markdown = '\n'.join(entrance._markdown_recent(result, t))
        html = entrance.render_recent_html(result, t)
        for row in rows:
            self.assertIn(quote(row['doc_path'], safe='/.'), markdown)
            self.assertIn(quote(row['doc_path'], safe='/.'), html)

    def test_begin_does_not_resolve_a_replaced_root_for_its_optional_alias(self):
        project = self.project / 'begin-swap'; root = project / '.claude_reports'
        root.mkdir(parents=True)
        outside = self.project / 'other-project/.claude_reports'; outside.mkdir(parents=True)
        (outside / 'sentinel').write_text('other project')
        def finish_then_swap(*args, **kwargs):
            root.rename(project / 'retained-original')
            root.symlink_to(outside, target_is_directory=True)
            return {'status': 'legacy-compat', 'layout': 'legacy',
                    'env': {'AGENT_ARTIFACT_ROOT': str(root)}}
        with mock.patch.object(producer, '_begin_cycle_record', side_effect=finish_then_swap):
            result = producer.begin(root, _reconcile=False, _deliver_history=False)
        self.assertEqual(result['root_alias']['status'], 'unknown')
        self.assertFalse((outside.parent / '.agent_reports').exists())
        self.assertEqual((outside / 'sentinel').read_text(), 'other project')


if __name__ == '__main__':unittest.main()
