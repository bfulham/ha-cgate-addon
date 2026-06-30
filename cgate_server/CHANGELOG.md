# Changelog

## 0.1.7

- Added Toolkit 1.17+ CBZ support for backups containing SQLite `.db` project databases.
- Added direct C-Gate `.db` upload support.
- Fixed the C-Gate 3 repository path and layout to `tag/<PROJECT>/<PROJECT>.db`.
- Validates SQLite database integrity and required C-Gate project tables before installation.
- Reads project name, tag name, DB version, description, and network count from the database.
- Removes the incorrect v0.1.6 `Projects/<PROJECT>.xml` copy when a replacement is applied.
- Preserves C-Gate's live `tag` repository during runtime package upgrades.
- Keeps legacy XML upload support for projects that C-Gate can convert.

## 0.1.6

- Added Toolkit project upload to the authenticated ingress Web UI.
- Accepts Toolkit `.cbz` backups and C-Gate project `.xml` files.
- Validates the project XML, extracts the project address/name, and reports the network count.
- Copies uploaded projects into C-Gate's persistent `Projects` directory.
- Automatically uses the uploaded project as the default when `project_name` is blank.
- Creates a timestamped backup before replacing an existing uploaded project.
- Restores uploaded projects after a C-Gate runtime reinstall or upgrade.
- Preserves C-Gate `Projects`, `config`, and `logs` directories during runtime upgrades.
- Added project status and removal controls to the Web UI.

## 0.1.5

- Exposed C-Gate secure ports 20124-20126 for Toolkit remote connections.
- Added clearer Toolkit connection documentation.

## 0.1.4

- Added chunked uploads to avoid Home Assistant ingress request-size limits.

## 0.1.3

- Fixed startup when `/share` is mounted read-only.

## 0.1.2

- Added the authenticated ingress upload Web UI.

## 0.1.1

- Added support for Schneider's outer C-Gate Linux package ZIP.

## 0.1.0

- Initial release.
