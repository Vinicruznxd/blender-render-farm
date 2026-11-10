from __future__ import annotations

import getpass
import ntpath
import os
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from .blender_runner import inspect_blend_file_version
from .models import ValidatePathsResponse
from .version_routing import normalize_version_routing_policy

if TYPE_CHECKING:
    from .blender_runner import BlenderRunner

WINDOWS_DRIVE_PATTERN = re.compile(r"^(?P<drive>[A-Za-z]):[\\/]")


def normalize_windows_path(raw_path: str) -> str:
    cleaned = raw_path.strip()
    if not cleaned:
        return cleaned
    return ntpath.normpath(cleaned)


def get_process_user() -> str:
    try:
        return psutil.Process(os.getpid()).username()
    except Exception:
        try:
            return getpass.getuser()
        except Exception:
            return os.environ.get("USERNAME", "unknown")


def get_process_cwd() -> str:
    try:
        return str(Path.cwd())
    except OSError:
        return ""


def extract_windows_drive(raw_path: str) -> str | None:
    match = WINDOWS_DRIVE_PATTERN.match(raw_path.strip())
    if not match:
        return None
    return match.group("drive").upper()


def build_unmounted_drive_message(drive_letter: str) -> str:
    return (
        f"Drive {drive_letter}: nao esta montado no contexto do processo do worker. "
        "O usuario logado pode enxergar essa unidade, mas o processo atual do worker nao. "
        "Execute o worker no mesmo usuario que mapeou a unidade ou use caminho UNC."
    )


def _drive_root_exists(drive_letter: str) -> bool:
    return Path(f"{drive_letter}:\\").exists()


def _path_exists(path: Path) -> bool:
    return path.exists()


def _path_is_file(path: Path) -> bool:
    return path.is_file()


def _path_is_dir(path: Path) -> bool:
    return path.is_dir()


def _mkdir_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _probe_blend_file_readable(path: Path) -> None:
    with path.open("rb") as file:
        file.read(1)


def _probe_output_folder_writable(path: Path) -> None:
    temp_file = path / f".worker_write_test_{uuid.uuid4().hex}.tmp"
    temp_file.write_text("write test", encoding="utf-8")
    temp_file.unlink(missing_ok=True)


def detect_unmounted_drive_issue(raw_path: str) -> str | None:
    drive_letter = extract_windows_drive(raw_path)
    if not drive_letter:
        return None
    if _drive_root_exists(drive_letter):
        return None
    return build_unmounted_drive_message(drive_letter)


