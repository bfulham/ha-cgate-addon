# Changelog

## 0.1.6

- Added an ingress uploader for Toolkit `.cbz` and C-Gate `.xml` projects.
- Uploaded projects are validated, backed up, persisted, copied into C-Gate, and selected automatically when no manual `project_name` override is configured.
- Project data and C-Gate configuration are preserved during runtime package upgrades.
- Added project status and removal controls to the Web UI.

## 0.1.5

- Exposed all four secure C-Gate interfaces required by Toolkit remote sites.

## 0.1.4

- Added ingress-safe chunked package uploads.

## 0.1.3

- Fixed read-only `/share` startup failure.

## 0.1.2

- Added package upload Web UI.

## 0.1.1

- Added support for Schneider's outer Linux package.

## 0.1.0

- Initial release.
