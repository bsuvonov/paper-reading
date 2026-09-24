"""Paper access commands installed in each Codex research workspace."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

from bs4 import BeautifulSoup
import pymupdf

import download_osdi as downloader
import extract_outlines as extractor
from projects import discover_conference, fetch_pdf


def write_index(workspace, data):
    lines = ['# Papers in this session', '', data['label'], '',
             'Use `python tools/papers.py list` for paper keys and local availability.',
             'Use `python tools/papers.py discover` to load uncatalogued years.', '']
    for paper in data['papers']:
        title = paper['title'].replace('\n', ' ')
        lines.extend([f'## {title}', f'- Key: `{paper["key"]}`',
                      f'- Source: {paper["project_name"]} {paper["year"]}',
                      f'- Local file: {paper.get("path") or "Not downloaded"}',
                      f'- Paper URL: {paper.get("source_url") or "Local import"}'])
        if paper.get('outline'):
            lines.append(f'- Outline: {paper["outline"]}')
        lines.append('')
    if data.get('missing_catalogs'):
        lines.extend(['## Proceedings still to discover', ''])
        lines.extend(f'- {entry["project_id"]} {entry["year"]}' for entry in data['missing_catalogs'])
    extractor.atomic_write(workspace / 'PAPERS.md', ('\n'.join(lines) + '\n').encode())


def save(workspace, data):
    extractor.write_json(workspace / 'scope.json', data)
    write_index(workspace, data)


def local_path(workspace, paper):
    name = paper.get('path')
    if not name or Path(name).is_absolute() or '..' in Path(name).parts:
        return None
    path = workspace / name
    return path if path.is_file() else None


def discover(workspace, data, client):
    remaining, errors = [], []
    records = {p['key']: p for p in data['papers']}
    pending = list(data.get('missing_catalogs', []))
    for index, choice in enumerate(pending):
        conference, year = choice['project_id'], choice['year']
        try:
            _, papers = discover_conference(client, conference, year)
            for paper in papers:
                raw = asdict(paper)
                prefix = '' if conference == 'osdi' else conference + ':'
                ident = hashlib.sha256(f'{prefix}{year}:{downloader.paper_key(raw)}'.encode()).hexdigest()[:20]
                key = conference + ':' + ident
                previous = records.get(key, {})
                records[key] = {**previous, 'key': key, 'id': ident, 'project_id': conference,
                                'project_name': conference.upper(), 'year': str(year), 'title': paper.title,
                                'category': paper.category, 'source_url': paper.page_url,
                                'file_urls': list(paper.file_urls), 'path': previous.get('path'),
                                'downloaded': local_path(workspace, previous) is not None}
            print(f'{conference} {year}: {len(papers)} papers.', flush=True)
            if choice not in data.setdefault('discovered_catalogs', []):
                data['discovered_catalogs'].append(choice)
        except (ValueError, downloader.DownloadError) as exc:
            remaining.append(choice)
            errors.append(f'{conference} {year}: {exc}')
            print(errors[-1], file=sys.stderr)
        data.update(papers=list(records.values()), missing_catalogs=remaining + pending[index + 1:])
        save(workspace, data)
    data.update(papers=list(records.values()), missing_catalogs=remaining)
    save(workspace, data)
    return errors


def fetch(workspace, data, paper, client):
    if local_path(workspace, paper):
        print(paper['key'] + ': already available at ' + paper['path'])
        return
    directory = workspace / 'papers' / paper['project_id'] / paper['year'] / paper['id']
    directory.mkdir(parents=True, exist_ok=True)
    if paper['year'].isdigit():
        item = downloader.Paper(int(paper['year']), paper['title'], paper['source_url'],
                                tuple(paper.get('file_urls', [])), category=paper.get('category', ''))
        result = downloader.retrieve_paper(client, item, directory, {})
        if result['status'] == 'failed':
            raise downloader.DownloadError(result.get('error', 'Download failed.'))
        path = directory / result['path']
    else:
        content, _, _ = fetch_pdf(client, paper['source_url'])
        downloader.validate_document(content, '.pdf')
        path = directory / 'paper.pdf'
        downloader.atomic_write(path, content)
    paper.update(path=path.relative_to(workspace).as_posix(), downloaded=True)
    save(workspace, data)
    print(paper['key'] + ': saved to ' + paper['path'])


def text(workspace, paper, pages=None):
    path = local_path(workspace, paper)
    if not path:
        raise ValueError('Paper is not downloaded. Run fetch with its paper key first.')
    fmt = extractor.source_format(path)
    if fmt in ('.html', '.htm'):
        files = [path, *sorted(p for p in path.parent.rglob('*.html') if p != path)]
        for source in files:
            soup = BeautifulSoup(source.read_bytes(), 'html.parser')
            for node in soup.select('script,style'):
                node.decompose()
            print(f'\n--- {source.name} ---\n' + soup.get_text('\n', strip=True))
    elif fmt == '.txt':
        print(path.read_text(errors='replace'))
    else:
        if fmt != '.pdf':
            source = extractor.Paper(path, paper['title'], paper['year'])
            path = extractor.prepare_pdf(source, workspace / '.cache', extractor.fingerprint(source))
        with pymupdf.open(path) as document:
            start, end = 1, len(document)
            if pages:
                parts = pages.split('-', 1)
                start, end = int(parts[0]), int(parts[-1])
                if not (1 <= start <= end <= len(document)):
                    raise ValueError('Page range is outside this paper.')
            for number in range(start - 1, end):
                output = document[number].get_text(sort=True)
                print(f'\n--- Page {number + 1} ---\n' + (output.strip() or '[No extractable text on this page; OCR may be needed.]'))


def main(workspace):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['list', 'discover', 'fetch', 'text'])
    parser.add_argument('key', nargs='?')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--pages')
    args = parser.parse_args()
    data = json.loads((workspace / 'scope.json').read_text())
    client = downloader.Client(timeout=30, retries=2)
    try:
        if args.command == 'list':
            for paper in data['papers']:
                print(f'{paper["key"]}\t{"local" if local_path(workspace, paper) else "missing"}\t{paper["title"]}')
        elif args.command == 'discover':
            if discover(workspace, data, client):
                raise ValueError('Some proceedings could not be loaded; the remaining entries are retained for retry.')
        else:
            papers = data['papers'] if args.all and args.command == 'fetch' else [p for p in data['papers'] if p['key'] == args.key]
            if not papers:
                raise ValueError('Choose a paper key from list.')
            errors = []
            for paper in papers:
                try:
                    fetch(workspace, data, paper, client) if args.command == 'fetch' else text(workspace, paper, args.pages)
                except (ValueError, OSError, downloader.DownloadError) as exc:
                    errors.append(f'{paper["key"]}: {exc}')
                    print(errors[-1], file=sys.stderr)
            if errors:
                raise ValueError(f'{len(errors)} paper(s) failed; completed work was retained.')
    except (ValueError, OSError, downloader.DownloadError) as exc:
        parser.exit(1, str(exc) + '\n')
