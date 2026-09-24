import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
import pymupdf

import download_osdi as downloader
import projects
import public_copies
import sosp
import web_app as web


def soup(html):
    return BeautifulSoup(html, 'html.parser')


class SospTests(unittest.TestCase):
    def test_years_and_conference_urls(self):
        years = projects.CONFERENCES['sosp'][1]
        self.assertEqual(years[:2], [1967, 1969])
        self.assertIn(2023, years)
        self.assertIn(2024, years)
        self.assertNotIn(2022, years)
        self.assertIn('/sosp/2024/', projects.conference_url('sosp', 2024))
        with self.assertRaises(ValueError):
            projects.discover_conference(Mock(), 'sosp', 2022)

    def test_archive_filters_year_front_matter_and_keynotes(self):
        html = '''<div class="conferenceTitle">1969 SOSP</div>
        <div class="sessionTitle">Storage</div>
        <div class="paper"><div class="marker">1969</div><div class="paperTitle">Later paper</div>
          <div class="paperCopy"><a href="abstracts.html#later">ABSTRACT</a></div></div>
        <div class="conferenceTitle">1967 SOSP</div>
        <div class="paper"><div class="marker">1967</div><div class="paperTitle">Program</div>
          <div class="paperCopy"><a href="00-toc.pdf">PDF</a></div></div>
        <div class="sessionTitle">Memory</div>
        <div class="paper"><div class="marker">1967</div><div class="paperTitle">A research paper</div>
          <div class="paperCopy"><a href="abstracts.html#paper">ABSTRACT</a>
          <a href="http://dl.acm.org/citation.cfm?doid=800001.800002">ACM</a>
          <a href="1967/paper.pdf">PDF</a></div></div>
        <div class="paper"><div class="marker">1967</div><div class="paperTitle">Keynote</div>
          <div class="paperCopy"><a href="slides/keynote.pdf">SLIDES</a></div></div>'''
        client = Mock()
        client.page.return_value = (sosp.ARCHIVE, soup(html))
        _, papers = projects.discover_conference(client, 'sosp', 1967)
        self.assertEqual([p.title for p in papers], ['A research paper'])
        self.assertEqual(papers[0].category, 'Memory')
        self.assertEqual(papers[0].page_url, 'https://dl.acm.org/doi/10.1145/800001.800002')
        self.assertTrue(papers[0].file_urls[0].endswith('/1967/paper.pdf'))
        projects.discover_conference(client, 'sosp', 1969)
        client.page.assert_called_once_with(sosp.ARCHIVE)

    def test_toc_uses_doi_pdfs_and_categories(self):
        html = '''<div id="DLcontent"><h2>SESSION: Memory</h2>
        <h3><a class="DLtitleLink" href="https://dl.acm.org/doi/10.1145/100.101">A paper</a></h3>
        <div class="DLabstract"><h2>Abstract heading</h2></div>
        <h2>SESSION: Networking</h2><h3><a class="DLtitleLink" href="https://dl.acm.org/doi/10.1145/100.102">B paper</a></h3></div>'''
        client = Mock()
        client.page.return_value = (sosp.edition_url(2023) + 'toc.html', soup(html))
        _, papers = projects.discover_conference(client, 'sosp', 2023)
        self.assertEqual([p.category for p in papers], ['Memory', 'Networking'])
        self.assertEqual(papers[0].file_urls, ('https://dl.acm.org/doi/pdf/10.1145/100.101',))
        self.assertEqual(downloader.file_format(papers[0].file_urls[0]), '.pdf')

    def test_program_paper_icons_exclude_video_gateway_and_slides(self):
        html = '''<table><tr class="info"><td><strong>Session 1: Learning (Chair Name)</strong></td></tr>
        <tr><td><span><a href="https://dl.acm.org/ft_gateway.cfm?id=100&amp;ftid=200"><img class="icon-paper"></a>
        <a href="https://dl.acm.org/ft_gateway.cfm?id=100&amp;ftid=201"><img class="icon-video"></a>
        <a href="slides/paper.pdf"><img class="icon-slide"></a></span>
        <strong><a href="https://dl.acm.org/authorize?N123">Video Analysis</a></strong></td></tr>
        <tr><td><strong><a href="javascript:void(0)">Welcome</a></strong></td></tr></table>'''
        papers = sosp.parse_schedule(2019, sosp.edition_url(2019) + 'program.html', soup(html))
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].category, 'Learning')
        self.assertEqual(papers[0].title, 'Video Analysis')
        self.assertEqual(len(papers[0].file_urls), 2)
        self.assertNotIn('ftid=201', ' '.join(papers[0].file_urls))

    def test_recent_schedule_links_and_order(self):
        html = '''<table><tr><h4 class="sch">Welcome and Session 1: Distributed Systems
        <span class="sch-time">8:30–10:30</span><span>Session Chair: Someone</span></h4></tr>
        <tr><td><b><a href="assets/papers/first.pdf">First paper</a></b><em>Authors</em></td></tr>
        <tr><td><a href="https://dl.acm.org/doi/10.1145/123.456"><strong>Video Systems</strong></a><em>Authors</em></td></tr></table>'''
        papers = sosp.parse_schedule(2024, sosp.edition_url(2024) + 'schedule.html', soup(html))
        self.assertEqual([p.program_order for p in papers], [0, 1])
        self.assertEqual({p.category for p in papers}, {'Distributed Systems'})
        self.assertIn('/2024/assets/papers/first.pdf', papers[0].file_urls[0])
        self.assertEqual(papers[1].file_urls, ('https://dl.acm.org/doi/pdf/10.1145/123.456',))

    def test_unpublished_fulltexts_still_have_distinct_catalog_entries(self):
        html = '''<table><tr><td><div class="session-title"><strong>Session 1A</strong> – <a href="#">Model Serving</a></div>
        <ul class="papers"><li>First paper<br><em>First authors</em></li>
        <li>Second paper<br><em>Second authors</em></li></ul></td></tr></table>'''
        papers = sosp.parse_schedule(2026, sosp.edition_url(2026) + 'schedule.html', soup(html))
        self.assertEqual([p.title for p in papers], ['First paper', 'Second paper'])
        self.assertEqual({p.category for p in papers}, {'Model Serving'})
        self.assertNotEqual(downloader.paper_key(vars(papers[0])), downloader.paper_key(vars(papers[1])))
        self.assertTrue(all(not p.file_urls for p in papers))

    def test_catalog_download_reader_and_codex_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = web.create_app(root)
            self.addCleanup(app.config['CODEX'].close)
            client = app.test_client()
            library = web.Library(root, 'sosp')
            html = '<table><tr><td><b><a href="assets/papers/paper.pdf">A real PDF</a></b></td></tr></table>'
            remote = Mock()
            remote.page.return_value = (sosp.edition_url(2024) + 'schedule.html', soup(html))
            web.sync_year(library, 2024, remote, catalog_only=True)
            catalog = client.get('/api/library?project=sosp').get_json()
            self.assertEqual(len(catalog['papers']), 1)
            self.assertFalse(catalog['papers'][0]['downloaded'])
            self.assertTrue(catalog['year_status']['2024']['categories_loaded'])
            with pymupdf.open() as document:
                document.new_page().insert_text((40, 40), 'SOSP fixture')
                pdf = document.tobytes()
            remote.get.return_value = Mock(content=pdf)
            ident = catalog['papers'][0]['id']
            web.download_one(library, library.get(ident), remote)
            detail = client.get('/api/papers/' + ident + '?project=sosp').get_json()
            self.assertTrue(detail['downloaded'])
            self.assertEqual(detail['viewer_type'], 'pdf')
            self.assertTrue((root / 'conferences/sosp/papers/2024/manifest.json').exists())
            preview = app.config['CODEX'].store.preview({'conferences': [{'id': 'sosp', 'years': [2024]}]})
            self.assertEqual(preview['downloaded'], 1)
            self.assertEqual(preview['total'], 1)
            self.assertNotIn('usenix.org', json.loads((library.root / '.catalog/2024.json').read_text())['edition_url'])

    def test_extensionless_pdf_endpoint_and_html_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            paper = downloader.Paper(2019, 'Paper', 'https://dl.acm.org/authorize?N123', ('https://dl.acm.org/authorize?N123',))
            client.get.return_value = Mock(content=b'%PDF-1.4\nTest\n%%EOF\n')
            record = downloader.retrieve_paper(client, paper, Path(directory), {})
            self.assertEqual(record['status'], 'downloaded')
            self.assertTrue(record['path'].endswith('.pdf'))
            client.get.return_value = Mock(content=b'<html>Publisher login</html>')
            record = downloader.retrieve_paper(client, paper, Path(directory), {})
            self.assertEqual(record['status'], 'failed')
            self.assertNotIn('path', record)

    def test_unavailable_paper_can_be_retried_when_program_adds_pdf(self):
        title = 'A newly accepted research paper'
        base = sosp.edition_url(2026) + 'schedule.html'
        before = soup(f'<table><tr><td><ul class="papers"><li>{title}<br><em>Authors</em></li></ul></td></tr></table>')
        after = soup(f'''<table><tr><td><ul class="papers">
            <li><a href="assets/papers/other.pdf">A different paper</a><br><em>Other authors</em></li>
            <li><a href="assets/papers/target.pdf">{title}</a><br><em>Authors</em></li>
            </ul></td></tr></table>''')
        paper = sosp.parse_schedule(2026, base, before)[0]
        fresh = sosp.parse_schedule(2026, base, after)[1]
        self.assertEqual(downloader.paper_key(vars(paper)), downloader.paper_key(vars(fresh)))
        remote = Mock(cancelled=threading.Event())
        remote.page.return_value = (base, before)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = web.create_app(root)
            self.addCleanup(app.config['CODEX'].close)
            library = web.Library(root, 'sosp')
            manifest = library.papers / '2026' / 'manifest.json'
            downloader.save_manifest(manifest, {'year': 2026, 'papers': [vars(paper)]})
            row = library.catalog()[0]
            with patch.object(public_copies, 'candidates', return_value=iter(())):
                with self.assertRaisesRegex(web.PaperUnavailable, 'no PDF link'):
                    web.download_one(library, row, remote)
            client = app.test_client()
            detail = client.get('/api/papers/' + row['id'] + '?project=sosp').get_json()
            self.assertTrue(detail['pdf_unavailable'])
            self.assertFalse(detail['downloaded'])
            remote.get.assert_not_called()

            remote.page.return_value = (base, after)
            remote.get.return_value = Mock(content=b'%PDF-1.4\nTest\n%%EOF\n')
            web.download_one(library, library.get(row['id']), remote)
            remote.get.assert_called_once_with(sosp.edition_url(2026) + 'assets/papers/target.pdf')
            detail = client.get('/api/papers/' + row['id'] + '?project=sosp').get_json()
            self.assertTrue(detail['downloaded'])
            self.assertFalse(detail['pdf_unavailable'])
            self.assertEqual(detail['viewer_type'], 'pdf')
            self.assertIsNone(detail['error'])

    def test_unlinked_paper_does_not_download_another_papers_pdf(self):
        base = sosp.edition_url(2026) + 'schedule.html'
        html = soup('''<table><tr><td><ul class="papers">
            <li>The paper without a link<br><em>Authors</em></li>
            <li><a href="other.pdf">Another paper</a><br><em>Authors</em></li>
            </ul></td></tr></table>''')
        paper = sosp.parse_schedule(2026, base, html)[0]
        remote = Mock(cancelled=threading.Event())
        remote.page.return_value = (base, html)
        with tempfile.TemporaryDirectory() as directory, patch.object(public_copies, 'candidates', return_value=iter(())):
            result = downloader.retrieve_paper(remote, paper, Path(directory), {})
            self.assertEqual(result['reason'], 'no_public_copy')
            self.assertEqual(list(Path(directory).iterdir()), [])
            remote.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
