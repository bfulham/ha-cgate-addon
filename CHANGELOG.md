# Changelog

## 0.1.7

- Added support for Toolkit 1.17+ `.cbz` backups containing SQLite `.db` projects.
- Fixed project storage for C-Gate 3 by using `tag/<PROJECT>/<PROJECT>.db` instead of the legacy flat `Projects/<PROJECT>.xml` path.
- Validates SQLite integrity, C-Gate schema tables, project identity, DB version, and network count before accepting an upload.
- Migrates away from the incorrect v0.1.6 XML project location and removes failed/pending conversion entries when applying a replacement project.
- Preserves the live C-Gate `tag` repository during runtime upgrades.
- Added raw `.db` upload support while retaining legacy `.xml` support.

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
