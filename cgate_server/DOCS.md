# C-Gate Server

This app runs Schneider Electric C-Gate inside Home Assistant OS/Supervisor so C-Bus Toolkit and a Home Assistant integration can use one C-Gate server.

## Requirements

- Home Assistant OS or another Supervisor installation.
- A current Schneider Electric C-Gate Linux package obtained from Schneider Electric or an authorised source.
- C-Bus Toolkit 1.17 or newer for C-Gate 3. Toolkit 1.19.4 is paired with C-Gate 3.7.1.
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

Set this only when multiple project XML files exist and you intentionally want to override the active uploaded project. The value must match the project address and XML filename exactly, without `.xml`.

### `force_reinstall`

Set to `true` for one start to reinstall the C-Gate runtime from the uploaded package, then return it to `false`. Project, configuration, and log directories are preserved.

## Upload the C-Gate runtime

Start the app and select **Open Web UI**. Upload either:

```text
C-Gate_3_Linux_Package_V3_7_1.zip
```

or its inner runtime archive:

```text
cgate-3.7.1_2287.zip
```

The app validates the package, including nested ZIPs, stores it privately under persistent app data, and installs it. Large files are split into 8 MiB requests to avoid Home Assistant ingress request-size limits.

The C-Gate runtime is proprietary and is not distributed with this open-source app.

## Upload a Toolkit project

In the same Web UI, use **Upload Toolkit project** and select either:

- a Toolkit `.cbz` backup; or
- a C-Gate project `.xml` file.

The app will:

1. Validate the archive and XML paths.
2. Find the C-Bus `Project` element.
3. Read the project address/name and network count.
4. Extract the XML from a `.cbz` backup.
5. Create a timestamped backup when replacing an existing uploaded project.
6. Store the canonical project XML under persistent app data.
7. Copy it into C-Gate's `Projects` directory.
8. Select it as the active/default project when `project_name` is blank.

Restart the app after uploading or replacing a project. The log should include a line similar to:

```text
Configured C-Gate to start project: THEBEND
```

If the live C-Gate project file ever needs to be restored from the uploaded seed copy, the log will also report that restoration. Normal restarts do not overwrite changes made through Toolkit.

The Web UI displays the active project name, source filename, and detected network count.

## Connect Toolkit

On a Toolkit computer whose IP is listed in `toolkit_clients`:

1. Open **File → Connect to a Remote C-Gate**.
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

After uploading and restarting, reconnect the Toolkit remote site. The uploaded project should appear beneath the site.

## Project replacement and backups

Uploading a newer `.cbz` or `.xml` with the same project address replaces the active project. Before replacement, the previous XML is copied to:

```text
/data/projects/backups/
```

This directory is private persistent app data. It is not exposed through Home Assistant's `/config` or `/share` folders.

Use **Remove uploaded project** in the Web UI only when you intend to remove the project from C-Gate. Restart afterward.

## Manual package fallback

A C-Gate runtime ZIP may still be placed in `/share/cgate/`. The `/share` mount is read-only inside the app, so create the folder and copy the file using another Home Assistant file-management tool. Web UI upload is preferred.

Toolkit projects should be uploaded through the Web UI rather than copied through `/share`.

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
