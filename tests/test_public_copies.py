from dataclasses import asdict
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import pymupdf

import download_osdi as downloader
import public_copies as copies


TITLE = 'Efficient Memory Management for Large Language Model Serving with PagedAttention'
DOI = '10.1145/3600006.3613165'
PUBLISHER = 'https://dl.acm.org/doi/pdf/' + DOI
ARXIV = 'https://arxiv.org/pdf/2309.06180.pdf'


def pdf(title):
    with pymupdf.open() as document:
        document.new_page().insert_textbox(pymupdf.Rect(40, 40, 550, 250), title, fontsize=18)
        return document.tobytes()


class PublicCopyTests(unittest.TestCase):
    def setUp(self):
        self.paper = downloader.Paper(2023, TITLE, 'https://dl.acm.org/doi/' + DOI, (PUBLISHER,))
        self.client = Mock(cancelled=threading.Event())

    def test_blocked_publisher_downloads_verified_arxiv_copy_and_resumes(self):
        self.client.get.side_effect = [
            downloader.DownloadError('HTTP 403: ' + PUBLISHER),
            Mock(json=lambda: {'title': TITLE, 'externalIds': {'DOI': DOI, 'ArXiv': '2309.06180'}}),
            Mock(content=pdf(TITLE), url=ARXIV),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = downloader.retrieve_paper(self.client, self.paper, root, {})
            self.assertEqual(result['status'], 'downloaded')
            self.assertEqual(result['url'], ARXIV)
            self.assertEqual(result['download_source']['version'], 'preprint')
            self.assertEqual(downloader.paper_key(result), downloader.paper_key(asdict(self.paper)))
            self.assertEqual(result['path'], downloader.paper_stem(self.paper) + '.pdf')
            self.assertTrue(downloader.verified_previous(result, root))
            self.assertEqual(self.client.get.call_count, 3)  # No extra lookups once a copy works.
            self.client.get.reset_mock()
            resumed = downloader.retrieve_paper(self.client, self.paper, root, result)
            self.assertEqual(resumed['status'], 'skipped')
            self.client.get.assert_not_called()

    def test_metadata_must_match_both_doi_and_title(self):
        for title, doi in [(TITLE + ' revisited', DOI), (TITLE, '10.1145/111.222')]:
            with self.subTest(title=title, doi=doi):
                self.client.get.side_effect = [
                    Mock(json=lambda: {'title': title, 'externalIds': {'DOI': doi, 'ArXiv': '2309.06180'}}),
                    Mock(json=lambda: {'title': title, 'doi': 'https://doi.org/' + doi,
                                      'locations': [{'is_oa': True, 'pdf_url': 'https://author.example/paper.pdf'}]}),
                    Mock(content=b'<feed xmlns="http://www.w3.org/2005/Atom"/>'),
                ]
                self.assertEqual(list(copies.candidates(self.client, self.paper)), [])

    def test_arxiv_title_search_after_provider_failure_rejects_related_paper(self):
        self.client.get.side_effect = [
            downloader.DownloadError('HTTP 429'), downloader.DownloadError('Request timed out'),
            Mock(content=f'''<feed xmlns="http://www.w3.org/2005/Atom">
              <entry><title>{TITLE} revisited</title><id>https://arxiv.org/abs/2309.99999</id></entry>
              <entry><title>{TITLE}</title><id>http://arxiv.org/abs/2309.06180v1</id></entry>
            </feed>'''.encode()),
        ]
        found = list(copies.candidates(self.client, self.paper))
        self.assertEqual([item['url'] for item in found], [ARXIV.removesuffix('.pdf') + 'v1.pdf'])
        self.assertEqual(found[0]['version'], 'preprint')

    def test_openalex_filters_publisher_and_closed_locations(self):
        self.client.get.side_effect = [
            downloader.DownloadError('Unavailable'),
            Mock(json=lambda: {'title': TITLE, 'doi': 'https://doi.org/' + DOI, 'locations': [
                {'is_oa': True, 'pdf_url': PUBLISHER},
                {'is_oa': False, 'pdf_url': 'https://author.example/closed.pdf'},
                {'is_oa': True, 'pdf_url': 'https://author.example/paper.pdf', 'version': 'acceptedVersion'},
            ]}),
        ]
        found = next(copies.candidates(self.client, self.paper))
        self.assertEqual(found['url'], 'https://author.example/paper.pdf')
        self.assertEqual(found['version'], 'acceptedVersion')

    def test_wrong_pdf_and_html_are_never_saved(self):
        candidates = [dict(url='https://author.example/paper.pdf', provider='Fixture', version='preprint')]
        for content in (pdf('An unrelated research paper'), b'<html>Access denied</html>'):
            with self.subTest(content=content[:20]), tempfile.TemporaryDirectory() as directory:
                self.client.get.side_effect = [downloader.DownloadError('HTTP 403'), Mock(content=content)]
                with patch.object(copies, 'candidates', return_value=iter(candidates)):
                    result = downloader.retrieve_paper(self.client, self.paper, Path(directory), {})
                self.assertEqual(result['status'], 'failed')
                self.assertIn('HTTP 403', result['error'])
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_doi_parsing_and_cancelled_download(self):
        legacy = downloader.Paper(1999, TITLE, 'https://dl.acm.org/citation.cfm?doid=3600006.3613165')
        self.assertEqual(copies.doi_for(legacy), DOI)
        self.assertEqual(copies.doi_for(self.paper), DOI)
        self.client.cancelled.set()
        self.client.get.side_effect = downloader.DownloadError('Download interrupted')
        with tempfile.TemporaryDirectory() as directory, patch.object(copies, 'candidates') as find:
            result = downloader.retrieve_paper(self.client, self.paper, Path(directory), {})
        self.assertEqual(result['status'], 'failed')
        find.assert_not_called()


if __name__ == '__main__':
    unittest.main()
