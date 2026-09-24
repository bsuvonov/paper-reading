import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import pymupdf

import codex_papers
import codex_sessions as codex
import web_app as web


class CodexScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for year in (2024, 2025):
            directory = self.root / 'papers' / str(year)
            directory.mkdir(parents=True)
            path = directory / 'example.pdf'
            with pymupdf.open() as document:
                page = document.new_page()
                page.insert_text((40, 50), f'Scoped research paper {year}')
                document.save(path)
            records = [dict(title=f'Paper {year}', year=year, page_url=f'https://example.org/{year}',
                            status='downloaded', path=path.name),
                       dict(title=f'Missing {year}', year=year, page_url=f'https://example.org/missing-{year}', status='not_selected')]
            (directory / 'manifest.json').write_text(json.dumps(dict(year=year, papers=records)))
        self.app = web.create_app(self.root)
        self.client = self.app.test_client()
        self.manager = self.app.config['CODEX']
        self.addCleanup(self.manager.close)
        self.store = self.manager.store
        self.headers = {'X-Library-Token': self.client.get('/api/library').get_json()['token']}
        self.scope = dict(conferences=[dict(id='osdi', years=[2025])])

    def test_scope_normalization_merges_duplicates_and_covered_papers(self):
        paper = next(p for p in web.Library(self.root).catalog() if p['year'] == '2025')
        combined = dict(conferences=[dict(id='osdi', years=[2025, 2024]), dict(id='osdi', years=[2025])],
                        papers=[dict(project_id='osdi', paper_id=paper['id'])])
        normalized = self.store.normalize(combined)
        self.assertEqual(normalized, dict(conferences=[dict(id='osdi', years=[2024, 2025])], projects=[], papers=[]))
        self.assertEqual(self.store.key(normalized), self.store.key(self.store.normalize(dict(conferences=[dict(id='osdi', years=[2024, 2025])]))))
        combined['conferences'].append(dict(id='osdi', years=None))
        self.assertIsNone(self.store.normalize(combined)['conferences'][0]['years'])

    def test_scope_supports_multiple_conferences_projects_and_individual_papers(self):
        project = self.store.projects.create('My reading')
        paper = next(p for p in web.Library(self.root).catalog() if p['year'] == '2024')
        scope = dict(conferences=[dict(id='osdi', years=[2025]), dict(id='fast', years=[2025])],
                     projects=[project['id']], papers=[dict(project_id='osdi', paper_id=paper['id'])])
        preview = self.store.preview(scope)
        self.assertEqual(preview['total'], 3)
        self.assertIn(dict(project_id='fast', year=2025), preview['missing_catalogs'])
        self.assertIn('My reading', preview['label'])

    def test_invalid_scopes_are_rejected(self):
        values = [None, {}, [], dict(conferences='osdi'), dict(conferences=[dict(id='osdi', years=[])]),
                  dict(conferences=[dict(id='osdi', years=['2025'])]), dict(projects=[{}]),
                  dict(papers=[dict(project_id={}, paper_id='x')]), dict(projects=['../../escape'])]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.normalize(value)

    def test_sessions_are_isolated_listed_by_exact_scope_and_newest_first(self):
        first = self.store.create(self.scope, 'First')
        second = self.store.create(self.scope, 'Second')
        other = self.store.create(dict(conferences=[dict(id='osdi', years=[2024])]), 'Different scope')
        records = self.store.sessions(self.store.normalize(self.scope))
        self.assertEqual([r['id'] for r in records], [second['id'], first['id']])
        self.assertNotEqual(first['workspace'], second['workspace'])
        self.assertNotEqual(first['scope_id'], other['scope_id'])
        reopened = codex.ScopeStore(self.root, web.Library)
        self.assertEqual(reopened.get(first['id'])['title'], 'First')

    def test_workspace_links_only_selected_papers_and_extracts_real_text(self):
        paper = next(p for p in web.Library(self.root).catalog() if p['year'] == '2025' and p['downloaded'])
        record = self.store.create(dict(papers=[dict(project_id='osdi', paper_id=paper['id'])]))
        workspace = Path(record['workspace'])
        data = json.loads((workspace / 'scope.json').read_text())
        self.assertEqual(len(data['papers']), 1)
        self.assertTrue((workspace / data['papers'][0]['path']).is_symlink())
        self.assertEqual(len(list((workspace / 'papers').rglob('*.pdf'))), 1)
        self.assertTrue((workspace / 'AGENTS.md').is_file())
        self.assertTrue((workspace / 'tools/papers.py').is_file())
        output = io.StringIO()
        with redirect_stdout(output):
            codex_papers.text(workspace, data['papers'][0])
        self.assertIn('Scoped research paper 2025', output.getvalue())
        self.assertIn('Page 1', output.getvalue())
        self.assertIn(data['papers'][0]['key'], (workspace / 'PAPERS.md').read_text())

    def test_helper_fetch_is_session_local_and_survives_context_refresh(self):
        record = self.store.create(self.scope)
        workspace = Path(record['workspace'])
        data = json.loads((workspace / 'scope.json').read_text())
        paper = next(p for p in data['papers'] if not p['downloaded'])
        original = (self.root / 'papers/2025/manifest.json').read_bytes()
        def download(client, item, directory, previous):
            path = directory / 'fetched.pdf'
            path.write_bytes((self.root / 'papers/2025/example.pdf').read_bytes())
            return dict(status='downloaded', path=path.name)
        with patch.object(web.downloader, 'retrieve_paper', side_effect=download):
            codex_papers.fetch(workspace, data, paper, None)
        refreshed = self.store.materialize(record)
        fetched = next(p for p in refreshed['papers'] if p['key'] == paper['key'])
        self.assertTrue(fetched['downloaded'])
        self.assertTrue((workspace / fetched['path']).is_file())
        self.assertEqual((self.root / 'papers/2025/manifest.json').read_bytes(), original)

    def test_helper_discovery_keeps_failures_for_retry(self):
        record = self.store.create(dict(conferences=[dict(id='fast', years=[2024, 2025])]))
        workspace = Path(record['workspace'])
        data = json.loads((workspace / 'scope.json').read_text())
        data['missing_catalogs'] = [dict(project_id='fast', year=2024), dict(project_id='fast', year=2025)]
        paper = web.downloader.Paper(2025, 'Discovered', 'https://example.org/discovered')
        with patch.object(codex_papers, 'discover_conference', side_effect=[ValueError('Unavailable'), ('https://example.org', [paper])]):
            errors = codex_papers.discover(workspace, data, None)
        self.assertEqual(len(errors), 1)
        self.assertEqual(data['missing_catalogs'], [dict(project_id='fast', year=2024)])
        self.assertTrue(any(p['title'] == 'Discovered' for p in data['papers']))
        refreshed = self.store.materialize(record)
        self.assertEqual(refreshed['missing_catalogs'], [dict(project_id='fast', year=2024)])
        self.assertTrue(any(p['title'] == 'Discovered' for p in refreshed['papers']))

    def test_api_protects_terminal_reads_and_writes_and_validates_bodies(self):
        self.assertEqual(self.client.get('/api/codex/options').status_code, 403)
        self.assertEqual(self.client.post('/api/codex/sessions', json={'scope': self.scope}).status_code, 403)
        self.assertEqual(self.client.post('/api/codex/sessions', json=[], headers=self.headers).status_code, 400)
        result = self.client.post('/api/codex/sessions', json={'scope': self.scope, 'title': 'Browser session'}, headers=self.headers)
        self.assertEqual(result.status_code, 201)
        record = result.get_json()['session']
        self.assertEqual(self.client.get('/api/codex/sessions/' + record['id']).status_code, 403)
        listing = self.client.post('/api/codex/sessions/query', json={'scope': self.scope}, headers=self.headers).get_json()
        self.assertEqual(listing['sessions'][0]['id'], record['id'])
        self.assertEqual(self.client.get('/api/codex/sessions/not-a-session', headers=self.headers).status_code, 400)
        response = self.client.patch('/api/codex/sessions/' + record['id'], json={'title': 'Renamed'}, headers=self.headers)
        self.assertEqual(response.get_json()['session']['title'], 'Renamed')

    def test_start_and_restart_resume_same_native_thread_without_duplicate_processes(self):
        record = self.store.create(self.scope, model='example-model')
        terminal = Mock(closed=False)
        with patch.object(self.manager.rpc, 'call') as rpc, \
                patch.object(codex, 'Terminal', return_value=terminal) as factory:
            self.manager.start(record['id'])
            self.manager.start(record['id'], 90, 30)
            self.assertEqual(factory.call_count, 1)
            rpc.assert_not_called()
            self.assertIn('example-model', factory.call_args.args[0])
            self.assertIn('--no-daemon', factory.call_args.args[0])
        with patch.object(self.manager.rpc, 'call', return_value={'data': [dict(id='native-thread', cwd=record['workspace'])]}):
            self.manager.capture_thread(record['id'], force=True)
        self.assertEqual(self.store.get(record['id'])['thread_id'], 'native-thread')
        reopened = codex.CodexSessions(self.root, web.Library)
        self.addCleanup(reopened.close)
        with patch.object(reopened.rpc, 'call') as rpc, patch.object(codex, 'Terminal', return_value=terminal) as factory:
            reopened.start(record['id'])
            rpc.assert_not_called()
            self.assertIn('native-thread', factory.call_args.args[0])


class NativeTerminalTests(unittest.TestCase):
    def test_pty_input_utf8_replay_resize_and_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            terminal = codex.Terminal([sys.executable, '-u', '-c',
                "import sys; print('READY',flush=True); value=input(); print('REPLY:'+value,flush=True)"], path, path, 80, 24)
            try:
                deadline = time.monotonic() + 5
                while b'READY' not in base64.b64decode(terminal.read(0, .2)['data']):
                    self.assertLess(time.monotonic(), deadline)
                terminal.resize(100, 35)
                terminal.write('Hello 世界\r')
                terminal.reader.join(timeout=5)
                output = terminal.read(0)
                self.assertFalse(output['running'])
                self.assertEqual(output['exit_code'], 0)
                self.assertIn('REPLY:Hello 世界', base64.b64decode(output['data']).decode())
                self.assertEqual(terminal.read(output['cursor'])['data'], '')
                self.assertIn(b'REPLY:', (path / 'terminal.log').read_bytes())
            finally:
                terminal.stop()


if __name__ == '__main__':
    unittest.main()
