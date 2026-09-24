"""SOSP paper lists from the official SIGOPS archive and conference programs."""
from datetime import date
import hashlib
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import download_osdi as downloader


YEARS = [*range(1967, 2024, 2), *range(2024, date.today().year + 1)]
BASE = 'https://www.sigops.org/s/conferences/sosp/'
ARCHIVE = BASE + '2015/archive/index.html'


def edition_url(year):
    return BASE + str(year) + '/'


def paper_url(base, href):
    url = urljoin(base, href)
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        return ''
    if parts.hostname == 'dl.acm.org':
        params = parse_qs(parts.query)
        doi = params.get('doid', [''])[0]
        if parts.path == '/citation.cfm' and doi:
            return 'https://dl.acm.org/doi/10.1145/' + doi
        url = parts._replace(scheme='https').geturl()
    return url


def downloads(base, links):
    """Keep paper PDFs and ACM full-text links, excluding slides and videos."""
    direct, acm = [], []
    for link in links:
        url = paper_url(base, link.get('href', ''))
        if not url:
            continue
        label = link.get_text(' ', strip=True).lower()
        parts = urlsplit(url)
        if (link.select_one('img.icon-slide, img.icon-video') or
                re.search(r'/(?:slides?|videos?)/|[-_]slides\.|frontmatter|backmatter', parts.path, re.I) or
                label in ('slides', 'video', 'cover', 'front matter', 'back matter')):
            continue
        if parts.path.lower().endswith('.pdf'):
            direct.append(url)
        elif parts.hostname == 'dl.acm.org':
            if re.fullmatch(r'/doi/10\.\d+/[^/]+', parts.path):
                acm.append('https://dl.acm.org/doi/pdf/' + parts.path.removeprefix('/doi/'))
            elif parts.path.startswith('/doi/pdf/') or parts.path in ('/authorize', '/ft_gateway.cfm'):
                acm.append(url)
    return tuple(dict.fromkeys(direct + acm))


def category(text):
    text = downloader.clean_text(text)
    text = re.sub(r'^.*?Session\s+\d+[A-Z]?\s*[:–-]\s*', '', text, flags=re.I)
    return re.sub(r'\s*\([^()]*\)\s*$', '', text).strip()


def parse_archive(year, base, soup):
    papers, session = [], ''
    for node in soup.select('.conferenceTitle, .sessionTitle, .paper'):
        if 'conferenceTitle' in node.get('class', []):
            session = ''
        elif 'sessionTitle' in node.get('class', []):
            session = downloader.clean_text(node.get_text(' ', strip=True))
        else:
            marker, title = node.select_one('.marker'), node.select_one('.paperTitle')
            # The archive supplies abstracts for research papers, but not
            # introductions, keynotes, front matter, or panel discussions.
            abstract = node.select_one('.paperCopy a[href*="abstracts.html#"]')
            if not marker or marker.get_text(strip=True) != str(year) or not title or not abstract:
                continue
            links = node.select('.paperCopy a[href]')
            citation = next((a for a in links if 'citation.cfm' in a['href']), abstract)
            urls = downloads(base, links)
            papers.append(downloader.Paper(year, downloader.clean_text(title.get_text(' ', strip=True)),
                          paper_url(base, citation['href']), urls, session, len(papers)))
    return papers


def parse_toc(year, base, soup):
    papers, session = [], ''
    for node in soup.select('#DLcontent > h2, #DLcontent > h3'):
        if node.name == 'h2':
            session = re.sub(r'^SESSION:\s*', '', node.get_text(' ', strip=True), flags=re.I)
        else:
            link = node.select_one('a.DLtitleLink[href]')
            if link:
                papers.append(downloader.Paper(year, downloader.clean_text(link.get_text(' ', strip=True)),
                              paper_url(base, link['href']), downloads(base, [link]), session, len(papers)))
    return papers


def parse_schedule(year, base, soup):
    papers, session = [], ''
    for node in soup.select('h4.sch, tr'):
        if node.name == 'h4':
            session = category(' '.join(node.find_all(string=True, recursive=False)))
            continue
        if 'info' in node.get('class', []):
            heading = node.select_one('strong')
            session = category(heading.get_text(' ', strip=True)) if heading else ''
            continue
        title = node.select_one('td > b > a[href], td > strong > a[href], td > a[href]:has(strong)')
        if not title or not downloads(base, [title]):
            continue
        # A paper icon can link to an alternate full text. Other icons are
        # slides/video even when the publisher endpoint has no extension.
        links = [title] + [a for a in node.select('a[href]') if a.select_one('img.icon-paper')]
        papers.append(downloader.Paper(year, downloader.clean_text(title.get_text(' ', strip=True)),
                      paper_url(base, title['href']), downloads(base, links), session, len(papers)))
    # The 2026 program also lists accepted papers before full texts are posted.
    for node in soup.select('ul.papers > li'):
        title_parts = []
        for child in node.contents:
            if getattr(child, 'name', None) in ('br', 'em'):
                break
            title_parts.append(child.get_text(' ', strip=True) if hasattr(child, 'get_text') else str(child))
        title = downloader.clean_text(' '.join(title_parts))
        if not title:
            continue
        heading = node.find_parent('td').select_one('.session-title')
        session = category(heading.get_text(' ', strip=True)) if heading else ''
        links = node.select('a[href]')
        urls = downloads(base, links)
        source = base.split('#', 1)[0] + '#paper-' + hashlib.sha256(title.encode()).hexdigest()[:16]
        papers.append(downloader.Paper(year, title, source, urls, session, len(papers)))
    return papers


def discover(client, year):
    if year not in YEARS:
        raise ValueError('Choose a valid SOSP year.')
    if year <= 2015:
        # An all-years Codex scope can reuse this single historical index.
        cached = client.__dict__.get('_sosp_archive')
        if cached is None:
            cached = client.page(ARCHIVE)
            client._sosp_archive = cached
        base, soup = cached
        papers = parse_archive(year, base, soup)
    else:
        page = 'program.html' if year in (2017, 2019) else 'toc.html' if year in (2021, 2023) else 'schedule.html'
        base, soup = client.page(edition_url(year) + page)
        papers = parse_toc(year, base, soup) if page == 'toc.html' else parse_schedule(year, base, soup)
    papers = list({downloader.paper_key(vars(p)): p for p in papers}.values())
    if not papers:
        raise downloader.DownloadError('No SOSP research papers found; the program may not be published: ' + base)
    return base, papers
