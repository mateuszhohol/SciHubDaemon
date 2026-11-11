#!/usr/bin/env python3
"""
SciHubDaemon - Batch DOI extractor and paper downloader.

Paste bibliographic descriptions, extract DOIs, download full papers. Legal
open-access copies are looked up first via OpenAlex, with Sci-Hub as fallback.
"""

VERSION = "2.2.2"

import errno
import os
import queue
import re
import sqlite3
import tempfile
import threading
import time
import webbrowser
from dataclasses import replace
from itertools import chain
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox, simpledialog
from urllib.parse import unquote, urldefrag, urljoin, quote, urlparse

import requests
from bs4 import BeautifulSoup

from paper_queue import PaperQueue, PaperResult, last_output_folder, remember_output_folder

# --- DOI Extraction ---

# Match as much as possible, then trim the tail below. Commas and quotes are
# the only separators excluded up front because they never occur inside a real DOI,
# but ";" ")" "]" and ">" all do (see the SICI format), so they can't be.
DOI_REGEX = re.compile(
    r'\b10\.\d{4,9}/[^\s,\"\']+',
    re.IGNORECASE,
)

# Closing brackets may legitimately appear inside a DOI, as in the old
# Wiley/SICI example 10.1002/(SICI)1097-0193(1999)8:4<194::AID-HBM4>3.0.CO;2-C.
# Strip them only when they have no matching opener.
_CLOSERS = {")": "(", "]": "[", "}": "{", ">": "<"}
# Punctuation that prose glues onto a DOI but is never part of one.
_TRAILING_PUNCT = ".,;:\"'"


def _trim_doi(doi: str) -> str:
    """Strip trailing punctuation the surrounding text glued onto a DOI."""
    while doi:
        last = doi[-1]
        if last in _TRAILING_PUNCT:
            doi = doi[:-1]
        elif last in _CLOSERS:
            if doi.count(_CLOSERS[last]) >= doi.count(last):
                break  # The balanced bracket belongs to the DOI.
            doi = doi[:-1]
        else:
            break
    return doi


def extract_dois(text: str) -> list[str]:
    """Extract unique DOIs from text, preserving order.

    DOIs are case-insensitive per spec, so entries differing only in case are
    collapsed onto the first spelling seen.
    """
    seen = set()
    dois = []
    for match in DOI_REGEX.finditer(text):
        doi = _trim_doi(match.group(0))
        if not doi.partition("/")[2]:
            continue  # nothing left after the slash once trimmed
        key = doi.lower()
        if key not in seen:
            seen.add(key)
            dois.append(doi)
    return dois


# --- Networking ---

SCIHUB_URLS = [
    "https://sci-hub.pl",
    "https://sci-hub.se",
    "https://sci-hub.st",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Connect and read timeouts for stalled connections.
REQUEST_TIMEOUT = (10, 20)
PDF_DOWNLOAD_TIMEOUT = (10, 30)
# One budget for metadata, candidate downloads and fallback.
HARD_TIMEOUT_PER_ITEM = 120
_PUBLISH_LOCK = threading.Lock()
_HTML_LIMIT = 256 * 1024


class DownloadStopped(Exception):
    pass


class DownloadDeadline(Exception):
    pass


class SourceIssue(Exception):
    def __init__(self, status, detail, url=""):
        super().__init__(detail)
        self.status = status
        self.url = url


class DownloadControl:
    def __init__(self, stop_event=None):
        self.stop_event = stop_event
        self.deadline = time.monotonic() + HARD_TIMEOUT_PER_ITEM

    def check(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise DownloadStopped
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadDeadline
        return remaining

    def get(self, session, url, timeout, **kwargs):
        remaining = self.check()
        return session.get(url, timeout=tuple(min(t, remaining) for t in timeout), **kwargs)


def _web_url(url):
    try:
        parsed = urlparse(url or "")
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None)
    except ValueError:
        return False


def _challenge(response, body=b""):
    body = body.lower()
    return response.headers.get("cf-mitigated", "").lower() == "challenge" or any(
        marker in body for marker in (
            b"captcha required", b"verify you are human", b"verify that you are human",
            b"g-recaptcha-response", b"h-captcha-response", b"cf-turnstile-response",
            b"/cdn-cgi/challenge-platform/", b'id="captcha"', b'name="captcha"',
        )
    )


def _check_response(response, body=b"", browser_url=""):
    if _challenge(response, body):
        raise SourceIssue("Verification required", "Complete verification in your browser.",
                          browser_url or response.url)
    if response.status_code in {401, 403}:
        raise SourceIssue("Access denied", f"HTTP {response.status_code}", browser_url or response.url)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "")
        detail = f"HTTP 429. Retry-After: {retry_after}" if retry_after else "HTTP 429"
        raise SourceIssue("Rate limited", detail)
    if response.status_code in {404, 410}:
        raise SourceIssue("Unavailable", f"HTTP {response.status_code}")
    if not response.ok:
        raise SourceIssue("Failed", f"HTTP {response.status_code}")


def _read_preview(chunks, control):
    preview = bytearray()
    for chunk in chunks:
        control.check()
        preview.extend(chunk[:_HTML_LIMIT - len(preview)])
        if len(preview) >= _HTML_LIMIT:
            break
    return bytes(preview)


def _read_signature(chunks, control):
    first = b""
    for chunk in chunks:
        control.check()
        first += chunk
        if len(first) >= 5:
            break
    return first


