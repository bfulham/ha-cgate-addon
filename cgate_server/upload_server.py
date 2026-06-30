#!/usr/bin/env python3
"""Small ingress-friendly upload UI for the Schneider C-Gate package."""

from __future__ import annotations

import html
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
from urllib.parse import urlparse
import zipfile

HOST: Final = "0.0.0.0"
PORT: Final = 8099
MAX_UPLOAD_BYTES: Final = 256 * 1024 * 1024
DATA_DIR: Final = Path("/data")
PACKAGE_DIR: Final = DATA_DIR / "packages"
PACKAGE_PATH: Final = PACKAGE_DIR / "cgate-package.zip"
METADATA_PATH: Final = PACKAGE_DIR / "metadata.json"
INSTALL_MARKER: Final = DATA_DIR / "cgate" / ".installed-package"
BUILD_INFO: Final = DATA_DIR / "cgate" / "BuildInfo.txt"


def _safe_archive_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ".." not in path.parts


def _zip_contains_cgate(blob: bytes, *, nested: bool = True) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
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
                        inner = archive.read(info)
                    except (RuntimeError, OSError, zipfile.BadZipFile):
                        continue
                    found, _ = _zip_contains_cgate(inner, nested=False)
                    if found:
                        return True, f"C-Gate runtime found in {PurePosixPath(info.filename).name}"
    except zipfile.BadZipFile:
        return False, "The uploaded file is not a valid ZIP archive"
    return False, "cgate.jar was not found in this archive or its nested ZIP"


def _sha256(path: Path) -> str:
    import hashlib

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
    button.danger {{ background:#b91c1c; }}
    .small {{ color:var(--muted); font-size:12px; }}
    .notice {{ padding:12px 14px; border-radius:8px; margin:14px 0; }} .success {{ background:#dcfce7; color:#166534; }} .error {{ background:#fee2e2; color:#991b1b; }}
    code {{ overflow-wrap:anywhere; }}
  </style>
</head>
<body><main>
  <h1>C-Gate Server</h1>
  <div class="sub">Upload the official Schneider Electric Linux C-Gate package, then use Toolkit through this Home Assistant app.</div>
  {notice}
  <section class="card">
    <h2>Status</h2>
    <div class="row"><span class="label">C-Gate</span>{runtime_state}</div>
    <div class="row"><span class="label">Package</span>{package_state}</div>
    <div class="row"><span class="label">Stored file</span><span>{detail}</span></div>
    {f'<div class="row"><span class="label">Build</span><span>{build}</span></div>' if build else ''}
  </section>
  <section class="card">
    <h2>Upload C-Gate package</h2>
    <form method="post" action="./upload" enctype="multipart/form-data">
      <input type="file" name="package" accept=".zip,application/zip" required>
      <label class="check"><input type="checkbox" name="accept_eula" value="yes" required><span>I obtained this package from Schneider Electric or an authorised source and accept the C-Gate licence agreement included in the package.</span></label>
      <button type="submit">Upload package</button>
      <div class="small">Accepted: Schneider's outer Linux package or the inner <code>cgate-*.zip</code>. Maximum size: 256 MB. The file is stored privately under this app's persistent data and is not uploaded to GitHub.</div>
    </form>
  </section>
  {f'''<section class="card"><h2>Remove uploaded package</h2><form method="post" action="./delete"><button class="danger" type="submit">Delete stored ZIP</button><div class="small">This does not remove an already installed C-Gate runtime. Reinstalling later requires uploading the package again.</div></form></section>''' if uploaded else ''}
</main></body></html>"""
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "CgateUpload/0.1"

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

    def do_GET(self) -> None:  # noqa: N802
        self._send_page()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path.endswith("/upload"):
            self._handle_upload()
            return
        if path.endswith("/delete"):
            self._handle_delete()
            return
        self._send_page("Unknown action", True, HTTPStatus.NOT_FOUND)

    def _handle_upload(self) -> None:
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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[upload-ui] Listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
