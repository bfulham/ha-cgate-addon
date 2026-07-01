#!/usr/bin/env python3
"""Ingress Web UI for managing the C-Gate runtime and Toolkit project."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import sqlite3
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final, Iterator
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET
import zipfile

HOST: Final = "0.0.0.0"
PORT: Final = 8099
CHUNK_BYTES: Final = 8 * 1024 * 1024
MAX_PACKAGE_BYTES: Final = 256 * 1024 * 1024
MAX_PROJECT_BYTES: Final = 64 * 1024 * 1024
MAX_PROJECT_ARCHIVE_ENTRIES: Final = 512

DATA_DIR: Final = Path("/data")
PACKAGE_DIR: Final = DATA_DIR / "packages"
PROJECT_DIR: Final = DATA_DIR / "projects"
UPLOAD_DIR: Final = DATA_DIR / "uploads"
PACKAGE_PATH: Final = PACKAGE_DIR / "cgate-package.zip"
PACKAGE_METADATA_PATH: Final = PACKAGE_DIR / "metadata.json"
INSTALL_MARKER: Final = DATA_DIR / "cgate" / ".installed-package"
BUILD_INFO: Final = DATA_DIR / "cgate" / "BuildInfo.txt"
PROJECT_METADATA_PATH: Final = PROJECT_DIR / "metadata.json"
PROJECT_BACKUP_DIR: Final = PROJECT_DIR / "backups"
CGATE_TAG_DIR: Final = DATA_DIR / "cgate" / "tag"
CGATE_LEGACY_PROJECTS_DIR: Final = DATA_DIR / "cgate" / "Projects"

UPLOAD_ID_RE: Final = re.compile(r"^[0-9a-f]{32}$")
PROJECT_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
REQUIRED_CGATE_TABLES: Final = {"installation", "network", "project", "tagged_entity"}


class ProjectBackupError(RuntimeError):
    """Raised when a current project backup cannot be created."""


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(4)}.tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def _check_zip_archive(
    archive: zipfile.ZipFile, *, nested: bool = True
) -> tuple[bool, str]:
    infos = archive.infolist()
    for info in infos:
        if not _safe_archive_name(info.filename):
            return False, f"Unsafe archive path: {info.filename}"

    if any(
        PurePosixPath(info.filename.replace("\\", "/")).name == "cgate.jar"
        for info in infos
    ):
        return True, "C-Gate runtime found"

    if nested:
        for info in infos:
            if info.is_dir() or not info.filename.lower().endswith(".zip"):
                continue
            if info.file_size <= 0 or info.file_size > MAX_PACKAGE_BYTES:
                continue
            try:
                with archive.open(info) as source, tempfile.SpooledTemporaryFile(
                    max_size=8 * 1024 * 1024
                ) as nested_file:
                    shutil.copyfileobj(source, nested_file, length=1024 * 1024)
                    nested_file.seek(0)
                    with zipfile.ZipFile(nested_file) as nested_archive:
                        found, _ = _check_zip_archive(nested_archive, nested=False)
                        if found:
                            name = PurePosixPath(info.filename).name
                            return True, f"C-Gate runtime found in {name}"
            except (RuntimeError, OSError, zipfile.BadZipFile):
                continue

    return False, "cgate.jar was not found in this archive or its nested ZIP"


def _zip_contains_cgate_path(path: Path) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return _check_zip_archive(archive)
    except zipfile.BadZipFile:
        return False, "The uploaded file is not a valid ZIP archive"


def _project_identity_xml(xml_path: Path) -> dict[str, object]:
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError) as err:
        raise ValueError(f"Invalid Toolkit project XML: {err}") from err

    root = tree.getroot()
    root_name = _local_name(root.tag)
    if root_name == "Project":
        project = root
    elif root_name == "Installation":
        project = next(
            (child for child in root if _local_name(child.tag) == "Project"), None
        )
    else:
        project = None

    if project is None:
        raise ValueError("The XML does not contain a C-Bus Toolkit Project element")

    direct_children: dict[str, ET.Element] = {}
    for child in project:
        direct_children.setdefault(_local_name(child.tag), child)

    address_node = direct_children.get("Address")
    tag_node = direct_children.get("TagName")
    address = (address_node.text or "").strip() if address_node is not None else ""
    tag_name = (tag_node.text or "").strip() if tag_node is not None else ""
    project_name = address or tag_name

    if not project_name:
        raise ValueError("The Toolkit project does not contain a project Address or TagName")
    if not PROJECT_NAME_RE.fullmatch(project_name):
        raise ValueError(
            "The project address must contain only letters, numbers, dot, underscore, or hyphen"
        )

    network_count = sum(1 for child in project if _local_name(child.tag) == "Network")
    return {
        "project_name": project_name,
        "tag_name": tag_name or project_name,
        "network_count": network_count,
        "project_format": "legacy_xml",
        "db_version": "",
    }


def _project_identity_db(db_path: Path) -> dict[str, object]:
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as err:
        raise ValueError(f"Invalid C-Gate project database: {err}") from err

    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if not quick_check or str(quick_check[0]).lower() != "ok":
            raise ValueError(
                f"C-Gate project database integrity check failed: {quick_check}"
            )

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = REQUIRED_CGATE_TABLES - tables
        if missing:
            raise ValueError(
                "SQLite file is not a C-Gate project database (missing: "
                + ", ".join(sorted(missing))
                + ")"
            )

        row = connection.execute(
            """
            SELECT te.address, te.tag_name, te.description
            FROM project AS p
            JOIN tagged_entity AS te ON te.id = p.tagged_entity_id
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise ValueError("The C-Gate database does not contain a project record")

        address = str(row[0] or "").strip()
        tag_name = str(row[1] or "").strip()
        project_name = address or tag_name
        if not project_name:
            raise ValueError("The C-Gate database does not contain a project address or name")
        if not PROJECT_NAME_RE.fullmatch(project_name):
            raise ValueError(
                "The project address must contain only letters, numbers, dot, underscore, or hyphen"
            )

        network_count = int(
            connection.execute("SELECT COUNT(*) FROM network").fetchone()[0]
        )
        version_row = connection.execute(
            "SELECT db_version FROM installation LIMIT 1"
        ).fetchone()
        db_version = str(version_row[0] or "") if version_row else ""

        return {
            "project_name": project_name,
            "tag_name": tag_name or project_name,
            "network_count": network_count,
            "project_format": "sqlite",
            "db_version": db_version,
            "description": str(row[2] or ""),
        }
    except sqlite3.DatabaseError as err:
        raise ValueError(f"Invalid C-Gate project database: {err}") from err
    finally:
        connection.close()


