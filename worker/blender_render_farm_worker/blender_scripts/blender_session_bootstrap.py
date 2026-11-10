# SPDX-FileCopyrightText: 2026 Vinicius D'Arcadia Cruz
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See COPYING in this directory or <https://www.gnu.org/licenses/>.

from __future__ import annotations

import argparse
import json
import sys
import traceback
from multiprocessing.connection import Client
from pathlib import Path

import bpy


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persistent Blender session bootstrap")
    parser.add_argument("--session-config", required=True)
    return parser.parse_args(argv)


def print_session_log(message: str) -> None:
    print(f"[WorkerSession] {message}", flush=True)


def configure_render_device(
    render_device_preference: str,
    *,
    gpu_backend: str = "AUTO",
    gpu_device_ids: list[str] | None = None,
    gpu_enable_all: bool = True,
    gpu_include_cpu: bool = False,
) -> dict[str, object]:
    target = str(render_device_preference or "CPU").strip().upper() or "CPU"
    requested_backend = str(gpu_backend or "AUTO").strip().upper() or "AUTO"
    selected_ids = {str(item).strip() for item in (gpu_device_ids or []) if str(item).strip()}
    preferences = getattr(bpy.context, "preferences", None) or getattr(bpy.context, "user_preferences", None)
    cycles_addon = None
    cycles_preferences = None
    if preferences is not None:
        try:
            cycles_addon = preferences.addons["cycles"]
        except Exception:
            cycles_addon = None
    if cycles_addon is not None:
        cycles_preferences = getattr(cycles_addon, "preferences", None)
    if cycles_preferences is not None:
        if target == "GPU":
            candidates = (
                [requested_backend]
                if requested_backend != "AUTO"
                else ["OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"]
            )
            for candidate in candidates:
                try:
                    cycles_preferences.compute_device_type = candidate
                    break
                except Exception:
                    continue
        for method_name in ("refresh_devices", "get_devices"):
            method = getattr(cycles_preferences, method_name, None)
            if not callable(method):
                continue
            try:
                method()
            except TypeError:
                try:
                    method(bpy.context)
                except Exception as exc:
                    print_session_log(f"{method_name} failed: {exc}")
            except Exception as exc:
                print_session_log(f"{method_name} failed: {exc}")
    devices: list[dict[str, object]] = []
    if cycles_preferences is not None:
        active_backend = str(getattr(cycles_preferences, "compute_device_type", "NONE")).upper()
        for device in getattr(cycles_preferences, "devices", []):
            name = str(getattr(device, "name", "unknown"))
            device_type = str(getattr(device, "type", "UNKNOWN")).upper()
            device_id = str(getattr(device, "id", ""))
            is_cpu = device_type == "CPU"
            matches_selection = not selected_ids or device_id in selected_ids or name in selected_ids
            enabled = (
                (target == "CPU" and is_cpu)
                or (
                    target == "GPU"
                    and (
                        (is_cpu and gpu_include_cpu)
                        or (
                            not is_cpu
                            and device_type == active_backend
                            and (gpu_enable_all or matches_selection)
                        )
                    )
                )
            )
            try:
                device.use = bool(enabled)
            except Exception as exc:
                print_session_log(f"Could not configure Cycles device {name}: {exc}")
            devices.append(
                {
                    "id": device_id,
                    "name": name,
                    "type": device_type,
                    "enabled": bool(getattr(device, "use", False)),
                }
            )
    for scene in bpy.data.scenes:
        cycles_settings = getattr(scene, "cycles", None)
        if cycles_settings is not None:
            cycles_settings.device = target
    result = {
        "target": target,
        "backend": str(getattr(cycles_preferences, "compute_device_type", "NONE")),
        "devices": devices,
        "enabledGpuCount": sum(
            1 for item in devices if item["type"] != "CPU" and item["enabled"]
        ),
    }
    print_session_log(f"Cycles device configuration: {json.dumps(result, ensure_ascii=True)}")
    return result


