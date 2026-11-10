"""Build an isolated portable release; never test inside the published bundle.

Usage: python build_support/package_release.py coordinator --build-root <dir> --output <dir>
Requires each application's existing .venv. Existing deliverables are never overwritten.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

VERSION = "1.2.2"
SUITE_ROOT = Path(__file__).resolve().parents[1]
APPLICATIONS = {
    "coordinator": ("coordinator/blender_render_farm_coordinator", "BlenderRenderFarmCoordinator"),
    "worker": ("worker/blender_render_farm_worker", "BlenderRenderFarmWorker"),
    "master": ("master/blender_render_farm_master", "BlenderRenderFarmMaster"),
    "viewer": ("image_viewer/BlenderImageViewer", "BlenderImageViewer"),
}


def clean_config(application: str, original: dict) -> dict:
    config = dict(original)
    config["language"] = "en"
    if application == "coordinator":
        config.update(coordinatorId="", databasePath="app/data/coordinator.db", logsDir="app/data/logs")
    elif application == "master":
        config.update(master_id="", master_name="", coordinator_url="", workers=[])
    elif application == "worker":
        config.update(workerName="", blenderPath="", authToken="")
    elif application == "viewer":
        config.update(last_image_folder="", last_blend_file="", last_output_folder="")
    return config


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_archive(path: Path, application: str) -> None:
    with zipfile.ZipFile(path) as archive:
        configs = []
        for member in archive.infolist():
            relative = Path(member.filename)
            if relative.suffix.lower() in {".db", ".log"} or relative.name.endswith((".db-wal", ".db-shm")):
                raise ValueError(f"Runtime data in release: {member.filename}")
            if relative.name == "config.json" and len(relative.parts) <= 3:
                payload = json.loads(archive.read(member).decode("utf-8-sig"))
                if clean_config(application, payload) != payload:
                    raise ValueError(f"Unclean configuration: {member.filename}")
                configs.append(member.filename)
        if not configs:
            raise ValueError("Missing portable configuration")
        corrupted = archive.testzip()
        if corrupted:
            raise ValueError(f"Corrupted archive entry: {corrupted}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("application", choices=APPLICATIONS)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--standalone", action="store_true")
    args = parser.parse_args()
    if args.standalone and args.application != "master":
        parser.error("Standalone packaging is supported for Master only")
    relative_project, name = APPLICATIONS[args.application]
    project = SUITE_ROOT / relative_project
    mode = "standalone" if args.standalone else "portable"
    release_name = f"{name}-{VERSION}-{mode}"
    destination = args.output.resolve() / (release_name + ".exe" if args.standalone else release_name)
    archive_path = args.output.resolve() / f"{release_name}.zip"
    build_root = args.build_root.resolve() / f"{args.application}-{mode}"
    for path in (destination, archive_path, build_root):
        if path.exists():
            raise FileExistsError(f"Use a fresh destination: {path}")
    build_root.mkdir(parents=True)
    args.output.mkdir(parents=True, exist_ok=True)
    spec = name + ("Standalone" if args.standalone else "") + ".spec"
    command = [str(project / ".venv/Scripts/python.exe"), "-m", "PyInstaller", "--noconfirm",
               "--distpath", str(build_root / "dist"), "--workpath", str(build_root / "build"), spec]
    with (build_root / "build.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, cwd=project, stdout=log, stderr=subprocess.STDOUT, check=True)
    if args.standalone:
        shutil.copy2(build_root / "dist" / f"{name}.exe", destination)
        artifact = destination
    else:
        shutil.copytree(build_root / "dist" / name, destination,
                        ignore=shutil.ignore_patterns("*.log", "*.db", "*.db-wal", "*.db-shm", "__pycache__"))
        config = clean_config(args.application, json.loads((project / "config.json").read_text(encoding="utf-8-sig")))
        serialized = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
        (destination / "config.json").write_text(serialized, encoding="utf-8")
        embedded_config = destination / "_internal/config.json"
        if embedded_config.exists():
            embedded_config.write_text(serialized, encoding="utf-8")
        if not (destination / "img").exists():
            shutil.copytree(project / "img", destination / "img")
        metadata = {
            "appVersion": VERSION, "protocolVersion": 2,
            "buildId": f"{args.application}-{VERSION}-qa",
            "buildTimestampUtc": datetime.now(timezone.utc).isoformat(),
            "bundleHash": digest(destination / f"{name}.exe"),
            "requirementsHash": digest(project / "requirements.txt"),
        }
        (destination / "build_info.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        (destination / "PORTABLE.txt").write_text(
            f"{name} {VERSION}\nKeep the entire folder, including _internal.\n"
            "Run the application from an extracted copy. IDs are generated on first start.\n"
            "Do not replace an active render farm installation during a job.\n"
            "See the QA report for validation results and remaining issues.\n", encoding="utf-8")
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for file in sorted(destination.rglob("*")):
                if file.is_file():
                    archive.write(file, file.relative_to(destination.parent))
        validate_archive(archive_path, args.application)
        artifact = archive_path
    result = {"file": artifact.name, "bytes": artifact.stat().st_size, "sha256": digest(artifact), "version": VERSION}
    (args.output / f"{release_name}.sha256.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
