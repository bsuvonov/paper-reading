import contextlib
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

import download_osdi as d


PDF = b"%PDF-1.4\nTest document\n%%EOF\n"


def soup(html):
    return BeautifulSoup(html, "html.parser")


class ParserTests(unittest.TestCase):
    def test_editions_include_irregular_and_future_years(self):
        html = ''.join(f'<a href="/conference/osdi{suffix}">OSDI</a>'
                       for suffix in ("94", "96", "99", "2000", "26", "27"))
        html += '<a href="/conference/sosp26">Other conference</a>'
        self.assertEqual(list(d.parse_editions(soup(html), d.ARCHIVE_URL)),
                         [1994, 1996, 1999, 2000, 2026, 2027])

    def test_legacy_program_keeps_short_titles_excludes_navigation(self):
        html = '''<a href="index.html">Home</a><a href="sw-caveat.html">Software caveat</a>
            <dt><a href="ford.html">CPU Inheritance Scheduling</a>
            <dd>Authors<dt><a href="necula.html">Safe Kernel Extensions</a>
            <a href="wip.html">Works in progress</a>
            <a href="invited_talks/a/abstract.html">Invited talk</a>'''
        papers = d.parse_program(1996, 'https://www.usenix.org/legacy/osdi96/', soup(html))
        self.assertEqual([p.title for p in papers], ["CPU Inheritance Scheduling", "Safe Kernel Extensions"])

    def test_2002_program_uses_tech_subdirectory(self):
        papers = d.parse_program(2002, 'https://www.usenix.org/legacy/osdi02/tech.html',
                                soup('<b><a href="tech/adya.html">FARSITE</a></b>'))
        self.assertEqual(papers[0].page_url, 'https://www.usenix.org/legacy/osdi02/tech/adya.html')

    def test_direct_papers_exclude_proceedings_and_slides(self):
        for year, cls in ((2008, "techdesc"), (2010, "fullpaper1")):
            html = f'''<a href="full_papers/osdi_proceedings.pdf">Proceedings</a>
                <p class="{cls}"><b>Research title</b><br>Authors</p>
                <p><a href="full_papers/author.pdf">Full paper</a>
                <a href="slides/author.pdf">Slides</a></p>'''
            papers = d.parse_program(year, 'https://www.usenix.org/legacy/tech/', soup(html))
            self.assertEqual(len(papers), 1)
            self.assertEqual(papers[0].title, "Research title")
            self.assertTrue(papers[0].file_urls[0].endswith('/full_papers/author.pdf'))

    def test_modern_program_excludes_joint_conference_and_keynote(self):
        html = ''.join(f'<article class="node-paper"><h2><a href="{url}">{title}</a></h2></article>'
                       for url, title in (
                           ('/conference/osdi26/presentation/author', 'A paper'),
                           ('/conference/osdi26/presentation/author', 'A paper'),
                           ('/conference/atc26/presentation/author', 'ATC paper'),
                           ('/conference/osdi26/presentation/keynote', 'Keynote'),
                           ('/conference/osdi26/presentation/wed-keynote', 'Wednesday keynote')))
        papers = d.parse_program(2026, 'https://www.usenix.org/', soup(html))
        self.assertEqual([p.title for p in papers], ["A paper"])

    def test_modern_file_selection_uses_paper_metadata(self):
        html = '''<meta name="citation_pdf_url" content="/system/files/paper.pdf">
            <div class="field-name-field-presentation-pdf"><a href="/system/files/paper.pdf">PDF</a></div>
            <div class="field-name-field-paper-slides"><a href="/slides.pdf">Slides</a></div>
            <a href="/unrelated.pdf">Other</a>'''
        self.assertEqual(d.parse_file_urls('https://www.usenix.org/', soup(html), True),
                         ['https://www.usenix.org/system/files/paper.pdf'])

    def test_session_categories_preserve_program_order_and_ignore_paper_headings(self):
        html = '''<article class="node-session"><h2>KV Cache and Long Context</h2>
          <div class="node-paper"><h2><a href="/conference/osdi26/presentation/z">Z paper</a></h2>
            <div><h3>A misleading abstract heading</h3></div></div>
          <div class="node-paper"><h2><a href="/conference/osdi26/presentation/a">A paper</a></h2></div>
        </article><article class="node-session"><h2>Memory Tiering and CXL</h2>
          <div class="node-paper"><h2><a href="/conference/osdi26/presentation/m">Memory paper</a></h2></div>
        </article><div class="node-paper"><h2><a href="/conference/osdi26/presentation/u">Unclassified</a></h2></div>'''
        papers = d.parse_program(2026, 'https://www.usenix.org/', soup(html))
        self.assertEqual([p.category for p in papers],
                         ['KV Cache and Long Context', 'KV Cache and Long Context', 'Memory Tiering and CXL', ''])
        self.assertEqual([p.program_order for p in papers], [0, 1, 2, 3])
        self.assertEqual([p.title for p in papers[:2]], ['Z paper', 'A paper'])

    def test_legacy_categories_use_session_chair_and_heading(self):
        html = '''<b><font>Storage</font></b><br><i>Session Chair: A Person</i>
          <p><b><a href="first.html">First paper</a></b></p>
          <p><b><a href="second.html">Second paper</a></b></p>
          <b>Scheduling</b><br><i>Session Chair: Another Person</i>
          <p><b><a href="third.html">Third paper</a></b></p>'''
        papers = d.parse_program(2000, 'https://www.usenix.org/legacy/osdi2000/', soup(html))
        self.assertEqual([p.category for p in papers], ['Storage', 'Storage', 'Scheduling'])

    def test_legacy_categories_without_session_chairs(self):
        html = '''<h2>Tuesday</h2><h3>I/O</h3><a href="first.html">First paper</a>
          <h3>Resource Management</h3><a href="second.html">Second paper</a>
          <h2>Wednesday</h2><a href="third.html">Unclassified paper</a>'''
        papers = d.parse_program(1999, 'https://www.usenix.org/legacy/osdi99/', soup(html))
        self.assertEqual([p.category for p in papers], ['I/O', 'Resource Management', ''])

    def test_legacy_prefers_publisher_copy_and_filters_talk_slides(self):
        html = '''<a href="talk_slides/a/a.ps">Talk slides</a>
            <a href="https://author.example/full.ps">Full paper</a>
            <a href="full_papers/a.ps">POSTSCRIPT</a>
            <a href="full_papers/a/index.html">HTML</a>'''
        urls = d.parse_file_urls('https://www.usenix.org/legacy/osdi96/', soup(html), False)
        self.assertEqual(urls[0], 'https://www.usenix.org/legacy/osdi96/full_papers/a.ps')
        self.assertEqual(len(urls), 3)

    def test_canonicalizes_old_usenix_links(self):
        self.assertEqual(d.absolute_url('https://www.usenix.org/', 'http://static.usenix.org/events/osdi04/tech/a.pdf'),
                         'https://www.usenix.org/legacy/events/osdi04/tech/a.pdf')

    def test_readable_safe_collision_resistant_names(self):
        a = d.Paper(2026, '../Bad: name / ' + 'X' * 300, 'https://example/a')
        b = d.Paper(2026, a.title, 'https://example/b')
        self.assertNotEqual(d.paper_stem(a), d.paper_stem(b))
        self.assertFalse(d.paper_stem(a).startswith('2026 - '))
        self.assertNotIn('/', d.paper_stem(a))
        self.assertLess(len(d.paper_stem(a)), 200)


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hits = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.hits[self.path] = cls.hits.get(self.path, 0) + 1
                if self.path == '/retry.pdf' and cls.hits[self.path] == 1:
                    self.send_response(503)
                    self.send_header('Retry-After', '0')
                    self.end_headers()
                    return
                routes = {
                    '/paper.pdf': ('application/pdf', PDF),
                    '/retry.pdf': ('application/pdf', PDF),
                    '/invalid.pdf': ('text/html', b'<html>Not a PDF</html>'),
                    '/fallback.ps': ('application/postscript', b'%!PS-Adobe-3.0\nExample\n%%EOF'),
                    '/full/index.html': ('text/html', b'<html><body>Paper<a href="section.html#intro">Next</a><img src="figure.png"><a href="../outside.html">Outside</a></body></html>'),
                    '/full/section.html': ('text/html', b'<html><body id="intro">Full section<a href="index.html">Back</a></body></html>'),
                    '/full/figure.png': ('image/png', b'\x89PNG\r\n\x1a\nfigure'),
                }
                if self.path not in routes:
                    self.send_response(404)
                    self.end_headers()
                    return
                content_type, data = routes[self.path]
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.client = d.Client(timeout=2, retries=1, delay=0)

    def paper(self, *paths):
        return d.Paper(2026, 'A test paper', self.base + '/abstract', tuple(self.base + p for p in paths))

    def test_real_http_download_resume_and_corruption_repair(self):
        paper = self.paper('/paper.pdf')
        first = d.retrieve_paper(self.client, paper, self.directory, {})
        self.assertEqual(first['status'], 'downloaded')
        before = self.hits['/paper.pdf']
        second = d.retrieve_paper(self.client, paper, self.directory, first)
        self.assertEqual(second['status'], 'skipped')
        self.assertEqual(self.hits['/paper.pdf'], before)
        (self.directory / first['path']).write_bytes(b'corrupt')
        third = d.retrieve_paper(self.client, paper, self.directory, second)
        self.assertEqual(third['status'], 'downloaded')
        self.assertEqual((self.directory / third['path']).read_bytes(), PDF)
        self.assertFalse(list(self.directory.glob('*.part')))

    def test_invalid_pdf_is_not_saved_and_falls_back(self):
        result = d.retrieve_paper(self.client, self.paper('/invalid.pdf', '/fallback.ps'), self.directory, {})
        self.assertEqual(result['status'], 'downloaded')
        self.assertTrue(result['path'].endswith('.ps'))
        self.assertFalse(list(self.directory.glob('*.pdf')))

    def test_failed_download_is_recorded(self):
        result = d.retrieve_paper(self.client, self.paper('/missing.pdf'), self.directory, {})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('HTTP 404', result['error'])

    def test_transient_http_failure_is_retried(self):
        result = d.retrieve_paper(self.client, self.paper('/retry.pdf'), self.directory, {})
        self.assertEqual(result['status'], 'downloaded')
        self.assertEqual(self.hits['/retry.pdf'], 2)

    def test_html_bundle_downloads_sections_and_figures_with_local_links(self):
        result = d.retrieve_paper(self.client, self.paper('/full/index.html'), self.directory, {})
        self.assertEqual(result['status'], 'downloaded')
        self.assertEqual(len(result['files']), 3)
        page = (self.directory / result['path']).read_text()
        self.assertIn('href="section.html#intro"', page)
        self.assertIn('src="figure.png"', page)
        self.assertNotIn('/outside.html', self.hits)
        self.assertTrue(d.verified_previous(result, self.directory))
        (self.directory / result['files'][-1]['path']).unlink()
        self.assertFalse(d.verified_previous(result, self.directory))

    def test_file_validation(self):
        d.validate_document(PDF, '.pdf')
        d.validate_document(gzip.compress(b'%!PS-Adobe-3.0\npaper'), '.ps.gz')
        for data, ext in ((b'%PDF-1.4\ntruncated', '.pdf'), (b'<html>error</html>', '.ps'), (b'invalid', '.ps.gz')):
            with self.subTest(ext=ext), self.assertRaises(d.DownloadError):
                d.validate_document(data, ext)

    def test_cli_records_failure_and_returns_nonzero(self):
        paper = self.paper('/missing.pdf')
        with patch.object(d.Client, 'page', return_value=(d.ARCHIVE_URL, soup(''))), \
             patch.object(d, 'parse_editions', return_value={2026: self.base}), \
             patch.object(d, 'discover_papers', return_value=(self.base, [paper])), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = d.main(['--years', '2026', '--output', str(self.directory), '--delay', '0'])
        self.assertEqual(code, 1)
        manifest = json.loads((self.directory / '2026' / 'manifest.json').read_text())
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['papers'][0]['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
