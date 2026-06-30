# Changelog

## 0.1.2

- Add an authenticated ingress Web UI for uploading the official C-Gate Linux ZIP.
- Allow the app to start and wait for an upload when no package is present.
- Validate ZIP safety and confirm that `cgate.jar` exists directly or in a nested ZIP.
- Store uploaded packages privately in persistent app data.
- Keep `/share/cgate/` as a manual fallback.
- Change the health check to the always-available upload UI.

## 0.1.1

- Accept Schneider's outer `C-Gate_3_Linux_Package_V3_7_1.zip` archive directly.
- Detect and extract nested runtime ZIPs automatically.

## 0.1.0

- Initial C-Gate-only Home Assistant app.
