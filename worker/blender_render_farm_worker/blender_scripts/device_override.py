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

"""Configure Cycles devices inside Blender; independent of the farm manager.

CLI: blender -b scene.blend --python device_override.py -- --device-config options.json
The worker may instead prefix this source with RENDERFARM_DEVICE_OPTIONS and
pass the resulting program to Blender through --python-expr.
"""
import argparse
import json
from pathlib import Path
import sys

import bpy


def _load_options():
    supplied = globals().get("RENDERFARM_DEVICE_OPTIONS")
    if supplied is not None:
        return supplied
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-config", required=True)
    args = parser.parse_args(argv)
    return json.loads(Path(args.device_config).read_text(encoding="utf-8-sig"))


options = _load_options()
target = str(options.get("renderDevicePreference", "CPU"))
requested_backend = str(options.get("gpuBackend", "AUTO"))
selected_device_ids = list(options.get("gpuDeviceIds", []))
enable_all = bool(options.get("gpuEnableAll", True))
include_cpu = bool(options.get("gpuIncludeCpu", False))
preferences = getattr(bpy.context, 'preferences', None) or getattr(bpy.context, 'user_preferences', None)
cycles_addon = None
cycles_preferences = None
if preferences is not None:
    try:
        cycles_addon = preferences.addons['cycles']
    except Exception:
        cycles_addon = None
if cycles_addon is not None:
    cycles_preferences = getattr(cycles_addon, 'preferences', None)
if cycles_preferences is None:
    print(f'Worker Cycles config: target={target} addon preferences unavailable')
else:
    if target == 'GPU':
        backend_candidates = ([requested_backend] if requested_backend != 'AUTO' else ['OPTIX', 'CUDA', 'HIP', 'ONEAPI', 'METAL'])
        for backend_candidate in backend_candidates:
            try:
                cycles_preferences.compute_device_type = backend_candidate
                break
            except Exception:
                continue
    for method_name in ('refresh_devices', 'get_devices'):
        method = getattr(cycles_preferences, method_name, None)
        if not callable(method):
            continue
        try:
            method()
        except TypeError:
            try:
                method(bpy.context)
            except Exception as exc:
                print(f'Worker Cycles config: {method_name} failed: {exc}')
        except Exception as exc:
            print(f'Worker Cycles config: {method_name} failed: {exc}')
    compute_device_type = getattr(cycles_preferences, 'compute_device_type', 'UNKNOWN')
    enabled_gpu_device_count = 0
    device_summaries = []
    for device in getattr(cycles_preferences, 'devices', []):
        device_name = getattr(device, 'name', 'unknown')
        device_type = str(getattr(device, 'type', 'UNKNOWN'))
        device_id = getattr(device, 'id', '')
        is_cpu = device_type.upper() == 'CPU'
        matches_selection = (not selected_device_ids or str(device_id) in selected_device_ids or str(device_name) in selected_device_ids)
        backend_matches = device_type.upper() == str(compute_device_type).upper()
        should_enable = (target == 'CPU' and is_cpu) or (target == 'GPU' and ((is_cpu and include_cpu) or (not is_cpu and backend_matches and (enable_all or matches_selection))))
        try:
            device.use = bool(should_enable)
        except Exception as exc:
            print(f'Worker Cycles config: could not set device {device_name}: {exc}')
        is_enabled = bool(getattr(device, 'use', False))
        if is_enabled and device_type.upper() != 'CPU':
            enabled_gpu_device_count += 1
        device_summaries.append(f"{device_name}|type={device_type}|use={is_enabled}|id={device_id}")
    devices_summary = ', '.join(device_summaries) if device_summaries else 'none'
    print(f'Worker Cycles config: target={target} compute_device_type={compute_device_type} devices=[{devices_summary}]')
    if target == 'GPU' and str(compute_device_type or '').upper() in {'', 'NONE'}:
        print('Worker Cycles config warning: target=GPU but compute_device_type is not configured in Blender preferences')
    if target == 'GPU' and enabled_gpu_device_count == 0:
        print('Worker Cycles config warning: target=GPU but no enabled GPU devices were found in Blender preferences')
for scene in bpy.data.scenes:
    cycles_settings = getattr(scene, 'cycles', None)
    if cycles_settings is not None:
        cycles_settings.device = target
