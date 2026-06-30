# Changelog

## 0.1.5

- Expose the complete secure C-Gate interface range `20123-20126` required by C-Bus Toolkit remote repositories.
- Add clear secure-port startup logging.
- Document that Toolkit must connect to the Home Assistant LAN address and must be compatible with C-Gate 3.7.1.

## 0.1.4

- Fix uploads of C-Gate packages larger than Home Assistant ingress' 16 MiB request limit.
- Upload large ZIPs in 8 MiB chunks and assemble them in persistent app storage.
- Add upload progress and package validation after the final chunk.
- Stream chunks to disk instead of buffering the entire outer package in memory.

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
