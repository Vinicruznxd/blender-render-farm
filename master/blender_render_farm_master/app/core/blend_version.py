from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import gzip
import re
from typing import Any

try:
    import zstandard
except ImportError:  # pragma: no cover - optional during bootstrap
    zstandard = None

from app.core.version_routing import (
    DEFAULT_VERSION_ROUTING_POLICY,
    SELECTION_BUCKET_EXACT,
    SELECTION_BUCKET_HIGHER_MAJOR,
    SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT,
    classify_selected_version_bucket,
    normalize_version_routing_policy,
)


BLEND_HEADER_MAGIC = b"BLENDER"
BLEND_HEADER_READ_SIZE = 32
BLEND_HEADER_VERSION_REGEX = re.compile(rb"^BLENDER(?:\d{2,}-\d{2,})?[_-]?[vV](\d{3,4})")
GZIP_MAGIC = b"\x1f\x8b"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


@dataclass(frozen=True)
class BlendVersionMetadata:
    version_code: int
    major: int
    minor: int
    preferred_major: int | None = None
    version_routing_policy: str = DEFAULT_VERSION_ROUTING_POLICY

    @classmethod
    def from_version_code(
        cls,
        version_code: int,
        *,
        preferred_major: int | None = None,
        version_routing_policy: str = DEFAULT_VERSION_ROUTING_POLICY,
    ) -> "BlendVersionMetadata | None":
        if version_code < 100:
            return None
        return cls(
            version_code=version_code,
            major=version_code // 100,
            minor=version_code % 100,
            preferred_major=preferred_major,
            version_routing_policy=normalize_version_routing_policy(version_routing_policy),
        )

    @classmethod
    def from_version_string(
        cls,
        value: str,
        *,
        preferred_major: int | None = None,
        version_routing_policy: str = DEFAULT_VERSION_ROUTING_POLICY,
    ) -> "BlendVersionMetadata | None":
        parsed = parse_blender_version_string(value)
        if parsed is None:
            return None
        major, minor = parsed
        return cls(
            version_code=(major * 100) + minor,
            major=major,
            minor=minor,
            preferred_major=preferred_major,
            version_routing_policy=normalize_version_routing_policy(version_routing_policy),
        )

    @property
    def required_blender_version(self) -> str:
        if self.minor < 10:
            return f"{self.major}.{self.minor}"
        return f"{self.major}.{self.minor:02d}"

    @property
    def preferred_blender_major(self) -> int:
        return int(self.preferred_major if self.preferred_major is not None else self.major)

    @property
    def policy_text(self) -> str:
        return f"preferir {self.preferred_blender_major}.x >= {self.required_blender_version}"

    def to_payload_fields(self) -> dict[str, Any]:
        return {
            "requiredBlenderVersion": self.required_blender_version,
            "preferredBlenderMajor": self.preferred_blender_major,
            "versionRoutingPolicy": self.version_routing_policy,
        }


def read_blend_version_metadata(path: Path) -> BlendVersionMetadata | None:
    header = _read_blend_header(path)
    if header is None or not header.startswith(BLEND_HEADER_MAGIC):
        return None
    match = BLEND_HEADER_VERSION_REGEX.match(header)
    if not match:
        return None
    try:
        version_code = int(match.group(1).decode("ascii"))
    except ValueError:
        return None
    return BlendVersionMetadata.from_version_code(version_code)


def blend_version_metadata_from_job(payload: dict[str, Any] | None) -> BlendVersionMetadata | None:
    if not isinstance(payload, dict):
        return None

    required_blender_version = payload.get("requiredBlenderVersion")
    if not required_blender_version:
        return None

    preferred_major = payload.get("preferredBlenderMajor")
    try:
        preferred_major_int = int(preferred_major) if preferred_major is not None else None
    except (TypeError, ValueError):
        preferred_major_int = None

    routing_policy = normalize_version_routing_policy(
        str(payload.get("versionRoutingPolicy") or DEFAULT_VERSION_ROUTING_POLICY)
    )
    return BlendVersionMetadata.from_version_string(
        str(required_blender_version),
        preferred_major=preferred_major_int,
        version_routing_policy=routing_policy,
    )


def parse_blender_version_string(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"(\d+)(?:\.(\d+))?", value.strip())
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major, minor


def compare_blender_versions(left: tuple[int, int], right: tuple[int, int]) -> int:
    if left == right:
        return 0
    if left[0] != right[0]:
        return 1 if left[0] > right[0] else -1
    if left[1] != right[1]:
        return 1 if left[1] > right[1] else -1
    return 0


def evaluate_worker_blender_version(
    worker_version: str | None,
    blend_metadata: BlendVersionMetadata | None,
) -> tuple[bool | None, bool | None]:
    if not worker_version or blend_metadata is None:
        return None, None

    parsed_worker_version = parse_blender_version_string(worker_version)
    if parsed_worker_version is None:
        return None, None

    bucket = classify_selected_version_bucket(
        blend_metadata.required_blender_version,
        f"{parsed_worker_version[0]}.{parsed_worker_version[1]}",
        preferred_major=blend_metadata.preferred_blender_major,
    )
    is_compatible = bucket in {
        SELECTION_BUCKET_EXACT,
        SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT,
        SELECTION_BUCKET_HIGHER_MAJOR,
    }
    is_preferred = bool(is_compatible and parsed_worker_version[0] == blend_metadata.preferred_blender_major)
    return is_compatible, is_preferred


def _read_blend_header(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(BLEND_HEADER_READ_SIZE)
    except OSError:
        return None

    if header.startswith(BLEND_HEADER_MAGIC):
        return header
    if header[:2] == GZIP_MAGIC:
        try:
            with gzip.open(path, "rb") as handle:
                return handle.read(BLEND_HEADER_READ_SIZE)
        except OSError:
            return None
    if header[:4] == ZSTD_MAGIC:
        if zstandard is None:
            return None
        try:
            with path.open("rb") as handle:
                with zstandard.ZstdDecompressor().stream_reader(handle) as reader:
                    return reader.read(BLEND_HEADER_READ_SIZE)
        except OSError:
            return None
        except zstandard.ZstdError:
            return None
    return header
