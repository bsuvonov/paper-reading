from dataclasses import asdict
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
import pymupdf

import asplos
import download_osdi as downloader
import projects
import public_copies
import web_app as web


def soup(html):
    return BeautifulSoup(html, 'html.parser')


def panel(title='A paper about systems', href=''):
    return f'''<div class="panel-session"><h4 class="panel-title">Session 1A: Memory Systems</h4>
      <div class="paper"><div class="paper-title">{title}</div>
      <div class="paper-links">{f'<a href="{href}">Paper</a>' if href else ''}</div></div></div>'''


class AsplosTests(unittest.TestCase):
    def test_registry_includes_irregular_editions_and_conference_urls(self):
        self.assertEqual(projects.CONFERENCES['asplos'][1][:5], [1982, 1987, 1989, 1991, 1992])
        self.assertNotIn(2007, asplos.YEARS)
        self.assertIn('asplos2025', projects.conference_url('asplos', 2025))
        self.assertIn('10.1145/2150976', projects.conference_url('asplos', 2012))
        with self.assertRaises(ValueError):
            projects.discover_conference(Mock(), 'asplos', 2007)

    def test_panels_keep_unlinked_papers_and_filter_keynotes_and_slides(self):
        html = panel('First paper', 'https://doi.org/10.1145/100.101') + panel('Second paper')
        html += panel('First paper', 'https://doi.org/10.1145/100.101')
        html += panel('Invited keynote').replace('Session 1A: Memory Systems', 'Keynote')
        papers = asplos.parse_program(2025, asplos.program_url(2025), soup(html))
        self.assertEqual([p.title for p in papers], ['First paper', 'Second paper'])
        self.assertEqual([p.category for p in papers], ['Memory Systems', 'Memory Systems'])
        self.assertEqual(papers[0].file_urls, ('https://dl.acm.org/doi/pdf/10.1145/100.101',))
        self.assertEqual(papers[1].file_urls, ())
        links = soup('<a href="paper-slides.pdf">Paper slides</a><a href="video.mp4">Video</a><a href="paper.pdf">Paper</a>')
        self.assertEqual(asplos.file_urls('https://example.org/', links.select('a')), ('https://example.org/paper.pdf',))

    def test_tables_use_only_paper_titles_and_session_categories(self):
        html = '''<table><thead><tr><th><span>1A: Compilers</span><br>(Location: Hall)<br>Session Chair: Someone</th></tr></thead>
          <tbody><tr><td><strong>A compiler paper</strong><br>Authors
            <a href="https://doi.org/10.1145/100.102">Paper</a><a href="slides.pdf">Slides</a></td></tr></tbody></table>
          <table><thead><tr><th>Keynote</th></tr></thead><tbody><tr><td><strong>Invited talk</strong></td></tr></tbody></table>'''
        papers = asplos.parse_program(2024, asplos.program_url(2024), soup(html))
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, 'A compiler paper')
        self.assertEqual(papers[0].category, 'Compilers')
        self.assertEqual(papers[0].file_urls, ('https://dl.acm.org/doi/pdf/10.1145/100.102',))

    def test_program_redirect_cannot_silently_replace_selected_year(self):
        remote = Mock()
        remote.page.return_value = (asplos.program_url(2026), soup('<title>ASPLOS 2026</title>' + panel()))
        with self.assertRaisesRegex(downloader.DownloadError, 'different conference year'):
            asplos.discover(remote, 2025)

    def test_historical_proceedings_exclude_foreign_records_and_front_matter(self):
        remote = Mock()
        remote.get.side_effect = [
            Mock(json=lambda: {'message': {'DOI': '10.1145/2150976', 'title': ['ASPLOS proceedings']}}),
            Mock(json=lambda: {'message': {'items': [
                {'DOI': '10.1145/2150976.2', 'title': ['Later research'], 'page': '20-29'},
                {'DOI': '10.1145/2150976.1', 'title': ['First <i>research</i>'], 'page': '1-10'},
                {'DOI': '10.1145/2150976.3', 'title': ['Preface'], 'page': 'i'},
                {'DOI': '10.1145/9999999.1', 'title': ['Different conference'], 'page': '0'},
            ]}}),
        ]
        _, papers = projects.discover_conference(remote, 'asplos', 2012)
        self.assertEqual([p.title for p in papers], ['First research', 'Later research'])
        self.assertEqual([p.program_order for p in papers], [0, 1])
        self.assertEqual(papers[0].file_urls, ('https://dl.acm.org/doi/pdf/10.1145/2150976.1',))

    def test_metadata_pagination_keeps_proceedings_title(self):
        remote = Mock()
        item = {'DOI': '10.1145/2150976.1', 'title': ['First paper']}
        remote.get.side_effect = [
            Mock(json=lambda: {'message': {'DOI': '10.1145/2150976', 'title': ['ASPLOS proceedings']}}),
            Mock(json=lambda: {'message': {'items': [item] * 1000, 'next-cursor': 'next'}}),
            Mock(json=lambda: {'message': {'items': [{'DOI': '10.1145/2150976.2', 'title': ['Last paper']}]}}),
        ]
        _, papers = asplos.discover_proceedings(remote, 2012)
        self.assertEqual(len(papers), 2)
        params = parse_qs(urlsplit(remote.get.call_args.args[0]).query)
        self.assertEqual(params['cursor'], ['next'])
        self.assertEqual(params['filter'], ['container-title:ASPLOS proceedings,type:proceedings-article'])

    def test_unlinked_paper_refreshes_only_its_own_links_and_keeps_identity(self):
        base = asplos.program_url(2026)
        old = asplos.parse_program(2026, base, soup(panel()))[0]
        fresh_html = panel('Other paper', 'other.pdf') + panel(href='target.pdf')
        fresh = asplos.parse_program(2026, base, soup(fresh_html))[1]
        self.assertEqual(downloader.paper_key(asdict(old)), downloader.paper_key(asdict(fresh)))
        self.assertTrue(public_copies.eligible(old))
        remote = Mock(cancelled=threading.Event())
        remote.page.return_value = (base, soup(fresh_html))
        remote.get.return_value = Mock(content=b'%PDF-1.4\nTest\n%%EOF\n')
        with tempfile.TemporaryDirectory() as directory:
            record = downloader.retrieve_paper(remote, old, Path(directory), {})
            self.assertEqual(record['status'], 'downloaded')
        remote.get.assert_called_once_with(base + 'target.pdf')

    def test_link_lookup_matches_exact_title_and_proceedings_and_keeps_identity(self):
        base = asplos.program_url(2026)
        papers = asplos.parse_program(2026, base, soup(panel('Matching paper') + panel('Unrelated paper') + panel('Ambiguous paper')))
        keys = [downloader.paper_key(asdict(p)) for p in papers]
        remote = Mock()
        remote.get.return_value = Mock(json=lambda: {'message': {'items': [
            {'DOI': '10.1145/3779212.111', 'title': ['Matching paper']},
            {'DOI': '10.1145/3779212.222', 'title': ['Unrelated paper extended']},
            {'DOI': '10.1145/9999999.333', 'title': ['Unrelated paper']},
            {'DOI': '10.1145/3779212.444', 'title': ['Ambiguous paper']},
            {'DOI': '10.1145/3760250.555', 'title': ['Ambiguous paper']},
        ]}})
        asplos.enrich_links(remote, 2026, papers)
        self.assertEqual(papers[0].file_urls, ('https://dl.acm.org/doi/pdf/10.1145/3779212.111',))
        self.assertFalse(papers[1].file_urls)
        self.assertFalse(papers[2].file_urls)
        self.assertEqual([downloader.paper_key(asdict(p)) for p in papers], keys)

    def test_link_lookup_failure_retains_program_titles(self):
        remote = Mock()
        remote.page.return_value = (asplos.program_url(2026), soup(panel()))
        remote.get.side_effect = downloader.DownloadError('Metadata unavailable')
        _, papers = asplos.discover(remote, 2026)
        self.assertEqual([p.title for p in papers], ['A paper about systems'])
        self.assertFalse(papers[0].file_urls)

    def test_catalog_download_reader_and_codex_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = web.create_app(root)
            self.addCleanup(app.config['CODEX'].close)
            library = web.Library(root, 'asplos')
            remote = Mock(cancelled=threading.Event())
            remote.page.return_value = (asplos.program_url(2025), soup(panel(href='paper.pdf')))
            web.sync_year(library, 2025, remote, catalog_only=True)
            client = app.test_client()
            catalog = client.get('/api/library?project=asplos').get_json()
            paper = catalog['papers'][0]
            self.assertFalse(paper['downloaded'])
            self.assertEqual(paper['category'], 'Memory Systems')
            with pymupdf.open() as doc:
                doc.new_page().insert_text((40, 40), 'A paper about systems')
                remote.get.return_value = Mock(content=doc.tobytes())
            web.download_one(library, library.get(paper['id']), remote)
            detail = client.get('/api/papers/' + paper['id'] + '?project=asplos').get_json()
            self.assertTrue(detail['downloaded'])
            self.assertEqual(detail['viewer_type'], 'pdf')
            self.assertIn('/project-files/asplos/', detail['viewer_url'])
            scope = app.config['CODEX'].store.preview({'conferences': [{'id': 'asplos', 'years': [2025]}]})
            self.assertEqual(scope['total'], 1)
            self.assertEqual(scope['downloaded'], 1)


if __name__ == '__main__':
    unittest.main()