def _landing_pdf_url(body, page_url, doi):
    soup = BeautifulSoup(body, "html.parser")
    metadata = soup.head or soup
    declared_doi = metadata.find("meta", attrs={"name": re.compile(r"^citation_doi$", re.I)})
    if declared_doi and declared_doi.get("content"):
        identifiers = extract_dois(unquote(declared_doi["content"]))
        if not identifiers or identifiers[0].lower() != doi.lower():
            return ""
    citation = metadata.find("meta", attrs={"name": re.compile(r"^citation_pdf_url$", re.I)})
    alternate = metadata.find("link", rel="alternate", type="application/pdf")
    page = urlparse(page_url)
    for tag, attr in ((citation, "content"), (alternate, "href")):
        if not tag or not tag.get(attr):
            continue
        try:
            url = urldefrag(urljoin(page_url, tag[attr].strip())).url
        except ValueError:
            continue
        if _web_url(url) and url != urldefrag(page_url).url:
            parsed = urlparse(url)
            if parsed.scheme == page.scheme and parsed.netloc == page.netloc:
                return _normalize_pdf_url(url)
    return ""


def _publish_pdf(temporary, destination, control):
    """Publish a complete file and pick a suffix if the name already exists."""
    with _PUBLISH_LOCK:
        control.check()
        number = 1
        candidate = destination
        while True:
            try:
                os.link(temporary, candidate)
            except FileExistsError:
                number += 1
                candidate = destination.with_stem(f"{destination.stem}_{number}")
            except OSError as exc:
                if exc.errno not in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}:
                    raise
                # Filesystems such as exFAT lack hard links. Reserve the name
                # exclusively before replacing it with the complete file.
                try:
                    with candidate.open("xb"):
                        pass
                except FileExistsError:
                    number += 1
                    candidate = destination.with_stem(f"{destination.stem}_{number}")
                    continue
                try:
                    os.replace(temporary, candidate)
                except OSError:
                    candidate.unlink(missing_ok=True)
                    raise
                return candidate
            else:
                return candidate


def _write_pdf(chunks, destination, control):
    destination = Path(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".paper-", suffix=".part", delete=False
        ) as stream:
            temporary = Path(stream.name)
            for chunk in chunks:
                control.check()
                stream.write(chunk)
        with temporary.open("rb") as stream:
            if stream.read(5) != b"%PDF-" or temporary.stat().st_size < 1024:
                raise SourceIssue("Failed", "Invalid or incomplete PDF.")
            stream.seek(-min(4096, temporary.stat().st_size), os.SEEK_END)
            if b"%%EOF" not in stream.read():
                raise SourceIssue("Failed", "The PDF is missing its end marker.")
        return _publish_pdf(temporary, destination, control)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _download_pdf(session, url, destination, control, *, headers=None, browser_url="", discover_doi=""):
    if not _web_url(url):
        raise SourceIssue("Failed", "Invalid download URL.")
    try:
        with control.get(
            session, url, PDF_DOWNLOAD_TIMEOUT, stream=True, headers=headers
        ) as response:
            control.check()
            if _challenge(response):
                _check_response(response, browser_url=browser_url)
            chunks = response.iter_content(chunk_size=8192)
            first = _read_signature(chunks, control)
            if response.ok and first.startswith(b"%PDF-"):
                return _write_pdf(chain((first,), chunks), destination, control)
            preview = _read_preview(chain((first,), chunks), control)
            _check_response(response, preview, browser_url)
            if "html" not in response.headers.get("Content-Type", "").lower() and b"<html" not in preview.lower():
                raise SourceIssue("Failed", "The server did not return a PDF.")
            linked_pdf = _landing_pdf_url(preview, response.url, discover_doi) if discover_doi else ""
            if not linked_pdf:
                raise SourceIssue("Browser download", "Open the source page to find the PDF.",
                                  browser_url or response.url)
            browser_url = browser_url or response.url
    except requests.RequestException:
        control.check()
        raise SourceIssue("Failed", "Network error while downloading PDF.") from None
    return _download_pdf(session, linked_pdf, destination, control, browser_url=browser_url)


def _save_pdf(session, pdf_url: str, filepath: str, log) -> bool:
    """Download and validate a PDF, reporting success to legacy callers."""
    try:
        saved = _download_pdf(session, pdf_url, filepath, DownloadControl())
    except (SourceIssue, DownloadDeadline, OSError) as exc:
        log(f"  Download failed: {exc}")
        return False
    log(f"  Saved: {saved.name}")
    return True


def _import_pdf(source, destination):
    with open(source, "rb") as stream:
        return _write_pdf(iter(lambda: stream.read(65536), b""), destination, DownloadControl())


# --- Open Access Resolver ---

OPENALEX_API = "https://api.openalex.org/works/doi:"
OPENALEX_TIMEOUT = (10, 20)

# DSpace 7 repositories publish /bitstreams/<uuid>/download, but that path is a
# route in their Angular front end: fetching it returns ~900 KB of app shell,
# never the file. The REST API behind it serves the actual PDF.
_DSPACE_BITSTREAM = re.compile(
    r"^(https?://[^/]+)/bitstreams/([0-9a-f-]{36})/download/?$", re.IGNORECASE
)

# OSF-hosted preprints (PsyArXiv and friends) map onto a predictable download
# URL. The version suffix belongs to the DOI, not to the OSF identifier, so
# 10.31234/osf.io/xw4jr_v1 is served at https://osf.io/xw4jr/download.
_OSF_DOI = re.compile(r"^10\.\d{4,9}/osf\.io/([a-z0-9]+?)(?:_v\d+)?$", re.IGNORECASE)

