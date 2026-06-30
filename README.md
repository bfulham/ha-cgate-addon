# C-Gate Server Home Assistant app repository

This repository contains a Home Assistant app that runs Schneider Electric C-Gate inside Home Assistant OS/Supervisor.

The C-Gate binary is **not redistributed**. After installing the app, start it and select **Open Web UI** to upload the official Schneider Electric Linux package. The package is stored privately in the app's persistent data directory.
Large packages are uploaded in 8 MiB chunks so they work through Home Assistant ingress without exceeding its per-request body limit.

The older manual package path is still supported as a read-only fallback:

```text
/share/cgate/
```

Create that folder outside the app using a Home Assistant file-management tool. Normal installations should use the Web UI uploader, which stores the package in the app's writable persistent `/data` directory.

## Repository installation

Add this URL in **Settings -> Apps -> App store -> menu -> Repositories**:

```text
https://github.com/bfulham/ha-cgate-addon
```

Then install **C-Gate Server**, start it, and open its Web UI to upload C-Gate.

See the app documentation for the complete setup and Toolkit test procedure.
## Toolkit remote connection

C-Bus Toolkit uses C-Gate's secure interface range `20123-20126`, not only the plain command port `20023`. Version 0.1.5 exposes all four secure ports.

Use the Home Assistant host's LAN IPv4 address in Toolkit unless a local DNS hostname is known to resolve to that same address. The Toolkit PC must also be listed in the app's `toolkit_clients` option.

C-Gate 3.7.1 is matched with C-Bus Toolkit 1.19.4. Toolkit 1.16.4 and older cannot connect to C-Gate 3.
