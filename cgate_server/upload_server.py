#!/usr/bin/env python3
"""Ingress Web UI for C-Gate runtime and Toolkit project uploads.

Home Assistant ingress may reject individual request bodies larger than 16 MiB.
The browser therefore sends files in small raw chunks. The app assembles them
under /data, validates them, and stores them persistently.
"""

from __future__ import annotations

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
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
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
CGATE_PROJECTS_DIR: Final = DATA_DIR / "cgate" / "Projects"

UPLOAD_ID_RE: Final = re.compile(r"^[0-9a-f]{32}$")
PROJECT_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _safe_archive_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
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


def _check_zip_archive(archive: zipfile.ZipFile, *, nested: bool = True) -> tuple[bool, str]:
    infos = archive.infolist()
    for info in infos:
        if not _safe_archive_name(info.filename):
            return False, f"Unsafe archive path: {info.filename}"

    if any(PurePosixPath(i.filename.replace("\\", "/")).name == "cgate.jar" for i in infos):
        return True, "C-Gate runtime found"

    if nested:
        for info in infos:
            if not info.filename.lower().endswith(".zip") or info.is_dir():
                continue
            if info.file_size > MAX_PACKAGE_BYTES:
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
                        return True, f"C-Gate runtime found in {PurePosixPath(info.filename).name}"
            except (RuntimeError, OSError, zipfile.BadZipFile):
                continue

    return False, "cgate.jar was not found in this archive or its nested ZIP"


def _zip_contains_cgate_path(path: Path) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return _check_zip_archive(archive)
    except zipfile.BadZipFile:
        return False, "The uploaded file is not a valid ZIP archive"


def _project_identity(xml_path: Path) -> dict[str, object]:
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError) as err:
        raise ValueError(f"Invalid Toolkit project XML: {err}") from err

    root = tree.getroot()
    root_name = _local_name(root.tag)
    if root_name == "Project":
        project = root
    elif root_name == "Installation":
        project = next((child for child in root if _local_name(child.tag) == "Project"), None)
    else:
        project = None

    if project is None:
        raise ValueError("The XML does not contain a C-Bus Toolkit Project element")

    direct_children: dict[str, ET.Element] = {}
    for child in project:
        direct_children.setdefault(_local_name(child.tag), child)

    address = (direct_children.get("Address").text or "").strip() if direct_children.get("Address") is not None else ""
    tag_name = (direct_children.get("TagName").text or "").strip() if direct_children.get("TagName") is not None else ""
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
    }


def _extract_toolkit_project(upload_path: Path, original_filename: str, work_dir: Path) -> tuple[Path, dict[str, object]]:
    suffix = Path(original_filename).suffix.lower()
    if suffix == ".xml":
        candidate = work_dir / "project.xml"
        shutil.copy2(upload_path, candidate)
        identity = _project_identity(candidate)
        return candidate, identity

    if suffix != ".cbz":
        raise ValueError("Select a Toolkit .cbz backup or C-Gate project .xml file")

    try:
        archive = zipfile.ZipFile(upload_path)
    except zipfile.BadZipFile as err:
        raise ValueError("The uploaded CBZ is not a valid ZIP archive") from err

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_PROJECT_ARCHIVE_ENTRIES:
            raise ValueError("The Toolkit backup contains too many archive entries")
        candidates: list[tuple[Path, dict[str, object]]] = []
        for index, info in enumerate(infos):
            if not _safe_archive_name(info.filename):
                raise ValueError(f"Unsafe archive path: {info.filename}")
            if info.is_dir() or not info.filename.lower().endswith(".xml"):
                continue
            if info.file_size <= 0 or info.file_size > MAX_PROJECT_BYTES:
                continue
            candidate = work_dir / f"candidate-{index}.xml"
            with archive.open(info) as source, candidate.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            try:
                identity = _project_identity(candidate)
            except ValueError:
                candidate.unlink(missing_ok=True)
                continue
            candidates.append((candidate, identity))

        if not candidates:
            raise ValueError("No valid C-Bus Toolkit project XML was found inside the CBZ")
        if len(candidates) > 1:
            names = ", ".join(str(item[1]["project_name"]) for item in candidates)
            raise ValueError(f"The CBZ contains multiple C-Bus projects ({names}); upload one project at a time")
        return candidates[0]


