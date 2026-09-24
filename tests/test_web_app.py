import json
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from bs4 import BeautifulSoup

import pymupdf

import web_app as web
import projects


class ControlledProcess:
    """A worker that finishes only when the test releases it."""
    def __init__(self):
        self.returncode = None
        self.finished = threading.Event()

    def poll(self):
        return self.returncode

    def wait(self):
        self.finished.wait()
        return self.returncode

    def finish(self, code=0):
        self.returncode = code
        self.finished.set()


class DownloadQueueTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.jobs = web.Jobs(self.root)
        self.launched = []
        self.changed = threading.Condition()
        self.fail_launch = None
        patcher = patch.object(web.subprocess, 'Popen', side_effect=self.launch)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.finish_all)

    def launch(self, args, **kwargs):
        payload = json.loads(Path(args[-1]).read_text())
        if payload['paper_id'] == self.fail_launch:
            raise OSError('Worker could not be started')
        process = ControlledProcess()
        with self.changed:
            self.launched.append((payload, process))
            self.changed.notify_all()
        return process

    def finish_all(self):
        with self.jobs.lock:
            self.jobs.pending.clear()
            for _, process in self.launched:
                process.finish()
            self.jobs.snapshot()

    def wait_for_workers(self, count):
        with self.changed:
            self.assertTrue(self.changed.wait_for(lambda: len(self.launched) >= count, timeout=5))

    def enqueue(self, ident, project='osdi', action='download'):
        return self.jobs.start(dict(action=action, paper_id=ident, project_id=project, title=ident))

    def test_fifo_deduplication_and_failure_continue_without_browser_polling(self):
        first = self.enqueue('one')
        self.enqueue('two')
        status = self.enqueue('three')
        self.assertEqual(status['id'], first['id'])
        self.assertEqual([(j['paper_id'], j['position']) for j in status['queue']], [('two', 1), ('three', 2)])
        for ident in ('one', 'two'):
            self.assertEqual(len(self.enqueue(ident)['queue']), 2)
        self.assertEqual(len(self.launched), 1)

        self.launched[0][1].finish()
        self.wait_for_workers(2)  # No snapshot/API request is needed to start the next worker.
        status = self.jobs.snapshot()
        self.assertEqual(status['paper_id'], 'two')
        self.assertEqual(status['completed'][0]['paper_id'], 'one')
        self.assertEqual(status['queue'][0]['position'], 1)
        self.launched[1][1].finish(1)
        self.wait_for_workers(3)
        self.launched[2][1].finish()
        status = self.jobs.snapshot()
        self.assertEqual(status['status'], 'done')
        self.assertEqual(status['queue'], [])
        self.assertEqual([(j['paper_id'], j['status']) for j in status['completed']],
                         [('one', 'done'), ('two', 'failed'), ('three', 'done')])
        self.assertEqual(len(list((self.root / '.web-jobs').glob('*.json'))), 3)

    def test_downloads_queue_behind_other_tasks_and_across_projects(self):
        self.enqueue('outline', action='outline')
        self.enqueue('same', 'osdi')
        status = self.enqueue('same', 'fast')
        self.assertEqual([j['project_id'] for j in status['queue']], ['osdi', 'fast'])
        with self.assertRaises(RuntimeError):
            self.enqueue('another-outline', action='outline')
        self.launched[0][1].finish()
        self.wait_for_workers(2)
        self.assertEqual(self.launched[1][0]['project_id'], 'osdi')

    def test_unavailable_pdf_does_not_stop_queue_or_count_as_success(self):
        self.enqueue('unavailable')
        self.enqueue('available')
        self.launched[0][1].finish(3)
        self.wait_for_workers(2)
        status = self.jobs.snapshot()
        self.assertEqual(status['completed'][0]['status'], 'unavailable')
        self.assertEqual(status['paper_id'], 'available')
        self.launched[1][1].finish()
        self.assertEqual(self.jobs.snapshot()['status'], 'done')

    def test_worker_launch_failure_does_not_strand_remaining_downloads(self):
        self.fail_launch = 'broken'
        self.enqueue('one')
        self.enqueue('broken')
        self.enqueue('three')
        self.launched[0][1].finish()
        self.wait_for_workers(2)
        status = self.jobs.snapshot()
        self.assertEqual(status['paper_id'], 'three')
        self.assertEqual(status['completed'][1]['status'], 'failed')
        self.assertIn('Worker could not be started', status['completed'][1]['log'])

    def test_api_accepts_multiple_downloads_and_restores_queue_on_refresh(self):
        directory = self.root / '.catalog'
        directory.mkdir()
        (directory / '2025.json').write_text(json.dumps({'year': 2025, 'papers': [
            dict(title=name, year=2025, page_url=f'https://example.org/{name}', status='not_selected')
            for name in ('one', 'two')]}))
        app = web.create_app(self.root)
        self.jobs = app.config['JOBS']
        client = app.test_client()
        catalog = client.get('/api/library').get_json()
        headers = {'X-Library-Token': catalog['token']}
        for paper in catalog['papers']:
            response = client.post('/api/jobs', json=dict(action='download', paper_id=paper['id']), headers=headers)
            self.assertEqual(response.status_code, 202)
        status = client.get('/api/library').get_json()['job']
        self.assertEqual(status['title'], 'one')
        self.assertEqual(status['queue'][0]['title'], 'two')
        self.assertEqual(status['queue'][0]['status'], 'queued')
        duplicate = client.post('/api/jobs', json=dict(action='download', paper_id=paper['id']), headers=headers)
        self.assertEqual(len(duplicate.get_json()['job']['queue']), 1)
        self.launched[0][1].finish()
        self.wait_for_workers(2)
        status = client.get('/api/jobs').get_json()['job']
        self.assertEqual(status['title'], 'two')
        self.assertEqual(status['completed'][0]['title'], 'one')


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        year = self.root / 'papers' / '2025'
        year.mkdir(parents=True)
        self.pdf = year / 'A paper.pdf'
        with pymupdf.open() as doc:
            page = doc.new_page()
            for i, title in enumerate(['1 Introduction', '2 Design', '3 Evaluation', '4 Conclusion']):
                page.insert_text((50, 60 + i * 130), title, fontsize=14, fontname='hebo')
                page.insert_text((50, 85 + i * 130), 'This is ordinary body text that explains the paper.', fontsize=10)
            doc.set_toc([[1, title, 1] for title in ['1 Introduction', '2 Design', '3 Evaluation', '4 Conclusion']])
            doc.save(self.pdf)
        self.manifest = year / 'manifest.json'
        self.manifest.write_text(json.dumps({'year': 2025, 'papers': [
            {'year': 2025, 'title': 'A paper', 'page_url': 'https://www.usenix.org/a', 'status': 'downloaded', 'path': self.pdf.name},
            {'year': 2025, 'title': 'Missing paper', 'page_url': 'https://www.usenix.org/b', 'status': 'failed', 'error': 'Network unavailable'},
        ]}))
        self.app = web.create_app(self.root)
        self.client = self.app.test_client()

    def catalog(self):
        return self.client.get('/api/library').get_json()

    def test_catalog_includes_missing_papers_and_pdf_supports_ranges(self):
        catalog = self.catalog()
        self.assertEqual(len(catalog['papers']), 2)
        self.assertEqual([p['downloaded'] for p in catalog['papers']], [True, False])
        detail = self.client.get('/api/papers/' + catalog['papers'][0]['id']).get_json()
        self.assertEqual(detail['viewer_type'], 'pdf')
        with self.client.get(detail['viewer_url'], headers={'Range': 'bytes=0-3'}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, b'%PDF')
        self.assertIsNone(self.client.get('/api/papers/' + catalog['papers'][1]['id']).get_json()['viewer_url'])

    def test_file_routes_reject_traversal_and_symlinks(self):
        (self.root / 'secret.txt').write_text('secret')
        (self.root / 'papers' / 'escape.txt').symlink_to(self.root / 'secret.txt')
        for url in ['/files/papers/../secret.txt', '/files/papers/%2e%2e/secret.txt', '/files/papers/escape.txt', '/files/other/secret.txt']:
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_html_is_sandboxed(self):
        (self.root / 'papers' / 'page.html').write_text('<script>alert(1)</script>')
        with self.client.get('/files/papers/page.html') as response:
            self.assertIn('sandbox', response.headers['Content-Security-Policy'])

    def test_jobs_require_token_and_validate_input(self):
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'catalog'}).status_code, 403)
        headers = {'X-Library-Token': self.catalog()['token']}
        for payload in [{'action': 'bad'}, {'action': 'catalog', 'year': 1995}, {'action': 'catalog', 'year': '2025'}]:
            self.assertEqual(self.client.post('/api/jobs', json=payload, headers=headers).status_code, 400)
        self.assertEqual(self.client.get('/api/library', headers={'Host': 'untrusted.example'}).status_code, 400)

    def test_real_outline_background_job_and_duplicate_rejection(self):
        data = self.catalog()
        ident = data['papers'][0]['id']
        response = self.client.post('/api/jobs', json={'action': 'outline', 'paper_id': ident},
                                    headers={'X-Library-Token': data['token']})
        self.assertEqual(response.status_code, 202)
        jobs = self.app.config['JOBS']
        self.addCleanup(lambda: jobs.process.poll() is None and jobs.process.kill())
        with self.assertRaises(RuntimeError):
            jobs.start({'action': 'outline', 'paper_id': ident})
        jobs.process.wait(timeout=20)
        job = self.client.get('/api/jobs').get_json()['job']
        self.assertEqual(job['status'], 'done', job.get('log'))
        detail = self.client.get('/api/papers/' + ident).get_json()
        self.assertEqual(len(detail['sections']), 4)
        self.assertTrue((self.root / 'outlines' / 'overview.md').is_file())
        self.assertTrue(detail['outline_url'].endswith('.md'))

    def test_download_updates_manifest_and_preserves_other_papers(self):
        library = self.app.config['LIBRARY']
        row = next(r for r in library.catalog() if not r['downloaded'])
        result = {**row['_record'], 'status': 'downloaded', 'path': 'downloaded.pdf'}
        with patch.object(web.downloader, 'retrieve_paper', return_value=result):
            web.download_one(library, row, None)
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest['papers'][0]['path'], 'A paper.pdf')
        self.assertEqual(manifest['papers'][1]['status'], 'downloaded')

    def test_catalog_refresh_preserves_completed_downloads(self):
        library = self.app.config['LIBRARY']
        papers = [web.downloader.Paper(2025, 'A paper', 'https://www.usenix.org/a'),
                  web.downloader.Paper(2025, 'New paper', 'https://www.usenix.org/new')]
        with patch.object(web.downloader, 'discover_papers', return_value=('https://www.usenix.org/program', papers)):
            web.sync_year(library, 2025, None)
        records = json.loads(self.manifest.read_text())['papers']
        self.assertEqual(records[0]['status'], 'downloaded')
        self.assertEqual(records[0]['path'], 'A paper.pdf')
        self.assertEqual(records[1]['status'], 'not_selected')

    def create_local(self, name='Reading project'):
        headers = {'X-Library-Token': self.catalog()['token']}
        response = self.client.post('/api/projects', json={'name': name}, headers=headers)
        self.assertEqual(response.status_code, 201, response.data)
        return response.get_json()['project']['id'], headers

    def upload_local(self, ident, headers, data=None):
        return self.client.post('/api/projects/' + ident + '/upload',
                                data={'file': (io.BytesIO(data if data is not None else self.pdf.read_bytes()), 'Imported.pdf')},
                                headers=headers)

    def test_local_project_create_rename_archive_preserves_files(self):
        ident, headers = self.create_local()
        self.assertEqual(self.upload_local(ident, headers).status_code, 201)
        response = self.client.patch('/api/projects/' + ident, json={'name': 'New name'}, headers=headers)
        self.assertEqual(response.get_json()['project']['name'], 'New name')
        self.assertEqual(web.ProjectStore(self.root).get(ident)['name'], 'New name')
        self.assertEqual(self.client.delete('/api/projects/' + ident, headers=headers).status_code, 200)
        self.assertNotIn(ident, [p['id'] for p in web.ProjectStore(self.root).list()])
        self.assertEqual(len(list((self.root / 'projects' / '.trash').rglob('*.pdf'))), 1)
        self.assertEqual(self.client.delete('/api/projects/osdi', headers=headers).status_code, 400)

    def test_project_papers_are_isolated_persistent_and_deduplicated(self):
        first, headers = self.create_local('First')
        second, _ = self.create_local('Second')
        self.assertEqual(self.upload_local(first, headers).status_code, 201)
        self.assertEqual(self.upload_local(first, headers).status_code, 201)
        a = self.client.get('/api/library?project=' + first).get_json()
        b = self.client.get('/api/library?project=' + second).get_json()
        self.assertEqual(len(a['papers']), 1)
        self.assertEqual(len(b['papers']), 0)
        paper_id = a['papers'][0]['id']
        self.assertEqual(self.client.get('/api/papers/' + paper_id + '?project=' + second).status_code, 404)
        detail = self.client.get('/api/papers/' + paper_id + '?project=' + first).get_json()
        self.assertIn('/project-files/' + first + '/', detail['viewer_url'])
        with self.client.get(detail['viewer_url']) as response:
            self.assertEqual(response.data, self.pdf.read_bytes())
        self.assertEqual(len(web.Library(self.root, first).catalog()), 1)
        self.assertEqual(len(self.catalog()['papers']), 2)

    def test_imports_are_local_only_and_reject_invalid_content(self):
        ident, headers = self.create_local()
        self.assertEqual(self.upload_local('osdi', headers).status_code, 400)
        self.assertEqual(self.upload_local(ident, headers, b'<html>Not a PDF</html>').status_code, 400)
        for payload in [dict(action='import_url', project_id='osdi', url='https://example.org/a.pdf'),
                        dict(action='import_url', project_id=ident, url='file:///etc/passwd'),
                        dict(action='catalog', project_id=ident)]:
            self.assertEqual(self.client.post('/api/jobs', json=payload, headers=headers).status_code, 400)
        self.assertEqual(web.Library(self.root, ident).catalog(), [])

    def test_local_outline_copy_and_removal_keep_conference_original(self):
        ident, headers = self.create_local()
        source = self.catalog()['papers'][0]
        web.run_worker(self.root, dict(action='copy_paper', project_id=ident, source_project='osdi', paper_id=source['id']))
        library = web.Library(self.root, ident)
        row = library.catalog()[0]
        web.run_worker(self.root, dict(action='outline', project_id=ident, paper_id=row['id']))
        detail = self.client.get('/api/papers/' + row['id'] + '?project=' + ident).get_json()
        self.assertEqual(len(detail['sections']), 4)
        self.assertEqual(self.client.delete('/api/papers/' + row['id'] + '?project=' + ident, headers=headers).status_code, 200)
        self.assertEqual(library.catalog(), [])
        self.assertTrue(self.pdf.is_file())
        self.assertTrue(list((library.root / '.trash').rglob('*.pdf')))

    def test_pdfs_copied_into_project_folder_are_listed(self):
        ident, _ = self.create_local()
        library = web.Library(self.root, ident)
        path = library.papers / 'local' / 'My existing paper.pdf'
        path.write_bytes(self.pdf.read_bytes())
        rows = library.catalog()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'My existing paper')

    def test_url_import_resolves_metadata_and_keeps_project_isolation(self):
        ident, _ = self.create_local()
        client = Mock()
        client.get.side_effect = [
            Mock(content=b'<meta name="citation_title" content="A linked paper"><meta name="citation_pdf_url" content="/files/paper.pdf">', url='https://example.org/article'),
            Mock(content=self.pdf.read_bytes(), url='https://example.org/files/paper.pdf'),
        ]
        with patch.object(web.downloader, 'Client', return_value=client):
            web.run_worker(self.root, dict(action='import_url', project_id=ident, url='https://example.org/article'))
        rows = web.Library(self.root, ident).catalog()
        self.assertEqual(rows[0]['title'], 'A linked paper')
        self.assertEqual(rows[0]['source_url'], 'https://example.org/article')
        self.assertEqual(client.get.call_args.args[0], 'https://example.org/files/paper.pdf')
        self.assertEqual(len(web.Library(self.root).catalog()), 2)

    def test_conference_discovery_uses_conference_path_and_excludes_keynotes(self):
        client = Mock()
        client.page.return_value = ('https://www.usenix.org/conference/fast25/technical-sessions', BeautifulSoup('''
          <div class="node-paper"><h2><a href="/conference/fast25/presentation/author">A FAST paper</a></h2></div>
          <div class="node-paper"><h2><a href="/conference/fast25/presentation/keynote">A keynote</a></h2></div>
          <div class="node-paper"><h2><a href="/conference/osdi25/presentation/author">Wrong venue</a></h2></div>
        ''', 'html.parser'))
        _, found = projects.discover_conference(client, 'fast', 2025)
        self.assertEqual([p.title for p in found], ['A FAST paper'])
        web.sync_year(web.Library(self.root, 'fast'), 2025, client)
        rows = web.Library(self.root, 'fast').catalog()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]['id'], self.catalog()['papers'][0]['id'])

    def test_mutations_wait_for_running_job(self):
        ident, headers = self.create_local()
        self.app.config['JOBS'].process = Mock(poll=Mock(return_value=None))
        self.assertEqual(self.client.delete('/api/projects/' + ident, headers=headers).status_code, 409)
        self.assertEqual(self.upload_local(ident, headers).status_code, 409)

    def test_year_status_tracks_complete_partial_and_missing_files(self):
        status = self.catalog()['year_status']['2025']
        self.assertEqual(status, dict(status='partial', downloaded=1, total=2, catalogued=True, categories_loaded=False))
        manifest = json.loads(self.manifest.read_text())
        manifest['papers'] = manifest['papers'][:1]
        self.manifest.write_text(json.dumps(manifest))
        self.assertEqual(self.catalog()['year_status']['2025']['status'], 'downloaded')
        manifest['discovered'] = 2
        self.manifest.write_text(json.dumps(manifest))
        self.assertEqual(self.catalog()['year_status']['2025']['status'], 'partial')
        manifest['discovered'] = 1
        manifest['papers'][0]['files'] = [{'path': 'missing-attachment.png'}]
        self.manifest.write_text(json.dumps(manifest))
        status = self.catalog()['year_status']['2025']
        self.assertEqual(status['status'], 'not_downloaded')
        self.assertEqual(status['downloaded'], 0)

    def test_download_year_requires_explicit_year(self):
        headers = {'X-Library-Token': self.catalog()['token']}
        for payload in [dict(action='download_year'), dict(action='download_year', year=None)]:
            response = self.client.post('/api/jobs', json=payload, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertIn('Select a conference year', response.get_json()['error'])
        with patch.object(self.app.config['JOBS'], 'start', return_value={'status': 'running'}) as start:
            response = self.client.post('/api/jobs', json=dict(action='download_year', year=2025), headers=headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(start.call_args.args[0]['year'], 2025)

    def test_download_worker_only_processes_selected_year(self):
        rows = [dict(year='2024', downloaded=False), dict(year='2025', downloaded=False)]
        with patch.object(web, 'ensure_downloader_idle'), patch.object(web, 'sync_year') as sync, \
                patch.object(web.Library, 'catalog', return_value=rows), patch.object(web, 'download_one') as download:
            web.run_worker(self.root, dict(action='download_year', year=2025))
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(sync.call_args.args[1], 2025)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(download.call_args.args[1]['year'], '2025')
            with self.assertRaisesRegex(ValueError, 'Select a conference year'):
                web.run_worker(self.root, dict(action='download_year'))

    def test_catalog_job_caches_titles_without_downloading_or_touching_manifest(self):
        before = self.manifest.read_bytes()
        papers = [web.downloader.Paper(2025, 'A paper', 'https://www.usenix.org/a'),
                  web.downloader.Paper(2025, 'New remote paper', 'https://www.usenix.org/new')]
        with patch.object(web, 'discover_conference', return_value=('https://www.usenix.org/program', papers)), \
                patch.object(web, 'download_one') as download, patch.object(web, 'ensure_downloader_idle') as idle:
            web.run_worker(self.root, dict(action='catalog', year=2025))
        download.assert_not_called()
        idle.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(), before)
        rows = self.catalog()['papers']
        self.assertTrue(next(p for p in rows if p['title'] == 'A paper')['downloaded'])
        remote = next(p for p in rows if p['title'] == 'New remote paper')
        self.assertFalse(remote['downloaded'])
        self.assertIsNone(self.client.get('/api/papers/' + remote['id']).get_json()['viewer_url'])
        self.assertTrue((self.root / '.catalog' / '2025.json').is_file())

    def test_category_refresh_preserves_downloads_and_overrides_stale_metadata(self):
        saved = json.loads(self.manifest.read_text())
        saved['papers'][0].update(category='Old session', program_order=99,
                                  files=[web.downloader.file_record(self.pdf, self.pdf.parent)])
        self.manifest.write_text(json.dumps(saved))
        before = self.manifest.read_bytes()
        original = self.catalog()['papers'][0]
        papers = [web.downloader.Paper(2025, 'A paper', 'https://www.usenix.org/a',
                                      category='Memory Tiering and CXL', program_order=2),
                  web.downloader.Paper(2025, 'Missing paper', 'https://www.usenix.org/b',
                                      category='KV Cache and Long Context', program_order=0)]
        with patch.object(web, 'discover_conference', return_value=('https://www.usenix.org/program', papers)):
            web.sync_year(web.Library(self.root), 2025, None, catalog_only=True)
        data = self.catalog()
        paper = next(p for p in data['papers'] if p['id'] == original['id'])
        self.assertTrue(paper['downloaded'])
        self.assertEqual(paper['category'], 'Memory Tiering and CXL')
        self.assertEqual(paper['program_order'], 2)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertTrue(data['year_status']['2025']['categories_loaded'])
        with patch.object(web.downloader, 'retrieve_paper', wraps=web.downloader.retrieve_paper) as retrieve:
            web.download_one(web.Library(self.root), web.Library(self.root).get(paper['id']), None)
        self.assertEqual(retrieve.call_args.args[1].category, 'Memory Tiering and CXL')
        self.assertEqual(json.loads(self.manifest.read_text())['papers'][0]['category'], 'Memory Tiering and CXL')

    def test_category_lookup_without_available_sessions_is_cached(self):
        paper = web.downloader.Paper(2025, 'A paper', 'https://www.usenix.org/a')
        with patch.object(web, 'discover_conference', return_value=('https://www.usenix.org/program', [paper])):
            web.sync_year(web.Library(self.root), 2025, None, catalog_only=True)
        self.assertTrue(self.catalog()['year_status']['2025']['categories_loaded'])
        self.assertEqual(self.catalog()['papers'][0]['category'], '')

    def test_other_conferences_also_capture_session_categories(self):
        client = Mock()
        client.page.return_value = ('https://www.usenix.org/conference/fast26/technical-sessions', BeautifulSoup('''
          <article class="node-session"><h2>Cloud Technologies I</h2><div class="content">
          <article class="node-paper"><h2><a href="/conference/fast26/presentation/author">A FAST paper</a></h2></article>
          </div></article>''', 'html.parser'))
        _, papers = projects.discover_conference(client, 'fast', 2026)
        self.assertEqual(papers[0].category, 'Cloud Technologies I')
        self.assertEqual(papers[0].program_order, 0)

    def test_individual_download_from_cached_catalog_becomes_readable(self):
        library = web.Library(self.root, 'fast')
        paper = web.downloader.Paper(2025, 'Remote PDF', 'https://example.org/paper')
        with patch.object(web, 'discover_conference', return_value=('https://example.org/program', [paper])):
            web.sync_year(library, 2025, None, catalog_only=True)
        row = library.catalog()[0]
        self.assertFalse(row['downloaded'])
        def retrieve(client, paper, directory, previous):
            path = directory / 'remote.pdf'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.pdf.read_bytes())
            return {**previous, 'path': path.name, 'status': 'downloaded',
                    'files': [web.downloader.file_record(path, directory)]}
        with patch.object(web.downloader, 'retrieve_paper', side_effect=retrieve) as fetch:
            web.run_worker(self.root, dict(action='download', project_id='fast', paper_id=row['id']))
        self.assertEqual(fetch.call_count, 1)
        detail = self.client.get('/api/papers/' + row['id'] + '?project=fast').get_json()
        self.assertTrue(detail['downloaded'])
        self.assertEqual(detail['viewer_type'], 'pdf')
        with self.client.get(detail['viewer_url']) as response:
            self.assertEqual(response.data, self.pdf.read_bytes())

    def test_individual_postscript_download_prepares_reader_automatically(self):
        before = web.Library(self.root).catalog()[0]
        after = {**before, '_path': self.pdf.with_suffix('.ps'), '_manifest': None, '_record': {}}
        with patch.object(web.Library, 'get', side_effect=[before, after]), \
                patch.object(web, 'ensure_downloader_idle'), patch.object(web, 'download_one'), \
                patch.object(web.extractor, 'fingerprint', return_value='digest'), \
                patch.object(web.extractor, 'prepare_pdf') as prepare:
            web.run_worker(self.root, dict(action='download', paper_id=before['id']))
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(prepare.call_args.args[0].path.suffix, '.ps')


if __name__ == '__main__':
    unittest.main()
