#!/usr/bin/env python3
"""Extract paper section hierarchies and build a comparison index locally."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import asdict, dataclass
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup
import pymupdf
import requests


VERSION = 4
OCR_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/eng.traineddata"
OCR_SHA256 = "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
NUMBER = r"(?:[1-9]\d?(?:\.\d{1,2}){0,3}|[A-Z](?:\.\d{1,2}){0,3})"
NUMBERED = re.compile(rf"^({NUMBER})\.?\s+(.+)$")
NUMBER_ONLY = re.compile(rf"^{NUMBER}\.?$")
COMMON = re.compile(
    r"^(?:abstract|introduction|background(?: and motivation)?|motivation|overview|"
    r"design(?: and implementation)?|implementation|evaluation|experimental results|"
    r"performance(?: evaluation)?|discussion|limitations|related work|"
    r"conclusions?(?: and future work)?|future work|acknowledg(?:e)?ments?|"
    r"references|bibliography|appendix(?:\s+[A-Z])?(?:[.:]?\s+.+)?)$", re.I)
AUXILIARY = re.compile(r"^(abstract|references|bibliography|acknowledg(?:e)?ments?)$", re.I)


def clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "")
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())


def normalized(text: str) -> str:
    return re.sub(r"[^\w]+", "", clean(text).lower())


def split_heading(text: str) -> tuple[str, str, int]:
    text = clean(text)
    match = NUMBERED.fullmatch(text)
    if match:
        number, title = match.groups()
        # A normal English sentence starting with the article "A" is not an appendix.
        return number, title, number.count(".") + 1
    return "", text, 1


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())


def source_format(path: Path) -> str:
    name = path.name.lower()
    return next((ext for ext in (".ps.gz", ".ps.z", ".pdf", ".ps", ".html", ".htm", ".txt")
                 if name.endswith(ext)), "")


@dataclass
class Paper:
    path: Path
    title: str
    year: str
    bundle_files: tuple[Path, ...] = ()

    @property
    def stem(self) -> str:
        return self.path.parent.name if source_format(self.path) in (".html", ".htm") and self.bundle_files else self.path.name[:-len(source_format(self.path))]


def discover(input_path: Path, years: list[int] | None) -> list[Paper]:
    """Use downloader metadata to avoid treating HTML section pages as papers."""
    papers = {}
    covered = set()
    manifests = sorted(input_path.rglob("manifest.json")) if input_path.is_dir() else []
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text())
            for item in data.get("papers", []):
                files = tuple((manifest.parent / f["path"]).resolve() for f in item.get("files", []))
                covered.update(files)
                if not item.get("path") or item.get("status") not in ("downloaded", "skipped"):
                    continue
                path = (manifest.parent / item["path"]).resolve()
                if path.is_file() and path.is_relative_to(input_path.resolve()):
                    papers[path] = Paper(path, item["title"], str(item.get("year", data.get("year", "unknown"))), files)
        except (ValueError, KeyError, TypeError) as exc:
            print(f"Warning: cannot read {manifest}: {exc}", file=sys.stderr)
    manifest_roots = [m.parent.resolve() for m in manifests]
    files = sorted(input_path.rglob("*")) if input_path.is_dir() else [input_path]
    for path in files:
        if not path.is_file() or not source_format(path) or path.resolve() in covered:
            continue
        if source_format(path) in (".html", ".htm") and input_path.is_dir():
            if any(path.resolve().is_relative_to(root) for root in manifest_roots):
                continue  # Partial/failed HTML bundles are not completed papers.
            if re.fullmatch(r"(?:node\d+|footnode|about)", path.stem, re.I):
                continue
        # HTML bundles are discovered through their manifest; standalone HTML is
        # also accepted. Section pages in a bundle must not become separate papers.
        if any(parent.name.startswith(".") for parent in path.relative_to(input_path.parent).parents):
            continue
        year = next((p.name for p in path.parents if re.fullmatch(r"(?:19|20)\d{2}", p.name)), "unknown")
        title = path.name[:-len(source_format(path))]
        title = re.sub(r" - [0-9a-f]{10}$", "", re.sub(r"^\d{4} - ", "", title))
        papers.setdefault(path.resolve(), Paper(path.resolve(), title, year))
    return sorted((p for p in papers.values() if not years or p.year in set(map(str, years))),
                  key=lambda p: (p.year, p.title.lower()))


def fingerprint(paper: Paper) -> str:
    digest = hashlib.sha256()
    paths = paper.bundle_files if source_format(paper.path) in (".html", ".htm") and paper.bundle_files else (paper.path,)
    for path in sorted(paths):
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def prepare_pdf(paper: Paper, cache: Path, digest: str) -> Path:
    if source_format(paper.path) == ".pdf":
        return paper.path
    gs = shutil.which("gs")
    if not gs:
        raise RuntimeError("PostScript conversion requires Ghostscript (the gs command)")
    output = cache / (digest + ".pdf")
    data = paper.path.read_bytes()
    if source_format(paper.path) == ".ps.gz":
        data = gzip.decompress(data)
    elif source_format(paper.path) == ".ps.z":
        program = shutil.which("gzip")
        if not program:
            raise RuntimeError("Compressed .ps.Z input requires gzip")
        data = subprocess.run([program, "-cd", str(paper.path)], capture_output=True,
                              check=True, timeout=120).stdout
    # One historical archive file has a mail MIME boundary after its PS trailer.
    data = re.sub(rb"\r?\n--boundary-[^\r\n]+--\s*$", b"\n", data)
    if output.exists():
        normalize_ps_page_order(output, data)
        return output
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache) as temporary:
        source = Path(temporary) / "source.ps"
        converted = Path(temporary) / "converted.pdf"
        source.write_bytes(data)
        result = subprocess.run([gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                                 f"-sOutputFile={converted.resolve()}", "-f", str(source.resolve())],
                                capture_output=True, timeout=120)
        if result.returncode:
            message = (result.stdout + result.stderr).decode(errors="replace")
            raise RuntimeError(f"Ghostscript failed: {message[:700]}")
        with pymupdf.open(converted) as document:
            if not len(document):
                raise RuntimeError("PostScript conversion produced no pages")
        converted.replace(output)
    normalize_ps_page_order(output, data)
    return output


def normalize_ps_page_order(path: Path, postscript: bytes) -> None:
    """Some printer-oriented PS files store the last page first."""
    labels = [int(n) for n in re.findall(rb'^%%Page:\s+"?(\d+)"?\s+\d+\s*$', postscript, re.M)]
    marker = "OSDI page order normalized"
    temporary = path.with_name(path.name + ".reordered")
    changed = False
    with pymupdf.open(path) as document:
        if marker in document.metadata.get("keywords", ""):
            return
        if len(labels) != len(document) or len(set(labels)) != len(labels):
            return
        order = sorted(range(len(labels)), key=labels.__getitem__)
        if order != list(range(len(document))):
            document.select(order)
            metadata = document.metadata
            metadata["keywords"] = (metadata.get("keywords", "") + " " + marker).strip()
            document.set_metadata(metadata)
            document.save(temporary)
            changed = True
    if changed:
        temporary.replace(path)


def get_ocr_data(cache: Path, explicit: Path | None) -> str:
    if explicit:
        if not (explicit / "eng.traineddata").is_file():
            raise RuntimeError(f"No eng.traineddata in {explicit}")
        return str(explicit.resolve())
    try:
        installed = pymupdf.get_tessdata()
        if (Path(installed) / "eng.traineddata").is_file():
            return installed
    except RuntimeError:
        pass
    directory = cache / "tessdata"
    model = directory / "eng.traineddata"
    if not model.exists() or hashlib.sha256(model.read_bytes()).hexdigest() != OCR_SHA256:
        print("  Downloading English OCR language data (4 MB); documents stay local.", flush=True)
        response = requests.get(OCR_URL, timeout=60)
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != OCR_SHA256:
            raise RuntimeError("OCR language data failed its SHA-256 integrity check")
        atomic_write(model, response.content)
    return str(directory.resolve())


@dataclass
class Line:
    text: str
    page: int
    box: tuple[float, float, float, float]
    size: float
    bold: float
    column: int = 0
    ocr: bool = False


def page_lines(page, textpage=None, ocr: bool = False) -> list[Line]:
    lines = []
    data = page.get_text("dict", textpage=textpage, flags=pymupdf.TEXTFLAGS_TEXT)
    for block in data["blocks"]:
        for raw in block.get("lines", []):
            if abs(raw.get("dir", (1, 0))[1]) > .1:
                continue
            spans = raw["spans"]
            text = clean("".join(s["text"] for s in spans))
            if not text:
                continue
            weights = [max(1, len(s["text"].strip())) for s in spans]
            weight = sum(weights)
            size = sum(s["size"] * w for s, w in zip(spans, weights)) / weight
            bold = sum(w for s, w in zip(spans, weights) if s["flags"] & 16 or
                       re.search(r"bold|medi|cmbx|demi", s["font"], re.I)) / weight
            lines.append(Line(text, page.number + 1, tuple(raw["bbox"]), size, bold, ocr=ocr))
    width = page.rect.width
    left = sum(len(l.text) > 35 and l.box[2] < width * .59 for l in lines)
    right = sum(len(l.text) > 35 and l.box[0] > width * .43 for l in lines)
    two_columns = left >= 4 and right >= 4
    for line in lines:
        line.column = int(two_columns and line.box[0] > width * .46)
    # TeX frequently emits the section number and title as separate PDF lines.
    text_sizes = Counter()
    for line in lines:
        if line.size >= 5 and len(line.text) > 30:
            text_sizes[round(line.size * 2) / 2] += len(line.text)
    typical_size = text_sizes.most_common(1)[0][0] if text_sizes else 10
    column_starts = {}
    for line in lines:
        if len(line.text) > 40:
            column_starts.setdefault(line.column, Counter())[round(line.box[0] / 5) * 5] += 1
    column_starts = {col: counts.most_common(1)[0][0] for col, counts in column_starts.items()}
    consumed = set()
    for i, line in enumerate(lines):
        if (i in consumed or not NUMBER_ONLY.fullmatch(line.text) or
                not ocr and line.bold < .65 and line.size < typical_size + .6):
            continue
        if ocr and (line.size < typical_size * .85 or abs(line.box[0] - column_starts.get(line.column, line.box[0])) > 12):
            continue
        while True:
            candidates = [(j, other) for j, other in enumerate(lines) if j != i and j not in consumed
                          and other.column == line.column
                          and (abs(other.box[3] - line.box[3]) < 4 if ocr else abs(other.box[1] - line.box[1]) < 2.5)
                          and -1 <= other.box[0] - line.box[2] <= max(35, line.size * 3)
                          and (ocr or other.bold >= .65 or other.size >= typical_size + .6)
                          and abs(other.size - line.size) < (4 if ocr else 1.1)
                          and any(c.isalpha() for c in other.text)]
            if not candidates:
                break
            j, other = min(candidates, key=lambda item: item[1].box[0])
            line.text += " " + other.text
            line.box = (line.box[0], min(line.box[1], other.box[1]), other.box[2], max(line.box[3], other.box[3]))
            line.bold = min(line.bold, other.bold)
            consumed.add(j)
    return sorted((l for i, l in enumerate(lines) if i not in consumed), key=lambda l: (l.column, l.box[1], l.box[0]))


def poor_text(lines: list[Line]) -> bool:
    chars = sum(len(l.text) for l in lines)
    return chars < 80 or sum(len(l.text) for l in lines if l.size < 3) > chars * .25


def make_section(number: str, title: str, level: int, page: int | None,
                 method: str, **extra) -> dict:
    return {"number": number, "title": clean(title), "level": level, "page": page,
            "method": method, **extra}


def repair_ocr_numbers(lines: list[Line]) -> list[Line]:
    """OCR can put a number and its title on overlapping, unequal-height lines."""
    consumed = set()
    for i, line in enumerate(lines):
        if not line.ocr or not NUMBER_ONLY.fullmatch(line.text):
            continue
        candidates = []
        for j, other in enumerate(lines):
            if (i == j or j in consumed or not other.ocr or other.page != line.page or
                    other.column != line.column or not other.text[0].isalpha()):
                continue
            overlap = min(line.box[3], other.box[3]) - max(line.box[1], other.box[1])
            height = min(line.box[3] - line.box[1], other.box[3] - other.box[1])
            if overlap >= height * .5 and 0 <= other.box[0] - line.box[2] <= 35:
                candidates.append((j, other))
        if candidates:
            j, other = min(candidates, key=lambda pair: pair[1].box[0])
            line.text += ' ' + other.text
            line.size = max(line.size, other.size)
            line.box = (line.box[0], min(line.box[1], other.box[1]), other.box[2], max(line.box[3], other.box[3]))
            consumed.add(j)
    return [line for i, line in enumerate(lines) if i not in consumed]


def title_case_fragment(text: str) -> bool:
    words = [w for w in re.findall(r"[A-Za-z]+", text) if w.lower() not in
             {"a", "an", "the", "of", "in", "on", "for", "with", "and", "to", "by"}]
    return 0 < len(words) <= 8 and sum(w[0].isupper() for w in words) >= len(words) * .5


def layout_sections(lines: list[Line]) -> list[dict]:
    lines = repair_ocr_numbers(lines)
    sizes = Counter()
    for line in lines:
        if line.size >= 5 and len(line.text) > 30 and line.bold < .5:
            sizes[round(line.size * 2) / 2] += len(line.text)
    body = sizes.most_common(1)[0][0] if sizes else 10
    margin_counts = {}
    for line in lines:
        if len(line.text) > 40 and line.size >= 5:
            key = (line.page, line.column)
            margin_counts.setdefault(key, Counter())[round(line.box[0] / 5) * 5] += 1
    margins = {key: counts.most_common(1)[0][0] for key, counts in margin_counts.items()}
    result = []
    consumed = set()
    seen = set()
    current_main = 0
    in_references = False
    started = False
    last_appendix = ord('A') - 1
    concluded = False
    for i, line in enumerate(lines):
        if i in consumed or line.size < 5:
            continue
        number, title, level = split_heading(line.text)
        common = bool(COMMON.fullmatch(line.text))
        if not number and not common:
            continue
        if not any(c.isalpha() for c in title) or len(title) > 170 or len(title.split()) > 24:
            continue
        if number and (not title[0].isalpha() or title[0].islower() or title.endswith(";")):
            continue
        if not number:
            if not started and normalized(title) != "abstract":
                continue  # Exclude headings on the publisher's cover sheet.
            if current_main and not AUXILIARY.fullmatch(title) and not title.lower().startswith("appendix"):
                continue  # Ordinary bold labels inside a numbered paper are not main sections.
        prominent = (line.bold >= .65 and line.size >= body - .6) or line.size >= body + .8
        if line.ocr:
            prominent = line.size >= body * .83
        if not prominent:
            continue
        margin = margins.get((line.page, line.column), line.box[0])
        if number and abs(line.box[0] - margin) > 30:
            continue
        if number and number[0].isalpha() and current_main < 2:
            continue
        if number and number[0].isalpha():
            if not concluded and not in_references:
                continue
            if line.ocr and in_references:
                continue
            if level == 1 and ord(number) != last_appendix + 1:
                continue
        if in_references and number and number[0].isdigit():
            continue
        if number and number[0].isdigit():
            main = int(number.split(".")[0])
            if level == 1 and (main <= current_main or main > 30):
                continue
            if level > 1 and current_main and main != current_main:
                continue
        # Join wrapped titles only when the next line has matching typography.
        for j in range(i + 1, min(i + 3, len(lines))):
            following = lines[j]
            matching_style = (abs(following.size - line.size) <= .6 and
                              (following.bold >= .65 or following.size >= body + .8))
            if line.ocr:
                # A short lowercase suffix can complete a hyphenated word even
                # though it is not title case. Retain the geometry/style checks
                # below so an ordinary body sentence is not absorbed.
                word_suffix = title.endswith("-") and bool(re.fullmatch(r"[a-z]{1,20}", following.text))
                matching_style = (following.size >= body + .5 and abs(following.size - line.size) < 3
                                  and len(following.text) < 65
                                  and (title_case_fragment(following.text) or word_suffix))
            if (following.page != line.page or following.column != line.column or
                not -line.size * .35 <= following.box[1] - line.box[3] <= line.size * .75 or
                not matching_style or
                abs(following.box[0] - line.box[0]) > 35 or
                NUMBERED.fullmatch(following.text) or AUXILIARY.fullmatch(following.text) or
                len(title + following.text) > 170):
                break
            if title.endswith("-") and following.text[0].islower():
                title = title[:-1] + following.text
            else:
                title += ("" if title.endswith("-") else " ") + following.text
            consumed.add(j)
            line = Line(line.text, line.page, (line.box[0], line.box[1], following.box[2], following.box[3]),
                        line.size, line.bold, line.column, line.ocr)
        key = (number, normalized(title))
        if key in seen:
            continue
        seen.add(key)
        started = True
        if number and number[0].isdigit() and level == 1:
            current_main = int(number)
        elif number and number[0].isalpha() and level == 1:
            last_appendix = ord(number)
        if normalized(title) in ("references", "bibliography"):
            in_references = True
        if level == 1 and re.search(r"conclu|^summary", title, re.I):
            concluded = True
        result.append(make_section(number, title, level, line.page, "ocr" if line.ocr else "typography",
                                  bbox=[round(n, 2) for n in line.box], column=line.column))
    return result


def extract_pdf(path: Path, cache: Path, ocr: str, tessdata: Path | None, dpi: int) -> tuple[list, int, list]:
    warnings = []
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise RuntimeError("The PDF is encrypted and requires a password")
        bookmarks = [item for item in document.get_toc() if 1 <= item[2] <= len(document)]
        useful_bookmarks = len(bookmarks) >= 3 and any(COMMON.fullmatch(split_heading(b[1])[1]) for b in bookmarks)
        native = [page_lines(page) for page in document]
        preliminary = layout_sections([line for page in native for line in page])
        mains = [s for s in preliminary if s["level"] == 1 and not AUXILIARY.fullmatch(s["title"])]
        numbers = [int(s["number"]) for s in mains if s["number"].isdigit()]
        bad_outline = len(mains) < 3 or bool(numbers and numbers[0] != 1)
        lines = []
        ocr_pages = 0
        ocr_key = None
        language_data = None
        for page in document:
            extracted = native[page.number]
            bad = poor_text(extracted)
            if ocr == "always" or (bad or bad_outline) and ocr == "auto" and not useful_bookmarks:
                if language_data is None:
                    language_data = get_ocr_data(cache, tessdata)
                    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    model_hash = hashlib.sha256((Path(language_data) / "eng.traineddata").read_bytes()).hexdigest()
                    ocr_key = hashlib.sha256(f"{content_hash}-{model_hash}-{pymupdf.VersionBind}-{dpi}".encode()).hexdigest()
                cached_page = cache / "ocr" / f"{ocr_key}-{page.number + 1}.json"
                if cached_page.exists():
                    extracted = [Line(**item) for item in json.loads(cached_page.read_text())["lines"]]
                else:
                    print(f"  OCR page {page.number + 1}/{len(document)}", flush=True)
                    textpage = page.get_textpage_ocr(language="eng", dpi=dpi, full=True, tessdata=language_data)
                    extracted = page_lines(page, textpage, ocr=True)
                    write_json(cached_page, {"lines": [asdict(line) for line in extracted]})
                ocr_pages += 1
            elif bad and not useful_bookmarks:
                warnings.append(f"Page {page.number + 1} has little usable text; rerun with --ocr auto.")
            lines.extend(extracted)
        inferred = layout_sections(lines)
        if useful_bookmarks:
            by_title = {normalized(s["title"]): s for s in inferred}
            sections = []
            for level, text, page_number in bookmarks:
                number, title, _ = split_heading(text)
                match = by_title.get(normalized(title), {})
                sections.append(make_section(number or match.get("number", ""), title, level, page_number, "bookmark"))
        else:
            sections = inferred
        if ocr_pages:
            warnings.append(f"Used OCR on {ocr_pages} pages; check recognition and heading boundaries.")
        return sections, len(document), warnings


def extract_html(paper: Paper) -> tuple[list, None, list]:
    root = paper.path.parent.resolve()
    queue = deque([paper.path])
    seen = set()
    sections = []
    tag_levels = []
    identities = set()
    while queue:
        path = queue.popleft().resolve()
        if path in seen or not path.is_relative_to(root):
            continue
        seen.add(path)
        if len(seen) > 500:
            raise RuntimeError("HTML paper has more than 500 linked section pages")
        soup = BeautifulSoup(path.read_bytes(), "html.parser")
        for tag in soup.select("h1, h2, h3, h4, h5, h6"):
            text = clean(tag.get_text(" ", strip=True))
            first_line = next((clean(line).rstrip(":") for line in tag.get_text("\n", strip=True).splitlines()
                               if clean(line)), "")
            if COMMON.fullmatch(first_line):
                text = first_line  # Some legacy pages never close their heading tag.
            number, title, level = split_heading(text)
            if not text or normalized(text) == normalized(paper.title) or normalized(title) == normalized(paper.title):
                continue
            if text.lower().startswith(("about this document", "footnotes", "contents", "table of contents")):
                continue
            if not sections and not number and not COMMON.fullmatch(text):
                continue  # Title-page author and affiliation headings.
            if len(title) > 170:
                continue
            if not number and not COMMON.fullmatch(text):
                # Ignore a title-page h1, but retain unnumbered subsection headings.
                if path == paper.path and tag.name == "h1":
                    continue
                level = int(tag.name[1])
            identity = (number, normalized(title))
            if identity not in identities:
                identities.add(identity)
                sections.append(make_section(number, title, level, None, "html", source=str(path)))
                tag_levels.append(int(tag.name[1]))
        for a in soup.select("a[href]"):
            parts = urlsplit(a["href"])
            if parts.scheme or parts.netloc or not parts.path:
                continue
            target = (path.parent / unquote(parts.path)).resolve()
            if source_format(target) in (".html", ".htm") and target.is_relative_to(root) and target.is_file():
                queue.append(target)
    base_level = min((tag_level for section, tag_level in zip(sections, tag_levels)
                      if not section["number"] and not AUXILIARY.fullmatch(section["title"])), default=1)
    for section, tag_level in zip(sections, tag_levels):
        if not section["number"]:
            section["level"] = 1 if AUXILIARY.fullmatch(section["title"]) else max(1, tag_level - base_level + 1)
    return sections, None, []


def extract_text(path: Path) -> tuple[list, None, list]:
    sections = []
    seen = set()
    current_main = 0
    in_references = False
    # Legacy ASCII exports sometimes place both columns on the same physical line.
    lines = path.read_text(errors="replace").splitlines()
    for index, line in enumerate(lines):
        for cell in re.split(r"[ \t]{12,}", line.strip()):
            number, title, level = split_heading(cell)
            if not number and not COMMON.fullmatch(clean(cell)):
                continue
            if len(title) > 110 or not title or not title[0].isupper():
                continue
            if number:
                if not number[0].isdigit() or in_references and not COMMON.fullmatch(title):
                    continue
                if level == 1:
                    if int(number) <= current_main:
                        continue
                    current_main = int(number)
            if normalized(title) in ("references", "bibliography"):
                in_references = True
            if not number and normalized(title) not in ("abstract", "references", "acknowledgements"):
                if index and lines[index - 1].strip():
                    continue
            key = (number, normalized(title))
            if key not in seen:
                seen.add(key)
                sections.append(make_section(number, title, level, None, "plain_text", line=index + 1))
    sections.sort(key=lambda s: (0, ()) if normalized(s['title']) == 'abstract' else
                  (1, tuple(map(int, s['number'].split('.')))) if s['number'] else
                  (3, ()) if normalized(s['title']) in ('references', 'bibliography') else (2, ()))
    return sections, None, ["Plain text has no typography or reliable page locations; verify this outline."]


def assess(sections: list, pages: int | None) -> list[str]:
    warnings = []
    main = [s for s in sections if s["level"] == 1 and not AUXILIARY.fullmatch(s["title"])]
    if len(main) < 3:
        warnings.append("Fewer than three main sections were found; the outline may be incomplete.")
    numbers = [int(s["number"]) for s in sections if s["level"] == 1 and s["number"].isdigit()]
    if numbers and numbers != list(range(1, max(numbers) + 1)):
        warnings.append("Main section numbering has gaps or is out of order; check for missed headings.")
    if not sections:
        warnings.append("No section headings found.")
    if any(s["title"].endswith("-") for s in sections):
        warnings.append("A heading ends with a hyphen; check for a missing wrapped continuation.")
    for i, section in enumerate(sections):
        if section["page"] is None:
            section["end_page"] = None
            continue
        next_section = next((s for s in sections[i + 1:] if s["level"] <= section["level"]), None)
        # Inclusive page spans intentionally overlap when sections share a page.
        section["end_page"] = max(section["page"], next_section["page"]) if next_section else pages
    return warnings


def relative_link(path: Path, directory: Path) -> str:
    return quote(Path(os.path.relpath(path, directory)).as_posix(), safe="/.")


def outline_markdown(record: dict, destination: Path) -> str:
    source = relative_link(Path(record["source"]), destination.parent)
    pdf_link = relative_link(Path(record.get("pdf_source", record["source"])), destination.parent)
    text = [f"# {record['title']}", "", f"Year: {record['year']} · [Original paper]({source})",
            "", f"Extraction: {record['status']} ({', '.join(record.get('methods', [])) or 'none'}).", ""]
    if record.get("page_count"):
        if record.get("pdf_source") and record["source_format"] != ".pdf":
            text += [f"[Converted PDF]({pdf_link})", ""]
        text += ["Page numbers refer to the PDF file, including any publisher cover. Ranges include shared boundary pages.", ""]
    if record.get("warnings"):
        text += ["Review notes:", ""] + [f"- {w}" for w in record["warnings"]] + [""]
    text += ["## Outline", ""]
    for section in record["sections"]:
        title = ((section["number"] + " ") if section["number"] else "") + section["title"]
        location = ""
        if section["page"]:
            end = section.get("end_page")
            label = f"pp. {section['page']}–{end}" if end and end != section["page"] else f"p. {section['page']}"
            location = f" — {label}"
            if record["source_format"] == ".pdf" or record.get("pdf_source"):
                location = f" — [{label}]({pdf_link}#page={section['page']})"
        text.append("  " * (section["level"] - 1) + f"- {title}{location}")
    if not record["sections"]:
        text.append("No outline could be extracted; see the review notes above.")
    return "\n".join(text) + "\n"


def table_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def write_comparison(records: list[dict], output: Path) -> None:
    counts = Counter(r["status"] for r in records)
    lines = ["# OSDI paper structures", "",
             f"{len(records)} papers: {counts['ok']} extracted, {counts['needs_review']} need review, {counts['failed']} failed.", "",
             "Use the outlines to compare section order and the placement of design, implementation, evaluation, and related work. "
             "Open the source before relying on inferred headings. This describes the downloaded sample, not all OSDI papers.", "",
             "## Comparison", "", "| Year | Paper / full outline | Main section sequence | Status |",
             "| --- | --- | --- | --- |"]
    frequencies = Counter()
    for record in records:
        main = [s for s in record["sections"] if s["level"] == 1 and not AUXILIARY.fullmatch(s["title"])]
        frequencies.update({s["title"].casefold() for s in main})
        link = relative_link(Path(record["outline_path"]), output)
        sequence = " → ".join(s["title"] for s in main) or "—"
        lines.append(f"| {record['year']} | [{table_cell(record['title'])}]({link}) | {table_cell(sequence)} | {record['status']} |")
    lines += ["", "## Recurring main-section titles", "",
              "Exact titles, ignoring case; counts are papers containing that title, including outlines marked for review.", "",
              "| Title | Papers |", "| --- | ---: |"]
    for title, count in frequencies.most_common(25):
        lines.append(f"| {table_cell(title)} | {count} |")
    atomic_write(output / "overview.md", ("\n".join(lines) + "\n").encode())
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["year", "paper", "status", "level", "number", "section", "page", "end_page", "method", "source"])
    for record in records:
        for section in record["sections"]:
            writer.writerow([record["year"], record["title"], record["status"], section["level"], section["number"],
                             section["title"], section["page"], section.get("end_page"), section["method"], record["source"]])
    atomic_write(output / "sections.csv", stream.getvalue().encode())
    write_json(output / "index.json", {"version": VERSION, "counts": dict(counts), "papers": records})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("papers"), help="paper directory or a single file (default: papers)")
    parser.add_argument("--output", type=Path, default=Path("outlines"), help="output directory (default: outlines)")
    parser.add_argument("--years", type=int, nargs="+", help="only these years")
    parser.add_argument("--max-depth", type=int, default=4, help="deepest section level to retain (default: 4)")
    parser.add_argument("--ocr", choices=("auto", "never", "always"), default="auto", help="OCR for unreadable text (default: auto)")
    parser.add_argument("--tessdata", type=Path, help="directory containing eng.traineddata; otherwise use installed or cached data")
    parser.add_argument("--dpi", type=int, default=300, help="OCR resolution (default: 300)")
    parser.add_argument("--force", action="store_true", help="re-extract unchanged papers")
    args = parser.parse_args(argv)
    if args.max_depth < 1 or args.dpi < 72:
        parser.error("--max-depth must be positive and --dpi must be at least 72")
    args.input = args.input.resolve()
    args.output = args.output.resolve()
    if not args.input.exists():
        parser.error(f"Input does not exist: {args.input}")
    if args.input.is_dir() and args.output.is_relative_to(args.input):
        parser.error("Keep the output directory outside the input directory")
    papers = discover(args.input, args.years)
    if not papers:
        parser.error("No completed paper files found for the selected years")
    options = {"ocr": args.ocr, "dpi": args.dpi, "max_depth": args.max_depth,
               "tessdata": str(args.tessdata) if args.tessdata else None}
    records = []
    outputs = set()
    for index, paper in enumerate(papers, 1):
        print(f"[{index}/{len(papers)}] {paper.year}: {paper.title}", flush=True)
        destination = args.output / paper.year / (paper.stem + ".json")
        if destination in outputs:
            destination = destination.with_name(paper.stem + " - " + hashlib.sha256(str(paper.path).encode()).hexdigest()[:8] + ".json")
        outputs.add(destination)
        markdown = destination.with_suffix(".md")
        record = {"version": VERSION, "year": paper.year, "title": paper.title, "source": str(paper.path),
                  "source_format": source_format(paper.path), "outline_path": str(markdown),
                  "options": options, "status": "failed", "sections": [], "warnings": [], "methods": []}
        try:
            digest = fingerprint(paper)
            record["fingerprint"] = digest
            old = json.loads(destination.read_text()) if destination.exists() else {}
            if (not args.force and old.get("version") == VERSION and old.get("fingerprint") == digest
                    and old.get("options") == options and old.get("status") in ("ok", "needs_review")):
                record = {**old, "outline_path": str(markdown)}
                print("  Cached outline", flush=True)
            else:
                fmt = source_format(paper.path)
                if fmt in (".pdf", ".ps", ".ps.gz", ".ps.z"):
                    pdf = prepare_pdf(paper, args.output / ".cache", digest)
                    record["pdf_source"] = str(pdf)
                    sections, pages, warnings = extract_pdf(pdf, args.output / ".cache", args.ocr, args.tessdata, args.dpi)
                elif fmt in (".html", ".htm"):
                    sections, pages, warnings = extract_html(paper)
                else:
                    sections, pages, warnings = extract_text(paper.path)
                warnings += assess(sections, pages)
                record.update(sections=[s for s in sections if s["level"] <= args.max_depth], page_count=pages,
                              warnings=warnings, status="needs_review" if warnings else "ok",
                              methods=sorted({s["method"] for s in sections}))
                print(f"  {len(record['sections'])} headings; {record['status']}", flush=True)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, requests.RequestException) as exc:
            record["warnings"].append(str(exc))
            print(f"  FAILED: {exc}", file=sys.stderr, flush=True)
        write_json(destination, record)
        atomic_write(markdown, outline_markdown(record, markdown).encode())
        records.append(record)
        write_comparison(records, args.output)
    print(f"\nComparison: {args.output / 'overview.md'}", flush=True)
    return 1 if any(r["status"] == "failed" for r in records) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted; completed outlines are saved. Rerun to continue.", file=sys.stderr)
        sys.exit(130)
