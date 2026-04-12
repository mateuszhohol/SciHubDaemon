#!/usr/bin/env python3
"""
SciHubDaemon - Batch DOI extractor and Sci-Hub downloader.

Paste bibliographic descriptions, extract DOIs, download full papers from Sci-Hub.
"""

import re
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --- DOI Extraction ---

DOI_REGEX = re.compile(
    r'\b(10\.\d{4,9}/[^\s,;}\]\"\']+[^\s,;}\]\"\'.])',
    re.IGNORECASE,
)


def extract_dois(text: str) -> list[str]:
    """Extract unique DOIs from text, preserving order."""
    seen = set()
    dois = []
    for match in DOI_REGEX.finditer(text):
        doi = match.group(1).rstrip(".")
        if doi not in seen:
            seen.add(doi)
            dois.append(doi)
    return dois


# --- Sci-Hub Downloader ---

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

# Timeout as (connect, read) tuple — prevents hanging on stalled connections
REQUEST_TIMEOUT = (10, 20)
PDF_DOWNLOAD_TIMEOUT = (10, 30)
# Hard timeout per DOI — kills the attempt if it exceeds this (seconds)
HARD_TIMEOUT_PER_ITEM = 90


def download_paper(doi: str, output_dir: str, scihub_url: str, log_callback=None) -> bool:
    """Download a single paper by DOI from Sci-Hub. Returns True on success."""

    def log(msg):
        if log_callback:
            log_callback(msg)

    session = requests.Session()
    session.headers.update(HEADERS)

    url = f"{scihub_url}/{doi}"
    log(f"  Requesting: {url}")

    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"  ERROR: Could not reach Sci-Hub: {e}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find the PDF embed/iframe/link
    pdf_url = None

    # Method 1: look for embed or iframe with .pdf src
    for tag in soup.find_all(["embed", "iframe"]):
        src = tag.get("src", "")
        if src:
            pdf_url = src
            break

    # Method 2: look for a direct link with .pdf
    if not pdf_url:
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if ".pdf" in href:
                pdf_url = href
                break

    # Method 3: look for onclick with location.href
    if not pdf_url:
        for button in soup.find_all(["button", "div"], onclick=True):
            onclick = button["onclick"]
            match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", onclick)
            if match:
                pdf_url = match.group(1)
                break

    if not pdf_url:
        text_lower = resp.text.lower()
        if "not found" in text_lower or "статья не найдена" in text_lower:
            log("  Article not found on Sci-Hub.")
        else:
            log("  Could not find PDF link on page.")
        return False

    # Fix relative/protocol-relative URLs
    if pdf_url.startswith("//"):
        pdf_url = "https:" + pdf_url
    elif pdf_url.startswith("/"):
        pdf_url = urljoin(scihub_url, pdf_url)

    log(f"  Downloading PDF: {pdf_url[:80]}...")

    try:
        pdf_resp = session.get(pdf_url, timeout=PDF_DOWNLOAD_TIMEOUT, stream=True)
        pdf_resp.raise_for_status()
    except requests.RequestException as e:
        log(f"  ERROR downloading PDF: {e}")
        return False

    # Check that we actually got a PDF
    content_type = pdf_resp.headers.get("Content-Type", "")
    if "pdf" not in content_type and not pdf_url.endswith(".pdf"):
        log(f"  WARNING: Response may not be a PDF (Content-Type: {content_type})")

    # Build filename from DOI
    safe_doi = re.sub(r'[^\w\-.]', '_', doi)
    filename = f"{safe_doi}.pdf"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "wb") as f:
        for chunk in pdf_resp.iter_content(chunk_size=8192):
            f.write(chunk)

    file_size = os.path.getsize(filepath)
    if file_size < 1024:
        log(f"  WARNING: File very small ({file_size} bytes), may not be valid PDF.")
        return False

    log(f"  Saved: {filename} ({file_size / 1024:.0f} KB)")
    return True


# --- GUI Application ---

class SciHubDaemonApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SciHubDaemon")
        self.root.geometry("800x700")
        self.root.minsize(600, 500)

        self.downloading = False
        self.stop_event = threading.Event()
        self.failed_dois = []

        self._build_ui()

    def _build_ui(self):
        # --- Top frame: input ---
        input_frame = ttk.LabelFrame(self.root, text="Bibliographic descriptions (paste here)")
        input_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 5))

        self.text_input = scrolledtext.ScrolledText(input_frame, wrap=tk.WORD, height=12)
        self.text_input.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # --- Middle frame: settings ---
        settings_frame = ttk.Frame(self.root)
        settings_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(settings_frame, text="Sci-Hub URL:").pack(side=tk.LEFT)
        self.scihub_var = tk.StringVar(value=SCIHUB_URLS[0])
        scihub_combo = ttk.Combobox(
            settings_frame, textvariable=self.scihub_var,
            values=SCIHUB_URLS, width=30
        )
        scihub_combo.pack(side=tk.LEFT, padx=(5, 20))

        ttk.Label(settings_frame, text="Output folder:").pack(side=tk.LEFT)
        self.output_var = tk.StringVar(value=os.path.expanduser("~/Downloads/papers"))
        ttk.Entry(settings_frame, textvariable=self.output_var, width=30).pack(side=tk.LEFT, padx=5)
        ttk.Button(settings_frame, text="Browse...", command=self._browse_folder).pack(side=tk.LEFT)

        ttk.Label(settings_frame, text="Delay (s):").pack(side=tk.LEFT, padx=(20, 0))
        self.delay_var = tk.StringVar(value="3")
        ttk.Spinbox(settings_frame, textvariable=self.delay_var, from_=1, to=30, width=4).pack(side=tk.LEFT, padx=5)

        # --- Buttons ---
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)

        self.download_btn = ttk.Button(btn_frame, text="Download All", command=self._start_download)
        self.download_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.retry_btn = ttk.Button(btn_frame, text="Retry Failed", command=self._retry_failed, state=tk.DISABLED)
        self.retry_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self._stop_download, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        self.clear_btn = ttk.Button(btn_frame, text="Clear Log", command=self._clear_log)
        self.clear_btn.pack(side=tk.RIGHT)

        # --- Progress ---
        self.progress_var = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.progress_var).pack(fill=tk.X, padx=10)

        self.progress_bar = ttk.Progressbar(self.root, mode="determinate")
        self.progress_bar.pack(fill=tk.X, padx=10, pady=(0, 5))

        # --- DOI list ---
        doi_frame = ttk.LabelFrame(self.root, text="Extracted DOIs")
        doi_frame.pack(fill=tk.X, padx=10, pady=5)

        self.doi_listbox = tk.Listbox(doi_frame, height=5)
        self.doi_listbox.pack(fill=tk.X, padx=5, pady=5)

        # --- Log ---
        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=10, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.output_var.set(folder)

    def _log(self, msg: str):
        def _append():
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        self.root.after(0, _append)

    def _clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _extract_dois(self):
        text = self.text_input.get("1.0", tk.END)
        dois = extract_dois(text)

        self.doi_listbox.delete(0, tk.END)
        for doi in dois:
            self.doi_listbox.insert(tk.END, doi)

        self._log(f"Extracted {len(dois)} DOI(s).")
        if not dois:
            self._log("No DOIs found. Make sure your text contains DOIs (e.g., 10.1234/...).")
        self.progress_var.set(f"{len(dois)} DOI(s) found")

    def _start_download(self):
        if self.doi_listbox.size() == 0:
            self._extract_dois()
            if self.doi_listbox.size() == 0:
                messagebox.showwarning("No DOIs", "No DOIs found in the input text.")
                return

        dois = [self.doi_listbox.get(i) for i in range(self.doi_listbox.size())]
        self._run_download(dois)

    def _retry_failed(self):
        if not self.failed_dois:
            messagebox.showinfo("Nothing to retry", "No failed downloads to retry.")
            return

        self._log(f"\n{'='*50}")
        self._log(f"RETRYING {len(self.failed_dois)} failed DOI(s)...")
        self._log(f"{'='*50}")

        dois_to_retry = list(self.failed_dois)
        self.failed_dois.clear()

        self.doi_listbox.delete(0, tk.END)
        for doi in dois_to_retry:
            self.doi_listbox.insert(tk.END, doi)

        self._run_download(dois_to_retry)

    def _run_download(self, dois: list[str]):
        output_dir = self.output_var.get()
        os.makedirs(output_dir, exist_ok=True)

        self.downloading = True
        self.stop_event.clear()
        self.download_btn.configure(state=tk.DISABLED)
        self.retry_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)

        thread = threading.Thread(target=self._download_thread, args=(dois, output_dir), daemon=True)
        thread.start()

    def _stop_download(self):
        self.stop_event.set()
        self._log("Stop requested — skipping current item...")

    def _download_thread(self, dois: list[str], output_dir: str):
        total = len(dois)
        success = 0
        failed = 0
        failed_dois = []
        scihub_url = self.scihub_var.get().rstrip("/")
        delay = max(1, int(self.delay_var.get()))

        self.root.after(0, lambda: self.progress_bar.configure(maximum=total, value=0))

        # Use a single-worker pool so we can enforce hard timeouts per item
        executor = ThreadPoolExecutor(max_workers=1)

        for i, doi in enumerate(dois):
            if self.stop_event.is_set():
                failed_dois.extend(dois[i:])
                failed += len(dois) - i
                self._log(f"\nStopped by user after {i}/{total} papers.")
                break

            self._log(f"\n[{i+1}/{total}] DOI: {doi}")
            self.root.after(0, lambda v=i+1: self.progress_var.set(f"Downloading {v}/{total}..."))

            # Submit download to executor with hard timeout
            future = executor.submit(
                download_paper, doi, output_dir, scihub_url, log_callback=self._log
            )

            try:
                ok = future.result(timeout=HARD_TIMEOUT_PER_ITEM)
            except FuturesTimeoutError:
                future.cancel()
                self._log(f"  TIMEOUT: Download exceeded {HARD_TIMEOUT_PER_ITEM}s — skipping.")
                ok = False
            except Exception as e:
                self._log(f"  UNEXPECTED ERROR: {e}")
                ok = False

            if ok:
                success += 1
            else:
                failed += 1
                failed_dois.append(doi)

            self.root.after(0, lambda v=i+1: self.progress_bar.configure(value=v))

            # Check stop again before delay
            if self.stop_event.is_set():
                remaining = dois[i+1:]
                if remaining:
                    failed_dois.extend(remaining)
                    failed += len(remaining)
                    self._log(f"\nStopped by user after {i+1}/{total} papers.")
                break

            # Delay between requests (interruptible)
            if i < total - 1:
                self._log(f"  Waiting {delay}s before next request...")
                if self.stop_event.wait(timeout=delay):
                    # Stop was requested during delay
                    remaining = dois[i+1:]
                    failed_dois.extend(remaining)
                    failed += len(remaining)
                    self._log(f"\nStopped by user after {i+1}/{total} papers.")
                    break

        executor.shutdown(wait=False)

        self.failed_dois = failed_dois

        self._log(f"\nDone! Success: {success}, Failed: {failed}, Total: {total}")
        self._log(f"Files saved to: {output_dir}")
        if failed_dois:
            self._log(f"\nFailed DOIs ({len(failed_dois)}):")
            for doi in failed_dois:
                self._log(f"  - {doi}")
            self._log('Click "Retry Failed" to try again.')

        def _finish():
            self.progress_var.set(f"Done: {success} downloaded, {failed} failed")
            self.download_btn.configure(state=tk.NORMAL)
            self.stop_btn.configure(state=tk.DISABLED)
            if failed_dois:
                self.retry_btn.configure(state=tk.NORMAL)
            else:
                self.retry_btn.configure(state=tk.DISABLED)

        self.root.after(0, _finish)
        self.downloading = False


def main():
    root = tk.Tk()
    app = SciHubDaemonApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
