# Home Assistant C-Gate Server App

Runs Schneider Electric C-Gate locally on Home Assistant OS/Supervisor for C-Bus Toolkit and Home Assistant integrations.

## Features

- Upload the official C-Gate Linux package through the authenticated app Web UI.
- Upload current Toolkit `.cbz` backups, C-Gate 3 `.db` project databases, or legacy `.xml` projects.
- Large files use ingress-safe 8 MiB chunked uploads.
- Current Toolkit SQLite projects are validated and installed in C-Gate's required `tag/<PROJECT>/<PROJECT>.db` repository layout.
- Uploaded projects are persisted and backed up before replacement.
- An uploaded project becomes the default automatically when the app's `project_name` option is blank.
- C-Gate project and configuration data survive runtime upgrades.
- Toolkit client addresses are written into C-Gate's access control file.
- Plain ports `20023-20026` and secure ports `20123-20126` are exposed.

## Installation

Add this repository to the Home Assistant App Store, install **C-Gate Server**, configure the Toolkit client IP address, then start the app.

Open **Web UI** and upload:

1. The official Schneider Electric C-Gate Linux ZIP.
2. A current Toolkit `.cbz` backup. C-Gate 3 `.db` and legacy `.xml` files are also accepted.

Restart the app after the project upload. C-Gate will install and start the uploaded project automatically unless `project_name` contains a manual override.

The proprietary C-Gate runtime is not included in this repository. Each user must obtain it from Schneider Electric or an authorised source and accept its licence.
