"""ASPLOS programs and historical proceedings from publisher-deposited Crossref metadata."""
from datetime import date
import hashlib
import re
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

import download_osdi as downloader
from public_copies import normalized_title


HOSTS = ('asplos-conference.org', 'www.asplos-conference.org')
# ACM proceedings identifiers, including the irregular early editions.
PROCEEDINGS = {
    1982: '800050', 1987: '36206', 1989: '70082', 1991: '106972',
    1992: '143365', 1994: '195473', 1996: '237090', 1998: '291069',
    2000: '378993', 2002: '605397', 2004: '1024393', 2006: '1168857',
    2008: '1346281', 2009: '1508244', 2010: '1736020', 2011: '1950365',
    2012: '2150976', 2013: '2451116', 2014: '2541940', 2015: '2694344',
    2016: '2872362', 2017: '3037697', 2018: '3173162', 2019: '3297858',
    2020: '3373376', 2021: '3445814', 2022: '3503222',
}
YEARS = [*PROCEEDINGS, *range(2023, date.today().year + 1)]
# The 2026 program omits paper links. Match only its published ACM volumes;
# volume 1 was published in 2025, ahead of the conference.
LINK_PROCEEDINGS = {2026: ('3760250', '3779212')}


def edition_url(year):
    if year < 2022:
        return 'https://dl.acm.org/doi/proceedings/10.1145/' + PROCEEDINGS[year]
    return f'https://www.asplos-conference.org/asplos{year}/'


def program_url(year):
    page = {2023: 'index.html%3Fp%3D3602.html', 2024: 'main-program/index.html',
            2025: 'program.html'}.get(year, 'program/')
    return edition_url(year) + page


def file_urls(base, links):
    urls = []
    for link in links:
        url = urljoin(base, link.get('href', ''))
        parts = urlsplit(url)
        label = link.get_text(' ', strip=True).lower()
        if parts.scheme not in ('http', 'https') or any(word in label for word in ('slide', 'video', 'lightning', 'artifact')):
            continue
        if re.search(r'(?:slides?|videos?)[/_.-]', parts.path, re.I):
            continue
        doi = re.search(r'10\.1145/\d+\.\d+$', parts.path)
        if parts.hostname in ('doi.org', 'dx.doi.org', 'dl.acm.org') and doi:
            urls.append('https://dl.acm.org/doi/pdf/' + doi[0])
        elif parts.path.lower().endswith('.pdf'):
            urls.append(url)
    return tuple(dict.fromkeys(urls))


def parse_program(year, base, soup):
    """Parse session panels (2023/2025/2026) and session tables (2022/2024)."""
    papers = {}

    def add(title, links, category):
        title = downloader.clean_text(title)
        if not title:
            return
        key = normalized_title(title)
        source = base.split('#', 1)[0] + '#paper-' + hashlib.sha256(key.encode()).hexdigest()[:16]
        papers.setdefault(key, downloader.Paper(year, title, source, file_urls(base, links), category, len(papers)))

    for panel in soup.select('.panel-session'):
        heading = panel.select_one('h4.panel-title')
        text = heading.get_text(' ', strip=True) if heading else ''
        match = re.match(r'Session\s+\d+[A-Z]?\s*:\s*(.+)', text, re.I)
        if not match:  # Exclude keynotes, awards, workshops, and social events.
            continue
        for node in panel.select('.paper'):
            title = node.select_one('.paper-title')
            if title:
                add(title.get_text(' ', strip=True), node.select('.paper-links a[href]'), match[1])

    for table in soup.select('table'):
        heading = table.select_one('thead th')
        text = heading.get_text(' ', strip=True) if heading else ''
        match = re.match(r'(?:Session\s+)?\d+[A-Z]\s*:\s*(.+)', text, re.I)
        if not match:
            continue
        category = re.split(r'\s*\(Location:|\s*Session Chair:', match[1], maxsplit=1, flags=re.I)[0].strip()
        for cell in table.select('tbody td'):
            title = cell.select_one('strong')
            if title:
                add(title.get_text(' ', strip=True), cell.select('a[href]'), category)
    return list(papers.values())


def read_program(client, year):
    base, soup = client.page(program_url(year))
    path = urlsplit(base).path
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    title_year = re.search(r'ASPLOS\s+(\d{4})', title, re.I)
    if (not path.startswith(f'/asplos{year}/') or
            (title_year and int(title_year[1]) != year)):
        raise downloader.DownloadError(f'ASPLOS {year} program redirected to a different conference year: {base}')
    papers = parse_program(year, base, soup)
    if not papers:
        raise downloader.DownloadError('No ASPLOS research papers found at ' + base)
    return base, papers