# doi.org links resolve to a publisher landing page, never to a PDF, so they
# are worth keeping as a manual link but never worth downloading.
_RESOLVER_HOSTS = {"doi.org", "dx.doi.org"}


def _normalize_pdf_url(url: str) -> str:
    """Rewrite known front-end routes into the endpoint that serves the file."""
    match = _DSPACE_BITSTREAM.match(url)
    if match:
        return f"{match.group(1)}/server/api/core/bitstreams/{match.group(2)}/content"
    return url


def _is_doi_resolver(url: str) -> bool:
    return urlparse(url).netloc.lower().removeprefix("www.") in _RESOLVER_HOSTS


def osf_pdf_url(doi: str) -> str | None:
    """Direct download URL for an OSF-hosted preprint, if the DOI is one.

    Works even when OpenAlex has no record of the preprint.
    """
    match = _OSF_DOI.match(doi)
    return f"https://osf.io/{match.group(1)}/download" if match else None


def lookup_open_access(doi, session, control, api_key=""):
    """Ask OpenAlex whether a legal free copy of `doi` exists.

    Returns metadata plus `pdf_urls` (direct PDF links, best first) and
    `landing_url`. Returns None when OpenAlex has no record of the DOI.
    """
    try:
        with control.get(
            session, OPENALEX_API + quote(doi, safe="/()"), OPENALEX_TIMEOUT,
            headers=_openalex_auth(api_key),
        ) as response:
            control.check()
            if response.status_code == 404:
                return None
            _check_response(response)
            work = response.json()
    except (requests.RequestException, ValueError):
        control.check()
        raise SourceIssue("Failed", "OpenAlex lookup failed.") from None

    oa = work.get("open_access") or {}
    best = work.get("best_oa_location") or {}

    # Try institutional repositories first. Publisher links can return an HTTP
    # error or a verification page instead of the indexed PDF.
    repositories, journals = [], []
    for loc in work.get("locations") or []:
        if not loc.get("is_oa"):
            continue
        source_type = ((loc.get("source") or {}).get("type") or "").lower()
        (repositories if source_type == "repository" else journals).append(loc)

    candidates = {}
    for location in (*repositories, best, *journals, {"pdf_url": oa.get("oa_url")}):
        candidate = location.get("pdf_url")
        if not _web_url(candidate) or _is_doi_resolver(candidate):
            continue
        candidate = _normalize_pdf_url(candidate)
        if _web_url(candidate):
            source = (location.get("source") or {}).get("display_name") or urlparse(candidate).hostname
            candidates.setdefault(candidate, (
                source, _VERSION_LABELS.get(location.get("version"), ""),
                location.get("landing_page_url") or "",
            ))

    landing_pages = {}
    for location in (*repositories, best, *journals):
        page = location.get("landing_page_url")
        if _web_url(page):
            landing_pages.setdefault(page, (
                (location.get("source") or {}).get("display_name") or urlparse(page).hostname,
                _VERSION_LABELS.get(location.get("version"), ""),
            ))

    return {
        "is_oa": bool(oa.get("is_oa")),
        "candidates": candidates,
        "landing_url": best.get("landing_page_url") or oa.get("oa_url") or "",
        "landing_pages": landing_pages,
        "content_pdf": (work.get("content_urls") or {}).get("pdf") or "",
        "title": work.get("title") or "",
        "year": work.get("publication_year"),
        "authors": [
            a["author"]["display_name"]
            for a in (work.get("authorships") or [])
            if (a.get("author") or {}).get("display_name")
        ],
    }


_VERSION_LABELS = {
    "submittedVersion": "Submitted manuscript",
    "acceptedVersion": "Accepted manuscript",
    "publishedVersion": "Version of record",
}


def _openalex_auth(api_key):
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _openalex_content_url(url):
    if not _web_url(url):
        return False
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == "content.openalex.org"
        and re.fullmatch(r"/works/W[0-9]+\.pdf", parsed.path) is not None
    )


def _doi_url(doi):
    return f"https://doi.org/{quote(doi, safe='/()')}"


def build_filename(doi: str, meta: dict | None) -> str:
    """Readable Author_Year_Title name when metadata allows, else the DOI."""
    if meta and meta.get("authors") and meta.get("year"):
        surname = meta["authors"][0].split()[-1]
        words = re.sub(r"[^\w\s-]", "", meta.get("title", "")).split()
        stem = f"{surname}_{meta['year']}_{'-'.join(words[:8])}".strip("_-")
        stem = re.sub(r"[^\w\-.]", "_", stem)[:120]
        if stem:
            return f"{stem}.pdf"
    stem = re.sub(r"[^\w\-.]", "_", doi)[:120].rstrip(".")
    return f"{stem}.pdf"


# --- Sci-Hub Downloader ---