def _store_project(upload_path: Path, original_filename: str) -> dict[str, object]:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    PROJECT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    CGATE_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cgate-project-") as temporary_dir:
        project_xml, identity = _extract_toolkit_project(
            upload_path, original_filename, Path(temporary_dir)
        )
        project_name = str(identity["project_name"])
        stored_path = PROJECT_DIR / f"{project_name}.xml"
        cgate_path = CGATE_PROJECTS_DIR / f"{project_name}.xml"

        backup_source = cgate_path if cgate_path.exists() else stored_path
        if backup_source.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = PROJECT_BACKUP_DIR / f"{project_name}-{timestamp}.xml"
            shutil.copy2(backup_source, backup_path)

        _atomic_copy(project_xml, stored_path)
        _atomic_copy(stored_path, cgate_path)

    metadata = _read_json(PROJECT_METADATA_PATH)
    projects = metadata.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    project_record: dict[str, object] = {
        "project_name": project_name,
        "tag_name": identity["tag_name"],
        "network_count": identity["network_count"],
        "source_filename": Path(original_filename).name,
        "size_bytes": stored_path.stat().st_size,
        "sha256": _sha256(stored_path),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    projects[project_name] = project_record
    metadata = {"active_project": project_name, "projects": projects}
    _atomic_write_json(PROJECT_METADATA_PATH, metadata)
    return project_record


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
        state = '<span class="pill ok">Installed</span>'
    elif uploaded and installed_hash:
        state = '<span class="pill warn">Restart app to apply</span>'
    elif uploaded:
        state = '<span class="pill warn">Ready to install</span>'
    else:
        state = '<span class="pill muted">Not uploaded</span>'

    filename = html.escape(str(metadata.get("original_filename", "C-Gate package")))
    size = int(metadata.get("size_bytes", 0) or 0)
    detail = f"{filename} ({size / 1024 / 1024:.1f} MB)" if uploaded and size else "No package stored"
    return state, detail, uploaded


def _project_status_html() -> tuple[str, str, bool]:
    metadata = _read_json(PROJECT_METADATA_PATH)
    active = str(metadata.get("active_project", "") or "")
    projects = metadata.get("projects")
    record = projects.get(active, {}) if active and isinstance(projects, dict) else {}
    stored_path = PROJECT_DIR / f"{active}.xml" if active else None
    exists = bool(stored_path and stored_path.is_file())
    if exists:
        state = '<span class="pill ok">Uploaded</span>'
        tag = html.escape(str(record.get("tag_name", active)))
        source = html.escape(str(record.get("source_filename", "Toolkit project")))
        networks = int(record.get("network_count", 0) or 0)
        detail = f"{tag} / {html.escape(active)} — {networks} networks — {source}"
    else:
        state = '<span class="pill muted">Not uploaded</span>'
        detail = "No Toolkit project stored"
    return state, detail, exists


def _render_page(message: str = "", error: bool = False) -> bytes:
    package_state, package_detail, package_uploaded = _package_status_html()
    project_state, project_detail, project_uploaded = _project_status_html()

    if _cgate_listening():
        runtime_state = '<span class="pill ok">Running</span>'
    elif INSTALL_MARKER.exists():
        runtime_state = '<span class="pill warn">Installed, not listening</span>'
    else:
        runtime_state = '<span class="pill muted">Waiting for package</span>'

    build = html.escape(_build_info())
    notice = ""
    if message:
        css_class = "notice error" if error else "notice success"
        notice = f'<div class="{css_class}">{html.escape(message)}</div>'

    package_delete = ""
    if package_uploaded:
        package_delete = """
        <form method="post" action="./package/delete" onsubmit="return confirm('Delete the stored C-Gate package ZIP? The installed runtime will remain.');">
          <button class="danger secondary" type="submit">Delete stored package ZIP</button>
        </form>"""

    project_delete = ""
    if project_uploaded:
        project_delete = """
        <form method="post" action="./project/delete" onsubmit="return confirm('Remove the uploaded Toolkit project from this app and C-Gate?');">
          <button class="danger secondary" type="submit">Remove uploaded project</button>
        </form>"""

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>C-Gate Server</title>
  <style>
    :root {{ color-scheme: light dark; --accent:#03a9f4; --card:#fff; --text:#202124; --muted:#6b7280; --border:#d1d5db; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --card:#1f2937; --text:#f3f4f6; --muted:#9ca3af; --border:#4b5563; }} }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--text); background:transparent; }}
    main {{ max-width:800px; margin:0 auto; padding:24px 16px 48px; }}
    h1 {{ margin:0 0 6px; font-size:28px; }} h2 {{ margin:0 0 12px; font-size:19px; }}
    .sub,.small {{ color:var(--muted); }} .sub {{ margin-bottom:20px; }}
    .card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:18px; margin:14px 0; box-shadow:0 1px 2px rgba(0,0,0,.08); }}
    .row {{ display:flex; justify-content:space-between; gap:18px; align-items:center; padding:8px 0; border-bottom:1px solid var(--border); }}
    .row:last-child {{ border-bottom:0; }} .label {{ color:var(--muted); }}
    .pill {{ display:inline-block; padding:3px 9px; border-radius:999px; font-size:12px; font-weight:600; }}
    .ok {{ background:#dcfce7; color:#166534; }} .warn {{ background:#fef3c7; color:#92400e; }} .muted {{ background:#e5e7eb; color:#374151; }}
    form {{ display:grid; gap:14px; margin:0; }} input[type=file] {{ width:100%; border:1px dashed var(--border); border-radius:8px; padding:14px; }}
    label.check {{ display:flex; gap:10px; align-items:flex-start; }}
    button {{ border:0; border-radius:8px; padding:11px 16px; background:var(--accent); color:white; font-weight:600; cursor:pointer; width:max-content; }}
    button:disabled {{ opacity:.55; cursor:wait; }} button.danger {{ background:#b91c1c; }} button.secondary {{ margin-top:14px; }}
    .notice {{ padding:12px 14px; border-radius:8px; margin:14px 0; }} .success {{ background:#dcfce7; color:#166534; }} .error {{ background:#fee2e2; color:#991b1b; }}
    .progress-wrap {{ display:none; gap:8px; }} .progress-wrap.active {{ display:grid; }} progress {{ width:100%; height:18px; }}
    code {{ overflow-wrap:anywhere; }}
  </style>
</head>
<body><main>
  <h1>C-Gate Server</h1>
  <div class="sub">Manage the Schneider C-Gate runtime and C-Bus Toolkit project stored by this Home Assistant app.</div>
  {notice}<div id="js-notice"></div>

  <section class="card">
    <h2>Status</h2>
    <div class="row"><span class="label">C-Gate</span>{runtime_state}</div>
    <div class="row"><span class="label">Runtime package</span>{package_state}</div>
    <div class="row"><span class="label">Package file</span><span>{package_detail}</span></div>
    <div class="row"><span class="label">Toolkit project</span>{project_state}</div>
    <div class="row"><span class="label">Project details</span><span>{project_detail}</span></div>
    {f'<div class="row"><span class="label">Build</span><span>{build}</span></div>' if build else ''}
  </section>

  <section class="card">
    <h2>Upload Toolkit project</h2>
    <form id="project-form">
      <input id="project-file" type="file" accept=".cbz,.xml,application/zip,application/xml,text/xml" required>
      <button id="project-button" type="submit">Upload project</button>
      <div id="project-progress-wrap" class="progress-wrap"><progress id="project-progress" max="100" value="0"></progress><span id="project-progress-text" class="small">Preparing upload…</span></div>
      <div class="small">Accepted: Toolkit <code>.cbz</code> backup or C-Gate project <code>.xml</code>. The project is validated, backed up when replacing an existing copy, copied into C-Gate's Projects directory, and set as the default project when the app configuration's <code>project_name</code> field is blank. Restart the app after upload to load it.</div>
    </form>
    {project_delete}
  </section>

  <section class="card">
    <h2>Upload C-Gate package</h2>
    <form id="package-form">
      <input id="package-file" type="file" accept=".zip,application/zip" required>
      <label class="check"><input id="accept-eula" type="checkbox" required><span>I obtained this package from Schneider Electric or an authorised source and accept the C-Gate licence agreement included in the package.</span></label>
      <button id="package-button" type="submit">Upload package</button>
      <div id="package-progress-wrap" class="progress-wrap"><progress id="package-progress" max="100" value="0"></progress><span id="package-progress-text" class="small">Preparing upload…</span></div>
      <div class="small">Accepted: Schneider's outer Linux package or inner <code>cgate-*.zip</code>. Maximum size 256 MB. Files are split into 8 MB chunks to stay below the Home Assistant ingress request limit.</div>
    </form>
    {package_delete}
  </section>

<script>
(() => {{
  const notice = document.getElementById('js-notice');
  function showNotice(text, isError) {{
    notice.className = 'notice ' + (isError ? 'error' : 'success');
    notice.textContent = text;
    notice.scrollIntoView({{behavior:'smooth', block:'nearest'}});
  }}
  async function jsonRequest(url, options) {{
    const response = await fetch(url, options);
    let payload = {{}};
    try {{ payload = await response.json(); }} catch (_) {{}}
    if (!response.ok) throw new Error(payload.error || `Request failed (HTTP ${{response.status}})`);
    return payload;
  }}
  function installUploader(kind, requireEula) {{
    const form = document.getElementById(kind + '-form');
    const input = document.getElementById(kind + '-file');
    const button = document.getElementById(kind + '-button');
    const wrap = document.getElementById(kind + '-progress-wrap');
    const progress = document.getElementById(kind + '-progress');
    const text = document.getElementById(kind + '-progress-text');
    const eula = requireEula ? document.getElementById('accept-eula') : null;
    form.addEventListener('submit', async (event) => {{
      event.preventDefault();
      const file = input.files[0];
      if (!file || (eula && !eula.checked)) return;
      button.disabled = true; input.disabled = true; if (eula) eula.disabled = true;
      wrap.classList.add('active'); progress.value = 0; notice.className = ''; notice.textContent = '';
      try {{
        const request = {{filename:file.name, size:file.size}};
        if (requireEula) request.accept_eula = true;
        const start = await jsonRequest(`./${{kind}}/start`, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(request)}});
        let offset = 0;
        while (offset < file.size) {{
          const end = Math.min(offset + start.chunk_size, file.size);
          await jsonRequest(`./${{kind}}/chunk?id=${{encodeURIComponent(start.upload_id)}}&offset=${{offset}}`, {{method:'POST', headers:{{'Content-Type':'application/octet-stream'}}, body:file.slice(offset,end)}});
          offset = end;
          const percent = Math.round((offset / file.size) * 100);
          progress.value = percent;
          text.textContent = `Uploading… ${{percent}}% (${{(offset/1024/1024).toFixed(1)}} of ${{(file.size/1024/1024).toFixed(1)}} MB)`;
        }}
        text.textContent = kind === 'project' ? 'Validating Toolkit project…' : 'Validating C-Gate package…';
        const finish = await jsonRequest(`./${{kind}}/finish`, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{upload_id:start.upload_id}})}});
        progress.value = 100; text.textContent = 'Upload complete'; showNotice(finish.message || 'Upload complete.', false);
        setTimeout(() => window.location.reload(), 1200);
      }} catch (error) {{ text.textContent = 'Upload failed'; showNotice(error.message || String(error), true); }}
      finally {{ button.disabled = false; input.disabled = false; if (eula) eula.disabled = false; }}
    }});
  }}
  installUploader('project', false);
  installUploader('package', true);
}})();
</script>
</main></body></html>"""
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "CgateUpload/0.3"

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"[upload-ui] {self.address_string()} - {format_string % args}", flush=True)

    def _send_page(self, message: str = "", error: bool = False, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = _render_page(message, error)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, limit: int = 64 * 1024) -> dict[str, object]:
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
            # Backward-compatible v0.1.4/v0.1.5 package endpoints.
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

    def _load_upload_state(self, upload_id: str, expected_kind: str) -> tuple[Path, Path, dict[str, object]]:
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
            request = self._read_json()
            filename = Path(str(request.get("filename", ""))).name
            size = int(request.get("size", 0))
            if kind == "package":
                if request.get("accept_eula") is not True:
                    raise ValueError("You must accept the included C-Gate licence agreement")
                if not filename.lower().endswith(".zip"):
                    raise ValueError("Select a valid ZIP file")
                maximum = MAX_PACKAGE_BYTES
            else:
                if Path(filename).suffix.lower() not in {".cbz", ".xml"}:
                    raise ValueError("Select a Toolkit .cbz or project .xml file")
                maximum = MAX_PROJECT_BYTES
            if size <= 0 or size > maximum:
                raise ValueError(f"Upload is empty or exceeds the {maximum // 1024 // 1024} MB limit")

            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            upload_id = secrets.token_hex(16)
            part_path, state_path = self._upload_paths(upload_id)
            part_path.touch(exist_ok=False)
            state_path.write_text(
                json.dumps({"kind": kind, "filename": filename, "expected_size": size}, indent=2),
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
                raise ValueError(f"Unexpected chunk offset; expected {current_size}, received {offset}")
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
            request = self._read_json()
            upload_id = str(request.get("upload_id", ""))
            part_path, state_path, state = self._load_upload_state(upload_id, kind)
            expected_size = int(state.get("expected_size", 0))
            actual_size = part_path.stat().st_size
            if actual_size != expected_size:
                raise ValueError(f"Upload is incomplete: received {actual_size} of {expected_size} bytes")
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
                    f"Project {record['project_name']} uploaded with {record['network_count']} networks and set as the active project. "
                    "Restart the app so C-Gate loads it."
                )

            state_path.unlink(missing_ok=True)
            self._send_json({"message": message})
        except (ValueError, OSError) as err:
            self._send_json({"error": str(err)}, HTTPStatus.BAD_REQUEST)

    def _handle_package_delete(self) -> None:
        PACKAGE_PATH.unlink(missing_ok=True)
        PACKAGE_METADATA_PATH.unlink(missing_ok=True)
        self._send_page("Stored package ZIP deleted")

    def _handle_project_delete(self) -> None:
        metadata = _read_json(PROJECT_METADATA_PATH)
        active = str(metadata.get("active_project", "") or "")
        if active and PROJECT_NAME_RE.fullmatch(active):
            (PROJECT_DIR / f"{active}.xml").unlink(missing_ok=True)
            (CGATE_PROJECTS_DIR / f"{active}.xml").unlink(missing_ok=True)
            projects = metadata.get("projects")
            if isinstance(projects, dict):
                projects.pop(active, None)
                remaining = sorted(projects)
                metadata["active_project"] = remaining[0] if remaining else ""
                metadata["projects"] = projects
                _atomic_write_json(PROJECT_METADATA_PATH, metadata)
            else:
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