def crossref(client, url):
    try:
        message = client.get(url, timeout=25, retries=1, delay=1).json()['message']
        if not isinstance(message, dict):
            raise ValueError('Expected a metadata object')
        return message
    except (KeyError, TypeError, ValueError) as exc:
        raise downloader.DownloadError('Invalid Crossref proceedings response: ' + url) from exc


def enrich_links(client, year, papers):
    """Fill missing links by exact title and proceedings DOI; keep catalog identity."""
    parents = LINK_PROCEEDINGS.get(year)
    if not parents or all(p.file_urls for p in papers):
        return
    query = urlencode({
        'query.container-title': 'Proceedings of the 31st ACM International Conference on Architectural Support for Programming Languages and Operating Systems',
        'filter': f'prefix:10.1145,type:proceedings-article,from-pub-date:{year - 1}-01-01,until-pub-date:{year}-12-31',
        'rows': 1000, 'select': 'DOI,title',
    })
    try:
        data = crossref(client, 'https://api.crossref.org/works?' + query)
        matches = {}
        for item in data.get('items', []):
            doi = item.get('DOI', '').lower()
            if not doi.startswith(tuple('10.1145/' + parent + '.' for parent in parents)):
                continue
            title = BeautifulSoup((item.get('title') or [''])[0], 'html.parser').get_text(' ', strip=True)
            matches.setdefault(normalized_title(title), set()).add(doi)
        for paper in papers:
            candidates = matches.get(normalized_title(paper.title), set())
            if not paper.file_urls and len(candidates) == 1:
                paper.file_urls = ('https://dl.acm.org/doi/pdf/' + next(iter(candidates)),)
    except (downloader.DownloadError, TypeError, AttributeError) as exc:
        # Link lookup is optional: the official program remains usable, and
        # individual downloads can still look for an author/repository copy.
        print('ASPLOS proceedings link lookup unavailable: ' + str(exc), flush=True)


def discover_proceedings(client, year):
    doi = '10.1145/' + PROCEEDINGS[year]
    parent = crossref(client, 'https://api.crossref.org/works/' + doi)
    proceedings_title = (parent.get('title') or [''])[0]
    if parent.get('DOI', '').lower() != doi or not proceedings_title:
        raise downloader.DownloadError('Crossref returned a different ASPLOS proceedings record.')
    records, cursor, seen_cursors = {}, '*', set()
    while True:
        query = urlencode({'filter': 'container-title:' + proceedings_title + ',type:proceedings-article',
                           'rows': 1000, 'cursor': cursor, 'select': 'DOI,title,page'})
        message = crossref(client, 'https://api.crossref.org/works?' + query)
        items = message.get('items')
        if not isinstance(items, list):
            raise downloader.DownloadError('Crossref did not return an ASPLOS paper list.')
        for item in items:
            identifier = item.get('DOI', '').lower()
            title = BeautifulSoup((item.get('title') or [''])[0], 'html.parser').get_text(' ', strip=True)
            if not identifier.startswith(doi + '.') or not title:
                continue
            if re.match(r'^(?:front matter|back matter|preface|foreword|keynote|panel(?: discussion)?)\b', title, re.I):
                continue
            page = re.match(r'\d+', item.get('page') or '')
            records[identifier] = (int(page[0]) if page else 10**9, title)
        if len(items) < 1000:
            break
        seen_cursors.add(cursor)
        cursor = message.get('next-cursor')
        if not cursor or cursor in seen_cursors:
            raise downloader.DownloadError('Crossref returned an incomplete ASPLOS paper list.')
    papers = [downloader.Paper(year, title, 'https://dl.acm.org/doi/' + identifier,
                              ('https://dl.acm.org/doi/pdf/' + identifier,), program_order=i)
              for i, (identifier, (_, title)) in enumerate(sorted(records.items(), key=lambda r: (r[1][0], r[0])))]
    if not papers:
        raise downloader.DownloadError(f'No research papers found in ASPLOS {year} proceedings.')
    return 'https://dl.acm.org/doi/proceedings/' + doi, papers


def discover(client, year):
    if year not in YEARS:
        raise ValueError('Choose a valid ASPLOS year.')
    if year >= 2022:
        try:
            base, papers = read_program(client, year)
            enrich_links(client, year, papers)
            return base, papers
        except downloader.DownloadError:
            if year not in PROCEEDINGS:
                raise
    return discover_proceedings(client, year)