def _download_from_scihub(doi, session, base_url, destination, control):
    url = f"{base_url}/{quote(doi, safe='/()')}"
    if not _web_url(url):
        raise SourceIssue("Failed", "Invalid Sci-Hub URL.")
    try:
        with control.get(session, url, REQUEST_TIMEOUT, stream=True) as response:
            control.check()
            if _challenge(response):
                _check_response(response)
            chunks = response.iter_content(chunk_size=8192)
            first = _read_signature(chunks, control)
            if response.ok and first.startswith(b"%PDF-"):
                return _write_pdf(chain((first,), chunks), destination, control)
            body = _read_preview(chain((first,), chunks), control)
            _check_response(response, body)
            page_url = response.url
    except requests.RequestException:
        control.check()
        raise SourceIssue("Failed", "Network error while opening the source.") from None

    soup = BeautifulSoup(body, "html.parser")
    links = []
    embeds = []
    for tag in soup.find_all(["embed", "iframe"], src=True):
        src = tag["src"]
        if tag.get("type") == "application/pdf" or ".pdf" in src.lower():
            links.append(src)
        else:
            embeds.append(src)
    links.extend(tag["href"] for tag in soup.find_all("a", href=True) if ".pdf" in tag["href"].lower())
    for button in soup.find_all(["button", "div"], onclick=True):
        match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", button["onclick"])
        if match:
            links.append(match.group(1))
    # Some mirrors embed the article in an endpoint without a .pdf suffix.
    links.extend(embeds)
    candidates = {}
    for link in links:
        try:
            pdf_url = urldefrag(urljoin(page_url, link.strip())).url
        except ValueError:
            continue
        if _web_url(pdf_url) and pdf_url != urldefrag(page_url).url:
            candidates[pdf_url] = None

    if not candidates:
        page_text = soup.get_text(" ", strip=True).lower()
        if "not found" in page_text or "статья не найдена" in page_text:
            raise SourceIssue("Unavailable", "Article unavailable on this source.")
        raise SourceIssue("Browser download", "No PDF link detected.", page_url)

    for pdf_url in list(candidates)[:3]:
        try:
            return _download_pdf(session, pdf_url, destination, control, browser_url=page_url)
        except SourceIssue as exc:
            if exc.status in {"Verification required", "Access denied", "Rate limited"}:
                raise
    raise SourceIssue("Browser download", "No advertised link returned a PDF.", page_url)


# --- Orchestration ---


_RESULT_PRIORITY = {
    "Verification required": 6,
    "Access denied": 5,
    "Rate limited": 4,
    "Browser download": 3,
    "Failed": 2,
    "Unavailable": 1,
}


def fetch_paper(
    doi, output_dir, scihub_url, use_open_access=True, *, api_key="",
    stop_event=None, log_callback=None,
):
    """Find and save one paper, keeping the best actionable result if no PDF is saved."""
    log = log_callback or (lambda message: None)
    control = DownloadControl(stop_event)
    result = PaperResult(status="Unavailable", detail="No downloadable copy found.")
    meta = None

    def remember(issue, source, version="", fallback_url=""):
        nonlocal result
        log(f"  [{source}] {issue}")
        if _RESULT_PRIORITY[issue.status] > _RESULT_PRIORITY.get(result.status, 0):
            url = issue.url or fallback_url
            result = PaperResult(
                status=issue.status, title=meta["title"] if meta else "",
                source=source, version=version, url=url if _web_url(url) else "",
                detail=str(issue),
            )

    try:
        with requests.Session() as session:
            session.headers.update(HEADERS)
            session.max_redirects = 8
            if use_open_access:
                try:
                    meta = lookup_open_access(doi, session, control, api_key)
                except SourceIssue as exc:
                    status = "Failed" if exc.status == "Verification required" else exc.status
                    remember(SourceIssue(status, f"OpenAlex lookup: {exc}"), "OpenAlex")

                candidates = dict(meta["candidates"]) if meta and meta["is_oa"] else {}
                osf_url = osf_pdf_url(doi)
                if osf_url:
                    candidates.setdefault(osf_url, ("OSF", "Preprint", ""))
                if api_key and meta and meta["is_oa"] and _openalex_content_url(meta["content_pdf"]):
                    candidates.setdefault(meta["content_pdf"], (
                        "OpenAlex archive", "", meta["landing_url"] or _doi_url(doi),
                    ))

                title = meta["title"] if meta else ""
                result = replace(result, title=title)
                destination = Path(output_dir) / build_filename(doi, meta)
                landing_pages = meta.get("landing_pages", {}) if meta and meta["is_oa"] else {}
                blocked_hosts = set()
                for url, (source, version, landing_url) in candidates.items():
                    host = urlparse(url).hostname
                    if host in blocked_hosts:
                        continue
                    log(f"  Trying {source}")
                    headers = _openalex_auth(api_key) if _openalex_content_url(url) else None
                    try:
                        saved = _download_pdf(
                            session, url, destination, control,
                            headers=headers, browser_url=landing_url,
                            discover_doi=doi if url in landing_pages else "",
                        )
                    except SourceIssue as exc:
                        remember(exc, source, version, landing_url)
                        if exc.status in {"Verification required", "Access denied", "Rate limited"}:
                            blocked_hosts.add(host)
                        continue
                    return PaperResult(
                        status="Downloaded", title=title, source=source, version=version,
                        url=landing_url or url, filename=saved.name,
                    )

                pages = [
                    (url, source, version) for url, (source, version) in landing_pages.items()
                    if url not in candidates and urlparse(url).hostname not in blocked_hosts
                ]
                for url, source, version in pages[:3]:
                    host = urlparse(url).hostname
                    if host in blocked_hosts:
                        continue
                    log(f"  Checking article page at {source}")
                    try:
                        saved = _download_pdf(
                            session, url, destination, control, browser_url=url, discover_doi=doi,
                        )
                    except SourceIssue as exc:
                        remember(exc, source, version, url)
                        if exc.status in {"Verification required", "Access denied", "Rate limited"}:
                            blocked_hosts.add(host)
                        continue
                    return PaperResult(
                        status="Downloaded", title=title, source=source, version=version,
                        url=url, filename=saved.name,
                    )

                if meta and meta["is_oa"] and _web_url(meta["landing_url"]):
                    remember(SourceIssue(
                        "Browser download", "An open-access source page is available.", meta["landing_url"]
                    ), "Open access")

            if scihub_url:
                log("  Trying Sci-Hub")
                destination = Path(output_dir) / build_filename(doi, meta)
                try:
                    saved = _download_from_scihub(doi, session, scihub_url, destination, control)
                except SourceIssue as exc:
                    remember(exc, "Sci-Hub")
                else:
                    return PaperResult(
                        status="Downloaded", title=meta["title"] if meta else "",
                        source="Sci-Hub", filename=saved.name,
                        url=f"{scihub_url}/{quote(doi, safe='/()')}",
                    )
    except DownloadDeadline:
        if result.status not in {"Verification required", "Browser download"}:
            result = replace(result, status="Timed out", detail="The download exceeded its time budget.")
    except OSError:
        result = replace(result, status="Failed", detail="Could not write to the output folder.")

    return result