def _extract_toolkit_project(
    upload_path: Path, original_filename: str, work_dir: Path
) -> tuple[Path, dict[str, object], str]:
    suffix = Path(original_filename).suffix.lower()

    if suffix == ".db":
        candidate = work_dir / "project.db"
        shutil.copy2(upload_path, candidate)
        return candidate, _project_identity_db(candidate), ".db"

    if suffix == ".xml":
        candidate = work_dir / "project.xml"
        shutil.copy2(upload_path, candidate)
        return candidate, _project_identity_xml(candidate), ".xml"

    if suffix != ".cbz":
        raise ValueError(
            "Select a Toolkit .cbz backup, C-Gate .db project, or legacy .xml project"
        )

    try:
        archive = zipfile.ZipFile(upload_path)
    except zipfile.BadZipFile as err:
        raise ValueError("The uploaded CBZ is not a valid ZIP archive") from err

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_PROJECT_ARCHIVE_ENTRIES:
            raise ValueError("The Toolkit backup contains too many archive entries")

        candidates: list[tuple[Path, dict[str, object], str]] = []
        for wanted_suffix, identity_reader in (
            (".db", _project_identity_db),
            (".xml", _project_identity_xml),
        ):
            for index, info in enumerate(infos):
                if not _safe_archive_name(info.filename):
                    raise ValueError(f"Unsafe archive path: {info.filename}")
                if info.is_dir() or not info.filename.lower().endswith(wanted_suffix):
                    continue
                if info.file_size <= 0 or info.file_size > MAX_PROJECT_BYTES:
                    continue

                candidate = work_dir / f"candidate-{index}{wanted_suffix}"
                with archive.open(info) as source, candidate.open("wb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)

                try:
                    identity = identity_reader(candidate)
                except ValueError:
                    candidate.unlink(missing_ok=True)
                    continue
                candidates.append((candidate, identity, wanted_suffix))

            if candidates:
                break

        if not candidates:
            raise ValueError(
                "No valid C-Gate project database or legacy Toolkit project XML was found inside the CBZ"
            )
        if len(candidates) > 1:
            names = ", ".join(str(item[1]["project_name"]) for item in candidates)
            raise ValueError(
                f"The CBZ contains multiple C-Bus projects ({names}); upload one project at a time"
            )
        return candidates[0]


def _store_project(upload_path: Path, original_filename: str) -> dict[str, object]:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    PROJECT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cgate-project-") as temporary_dir:
        project_file, identity, extension = _extract_toolkit_project(
            upload_path, original_filename, Path(temporary_dir)
        )
        project_name = str(identity["project_name"])
        stored_filename = f"{project_name}{extension}"
        stored_path = PROJECT_DIR / stored_filename

        for previous in (
            PROJECT_DIR / f"{project_name}.db",
            PROJECT_DIR / f"{project_name}.xml",
        ):
            if previous.exists():
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_path = (
                    PROJECT_BACKUP_DIR / f"{project_name}-{timestamp}{previous.suffix}"
                )
                shutil.copy2(previous, backup_path)
                if previous != stored_path:
                    previous.unlink(missing_ok=True)

        _atomic_copy(project_file, stored_path)

    metadata = _read_json(PROJECT_METADATA_PATH)
    projects = metadata.get("projects")
    if not isinstance(projects, dict):
        projects = {}

    project_record: dict[str, object] = {
        "project_name": project_name,
        "tag_name": identity["tag_name"],
        "network_count": identity["network_count"],
        "project_format": identity["project_format"],
        "db_version": identity.get("db_version", ""),
        "source_filename": Path(original_filename).name,
        "stored_filename": stored_filename,
        "size_bytes": stored_path.stat().st_size,
        "sha256": _sha256(stored_path),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "pending_apply": True,
    }
    projects[project_name] = project_record
    _atomic_write_json(
        PROJECT_METADATA_PATH,
        {"active_project": project_name, "projects": projects},
    )
    return project_record


def _single_live_project() -> str:
    try:
        candidates = [
            child.name
            for child in CGATE_TAG_DIR.iterdir()
            if child.is_dir()
            and PROJECT_NAME_RE.fullmatch(child.name)
            and (child / f"{child.name}.db").is_file()
        ]
    except OSError:
        return ""
    return candidates[0] if len(candidates) == 1 else ""


def _resolve_backup_source() -> tuple[str, Path]:
    metadata = _read_json(PROJECT_METADATA_PATH)
    active = str(metadata.get("active_project", "") or "")
    projects = metadata.get("projects")
    record = projects.get(active, {}) if active and isinstance(projects, dict) else {}

    if not PROJECT_NAME_RE.fullmatch(active):
        active = _single_live_project()
        record = {}

    if not active:
        raise ProjectBackupError(
            "No active C-Gate project was found. Upload or select a project first."
        )

    candidates: list[Path] = [CGATE_TAG_DIR / active / f"{active}.db"]
    if isinstance(record, dict):
        stored_filename = str(record.get("stored_filename", "") or "")
        if stored_filename and Path(stored_filename).name == stored_filename:
            candidates.append(PROJECT_DIR / stored_filename)
    candidates.extend((PROJECT_DIR / f"{active}.db", PROJECT_DIR / f"{active}.xml"))

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
    try:
        with sqlite3.connect(
            f"file:{source_path}?mode=ro", uri=True, timeout=15
        ) as source, sqlite3.connect(destination_path, timeout=15) as destination:
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
            missing = REQUIRED_CGATE_TABLES - tables
            if missing:
                raise ProjectBackupError(
                    "The live database is missing required C-Gate tables: "
                    + ", ".join(sorted(missing))
                )
    except ProjectBackupError:
        raise
    except sqlite3.Error as err:
        raise ProjectBackupError(
            f"Unable to snapshot the live C-Gate database: {err}"
        ) from err


@contextmanager
def _create_project_backup() -> Iterator[tuple[Path, str]]:
    project_name, source_path = _resolve_backup_source()
    suffix = source_path.suffix.lower()
    if suffix not in {".db", ".xml"}:
        raise ProjectBackupError(
            f"Unsupported active project format: {suffix or 'unknown'}"
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{project_name}_{timestamp}_CGATE.cbz"

    with tempfile.TemporaryDirectory(prefix="cgate-download-") as temporary_dir:
        temporary_path = Path(temporary_dir)
        snapshot_path = temporary_path / f"{project_name}{suffix}"
        archive_path = temporary_path / filename

        if suffix == ".db":
            _snapshot_sqlite(source_path, snapshot_path)
        else:
            try:
                shutil.copy2(source_path, snapshot_path)
            except OSError as err:
                raise ProjectBackupError(
                    f"Unable to copy the active XML project: {err}"
                ) from err

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

        yield archive_path, filename


def _cgate_listening() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 20023), timeout=0.25):
            return True
    except OSError:
        return False