def apply_render_settings(output_pattern: str, image_format: str, start_frame: int, end_frame: int) -> None:
    for scene in bpy.data.scenes:
        scene.render.filepath = output_pattern
        scene.render.image_settings.file_format = image_format
        scene.frame_start = int(start_frame)
        scene.frame_end = int(end_frame)
        scene.frame_current = int(start_frame)


def main() -> int:
    raw_args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    args = parse_args(raw_args)
    config_path = Path(args.session_config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    pipe_address = str(config["pipeAddress"])
    auth_key = bytes.fromhex(str(config["authKeyHex"]))
    device_configuration = configure_render_device(
        str(config.get("renderDevicePreference") or "CPU"),
        gpu_backend=str(config.get("gpuBackend") or "AUTO"),
        gpu_device_ids=list(config.get("gpuDeviceIds") or []),
        gpu_enable_all=bool(config.get("gpuEnableAll", True)),
        gpu_include_cpu=bool(config.get("gpuIncludeCpu", False)),
    )
    apply_render_settings(
        str(config.get("outputPattern") or ""),
        str(config.get("format") or "PNG"),
        1,
        1,
    )

    print_session_log(f"Connecting persistent session pipe for job {config.get('jobId')}")
    with Client(pipe_address, family="AF_PIPE", authkey=auth_key) as connection:
        connection.send(
            {
                "type": "ready",
                "jobId": config.get("jobId"),
                "blendFile": config.get("blendFile"),
                "renderDevicePreference": config.get("renderDevicePreference"),
                "deviceConfiguration": device_configuration,
            }
        )
        print_session_log("Persistent Blender session ready")
        while True:
            message = connection.recv()
            if not isinstance(message, dict):
                connection.send(
                    {
                        "type": "error",
                        "ok": False,
                        "message": f"Invalid control payload: {message!r}",
                    }
                )
                continue

            message_type = str(message.get("type") or "").strip().lower()
            if message_type == "shutdown":
                print_session_log(f"Shutdown requested: {message.get('reason') or 'no reason provided'}")
                connection.send({"type": "shutdown_ack", "ok": True})
                return 0

            if message_type != "render":
                connection.send(
                    {
                        "type": "error",
                        "ok": False,
                        "message": f"Unsupported control message type: {message_type}",
                    }
                )
                continue

            start_frame = int(message["startFrame"])
            end_frame = int(message["endFrame"])
            output_pattern = str(message["outputPattern"])
            image_format = str(message.get("format") or "PNG").upper()
            render_device = str(message.get("renderDevicePreference") or config.get("renderDevicePreference") or "CPU")
            try:
                configure_render_device(
                    render_device,
                    gpu_backend=str(config.get("gpuBackend") or "AUTO"),
                    gpu_device_ids=list(config.get("gpuDeviceIds") or []),
                    gpu_enable_all=bool(config.get("gpuEnableAll", True)),
                    gpu_include_cpu=bool(config.get("gpuIncludeCpu", False)),
                )
                apply_render_settings(output_pattern, image_format, start_frame, end_frame)
                print_session_log(
                    f"Rendering {message.get('jobId')}/{message.get('blockId')} frames {start_frame}-{end_frame} on {render_device}"
                )
                bpy.ops.render.render(animation=True, write_still=False)
                connection.send(
                    {
                        "type": "render_result",
                        "ok": True,
                        "message": "render completed",
                        "outputPaths": list(message.get("expectedOutputPaths") or []),
                    }
                )
            except Exception as exc:
                trace = traceback.format_exc()
                print_session_log(f"Render failed: {exc}\n{trace}")
                connection.send(
                    {
                        "type": "render_result",
                        "ok": False,
                        "message": str(exc),
                        "error": str(exc),
                        "traceback": trace,
                        "outputPaths": list(message.get("expectedOutputPaths") or []),
                    }
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
