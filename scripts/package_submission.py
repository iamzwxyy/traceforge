#!/usr/bin/env python3
"""Validate and package the two-file TraceForge submission."""

from __future__ import annotations

import argparse
import os
import struct
import tempfile
import zipfile
from pathlib import Path

README_LIMIT = 1_000
VIDEO_SIZE_LIMIT = 200 * 1024 * 1024
VIDEO_DURATION_LIMIT = 120.0
EXPECTED_ARCHIVE_NAMES = ("README.txt", "TraceForge-demo.mp4")


class SubmissionError(ValueError):
    """Raised when a submission artifact violates the published contract."""


def validate_readme(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SubmissionError(f"README.txt must be readable UTF-8 text: {exc}") from exc
    if not text.strip():
        raise SubmissionError("README.txt must not be empty")
    if len(text) > README_LIMIT:
        raise SubmissionError(
            f"README.txt has {len(text)} characters; limit is {README_LIMIT}"
        )
    return len(text)


def validate_video(path: Path) -> tuple[int, float]:
    try:
        size = path.stat().st_size
        data = path.read_bytes()
    except OSError as exc:
        raise SubmissionError(f"MP4 must be readable: {exc}") from exc
    if path.suffix.lower() != ".mp4":
        raise SubmissionError("Video must use the .mp4 extension")
    if size == 0 or size > VIDEO_SIZE_LIMIT:
        raise SubmissionError(
            f"MP4 size is {size} bytes; limit is {VIDEO_SIZE_LIMIT} bytes"
        )
    duration = _mp4_duration(data)
    if duration <= 0 or duration > VIDEO_DURATION_LIMIT:
        raise SubmissionError(
            f"MP4 duration is {duration:.2f}s; limit is {VIDEO_DURATION_LIMIT:.0f}s"
        )
    return size, duration


def package_submission(
    *, name: str, readme: Path, video: Path, output_dir: Path, force: bool = False
) -> Path:
    safe_name = _validated_name(name)
    validate_readme(readme)
    validate_video(video)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{safe_name}.zip"
    if target.exists() and not force:
        raise SubmissionError(f"Refusing to replace existing archive: {target}")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{safe_name}.", suffix=".zip", dir=output_dir, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        with zipfile.ZipFile(
            temporary_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            _write_deterministic(archive, EXPECTED_ARCHIVE_NAMES[0], readme.read_bytes())
            _write_deterministic(archive, EXPECTED_ARCHIVE_NAMES[1], video.read_bytes())
        _audit_archive(temporary_path)
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target


def _validated_name(raw: str) -> str:
    name = raw.strip()
    if name.lower().endswith(".zip"):
        name = name[:-4].strip()
    if not name or name in {".", ".."}:
        raise SubmissionError("A real-name archive filename is required")
    if len(name) > 80 or any(character in name for character in ("/", "\\", "\0")):
        raise SubmissionError("Archive name must be a plain filename of at most 80 characters")
    return name


def _write_deterministic(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = 0o100644 << 16
    archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _audit_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = tuple(archive.namelist())
        if names != EXPECTED_ARCHIVE_NAMES:
            raise SubmissionError(f"Archive contains unexpected entries: {names}")
        if archive.testzip() is not None:
            raise SubmissionError("Archive CRC validation failed")


def _mp4_duration(data: bytes) -> float:
    moov_payload = _box_payload(data, b"moov")
    if moov_payload is None:
        raise SubmissionError("MP4 does not contain a moov box")
    mvhd_payload = _box_payload(moov_payload, b"mvhd")
    if mvhd_payload is None or len(mvhd_payload) < 20:
        raise SubmissionError("MP4 does not contain a valid mvhd box")

    version = mvhd_payload[0]
    if version == 0:
        timescale, duration = struct.unpack_from(">II", mvhd_payload, 12)
    elif version == 1 and len(mvhd_payload) >= 32:
        timescale = struct.unpack_from(">I", mvhd_payload, 20)[0]
        duration = struct.unpack_from(">Q", mvhd_payload, 24)[0]
    else:
        raise SubmissionError(f"Unsupported mvhd version: {version}")
    if timescale == 0:
        raise SubmissionError("MP4 timescale must be non-zero")
    return duration / timescale


def _box_payload(data: bytes, wanted: bytes) -> bytes | None:
    cursor = 0
    while cursor + 8 <= len(data):
        size = struct.unpack_from(">I", data, cursor)[0]
        box_type = data[cursor + 4 : cursor + 8]
        header_size = 8
        if size == 1:
            if cursor + 16 > len(data):
                break
            size = struct.unpack_from(">Q", data, cursor + 8)[0]
            header_size = 16
        elif size == 0:
            size = len(data) - cursor
        if size < header_size or cursor + size > len(data):
            break
        if box_type == wanted:
            return data[cursor + header_size : cursor + size]
        cursor += size
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", help="real name used as the ZIP filename")
    parser.add_argument(
        "--readme", type=Path, default=Path("artifacts/submission/README.txt")
    )
    parser.add_argument(
        "--video", type=Path, default=Path("artifacts/submission/TraceForge-demo.mp4")
    )
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/submission"))
    parser.add_argument("--force", action="store_true", help="replace an existing archive")
    parser.add_argument(
        "--check-only", action="store_true", help="validate inputs without creating a ZIP"
    )
    args = parser.parse_args()

    readme_characters = validate_readme(args.readme)
    video_bytes, video_seconds = validate_video(args.video)
    print(
        f"Validated README.txt: {readme_characters} characters; "
        f"MP4: {video_seconds:.2f}s, {video_bytes} bytes"
    )
    if args.check_only:
        return 0
    if not args.name:
        parser.error("--name is required unless --check-only is used")
    archive = package_submission(
        name=args.name,
        readme=args.readme,
        video=args.video,
        output_dir=args.output_dir,
        force=args.force,
    )
    print(f"Created {archive} with exactly: {', '.join(EXPECTED_ARCHIVE_NAMES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
