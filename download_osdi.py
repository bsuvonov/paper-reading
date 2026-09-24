#!/usr/bin/env python3
"""Download the OSDI research papers in USENIX's archive, organized by year."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading
import time
import unicodedata
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup


ARCHIVE_URL = "https://www.usenix.org/conferences/byname/179"
LEGACY_PROGRAMS = {
    1994: "publications/library/proceedings/osdi/index.html",
    1996: "publications/library/proceedings/osdi96/",
    1999: "publications/library/proceedings/osdi99/technical.html",
    2000: "publications/library/proceedings/osdi2000/technical.html",
    2002: "publications/library/proceedings/osdi02/tech.html",
    2004: "events/osdi04/tech/",
    2006: "events/osdi06/tech/",
    2008: "events/osdi08/tech/",
    2010: "events/osdi10/tech/",
}
NON_PAPERS = {
    "index", "technical", "tech", "cfp", "external_reviewers", "login-summary",
    "wip", "wips", "sw-caveat", "organizers", "preface", "proceedings",
}
FORMATS = (".pdf", ".ps", ".ps.gz", ".ps.z", ".html", ".htm", ".txt", ".a")


class DownloadError(Exception):
    pass


@dataclass
class Paper:
    year: int
    title: str
    page_url: str
    # Some legacy programs link directly to the paper.
    file_urls: tuple[str, ...] = ()


def clean_text(value: str) -> str:
    return " ".join(value.split())


def absolute_url(base: str, href: str) -> str:
    url = urldefrag(urljoin(base, href.strip()))[0]
    # Old USENIX pages retain HTTP links and paths from before /legacy existed.
    parts = urlsplit(url)
    if parts.hostname in {"www.usenix.org", "usenix.org", "static.usenix.org"}:
        path = parts.path
        if path.startswith(("/events/", "/event/", "/publications/library/")):
            path = "/legacy" + path
        url = parts._replace(scheme="https", netloc="www.usenix.org", path=path).geturl()
    return url


def file_format(url: str) -> str:
    path = urlsplit(url).path.lower()
    return next((ext for ext in FORMATS if path.endswith(ext)), "")


class Client:
    """Per-thread sessions with a shared request rate limit and bounded retries."""

    def __init__(self, timeout: float = 45, retries: int = 3, delay: float = 0.3):
        self.timeout, self.retries, self.delay = timeout, retries, delay
        self.local = threading.local()
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.next_request = 0.0

    def get(self, url: str) -> requests.Response:
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers.update({
                "User-Agent": "OSDI-paper-archiver/1.0 (personal research archive)",
                "Accept-Encoding": "identity",
            })
        for attempt in range(self.retries + 1):
            with self.lock:
                wait = max(0, self.next_request - time.monotonic())
                self.next_request = time.monotonic() + wait + self.delay
            if self.cancelled.wait(wait):
                raise DownloadError("Download interrupted")
            response = None
            try:
                response = self.local.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                expected = response.headers.get("Content-Length")
                if expected and not response.headers.get("Content-Encoding"):
                    if len(response.content) != int(expected):
                        raise DownloadError(f"Truncated response from {url}")
                return response
            except (requests.RequestException, DownloadError) as exc:
                status = response.status_code if response is not None else None
                if status is not None and status < 500 and status not in (408, 429):
                    raise DownloadError(f"HTTP {status}: {url}") from exc
                if attempt == self.retries:
                    raise DownloadError(f"Failed to fetch {url}: {exc}") from exc
                retry_after = response.headers.get("Retry-After", "") if response is not None else ""
                pause = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                if self.cancelled.wait(min(pause, 60)):
                    raise DownloadError("Download interrupted") from exc
        raise AssertionError("unreachable")

    def page(self, url: str) -> tuple[str, BeautifulSoup]:
        response = self.get(url)
        soup = BeautifulSoup(response.content, "html.parser")
        title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
        if any(t in title for t in ("just a moment", "not a bot", "access denied")):
            raise DownloadError(f"The server returned an access challenge: {url}")
        return response.url, soup


def parse_editions(soup: BeautifulSoup, base: str) -> dict[int, str]:
    editions = {}
    for a in soup.select("a[href]"):
        url = absolute_url(base, a["href"])
        match = re.fullmatch(r"/conference/osdi(\d{2}|\d{4})/?", urlsplit(url).path)
        if match:
            n = int(match[1])
            year = n if n >= 1000 else n + (1900 if n >= 94 else 2000)
            editions[year] = url.rstrip("/")
    if not editions or 1994 not in editions:
        raise DownloadError("Could not read the complete OSDI edition list from USENIX")
    return dict(sorted(editions.items()))


def parse_program(year: int, base: str, soup: BeautifulSoup) -> list[Paper]:
    papers: dict[str, Paper] = {}
    if year >= 2012:
        pattern = re.compile(rf"/conference/osdi{year % 100:02d}/(?:technical-sessions/)?presentation/[^/]+/?$")
        for node in soup.select(".node-paper"):
            a = node.select_one("h2 a[href], h3 a[href]")
            if not a:
                continue
            url = absolute_url(base, a["href"])
            title = clean_text(a.get_text(" ", strip=True))
            if not pattern.fullmatch(urlsplit(url).path) or not title:
                continue
            slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            if "keynote" in slug or slug.startswith(("opening-remarks", "award")):
                continue
            papers[url] = Paper(year, title, url)
    elif year in (2008, 2010):
        for a in soup.select("a[href]"):
            url = absolute_url(base, a["href"])
            if "/full_papers/" not in url or file_format(url) != ".pdf":
                continue
            if any(word in PurePosixPath(urlsplit(url).path).name.lower() for word in ("proceedings", "errata")):
                continue
            desc = a.find_previous("p", class_="techdesc" if year == 2008 else "fullpaper1")
            heading = desc.find("b") if desc else None
            if not heading:
                raise DownloadError(f"Cannot find a title for {url}")
            title = clean_text(heading.get_text(" ", strip=True))
            papers[url] = Paper(year, title, base, (url,))
    else:
        for a in soup.select("a[href]"):
            href = a["href"].strip()
            # Research papers in 1994--2006 have local author.html abstract links.
            if not re.fullmatch(r"(?:tech/)?[\w-]+\.html", href):
                continue
            if PurePosixPath(href).stem.lower() in NON_PAPERS:
                continue
            title = clean_text(a.get_text(" ", strip=True))
            if not title:
                continue
            url = absolute_url(base, href)
            papers[url] = Paper(year, title, url)
    return list(papers.values())


def discover_papers(client: Client, year: int, edition_url: str) -> tuple[str, list[Paper]]:
    program = ("https://www.usenix.org/legacy/" + LEGACY_PROGRAMS[year]
               if year in LEGACY_PROGRAMS else edition_url + "/technical-sessions")
    base, soup = client.page(program)
    papers = parse_program(year, base, soup)
    if not papers:
        raise DownloadError(f"No research papers found; proceedings may not be published: {base}")
    return base, papers


def parse_file_urls(base: str, soup: BeautifulSoup, modern: bool) -> list[str]:
    urls = []
    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta and meta.get("content"):
        urls.append(absolute_url(base, meta["content"]))
    if modern:
        links = soup.select(".field-name-field-presentation-pdf a[href], .field-name-field-paper-pdf a[href]")
    else:
        links = soup.select("a[href]")
    for a in links:
        url = absolute_url(base, a["href"])
        label = a.get_text(" ", strip=True).lower()
        if urlsplit(url).scheme not in {"http", "https"} or not file_format(url):
            continue
        if any(word in url.lower() + " " + label for word in ("slides", ".talk.", "preface", "tech report")):
            continue
        if modern or "/full_papers/" in url or file_format(url) in (".pdf", ".ps", ".ps.gz", ".ps.z"):
            urls.append(url)
    # Prefer PDF, then PostScript, then complete HTML, then plain text. Prefer
    # publisher-hosted copies over old author home pages within each format.
    order = {ext: i for i, ext in enumerate(FORMATS)}
    return sorted(dict.fromkeys(urls), key=lambda u: (
        order[file_format(u)], urlsplit(u).hostname != "www.usenix.org"
    ))


def paper_stem(paper: Paper) -> str:
    title = unicodedata.normalize("NFKD", paper.title).encode("ascii", "ignore").decode()
    title = re.sub(r"[^\w .()-]+", "_", title)
    title = clean_text(title).strip(" ._")[:140].rstrip(" .") or "paper"
    identity = paper.file_urls[0] if paper.file_urls else paper.page_url
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:10]
    return f"{title} - {suffix}"


def validate_document(data: bytes, ext: str) -> None:
    if ext == ".pdf":
        valid = b"%PDF-" in data[:1024] and b"%%EOF" in data[-4096:]
    elif ext == ".ps":
        valid = data.lstrip().startswith(b"%!")
    elif ext == ".ps.gz":
        try:
            valid = gzip.decompress(data).lstrip().startswith(b"%!")
        except (OSError, EOFError):
            valid = False
    elif ext == ".ps.z":
        valid = data.startswith(b"\x1f\x9d")
    else:
        valid = bool(data.strip()) and not data.lstrip().lower().startswith((b"<!doctype html", b"<html"))
    if not valid:
        raise DownloadError(f"Invalid or incomplete {ext} document (possibly an HTML error page)")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def file_record(path: Path, directory: Path) -> dict:
    data = path.read_bytes()
    return {"path": path.relative_to(directory).as_posix(), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def verified_previous(record: dict, directory: Path) -> bool:
    if record.get("status") not in ("downloaded", "skipped") or not record.get("files"):
        return False
    for item in record["files"]:
        path = directory / item["path"]
        if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            return False
        data = path.read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            return False
    return True


def download_html(client: Client, url: str, directory: Path) -> tuple[Path, list[Path]]:
    """Mirror a legacy full paper, including its section pages and local figures."""
    root = urljoin(url, ".")
    queue = deque([url])
    seen = set()
    saved = []

    def local_path(address: str) -> Path | None:
        if not address.startswith(root) or urlsplit(address).query:
            return None
        relative = unquote(address[len(root):])
        path = directory / relative
        if not relative or not path.resolve().is_relative_to(directory.resolve()):
            return None
        return path

    while queue:
        address = queue.popleft()
        if address in seen:
            continue
        seen.add(address)
        if len(seen) > 500:
            raise DownloadError(f"Unexpectedly large HTML paper bundle: {url}")
        path = local_path(address)
        if path is None:
            raise DownloadError(f"Invalid HTML resource path: {address}")
        response = client.get(address)
        data = response.content
        if file_format(address) in (".html", ".htm"):
            soup = BeautifulSoup(data, "html.parser")
            if not soup.find(["html", "body"]) or not soup.get_text(strip=True):
                raise DownloadError(f"Invalid full-text HTML: {address}")
            for element in soup.select("script, iframe, base"):
                element.decompose()
            for element in soup.select("a[href], img[src], link[href]"):
                attr = "src" if element.name == "img" else "href"
                href = element.get(attr, "")
                if not href or href.startswith(("#", "mailto:", "javascript:")):
                    continue
                target, fragment = urldefrag(urljoin(address, href))
                target = absolute_url(address, target)
                local = local_path(target)
                is_resource = element.name in ("img", "link") or file_format(target) in (".html", ".htm")
                if local is not None and is_resource:
                    queue.append(target)
                    # Legacy section links are already relative to their page.
                    element[attr] = Path(os.path.relpath(local, path.parent)).as_posix()
                else:
                    element[attr] = target
                if fragment:
                    element[attr] += "#" + fragment
            data = str(soup).encode("utf-8")
        elif not data or "text/html" in response.headers.get("Content-Type", ""):
            raise DownloadError(f"Invalid HTML paper asset: {address}")
        atomic_write(path, data)
        saved.append(path)
    entry = local_path(url)
    assert entry is not None
    return entry, saved


def retrieve_paper(client: Client, paper: Paper, directory: Path, previous: dict,
                   force: bool = False) -> dict:
    record = asdict(paper)
    if not force and verified_previous(previous, directory):
        return {**previous, **record, "status": "skipped"}
    try:
        urls = list(paper.file_urls)
        if not urls:
            base, soup = client.page(paper.page_url)
            urls = parse_file_urls(base, soup, paper.year >= 2012)
        if not urls:
            raise DownloadError("No full-text download link on the paper page")
        errors = []
        for url in urls:
            try:
                ext = file_format(url)
                if ext in (".html", ".htm"):
                    path, files = download_html(client, url, directory / paper_stem(paper))
                else:
                    path = directory / (paper_stem(paper) + (".txt" if ext == ".a" else ext))
                    response = client.get(url)
                    validate_document(response.content, ext)
                    atomic_write(path, response.content)
                    files = [path]
                return {**record, "status": "downloaded", "url": url,
                        "path": path.relative_to(directory).as_posix(),
                        "files": [file_record(f, directory) for f in files]}
            except (DownloadError, OSError) as exc:
                errors.append(f"{url}: {exc}")
        raise DownloadError("; ".join(errors))
    except (DownloadError, OSError) as exc:
        return {**record, "status": "failed", "error": str(exc)}


def paper_key(record: dict) -> str:
    return (record.get("file_urls") or [record["page_url"]])[0]


def save_manifest(path: Path, manifest: dict) -> None:
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write(path, (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode())


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def nonnegative_float(value: str) -> float:
    number = float(value)
    if not 0 <= number < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("papers"), help="output directory (default: papers)")
    parser.add_argument("--years", type=int, nargs="+", help="only these years (default: all editions through this year)")
    parser.add_argument("--list-years", action="store_true", help="list editions from USENIX and exit")
    parser.add_argument("--dry-run", action="store_true", help="list paper titles without fetching paper pages or writing files")
    parser.add_argument("--workers", type=positive_int, default=4, help="concurrent paper downloads (default: 4)")
    parser.add_argument("--delay", type=nonnegative_float, default=0.3, help="minimum seconds between requests, shared by all workers")
    parser.add_argument("--timeout", type=positive_int, default=45, help="HTTP timeout in seconds (default: 45)")
    parser.add_argument("--retries", type=int, default=3, help="retries after transient HTTP/network failures (default: 3)")
    parser.add_argument("--limit-per-year", type=positive_int, help="download only the first N papers per year (for a smoke test)")
    parser.add_argument("--force", action="store_true", help="redownload even verified files")
    args = parser.parse_args(argv)
    if args.retries < 0:
        parser.error("--retries must be nonnegative")
    client = Client(args.timeout, args.retries, args.delay)
    base, soup = client.page(ARCHIVE_URL)
    editions = parse_editions(soup, base)
    if args.list_years:
        for year, url in editions.items():
            print(f"{year}: {url}")
        return 0
    years = sorted(set(args.years)) if args.years else [y for y in editions if y <= date.today().year]
    unknown = set(years) - editions.keys()
    if unknown:
        parser.error(f"No OSDI edition listed for: {', '.join(map(str, sorted(unknown)))}")
    counts: Counter = Counter()
    for year in years:
        print(f"\n{year}: reading proceedings...", flush=True)
        directory = args.output / str(year)
        manifest_path = directory / "manifest.json"
        previous = {}
        if manifest_path.exists() and not args.dry_run:
            try:
                previous = {paper_key(p): p for p in json.loads(manifest_path.read_text())["papers"]}
            except (ValueError, KeyError, TypeError) as exc:
                print(f"  Warning: cannot reuse manifest: {exc}", file=sys.stderr)
        try:
            program_url, papers = discover_papers(client, year, editions[year])
        except DownloadError as exc:
            print(f"  FAILED: {exc}", file=sys.stderr, flush=True)
            counts["failed_years"] += 1
            if not args.dry_run:
                save_manifest(manifest_path, {"year": year, "edition_url": editions[year],
                              "status": "failed", "error": str(exc), "papers": list(previous.values())})
            continue
        total = len(papers)
        selected = papers[:args.limit_per_year] if args.limit_per_year else papers
        print(f"  Found {total} papers; selected {len(selected)}.", flush=True)
        counts["discovered"] += total
        if args.dry_run:
            for paper in selected:
                print(f"  {paper.title}\n    {paper.file_urls[0] if paper.file_urls else paper.page_url}")
            continue
        # Keep unselected records when doing a limited run after a full run.
        records = {paper_key(asdict(p)): {**previous.get(paper_key(asdict(p)), {}), **asdict(p)} for p in papers}
        selected_keys = {paper_key(asdict(p)) for p in selected}
        for key, record in records.items():
            if key in selected_keys:
                record["status"] = "pending"
            else:
                record.setdefault("status", "not_selected")
        manifest = {"year": year, "edition_url": editions[year], "program_url": program_url,
                    "discovered": total, "selected": len(selected), "status": "in_progress",
                    "papers": list(records.values())}
        save_manifest(manifest_path, manifest)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(retrieve_paper, client, paper, directory,
                                   previous.get(paper_key(asdict(paper)), {}), args.force): paper
                       for paper in selected}
            try:
                for index, future in enumerate(as_completed(futures), 1):
                    result = future.result()
                    records[paper_key(result)] = result
                    counts[result["status"]] += 1
                    manifest["papers"] = list(records.values())
                    save_manifest(manifest_path, manifest)
                    print(f"  [{index}/{len(selected)}] {result['status']}: {result['title']}", flush=True)
                    if result["status"] == "failed":
                        print(f"    {result['error']}", file=sys.stderr, flush=True)
            except KeyboardInterrupt:
                client.cancelled.set()
                for future in futures:
                    future.cancel()
                raise
        statuses = Counter(p.get("status") for p in records.values())
        manifest["status"] = ("failed" if statuses["failed"] else "partial" if statuses["not_selected"] else "complete")
        save_manifest(manifest_path, manifest)
    print(f"\nDiscovered {counts['discovered']} papers. Downloaded {counts['downloaded']}; "
          f"skipped {counts['skipped']}; failed {counts['failed']}; failed years {counts['failed_years']}.")
    return 1 if counts["failed"] or counts["failed_years"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command to continue; completed files are retained.", file=sys.stderr)
        sys.exit(130)
    except (DownloadError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
