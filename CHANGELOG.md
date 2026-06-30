# Changelog

## 0.1.3

- Fix startup failure when the Supervisor `/share` mount is read-only.
- Stop trying to create `/share/cgate` from inside the app.
- Keep `/share/cgate` as an optional read-only fallback when the directory already exists.
- Continue storing Web UI uploads in the app's persistent writable `/data` directory.

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
