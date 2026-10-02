import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import pymupdf

import conference_search as search_module
import web_app as web


class ConferenceSearchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        registry = patch.object(search_module, 'CONFERENCES', {'osdi': ('OSDI', [2024, 2025]), 'fast': ('FAST', [2024, 2025])})
        registry.start()
        self.addCleanup(registry.stop)
        self.app = web.create_app(self.root)
        self.client = self.app.test_client()
        self.index = self.app.config['CONFERENCE_SEARCH']
        self.headers = {'X-Library-Token': self.client.get('/api/library').get_json()['token']}

    def seed(self, ident, year, title='Memory systems', downloaded=False):
        library = web.Library(self.root, ident)
        record = dict(year=year, title=title, category='Caching', page_url=f'https://example.org/{ident}/{year}', status='not_selected')
        folder = library.root / '.catalog'
        if downloaded:
            folder = library.papers / str(year)
            folder.mkdir(parents=True, exist_ok=True)
            with pymupdf.open() as pdf:
                pdf.new_page()
                pdf.save(folder / 'paper.pdf')
            record.update(path='paper.pdf', status='downloaded')
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ('manifest.json' if downloaded else str(year) + '.json')
        path.write_text(json.dumps(dict(year=year, discovered=1, papers=[record])))
        self.index.expires = 0
        return library, path

    def test_search_spans_conferences_years_and_missing_pdfs_but_not_local_projects(self):
        self.seed('osdi', 2024, downloaded=True)
        self.seed('fast', 2025)
        local = web.ProjectStore(self.root).create('Private local project')
        self.seed(local['id'], 2025)
        data = self.client.get('/api/search?q=memory').get_json()
        self.assertEqual(data['total'], 2)
        self.assertEqual([(p['project_id'], p['year']) for p in data['papers']], [('fast', '2025'), ('osdi', '2024')])
        self.assertEqual(data['index']['indexed'], 2)
        self.assertEqual(data['index']['total'], 4)
        self.assertFalse(data['index']['running'])
        for row in data['papers']:
            self.assertFalse(any(key.startswith('_') for key in row))
            detail = self.client.get('/api/papers/' + row['id'] + '?project=' + row['project_id']).get_json()
            self.assertEqual(detail['id'], row['id'])
            self.assertEqual(detail['downloaded'], row['project_id'] == 'osdi')
        self.assertEqual(self.client.get('/api/search?q=memory&availability=downloaded').get_json()['total'], 1)
        self.assertEqual(self.client.get('/api/search?q=memory&availability=missing').get_json()['papers'][0]['project_id'], 'fast')
        self.assertEqual(self.client.get('/api/search?q=memory&availability=outlines').get_json()['total'], 0)
        self.assertEqual(self.client.get('/api/search?q=FAST%202025%20Caching').get_json()['total'], 1)

    def test_limits_and_validation(self):
        self.seed('osdi', 2024)
        self.seed('fast', 2025)
        result = self.client.get('/api/search?q=MEMORY&limit=1').get_json()
        self.assertEqual(result['total'], 2)
        self.assertEqual(len(result['papers']), 1)
        self.assertEqual(self.client.get('/api/search?q=missing').get_json()['total'], 0)
        for suffix in ['q=' + 'x' * 301, 'limit=no', 'limit=0', 'limit=5001', 'availability=bad']:
            self.assertEqual(self.client.get('/api/search?' + suffix).status_code, 400)
        self.assertEqual(self.client.post('/api/search/index', json={}).status_code, 403)
        for payload in [[], None, {'retry': 'true'}]:
            self.assertEqual(self.client.post('/api/search/index', json=payload, headers=self.headers).status_code, 400)

    def test_missing_lists_load_once_without_downloading_or_mutating_saved_papers(self):
        _, manifest = self.seed('osdi', 2024, downloaded=True)
        before = manifest.read_bytes()
        gate = threading.Event()
        started = threading.Event()
        calls = []
        def discover(client, ident, year):
            calls.append((ident, year))
            started.set()
            gate.wait(5)
            return 'https://example.org/program', [web.downloader.Paper(year, 'New memory paper', f'https://example.org/{ident}/{year}')]
        with patch.object(search_module, 'discover_conference', side_effect=discover), patch.object(web.downloader, 'retrieve_paper') as download:
            try:
                response = self.client.post('/api/search/index', json={}, headers=self.headers)
                self.assertEqual(response.status_code, 202)
                self.assertTrue(started.wait(5))
                first_thread = self.index.thread
                self.index.start()
                self.assertIs(self.index.thread, first_thread)
                self.assertEqual(self.index.search('memory')['total'], 1)
            finally:
                gate.set()
                self.index.thread.join(5)
            self.assertFalse(self.index.running)
            self.assertEqual(set(calls), {('osdi', 2025), ('fast', 2024), ('fast', 2025)})
            self.assertEqual(len(calls), 3)
            self.assertEqual(self.index.search('memory')['total'], 4)
            self.assertEqual(self.index.search('memory')['index']['indexed'], 4)
            self.assertEqual(manifest.read_bytes(), before)
            self.assertEqual(len(list(self.root.rglob('*.pdf'))), 1)
            download.assert_not_called()

    def test_failed_edition_preserves_results_and_can_retry(self):
        for ident, year in [('osdi', 2024), ('osdi', 2025), ('fast', 2024)]:
            self.seed(ident, year)
        with patch.object(search_module, 'discover_conference', side_effect=RuntimeError('Offline')) as discover:
            self.index.start()
            self.index.thread.join(5)
            data = self.index.search('memory')
            self.assertEqual(data['total'], 3)
            self.assertEqual(data['index']['errors'], [dict(conference='fast', year=2025, message='Offline')])
            self.index.start()
            self.assertEqual(discover.call_count, 1)
        paper = web.downloader.Paper(2025, 'Memory retry', 'https://example.org/retry')
        with patch.object(search_module, 'discover_conference', return_value=('https://example.org/program', [paper])) as discover:
            self.index.start(retry=True)
            self.index.thread.join(5)
            self.assertEqual(discover.call_count, 1)
        data = self.index.search('memory')
        self.assertEqual(data['total'], 4)
        self.assertEqual(data['index']['errors'], [])
        self.assertFalse(list(self.root.rglob('*.part')))