def search_crossref(query):
    with requests.get(
        "https://api.crossref.org/works",
        params={"query.bibliographic": query, "rows": 5, "select": "DOI,title,author,issued"},
        timeout=REQUEST_TIMEOUT,
    ) as response:
        response.raise_for_status()
        items = response.json()["message"]["items"]

    matches = []
    for item in items:
        doi = item.get("DOI", "")
        if not DOI_REGEX.fullmatch(doi):
            continue
        title = (item.get("title") or [""])[0]
        first_author = (item.get("author") or [{}])[0].get("family", "")
        date_parts = (item.get("issued") or {}).get("date-parts") or [[]]
        year = str(date_parts[0][0]) if date_parts[0] else ""
        matches.append((doi, BeautifulSoup(title, "html.parser").get_text(), first_author, year))
    return matches


# --- GUI Application ---

_RETRYABLE = {
    "Failed", "Timed out", "Unavailable", "Access denied", "Rate limited", "Missing file",
    "Browser download", "Verification required", "File unverified",
}

_QUEUE_COLUMNS = (
    ("doi", "DOI", 315), ("status", "Status", 155),
    ("source", "Source", 170), ("version", "Version", 150),
)


class SciHubDaemonApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"SciHubDaemon v{VERSION}")
        root.geometry("980x760")
        root.minsize(720, 550)
        root.protocol("WM_DELETE_WINDOW", self._close)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.downloading = False
        self.searching = False
        self.closing = False
        self.active_doi = None
        self.batch_downloaded = 0
        self.store = None
        self.results = {}
        self.sort_column = None
        self.sort_descending = False

        self._build_ui()
        self._update_actions()
        self._load_folder()
        root.bind("<FocusIn>", self._refresh_on_focus, add="+")
        self.event_timer = root.after(60, self._process_events)

    def _build_ui(self):
        input_frame = ttk.LabelFrame(self.root, text="Bibliographic descriptions or DOIs")
        input_frame.pack(fill=tk.X, padx=10, pady=(10, 5))
        self.text_input = scrolledtext.ScrolledText(input_frame, wrap=tk.WORD, height=5)
        self.text_input.pack(fill=tk.X, padx=5, pady=5)

        settings = ttk.Frame(self.root)
        settings.pack(fill=tk.X, padx=10, pady=5)
        settings.columnconfigure(1, weight=1)
        self.output_var = tk.StringVar(value=last_output_folder())
        ttk.Label(settings, text="Output folder").grid(row=0, column=0, sticky="w")
        self.output_entry = ttk.Entry(settings, textvariable=self.output_var)
        self.output_entry.grid(row=0, column=1, padx=5, sticky="ew")
        self.output_entry.bind("<Return>", lambda event: self._load_folder())
        self.output_entry.bind("<FocusOut>", lambda event: self._load_folder())
        self.browse_btn = ttk.Button(settings, text="Browse…", command=self._browse_folder)
        self.browse_btn.grid(row=0, column=2, sticky="w")

        self.scihub_var = tk.StringVar(value=SCIHUB_URLS[0])
        ttk.Label(settings, text="Sci-Hub URL").grid(row=1, column=0, pady=4, sticky="w")
        ttk.Combobox(settings, textvariable=self.scihub_var, values=SCIHUB_URLS).grid(
            row=1, column=1, padx=5, sticky="ew",
        )
        self.delay_var = tk.StringVar(value="3")
        ttk.Label(settings, text="Delay (s)").grid(row=1, column=2, sticky="e")
        ttk.Spinbox(settings, textvariable=self.delay_var, from_=1, to=30, width=5).grid(
            row=1, column=3, padx=5, sticky="w",
        )

        self.oa_var = tk.BooleanVar(value=True)
        self.api_key_var = tk.StringVar(value=os.environ.get("OPENALEX_API_KEY", ""))
        ttk.Label(settings, text="OpenAlex API key").grid(row=2, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.api_key_var, show="*").grid(
            row=2, column=1, padx=5, sticky="ew",
        )
        ttk.Checkbutton(settings, text="Try open access first", variable=self.oa_var).grid(
            row=2, column=2, columnspan=2, sticky="w",
        )

        buttons = ttk.Frame(self.root)
        buttons.pack(fill=tk.X, padx=10, pady=5)
        self.download_btn = ttk.Button(buttons, text="Download All", command=self._start_download)
        self.resume_btn = ttk.Button(buttons, text="Resume Pending", command=self._resume)
        self.retry_btn = ttk.Button(buttons, text="Retry Unresolved", command=self._retry_unresolved)
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self._stop_download)
        self.search_btn = ttk.Button(buttons, text="Find DOI…", command=self._find_doi)
        for button in (self.download_btn, self.resume_btn, self.retry_btn, self.stop_btn, self.search_btn):
            button.pack(side=tk.LEFT, padx=(0, 5))

        self.progress_var = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.progress_var).pack(fill=tk.X, padx=10)
        self.progress_bar = ttk.Progressbar(self.root, mode="determinate")
        self.progress_bar.pack(fill=tk.X, padx=10, pady=(0, 5))

        self.results_frame = ttk.LabelFrame(self.root, text="Download queue")
        self.results_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.table = ttk.Treeview(
            self.results_frame, columns=[column for column, _, _ in _QUEUE_COLUMNS],
            show="headings", height=7, selectmode="browse",
        )
        for column, label, width in _QUEUE_COLUMNS:
            self.table.heading(column, text=label, command=lambda name=column: self._sort_by(name))
            self.table.column(column, width=width, minwidth=80)
        scroll = ttk.Scrollbar(self.results_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.table.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.table.bind("<<TreeviewSelect>>", lambda event: self._update_actions())

        self.detail_var = tk.StringVar()
        ttk.Label(self.root, textvariable=self.detail_var, wraplength=690).pack(
            fill=tk.X, padx=10, pady=(0, 4),
        )
        actions = ttk.Frame(self.root)
        actions.pack(fill=tk.X, padx=10, pady=(0, 5))
        self.open_btn = ttk.Button(actions, text="Open in Browser", command=self._open_selected)
        self.import_btn = ttk.Button(actions, text="Attach PDF…", command=self._attach_pdf)
        self.export_btn = ttk.Button(actions, text="Export CSV…", command=self._export)
        self.open_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.import_btn.pack(side=tk.LEFT, padx=(0, 5))
        self.export_btn.pack(side=tk.RIGHT)

        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill=tk.BOTH, padx=10, pady=(0, 10))
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=5, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        ttk.Button(log_frame, text="Clear Log", command=self._clear_log).pack(
            anchor="e", padx=5, pady=(0, 5),
        )

    def _log(self, message):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.output_var.get(), parent=self.root)
        if folder:
            self.output_var.set(folder)
            if self._load_folder():
                self._remember_folder()

    def _remember_folder(self):
        try:
            remember_output_folder(self.store.folder)
        except OSError as exc:
            self._log(f"Could not remember the output folder: {exc}")

    def _load_folder(self):
        if self.downloading:
            return False
        raw_folder = self.output_var.get().strip()
        if not raw_folder:
            return False
        try:
            folder = Path(raw_folder).expanduser().resolve()
            if self.store and folder == self.store.folder:
                self._refresh_folder_files()
                return True
            store = PaperQueue(folder)
            results = store.load()
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            self._log(f"Could not load queue: {exc}")
            return False
        self.store = store
        self.results = results
        self.table.delete(*self.table.get_children())
        for doi in results:
            self._render_row(doi)
        self._refresh_folder_files()
        pending = sum(result.status == "Pending" for result in results.values())
        self.progress_bar.configure(value=0)
        self.progress_var.set(f"{len(results)} saved entries, {pending} pending" if results else "Ready")
        return True

    def _refresh_on_focus(self, event):
        if event.widget is self.root and not self.downloading and not self.closing:
            self._refresh_folder_files()

    def _refresh_folder_files(self):
        if not self.store:
            return
        for doi, result in self.results.items():
            checked = self.store.check_saved_file(result)
            if checked != result:
                self.results[doi] = checked
                self._render_row(doi)
        try:
            count = self.store.count_pdfs()
        except OSError:
            label = "Download queue (folder count unavailable)"
        else:
            label = f"Download queue ({count} PDFs in folder)"
        self.results_frame.configure(text=label)
        self._apply_sort()
        self._update_actions()

    def _sort_by(self, column):
        self.sort_descending = self.sort_column == column and not self.sort_descending
        self.sort_column = column
        for name, label, _ in _QUEUE_COLUMNS:
            arrow = (" ▼" if self.sort_descending else " ▲") if name == column else ""
            self.table.heading(name, text=label + arrow)
        self._apply_sort()
        selected = self._selected_doi()
        if selected:
            self.table.see(selected)

    def _apply_sort(self):
        if self.sort_column is None:
            return
        values = {
            doi: self.table.set(doi, self.sort_column).casefold()
            for doi in self.results
        }
        present = [doi for doi, value in values.items() if value]
        blank = [doi for doi, value in values.items() if not value]
        present.sort(key=values.__getitem__, reverse=self.sort_descending)
        self.table.set_children("", *present, *blank)

    def _render_row(self, doi):
        result = self.results[doi]
        status = "Downloading" if doi == self.active_doi else result.status
        values = (doi, status, result.source, result.version)
        if self.table.exists(doi):
            self.table.item(doi, values=values)
        else:
            self.table.insert("", tk.END, iid=doi, values=values)

    def _selected_doi(self):
        selection = self.table.selection()
        return selection[0] if selection else None

    def _update_actions(self):
        doi = self._selected_doi()
        result = self.results.get(doi)
        self.detail_var.set("  |  ".join(
            value for value in (result.title, result.filename, result.detail) if value
        ) if result else "")
        idle = not self.downloading and not self.closing
        states = (
            (self.download_btn, idle),
            (self.resume_btn, idle and any(r.status == "Pending" for r in self.results.values())),
            (self.retry_btn, idle and any(r.status in _RETRYABLE for r in self.results.values())),
            (self.stop_btn, self.downloading and not self.stop_event.is_set()),
            (self.browse_btn, idle),
            (self.output_entry, idle),
            (self.search_btn, not self.searching and not self.closing),
            (self.open_btn, result is not None and not self.closing),
            (self.import_btn, result is not None and result.status != "Downloaded"
             and result.status != "Pending" and doi != self.active_doi and not self.closing),
            (self.export_btn, bool(self.results) and not self.closing),
        )
        for widget, enabled in states:
            widget.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_download(self):
        dois = extract_dois(self.text_input.get("1.0", "end-1c"))
        if not dois:
            messagebox.showwarning("No DOIs", "Paste a DOI or use Find DOI to search for a reference.",
                                   parent=self.root)
            return
        self._run_download(dois)

    def _resume(self):
        self._run_download()

    def _retry_unresolved(self):
        self._run_download(retry=True)

    def _run_download(self, input_dois=None, *, retry=False):
        if self.downloading or self.closing:
            return
        if not self._load_folder():
            messagebox.showerror("Output folder", "Choose a valid output folder.", parent=self.root)
            return
        try:
            delay = int(self.delay_var.get())
            if not 1 <= delay <= 30:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid delay", "Enter a whole number between 1 and 30.",
                                 parent=self.root)
            return

        source = self.scihub_var.get().strip().rstrip("/")
        use_oa = self.oa_var.get()
        api_key = self.api_key_var.get().strip()
        if (source and not _web_url(source)) or not (source or use_oa):
            messagebox.showerror("Invalid source", "Enter a valid source URL or enable open access.",
                                 parent=self.root)
            return
        if api_key and any(character.isspace() for character in api_key):
            messagebox.showerror("Invalid API key", "The OpenAlex key must not contain spaces.",
                                 parent=self.root)
            return

        try:
            self.store.folder.mkdir(parents=True, exist_ok=True)
            if input_dois:
                self.store.enqueue(input_dois)
                known = {doi.lower(): doi for doi in self.results}
                for doi in input_dois:
                    if doi.lower() not in known:
                        self.results[doi] = PaperResult()
                        self._render_row(doi)
                dois = [known.get(doi.lower(), doi) for doi in input_dois]
                dois = [doi for doi in dois if self.results[doi].status in _RETRYABLE | {"Pending"}]
            else:
                statuses = _RETRYABLE if retry else {"Pending"}
                dois = [doi for doi, result in self.results.items() if result.status in statuses]

            requeued = {
                doi: replace(self.results[doi], status="Pending", filename="", detail="")
                for doi in dois if self.results[doi].status != "Pending"
            }
            if requeued:
                self.store.save_many(requeued)
                self.results.update(requeued)
                for doi in requeued:
                    self._render_row(doi)
        except (OSError, sqlite3.Error) as exc:
            messagebox.showerror("Could not save queue", str(exc), parent=self.root)
            self._update_actions()
            return

        self._apply_sort()
        if not dois:
            self.progress_var.set("No matching entries to download")
            self._update_actions()
            return
        self._remember_folder()
        self.stop_event.clear()
        self.downloading = True
        self.batch_downloaded = 0
        self.results_frame.configure(text="Download queue")
        self.progress_bar.configure(maximum=len(dois), value=0)
        self._update_actions()
        thread = threading.Thread(
            target=self._download_thread, args=(dois, self.store.folder, source, use_oa, api_key, delay),
            daemon=True,
        )
        thread.start()

    def _download_thread(self, dois, folder, source, use_oa, api_key, delay):
        try:
            for index, doi in enumerate(dois, start=1):
                if self.stop_event.is_set():
                    break
                self.events.put(("started", doi, index, len(dois)))
                try:
                    result = fetch_paper(
                        doi, folder, source, use_oa, api_key=api_key, stop_event=self.stop_event,
                        log_callback=lambda message: self.events.put(("log", message)),
                    )
                except DownloadStopped:
                    break
                except Exception as exc:
                    result = PaperResult(status="Failed", detail=f"Unexpected error: {type(exc).__name__}")
                self.events.put(("result", doi, result, index))
                if index < len(dois) and self.stop_event.wait(delay):
                    break
        finally:
            self.events.put(("finished",))

    def _stop_download(self):
        if not self.downloading:
            return
        with _PUBLISH_LOCK:
            self.stop_event.set()
        self.progress_var.set("Stopping after the current network operation…")
        self._update_actions()

    def _process_events(self):
        self.event_timer = None
        for _ in range(100):
            try:
                event, *data = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "log":
                self._log(data[0])
            elif event == "started":
                doi, index, total = data
                self.active_doi = doi
                self._render_row(doi)
                if self.sort_column == "status":
                    self._apply_sort()
                if not self.stop_event.is_set():
                    self.progress_var.set(f"Downloading {index}/{total}")
                self._log(f"[{index}/{total}] {doi}")
                self._update_actions()
            elif event == "result":
                doi, result, index = data
                self.active_doi = None
                checked = self._record_result(doi, result)
                self.batch_downloaded += checked.status == "Downloaded"
                self.progress_bar.configure(value=index)
            elif event == "finished":
                if self.active_doi:
                    doi, self.active_doi = self.active_doi, None
                    self._render_row(doi)
                self.downloading = False
                self._refresh_folder_files()
                if self.closing:
                    self.root.destroy()
                    return
                finished = int(self.progress_bar["value"])
                pending = sum(result.status == "Pending" for result in self.results.values())
                label = "Stopped" if self.stop_event.is_set() else "Finished"
                self.progress_var.set(
                    f"{label}. {self.batch_downloaded} downloaded, "
                    f"{finished - self.batch_downloaded} unresolved, {pending} pending in folder."
                )
                self._update_actions()
            elif event == "search":
                self.searching = False
                self._update_actions()
                if not self.closing:
                    self._show_matches(*data)
        self.event_timer = self.root.after(60, self._process_events)

    def _record_result(self, doi, result):
        try:
            self.store.save(doi, result)
        except (OSError, sqlite3.Error) as exc:
            self._log(f"Could not persist {doi}: {exc}")
        checked = self.store.check_saved_file(result)
        self.results[doi] = checked
        self._render_row(doi)
        if self.downloading:
            self._apply_sort()
            self._update_actions()
        else:
            self._refresh_folder_files()
        message = checked.filename if checked.status == "Downloaded" else checked.detail
        self._log(f"  {checked.status}: {message}")
        return checked

    def _open_selected(self):
        doi = self._selected_doi()
        if doi:
            result = self.results[doi]
            url = result.url if _web_url(result.url) else _doi_url(doi)
            try:
                opened = webbrowser.open(url)
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                messagebox.showinfo("Open in Browser", url, parent=self.root)

    def _attach_pdf(self):
        doi = self._selected_doi()
        if not doi:
            return
        store = self.store
        source = filedialog.askopenfilename(
            title=f"Attach PDF for {doi}", filetypes=[("PDF files", "*.pdf")], parent=self.root,
        )
        if not source or store is not self.store:
            return
        result = self.results.get(doi)
        if not result or result.status in {"Downloaded", "Pending"} or doi == self.active_doi:
            return
        try:
            saved = _import_pdf(source, store.folder / build_filename(doi, None))
        except (OSError, SourceIssue, DownloadDeadline) as exc:
            messagebox.showerror("Could not attach PDF", str(exc) or "Time limit reached.",
                                 parent=self.root)
            return
        self._record_result(doi, replace(
            result, status="Downloaded", source="Manual import", version="",
            filename=saved.name, detail="",
        ))

    def _export(self):
        store = self.store
        if not store:
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, initialdir=store.folder, initialfile="paper-results.csv",
            defaultextension=".csv", filetypes=[("CSV files", "*.csv")],
        )
        if not path or store is not self.store:
            return
        self._refresh_folder_files()
        try:
            store.export_csv(path, self.results)
        except OSError as exc:
            messagebox.showerror("Could not export CSV", str(exc), parent=self.root)
        else:
            self._log(f"Exported results to {path}")

    def _find_doi(self):
        selection = self.text_input.tag_ranges("sel")
        suggested = self.text_input.get(*selection).strip() if selection else ""
        query = simpledialog.askstring(
            "Find DOI", "Enter the article title or bibliographic reference.",
            initialvalue=suggested, parent=self.root,
        )
        if not query or not query.strip():
            return
        self.searching = True
        self._update_actions()
        self._log("Searching Crossref…")
        threading.Thread(target=self._search_thread, args=(query.strip(),), daemon=True).start()

    def _search_thread(self, query):
        try:
            matches = search_crossref(query)
        except (requests.RequestException, KeyError, ValueError, TypeError, AttributeError) as exc:
            response = getattr(exc, "response", None)
            message = f"Crossref returned HTTP {response.status_code}." if response is not None else (
                "Could not read the Crossref response."
            )
            self.events.put(("search", [], message))
        else:
            self.events.put(("search", matches, ""))

    def _show_matches(self, matches, error):
        if error or not matches:
            messagebox.showinfo("Find DOI", error or "Crossref returned no matches.", parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Choose a Crossref match")
        dialog.transient(self.root)
        dialog.grab_set()
        choices = ttk.Treeview(
            dialog, columns=("title", "author", "year", "doi"),
            show="headings", height=len(matches), selectmode="browse",
        )
        for column, label, width in (
            ("title", "Title", 410), ("author", "First author", 110),
            ("year", "Year", 55), ("doi", "DOI", 205),
        ):
            choices.heading(column, text=label)
            choices.column(column, width=width)
        for doi, title, author, year in matches:
            choices.insert("", tk.END, values=(title, author, year, doi))
        choices.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        choices.focus_set()

        def add(event=None):
            selected = choices.selection()
            if selected:
                doi = choices.item(selected[0], "values")[-1]
                if doi.lower() not in {item.lower() for item in extract_dois(
                    self.text_input.get("1.0", "end-1c")
                )}:
                    self.text_input.insert("end-1c", f"\n{doi}")
                dialog.destroy()

        choices.bind("<Double-1>", add)
        choices.bind("<Return>", add)
        ttk.Button(dialog, text="Add Selected DOI", command=add).pack(pady=(0, 10))

    def _close(self):
        if self.downloading:
            self.closing = True
            self._stop_download()
        else:
            if self.event_timer is not None:
                self.root.after_cancel(self.event_timer)
            self.root.destroy()


def main():
    root = tk.Tk()
    app = SciHubDaemonApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
