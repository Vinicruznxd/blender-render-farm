"""Exercise the external script contract and frozen-bundle resource lookup."""
import json
from pathlib import Path
import runpy
import shutil
import sys
import types

from app.blender_runner import BlenderRunner
from app.config import WorkerConfig


def fake_blender():
    module = types.ModuleType("bpy")
    module.context = types.SimpleNamespace(preferences=types.SimpleNamespace(addons={}))
    scene = types.SimpleNamespace(cycles=types.SimpleNamespace(device="GPU"))
    module.data = types.SimpleNamespace(scenes=[scene])
    return module, scene


def test_device_script_accepts_standalone_json_config(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "blender_scripts/device_override.py"
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"renderDevicePreference": "CPU"}), encoding="utf-8")
    bpy, scene = fake_blender()
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setattr(sys, "argv", ["blender", "--", "--device-config", str(options)])
    runpy.run_path(str(script), run_name="__main__")
    assert scene.cycles.device == "CPU"


def test_frozen_worker_loads_external_device_script_and_session_bootstrap(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "blender_scripts"
    bundle = tmp_path / "_internal"
    shutil.copytree(source, bundle / "blender_scripts")
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    runner = BlenderRunner.__new__(BlenderRunner)
    runner.project_root = tmp_path
    runner.config = WorkerConfig()
    assert runner._resolve_blender_script("device_override.py") == bundle / "blender_scripts/device_override.py"
    assert runner._resolve_persistent_session_bootstrap_script() == bundle / "blender_scripts/blender_session_bootstrap.py"
    bpy, scene = fake_blender()
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    exec(runner._device_override_expression("CPU"), {})
    assert scene.cycles.device == "CPU"
