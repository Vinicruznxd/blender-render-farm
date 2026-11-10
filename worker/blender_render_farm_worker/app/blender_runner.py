from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

try:
    import winreg
except ImportError:  # pragma: no cover - only relevant outside Windows
    winreg = None

try:
    import zstandard
except ImportError:  # pragma: no cover - optional dependency during bootstrap
    zstandard = None

from .config import (
    COMMON_BLENDER_PATHS,
    ConfigManager,
    WorkerConfig,
    render_device_preference_to_use_gpu,
)
from .logger import get_job_log_path
from .models import RenderBlockRequest
from .persistent_session import (
    PersistentSessionManager,
    QueuedRenderRequest,
    SessionCompatibilityKey,
)
from .status_manager import WorkerStatusManager
from .subprocess_utils import decode_subprocess_output
from .version_routing import (
    DEFAULT_VERSION_ROUTING_POLICY,
    PREFER_HIGHER_MAJOR_WHEN_SAME_MAJOR_OLDER_POLICY,
    PREFER_SAME_MAJOR_ANY_VERSION_POLICY,
    SELECTION_BUCKET_EXACT,
    SELECTION_BUCKET_HIGHER_MAJOR,
    SELECTION_BUCKET_SAME_MAJOR_OLDER,
    SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT,
    classify_selected_version_bucket,
    normalize_version_routing_policy,
)

if TYPE_CHECKING:
    from .coordinator_discovery import CoordinatorDiscoveryService

