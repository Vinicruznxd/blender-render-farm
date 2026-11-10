from __future__ import annotations

import locale


def decode_subprocess_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value

    encodings: list[str] = ["utf-8-sig", "utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred:
        encodings.append(preferred)
    encodings.extend(["cp850", "cp1252", "latin-1"])

    tried: set[str] = set()
    for encoding in encodings:
        normalized = (encoding or "").strip().lower()
        if not normalized or normalized in tried:
            continue
        tried.add(normalized)
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue

    return value.decode("utf-8", errors="replace")
