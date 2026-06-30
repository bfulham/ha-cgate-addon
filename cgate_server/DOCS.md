# C-Gate Server

This app runs Schneider Electric C-Gate inside Home Assistant OS/Supervisor so C-Bus Toolkit and a Home Assistant integration can share one C-Gate server.

## Requirements

- Home Assistant OS or another Supervisor installation.
- A current Schneider Electric C-Gate Linux package obtained from Schneider Electric or an authorised source.
- C-Bus Toolkit 1.17 or newer for C-Gate 3.
- The IPv4 address of every Toolkit computer allowed to connect.

## Configure the app

Recommended initial configuration:

```yaml
package_filename: C-Gate_3_Linux_Package_V3_7_1.zip
toolkit_clients:
  - 10.0.10.50
integration_clients:
  - 172.30.32.1
project_name: ""
java_max_memory_mb: 512
force_reinstall: false
```

### `toolkit_clients`

Enter the exact IPv4 address of each Windows computer running Toolkit. The app writes each address to C-Gate's access file with `Program` permission.

### `integration_clients`

Addresses permitted to use the C-Gate command interfaces. The default `172.30.32.1` is intended for Home Assistant's internal app network.

### `project_name`

Normally leave this blank. When a Toolkit project is uploaded through the Web UI, the app selects it automatically.

Set this only when multiple projects exist and you intentionally want to override the active uploaded project. The value must match the C-Gate project address exactly.

### `force_reinstall`

Set to `true` for one start to reinstall the C-Gate runtime from the uploaded package, then return it to `false`. Project, configuration, and log directories are preserved.

## Upload the C-Gate runtime

Start the app and select **Open Web UI**. Upload either Schneider's outer Linux package or its inner `cgate-*.zip` runtime archive.

The app validates the package, including nested ZIPs, stores it privately under persistent app data, and installs it. Large files are split into 8 MiB requests to avoid Home Assistant ingress request-size limits.

The C-Gate runtime is proprietary and is not distributed with this open-source app.

## Upload a Toolkit project

Use **Upload Toolkit project** and select one of:

- a current Toolkit `.cbz` backup;
- a C-Gate 3 `.db` project database; or
- a legacy Toolkit/C-Gate `.xml` project.

Toolkit 1.17 and later backups normally contain a SQLite database. For example, a converted backup may contain:

```text
THEBEND.db
```

The app validates the SQLite database, reads the project identity and network count, stores it as a pending update, and installs it on the next app start using C-Gate 3's required layout:

```text
/data/cgate/tag/THEBEND/THEBEND.db
```

This differs from the legacy flat XML layout. App v0.1.6 incorrectly placed XML under `Projects/`, which could leave the project in C-Gate's `Converting` or `Failed` state. v0.1.7 removes that obsolete copy when the replacement project is applied.

For SQLite projects, validation includes:

1. SQLite integrity check.
2. Required C-Gate project tables.
3. Project address and tag name.
4. Database version.
5. Network count.

Legacy XML remains supported, but C-Gate must convert it. Very old XML projects may need to be opened and backed up with a current Toolkit version first.

After upload, restart the app. The log should include lines similar to:

```text
Installed project THEBEND as tag/THEBEND/THEBEND.db
Configured C-Gate to start project: THEBEND
```

Reconnect the remote C-Gate site in Toolkit. The project should open normally rather than reporting that conversion is pending or failed.

## Project replacement and backups

Uploading a newer project with the same project address marks it for replacement at the next restart. Before replacing the live project, the app copies the existing C-Gate project directory into:

```text
/data/projects/backups/
```

Normal restarts do not overwrite a live project that Toolkit has subsequently modified unless a new upload is pending.

## Connect Toolkit

On a Toolkit computer whose IP is listed in `toolkit_clients`:

1. Open **File -> Connect to a Remote C-Gate**.
2. Enter a site name with no spaces.
3. Leave **Host Name** blank for initial testing.
4. Enter the Home Assistant machine's LAN IPv4 address.
5. Save and connect.

Test connectivity from PowerShell:

```powershell
$ha = "HOME_ASSISTANT_IP"
20023..20026 | ForEach-Object { Test-NetConnection $ha -Port $_ }
20123..20126 | ForEach-Object { Test-NetConnection $ha -Port $_ }
```

All eight ports should report `TcpTestSucceeded : True`.

## Manual package fallback

A C-Gate runtime ZIP may still be placed in `/share/cgate/`. The `/share` mount is read-only inside the app, so Web UI upload is preferred.

Toolkit projects should be uploaded through the Web UI.

## Ports

| Port | Purpose |
|---:|---|
| 20023 | Command/program interface |
| 20024 | Event interface |
| 20025 | Load-change interface |
| 20026 | Configuration-change interface |
| 20123 | Secure command interface used by Toolkit |
| 20124 | Secure event interface used by Toolkit |
| 20125 | Secure status-change interface used by Toolkit |
| 20126 | Secure configuration-change interface used by Toolkit |

Do not expose these ports to the internet.