FRAME_REGEX = re.compile(r"Fra:(\d+)")
SAMPLE_PROGRESS_REGEX = re.compile(r"\bSample\s+(\d+)\s*/\s*(\d+)\b", re.IGNORECASE)
TILE_PROGRESS_REGEX = re.compile(
    r"\bRendered\s+(\d+)\s*/\s*(\d+)\s+Tiles\b",
    re.IGNORECASE,
)
PATH_TRACING_TILE_PROGRESS_REGEX = re.compile(r"\b(?:Path\s+Tracing\s+)?Tile\s+(\d+)\s*/\s*(\d+)\b", re.IGNORECASE)
REMAINING_TIME_REGEX = re.compile(r"\bRemaining:\s*([0-9:]+(?:\.[0-9]+)?)\b", re.IGNORECASE)
SAVED_OUTPUT_REGEX = re.compile(r"^\s*Saved:", re.IGNORECASE)
FRAME_HASH_REGEX = re.compile(r"#+")
BLENDER_EXE_PATTERN = re.compile(r'([A-Za-z]:\\[^"\r\n]*?blender\.exe)', re.IGNORECASE)
BLENDER_VERSION_OUTPUT_REGEX = re.compile(r"Blender\s+([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)
BLEND_HEADER_VERSION_REGEX = re.compile(rb"^BLENDER(?:\d{2,}-\d{2,})?[_-]?[vV](\d{3,4})")
LEGACY_BLEND_HEADER_PREFIX_REGEX = re.compile(rb"^BLENDER[_-][vV]")
MODERN_BLEND_HEADER_PREFIX_REGEX = re.compile(rb"^BLENDER\d{2,}-\d{2,}[vV]")
REGISTRY_UNINSTALL_PATHS = [
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
]
BLEND_HEADER_MAGIC = b"BLENDER"
BLEND_HEADER_MIN_SIZE = 12
BLEND_HEADER_READ_SIZE = 32
GZIP_MAGIC = b"\x1f\x8b"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
FORMAT_EXTENSION_MAP = {
    "PNG": "png",
    "JPEG": "jpg",
    "JPG": "jpg",
    "OPEN_EXR": "exr",
    "EXR": "exr",
    "TIFF": "tif",
    "BMP": "bmp",
}
BLENDER_HEADLESS_PROBE_SENTINEL = "BLENDER_HEADLESS_STARTUP_OK"
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002
SEM_NOOPENFILEERRORBOX = 0x8000


def extract_blender_executable(raw_value: str) -> str | None:
    cleaned = raw_value.strip().strip('"')
    if not cleaned:
        return None

    match = BLENDER_EXE_PATTERN.search(cleaned)
    if match:
        return match.group(1)

    if cleaned.lower().endswith("blender.exe"):
        return cleaned

    return None


def normalize_blender_candidate(raw_value: str | Path) -> Path | None:
    text = str(raw_value).strip()
    if not text:
        return None

    extracted = extract_blender_executable(text)
    if extracted:
        return Path(extracted)

    candidate = Path(text.strip('"'))
    if candidate.name.lower() == "blender.exe":
        return candidate

    return candidate / "blender.exe"


def deduplicate_paths(paths: Iterable[Path]) -> list[Path]:
    deduplicated: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = str(path).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduplicated.append(path)
    return deduplicated


def iter_windows_blender_search_roots(environ: dict[str, str] | None = None) -> list[Path]:
    env = environ or dict(os.environ)
    roots = [
        Path(r"C:\Program Files\Blender Foundation"),
        Path(r"C:\Program Files (x86)\Blender Foundation"),
    ]

    program_files = env.get("PROGRAMFILES", "").strip()
    if program_files:
        roots.append(Path(program_files) / "Blender Foundation")

    program_files_x86 = env.get("PROGRAMFILES(X86)", "").strip()
    if program_files_x86:
        roots.append(Path(program_files_x86) / "Blender Foundation")

    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        local_programs = Path(local_app_data) / "Programs" / "Blender Foundation"
        roots.extend(
            [
                local_programs,
                local_programs / "Blender",
            ]
        )

    return deduplicate_paths(roots)


def discover_blender_executables(root: Path) -> list[Path]:
    if not root.exists():
        return []

    if root.is_file():
        return [root] if root.name.lower() == "blender.exe" else []

    discovered: list[Path] = []
    direct_candidate = root / "blender.exe"
    if direct_candidate.is_file():
        discovered.append(direct_candidate)

    recursive_candidates = sorted(root.rglob("blender.exe"), reverse=True)
    discovered.extend(recursive_candidates)
    return deduplicate_paths(discovered)


def read_registry_value(key, value_name: str) -> str | None:
    try:
        value, _ = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None

    if value is None:
        return None
    return str(value).strip()


@dataclass(frozen=True, slots=True)
class ParsedBlenderVersion:
    value: tuple[int, ...]
    display: str

    @property
    def major(self) -> int:
        return self.value[0]

    @property
    def minor(self) -> int:
        return self.value[1] if len(self.value) > 1 else 0

    @property
    def major_minor(self) -> tuple[int, int]:
        return (self.major, self.minor)


@dataclass(frozen=True, slots=True)
class BlendFileVersionInfo:
    detected: bool
    version: str = ""
    reason: str = ""
    parsed_version: ParsedBlenderVersion | None = None
    header_format: str = ""
    compression: str = ""
    version_source: str = ""


@dataclass(frozen=True, slots=True)
class BlenderInstallation:
    path: str
    version: str
    parsed_version: ParsedBlenderVersion | None
    priority: int
    startup_ok: bool = True
    startup_error: str = ""


@dataclass(frozen=True, slots=True)
class BlenderSelectionResult:
    compatible: bool
    blend_version_detected: str = ""
    version_source: str = ""
    blend_header_format: str = ""
    blend_compression: str = ""
    required_blender_version: str = ""
    preferred_blender_major: int | None = None
    version_routing_policy: str = ""
    selected_blender_version: str = ""
    selected_blender_path: str = ""
    selected_version_bucket: str = ""
    same_major_sufficient_available: bool = False
    same_major_older_available: bool = False
    best_same_major_older_version: str = ""
    higher_major_available: bool = False
    best_higher_major_version: str = ""
    compatibility_reason: str = ""


def parse_blender_version(raw_value: str) -> ParsedBlenderVersion | None:
    cleaned = raw_value.strip()
    if not cleaned:
        return None

    parts = cleaned.split(".")
    if not all(part.isdigit() for part in parts):
        return None

    numeric_parts = tuple(int(part) for part in parts)
    normalized = ".".join(str(part) for part in numeric_parts)
    return ParsedBlenderVersion(value=numeric_parts, display=normalized)


def _format_blend_version(major: int, minor: int) -> str:
    return f"{major}.{minor}"


def build_output_path(output_pattern: str, frame_number: int, image_format: str) -> Path:
    format_extension = FORMAT_EXTENSION_MAP.get(image_format.upper(), image_format.lower())
    if "#" in output_pattern:
        rendered = FRAME_HASH_REGEX.sub(
            lambda match: f"{frame_number:0{len(match.group(0))}d}",
            output_pattern,
            count=1,
        )
        path = Path(rendered)
        return path if path.suffix else path.with_suffix(f".{format_extension}")

    path = Path(output_pattern)
    if path.suffix:
        return path.with_name(f"{path.stem}{frame_number:04d}{path.suffix}")
    return Path(f"{output_pattern}{frame_number:04d}.{format_extension}")


def _read_blend_header(blend_path: Path) -> tuple[bytes, str]:
    with blend_path.open("rb") as file:
        prefix = file.read(4)

    if prefix[:2] == GZIP_MAGIC:
        with gzip.open(blend_path, "rb") as file:
            return file.read(BLEND_HEADER_READ_SIZE), "gzip"

    if prefix == ZSTD_MAGIC:
        if zstandard is None:
            raise OSError(
                "This .blend file is compressed with Zstandard, but the zstandard package is not installed."
            )
        with blend_path.open("rb") as file:
            try:
                with zstandard.ZstdDecompressor().stream_reader(file) as reader:
                    return reader.read(BLEND_HEADER_READ_SIZE), "zstd"
            except zstandard.ZstdError as exc:
                raise OSError(
                    f"Could not decompress Zstandard-compressed blend file header: {blend_path} ({exc})"
                ) from exc

    with blend_path.open("rb") as file:
        return file.read(BLEND_HEADER_READ_SIZE), "raw"

def _parse_blend_version_from_header(header: bytes) -> tuple[ParsedBlenderVersion | None, str]:
    match = BLEND_HEADER_VERSION_REGEX.match(header)
    if not match:
        return None, ""

    version_code = match.group(1).decode("ascii")
    if MODERN_BLEND_HEADER_PREFIX_REGEX.match(header):
        header_format = "modern_v1"
    elif LEGACY_BLEND_HEADER_PREFIX_REGEX.match(header):
        header_format = "legacy_v0"
    else:
        header_format = ""
    if len(version_code) == 3:
        major = int(version_code[:-2])
        minor = int(version_code[-2:])
    elif len(version_code) == 4:
        major = int(version_code[:2])
        minor = int(version_code[2:])
    else:
        return None, header_format

    return parse_blender_version(_format_blend_version(major, minor)), header_format


def inspect_blend_file_version(blend_path: str | Path) -> BlendFileVersionInfo:
    candidate = Path(blend_path)

    try:
        header, compression = _read_blend_header(candidate)
    except OSError as exc:
        return BlendFileVersionInfo(
            detected=False,
            reason=f"Could not read blend file header: {candidate} ({exc})",
            compression="",
        )

    if len(header) < BLEND_HEADER_MIN_SIZE:
        return BlendFileVersionInfo(
            detected=False,
            reason=f"Blend file header is too short to detect the saved Blender version: {candidate}",
            compression=compression,
        )

    if not header.startswith(BLEND_HEADER_MAGIC):
        return BlendFileVersionInfo(
            detected=False,
            reason=(
                "Blend file header is invalid or unsupported. "
                f"Could not detect the saved Blender version for {candidate}."
            ),
            compression=compression,
        )

    parsed_version, header_format = _parse_blend_version_from_header(header)
    if parsed_version is None:
        return BlendFileVersionInfo(
            detected=False,
            reason=(
                "Blend file header does not contain a valid Blender version. "
                f"Could not detect the saved Blender version for {candidate}."
            ),
            compression=compression,
        )
    return BlendFileVersionInfo(
        detected=True,
        version=parsed_version.display,
        reason=f"Blend file version detected: {parsed_version.display}",
        parsed_version=parsed_version,
        header_format=header_format,
        compression=compression,
        version_source="header",
    )


def read_blend_file_version(blend_path: str | Path) -> str | None:
    info = inspect_blend_file_version(blend_path)
    return info.version or None


@dataclass(slots=True)
class BlenderInfo:
    found: bool
    path: str = ""
    version: str = ""


@dataclass(slots=True)
class RenderAttemptResult:
    exit_code: int
    output_paths: list[str]
    output_verified: bool | None


def configure_windows_error_mode() -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        current_mode = int(kernel32.GetErrorMode())
        kernel32.SetErrorMode(
            current_mode
            | SEM_FAILCRITICALERRORS
            | SEM_NOGPFAULTERRORBOX
            | SEM_NOOPENFILEERRORBOX
        )
    except Exception:
        return


class BlenderRunner:
    def __init__(
        self,
        *,
        project_root: Path,
        config: WorkerConfig,
        config_manager: ConfigManager | None = None,
        status_manager: WorkerStatusManager,
        logger,
        coordinator_discovery_service: "CoordinatorDiscoveryService | None" = None,
    ) -> None:
        self.project_root = project_root
        self.config = config
        self.config_manager = config_manager
        self.status_manager = status_manager
        self.logger = logger
        self.coordinator_discovery_service = coordinator_discovery_service
        self._process_lock = threading.RLock()
        self._inventory_lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._cancel_requested = False
        self._blender_info = BlenderInfo(found=False)
        self._blender_inventory: list[BlenderInstallation] = []
        self._persistent_session_manager = PersistentSessionManager(
            project_root=project_root,
            config=config,
            status_manager=status_manager,
            logger=logger,
            configure_windows_error_mode=configure_windows_error_mode,
            build_session_command=self._build_persistent_session_command,
            expected_output_paths=self._expected_output_paths,
            wait_for_output_paths=self._wait_for_output_paths,
            terminate_process_tree=self._terminate_process_tree,
            persist_render_device_preference=self._persist_render_device_preference,
            run_direct_render_fallback=self._run_direct_render_fallback,
            cancel_direct_render=self._cancel_direct_render,
            update_progress_from_line=self._update_progress_from_line,
        )

    @property
    def blender_info(self) -> BlenderInfo:
        return self._blender_info

    @property
    def blender_inventory(self) -> list[BlenderInstallation]:
        with self._inventory_lock:
            return list(self._blender_inventory)

    def detect_blender(self) -> BlenderInfo:
        self.discover_blender_inventory(force_refresh=True)
        return self._blender_info

    def discover_blender_inventory(self, *, force_refresh: bool = False) -> list[BlenderInstallation]:
        with self._inventory_lock:
            if self._blender_inventory and not force_refresh:
                return list(self._blender_inventory)

        inventory = self._scan_blender_inventory()
        default_info = self._default_blender_info_from_inventory(inventory)

        with self._inventory_lock:
            self._blender_inventory = inventory
            self._blender_info = default_info

        self.status_manager.set_blender_info(default_info.found, default_info.path, default_info.version)
        if default_info.found:
            self.logger.info(
                "Default Blender for worker status is %s at %s",
                default_info.version or "unknown",
                default_info.path,
            )
            self.status_manager.append_log(
                f"Default Blender: {default_info.version or 'unknown'} at {default_info.path}"
            )
        return list(inventory)

    def ensure_blender_available(self) -> BlenderInfo:
        if self._blender_info.found:
            return self._blender_info
        self.detect_blender()
        return self._blender_info

    def select_blender_for_blend_file(
        self,
        blend_file: str | Path,
        *,
        required_blender_version: str | None = None,
        preferred_blender_major: int | None = None,
        version_routing_policy: str | None = None,
        blend_version_detected_hint: str | None = None,
    ) -> BlenderSelectionResult:
        blend_path = Path(blend_file)

        inventory = self.discover_blender_inventory()
        selection = self._select_blender_from_inventory(
            blend_path,
            inventory,
            required_blender_version=required_blender_version,
            preferred_blender_major=preferred_blender_major,
            version_routing_policy=version_routing_policy,
            blend_version_detected_hint=blend_version_detected_hint,
        )
        if selection.compatible:
            return selection

        refreshed_inventory = self.discover_blender_inventory(force_refresh=True)
        if refreshed_inventory != inventory:
            refreshed_selection = self._select_blender_from_inventory(
                blend_path,
                refreshed_inventory,
                required_blender_version=required_blender_version,
                preferred_blender_major=preferred_blender_major,
                version_routing_policy=version_routing_policy,
                blend_version_detected_hint=blend_version_detected_hint,
            )
            if refreshed_selection.compatible or refreshed_selection.compatibility_reason != selection.compatibility_reason:
                return refreshed_selection

        return selection

    def is_busy(self) -> bool:
        with self._process_lock:
            if self._process and self._process.poll() is None:
                return True
            if self._thread and self._thread.is_alive():
                return True
        if self._persistent_session_manager.has_logical_session():
            return True
        return self.status_manager.is_busy()

    def start_render(
        self,
        request: RenderBlockRequest,
        *,
        selected_blender_path: str | None = None,
        selected_blender_version: str | None = None,
        blend_version_detected: str | None = None,
    ) -> tuple[bool, str | None]:
        use_persistent_session = (
            self.config.persistentSessionEnabled
            and self._persistent_session_manager.persistent_mode_available()
        )
        if not use_persistent_session and self.is_busy():
            return False, "busy"

        resolved_blender_path = selected_blender_path or ""
        resolved_blender_version = selected_blender_version or ""
        resolved_blend_version = blend_version_detected or ""

        if not resolved_blender_path or not Path(resolved_blender_path).is_file():
            selection = self.select_blender_for_blend_file(
                request.blendFile,
                required_blender_version=request.requiredBlenderVersion,
                preferred_blender_major=request.preferredBlenderMajor,
                version_routing_policy=request.versionRoutingPolicy,
                blend_version_detected_hint=request.blendVersionDetected,
            )
            if not selection.compatible:
                self.logger.warning(
                    "Render rejected job=%s block=%s reason=%s",
                    request.jobId,
                    request.blockId,
                    selection.compatibility_reason,
                )
                self.status_manager.append_log(f"Render rejected: {selection.compatibility_reason}")
                return False, "incompatible_blender_version"

            resolved_blender_path = selection.selected_blender_path
            resolved_blender_version = selection.selected_blender_version
            resolved_blend_version = selection.blend_version_detected

        if resolved_blender_path:
            self.status_manager.set_blender_info(
                True,
                resolved_blender_path,
                resolved_blender_version,
            )

        render_device_preference = self.config.renderDevicePreference
        effective_use_gpu = render_device_preference_to_use_gpu(render_device_preference)
        payload = request.model_dump()
        payload["requestedUseGpu"] = request.useGpu
        payload["useGpu"] = effective_use_gpu
        payload["renderDevicePreference"] = render_device_preference
        payload["blendVersionDetected"] = resolved_blend_version
        payload["requiredBlenderVersion"] = (
            request.requiredBlenderVersion or resolved_blend_version
        )
        payload["preferredBlenderMajor"] = request.preferredBlenderMajor
        payload["versionRoutingPolicy"] = request.versionRoutingPolicy or ""
        payload["selectedBlenderVersion"] = resolved_blender_version
        payload["selectedBlenderPath"] = resolved_blender_path
        payload["executionMode"] = "persistent_session" if use_persistent_session else "direct"
        payload["recoveryActions"] = []

        if use_persistent_session:
            queued_request = QueuedRenderRequest(
                request=request,
                status_payload=payload,
                compatibility_key=SessionCompatibilityKey.from_payload(payload),
                selected_blender_path=resolved_blender_path,
                selected_blender_version=resolved_blender_version,
                blend_version_detected=resolved_blend_version,
                render_device_preference=render_device_preference,
                effective_use_gpu=effective_use_gpu,
            )
            return self._persistent_session_manager.queue_render(queued_request)

        return self._start_direct_render(
            request=request,
            payload=payload,
            render_device_preference=render_device_preference,
            effective_use_gpu=effective_use_gpu,
            selected_blender_path=resolved_blender_path,
            selected_blender_version=resolved_blender_version,
            blend_version_detected=resolved_blend_version,
        )

    def cancel_render(
        self,
        *,
        requested_job_id: str | None = None,
        requested_assignment_id: str | None = None,
    ) -> dict[str, object]:
        if self.config.persistentSessionEnabled and self._persistent_session_manager.has_logical_session():
            return self._persistent_session_manager.cancel_session(
                requested_job_id=requested_job_id,
                requested_assignment_id=requested_assignment_id,
            )

        return self._cancel_direct_render(
            requested_job_id=requested_job_id,
            requested_assignment_id=requested_assignment_id,
        )

    def _start_direct_render(
        self,
        *,
        request: RenderBlockRequest,
        payload: dict[str, object],
        render_device_preference: str,
        effective_use_gpu: bool,
        selected_blender_path: str,
        selected_blender_version: str,
        blend_version_detected: str,
    ) -> tuple[bool, str | None]:
        command = self._build_command(
            selected_blender_path,
            request,
            render_device_preference=render_device_preference,
        )

        thread = threading.Thread(
            target=self._run_render,
            args=(
                request,
                command,
                render_device_preference,
                effective_use_gpu,
                selected_blender_path,
                selected_blender_version,
                blend_version_detected,
            ),
            name=f"render-{request.jobId}-{request.blockId}",
            daemon=True,
        )
        try:
            with self._process_lock:
                if self._process and self._process.poll() is None:
                    return False, "busy"
                if self._thread and self._thread.is_alive():
                    return False, "busy"
                if not self.status_manager.try_begin_render_block(payload):
                    return False, "busy"
                self._cancel_requested = False
                self._thread = thread
            thread.start()
        except Exception as exc:
            self.logger.exception("Failed to start render thread")
            with self._process_lock:
                self._thread = None
                self._cancel_requested = False
            self.status_manager.finish_render_error(
                exit_code=-1,
                elapsed_seconds=0,
                error=str(exc),
                message=f"Failed to start render thread: {exc}",
                failure_category="worker_runtime",
                failure_code="thread_start_failed",
                failure_details=str(exc),
            )
            return False, "thread_start_failed"
        return True, None

    def _cancel_direct_render(
        self,
        *,
        requested_job_id: str | None = None,
        requested_assignment_id: str | None = None,
    ) -> dict[str, object]:
        previous_job_id, previous_block_id = self.status_manager.get_current_identifiers()
        if previous_job_id is None or previous_block_id is None:
            return {
                "cancelled": False,
                "message": "no active render",
            }

        active_summary = self.status_manager.get_active_render_summary()
        active_assignment_id = active_summary.get("assignmentId")
        if requested_job_id and requested_job_id != previous_job_id:
            return {
                "cancelled": False,
                "message": "active render does not match requested jobId",
                "currentJobId": previous_job_id,
                "currentBlockId": previous_block_id,
                "assignmentId": active_assignment_id,
            }
        if requested_assignment_id and requested_assignment_id != active_assignment_id:
            return {
                "cancelled": False,
                "message": "active render does not match requested assignmentId",
                "currentJobId": previous_job_id,
                "currentBlockId": previous_block_id,
                "assignmentId": active_assignment_id,
            }

        with self._process_lock:
            process = self._process
            thread = self._thread
            self._cancel_requested = True

        self.status_manager.mark_cancelling()
        self.logger.info("Cancelling render for job=%s block=%s", previous_job_id, previous_block_id)

        wait_deadline = time.time() + 2
        while process is None and time.time() < wait_deadline:
            if not self.status_manager.has_active_render(
                job_id=previous_job_id,
                block_id=previous_block_id,
            ):
                break
            if thread is not None and not thread.is_alive():
                break
            time.sleep(0.05)
            with self._process_lock:
                process = self._process
                thread = self._thread

        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)

        if self.status_manager.has_active_render(job_id=previous_job_id, block_id=previous_block_id):
            self.logger.warning(
                "Render thread cleanup did not finish in time for job=%s block=%s; worker remains busy until Blender exits",
                previous_job_id,
                previous_block_id,
            )
            return {
                "cancelled": True,
                "cleanupPending": True,
                "message": "cancel requested; waiting for Blender to exit",
                "previousJobId": previous_job_id,
                "previousBlockId": previous_block_id,
                "assignmentId": active_assignment_id,
            }

        return {
            "cancelled": True,
            "cleanupPending": False,
            "previousJobId": previous_job_id,
            "previousBlockId": previous_block_id,
            "assignmentId": active_assignment_id,
        }

    def _run_direct_render_fallback(self, queued_request: QueuedRenderRequest, reason: str) -> None:
        request = queued_request.request
        recovery_action = "Persistent Blender session failed, so the worker switched to one-shot render automatically."
        if not self.status_manager.has_active_render(job_id=request.jobId, block_id=request.blockId):
            queued_request.status_payload["executionMode"] = "direct_fallback"
            queued_request.status_payload["persistentSessionFallbackReason"] = reason
            queued_request.status_payload["recoveryActions"] = list(
                queued_request.status_payload.get("recoveryActions") or []
            )
            queued_request.status_payload["recoveryActions"].append(recovery_action)
            if not self.status_manager.try_begin_render_block(queued_request.status_payload):
                raise RuntimeError("render slot is already reserved during direct fallback")
        else:
            self.status_manager.update_active_render_metadata(
                executionMode="direct_fallback",
                persistentSessionFallbackReason=reason,
            )
            self.status_manager.add_active_render_recovery_action(recovery_action)

        self.status_manager.update_active_render_metadata(
            failureCategory="persistent_session",
            failureCode="persistent_session_fallback",
            failureDetails=reason,
        )
        self.status_manager.add_active_render_recovery_action(recovery_action)

        self.status_manager.append_log(
            "Persistent Blender session unavailable; falling back to one-shot Blender render"
        )
        self.logger.warning(
            "Falling back to one-shot Blender render job=%s block=%s reason=%s",
            request.jobId,
            request.blockId,
            reason,
        )
        command = self._build_command(
            queued_request.selected_blender_path,
            request,
            render_device_preference=queued_request.render_device_preference,
        )
        self._run_render(
            request,
            command,
            queued_request.render_device_preference,
            queued_request.effective_use_gpu,
            queued_request.selected_blender_path,
            queued_request.selected_blender_version,
            queued_request.blend_version_detected,
        )

    def request_job_drain(
        self,
        *,
        job_id: str,
        coordinator_id: str | None = None,
    ) -> dict[str, object]:
        if not self.config.persistentSessionEnabled:
            return {
                "accepted": False,
                "jobId": job_id,
                "message": "persistent sessions are disabled",
            }
        return self._persistent_session_manager.request_job_drain(
            job_id=job_id,
            coordinator_id=coordinator_id,
        )

    def shutdown(self) -> None:
        if self.config.persistentSessionEnabled and self._persistent_session_manager.has_logical_session():
            self._persistent_session_manager.shutdown()
            return
        if self.is_busy():
            self.cancel_render()

    def _candidate_paths(self) -> list[Path]:
        ordered_candidates: list[Path] = []

        configured = normalize_blender_candidate(self.config.blenderPath)
        if configured is not None:
            ordered_candidates.append(configured)

        ordered_candidates.extend(
            candidate
            for candidate in (
                normalize_blender_candidate(path)
                for path in COMMON_BLENDER_PATHS
            )
            if candidate is not None
        )

        ordered_candidates.extend(self._collect_environment_candidates())
        ordered_candidates.extend(self._collect_path_candidates())
        ordered_candidates.extend(self._collect_registry_candidates())
        ordered_candidates.extend(self._collect_common_install_candidates())
        return deduplicate_paths(ordered_candidates)

    def _collect_environment_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        for env_name in ("BLENDER_PATH", "BLENDER_HOME"):
            raw_value = os.environ.get(env_name, "").strip()
            normalized = normalize_blender_candidate(raw_value) if raw_value else None
            if normalized is not None:
                candidates.append(normalized)
        return candidates

    def _collect_path_candidates(self) -> list[Path]:
        candidates: list[Path] = []

        for executable_name in ("blender.exe", "blender"):
            located = shutil.which(executable_name)
            normalized = normalize_blender_candidate(located) if located else None
            if normalized is not None:
                candidates.append(normalized)

        try:
            result = subprocess.run(
                ["where.exe", "blender"],
                capture_output=True,
                text=False,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return deduplicate_paths(candidates)

        if result.returncode == 0:
            for line in decode_subprocess_output(result.stdout).splitlines():
                normalized = normalize_blender_candidate(line)
                if normalized is not None:
                    candidates.append(normalized)

        return deduplicate_paths(candidates)

    def _collect_registry_candidates(self) -> list[Path]:
        if winreg is None:
            return []

        raw_candidates: list[str] = []
        raw_candidates.extend(self._read_registry_command_candidates())
        raw_candidates.extend(self._read_registry_uninstall_candidates())

        normalized_candidates = [
            candidate
            for candidate in (
                normalize_blender_candidate(raw_value)
                for raw_value in raw_candidates
            )
            if candidate is not None
        ]
        return deduplicate_paths(normalized_candidates)

    def _read_registry_command_candidates(self) -> list[str]:
        if winreg is None:
            return []

        candidates: list[str] = []
        command_keys = [
            (winreg.HKEY_CLASSES_ROOT, r"blendfile\shell\open\command"),
            (winreg.HKEY_CLASSES_ROOT, r"Applications\blender.exe\shell\open\command"),
        ]

        for hive, key_path in command_keys:
            try:
                value = winreg.QueryValue(hive, key_path)
            except OSError:
                continue
            if value:
                candidates.append(str(value).strip())

        return candidates

    def _read_registry_uninstall_candidates(self) -> list[str]:
        if winreg is None:
            return []

        candidates: list[str] = []
        hives = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]

        for hive in hives:
            for base_path in REGISTRY_UNINSTALL_PATHS:
                try:
                    with winreg.OpenKey(hive, base_path) as uninstall_key:
                        subkey_index = 0
                        while True:
                            try:
                                subkey_name = winreg.EnumKey(uninstall_key, subkey_index)
                            except OSError:
                                break
                            subkey_index += 1

                            try:
                                with winreg.OpenKey(uninstall_key, subkey_name) as app_key:
                                    display_name = read_registry_value(app_key, "DisplayName") or ""
                                    if "blender" not in display_name.lower():
                                        continue

                                    for value_name in ("DisplayIcon", "InstallLocation"):
                                        value = read_registry_value(app_key, value_name)
                                        if value:
                                            candidates.append(value)
                            except OSError:
                                continue
                except OSError:
                    continue

        return candidates

    def _collect_common_install_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        for root in iter_windows_blender_search_roots():
            candidates.extend(discover_blender_executables(root))
        return deduplicate_paths(candidates)

    def _scan_blender_inventory(self) -> list[BlenderInstallation]:
        candidates = self._candidate_paths()
        self.logger.info("Refreshing Blender inventory across %s candidate paths", len(candidates))
        for candidate in candidates:
            self.logger.debug("Checking Blender candidate: %s", candidate)

        inventory: list[BlenderInstallation] = []
        for priority, candidate in enumerate(candidates):
            if not candidate.is_file():
                continue

            version = self._read_blender_version(candidate)
            parsed_version = parse_blender_version(version) if version else None
            startup_ok, startup_error = self._probe_blender_headless_startup(candidate)
            inventory.append(
                BlenderInstallation(
                    path=str(candidate),
                    version=version,
                    parsed_version=parsed_version,
                    priority=priority,
                    startup_ok=startup_ok,
                    startup_error=startup_error,
                )
            )

        if inventory:
            healthy = [item for item in inventory if item.startup_ok]
            self.logger.info(
                "Blender inventory ready with %s installation(s), %s healthy: %s",
                len(inventory),
                len(healthy),
                self._describe_inventory(inventory, include_paths=True),
            )
            self.status_manager.append_log(
                f"Blender inventory ready with {len(inventory)} installation(s)"
            )
        else:
            self.logger.warning(
                "Blender not found. Checked %s candidate paths. Configure blenderPath manually if needed.",
                len(candidates),
            )
            self.status_manager.append_log(
                "Blender not found. Configure blenderPath in config.json if Blender is installed in a custom path."
            )

        return inventory

    def _default_blender_info_from_inventory(
        self,
        inventory: list[BlenderInstallation],
    ) -> BlenderInfo:
        if not inventory:
            return BlenderInfo(found=False, path="", version="")

        healthy_inventory = [item for item in inventory if item.startup_ok]
        if not healthy_inventory:
            return BlenderInfo(found=False, path="", version="")

        versioned = [item for item in healthy_inventory if item.parsed_version is not None]
        if versioned:
            selected = max(
                versioned,
                key=lambda item: (item.parsed_version.value, -item.priority),
            )
        else:
            selected = healthy_inventory[0]

        return BlenderInfo(
            found=True,
            path=selected.path,
            version=selected.version,
        )

    def _select_blender_from_inventory(
        self,
        blend_path: Path,
        inventory: list[BlenderInstallation],
        *,
        required_blender_version: str | None = None,
        preferred_blender_major: int | None = None,
        version_routing_policy: str | None = None,
        blend_version_detected_hint: str | None = None,
    ) -> BlenderSelectionResult:
        blend_info = inspect_blend_file_version(blend_path)
        effective_required_version = (required_blender_version or "").strip()
        detected_hint = (blend_version_detected_hint or "").strip()
        fallback_source = effective_required_version or detected_hint
        fallback_parsed_version = parse_blender_version(fallback_source) if fallback_source else None
        policy = normalize_version_routing_policy(
            version_routing_policy or DEFAULT_VERSION_ROUTING_POLICY
        )

        if blend_info.detected and blend_info.parsed_version is not None:
            resolved_required_version = blend_info.version
            resolved_parsed_version = blend_info.parsed_version
            version_source = blend_info.version_source or "header"
        elif fallback_parsed_version is not None:
            resolved_required_version = fallback_source
            resolved_parsed_version = fallback_parsed_version
            version_source = "hint"
            self.logger.warning(
                "Blend header version detection failed for %s; using provided version hint %s instead.",
                blend_path,
                fallback_source,
            )
        else:
            reason = blend_info.reason or f"Could not detect the saved Blender version for {blend_path}"
            return BlenderSelectionResult(
                compatible=False,
                blend_version_detected=blend_info.version,
                version_source="",
                blend_header_format=blend_info.header_format,
                blend_compression=blend_info.compression,
                required_blender_version=effective_required_version,
                preferred_blender_major=preferred_blender_major,
                version_routing_policy=policy,
                compatibility_reason=reason,
            )

        if not inventory:
            return BlenderSelectionResult(
                compatible=False,
                blend_version_detected=blend_info.version or detected_hint,
                version_source=version_source,
                blend_header_format=blend_info.header_format,
                blend_compression=blend_info.compression,
                required_blender_version=resolved_required_version,
                preferred_blender_major=preferred_blender_major,
                version_routing_policy=policy,
                compatibility_reason=(
                    f"No local Blender installation was found for blend version {resolved_required_version}."
                ),
            )

        healthy_inventory = [item for item in inventory if item.startup_ok]
        if not healthy_inventory:
            broken_descriptions = self._describe_inventory(inventory, include_paths=True)
            reason = (
                "Blender installations were found locally, but none could start correctly in headless mode. "
                f"Checked: {broken_descriptions}."
            )
            return BlenderSelectionResult(
                compatible=False,
                blend_version_detected=blend_info.version or detected_hint,
                version_source=version_source,
                blend_header_format=blend_info.header_format,
                blend_compression=blend_info.compression,
                required_blender_version=resolved_required_version,
                preferred_blender_major=preferred_blender_major,
                version_routing_policy=policy,
                compatibility_reason=reason,
            )

        versioned_inventory = [item for item in healthy_inventory if item.parsed_version is not None]
        if not versioned_inventory:
            return BlenderSelectionResult(
                compatible=False,
                blend_version_detected=blend_info.version or detected_hint,
                version_source=version_source,
                blend_header_format=blend_info.header_format,
                blend_compression=blend_info.compression,
                required_blender_version=resolved_required_version,
                preferred_blender_major=preferred_blender_major,
                version_routing_policy=policy,
                compatibility_reason=(
                    "Blender installations were found locally and started correctly, but none reported a readable version. "
                    f"Installed candidates: {self._describe_inventory(healthy_inventory, include_paths=True)}."
                ),
            )

        required = resolved_parsed_version
        target_major = preferred_blender_major or required.major

        exact_candidates = [
            item
            for item in versioned_inventory
            if item.parsed_version is not None
            and item.parsed_version.major_minor == required.major_minor
        ]
        same_major_sufficient_candidates = [
            item
            for item in versioned_inventory
            if item.parsed_version is not None
            and item.parsed_version.major == target_major
            and item.parsed_version.value >= required.value
        ]
        same_major_older_candidates = [
            item
            for item in versioned_inventory
            if item.parsed_version is not None
            and item.parsed_version.major == target_major
            and item.parsed_version.value < required.value
        ]
        same_major_any_candidates = [
            item
            for item in versioned_inventory
            if item.parsed_version is not None and item.parsed_version.major == target_major
        ]
        newer_major_candidates = [
            item
            for item in versioned_inventory
            if item.parsed_version is not None and item.parsed_version.major > required.major
        ]
        higher_major_available = bool(newer_major_candidates and self.config.allowNewerMajorBlender)
        best_same_major_older_version = self._best_version_text(same_major_older_candidates)
        best_higher_major_version = self._best_version_text(
            newer_major_candidates if self.config.allowNewerMajorBlender else []
        )

        selection_kwargs = {
            "blend_version": blend_info.version or detected_hint or resolved_required_version,
            "version_source": version_source,
            "blend_header_format": blend_info.header_format,
            "blend_compression": blend_info.compression,
            "required_blender_version": resolved_required_version,
            "preferred_blender_major": preferred_blender_major,
            "version_routing_policy": policy,
            "same_major_sufficient_available": bool(same_major_sufficient_candidates),
            "same_major_older_available": bool(same_major_older_candidates),
            "best_same_major_older_version": best_same_major_older_version,
            "higher_major_available": higher_major_available,
            "best_higher_major_version": best_higher_major_version,
        }

        if exact_candidates:
            selected = max(
                exact_candidates,
                key=lambda item: (item.parsed_version.value, -item.priority),
            )
            return self._build_selection_result(
                selected,
                policy_reason="exact major/minor match",
                **selection_kwargs,
            )

        if policy == PREFER_SAME_MAJOR_ANY_VERSION_POLICY and same_major_any_candidates:
            selected = max(
                same_major_any_candidates,
                key=lambda item: (item.parsed_version.value, -item.priority),
            )
            return self._build_selection_result(
                selected,
                policy_reason="same major fallback",
                **selection_kwargs,
            )

        if same_major_sufficient_candidates:
            selected = min(
                same_major_sufficient_candidates,
                key=lambda item: (item.parsed_version.value, item.priority),
            )
            return self._build_selection_result(
                selected,
                policy_reason="same major fallback",
                **selection_kwargs,
            )

        if higher_major_available:
            if policy != PREFER_SAME_MAJOR_ANY_VERSION_POLICY or not same_major_older_candidates:
                selected = min(
                    newer_major_candidates,
                    key=lambda item: (item.parsed_version.major, item.parsed_version.value, item.priority),
                )
                return self._build_selection_result(
                    selected,
                    policy_reason="newer major fallback",
                    **selection_kwargs,
                )

        if newer_major_candidates and not self.config.allowNewerMajorBlender:
            reason = (
                f"Blend version {resolved_required_version} requires a newer Blender major version, "
                "but allowNewerMajorBlender is disabled. "
                f"Installed candidates: {self._describe_inventory(versioned_inventory, include_paths=True)}."
            )
        elif policy == PREFER_HIGHER_MAJOR_WHEN_SAME_MAJOR_OLDER_POLICY and same_major_older_candidates:
            reason = (
                f"Only older Blender {target_major}.x versions are available for blend version {resolved_required_version}, "
                "and no newer Blender major version is available for the selected routing policy. "
                f"Installed candidates: {self._describe_inventory(versioned_inventory, include_paths=True)}."
            )
        elif same_major_older_candidates:
            reason = (
                f"Only older Blender {target_major}.x versions are available for blend version {resolved_required_version}. "
                f"Installed candidates: {self._describe_inventory(versioned_inventory, include_paths=True)}."
            )
        else:
            reason = (
                f"No compatible Blender installation found for blend version {resolved_required_version}. "
                f"Installed candidates: {self._describe_inventory(versioned_inventory, include_paths=True)}."
            )

        return BlenderSelectionResult(
            compatible=False,
            blend_version_detected=blend_info.version or detected_hint,
            version_source=version_source,
            blend_header_format=blend_info.header_format,
            blend_compression=blend_info.compression,
            required_blender_version=resolved_required_version,
            preferred_blender_major=preferred_blender_major,
            version_routing_policy=policy,
            same_major_sufficient_available=bool(same_major_sufficient_candidates),
            same_major_older_available=bool(same_major_older_candidates),
            best_same_major_older_version=best_same_major_older_version,
            higher_major_available=higher_major_available,
            best_higher_major_version=best_higher_major_version,
            compatibility_reason=reason,
        )

    def _build_selection_result(
        self,
        installation: BlenderInstallation,
        *,
        blend_version: str,
        version_source: str,
        blend_header_format: str,
        blend_compression: str,
        required_blender_version: str,
        preferred_blender_major: int | None,
        version_routing_policy: str,
        policy_reason: str,
        same_major_sufficient_available: bool,
        same_major_older_available: bool,
        best_same_major_older_version: str,
        higher_major_available: bool,
        best_higher_major_version: str,
    ) -> BlenderSelectionResult:
        selected_version = installation.version
        if not selected_version and installation.parsed_version is not None:
            selected_version = installation.parsed_version.display
        selected_bucket = classify_selected_version_bucket(
            required_blender_version,
            selected_version,
            preferred_major=preferred_blender_major,
        )

        return BlenderSelectionResult(
            compatible=True,
            blend_version_detected=blend_version,
            version_source=version_source,
            blend_header_format=blend_header_format,
            blend_compression=blend_compression,
            required_blender_version=required_blender_version,
            preferred_blender_major=preferred_blender_major,
            version_routing_policy=version_routing_policy,
            selected_blender_version=selected_version,
            selected_blender_path=installation.path,
            selected_version_bucket=selected_bucket,
            same_major_sufficient_available=same_major_sufficient_available,
            same_major_older_available=same_major_older_available,
            best_same_major_older_version=best_same_major_older_version,
            higher_major_available=higher_major_available,
            best_higher_major_version=best_higher_major_version,
            compatibility_reason=(
                f"Selected Blender {selected_version} at {installation.path} "
                f"for blend version {blend_version} ({policy_reason})."
            ),
        )

    @staticmethod
    def _best_version_text(candidates: list[BlenderInstallation]) -> str:
        if not candidates:
            return ""
        selected = max(
            candidates,
            key=lambda item: (item.parsed_version.value if item.parsed_version is not None else (), -item.priority),
        )
        return selected.version or (selected.parsed_version.display if selected.parsed_version is not None else "")

    def _describe_inventory(
        self,
        inventory: list[BlenderInstallation],
        *,
        include_paths: bool,
    ) -> str:
        descriptions: list[str] = []
        for item in inventory:
            version = item.version or "unknown"
            startup_suffix = ""
            if not item.startup_ok and item.startup_error:
                startup_suffix = f" [startup failed: {item.startup_error}]"
            if include_paths:
                descriptions.append(f"{version} at {item.path}{startup_suffix}")
            else:
                descriptions.append(f"{version}{startup_suffix}")
        return "; ".join(descriptions) if descriptions else "none"

    def _read_blender_version(self, blender_path: Path) -> str:
        configure_windows_error_mode()
        try:
            result = subprocess.run(
                [str(blender_path), "--version"],
                capture_output=True,
                text=False,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self.logger.warning("Failed to read Blender version from %s: %s", blender_path, exc)
            return ""

        combined_output = "\n".join(
            part
            for part in [
                decode_subprocess_output(result.stdout),
                decode_subprocess_output(result.stderr),
            ]
            if part
        )
        match = BLENDER_VERSION_OUTPUT_REGEX.search(combined_output)
        return match.group(1) if match else ""

    def _probe_blender_headless_startup(self, blender_path: Path) -> tuple[bool, str]:
        configure_windows_error_mode()
        try:
            result = subprocess.run(
                [
                    str(blender_path),
                    "-b",
                    "--factory-startup",
                    "--disable-autoexec",
                    "--python-expr",
                    (
                        "import sys;"
                        f"print('{BLENDER_HEADLESS_PROBE_SENTINEL}');"
                        "sys.stdout.flush()"
                    ),
                ],
                capture_output=True,
                text=False,
                timeout=20,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            return False, f"launch failed: {exc}"
        except subprocess.TimeoutExpired:
            return False, "startup probe timed out"

        combined_output = "\n".join(
            part
            for part in [
                decode_subprocess_output(result.stdout),
                decode_subprocess_output(result.stderr),
            ]
            if part
        )
        if result.returncode == 0 and BLENDER_HEADLESS_PROBE_SENTINEL in combined_output:
            return True, ""

        detail = combined_output.strip().splitlines()
        summary = detail[-1] if detail else f"exit code {result.returncode}"
        return False, summary[:300]

    def _build_command(
        self,
        blender_path: str,
        request: RenderBlockRequest,
        *,
        render_device_preference: str,
    ) -> list[str]:
        command = [
            blender_path,
            "-b",
            request.blendFile,
            "-o",
            request.outputPattern,
            "-s",
            str(request.startFrame),
            "-e",
            str(request.endFrame),
            "-F",
            request.format,
            "--python-expr",
            self._device_override_expression(render_device_preference),
            "-a",
        ]
        return command

    def _build_persistent_session_command(
        self,
        blender_path: str,
        blend_file: str,
        session_config_path: Path,
    ) -> list[str]:
        bootstrap_script = self._resolve_persistent_session_bootstrap_script()
        return [
            blender_path,
            "-b",
            blend_file,
            "--python",
            str(bootstrap_script),
            "--",
            "--session-config",
            str(session_config_path),
        ]

    def _resolve_persistent_session_bootstrap_script(self) -> Path:
        candidates = [
            self.project_root / "blender_scripts" / "blender_session_bootstrap.py",
            self.project_root / "app" / "blender_session_bootstrap.py",  # legacy bundles
        ]
        meipass_root = getattr(sys, "_MEIPASS", None)
        if meipass_root:
            candidates.append(Path(str(meipass_root)) / "blender_scripts" / "blender_session_bootstrap.py")
            candidates.append(Path(str(meipass_root)) / "app" / "blender_session_bootstrap.py")
        candidates.append(Path(__file__).resolve().parents[1] / "blender_scripts" / "blender_session_bootstrap.py")

        for candidate in candidates:
            if candidate.exists():
                return candidate

        raise FileNotFoundError(
            "Persistent session bootstrap script not found in expected locations: "
            + ", ".join(str(candidate) for candidate in candidates)
        )

    def _device_override_expression(self, render_device_preference: str) -> str:
        # The Blender-side program is GPL-licensed and is loaded as external
        # source, not imported into the noncommercial manager process.
        options = {
            "renderDevicePreference": render_device_preference,
            "gpuBackend": self.config.gpuBackend,
            "gpuDeviceIds": list(self.config.gpuDeviceIds),
            "gpuEnableAll": bool(self.config.gpuEnableAll),
            "gpuIncludeCpu": bool(self.config.gpuIncludeCpu),
        }
        script = self._resolve_blender_script("device_override.py")
        return f"RENDERFARM_DEVICE_OPTIONS = {options!r}\n" + script.read_text(encoding="utf-8")

    def _resolve_blender_script(self, filename: str) -> Path:
        candidates = [self.project_root / "blender_scripts" / filename]
        meipass_root = getattr(sys, "_MEIPASS", None)
        if meipass_root:
            candidates.append(Path(str(meipass_root)) / "blender_scripts" / filename)
        candidates.append(Path(__file__).resolve().parents[1] / "blender_scripts" / filename)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise FileNotFoundError(f"Blender integration script not found: {filename}")

    def _run_render(
        self,
        request: RenderBlockRequest,
        command: list[str],
        render_device_preference: str,
        effective_use_gpu: bool,
        selected_blender_path: str,
        selected_blender_version: str,
        blend_version_detected: str,
    ) -> None:
        start_time = time.monotonic()
        job_log_path = get_job_log_path(self.project_root, request.jobId, request.blockId)
        completion_recorded = False
        attempted_cpu_fallback = False
        gpu_failure_exit_code: int | None = None

        self.logger.info(
            "Starting render job=%s block=%s frames=%s-%s device=%s requestedUseGpu=%s "
            "assignmentId=%s coordinatorId=%s selectedBlenderVersion=%s selectedBlenderPath=%s "
            "blendVersion=%s",
            request.jobId,
            request.blockId,
            request.startFrame,
            request.endFrame,
            render_device_preference,
            request.useGpu,
            request.assignmentId,
            request.coordinatorId,
            selected_blender_version,
            selected_blender_path,
            blend_version_detected,
        )
        received_block_log = (
            f"Received block {request.startFrame}-{request.endFrame} for job {request.jobId}"
        )
        if request.assignmentId:
            received_block_log += f" assignmentId={request.assignmentId}"
        if request.coordinatorId:
            received_block_log += f" coordinatorId={request.coordinatorId}"
        self.status_manager.append_log(received_block_log)
        self.status_manager.append_log(
            "Using local render device preference "
            f"{render_device_preference} (requestedUseGpu={request.useGpu})"
        )
        if selected_blender_path:
            self.status_manager.append_log(
                f"Selected Blender {selected_blender_version or 'unknown'} at {selected_blender_path}"
            )
        if blend_version_detected:
            self.status_manager.append_log(
                f"Blend version detected: {blend_version_detected}"
            )
        self.status_manager.append_log("Starting Blender...")

        try:
            with job_log_path.open("a", encoding="utf-8", buffering=1) as job_log:
                job_log.write(f"Command: {command}\n")
                job_log.write(
                    "Render device preference: "
                    f"{render_device_preference} (effectiveUseGpu={effective_use_gpu}, "
                    f"requestedUseGpu={request.useGpu})\n"
                )
                job_log.write(
                    "Blender selection: "
                    f"blendVersionDetected={blend_version_detected}; "
                    f"selectedBlenderVersion={selected_blender_version}; "
                    f"selectedBlenderPath={selected_blender_path}\n"
                )
                if request.assignmentId or request.coordinatorId or request.submittedByMasterId:
                    job_log.write(
                        "Coordinator metadata: "
                        f"assignmentId={request.assignmentId}; "
                        f"coordinatorId={request.coordinatorId}; "
                        f"submittedByMasterId={request.submittedByMasterId}; "
                        f"submittedByMasterName={request.submittedByMasterName}\n"
                    )
                attempt = self._run_render_attempt(
                    request=request,
                    command=command,
                    job_log=job_log,
                )
                exit_code = attempt.exit_code
                elapsed_seconds = int(time.monotonic() - start_time)
                cancelled = self._cancel_requested
                output_paths = attempt.output_paths
                output_verified = attempt.output_verified

                if not cancelled and exit_code != 0 and render_device_preference == "GPU":
                    attempted_cpu_fallback = True
                    gpu_failure_exit_code = exit_code
                    self.status_manager.update_active_render_metadata(
                        gpuRetryAttempted=True,
                        gpuFailureExitCode=exit_code,
                    )
                    self.status_manager.add_active_render_recovery_action(
                        f"GPU render failed with exitCode={exit_code}; retried automatically on CPU."
                    )
                    self.logger.warning(
                        "GPU render failed job=%s block=%s exitCode=%s; retrying once on CPU",
                        request.jobId,
                        request.blockId,
                        exit_code,
                    )
                    self.status_manager.append_log(
                        f"GPU render failed with exitCode={exit_code}; retrying once on CPU"
                    )
                    job_log.write(
                        f"GPU render failed with exitCode={exit_code}; retrying once on CPU\n"
                    )
                    render_device_preference = "CPU"
                    effective_use_gpu = False
                    self.status_manager.override_active_render_device(
                        "CPU",
                        use_gpu=False,
                        selected_blender_version=selected_blender_version,
                        selected_blender_path=selected_blender_path,
                    )
                    cpu_command = self._build_command(
                        selected_blender_path,
                        request,
                        render_device_preference="CPU",
                    )
                    attempt = self._run_render_attempt(
                        request=request,
                        command=cpu_command,
                        job_log=job_log,
                    )
                    exit_code = attempt.exit_code
                    elapsed_seconds = int(time.monotonic() - start_time)
                    cancelled = self._cancel_requested
                    output_paths = attempt.output_paths
                    output_verified = attempt.output_verified

                if cancelled:
                    self.logger.info(
                        "Render cancelled job=%s block=%s exitCode=%s elapsed=%ss",
                        request.jobId,
                        request.blockId,
                        exit_code,
                        elapsed_seconds,
                    )
                    self.status_manager.finish_render_cancelled(
                        exit_code=exit_code,
                        elapsed_seconds=elapsed_seconds,
                        output_paths=output_paths,
                        output_verified=output_verified,
                        message=f"Render cancelled with exitCode={exit_code}",
                        failure_category="cancelled",
                        failure_code="render_cancelled",
                    )
                elif exit_code == 0 and output_verified:
                    self.status_manager.update_render_progress(
                        request.endFrame,
                        frame_completion=1.0,
                    )
                    message = f"Render finished successfully with exitCode={exit_code}"
                    if attempted_cpu_fallback:
                        message += " after retrying on CPU"
                        if self.config.persistCpuFallback:
                            self._persist_render_device_preference("CPU")
                    self.logger.info(
                        "Render finished job=%s block=%s result=done exitCode=%s elapsed=%ss",
                        request.jobId,
                        request.blockId,
                        exit_code,
                        elapsed_seconds,
                    )
                    self.status_manager.finish_render_success(
                        exit_code=exit_code,
                        elapsed_seconds=elapsed_seconds,
                        output_paths=output_paths,
                        output_verified=output_verified,
                        message=message,
                    )
                elif exit_code == 0:
                    error = (
                        "Blender exited with code 0, but the expected output file was not found "
                        "after waiting for visibility."
                    )
                    if attempted_cpu_fallback:
                        error = (
                            "GPU render failed and the CPU retry exited with code 0, but the "
                            "expected output file was not found after waiting for visibility."
                        )
                    self.logger.error(
                        "Render finished without visible output job=%s block=%s elapsed=%ss paths=%s",
                        request.jobId,
                        request.blockId,
                        elapsed_seconds,
                        output_paths,
                    )
                    self.status_manager.finish_render_error(
                        exit_code=exit_code,
                        elapsed_seconds=elapsed_seconds,
                        error=error,
                        output_paths=output_paths,
                        output_verified=False,
                        message="Render finished but the expected output file was not found",
                        failure_category="output",
                        failure_code="output_not_found",
                        failure_details=error,
                    )
                else:
                    error = f"Blender exited with code {exit_code}"
                    if attempted_cpu_fallback and gpu_failure_exit_code is not None:
                        error = (
                            f"GPU render failed with exit code {gpu_failure_exit_code}; "
                            f"CPU retry exited with code {exit_code}"
                        )
                    self.logger.error(
                        "Render failed job=%s block=%s exitCode=%s elapsed=%ss",
                        request.jobId,
                        request.blockId,
                        exit_code,
                        elapsed_seconds,
                    )
                    self.status_manager.finish_render_error(
                        exit_code=exit_code,
                        elapsed_seconds=elapsed_seconds,
                        error=error,
                        output_paths=output_paths,
                        output_verified=output_verified,
                        message=f"Render failed with exitCode={exit_code}",
                        failure_category="blender_process",
                        failure_code="exit_nonzero",
                        failure_details=error,
                    )

                completion_recorded = True
        except OSError as exc:
            elapsed_seconds = int(time.monotonic() - start_time)
            self.logger.exception("Failed to start or monitor Blender process")
            self.status_manager.finish_render_error(
                exit_code=-1,
                elapsed_seconds=elapsed_seconds,
                error=str(exc),
                output_paths=self._expected_output_paths(request),
                output_verified=None,
                message=f"Render error: {exc}",
                failure_category="worker_runtime",
                failure_code="process_start_failed",
                failure_details=str(exc),
            )
            completion_recorded = True
        except Exception as exc:
            elapsed_seconds = int(time.monotonic() - start_time)
            self.logger.exception("Unexpected error while monitoring Blender process")
            self.status_manager.finish_render_error(
                exit_code=-1,
                elapsed_seconds=elapsed_seconds,
                error=str(exc),
                output_paths=self._expected_output_paths(request),
                output_verified=None,
                message=f"Unexpected render error: {exc}",
                failure_category="worker_runtime",
                failure_code="unexpected_exception",
                failure_details=str(exc),
            )
            completion_recorded = True
        finally:
            if (
                not completion_recorded
                and self.status_manager.has_active_render(
                    job_id=request.jobId,
                    block_id=request.blockId,
                )
            ):
                elapsed_seconds = int(time.monotonic() - start_time)
                if self._cancel_requested:
                    self.status_manager.finish_render_cancelled(
                        exit_code=-1,
                        elapsed_seconds=elapsed_seconds,
                        output_paths=self._expected_output_paths(request),
                        output_verified=None,
                        message="Render cancelled during final cleanup",
                        failure_category="cancelled",
                        failure_code="render_cancelled",
                    )
                else:
                    self.status_manager.finish_render_error(
                        exit_code=-1,
                        elapsed_seconds=elapsed_seconds,
                        error="Render ended without final state cleanup",
                        output_paths=self._expected_output_paths(request),
                        output_verified=None,
                        message="Render ended unexpectedly and state was reset",
                        failure_category="worker_runtime",
                        failure_code="final_state_cleanup_missing",
                        failure_details="Render ended without final state cleanup",
                    )

            with self._process_lock:
                self._process = None
                self._thread = None
                self._cancel_requested = False

    def _extract_discrete_progress_ratio_from_line(self, line: str) -> tuple[float | None, int | None]:
        for regex in (SAMPLE_PROGRESS_REGEX, TILE_PROGRESS_REGEX, PATH_TRACING_TILE_PROGRESS_REGEX):
            match = regex.search(line)
            if not match:
                continue
            total = int(match.group(2))
            if total <= 0:
                continue
            current = int(match.group(1))
            return max(0.0, min(current / total, 1.0)), total
        return None, None

    @staticmethod
    def _parse_remaining_seconds(raw_value: str) -> float | None:
        cleaned = str(raw_value or "").strip()
        if not cleaned:
            return None
        parts = cleaned.split(":")
        try:
            if len(parts) == 3:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
                return max(0.0, hours * 3600 + minutes * 60 + seconds)
            if len(parts) == 2:
                minutes = int(parts[0])
                seconds = float(parts[1])
                return max(0.0, minutes * 60 + seconds)
            if len(parts) == 1:
                return max(0.0, float(parts[0]))
        except ValueError:
            return None
        return None

    def _estimate_progress_ratio_from_remaining(self, line: str) -> float | None:
        match = REMAINING_TIME_REGEX.search(line)
        if not match:
            return None
        remaining_seconds = self._parse_remaining_seconds(match.group(1))
        if remaining_seconds is None:
            return None
        elapsed_seconds = self.status_manager.get_current_job_elapsed_seconds()
        if elapsed_seconds is None:
            return None
        total_estimated_seconds = elapsed_seconds + remaining_seconds
        if total_estimated_seconds <= 0.0:
            return None
        return max(0.0, min(elapsed_seconds / total_estimated_seconds, 1.0))

    def _extract_progress_ratio_from_line(self, line: str) -> float | None:
        discrete_ratio, discrete_total = self._extract_discrete_progress_ratio_from_line(line)
        estimated_ratio = self._estimate_progress_ratio_from_remaining(line)

        if discrete_ratio is None:
            return estimated_ratio
        if estimated_ratio is None:
            return discrete_ratio
        if discrete_total is not None and discrete_total <= 8:
            coarse_ratio = max(discrete_ratio * 0.35, estimated_ratio)
            return max(0.0, min(coarse_ratio, 1.0))
        return max(discrete_ratio, estimated_ratio)

    def _update_progress_from_line(self, line: str) -> None:
        frame_match = FRAME_REGEX.search(line)
        frame = int(frame_match.group(1)) if frame_match else self.status_manager.get_current_frame_number()
        progress_ratio = self._extract_progress_ratio_from_line(line)

        if frame is not None and (frame_match is not None or progress_ratio is not None):
            self.status_manager.update_render_progress(frame, frame_completion=progress_ratio)

        if frame is not None and SAVED_OUTPUT_REGEX.search(line):
            self.status_manager.update_render_progress(frame, frame_completion=1.0)

    def _run_render_attempt(
        self,
        *,
        request: RenderBlockRequest,
        command: list[str],
        job_log,
    ) -> RenderAttemptResult:
        output_paths = self._expected_output_paths(request)
        output_baselines = self._capture_output_fingerprints(output_paths)
        configure_windows_error_mode()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=False,
            bufsize=0,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
        with self._process_lock:
            self._process = process

        if self._cancel_requested and process.poll() is None:
            self._terminate_process_tree(process)

        try:
            if process.stdout is not None:
                with process.stdout:
                    for raw_line in process.stdout:
                        line = decode_subprocess_output(raw_line).rstrip("\r\n")
                        if not line:
                            continue
                        self.status_manager.append_log(line)
                        self._update_progress_from_line(line)
                        self.logger.info("[Blender] %s", line)
                        job_log.write(f"{line}\n")

            exit_code = process.wait()
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None

        cancelled = self._cancel_requested
        output_verified = (
            self._wait_for_output_paths(output_paths, baselines=output_baselines)
            if exit_code == 0 and not cancelled
            else None
        )
        return RenderAttemptResult(
            exit_code=exit_code,
            output_paths=output_paths,
            output_verified=output_verified,
        )

    def _persist_render_device_preference(self, render_device_preference: str) -> None:
        if self.config.renderDevicePreference == render_device_preference:
            return
        saved_preference = render_device_preference
        if self.config_manager is not None:
            saved_preference = self.config_manager.set_render_device_preference(
                render_device_preference
            )
        else:
            self.config.renderDevicePreference = render_device_preference
        self.status_manager.set_render_device_preference(saved_preference)
        self.logger.warning(
            "Worker render device preference automatically changed to %s after a successful fallback",
            saved_preference,
        )
        self.status_manager.append_log(
            f"Worker render device preference automatically changed to {saved_preference}"
        )
        if self.coordinator_discovery_service is not None:
            self.coordinator_discovery_service.request_reregister()

    def _expected_output_paths(self, request: RenderBlockRequest) -> list[str]:
        return [
            str(build_output_path(request.outputPattern, frame_number, request.format))
            for frame_number in range(int(request.startFrame), int(request.endFrame) + 1)
        ]

    def _wait_for_output_paths(
        self,
        output_paths: list[str],
        *,
        baselines: dict[str, tuple[int, int] | None] | None = None,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.25,
    ) -> bool:
        if not output_paths:
            return False

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._outputs_are_fresh(output_paths, baselines):
                return True
            time.sleep(poll_interval_seconds)
        return self._outputs_are_fresh(output_paths, baselines)

    @staticmethod
    def _capture_output_fingerprints(output_paths: list[str]) -> dict[str, tuple[int, int] | None]:
        fingerprints: dict[str, tuple[int, int] | None] = {}
        for raw_path in output_paths:
            path = Path(raw_path)
            try:
                stat = path.stat()
                fingerprints[raw_path] = (int(stat.st_size), int(stat.st_mtime_ns)) if path.is_file() else None
            except OSError:
                fingerprints[raw_path] = None
        return fingerprints

    @staticmethod
    def _outputs_are_fresh(
        output_paths: list[str],
        baselines: dict[str, tuple[int, int] | None] | None,
    ) -> bool:
        for raw_path in output_paths:
            path = Path(raw_path)
            try:
                stat = path.stat()
            except OSError:
                return False
            if not path.is_file() or stat.st_size <= 0:
                return False
            baseline = (baselines or {}).get(raw_path)
            if baseline is not None and baseline == (int(stat.st_size), int(stat.st_mtime_ns)):
                return False
        return True

    def _terminate_process_tree(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return

        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=False,
                    timeout=10,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                process.wait(timeout=10)
                return
            except Exception:
                self.logger.warning("taskkill failed for Blender PID %s, falling back to terminate()", process.pid)

        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.logger.warning("Blender did not terminate gracefully, forcing kill")
            process.kill()
            process.wait(timeout=5)
