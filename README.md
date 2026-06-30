# C-Gate Server Home Assistant app repository

This repository contains a Home Assistant app that runs Schneider Electric C-Gate inside Home Assistant OS/Supervisor.

The C-Gate binary is **not redistributed**. After installing the app, start it and select **Open Web UI** to upload the official Schneider Electric Linux package. The package is stored privately in the app's persistent data directory.

The older manual package path is still supported as a fallback:

```text
/share/cgate/
```

## Repository installation

Add this URL in **Settings -> Apps -> App store -> menu -> Repositories**:

```text
https://github.com/bfulham/ha-cgate-addon
```

Then install **C-Gate Server**, start it, and open its Web UI to upload C-Gate.

See the app documentation for the complete setup and Toolkit test procedure.
