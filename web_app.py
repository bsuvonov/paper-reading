#!/usr/bin/env python3
"""paper-reading: a local paper library and reader. Run: python3 web_app.py"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import quote

from flask import Flask, abort, jsonify, request, send_file

import download_osdi as downloader
import extract_outlines as extractor
from projects import ProjectStore, CONFERENCES, conference_url, discover_conference, fetch_pdf, save_pdf, validate_url

ROOT = Path(__file__).resolve().parent
YEARS = CONFERENCES['osdi'][1]


class PaperUnavailable(RuntimeError):
    pass


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def within(root, value):
    path = (root / value).resolve()
    return path if path.is_relative_to(root.resolve()) else None


def record_downloaded(directory, record):
    if record.get('status') not in ('downloaded', 'skipped') or not record.get('path'):
        return False
    names = [record['path'], *(f['path'] for f in record.get('files', []))]
    return all((path := within(directory, name)) is not None and path.is_file() for name in names)


class Library:
    def __init__(self, root, project_id='osdi'):
        self.workspace = Path(root).resolve()
        self.store = ProjectStore(self.workspace)
        self.project = self.store.get(project_id)
        self.root = self.store.directory(project_id)
        self.papers = self.root / 'papers'
        self.outlines = self.root / 'outlines'

    def outline_records(self):
        return [r for p in self.outlines.glob('*/*.json')
                if (r := read_json(p)).get('source') and 'sections' in r]

    def catalog_data(self, year):
        saved = read_json(self.papers / str(year) / 'manifest.json')
        cached = read_json(self.root / '.catalog' / (str(year) + '.json'))
        records = {downloader.paper_key(r): r for r in cached.get('papers', [])}
        for record in saved.get('papers', []):
            key = downloader.paper_key(record)
            cached_record = records.get(key, {})
            records[key] = {**cached_record, **record,
                            **{k: cached_record[k] for k in ('category', 'program_order') if k in cached_record}}
        return {**cached, **saved, 'papers': list(records.values()),
                'categories_version': max(cached.get('categories_version', 0), saved.get('categories_version', 0)),
                'discovered': max(cached.get('discovered', 0), saved.get('discovered', 0), len(records))}

    def catalog(self):
        outlines = {r['source']: r for r in self.outline_records()}
        rows = {}
        years = {p.parent.name for p in self.papers.glob('*/manifest.json')}
        years.update(p.stem for p in (self.root / '.catalog').glob('*.json') if p.stem.isdigit())
        for year in sorted(years):
            manifest = self.papers / year / 'manifest.json'
            data = self.catalog_data(year)
            for record in data.get('papers', []):
                key = downloader.paper_key(record)
                prefix = '' if self.project['id'] == 'osdi' else self.project['id'] + ':'
                ident = hashlib.sha256(f'{prefix}{manifest.parent.name}:{key}'.encode()).hexdigest()[:20]
                path = within(manifest.parent, record['path']) if record.get('path') else None
                downloaded = record_downloaded(manifest.parent, record)
                outline = outlines.get(str(path), {}) if downloaded else {}
                rows[ident] = dict(id=ident, title=record['title'], year=manifest.parent.name,
                                   category=record.get('category', ''), program_order=record.get('program_order'),
                                   downloaded=downloaded, outline_status=outline.get('status'),
                                   pdf_unavailable=not downloaded and record.get('reason') == 'no_public_copy',
                                   section_count=len(outline.get('sections', [])),
                                   error=record.get('error'), source_url=record.get('page_url'),
                                   _path=path, _record=record, _manifest=manifest, _outline=outline)
        # Also support papers copied into the library without a download manifest.
        known_paths = {str(r['_path']) for r in rows.values() if r['_path']}
        for paper in extractor.discover(self.papers, None):
            if str(paper.path) in known_paths:
                continue
            ident = hashlib.sha256(str(paper.path).encode()).hexdigest()[:20]
            outline = outlines.get(str(paper.path), {})
            rows[ident] = dict(id=ident, title=paper.title, year=paper.year, downloaded=True,
                               category='', program_order=None,
                               outline_status=outline.get('status'), section_count=len(outline.get('sections', [])),
                               source_url=None, error=None, _path=paper.path, _record={},
                               _manifest=None, _outline=outline)
        return sorted(rows.values(), key=lambda r: (-int(r['year']) if r['year'].isdigit() else 0, r['title'].lower()))

    def year_status(self, rows):
        statuses = {}
        for year in self.project['years']:
            directory = self.papers / str(year)
            manifest = self.catalog_data(year)
            records = manifest.get('papers', [])
            total = max(len(records), manifest.get('discovered', 0))
            downloaded = sum(record_downloaded(directory, r) for r in records)
            status = 'downloaded' if total and downloaded == total else 'partial' if downloaded else 'not_downloaded'
            if manifest.get('status') == 'in_progress':
                status = 'in_progress'
            if not total:
                # Loose files alone cannot establish that an entire edition is saved.
                downloaded = sum(r['downloaded'] for r in rows if r['year'] == str(year))
                status = 'partial' if downloaded else 'not_downloaded'
            statuses[str(year)] = dict(status=status, downloaded=downloaded, total=total or None,
                                       categories_loaded=manifest['categories_version'] >= downloader.CATEGORY_VERSION,
                                       catalogued=bool(records) and len(records) >= total)
        return statuses

    def get(self, ident):
        return next((r for r in self.catalog() if r['id'] == ident), None)

    def public(self, row):
        return {**{k: v for k, v in row.items() if not k.startswith('_')},
                'project_id': self.project['id'], 'project_name': self.project['name'],
                'project_kind': self.project['kind']}

    def file_url(self, path):
        path = Path(path).resolve()
        for name, root in [('papers', self.papers), ('outlines', self.outlines)]:
            if path.is_relative_to(root):
                prefix = '/files/' if self.project['id'] == 'osdi' else '/project-files/' + self.project['id'] + '/'
                return prefix + name + '/' + quote(path.relative_to(root).as_posix())
        return None

    def paper(self, row):
        files = tuple(p for f in row['_record'].get('files', [])
                      if (p := within(row['_manifest'].parent, f['path']))) if row['_manifest'] else ()
        return extractor.Paper(row['_path'], row['title'], row['year'], files)

    def detail(self, row):
        result = self.public(row)
        outline = row['_outline']
        result.update(sections=outline.get('sections', []), warnings=outline.get('warnings', []),
                      viewer_url=None, viewer_type=None, original_url=None, outline_url=None)
        result['sections'] = [{**s, 'url': self.file_url(s['source']) if s.get('source') else None}
                              for s in result['sections']]
        if outline.get('outline_path') and Path(outline['outline_path']).is_file():
            result['outline_url'] = self.file_url(outline['outline_path'])
        if row['downloaded']:
            path = row['_path']
            result['original_url'] = self.file_url(path)
            fmt = extractor.source_format(path)
            if fmt.startswith('.ps'):
                digest = extractor.fingerprint(self.paper(row))
                path = self.outlines / '.cache' / (digest + '.pdf')
                fmt = '.pdf'
            if path.is_file():
                result.update(viewer_url=self.file_url(path), viewer_type='pdf' if fmt == '.pdf' else 'html' if fmt in ('.html', '.htm') else 'text')
        return result

    def remove_paper(self, row):
        if self.project['kind'] != 'local':
            raise ValueError('Only local project papers can be removed.')
        trash = self.root / '.trash' / f'{row["id"]}-{time.time_ns()}'
        paths = self.paper(row).bundle_files if row['_path'] else ()
        paths = paths or ([row['_path']] if row['_path'] else [])
        for path in paths:
            if path.is_file() and path.is_relative_to(self.papers):
                destination = trash / 'papers' / path.relative_to(self.papers)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(path, destination)
        manifest = row['_manifest']
        if manifest:
            data = read_json(manifest)
            key = downloader.paper_key(row['_record'])
            data['papers'] = [r for r in data['papers'] if downloader.paper_key(r) != key]
            downloader.save_manifest(manifest, data)
        outline = row['_outline']
        if outline.get('outline_path'):
            for path in [Path(outline['outline_path']), Path(outline['outline_path']).with_suffix('.json')]:
                if path.is_file() and path.is_relative_to(self.outlines):
                    destination = trash / 'outlines' / path.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(path, destination)
        extractor.write_comparison(self.outline_records(), self.outlines)


class Jobs:
    """Run one worker at a time, with a FIFO queue for paper downloads."""
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.current = None
        self.process = None
        self.pending = deque()
        self.completed = deque(maxlen=50)

    def _log(self, job, limit=12000):
        path = self.root / '.web-jobs' / (job['id'] + '.log')
        if not path.exists():
            return ''
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - limit))
            return stream.read().decode(errors='replace')

    def _launch(self, job):
        self.current = job
        job.update(status='running', started=time.time())
        directory = self.root / '.web-jobs'
        try:
            with (directory / (job['id'] + '.log')).open('wb') as log:
                self.process = subprocess.Popen([sys.executable, '-u', str(ROOT / 'web_app.py'),
                                                 '--root', str(self.root), '--worker',
                                                 str(directory / (job['id'] + '.json'))],
                                                stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
        except OSError as exc:
            self.process = None
            (directory / (job['id'] + '.log')).write_text(f'Could not start task: {exc}\n')
            self._finish(1)
            return
        # The queue continues even when the browser is closed or stops polling.
        threading.Thread(target=self._watch, args=(self.process,), daemon=True).start()

    def _watch(self, process):
        process.wait()
        with self.lock:
            if self.process is process:
                self._advance()

    def _finish(self, code):
        self.current.update(status={0: 'done', 3: 'unavailable'}.get(code, 'failed'), finished=time.time())
        completed = dict(self.current)
        if code:
            completed['log'] = self._log(completed, 1000)
        self.completed.append(completed)

    def _advance(self):
        if self.current and self.current['status'] == 'running':
            code = self.process.poll()
            if code is None:
                return
            self._finish(code)
        while self.pending and (not self.current or self.current['status'] != 'running'):
            self._launch(self.pending.popleft())

    def snapshot(self):
        with self.lock:
            self._advance()
            if not self.current:
                return None
            return {**self.current, 'log': self._log(self.current),
                    'queue': [{**job, 'position': i} for i, job in enumerate(self.pending, 1)],
                    'completed': list(self.completed)}

    def start(self, payload):
        with self.lock:
            self._advance()
            busy = self.process and self.process.poll() is None
            if busy and payload['action'] != 'download':
                raise RuntimeError('A task is already running. You can keep reading while it finishes.')
            if payload['action'] == 'download':
                candidates = ([self.current] if busy else []) + list(self.pending)
                if any(job['action'] == 'download' and job['paper_id'] == payload.get('paper_id') and
                       job['project_id'] == payload.get('project_id', 'osdi') for job in candidates):
                    return self.snapshot()
            ident = secrets.token_hex(8)
            directory = self.root / '.web-jobs'
            directory.mkdir(exist_ok=True)
            spec = directory / (ident + '.json')
            extractor.write_json(spec, payload)
            self.pending.append(dict(id=ident, action=payload['action'], paper_id=payload.get('paper_id'),
                                     project_id=payload.get('project_id', 'osdi'), year=payload.get('year'),
                                     title=payload.get('title'), status='queued', queued=time.time()))
            self._advance()
            return self.snapshot()


def ensure_downloader_idle(root):
    # Older downloader processes do not share the app's job lock. Do not race
    # their manifest updates; this also protects the user's existing download.
    proc = Path('/proc')
    for process in proc.glob('[0-9]*'):
        try:
            args = (process / 'cmdline').read_bytes().split(b'\0')
            if any(Path(a.decode()).name == 'download_osdi.py' for a in args if a):
                if (process / 'cwd').resolve() == root:
                    raise RuntimeError('The command-line downloader is still running. Wait for it to finish before starting another download or refreshing the catalog.')
        except (OSError, UnicodeError):
            pass


def sync_year(library, year, client, catalog_only=False):
    print(f'{library.project["name"]} {year}: finding papers in the conference proceedings…', flush=True)
    manifest = library.papers / str(year) / 'manifest.json'
    previous = read_json(manifest)
    old = {downloader.paper_key(r): r for r in previous.get('papers', [])}
    conference = library.project['id']
    edition = conference_url(conference, year)
    program, papers = discover_conference(client, conference, year)
    if catalog_only:
        records = [{**asdict(p), 'status': 'not_selected'} for p in papers]
        downloader.save_manifest(library.root / '.catalog' / f'{year}.json',
                                 dict(year=year, edition_url=edition, program_url=program,
                                      categories_version=downloader.CATEGORY_VERSION,
                                      discovered=len(records), status='catalogued', papers=records))
        print(f'{year}: found {len(records)} papers.', flush=True)
        return
    records = [{**old.get(downloader.paper_key(asdict(p)), {}), **asdict(p)} for p in papers]
    for record in records:
        record.setdefault('status', 'not_selected')
    downloader.save_manifest(manifest, dict(year=year, edition_url=edition, program_url=program,
                                            categories_version=downloader.CATEGORY_VERSION,
                                            discovered=len(records), status='catalogued', papers=records))
    print(f'{year}: found {len(records)} papers.', flush=True)


def download_one(library, row, client):
    if not row['_manifest']:
        raise RuntimeError('This paper has no download URL.')
    raw = row['_record']
    if library.project['kind'] == 'local':
        data, resolved, title = fetch_pdf(client, raw['page_url'])
        save_pdf(library, data, row['title'] or title, raw['page_url'], resolved)
        return
    paper = downloader.Paper(int(row['year']), row['title'], raw['page_url'], tuple(raw.get('file_urls', [])),
                             category=raw.get('category', ''), program_order=raw.get('program_order'))
    print('Downloading: ' + row['title'], flush=True)
    record = downloader.retrieve_paper(client, paper, row['_manifest'].parent, raw)
    manifest = library.catalog_data(row['year'])
    manifest['papers'] = [record if downloader.paper_key(r) == downloader.paper_key(raw) else r
                          for r in manifest['papers']]
    downloader.save_manifest(row['_manifest'], manifest)
    if record['status'] == 'failed':
        if record.get('reason') == 'no_public_copy':
            raise PaperUnavailable(record['error'])
        raise RuntimeError(record.get('error', 'Download failed'))
    print('Paper saved.', flush=True)


def generate_outline(library, row):
    paper = library.paper(row)
    digest = extractor.fingerprint(paper)
    destination = library.outlines / paper.year / (paper.stem + '.json')
    markdown = destination.with_suffix('.md')
    fmt = extractor.source_format(paper.path)
    record = dict(version=extractor.VERSION, year=paper.year, title=paper.title, source=str(paper.path),
                  source_format=fmt, fingerprint=digest, outline_path=str(markdown),
                  options=dict(ocr='auto', dpi=300, max_depth=4, tessdata=None))
    if fmt in ('.html', '.htm'):
        sections, pages, warnings = extractor.extract_html(paper)
    elif fmt == '.txt':
        sections, pages, warnings = extractor.extract_text(paper.path)
    else:
        pdf = extractor.prepare_pdf(paper, library.outlines / '.cache', digest)
        record['pdf_source'] = str(pdf)
        sections, pages, warnings = extractor.extract_pdf(pdf, library.outlines / '.cache', 'auto', None, 300)
    warnings += extractor.assess(sections, pages)
    record.update(sections=[s for s in sections if s['level'] <= 4], page_count=pages, warnings=warnings,
                  status='needs_review' if warnings else 'ok', methods=sorted({s['method'] for s in sections}))
    extractor.write_json(destination, record)
    extractor.atomic_write(markdown, extractor.outline_markdown(record, markdown).encode())
    extractor.write_comparison(library.outline_records(), library.outlines)
    print(f'Saved {len(record["sections"])} headings.' + (' Review notes included.' if warnings else ''), flush=True)


def run_worker(root, payload):
    library = Library(root, payload.get('project_id', 'osdi'))
    action = payload['action']
    client = downloader.Client(timeout=30, retries=2)
    if library.project['id'] == 'osdi' and action in ('download', 'download_year'):
        ensure_downloader_idle(root)
    if action == 'import_url':
        if library.project['kind'] != 'local':
            raise ValueError('URL import is available only in local projects.')
        data, resolved, title = fetch_pdf(client, payload['url'])
        save_pdf(library, data, payload.get('title') or title, payload['url'], resolved)
        return
    if action == 'copy_paper':
        if library.project['kind'] != 'local':
            raise ValueError('Choose a local project as the destination.')
        source = Library(root, payload['source_project'])
        row = source.get(payload['paper_id'])
        if not row or not row['downloaded']:
            raise ValueError('Download the source paper first.')
        paper = source.paper(row)
        fmt = extractor.source_format(paper.path)
        if fmt in ('.pdf', '.ps', '.ps.gz', '.ps.z'):
            pdf = extractor.prepare_pdf(paper, source.outlines / '.cache', extractor.fingerprint(paper))
            save_pdf(library, pdf.read_bytes(), row['title'], row['source_url'] or 'copy:' + row['id'])
        else:
            # Preserve complete legacy HTML bundles and text papers.
            target = library.papers / 'local' / row['id']
            paths = paper.bundle_files or (paper.path,)
            for path in paths:
                destination = target / path.relative_to(paper.path.parent)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
            manifest = library.papers / 'local' / 'manifest.json'
            data = read_json(manifest, {'papers': []})
            key = 'copy:' + row['id']
            record = dict(title=row['title'], year='local', page_url=key, status='downloaded',
                          path=(target / paper.path.name).relative_to(manifest.parent).as_posix(),
                          files=[downloader.file_record(target / p.relative_to(paper.path.parent), manifest.parent) for p in paths])
            data['papers'] = [r for r in data['papers'] if r.get('page_url') != key] + [record]
            downloader.save_manifest(manifest, data)
        print('Paper added to project.', flush=True)
        return
    if action in ('catalog', 'download_year'):
        if library.project['kind'] != 'conference':
            raise ValueError('Catalog discovery is available only for conference libraries.')
        if action == 'download_year' and (type(payload.get('year')) is not int or payload['year'] not in library.project['years']):
            raise ValueError('Select a conference year to download.')
        years = [payload['year']] if payload.get('year') else library.project['years']
        errors = []
        for year in years:
            try:
                sync_year(library, year, client, catalog_only=action == 'catalog')
                if action == 'download_year':
                    for row in library.catalog():
                        if row['year'] == str(year) and not row['downloaded']:
                            try:
                                download_one(library, row, client)
                            except Exception as exc:
                                errors.append(str(exc))
                                print('FAILED: ' + str(exc), flush=True)
            except Exception as exc:
                errors.append(str(exc))
                print('FAILED: ' + str(exc), flush=True)
        if errors:
            raise RuntimeError(f'{len(errors)} task(s) failed; see the progress log. Completed files have been kept.')
        return
    row = library.get(payload['paper_id'])
    if not row:
        raise RuntimeError('Paper no longer exists in the catalog. Refresh the library.')
    if action == 'download':
        download_one(library, row, client)
        row = library.get(payload['paper_id'])
        if row and row['downloaded'] and extractor.source_format(row['_path']).startswith('.ps'):
            print('Preparing the downloaded paper for reading…', flush=True)
            paper = library.paper(row)
            extractor.prepare_pdf(paper, library.outlines / '.cache', extractor.fingerprint(paper))
    elif action == 'outline':
        if not row['downloaded']:
            raise RuntimeError('Download this paper first.')
        print('Extracting outline: ' + row['title'], flush=True)
        generate_outline(library, row)
    elif action == 'prepare':
        if not row['downloaded']:
            raise RuntimeError('Download this paper first.')
        paper = library.paper(row)
        print('Preparing this paper for the reader…', flush=True)
        extractor.prepare_pdf(paper, library.outlines / '.cache', extractor.fingerprint(paper))


def create_app(root=ROOT):
    app = Flask(__name__, static_folder=str(ROOT / 'web'), static_url_path='/static')
    library = Library(root)
    jobs = Jobs(library.root)
    token = secrets.token_urlsafe(32)
    store = ProjectStore(root)
    from codex_sessions import register_codex
    register_codex(app, root, Library)
    app.config.update(LIBRARY=library, JOBS=jobs, TRUSTED_HOSTS=['localhost', '127.0.0.1', '[::1]'],
                      MAX_CONTENT_LENGTH=101 * 1024 * 1024)

    @app.before_request
    def protect_writes():
        if request.path.startswith('/api/codex/') or (request.path.startswith('/api/') and request.method in ('POST', 'PATCH', 'DELETE')):
            if not secrets.compare_digest(request.headers.get('X-Library-Token', ''), token):
                return jsonify(error='Reload the page before making changes.'), 403

    @app.errorhandler(ValueError)
    @app.errorhandler(downloader.DownloadError)
    def invalid_input(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(RuntimeError)
    def conflict(exc):
        return jsonify(error=str(exc)), 409

    @app.errorhandler(413)
    def too_large(exc):
        return jsonify(error='The upload exceeds the 100 MB limit.'), 413

    def selected_library():
        return Library(root, request.args.get('project', 'osdi'))

    @contextmanager
    def edit_library():
        with jobs.lock:
            jobs._advance()
            if jobs.process and jobs.process.poll() is None:
                raise RuntimeError('Wait for the current task to finish before changing projects or papers.')
            yield

    @app.get('/')
    def index():
        return send_file(ROOT / 'web/index.html')

    @app.get('/api/library')
    def catalog():
        selected = selected_library()
        rows = selected.catalog()
        return jsonify(papers=[selected.public(r) for r in rows], years=selected.project['years'],
                       year_status=selected.year_status(rows),
                       project=selected.project, projects=store.list(), token=token, job=jobs.snapshot())

    @app.post('/api/projects')
    def create_project():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            raise ValueError('Enter a project name.')
        with edit_library():
            project = store.create(payload.get('name'))
        return jsonify(project=project), 201

    @app.route('/api/projects/<ident>', methods=['PATCH', 'DELETE'])
    def edit_project(ident):
        with edit_library():
            if request.method == 'DELETE':
                store.archive(ident)
                return jsonify(removed=True)
            payload = request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                raise ValueError('Enter a project name.')
            return jsonify(project=store.rename(ident, payload.get('name')))

    @app.post('/api/projects/<ident>/upload')
    def upload_paper(ident):
        with edit_library():
            selected = Library(root, ident)
            if selected.project['kind'] != 'local':
                raise ValueError('PDF uploads are available only in local projects.')
            uploaded = request.files.get('file')
            if not uploaded or not uploaded.filename:
                raise ValueError('Choose a PDF to upload.')
            data = uploaded.read(100 * 1024 * 1024 + 1)
            title = request.form.get('title', '').strip() or Path(uploaded.filename).stem
            record = save_pdf(selected, data, title, 'upload:' + hashlib.sha256(data).hexdigest())
        return jsonify(title=record['title']), 201

    @app.get('/api/papers/<ident>')
    def detail(ident):
        selected = selected_library()
        row = selected.get(ident)
        if not row:
            abort(404)
        return jsonify(selected.detail(row))

    @app.delete('/api/papers/<ident>')
    def remove_paper(ident):
        with edit_library():
            selected = selected_library()
            row = selected.get(ident)
            if not row:
                abort(404)
            selected.remove_paper(row)
        return jsonify(removed=True)

    @app.get('/api/jobs')
    def job_status():
        return jsonify(job=jobs.snapshot())

    @app.post('/api/jobs')
    def start_job():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get('action') not in ('catalog', 'download', 'download_year', 'outline', 'prepare', 'import_url', 'copy_paper'):
            return jsonify(error='Unknown task.'), 400
        selected = Library(root, payload.get('project_id', 'osdi'))
        payload['project_id'] = selected.project['id']
        if payload['action'] in ('catalog', 'download_year'):
            if selected.project['kind'] != 'conference':
                raise ValueError('Choose a conference library to find papers online.')
            if payload['action'] == 'download_year' and payload.get('year') is None:
                raise ValueError('Select a conference year to download.')
            if payload.get('year') is not None and (type(payload['year']) is not int or payload['year'] not in selected.project['years']):
                return jsonify(error='Choose a valid conference year.'), 400
        elif payload['action'] == 'import_url':
            if selected.project['kind'] != 'local':
                raise ValueError('URL import is available only in local projects.')
            payload['url'] = validate_url(payload.get('url'))
            if not isinstance(payload.get('title', ''), str) or len(payload.get('title', '')) > 300:
                raise ValueError('Paper titles must be at most 300 characters.')
        elif payload['action'] == 'copy_paper':
            if selected.project['kind'] != 'local':
                raise ValueError('Choose a local project as the destination.')
            source = Library(root, payload.get('source_project', 'osdi'))
            row = source.get(payload.get('paper_id'))
            if not row or not row['downloaded']:
                raise ValueError('Download the source paper first.')
        else:
            row = selected.get(payload.get('paper_id'))
            if not row:
                return jsonify(error='Paper not found.'), 404
            payload['title'] = row['title']
        try:
            return jsonify(job=jobs.start(payload)), 202
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409

    @app.get('/files/<area>/<path:name>')
    @app.get('/project-files/<project_id>/<area>/<path:name>')
    def files(area, name, project_id='osdi'):
        selected = Library(root, project_id)
        directory = {'papers': selected.papers, 'outlines': selected.outlines}.get(area)
        path = within(directory, name) if directory else None
        if not path or not path.is_file() or path.suffix.lower() not in ('.pdf', '.html', '.htm', '.txt', '.md', '.json', '.ps', '.gz', '.z', '.gif', '.png', '.jpg', '.jpeg', '.svg', '.css'):
            abort(404)
        response = send_file(path, conditional=True)
        if path.suffix.lower() in ('.html', '.htm', '.svg'):
            response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.after_request
    def no_cache(response):
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--root', type=Path, default=ROOT, help='directory containing papers/ and outlines/')
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        try:
            run_worker(args.root.resolve(), read_json(args.worker))
        except PaperUnavailable as exc:
            print(str(exc), flush=True)
            sys.exit(3)
        except Exception as exc:
            print('Error: ' + str(exc), flush=True)
            sys.exit(1)
    else:
        create_app(args.root).run(host='127.0.0.1', port=args.port, threaded=True, debug=False)
