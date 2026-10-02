"""Search every conference catalog and discover missing paper lists in the background."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import threading
import time
import unicodedata

import download_osdi as downloader
from projects import CONFERENCES, conference_url, discover_conference


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


class ConferenceSearch:
    def __init__(self, root, library_class):
        self.root = Path(root)
        self.library_class = library_class
        self.lock = threading.RLock()
        self.expires = 0
        self.rows = []
        self.ready = set()
        self.editions = {(ident, year) for ident, (_, years) in CONFERENCES.items() for year in years}
        self.running = False
        self.started = False
        self.errors = {}
        self.thread = None
        self.client = downloader.Client(timeout=15, retries=0)

    def _refresh(self):
        if time.monotonic() < self.expires:
            return
        rows, ready = [], set()
        for ident in CONFERENCES:
            library = self.library_class(self.root, ident)
            rows.extend(library.public(row) for row in library.catalog())
            for year in CONFERENCES[ident][1]:
                data = library.catalog_data(year)
                count = len(data.get('papers', []))
                if (count and count >= data.get('discovered', count)) or (data.get('status') == 'catalogued' and not data.get('discovered')):
                    ready.add((ident, year))
        self.rows = [(row, normalize(f'{row["title"]} {row.get("category", "")} {row["project_name"]} {row["year"]}')) for row in rows]
        self.ready = ready
        self.expires = time.monotonic() + 2

    def _status(self):
        errors = [dict(conference=ident, year=year, message=message)
                  for (ident, year), message in sorted(self.errors.items()) if (ident, year) not in self.ready]
        return dict(indexed=len(self.ready), total=len(self.editions), running=self.running,
                    started=self.started, errors=errors)

    def search(self, query, availability='all', limit=100):
        if not isinstance(query, str) or len(query) > 300:
            raise ValueError('Search text must be at most 300 characters.')
        if availability not in ('all', 'downloaded', 'missing', 'outlines'):
            raise ValueError('Choose a valid availability filter.')
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError('Search result limit must be between 1 and 5000.')
        query = normalize(query)
        terms = query.split()
        with self.lock:
            self._refresh()
            matches = [row for row, text in self.rows if query and all(word in text for word in terms) and
                       (availability == 'all' or availability == 'downloaded' and row['downloaded'] or
                        availability == 'missing' and not row['downloaded'] or
                        availability == 'outlines' and row['section_count'] > 0)]
            matches.sort(key=lambda row: (normalize(row['title']) != query, not normalize(row['title']).startswith(query),
                                         -int(row['year']) if row['year'].isdigit() else 0,
                                         row['project_name'], normalize(row['title']), row['id']))
            return dict(papers=matches[:limit], total=len(matches), index=self._status())

    def start(self, retry=False):
        with self.lock:
            self._refresh()
            if self.running or self.started and not retry:
                return self._status()
            missing = sorted(self.editions - self.ready, key=lambda edition: (-edition[1], edition[0]))
            self.started = True
            self.errors = {}
            if missing:
                self.running = True
                self.thread = threading.Thread(target=self._build, args=(missing,), daemon=True)
                self.thread.start()
            return self._status()

    def _fetch(self, edition):
        ident, year = edition
        library = self.library_class(self.root, ident)
        program, papers = discover_conference(self.client, ident, year)
        records = [{**asdict(paper), 'status': 'not_selected'} for paper in papers]
        data = dict(year=year, edition_url=conference_url(ident, year), program_url=program,
                    categories_version=downloader.CATEGORY_VERSION, discovered=len(records), status='catalogued', papers=records)
        directory = library.root / '.catalog'
        directory.mkdir(parents=True, exist_ok=True)
        # Other workers can refresh this same edition. A unique temporary file
        # keeps readers safe and avoids racing their fixed .part filename.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, suffix='.part', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False)
            temporary.replace(directory / f'{year}.json')
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def _build(self, editions):
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(self._fetch, edition): edition for edition in editions}
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        with self.lock:
                            self.errors[futures[future]] = str(exc)
                    finally:
                        with self.lock:
                            self.expires = 0
        finally:
            with self.lock:
                self.running = False
                self.expires = 0