def validate_paths(
    blend_file: str,
    output_folder: str,
    logger,
    *,
    blender_runner: BlenderRunner | None = None,
    required_blender_version: str | None = None,
    preferred_blender_major: int | None = None,
    version_routing_policy: str | None = None,
    blend_version_detected_hint: str | None = None,
) -> ValidatePathsResponse:
    process_user = get_process_user()
    cwd = get_process_cwd()
    normalized_blend_file = normalize_windows_path(blend_file)
    normalized_output_folder = normalize_windows_path(output_folder)

    logger.info(
        "Path validation started blendFile=%s outputFolder=%s normalizedBlendFile=%s "
        "normalizedOutputFolder=%s processUser=%s cwd=%s",
        blend_file,
        output_folder,
        normalized_blend_file,
        normalized_output_folder,
        process_user,
        cwd,
    )

    errors: list[str] = []
    details: list[str] = []
    drive_issue_messages: list[str] = []

    def record_error(message: str) -> None:
        if message not in errors:
            errors.append(message)
        logger.warning(message)

    def record_detail(message: str) -> None:
        if message not in details:
            details.append(message)

    blend_path = Path(normalized_blend_file)
    output_path = Path(normalized_output_folder)

    blend_exists = False
    blend_is_file = False
    blend_accessible = False
    output_exists = False
    output_is_dir = False
    output_created = False
    can_write_output = False
    output_accessible = False
    blend_version_detected = ""
    version_source = ""
    blend_header_format = ""
    blend_compression = ""
    resolved_version_routing_policy = normalize_version_routing_policy(version_routing_policy)
    selected_blender_version = ""
    selected_blender_path = ""
    selected_version_bucket = ""
    same_major_sufficient_available = False
    same_major_older_available = False
    best_same_major_older_version = ""
    higher_major_available = False
    best_higher_major_version = ""
    compatible = blender_runner is None
    compatibility_reason = (
        "Compatibility check skipped because no Blender inventory was provided."
        if blender_runner is None
        else ""
    )

    if not normalized_blend_file.lower().endswith(".blend"):
        record_error("blendFile must end with .blend")

    blend_drive_issue = detect_unmounted_drive_issue(normalized_blend_file)
    output_drive_issue = detect_unmounted_drive_issue(normalized_output_folder)

    for drive_issue in [blend_drive_issue, output_drive_issue]:
        if drive_issue and drive_issue not in drive_issue_messages:
            drive_issue_messages.append(drive_issue)
            record_error(drive_issue)

    if blend_drive_issue is None:
        blend_exists = _path_exists(blend_path)
        if not blend_exists:
            record_error(f"Blend file not found: {normalized_blend_file}")
        else:
            blend_is_file = _path_is_file(blend_path)
            if not blend_is_file:
                record_error(f"Blend path is not a file: {normalized_blend_file}")
            else:
                try:
                    _probe_blend_file_readable(blend_path)
                    blend_accessible = True
                    record_detail("Blend file is readable.")
                except PermissionError as exc:
                    record_error(
                        f"Permission denied reading blend file: {normalized_blend_file} ({exc})"
                    )
                except OSError as exc:
                    record_error(f"Could not read blend file: {normalized_blend_file} ({exc})")

    if output_drive_issue is None:
        output_exists = _path_exists(output_path)
        if output_exists:
            output_is_dir = _path_is_dir(output_path)
            if not output_is_dir:
                record_error(f"Output path is not a directory: {normalized_output_folder}")
        else:
            try:
                logger.info("Output folder does not exist, attempting creation: %s", output_path)
                _mkdir_path(output_path)
                output_exists = _path_exists(output_path)
                output_is_dir = output_exists and _path_is_dir(output_path)
                output_created = output_exists and output_is_dir
                if output_created:
                    logger.info("Output folder created successfully: %s", output_path)
                    record_detail("Output folder was created successfully.")
                else:
                    record_error(
                        f"Could not create output folder: {normalized_output_folder} "
                        "(directory still unavailable after creation attempt)"
                    )
            except PermissionError as exc:
                record_error(
                    f"Permission denied creating output folder: {normalized_output_folder} ({exc})"
                )
            except OSError as exc:
                record_error(
                    f"Could not create output folder: {normalized_output_folder} ({exc})"
                )

        if output_exists and output_is_dir:
            try:
                _probe_output_folder_writable(output_path)
                can_write_output = True
                output_accessible = True
                record_detail("Output folder is writable.")
            except PermissionError as exc:
                record_error(
                    f"Permission denied writing to output folder: {normalized_output_folder} ({exc})"
                )
            except OSError as exc:
                record_error(
                    f"Cannot write to output folder: {normalized_output_folder} ({exc})"
                )

    if blend_accessible:
        if blender_runner is None:
            blend_version_info = inspect_blend_file_version(blend_path)
            blend_version_detected = blend_version_info.version
            version_source = blend_version_info.version_source
            blend_header_format = blend_version_info.header_format
            blend_compression = blend_version_info.compression
            if blend_version_info.detected:
                record_detail(f"Blend file version detected: {blend_version_detected}.")
            else:
                record_detail(blend_version_info.reason)
        else:
            selection = blender_runner.select_blender_for_blend_file(
                blend_path,
                required_blender_version=required_blender_version,
                preferred_blender_major=preferred_blender_major,
                version_routing_policy=version_routing_policy,
                blend_version_detected_hint=blend_version_detected_hint,
            )
            blend_version_detected = selection.blend_version_detected
            version_source = selection.version_source
            blend_header_format = selection.blend_header_format
            blend_compression = selection.blend_compression
            selected_blender_version = selection.selected_blender_version
            selected_blender_path = selection.selected_blender_path
            selected_version_bucket = selection.selected_version_bucket
            same_major_sufficient_available = selection.same_major_sufficient_available
            same_major_older_available = selection.same_major_older_available
            best_same_major_older_version = selection.best_same_major_older_version
            higher_major_available = selection.higher_major_available
            best_higher_major_version = selection.best_higher_major_version
            compatible = selection.compatible
            compatibility_reason = selection.compatibility_reason
            resolved_version_routing_policy = normalize_version_routing_policy(
                selection.version_routing_policy
            )
            if selection.compatible:
                record_detail(selection.compatibility_reason)
            else:
                record_error(selection.compatibility_reason)

    ok = blend_accessible and output_accessible and compatible
    message = "Paths validated successfully." if ok else (
        drive_issue_messages[0] if drive_issue_messages else (errors[0] if errors else "Path validation failed.")
    )

    diagnostic_summary = (
        f"processUser={process_user}; cwd={cwd}; "
        f"normalizedBlendFile={normalized_blend_file}; "
        f"normalizedOutputFolder={normalized_output_folder}; "
        f"outputCreated={output_created}; "
        f"blendExists={blend_exists}; outputExists={output_exists}; "
        f"blendAccessible={blend_accessible}; outputAccessible={output_accessible}; "
        f"blendVersionDetected={blend_version_detected or 'unknown'}; "
        f"versionSource={version_source or 'unknown'}; "
        f"blendHeaderFormat={blend_header_format or 'unknown'}; "
        f"blendCompression={blend_compression or 'unknown'}; "
        f"selectedBlenderVersion={selected_blender_version or 'none'}; "
        f"selectedBlenderPath={selected_blender_path or 'none'}; "
        f"selectedVersionBucket={selected_version_bucket or 'none'}; "
        f"sameMajorSufficientAvailable={same_major_sufficient_available}; "
        f"sameMajorOlderAvailable={same_major_older_available}; "
        f"bestSameMajorOlderVersion={best_same_major_older_version or 'none'}; "
        f"higherMajorAvailable={higher_major_available}; "
        f"bestHigherMajorVersion={best_higher_major_version or 'none'}; "
        f"compatible={compatible}; compatibilityReason={compatibility_reason or 'n/a'}"
    )
    if details:
        diagnostic_summary = f"{diagnostic_summary}; notes={' | '.join(details)}"

    logger.info(
        "Path validation finished ok=%s blendAccessible=%s outputAccessible=%s "
        "blendExists=%s outputExists=%s outputCreated=%s canWriteOutput=%s "
        "compatible=%s blendVersionDetected=%s selectedBlenderVersion=%s selectedBlenderPath=%s message=%s",
        ok,
        blend_accessible,
        output_accessible,
        blend_exists,
        output_exists,
        output_created,
        can_write_output,
        compatible,
        blend_version_detected,
        selected_blender_version,
        selected_blender_path,
        message,
    )
    if errors:
        logger.warning("Path validation errors: %s", " | ".join(errors))

    return ValidatePathsResponse(
        ok=ok,
        blendAccessible=blend_accessible,
        outputAccessible=output_accessible,
        message=message,
        normalizedBlendFile=normalized_blend_file,
        normalizedOutputFolder=normalized_output_folder,
        blendExists=blend_exists,
        outputExists=output_exists,
        outputCreated=output_created,
        processUser=process_user,
        cwd=cwd,
        details=diagnostic_summary,
        blendFileExists=blend_exists and blend_is_file,
        outputFolderExists=output_exists and output_is_dir,
        canWriteOutput=can_write_output,
        blendVersionDetected=blend_version_detected,
        versionSource=version_source,
        blendHeaderFormat=blend_header_format,
        blendCompression=blend_compression,
        requiredBlenderVersion=required_blender_version or blend_version_detected,
        preferredBlenderMajor=preferred_blender_major,
        versionRoutingPolicy=resolved_version_routing_policy,
        selectedBlenderVersion=selected_blender_version,
        selectedBlenderPath=selected_blender_path,
        selectedVersionBucket=selected_version_bucket,
        sameMajorSufficientAvailable=same_major_sufficient_available,
        sameMajorOlderAvailable=same_major_older_available,
        bestSameMajorOlderVersion=best_same_major_older_version,
        higherMajorAvailable=higher_major_available,
        bestHigherMajorVersion=best_higher_major_version,
        compatible=compatible,
        compatibilityReason=compatibility_reason,
        errors=errors,
    )
