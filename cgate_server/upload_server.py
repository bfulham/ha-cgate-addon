#!/usr/bin/env python3
"""Ingress-friendly upload UI for the Schneider C-Gate package.

Home Assistant ingress may reject individual request bodies larger than 16 MiB.
The browser therefore uploads large ZIPs in small raw chunks and the app
assembles them under /data before validating and installing the package.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import tempfile
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
from urllib.parse import parse_qs, urlparse
import zipfile

HOST: Final = "0.0.0.0"
PORT: Final = 8099
MAX_UPLOAD_BYTES: Final = 256 * 1024 * 1024
# Keep every request well below Home Assistant ingress' observed 16 MiB limit.
CHUNK_BYTES: Final = 8 * 1024 * 1024
DATA_DIR: Final = Path("/data")
PACKAGE_DIR: Final = DATA_DIR / "packages"
UPLOAD_DIR: Final = PACKAGE_DIR / "uploads"
PACKAGE_PATH: Final = PACKAGE_DIR / "cgate-package.zip"
METADATA_PATH: Final = PACKAGE_DIR / "metadata.json"
INSTALL_MARKER: Final = DATA_DIR / "cgate" / ".installed-package"
BUILD_INFO: Final = DATA_DIR / "cgate" / "BuildInfo.txt"
UPLOAD_ID_RE: Final = re.compile(r"^[0-9a-f]{32}$")


def _safe_archive_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ".." not in path.parts


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
            if info.file_size > MAX_UPLOAD_BYTES:
                continue
            try:
                # Spooled storage avoids keeping large nested packages entirely
                # in RAM while still providing a seekable object to ZipFile.
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


def _zip_contains_cgate(blob: bytes) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            return _check_zip_archive(archive)
    except zipfile.BadZipFile:
        return False, "The uploaded file is not a valid ZIP archive"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_metadata() -> dict[str, object]:
    try:
        return json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


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


def _render_page(message: str = "", error: bool = False) -> bytes:
    metadata = _read_metadata()
    uploaded = PACKAGE_PATH.is_file()
    current_hash = _sha256(PACKAGE_PATH) if uploaded else ""
    installed_hash = ""
    try:
        installed_hash = INSTALL_MARKER.read_text(encoding="utf-8").strip()
    except OSError:
        pass

    if _cgate_listening():
        runtime_state = '<span class="pill ok">Running</span>'
    elif installed_hash:
        runtime_state = '<span class="pill warn">Installed, not listening</span>'
    else:
        runtime_state = '<span class="pill muted">Waiting for package</span>'

    if uploaded and current_hash == installed_hash:
        package_state = '<span class="pill ok">Installed</span>'
    elif uploaded and installed_hash:
        package_state = '<span class="pill warn">Restart app to apply</span>'
    elif uploaded:
        package_state = '<span class="pill warn">Ready to install</span>'
    else:
        package_state = '<span class="pill muted">Not uploaded</span>'

    filename = html.escape(str(metadata.get("original_filename", "C-Gate package")))
    uploaded_size = int(metadata.get("size_bytes", 0) or 0)
    size_text = f"{uploaded_size / 1024 / 1024:.1f} MB" if uploaded_size else ""
    detail = f"{filename} ({size_text})" if uploaded else "No package stored in the app data directory."
    build = html.escape(_build_info())

    notice = ""
    if message:
        cls = "notice error" if error else "notice success"
        notice = f'<div class="{cls}">{html.escape(message)}</div>'

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>C-Gate Server</title>
  <style>
    :root {{ color-scheme: light dark; --accent:#03a9f4; --card:#ffffff; --text:#202124; --muted:#6b7280; --border:#d1d5db; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --card:#1f2937; --text:#f3f4f6; --muted:#9ca3af; --border:#4b5563; }} }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--text); background:transparent; }}
    main {{ max-width:760px; margin:0 auto; padding:24px 16px 48px; }}
    h1 {{ margin:0 0 6px; font-size:28px; }} h2 {{ margin-top:0; font-size:19px; }}
    .sub {{ color:var(--muted); margin-bottom:20px; }}
    .card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:18px; margin:14px 0; box-shadow:0 1px 2px rgba(0,0,0,.08); }}
    .row {{ display:flex; justify-content:space-between; gap:18px; align-items:center; padding:8px 0; border-bottom:1px solid var(--border); }}
    .row:last-child {{ border-bottom:0; }}
    .label {{ color:var(--muted); }}
    .pill {{ display:inline-block; padding:3px 9px; border-radius:999px; font-size:12px; font-weight:600; }}
    .ok {{ background:#dcfce7; color:#166534; }} .warn {{ background:#fef3c7; color:#92400e; }} .muted {{ background:#e5e7eb; color:#374151; }}
    form {{ display:grid; gap:14px; }} input[type=file] {{ width:100%; border:1px dashed var(--border); border-radius:8px; padding:14px; }}
    label.check {{ display:flex; gap:10px; align-items:flex-start; }}
    button {{ border:0; border-radius:8px; padding:11px 16px; background:var(--accent); color:white; font-weight:600; cursor:pointer; width:max-content; }}
    button:disabled {{ opacity:.55; cursor:wait; }} button.danger {{ background:#b91c1c; }}
    .small {{ color:var(--muted); font-size:12px; }}
    .notice {{ padding:12px 14px; border-radius:8px; margin:14px 0; }} .success {{ background:#dcfce7; color:#166534; }} .error {{ background:#fee2e2; color:#991b1b; }}
    .progress-wrap {{ display:none; gap:8px; }} .progress-wrap.active {{ display:grid; }}
    progress {{ width:100%; height:18px; }}
    code {{ overflow-wrap:anywhere; }}
  </style>
</head>
<body><main>
  <h1>C-Gate Server</h1>
  <div class="sub">Upload the official Schneider Electric Linux C-Gate package, then use Toolkit through this Home Assistant app.</div>
  {notice}
  <div id="js-notice"></div>
  <section class="card">
    <h2>Status</h2>
    <div class="row"><span class="label">C-Gate</span>{runtime_state}</div>
    <div class="row"><span class="label">Package</span>{package_state}</div>
    <div class="row"><span class="label">Stored file</span><span>{detail}</span></div>
    {f'<div class="row"><span class="label">Build</span><span>{build}</span></div>' if build else ''}
  </section>
  <section class="card">
    <h2>Upload C-Gate package</h2>
    <form id="upload-form" method="post" action="./upload" enctype="multipart/form-data">
      <input id="package" type="file" name="package" accept=".zip,application/zip" required>
      <label class="check"><input id="accept-eula" type="checkbox" name="accept_eula" value="yes" required><span>I obtained this package from Schneider Electric or an authorised source and accept the C-Gate licence agreement included in the package.</span></label>
      <button id="upload-button" type="submit">Upload package</button>
      <div id="progress-wrap" class="progress-wrap"><progress id="progress" max="100" value="0"></progress><span id="progress-text" class="small">Preparing upload…</span></div>
      <div class="small">Accepted: Schneider's outer Linux package or the inner <code>cgate-*.zip</code>. Maximum size: 256 MB. Large files are automatically split into 8 MB chunks to stay below the Home Assistant ingress request limit.</div>
    </form>
  </section>
  {f'''<section class="card"><h2>Remove uploaded package</h2><form method="post" action="./delete"><button class="danger" type="submit">Delete stored ZIP</button><div class="small">This does not remove an already installed C-Gate runtime. Reinstalling later requires uploading the package again.</div></form></section>''' if uploaded else ''}
<script>
(() => {{
  const form = document.getElementById('upload-form');
  const fileInput = document.getElementById('package');
  const eula = document.getElementById('accept-eula');
  const button = document.getElementById('upload-button');
  const progressWrap = document.getElementById('progress-wrap');
  const progress = document.getElementById('progress');
  const progressText = document.getElementById('progress-text');
  const notice = document.getElementById('js-notice');

  function showNotice(text, isError) {{
    notice.className = 'notice ' + (isError ? 'error' : 'success');
    notice.textContent = text;
    notice.scrollIntoView({{behavior: 'smooth', block: 'nearest'}});
  }}

  async function jsonRequest(url, options) {{
    const response = await fetch(url, options);
    let payload = {{}};
    try {{ payload = await response.json(); }} catch (_) {{}}
    if (!response.ok) throw new Error(payload.error || `Upload failed (HTTP ${{response.status}})`);
    return payload;
  }}

  form.addEventListener('submit', async (event) => {{
    event.preventDefault();
    const file = fileInput.files[0];
    if (!file || !eula.checked) return;

    button.disabled = true;
    fileInput.disabled = true;
    eula.disabled = true;
    progressWrap.classList.add('active');
    progress.value = 0;
    notice.className = '';
    notice.textContent = '';

    try {{
      const start = await jsonRequest('./upload/start', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{filename: file.name, size: file.size, accept_eula: true}})
      }});
      const chunkSize = start.chunk_size;
      let offset = 0;
      while (offset < file.size) {{
        const end = Math.min(offset + chunkSize, file.size);
        const chunk = file.slice(offset, end);
        await jsonRequest(`./upload/chunk?id=${{encodeURIComponent(start.upload_id)}}&offset=${{offset}}`, {{
          method: 'POST',
          headers: {{'Content-Type': 'application/octet-stream'}},
          body: chunk
        }});
        offset = end;
        const percent = Math.round((offset / file.size) * 100);
        progress.value = percent;
        progressText.textContent = `Uploading… ${{percent}}% (${{(offset / 1024 / 1024).toFixed(1)}} of ${{(file.size / 1024 / 1024).toFixed(1)}} MB)`;
      }}
      progressText.textContent = 'Validating C-Gate package…';
      const finish = await jsonRequest('./upload/finish', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{upload_id: start.upload_id}})
      }});
      progress.value = 100;
      progressText.textContent = 'Upload complete';
      showNotice(finish.message || 'Package uploaded successfully.', false);
      setTimeout(() => window.location.reload(), 1200);
    }} catch (error) {{
      progressText.textContent = 'Upload failed';
      showNotice(error.message || String(error), true);
    }} finally {{
      button.disabled = false;
      fileInput.disabled = false;
      eula.disabled = false;
    }}
  }});
}})();
</script>
</main></body></html>"""
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "CgateUpload/0.2"

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
        if path.endswith("/upload/start"):
            self._handle_upload_start()
            return
        if path.endswith("/upload/chunk"):
            self._handle_upload_chunk()
            return
        if path.endswith("/upload/finish"):
            self._handle_upload_finish()
            return
        if path.endswith("/upload"):
            self._handle_upload_legacy()
            return
        if path.endswith("/delete"):
            self._handle_delete()
            return
        self._send_page("Unknown action", True, HTTPStatus.NOT_FOUND)

    def _upload_paths(self, upload_id: str) -> tuple[Path, Path]:
        if not UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError("Invalid upload ID")
        return UPLOAD_DIR / f"{upload_id}.part", UPLOAD_DIR / f"{upload_id}.json"

    def _load_upload_state(self, upload_id: str) -> tuple[Path, Path, dict[str, object]]:
        part_path, state_path = self._upload_paths(upload_id)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as err:
            raise ValueError("Upload session was not found or has expired") from err
        if not isinstance(state, dict):
            raise ValueError("Invalid upload session")
        return part_path, state_path, state

    def _handle_upload_start(self) -> None:
        try:
            request = self._read_json()
            filename = Path(str(request.get("filename", ""))).name
            size = int(request.get("size", 0))
            accepted = request.get("accept_eula") is True
            if not accepted:
                raise ValueError("You must accept the included C-Gate licence agreement")
            if not filename.lower().endswith(".zip"):
                raise ValueError("Select a valid ZIP file")
            if size <= 0 or size > MAX_UPLOAD_BYTES:
                raise ValueError("Upload is empty or exceeds the 256 MB limit")

            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            upload_id = secrets.token_hex(16)
            part_path, state_path = self._upload_paths(upload_id)
            part_path.touch(exist_ok=False)
            state_path.write_text(
                json.dumps({"filename": filename, "expected_size": size}, indent=2),
                encoding="utf-8",
            )
            self._send_json({"upload_id": upload_id, "chunk_size": CHUNK_BYTES})
        except (ValueError, OSError) as err:
            self._send_json({"error": str(err)}, HTTPStatus.BAD_REQUEST)

    def _handle_upload_chunk(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        upload_id = query.get("id", [""])[0]
        try:
            offset = int(query.get("offset", ["-1"])[0])
            part_path, _state_path, state = self._load_upload_state(upload_id)
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
            if current_size + content_length > expected_size or current_size + content_length > MAX_UPLOAD_BYTES:
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

    def _handle_upload_finish(self) -> None:
        part_path: Path | None = None
        state_path: Path | None = None
        try:
            request = self._read_json()
            upload_id = str(request.get("upload_id", ""))
            part_path, state_path, state = self._load_upload_state(upload_id)
            expected_size = int(state.get("expected_size", 0))
            actual_size = part_path.stat().st_size
            if actual_size != expected_size:
                raise ValueError(f"Upload is incomplete: received {actual_size} of {expected_size} bytes")

            valid, validation_message = _zip_contains_cgate_path(part_path)
            if not valid:
                raise ValueError(validation_message)

            PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
            os.replace(part_path, PACKAGE_PATH)
            METADATA_PATH.write_text(
                json.dumps(
                    {
                        "original_filename": str(state.get("filename", "cgate-package.zip")),
                        "size_bytes": actual_size,
                        "validation": validation_message,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            try:
                state_path.unlink()
            except FileNotFoundError:
                pass
            self._send_json(
                {
                    "message": "Package uploaded successfully. It will install automatically if C-Gate has not started; otherwise restart the app to apply it."
                }
            )
        except (ValueError, OSError) as err:
            self._send_json({"error": str(err)}, HTTPStatus.BAD_REQUEST)

    def _handle_upload_legacy(self) -> None:
        """Retain small single-request uploads for browsers without JavaScript."""
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
            self._send_page("Upload is empty or exceeds the 256 MB limit", True, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._send_page("Expected a multipart file upload", True, HTTPStatus.BAD_REQUEST)
            return

        raw_body = self.rfile.read(content_length)
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw_body
        )

        accepted = False
        filename = ""
        package_data: bytes | None = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "accept_eula":
                accepted = part.get_content().strip().lower() in {"yes", "on", "true", "1"}
            elif name == "package":
                filename = Path(part.get_filename() or "cgate-package.zip").name
                package_data = part.get_payload(decode=True)

        if not accepted:
            self._send_page("You must accept the included C-Gate licence agreement", True, HTTPStatus.BAD_REQUEST)
            return
        if not package_data or not filename.lower().endswith(".zip"):
            self._send_page("Select a valid ZIP file", True, HTTPStatus.BAD_REQUEST)
            return

        valid, validation_message = _zip_contains_cgate(package_data)
        if not valid:
            self._send_page(validation_message, True, HTTPStatus.BAD_REQUEST)
            return

        PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = PACKAGE_PATH.with_suffix(".zip.tmp")
        temp_path.write_bytes(package_data)
        os.replace(temp_path, PACKAGE_PATH)
        METADATA_PATH.write_text(
            json.dumps(
                {
                    "original_filename": filename,
                    "size_bytes": len(package_data),
                    "validation": validation_message,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self._send_page(
            "Package uploaded successfully. It will install automatically if C-Gate has not started; otherwise restart the app to apply it."
        )

    def _handle_delete(self) -> None:
        for path in (PACKAGE_PATH, METADATA_PATH):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._send_page("Stored package deleted")


def main() -> None:
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[upload-ui] Listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
