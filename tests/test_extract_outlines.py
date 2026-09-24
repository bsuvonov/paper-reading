import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

import extract_outlines as e


class OutlineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_pdf(self, bookmarks=False):
        path = self.root / 'paper.pdf'
        doc = pymupdf.open()
        cover = doc.new_page(width=612, height=792)
        cover.insert_text((80, 200), 'Design and Implementation', fontsize=18, fontname='tibo')
        page = doc.new_page(width=612, height=792)
        page.insert_text((50, 55), 'Abstract', fontsize=12, fontname='tibo')
        # Separate number, title words, and a wrapped continuation.
        page.insert_text((50, 100), '1', fontsize=12, fontname='tibo')
        page.insert_text((68, 100), 'Introduction', fontsize=12, fontname='tibo')
        page.insert_text((50, 190), '1.1 Motivation', fontsize=11, fontname='tibo')
        page.insert_text((50, 300), '2', fontsize=12, fontname='tibo')
        page.insert_text((68, 300), 'System', fontsize=12, fontname='tibo')
        page.insert_text((110, 300), 'Overview,', fontsize=12, fontname='tibo')
        page.insert_text((68, 314), 'Design', fontsize=12, fontname='tibo')
        # Right-column heading must follow the whole left column, even at smaller y.
        page.insert_text((320, 110), '3 Evaluation', fontsize=12, fontname='tibo')
        for x in (50, 320):
            for y in (130, 143, 156, 169, 225, 238, 251, 264, 350, 363):
                page.insert_text((x, y), 'This is ordinary body text for our test paper.', fontsize=10, fontname='tiro')
        page.insert_text((320, 400), 'Figure 1: A graph', fontsize=12, fontname='tibo')
        page.insert_text((320, 420), '4 items appear in this body sentence.', fontsize=10, fontname='tiro')
        page.insert_text((320, 520), 'References', fontsize=12, fontname='tibo')
        if bookmarks:
            doc.set_toc([[1, 'Introduction', 2], [2, 'Motivation', 2],
                         [1, 'System Overview, Design', 2], [1, 'Evaluation', 2]])
        doc.save(path)
        doc.close()
        return path

    def test_two_columns_split_numbers_wrapped_headings_and_cover(self):
        path = self.make_pdf()
        sections, pages, warnings = e.extract_pdf(path, self.root / '.cache', 'never', None, 200)
        numbered = [s for s in sections if s['number']]
        self.assertEqual([s['number'] for s in numbered], ['1', '1.1', '2', '3'])
        self.assertEqual(numbered[2]['title'], 'System Overview, Design')
        self.assertEqual(numbered[1]['level'], 2)
        self.assertTrue(all(s['page'] == 2 for s in sections))
        self.assertEqual(pages, 2)
        self.assertNotIn('Design and Implementation', [s['title'] for s in sections])
        self.assertFalse(any('Figure' in s['title'] for s in sections))

    def test_pdf_bookmarks_take_precedence(self):
        sections, _, _ = e.extract_pdf(self.make_pdf(bookmarks=True), self.root / '.cache', 'never', None, 200)
        self.assertEqual([s['method'] for s in sections], ['bookmark'] * 4)
        self.assertEqual([s['level'] for s in sections], [1, 2, 1, 1])
        self.assertEqual(sections[0]['number'], '1')

    def test_type3_without_bold_flags_can_still_be_a_heading(self):
        # A PDF font's bold bit is not required if its heading is visibly larger.
        lines = [e.Line('Abstract', 1, (50, 50, 100, 62), 12, 0),
                 e.Line('Body text ' * 12, 1, (50, 70, 250, 80), 10, 0),
                 e.Line('1 Introduction', 1, (50, 100, 180, 112), 12, 0)]
        self.assertEqual(e.layout_sections(lines)[-1]['number'], '1')

    def test_unreadable_type3_text_requires_ocr(self):
        self.assertTrue(e.poor_text([e.Line('unreadable text ' * 40, 1, (10, 10, 200, 20), .2, 0)]))
        self.assertFalse(e.poor_text([e.Line('readable text ' * 40, 1, (10, 10, 200, 20), 10, 0)]))

    def test_wrapped_heading_without_bold_metadata_preserves_acronym(self):
        lines = [e.Line('1 Two-Level Replacement and LRU-', 1, (50, 50, 290, 62), 12, 0),
                 e.Line('SP', 1, (70, 64, 85, 76), 12, 0),
                 e.Line('This is ordinary body text following the heading.', 1, (50, 80, 290, 90), 10, 0)]
        sections = e.layout_sections(lines)
        self.assertEqual(sections[0]['title'], 'Two-Level Replacement and LRU-SP')

    def test_ocr_rejoins_hyphenated_word_but_not_body_sentence(self):
        for continuation, expected in [('uation', 'Experiments and Performance Evaluation'),
                                       ('under these conditions the system works.', 'Experiments and Performance Eval-')]:
            with self.subTest(continuation=continuation):
                lines = [e.Line('1 Experiments and Performance Eval-', 1, (50, 50, 290, 62), 12, 0, ocr=True),
                         e.Line(continuation, 1, (70, 64, 260, 76), 11.5, 0, ocr=True),
                         e.Line('Ordinary body text ' * 12, 1, (50, 100, 290, 110), 10, 0, ocr=True)]
                sections = e.layout_sections(lines)
                self.assertEqual(sections[0]['title'], expected)

    def test_unresolved_wrapped_heading_is_flagged(self):
        sections = [e.make_section(str(i), title, 1, i, 'typography')
                    for i, title in enumerate(['Introduction', 'Performance Eval-', 'Conclusion'], 1)]
        self.assertTrue(any('wrapped continuation' in w for w in e.assess(sections, 3)))

    def test_descending_postscript_pages_are_reordered_once(self):
        path = self.root / 'reversed.pdf'
        with pymupdf.open() as document:
            for title in ('Second page', 'First page'):
                page = document.new_page()
                page.insert_text((50, 50), title)
            document.save(path)
        source = b'%!PS\n%%Page: "2" 1\n%%Page: "1" 2\n'
        e.normalize_ps_page_order(path, source)
        e.normalize_ps_page_order(path, source)
        with pymupdf.open(path) as document:
            self.assertEqual(document[0].get_text().strip(), 'First page')
            self.assertEqual(document[1].get_text().strip(), 'Second page')

    def test_html_hierarchy_navigation_and_deduplication(self):
        entry = self.root / 'index.html'
        node = self.root / 'node1.html'
        entry.write_text('<h1>A Paper</h1><h4>Author One and Author Two</h4>'
                         '<h3>Abstract:</h3><a href="node1.html">Introduction</a>')
        node.write_text('<h1>Introduction</h1><h2>Motivation</h2><h1>Evaluation</h1>'
                        '<h1>Conclusion</h1><h1>About this document ...</h1><a href="index.html">Back</a>')
        sections, pages, _ = e.extract_html(e.Paper(entry, 'A Paper', '1994'))
        self.assertEqual([s['title'] for s in sections], ['Abstract', 'Introduction', 'Motivation', 'Evaluation', 'Conclusion'])
        self.assertEqual([s['level'] for s in sections], [1, 1, 2, 1, 1])
        self.assertIsNone(pages)

    def test_discovery_skips_partial_html_bundles(self):
        year = self.root / 'papers' / '1994'
        bundle = year / 'broken-paper'
        bundle.mkdir(parents=True)
        (bundle / 'index.html').write_text('<h1>Paper title</h1>')
        (bundle / 'node1.html').write_text('<h1>Introduction</h1>')
        (year / 'manifest.json').write_text(json.dumps({'papers': [{'title': 'Broken paper', 'status': 'failed'}]}))
        self.assertEqual(e.discover(self.root / 'papers', None), [])

    def test_discovery_uses_one_html_entry_per_manifest_paper(self):
        year = self.root / 'papers' / '1999'
        bundle = year / 'A Paper'
        bundle.mkdir(parents=True)
        for name in ('index.html', 'node1.html'):
            (bundle / name).write_text('<h1>Heading</h1>')
        (year / 'manifest.json').write_text(json.dumps({'papers': [{
            'title': 'A Paper', 'year': 1999, 'status': 'downloaded', 'path': 'A Paper/index.html',
            'files': [{'path': 'A Paper/index.html'}, {'path': 'A Paper/node1.html'}]}]}))
        papers = e.discover(self.root / 'papers', [1999])
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].stem, 'A Paper')

    def test_number_gaps_and_inclusive_page_spans(self):
        sections = [e.make_section('1', 'Introduction', 1, 2, 'typography'),
                    e.make_section('2', 'Design', 1, 3, 'typography'),
                    e.make_section('4', 'Evaluation', 1, 5, 'typography'),
                    e.make_section('', 'References', 1, 7, 'typography')]
        warnings = e.assess(sections, 9)
        self.assertTrue(any('gaps' in warning for warning in warnings))
        self.assertEqual([s['end_page'] for s in sections], [3, 5, 7, 9])

    def test_ocr_does_not_turn_articles_or_reference_authors_into_appendices(self):
        texts = ['Abstract', '1 Introduction', '2 Design', '3 Implementation',
                 'A DSM requires reliable communication', '4 Conclusion', 'References',
                 'A Smith and B Jones', 'W Fenton, B Ramkumar']
        lines = [e.Line(text, 1, (50, i * 30, 250, i * 30 + 12), 12, 0, ocr=True)
                 for i, text in enumerate(texts)]
        lines.append(e.Line('Ordinary body text ' * 12, 1, (50, 500, 250, 510), 10, 0, ocr=True))
        headings = e.layout_sections(lines)
        self.assertEqual([s['number'] for s in headings if s['number']], ['1', '2', '3', '4'])

    def test_plain_text_does_not_promote_numbered_body_lists(self):
        path = self.root / 'paper.txt'
        path.write_text('Abstract\n\n1     Introduction\n\n2     Design\n\n'
                        '1. First body step\n2. Second body step\n\n3     Evaluation\n\nReferences\nE Author citation\n')
        sections, _, warnings = e.extract_text(path)
        self.assertEqual([s['number'] for s in sections if s['number']], ['1', '2', '3'])
        self.assertTrue(warnings)

    def test_cli_writes_linked_comparison_json_csv_and_reuses_cache(self):
        source = self.make_pdf(bookmarks=True)
        output = self.root / 'outlines'
        args = ['--input', str(source), '--output', str(output), '--ocr', 'never']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(e.main(args), 0)
        result = json.loads((output / 'index.json').read_text())['papers'][0]
        self.assertEqual(result['status'], 'ok')
        self.assertTrue((output / 'sections.csv').is_file())
        outline = Path(result['outline_path']).read_text()
        self.assertIn('  - 1.1 Motivation', outline)
        self.assertIn('#page=2', outline)
        with patch.object(e, 'extract_pdf', side_effect=AssertionError('should use cache')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(e.main(args), 0)


if __name__ == '__main__':
    unittest.main()
