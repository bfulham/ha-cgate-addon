# Home Assistant C-Gate Server App

Runs Schneider Electric C-Gate locally on Home Assistant OS/Supervisor for C-Bus Toolkit and Home Assistant integrations.

## Features

- Upload the official C-Gate Linux package through the authenticated app Web UI.
- Upload Toolkit `.cbz` backups or C-Gate project `.xml` files through the same Web UI.
- Large files use ingress-safe 8 MiB chunked uploads.
- Projects are validated, persisted, backed up when replaced, and copied into C-Gate's `Projects` directory.
- An uploaded project becomes the default automatically when the app's `project_name` option is blank.
- C-Gate project and configuration data survive runtime upgrades.
- Toolkit client addresses are written into C-Gate's access control file.
- Plain ports `20023-20026` and secure ports `20123-20126` are exposed.

## Installation

Add this repository to the Home Assistant App Store, install **C-Gate Server**, configure the Toolkit client IP address, then start the app.

Open **Web UI** and upload:

1. The official Schneider Electric C-Gate Linux ZIP.
2. Your Toolkit `.cbz` backup or C-Gate `.xml` project.

Restart the app after the project upload. C-Gate will start the uploaded project automatically unless `project_name` contains a manual override.

The proprietary C-Gate runtime is not included in this repository. Each user must obtain it from Schneider Electric or an authorised source and accept its licence.
