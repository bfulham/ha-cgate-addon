# C-Gate Server

This app runs Schneider Electric C-Gate inside Home Assistant OS/Supervisor so C-Bus Toolkit and a future native Home Assistant integration can share one C-Gate server.

## Requirements

- Home Assistant OS or another Supervisor installation.
- A current Schneider Electric C-Gate Linux package obtained from Schneider Electric or an authorised source.
- C-Bus Toolkit 1.17 or newer when using C-Gate 3. C-Gate 3 is not compatible with Toolkit 1.16.4 and older.
- The IP address of every Toolkit computer that is allowed to connect.

## 1. Upload C-Gate

Start the app, then select **Open Web UI**. Upload either:

```text
C-Gate_3_Linux_Package_V3_7_1.zip
```

or its inner runtime archive:

```text
cgate-3.7.1_2287.zip
```

The uploader verifies that the archive contains `cgate.jar`, stores it privately in the app's persistent `/data` directory, and installs it automatically. The upload page requires confirmation that you obtained the package legitimately and accept the licence included by Schneider Electric.

The standard Home Assistant app Configuration tab only supports typed option fields, not file attachments. The upload control is therefore supplied through the app's authenticated ingress Web UI.

### Manual fallback

The previous `/share/cgate/` method is still supported. A manually copied package is used when no package has been uploaded through the Web UI. The Supervisor mounts `/share` read-only inside this app, so create the `cgate` folder and copy the ZIP using File editor, Studio Code Server, Samba, or another Home Assistant file-management tool before starting the app. The app will not try to create that folder itself.

## 2. Configure the app

Example configuration:

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

Enter the exact IP address of each Windows computer running Toolkit. The app writes each address to C-Gate's `access.txt` with `Program` permission.

Do not enter a subnet or CIDR range. Add each computer separately.

### `integration_clients`

Addresses permitted to use the C-Gate command interfaces. The default `172.30.32.1` is intended for Home Assistant's internal app network and can be changed later if the native integration connects from a different source address.

### `project_name`

Leave blank for the first Toolkit connection test. Toolkit can then create or import the remote project. Set this later to the exact C-Gate project name to start it automatically with the app.

### `force_reinstall`

Set to `true` once to reinstall C-Gate from the selected package. Set it back to `false` after the next successful start.

## 3. Start and verify

A successful startup should show the C-Gate banner and indicate that the command interfaces are listening.

From the Toolkit computer, test the Home Assistant host address:

```powershell
Test-NetConnection HOME_ASSISTANT_IP -Port 20023
```

## 4. Connect from Toolkit

On the Toolkit computer whose IP is listed in `toolkit_clients`:

1. Close any local C-Gate process that might already be using Toolkit.
2. Open C-Bus Toolkit 1.17 or newer. Toolkit 1.19.4 is paired with C-Gate 3.7.1.
3. Open **File -> Connect to a Remote C-Gate**.
4. Enter a site name containing letters and numbers only, with no spaces.
5. Leave Host Name blank.
6. Enter the Home Assistant machine's LAN IP address.
7. Confirm the connection.

## 5. Import the project

Once the remote C-Gate connection works, use Toolkit to restore/import your `.cbz` backup into the remote site. This lets Toolkit perform any database migration required by C-Gate 3.

After the remote project exists, set `project_name` in the app to the exact project name and restart the app.

## Ports

| Port | Purpose |
|---:|---|
| 20023 | Command/program interface |
| 20024 | Event interface |
| 20025 | Load-change interface |
| 20026 | Configuration-change interface |
| 20123 | Secure command interface |

Do not expose these ports to the internet. Restrict access with your LAN firewall.

## Updating C-Gate

1. Open the app Web UI.
2. Upload the newer official C-Gate ZIP.
3. Restart the app if C-Gate is already running.
4. Confirm the new build starts correctly.

The project and configuration remain in the app's persistent `/data` directory.

## Why the C-Gate runtime is not bundled

The add-on code is open-source, but C-Gate is Schneider Electric proprietary software supplied under its own EULA. Publicly embedding the runtime in the GitHub repository or container image would redistribute Schneider's software to every user. The add-on instead lets each user obtain the package from Schneider, accept its licence, and upload their own copy privately.