def _build_info() -> str:
    try:
        text = BUILD_INFO.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return " ".join(line.strip() for line in text.splitlines() if line.strip())[:300]


def _package_status_html() -> tuple[str, str, bool]:
    metadata = _read_json(PACKAGE_METADATA_PATH)
    uploaded = PACKAGE_PATH.is_file()
    current_hash = _sha256(PACKAGE_PATH) if uploaded else ""
    try:
        installed_hash = INSTALL_MARKER.read_text(encoding="utf-8").strip()
    except OSError:
        installed_hash = ""

    if uploaded and current_hash == installed_hash:
        state = "Installed"
    elif uploaded and installed_hash:
        state = "Restart app to apply"
    elif uploaded:
        state = "Ready to install"
    else:
        state = "Not uploaded"

    filename = html.escape(str(metadata.get("original_filename", "C-Gate package")))
    size = int(metadata.get("size_bytes", 0) or 0)
    detail = (
        f"{filename} ({size / 1024 / 1024:.1f} MB)"
        if uploaded and size
        else "No package stored"
    )
    return state, detail, uploaded


def _project_status_html() -> tuple[str, str, bool]:
    metadata = _read_json(PROJECT_METADATA_PATH)
    active = str(metadata.get("active_project", "") or "")
    projects = metadata.get("projects")
    record = projects.get(active, {}) if active and isinstance(projects, dict) else {}

    stored_filename = (
        str(record.get("stored_filename", "") or "") if isinstance(record, dict) else ""
    )
    stored_path = PROJECT_DIR / stored_filename if stored_filename else None
    stored_exists = bool(stored_path and stored_path.is_file())
    live_exists = bool(
        active
        and PROJECT_NAME_RE.fullmatch(active)
        and (CGATE_TAG_DIR / active / f"{active}.db").is_file()
    )

    if stored_exists or live_exists:
        pending = bool(record.get("pending_apply", False)) if isinstance(record, dict) else False
        state = "Restart required" if pending else "Installed"
        tag = html.escape(str(record.get("tag_name", active)) if isinstance(record, dict) else active)
        source = html.escape(
            str(record.get("source_filename", "Live C-Gate project"))
            if isinstance(record, dict)
            else "Live C-Gate project"
        )
        networks = int(record.get("network_count", 0) or 0) if isinstance(record, dict) else 0
        project_format = (
            str(record.get("project_format", "sqlite")) if isinstance(record, dict) else "sqlite"
        )
        format_label = "C-Gate 3 SQLite" if project_format == "sqlite" else "legacy XML"
        db_version = str(record.get("db_version", "") or "") if isinstance(record, dict) else ""
        version_text = f" DB {html.escape(db_version)}" if db_version else ""
        network_text = f" — {networks} networks" if networks else ""
        detail = (
            f"{tag} / {html.escape(active)}{network_text} — "
            f"{format_label}{version_text} — {source}"
        )
        return state, detail, True

    live_project = _single_live_project()
    if live_project:
        return "Installed", f"{html.escape(live_project)} — live C-Gate SQLite project", True

    return "Not uploaded", "No Toolkit project stored", False


