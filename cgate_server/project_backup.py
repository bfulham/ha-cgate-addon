#!/usr/bin/env python3
"""Create downloadable snapshots of the active C-Gate project."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Iterator
import zipfile

PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REQUIRED_CGATE_TABLES = {"installation", "network", "project", "tagged_entity"}


class ProjectBackupError(RuntimeError):
    """Raised when a current project backup cannot be created."""


def _read_metadata(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _single_live_project(cgate_tag_dir: Path) -> str:
    """Return the only valid live project name, if there is exactly one."""
    try:
        candidates = [
            child.name
            for child in cgate_tag_dir.iterdir()
            if child.is_dir()
            and PROJECT_NAME_RE.fullmatch(child.name)
            and (child / f"{child.name}.db").is_file()
        ]
    except OSError:
        return ""
    return candidates[0] if len(candidates) == 1 else ""


def _resolve_source(
    project_metadata_path: Path,
    project_dir: Path,
    cgate_tag_dir: Path,
) -> tuple[str, Path]:
    metadata = _read_metadata(project_metadata_path)
    active = str(metadata.get("active_project", "") or "")
    projects = metadata.get("projects")
    record = projects.get(active, {}) if active and isinstance(projects, dict) else {}

    if not PROJECT_NAME_RE.fullmatch(active):
        active = _single_live_project(cgate_tag_dir)
        record = {}

    if not active:
        raise ProjectBackupError(
            "No active C-Gate project was found. Upload or select a project first."
        )

    candidates: list[Path] = [cgate_tag_dir / active / f"{active}.db"]

    if isinstance(record, dict):
        stored_filename = str(record.get("stored_filename", "") or "")
        if stored_filename and Path(stored_filename).name == stored_filename:
            candidates.append(project_dir / stored_filename)

    candidates.extend((project_dir / f"{active}.db", project_dir / f"{active}.xml"))

    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return active, candidate
        except OSError:
            continue

    raise ProjectBackupError(
        f"The active project {active} has no readable live or stored project file."
    )


def _snapshot_sqlite(source_path: Path, destination_path: Path) -> None:
    """Use SQLite's online backup API to create a consistent database snapshot."""
    source_uri = f"file:{source_path}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True, timeout=15) as source:
            with sqlite3.connect(destination_path, timeout=15) as destination:
                source.backup(destination)
                quick_check = destination.execute("PRAGMA quick_check").fetchone()
                if not quick_check or str(quick_check[0]).lower() != "ok":
                    raise ProjectBackupError(
                        f"The generated SQLite snapshot failed its integrity check: {quick_check}"
                    )
                tables = {
                    str(row[0])
                    for row in destination.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                missing = _REQUIRED_CGATE_TABLES - tables
                if missing:
                    raise ProjectBackupError(
                        "The live database is missing required C-Gate tables: "
                        + ", ".join(sorted(missing))
                    )
    except ProjectBackupError:
        raise
    except sqlite3.Error as err:
        raise ProjectBackupError(f"Unable to snapshot the live C-Gate database: {err}") from err


@contextmanager
def create_project_backup(
    project_metadata_path: Path,
    project_dir: Path,
    cgate_tag_dir: Path,
) -> Iterator[tuple[Path, str]]:
    """Build a temporary CBZ archive and yield ``(path, download_filename)``.

    A live SQLite project is copied with SQLite's online backup API, so the
    archive remains internally consistent even while C-Gate is running.
    """
    project_name, source_path = _resolve_source(
        project_metadata_path, project_dir, cgate_tag_dir
    )
    suffix = source_path.suffix.lower()
    if suffix not in {".db", ".xml"}:
        raise ProjectBackupError(f"Unsupported active project format: {suffix or 'unknown'}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    download_filename = f"{project_name}_{timestamp}_CGATE.cbz"

    with tempfile.TemporaryDirectory(prefix="cgate-download-") as temporary_dir:
        temporary_path = Path(temporary_dir)
        snapshot_path = temporary_path / f"{project_name}{suffix}"
        archive_path = temporary_path / download_filename

        if suffix == ".db":
            _snapshot_sqlite(source_path, snapshot_path)
        else:
            try:
                shutil.copy2(source_path, snapshot_path)
            except OSError as err:
                raise ProjectBackupError(f"Unable to copy the active XML project: {err}") from err

        try:
            with zipfile.ZipFile(
                archive_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                archive.write(snapshot_path, arcname=snapshot_path.name)
        except (OSError, zipfile.BadZipFile) as err:
            raise ProjectBackupError(f"Unable to create the CBZ backup: {err}") from err

        yield archive_path, download_filename
