# Blender Render Farm

A Windows/LAN render farm for Blender with a central queue, CPU/GPU workers, desktop submission tools and an image viewer.

[Português / complete documentation](README.md)

**Noncommercial use only for the management software.** Its license is [PolyForm Noncommercial 1.0.0](LICENSE). Operating a commercial render farm, selling rendering services or offering a paid derivative service using this code is not permitted by that license. See [licensing scope and exceptions](docs/LICENSING.md). This is a source-available project, not an OSI open-source license.

## Quick start

Use Windows 10/11, 64-bit Python 3.11 or 3.12 with Tcl/Tk, and a separately installed Blender on each rendering machine. Validation uses Python 3.12. Keep the whole repository layout: every application imports the `shared/` directory.

```powershell
git clone https://github.com/Vinicruznxd/blender-render-farm.git
cd blender-render-farm
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item coordinator/blender_render_farm_coordinator/config.example.json coordinator/blender_render_farm_coordinator/config.json
Copy-Item worker/blender_render_farm_worker/config.example.json worker/blender_render_farm_worker/config.json
Copy-Item master/blender_render_farm_master/config.example.json master/blender_render_farm_master/config.json
Copy-Item image_viewer/BlenderImageViewer/config.example.json image_viewer/BlenderImageViewer/config.json
```

Run these commands in separate terminals from the repository root:

```powershell
.\.venv\Scripts\python.exe coordinator/blender_render_farm_coordinator/main.py
.\.venv\Scripts\python.exe worker/blender_render_farm_worker/worker.py
.\.venv\Scripts\python.exe master/blender_render_farm_master/main.py
```

The example configuration starts on loopback with discovery disabled. To deploy across your LAN, follow [installation](docs/INSTALLATION.md). Use one Coordinator, one Worker per rendering machine, and as many Master clients as needed. All machines must access the same scene, textures and output paths; files are not automatically uploaded to workers.

Coordinator: HTTP `8010`, dashboard `/dashboard`, API `/docs`. Worker: HTTP `9000`. Optional Coordinator discovery: UDP `8011`.

The Coordinator has no authentication or TLS. Keep it on a trusted private network. The Worker's optional bearer token is not forwarded by the bundled Master/Coordinator clients. This is not a hosted multi-tenant service.

```powershell
# Run each component's tests in a separate process
.\.venv\Scripts\python.exe scripts/run_tests.py
```

See [validation](docs/VALIDATION.md) for what was actually tested. Automated rendering tests use mocks; hardware rendering, GPU drivers and shared folders need validation in your own environment.

Independent project by [Vinicius D'Arcadia Cruz](https://github.com/Vinicruznxd), not affiliated with the Blender Foundation.
