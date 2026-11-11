"""Durable download results for a single output folder."""

import csv
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path


@dataclass(frozen=True)
class PaperResult:
    status: str = "Pending"
    title: str = ""
    source: str = ""
    version: str = ""
    url: str = ""
    filename: str = ""
    detail: str = ""


_COLUMNS = ("doi", *(field.name for field in fields(PaperResult)))


def _settings_path():
    if sys.platform == "darwin":
        config = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        config = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "SearchFullPaper" / "settings.json"


def last_output_folder():
    try:
        settings = json.loads(_settings_path().read_text(encoding="utf-8"))
        folder = settings.get("output_folder") if isinstance(settings, dict) else None
        if isinstance(folder, str) and Path(folder).is_dir():
            return folder
    except (OSError, ValueError):
        pass
    return str(Path.home() / "Downloads" / "papers")


def remember_output_folder(folder):
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump({"output_folder": str(folder)}, stream)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class PaperQueue:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.database = self.folder / ".searchfullpaper.sqlite3"

    def _connect(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS papers (
                doi TEXT PRIMARY KEY COLLATE NOCASE, status TEXT NOT NULL,
                title TEXT NOT NULL, source TEXT NOT NULL, version TEXT NOT NULL,
                url TEXT NOT NULL, filename TEXT NOT NULL, detail TEXT NOT NULL
            )"""
        )
        return connection

    def load(self):
        if not self.database.is_file():
            return {}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM papers ORDER BY rowid"
            ).fetchall()
        return {doi: PaperResult(*values) for doi, *values in rows}

    def enqueue(self, dois):
        placeholders = ", ".join("?" for _ in _COLUMNS)
        defaults = tuple(asdict(PaperResult()).values())
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                f"INSERT OR IGNORE INTO papers VALUES ({placeholders})",
                ((doi, *defaults) for doi in dois),
            )

    def save(self, doi, result):
        self.save_many({doi: result})

    def save_many(self, results):
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """INSERT INTO papers (doi, status, title, source, version, url, filename, detail)
                   VALUES (:doi, :status, :title, :source, :version, :url, :filename, :detail)
                   ON CONFLICT (doi) DO UPDATE SET
                       status=excluded.status, title=excluded.title, source=excluded.source,
                       version=excluded.version, url=excluded.url, filename=excluded.filename,
                       detail=excluded.detail""",
                ({"doi": doi, **asdict(result)} for doi, result in results.items()),
            )

    def check_saved_file(self, result):
        if result.status not in {"Downloaded", "Missing file", "File unverified"}:
            return result
        filename = result.filename
        try:
            exists = filename and Path(filename).name == filename and (self.folder / filename).is_file()
        except OSError:
            return replace(result, status="File unverified", detail="Could not check the saved PDF.")
        if exists:
            return result if result.status == "Downloaded" else replace(result, status="Downloaded", detail="")
        return replace(result, status="Missing file", detail="The saved PDF is missing.")

    def count_pdfs(self):
        try:
            return sum(
                entry.suffix.lower() == ".pdf" and entry.is_file()
                for entry in self.folder.iterdir()
            )
        except FileNotFoundError:
            return 0

    def export_csv(self, path, results):
        with open(path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("doi", "title", "source", "version", "status", "path", "url", "detail"))
            for doi, result in results.items():
                values = (
                    doi, result.title, result.source, result.version, result.status,
                    str(self.folder / result.filename) if result.filename else "",
                    result.url, result.detail,
                )
                # Metadata comes from external sources. Prevent formula evaluation in spreadsheets.
                writer.writerow(
                    "'" + value if value.lstrip().startswith(("=", "+", "-", "@"))
                    or value.startswith(("\t", "\r", "\n")) else value
                    for value in values
                )
