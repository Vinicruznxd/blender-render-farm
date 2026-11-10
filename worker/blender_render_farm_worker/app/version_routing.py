from __future__ import annotations


DEFAULT_VERSION_ROUTING_POLICY = "prefer_same_major_min_version"
PREFER_SAME_MAJOR_ANY_VERSION_POLICY = "prefer_same_major_any_version"
PREFER_HIGHER_MAJOR_WHEN_SAME_MAJOR_OLDER_POLICY = "prefer_higher_major_when_same_major_older"
LEGACY_POLICY_ALIASES = {
    "prefer_same_major_then_higher": DEFAULT_VERSION_ROUTING_POLICY,
}
SUPPORTED_VERSION_ROUTING_POLICIES = (
    DEFAULT_VERSION_ROUTING_POLICY,
    PREFER_SAME_MAJOR_ANY_VERSION_POLICY,
    PREFER_HIGHER_MAJOR_WHEN_SAME_MAJOR_OLDER_POLICY,
)

SELECTION_BUCKET_EXACT = "same_major_exact"
SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT = "same_major_sufficient"
SELECTION_BUCKET_SAME_MAJOR_OLDER = "same_major_older"
SELECTION_BUCKET_HIGHER_MAJOR = "higher_major"
SELECTION_BUCKET_LOWER_MAJOR = "lower_major"
SELECTION_BUCKET_UNKNOWN = "unknown"
SAME_MAJOR_SELECTION_BUCKETS = (
    SELECTION_BUCKET_EXACT,
    SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT,
    SELECTION_BUCKET_SAME_MAJOR_OLDER,
)


def normalize_version_routing_policy(value: str | None) -> str:
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return DEFAULT_VERSION_ROUTING_POLICY
    if cleaned in LEGACY_POLICY_ALIASES:
        return LEGACY_POLICY_ALIASES[cleaned]
    if cleaned in SUPPORTED_VERSION_ROUTING_POLICIES:
        return cleaned
    return DEFAULT_VERSION_ROUTING_POLICY


def validate_version_routing_policy(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    normalized = normalize_version_routing_policy(cleaned)
    lowered = cleaned.lower()
    if lowered not in SUPPORTED_VERSION_ROUTING_POLICIES and lowered not in LEGACY_POLICY_ALIASES:
        raise ValueError(
            "versionRoutingPolicy must be one of: "
            + ", ".join(SUPPORTED_VERSION_ROUTING_POLICIES)
        )
    return normalized


def parse_version_tuple(value: str | None) -> tuple[int, int] | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    parts = cleaned.split(".")
    if not parts or not parts[0].isdigit():
        return None
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return (major, minor)


def classify_selected_version_bucket(
    required_version: str | None,
    selected_version: str | None,
    *,
    preferred_major: int | None = None,
) -> str:
    required_tuple = parse_version_tuple(required_version)
    selected_tuple = parse_version_tuple(selected_version)
    if not required_tuple or not selected_tuple:
        return SELECTION_BUCKET_UNKNOWN

    required_major = required_tuple[0]
    target_major = preferred_major or required_major
    if selected_tuple == required_tuple:
        return SELECTION_BUCKET_EXACT
    if selected_tuple[0] == target_major:
        if selected_tuple >= required_tuple:
            return SELECTION_BUCKET_SAME_MAJOR_SUFFICIENT
        return SELECTION_BUCKET_SAME_MAJOR_OLDER
    if selected_tuple[0] > required_major:
        return SELECTION_BUCKET_HIGHER_MAJOR
    return SELECTION_BUCKET_LOWER_MAJOR
