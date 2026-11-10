from __future__ import annotations

from textwrap import wrap


PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN_X = 36
TOP_Y = 806
FONT_SIZE = 10
LINE_HEIGHT = 13
WRAP_WIDTH = 104


def build_text_pdf(*, title: str, lines: list[str]) -> bytes:
    normalized_lines = _normalize_lines([title, ""] + list(lines))
    lines_per_page = max(1, int((TOP_Y - MARGIN_X) / LINE_HEIGHT) - 1)
    pages = [
        normalized_lines[index:index + lines_per_page]
        for index in range(0, len(normalized_lines), lines_per_page)
    ] or [["No report data available."]]

    objects: list[bytes] = []
    page_object_numbers: list[int] = []
    next_object_number = 4

    for page_lines in pages:
        page_object_number = next_object_number
        content_object_number = next_object_number + 1
        page_object_numbers.append(page_object_number)
        next_object_number += 2

        content_stream = _build_content_stream(page_lines)
        objects.append(
            (
                f"{page_object_number} 0 obj\n"
                "<< /Type /Page /Parent 2 0 R "
                "/MediaBox [0 0 595 842] "
                "/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_object_number} 0 R >>\n"
                "endobj\n"
            ).encode("latin-1")
        )
        objects.append(
            (
                f"{content_object_number} 0 obj\n"
                f"<< /Length {len(content_stream)} >>\n"
                "stream\n"
            ).encode("latin-1")
            + content_stream
            + b"\nendstream\nendobj\n"
        )

    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)
    catalog = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    pages_object = (
        f"2 0 obj\n<< /Type /Pages /Count {len(page_object_numbers)} /Kids [{kids}] >>\nendobj\n"
    ).encode("latin-1")
    font_object = (
        b"3 0 obj\n"
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>\n"
        b"endobj\n"
    )

    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    object_parts = [catalog, pages_object, font_object, *objects]
    body = header
    object_offsets: list[int] = []
    for part in object_parts:
        object_offsets.append(len(body))
        body += part
    xref_start = len(body)
    total_objects = 3 + len(objects)
    xref_lines = [f"xref\n0 {total_objects + 1}\n".encode("latin-1"), b"0000000000 65535 f \n"]
    for offset in object_offsets:
        xref_lines.append(f"{offset:010d} 00000 n \n".encode("latin-1"))

    trailer = (
        f"trailer\n<< /Size {total_objects + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_start}\n%%EOF"
    ).encode("latin-1")
    return body + b"".join(xref_lines) + trailer


def _normalize_lines(lines: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw_line in lines:
        line = _sanitize_text(raw_line)
        if not line:
            normalized.append("")
            continue
        wrapped = wrap(
            line,
            width=WRAP_WIDTH,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        )
        normalized.extend(wrapped or [""])
    return normalized


def _sanitize_text(value: str) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_content_stream(lines: list[str]) -> bytes:
    commands = [
        "BT",
        f"/F1 {FONT_SIZE} Tf",
        f"{LINE_HEIGHT} TL",
        f"{MARGIN_X} {TOP_Y} Td",
    ]
    for index, line in enumerate(lines):
        escaped = _escape_pdf_text(line)
        if index == 0:
            commands.append(f"({escaped}) Tj")
        else:
            commands.append("T*")
            commands.append(f"({escaped}) Tj")
    commands.append("ET")
    return "\n".join(commands).encode("latin-1")
