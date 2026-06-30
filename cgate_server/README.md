# C-Gate Server

Home Assistant app for running Schneider Electric C-Gate.

- User-supplied official C-Gate runtime package
- Current Toolkit `.cbz`, C-Gate 3 `.db`, and legacy `.xml` project upload through the ingress Web UI
- Correct C-Gate 3 project repository layout under `tag/<PROJECT>/`
- Persistent project database with automatic backups on replacement
- Toolkit remote access control generated from app options
- Standard and secure C-Gate ports exposed to the LAN
- No MQTT bridge or Home Assistant entities

See `DOCS.md` for setup and testing instructions.
