"""Conference sources and persistent local reading projects."""
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urljoin, urlsplit, unquote

from bs4 import BeautifulSoup
import pymupdf

import download_osdi as downloader
import extract_outlines as extractor


CONFERENCES = {
    'osdi': ('OSDI', [1994, 1996, 1999, *range(2000, 2021, 2), *range(2021, date.today().year + 1)]),
    'nsdi': ('NSDI', list(range(2012, date.today().year + 1))),
    'fast': ('FAST', list(range(2012, date.today().year + 1))),
    'atc': ('USENIX ATC', list(range(2012, min(date.today().year, 2025) + 1))),
    'usenixsecurity': ('USENIX Security', list(range(2012, date.today().year + 1))),
}


class ProjectStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def list(self):
        result = [dict(id=ident, name=name, kind='conference', years=years)
                  for ident, (name, years) in CONFERENCES.items()]
        for path in sorted((self.root / 'projects').glob('local-*/project.json')):
            if not path.resolve().is_relative_to(self.root / 'projects'):
                continue
            try:
                record = json.loads(path.read_text())
                if record.get('id') == path.parent.name:
                    result.append(dict(id=record['id'], name=record['name'], kind='local', years=[]))
            except (OSError, ValueError, KeyError):
                continue
        return result

    def get(self, ident):
        project = next((p for p in self.list() if p['id'] == ident), None)
        if project is None:
            raise ValueError('Project not found.')
        return project

    def directory(self, ident):
        project = self.get(ident)
        if ident == 'osdi':
            return self.root
        return self.root / ('projects' if project['kind'] == 'local' else 'conferences') / ident

    def validate_name(self, name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError('Enter a project name between 1 and 100 characters.')
        return name.strip()

    def create(self, name):
        name = self.validate_name(name)
        record = dict(id='local-' + secrets.token_hex(6), name=name, kind='local', years=[])
        directory = self.root / 'projects' / record['id']
        extractor.write_json(directory / 'project.json', record)
        (directory / 'papers' / 'local').mkdir(parents=True, exist_ok=True)
        return record

    def rename(self, ident, name):
        record = self.get(ident)
        if record['kind'] != 'local':
            raise ValueError('Conference libraries cannot be renamed.')
        record['name'] = self.validate_name(name)
        extractor.write_json(self.directory(ident) / 'project.json', record)
        return record

    def archive(self, ident):
        record = self.get(ident)
        if record['kind'] != 'local':
            raise ValueError('Only local projects can be removed.')
        trash = self.root / 'projects' / '.trash'
        trash.mkdir(parents=True, exist_ok=True)
        self.directory(ident).rename(trash / f'{ident}-{time.time_ns()}')


def discover_conference(client, conference, year):
    edition = f'https://www.usenix.org/conference/{conference}{year % 100:02d}'
    if conference == 'osdi':
        return downloader.discover_papers(client, year, edition)
    base, soup = client.page(edition + '/technical-sessions')
    pattern = re.compile(rf'/conference/{re.escape(conference)}{year % 100:02d}/(?:technical-sessions/)?presentation/[^/]+/?$')
    papers = {}
    for node in soup.select('.node-paper'):
        link = node.select_one('h2 a[href], h3 a[href]')
        if not link:
            continue
        url = downloader.absolute_url(base, link['href'])
        slug = urlsplit(url).path.rsplit('/', 1)[-1]
        if not pattern.fullmatch(urlsplit(url).path) or any(word in slug for word in ('keynote', 'opening-remarks', 'invited-talk', 'award')):
            continue
        title = downloader.clean_text(link.get_text(' ', strip=True))
        if title:
            papers.setdefault(url, downloader.Paper(year, title, url,
                              category=downloader.paper_category(node), program_order=len(papers)))
    if not papers:
        raise downloader.DownloadError('No research papers found at ' + base)
    return base, list(papers.values())


def validate_url(value):
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError('Enter an HTTP or HTTPS paper URL.')
    parts = urlsplit(value.strip())
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Enter an HTTP or HTTPS paper URL without embedded credentials.')
    return value.strip()


def fetch_pdf(client, url):
    """Resolve direct PDFs, arXiv abstracts, and common publication metadata."""
    url = validate_url(url)
    parts = urlsplit(url)
    initial = url
    if parts.hostname in ('arxiv.org', 'www.arxiv.org', 'export.arxiv.org') and parts.path.startswith('/abs/'):
        initial = 'https://arxiv.org/pdf/' + parts.path.removeprefix('/abs/')
    print('Fetching ' + initial, flush=True)
    response = client.get(initial)
    if response.content.lstrip().startswith(b'%PDF-'):
        return response.content, response.url, ''
    soup = BeautifulSoup(response.content, 'html.parser')
    title = soup.find('meta', attrs={'name': re.compile('^citation_title$', re.I)})
    title = title.get('content', '') if title else ''
    candidates = [m['content'] for m in soup.find_all('meta', attrs={'name': re.compile('^(citation_pdf_url|wkhealth_pdf_url)$', re.I)}) if m.get('content')]
    candidates += [a['href'] for a in soup.select('a[href], link[href]')
                   if (urlsplit(a['href']).path.lower().endswith('.pdf') or a.get('type') == 'application/pdf')
                   and not any(word in a['href'].lower() for word in ('slides', 'supplement'))]
    errors = []
    for candidate in list(dict.fromkeys(candidates))[:5]:
        try:
            target = validate_url(urljoin(response.url, candidate))
            print('Fetching PDF ' + target, flush=True)
            pdf = client.get(target)
            if pdf.content.lstrip().startswith(b'%PDF-'):
                return pdf.content, pdf.url, title
        except (ValueError, downloader.DownloadError) as exc:
            errors.append(str(exc))
    raise ValueError('No accessible PDF found. Use a direct PDF URL or upload the PDF. ' + '; '.join(errors))


def save_pdf(library, data, title, source_url, resolved_url=None):
    if library.project['kind'] != 'local':
        raise ValueError('Papers can be imported only into local projects.')
    if len(data) > 100 * 1024 * 1024:
        raise ValueError('The PDF exceeds the 100 MB limit.')
    downloader.validate_document(data, '.pdf')
    with pymupdf.open(stream=data, filetype='pdf') as document:
        if not len(document):
            raise ValueError('The PDF has no pages.')
        title = title.strip() or (document.metadata or {}).get('title', '').strip()
    if not title:
        title = unquote(Path(urlsplit(resolved_url or source_url).path).stem) or 'Untitled paper'
    title = downloader.clean_text(title)[:300]
    digest = hashlib.sha256(data).hexdigest()
    manifest = library.papers / 'local' / 'manifest.json'
    previous = json.loads(manifest.read_text()) if manifest.exists() else {'papers': []}
    duplicate = next((p for p in previous['papers'] if p.get('sha256') == digest), None)
    if duplicate and (manifest.parent / duplicate['path']).is_file():
        print('This paper is already in the project.', flush=True)
        return duplicate
    stem = re.sub(r'[^\w .()-]', '_', title).strip(' .')[:130] or 'Paper'
    path = manifest.parent / f'{stem} - {digest[:10]}.pdf'
    extractor.atomic_write(path, data)
    record = dict(year='local', title=title, page_url=source_url, resolved_url=resolved_url,
                  status='downloaded', path=path.name, sha256=digest,
                  files=[downloader.file_record(path, manifest.parent)])
    previous['papers'] = [p for p in previous['papers'] if p.get('page_url') != source_url and p.get('sha256') != digest] + [record]
    downloader.save_manifest(manifest, previous)
    print('Saved to ' + library.project['name'] + ': ' + title, flush=True)
    return record
