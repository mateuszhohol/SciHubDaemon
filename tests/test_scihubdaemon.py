"""Offline regression tests for SciHubDaemon.

Deliberately network-free: the download tests run against a throwaway HTTP
server on localhost, so the suite is safe to run in CI and never touches
OpenAlex, a publisher or Sci-Hub.

Run with:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scihubdaemon import (  # noqa: E402
    HEADERS,
    _is_doi_resolver,
    _normalize_pdf_url,
    _save_pdf,
    _trim_doi,
    build_filename,
    extract_dois,
    osf_pdf_url,
)

SICI = "10.1002/(SICI)1097-0193(1999)8:4<194::AID-HBM4>3.0.CO;2-C"


class ExtractDoisTest(unittest.TestCase):
    def test_strips_sentence_period(self):
        self.assertEqual(
            extract_dois("Hohol (2019). https://doi.org/10.4324/9780429056291."),
            ["10.4324/9780429056291"],
        )

    def test_strips_unbalanced_closing_paren(self):
        # The "(doi: ...)" form is extremely common and used to yield a DOI
        # with a trailing ")" that could never be downloaded.
        self.assertEqual(
            extract_dois("(doi: 10.1016/j.cognition.2020.104223)"),
            ["10.1016/j.cognition.2020.104223"],
        )

    def test_strips_paren_and_period_together(self):
        self.assertEqual(
            extract_dois("see (Smith; doi 10.1126/science.abc1234)."),
            ["10.1126/science.abc1234"],
        )

    def test_strips_square_and_angle_brackets(self):
        self.assertEqual(
            extract_dois("[10.3758/s13423-021-01986-x]"), ["10.3758/s13423-021-01986-x"]
        )
        self.assertEqual(extract_dois("<10.1038/nature12373>"), ["10.1038/nature12373"])

    def test_keeps_balanced_parens_inside_sici_doi(self):
        # SICI DOIs legitimately contain parens and a semicolon, so neither may
        # be stripped wholesale.
        self.assertEqual(extract_dois(SICI), [SICI])
        self.assertEqual(extract_dois(f"Zobacz {SICI}, oraz dalej."), [SICI])

    def test_splits_on_comma_with_and_without_space(self):
        expected = ["10.1037/xge0001010", "10.1038/s41586-021-03819-2"]
        self.assertEqual(extract_dois("10.1037/xge0001010, 10.1038/s41586-021-03819-2"), expected)
        self.assertEqual(extract_dois("10.1037/xge0001010,10.1038/s41586-021-03819-2"), expected)

    def test_semicolon_separates_two_dois(self):
        self.assertEqual(extract_dois("10.1234/abc; 10.5678/def"), ["10.1234/abc", "10.5678/def"])

    def test_deduplicates_case_insensitively(self):
        # DOIs are case-insensitive per spec; the first spelling seen wins.
        self.assertEqual(
            extract_dois("10.1016/J.COGNITION.2020.104223 and 10.1016/j.cognition.2020.104223"),
            ["10.1016/J.COGNITION.2020.104223"],
        )

    def test_drops_match_with_nothing_after_slash(self):
        self.assertEqual(extract_dois("10.1234/)"), [])

    def test_finds_dois_across_lines_and_ignores_prose(self):
        text = (
            "1. doi:10.1038/s41562-021-01117-5\n"
            "2. https://dx.doi.org/10.1016/j.cognition.2019.104038\n"
            "3. no doi on this line"
        )
        self.assertEqual(
            extract_dois(text),
            ["10.1038/s41562-021-01117-5", "10.1016/j.cognition.2019.104038"],
        )

    def test_trim_doi_leaves_clean_doi_untouched(self):
        self.assertEqual(_trim_doi("10.1234/abc"), "10.1234/abc")


class UrlRewritingTest(unittest.TestCase):
    def test_dspace_download_route_becomes_rest_endpoint(self):
        # /bitstreams/<uuid>/download is an Angular route that returns the app
        # shell; the REST endpoint behind it serves the file.
        uuid = "1f885885-3cea-4f55-87ba-95a0b7cfc12a"
        self.assertEqual(
            _normalize_pdf_url(f"http://ruj.uj.edu.pl/bitstreams/{uuid}/download"),
            f"http://ruj.uj.edu.pl/server/api/core/bitstreams/{uuid}/content",
        )

    def test_non_dspace_url_is_untouched(self):
        url = "https://www.nature.com/articles/s41598-024-59521-6.pdf"
        self.assertEqual(_normalize_pdf_url(url), url)

    def test_doi_resolver_hosts_are_recognised(self):
        self.assertTrue(_is_doi_resolver("https://doi.org/10.1/x"))
        self.assertTrue(_is_doi_resolver("http://dx.doi.org/10.1/x"))

    def test_publisher_host_is_not_a_resolver(self):
        self.assertFalse(_is_doi_resolver("https://www.nature.com/articles/x.pdf"))


class OsfPreprintTest(unittest.TestCase):
    def test_version_suffix_is_dropped(self):
        # The _vN suffix belongs to the DOI, not to the OSF identifier.
        self.assertEqual(osf_pdf_url("10.31234/osf.io/xw4jr_v1"), "https://osf.io/xw4jr/download")

    def test_doi_without_version_suffix(self):
        self.assertEqual(osf_pdf_url("10.31234/osf.io/5trew"), "https://osf.io/5trew/download")

    def test_non_osf_doi_returns_none(self):
        self.assertIsNone(osf_pdf_url("10.1038/s41598-024-59521-6"))


class BuildFilenameTest(unittest.TestCase):
    def test_uses_metadata_when_available(self):
        meta = {
            "authors": ["Mateusz Hohol"],
            "year": 2019,
            "title": "Foundations of Geometric Cognition",
        }
        self.assertEqual(
            build_filename("10.4324/9780429056291", meta),
            "Hohol_2019_Foundations-of-Geometric-Cognition.pdf",
        )

    def test_falls_back_to_doi_without_metadata(self):
        self.assertEqual(
            build_filename("10.4324/9780429056291", None), "10.4324_9780429056291.pdf"
        )

    def test_falls_back_when_metadata_incomplete(self):
        self.assertEqual(
            build_filename("10.1234/abc", {"authors": [], "year": None, "title": ""}),
            "10.1234_abc.pdf",
        )


ERROR_PAGE = b"<html><body>" + b"<p>CAPTCHA required.</p>" * 80 + b"</body></html>"
REAL_PDF = b"%PDF-1.4\n" + b"padding for a plausible body " * 80 + b"\n%%EOF\n"
TINY_PDF = b"%PDF-1.4\ntiny\n"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        routes = {
            "/error.pdf": (ERROR_PAGE, "text/html"),
            "/real.pdf": (REAL_PDF, "application/pdf"),
            "/tiny.pdf": (TINY_PDF, "application/pdf"),
        }
        body, ctype = routes.get(self.path, (b"not found", "text/html"))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SavePdfTest(unittest.TestCase):
    """A downloaded file is kept only if it is really a PDF."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.dir = tempfile.mkdtemp()

    def _save(self, route):
        path = os.path.join(self.dir, "out.pdf")
        ok = _save_pdf(self.session, f"{self.base}{route}", path, lambda msg: None)
        return ok, os.path.exists(path)

    def test_html_masquerading_as_pdf_is_rejected_and_removed(self):
        # Bigger than 1 KB, so a size-only check would have accepted this.
        self.assertGreater(len(ERROR_PAGE), 1024)
        self.assertEqual(self._save("/error.pdf"), (False, False))

    def test_real_pdf_is_kept(self):
        self.assertEqual(self._save("/real.pdf"), (True, True))

    def test_undersized_pdf_is_rejected_and_removed(self):
        self.assertEqual(self._save("/tiny.pdf"), (False, False))


if __name__ == "__main__":
    unittest.main()
