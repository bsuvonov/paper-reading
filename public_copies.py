"""Find public research-paper copies without bypassing publisher access checks."""
import re
import unicodedata
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
import xml.etree.ElementTree as ET

import pymupdf

from download_osdi import DownloadError


def normalized_title(title):
    return ''.join(c for c in unicodedata.normalize('NFKD', title).casefold() if c.isalnum())


def doi_for(paper):
    for url in (paper.page_url, *paper.file_urls):
        parts = urlsplit(url)
        match = re.search(r'10\.\d{4,9}/[^?#]+', unquote(parts.path))
        if match:
            return match[0].lower()
        if parts.hostname == 'dl.acm.org':
            value = parse_qs(parts.query).get('doid', [''])[0]
            if re.fullmatch(r'\d+\.\d+', value):
                return '10.1145/' + value
    return None


def eligible(paper):
    # Conference sites can also withdraw their previously public PDF copies.
    return any(urlsplit(url).hostname in ('dl.acm.org', 'sigops.org', 'www.sigops.org')
               for url in (paper.page_url, *paper.file_urls))


def public_url(value):
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        return None
    # These are the publisher endpoints that have already failed.
    if parts.hostname in ('doi.org', 'dx.doi.org', 'dl.acm.org'):
        return None
    return value


def arxiv_pdf(value):
    if isinstance(value, str) and re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?', value):
        return 'https://arxiv.org/pdf/' + value + '.pdf'
    return None


def metadata(client, url, *, arxiv=False):
    return client.get(url, timeout=12, retries=0, delay=3 if arxiv else 1)


def candidates(client, paper):
    """Yield verified metadata matches lazily; stop querying once a copy works."""
    doi = doi_for(paper)
    title = normalized_title(paper.title)
    seen = set(paper.file_urls)

    def candidate(url, provider, version='author/repository copy', source=None):
        url = public_url(url)
        if not url or url in seen:
            return None
        seen.add(url)
        return dict(url=url, provider=provider, version=version, source=source or url)

    if doi:
        try:
            data = metadata(client, 'https://api.semanticscholar.org/graph/v1/paper/DOI:' +
                            quote(doi, safe='/') + '?fields=title,externalIds,openAccessPdf').json()
            ids = data.get('externalIds') or {}
            if (normalized_title(data.get('title') or '') == title and
                    (ids.get('DOI') or '').lower() == doi):
                url = arxiv_pdf(ids.get('ArXiv'))
                if url and (item := candidate(url, 'Semantic Scholar / arXiv', 'preprint',
                                              url.replace('/pdf/', '/abs/').removesuffix('.pdf'))):
                    yield item
                url = (data.get('openAccessPdf') or {}).get('url')
                if item := candidate(url, 'Semantic Scholar'):
                    yield item
        except (DownloadError, ValueError, TypeError, AttributeError) as exc:
            print('Semantic Scholar lookup unavailable: ' + str(exc), flush=True)
        try:
            data = metadata(client, 'https://api.openalex.org/works/https://doi.org/' + quote(doi, safe='/')).json()
            if ((data.get('doi') or '').lower().removeprefix('https://doi.org/') == doi and
                    normalized_title(data.get('title') or '') == title):
                for location in data.get('locations') or []:
                    if not location.get('is_oa'):
                        continue
                    url = location.get('pdf_url')
                    if not url:
                        landing = location.get('landing_page_url') or ''
                        parts = urlsplit(landing)
                        if parts.hostname in ('arxiv.org', 'export.arxiv.org') and parts.path.startswith('/abs/'):
                            url = arxiv_pdf(parts.path.removeprefix('/abs/'))
                    if item := candidate(url, 'OpenAlex', location.get('version') or 'author/repository copy',
                                         location.get('landing_page_url')):
                        yield item
        except (DownloadError, ValueError, TypeError, AttributeError) as exc:
            print('OpenAlex lookup unavailable: ' + str(exc), flush=True)
    # Useful for old ACM authorize links and conference PDFs without a DOI.
    # Exact title equality avoids silently selecting a merely related paper.
    if len(paper.title.split()) < 4:
        return
    try:
        query = urlencode({'search_query': 'ti:"' + paper.title.replace('"', ' ') + '"', 'max_results': 5})
        root = ET.fromstring(metadata(client, 'https://export.arxiv.org/api/query?' + query, arxiv=True).content)
        ns = {'a': 'http://www.w3.org/2005/Atom'}
        for entry in root.findall('a:entry', ns):
            if normalized_title(entry.findtext('a:title', '', ns)) != title:
                continue
            source = entry.findtext('a:id', '', ns)
            parts = urlsplit(source)
            if parts.hostname not in ('arxiv.org', 'export.arxiv.org') or not parts.path.startswith('/abs/'):
                continue
            url = arxiv_pdf(parts.path.removeprefix('/abs/'))
            if item := candidate(url, 'arXiv', 'preprint', source.replace('http:', 'https:')):
                yield item
    except (DownloadError, ValueError, ET.ParseError) as exc:
        print('arXiv lookup unavailable: ' + str(exc), flush=True)


def validate_title(content, title):
    """Check the retrieved PDF itself, not only the search provider's metadata."""
    try:
        with pymupdf.open(stream=content, filetype='pdf') as document:
            text = '\n'.join(document[i].get_text() for i in range(min(2, len(document))))
    except (RuntimeError, ValueError) as exc:
        raise DownloadError('The public copy is not a readable PDF.') from exc
    if normalized_title(title) not in normalized_title(text):
        raise DownloadError('The public PDF does not contain the expected paper title on its opening pages.')