def _render_page(message: str = "", error: bool = False) -> bytes:
    package_state, package_detail, package_uploaded = _package_status_html()
    project_state, project_detail, project_available = _project_status_html()

    if _cgate_listening():
        runtime_state = "Running"
    elif INSTALL_MARKER.exists():
        runtime_state = "Installed, not listening"
    else:
        runtime_state = "Waiting for package"

    build = html.escape(_build_info())
    notice = ""
    if message:
        css_class = "notice error" if error else "notice success"
        notice = f'<div class="{css_class}">{html.escape(message)}</div>'

    project_actions = ""
    if project_available:
        project_actions = """
        <div class="actions">
          <a class="button secondary" href="./project/backup">Download current backup</a>
          <form method="post" action="./project/delete">
            <button class="danger" type="submit">Remove uploaded project</button>
          </form>
        </div>"""

    package_actions = ""
    if package_uploaded:
        package_actions = """
        <form method="post" action="./package/delete">
          <button class="danger" type="submit">Delete stored package ZIP</button>
        </form>"""

    build_row = f"<dt>Build</dt><dd>{build}</dd>" if build else ""

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>C-Gate Server</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; background: #101418; color: #e8edf2; }}
    main {{ max-width: 880px; margin: 0 auto; padding: 24px 18px 48px; }}
    h1 {{ margin: 0 0 8px; }}
    h2 {{ margin-top: 0; }}
    .lead {{ color: #b9c3cd; margin-bottom: 22px; }}
    .card {{ background: #1a2026; border: 1px solid #323b44; border-radius: 12px; padding: 20px; margin: 16px 0; }}
    dl {{ display: grid; grid-template-columns: minmax(150px, 220px) 1fr; gap: 8px 16px; margin: 0; }}
    dt {{ color: #9eabb7; }} dd {{ margin: 0; overflow-wrap: anywhere; }}
    label {{ display: block; margin: 12px 0 6px; }}
    input[type=file] {{ display: block; width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #47525d; border-radius: 7px; background: #11161b; color: inherit; }}
    .check {{ display: flex; align-items: flex-start; gap: 8px; }}
    .check input {{ margin-top: 4px; }}
    button, .button {{ display: inline-block; border: 0; border-radius: 7px; padding: 10px 15px; font-weight: 650; cursor: pointer; text-decoration: none; background: #03a9f4; color: #001018; }}
    button:disabled {{ opacity: .55; cursor: not-allowed; }}
    .secondary {{ background: #d9e2ea; color: #111820; }}
    .danger {{ background: #df6671; color: #180306; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    .actions form {{ margin: 0; }}
    .notice {{ border-radius: 8px; padding: 12px 14px; margin: 16px 0; }}
    .success {{ background: #173c2b; border: 1px solid #2e8059; }}
    .error {{ background: #482229; border: 1px solid #a54e5b; }}
    .hint {{ color: #aeb9c4; font-size: .93rem; line-height: 1.45; }}
    .progress {{ min-height: 1.4em; margin: 10px 0; color: #a9dfff; }}
    code {{ background: #0d1115; border-radius: 4px; padding: 1px 5px; }}
    @media (max-width: 620px) {{ dl {{ grid-template-columns: 1fr; gap: 3px; }} dd {{ margin-bottom: 8px; }} }}
  </style>
</head>
<body>
<main>
  <h1>C-Gate Server</h1>
  <p class="lead">Manage the Schneider C-Gate runtime and C-Bus Toolkit project stored by this Home Assistant app.</p>
  {notice}

  <section class="card">
    <h2>Status</h2>
    <dl>
      <dt>C-Gate</dt><dd>{html.escape(runtime_state)}</dd>
      <dt>Runtime package</dt><dd>{html.escape(package_state)}</dd>
      <dt>Package file</dt><dd>{package_detail}</dd>
      <dt>Toolkit project</dt><dd>{html.escape(project_state)}</dd>
      <dt>Project details</dt><dd>{project_detail}</dd>
      {build_row}
    </dl>
  </section>

  <section class="card">
    <h2>Toolkit project</h2>
    <form id="project-form">
      <label for="project-file">Toolkit project file</label>
      <input id="project-file" type="file" accept=".cbz,.db,.xml" required>
      <p class="hint">Accepted: Toolkit <code>.cbz</code>, C-Gate 3 <code>.db</code>, or legacy <code>.xml</code>. Restart the app after uploading.</p>
      <div id="project-progress" class="progress"></div>
      <button id="project-submit" type="submit">Upload project</button>
    </form>
    {project_actions}
  </section>

  <section class="card">
    <h2>C-Gate package</h2>
    <form id="package-form">
      <label for="package-file">Official C-Gate Linux ZIP</label>
      <input id="package-file" type="file" accept=".zip" required>
      <label class="check"><input id="package-eula" type="checkbox" required><span>I obtained this package from Schneider Electric or an authorised source and accept the licence included in it.</span></label>
      <p class="hint">Maximum size 256 MB. Uploads are sent in 8 MB chunks for Home Assistant ingress compatibility.</p>
      <div id="package-progress" class="progress"></div>
      <button id="package-submit" type="submit">Upload package</button>
    </form>
    <div class="actions">{package_actions}</div>
  </section>
</main>
<script>
const CHUNK_SIZE = {CHUNK_BYTES};

async function jsonRequest(url, options) {{
  const response = await fetch(url, options);
  let payload = {{}};
  try {{ payload = await response.json(); }} catch (_) {{}}
  if (!response.ok || payload.error) {{
    throw new Error(payload.error || `Request failed: ${{response.status}}`);
  }}
  return payload;
}}

async function uploadFile(kind, file, acceptEula, progress) {{
  progress.textContent = "Preparing upload…";
  const start = await jsonRequest(`./${{kind}}/start`, {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{filename: file.name, size: file.size, accept_eula: acceptEula}})
  }});
  const chunkSize = start.chunk_size || CHUNK_SIZE;
  let offset = 0;
  while (offset < file.size) {{
    const chunk = file.slice(offset, Math.min(offset + chunkSize, file.size));
    const response = await jsonRequest(`./${{kind}}/chunk?id=${{encodeURIComponent(start.upload_id)}}&offset=${{offset}}`, {{
      method: "POST",
      headers: {{"Content-Type": "application/octet-stream"}},
      body: chunk
    }});
    offset = response.received;
    progress.textContent = `Uploading… ${{Math.round(offset / file.size * 100)}}%`;
  }}
  const finish = await jsonRequest(`./${{kind}}/finish`, {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{upload_id: start.upload_id}})
  }});
  progress.textContent = finish.message || "Upload complete";
  setTimeout(() => location.reload(), 900);
}}

function setupUploader(kind) {{
  const form = document.getElementById(`${{kind}}-form`);
  const fileInput = document.getElementById(`${{kind}}-file`);
  const progress = document.getElementById(`${{kind}}-progress`);
  const button = document.getElementById(`${{kind}}-submit`);
  form.addEventListener("submit", async event => {{
    event.preventDefault();
    const file = fileInput.files[0];
    if (!file) return;
    const acceptEula = kind !== "package" || document.getElementById("package-eula").checked;
    button.disabled = true;
    try {{
      await uploadFile(kind, file, acceptEula, progress);
    }} catch (error) {{
      progress.textContent = error.message;
      button.disabled = false;
    }}
  }});
}}
setupUploader("project");
setupUploader("package");
</script>
</body>
</html>
"""
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "CgateUpload/0.4"

    def log_message(self, format_string: str, *args: object) -> None:
        print(
            f"[upload-ui] {self.address_string()} - {format_string % args}",
            flush=True,
        )

    def _send_page(
        self,
        message: str = "",
        error: bool = False,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = _render_page(message, error)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self,
        payload: dict[str, object],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_request(self, limit: int = 64 * 1024) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as err:
            raise ValueError("Invalid Content-Length") from err
        if length <= 0 or length > limit:
            raise ValueError("Invalid request size")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise ValueError("Invalid JSON request") from err
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path.endswith("/project/backup"):
            self._handle_project_backup()
            return
        self._send_page()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        routes = {
            "/package/start": lambda: self._handle_start("package"),
            "/package/chunk": lambda: self._handle_chunk("package"),
            "/package/finish": lambda: self._handle_finish("package"),
            "/package/delete": self._handle_package_delete,
            "/project/start": lambda: self._handle_start("project"),
            "/project/chunk": lambda: self._handle_chunk("project"),
            "/project/finish": lambda: self._handle_finish("project"),
            "/project/delete": self._handle_project_delete,
            "/upload/start": lambda: self._handle_start("package"),
            "/upload/chunk": lambda: self._handle_chunk("package"),
            "/upload/finish": lambda: self._handle_finish("package"),
            "/delete": self._handle_package_delete,
        }
        for suffix, handler in routes.items():
            if path.endswith(suffix):
                handler()
                return
        self._send_page("Unknown action", True, HTTPStatus.NOT_FOUND)

    def _upload_paths(self, upload_id: str) -> tuple[Path, Path]:
        if not UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError("Invalid upload ID")
        return UPLOAD_DIR / f"{upload_id}.part", UPLOAD_DIR / f"{upload_id}.json"

    def _load_upload_state(
        self, upload_id: str, expected_kind: str
    ) -> tuple[Path, Path, dict[str, object]]:
        part_path, state_path = self._upload_paths(upload_id)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as err:
            raise ValueError("Upload session was not found or has expired") from err
        if not isinstance(state, dict) or state.get("kind") != expected_kind:
            raise ValueError("Invalid upload session")
        return part_path, state_path, state

    def _handle_start(self, kind: str) -> None:
        try:
            request = self._read_json_request()
            filename = Path(str(request.get("filename", ""))).name
            size = int(request.get("size", 0))

            if kind == "package":
                if request.get("accept_eula") is not True:
                    raise ValueError("You must accept the included C-Gate licence agreement")
                if not filename.lower().endswith(".zip"):
                    raise ValueError("Select a valid ZIP file")
                maximum = MAX_PACKAGE_BYTES
            else:
                if Path(filename).suffix.lower() not in {".cbz", ".db", ".xml"}:
                    raise ValueError(
                        "Select a Toolkit .cbz, C-Gate .db, or legacy .xml project"
                    )
                maximum = MAX_PROJECT_BYTES

            if size <= 0 or size > maximum:
                raise ValueError(
                    f"Upload is empty or exceeds the {maximum // 1024 // 1024} MB limit"
                )

            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            upload_id = secrets.token_hex(16)
            part_path, state_path = self._upload_paths(upload_id)
            part_path.touch(exist_ok=False)
            state_path.write_text(
                json.dumps(
                    {"kind": kind, "filename": filename, "expected_size": size},
                    indent=2,
                ),
                encoding="utf-8",
            )
            self._send_json({"upload_id": upload_id, "chunk_size": CHUNK_BYTES})
        except (ValueError, OSError) as err:
            self._send_json({"error": str(err)}, HTTPStatus.BAD_REQUEST)

    def _handle_chunk(self, kind: str) -> None:
        query = parse_qs(urlparse(self.path).query)
        upload_id = query.get("id", [""])[0]
        try:
            offset = int(query.get("offset", ["-1"])[0])
            part_path, _state_path, state = self._load_upload_state(upload_id, kind)
            expected_size = int(state.get("expected_size", 0))
            current_size = part_path.stat().st_size
            if offset != current_size:
                raise ValueError(
                    f"Unexpected chunk offset; expected {current_size}, received {offset}"
                )

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError as err:
                raise ValueError("Invalid Content-Length") from err
            if content_length <= 0 or content_length > CHUNK_BYTES:
                raise ValueError("Chunk is empty or exceeds the 8 MB request limit")
            if current_size + content_length > expected_size:
                raise ValueError("Chunk would exceed the declared upload size")

            remaining = content_length
            with part_path.open("ab") as destination:
                while remaining:
                    block = self.rfile.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("Upload chunk ended unexpectedly")
                    destination.write(block)
                    remaining -= len(block)

            self._send_json({"received": part_path.stat().st_size})
        except (ValueError, OSError) as err:
            self._send_json({"error": str(err)}, HTTPStatus.BAD_REQUEST)

    def _handle_finish(self, kind: str) -> None:
        part_path: Path | None = None
        state_path: Path | None = None
        try:
            request = self._read_json_request()
            upload_id = str(request.get("upload_id", ""))
            part_path, state_path, state = self._load_upload_state(upload_id, kind)
            expected_size = int(state.get("expected_size", 0))
            actual_size = part_path.stat().st_size
            if actual_size != expected_size:
                raise ValueError(
                    f"Upload is incomplete: received {actual_size} of {expected_size} bytes"
                )

            original_filename = str(state.get("filename", ""))
            if kind == "package":
                valid, validation_message = _zip_contains_cgate_path(part_path)
                if not valid:
                    raise ValueError(validation_message)
                PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
                os.replace(part_path, PACKAGE_PATH)
                _atomic_write_json(
                    PACKAGE_METADATA_PATH,
                    {
                        "original_filename": original_filename or "cgate-package.zip",
                        "size_bytes": actual_size,
                        "validation": validation_message,
                    },
                )
                message = "Package uploaded successfully. Restart the app to install or apply it."
            else:
                record = _store_project(part_path, original_filename)
                part_path.unlink(missing_ok=True)
                message = (
                    f"Project {record['project_name']} uploaded with "
                    f"{record['network_count']} networks as {record['project_format']}. "
                    "Restart the app so C-Gate installs and loads it."
                )

            state_path.unlink(missing_ok=True)
            self._send_json({"message": message})
        except (ValueError, OSError) as err:
            self._send_json({"error": str(err)}, HTTPStatus.BAD_REQUEST)

    def _handle_project_backup(self) -> None:
        try:
            with _create_project_backup() as (archive_path, filename):
                size = archive_path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{filename}"'
                )
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with archive_path.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
        except ProjectBackupError as err:
            self._send_page(str(err), True, HTTPStatus.BAD_REQUEST)
        except OSError as err:
            self._send_page(
                f"Unable to send the project backup: {err}",
                True,
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _handle_package_delete(self) -> None:
        PACKAGE_PATH.unlink(missing_ok=True)
        PACKAGE_METADATA_PATH.unlink(missing_ok=True)
        self._send_page("Stored package ZIP deleted")

    def _handle_project_delete(self) -> None:
        metadata = _read_json(PROJECT_METADATA_PATH)
        active = str(metadata.get("active_project", "") or "")

        if active and PROJECT_NAME_RE.fullmatch(active):
            projects = metadata.get("projects")
            record = projects.get(active, {}) if isinstance(projects, dict) else {}
            stored_filename = (
                str(record.get("stored_filename", "") or "")
                if isinstance(record, dict)
                else ""
            )
            if stored_filename and Path(stored_filename).name == stored_filename:
                (PROJECT_DIR / stored_filename).unlink(missing_ok=True)
            (PROJECT_DIR / f"{active}.db").unlink(missing_ok=True)
            (PROJECT_DIR / f"{active}.xml").unlink(missing_ok=True)
            shutil.rmtree(CGATE_TAG_DIR / active, ignore_errors=True)
            (CGATE_LEGACY_PROJECTS_DIR / f"{active}.xml").unlink(missing_ok=True)

            if isinstance(projects, dict):
                projects.pop(active, None)
                remaining = sorted(projects)
                metadata["active_project"] = remaining[0] if remaining else ""
                metadata["projects"] = projects
                _atomic_write_json(PROJECT_METADATA_PATH, metadata)
            else:
                PROJECT_METADATA_PATH.unlink(missing_ok=True)
        else:
            live_project = _single_live_project()
            if live_project:
                shutil.rmtree(CGATE_TAG_DIR / live_project, ignore_errors=True)
            PROJECT_METADATA_PATH.unlink(missing_ok=True)

        self._send_page("Uploaded project removed. Restart the app to apply the change.")


def main() -> None:
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[upload-ui] Listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
