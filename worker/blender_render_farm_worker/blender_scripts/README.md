# Blender-side programs

Everything in this directory is licensed under **GPL-3.0-or-later**. See [COPYING](COPYING). These programs may be used commercially under the GPL; the noncommercial license of the separate farm manager does not apply to this directory.

Copyright (c) 2026 Vinicius D'Arcadia Cruz.

These scripts run inside a separately installed Blender and import `bpy`. They do not import the farm manager's Python modules.

## Device selection

Create a JSON file with these options:

```json
{
  "renderDevicePreference": "GPU",
  "gpuBackend": "AUTO",
  "gpuDeviceIds": [],
  "gpuEnableAll": true,
  "gpuIncludeCpu": false
}
```

Configure the devices in a loaded scene:

```powershell
blender -b scene.blend --python device_override.py -- --device-config options.json
```

This command configures the loaded scene; it does not start a render by itself. The farm worker supplies the same options as a Python dictionary named `RENDERFARM_DEVICE_OPTIONS`, prefixes the script with that dictionary, and passes the source to Blender with `--python-expr` before its render arguments.

## Persistent session

```powershell
blender -b scene.blend --python blender_session_bootstrap.py -- --session-config session.json
```

The session configuration provides the named-pipe address and authentication key for an already listening local controller. This program is not a standalone HTTP server. See the source for the control messages and render options. Treat session configuration/authentication material as local runtime data; never publish real keys.

Redistribution, modifications, source availability and notices are governed by the GPL. No warranty is provided.
