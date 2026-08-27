from __future__ import annotations

import struct
import subprocess
import sys
import zipfile
from pathlib import Path


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def _minimal_mp4(duration_seconds: int = 54) -> bytes:
    mvhd = bytes(12) + struct.pack(">II", 1_000, duration_seconds * 1_000)
    return _box(b"ftyp", b"isom") + _box(b"moov", _box(b"mvhd", mvhd))


def test_submission_script_packages_exactly_two_files(tmp_path: Path) -> None:
    readme = tmp_path / "README.txt"
    video = tmp_path / "demo.mp4"
    readme.write_text("项目说明", encoding="utf-8")
    video.write_bytes(_minimal_mp4())

    result = subprocess.run(
        [
            sys.executable,
            "scripts/package_submission.py",
            "--name",
            "测试姓名",
            "--readme",
            str(readme),
            "--video",
            str(video),
            "--output-dir",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    archive_path = tmp_path / "测试姓名.zip"
    assert "Created" in result.stdout
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["README.txt", "TraceForge-demo.mp4"]
        assert archive.testzip() is None


def test_submission_script_rejects_oversized_readme(tmp_path: Path) -> None:
    readme = tmp_path / "README.txt"
    video = tmp_path / "demo.mp4"
    readme.write_text("字" * 1_001, encoding="utf-8")
    video.write_bytes(_minimal_mp4())

    result = subprocess.run(
        [
            sys.executable,
            "scripts/package_submission.py",
            "--check-only",
            "--readme",
            str(readme),
            "--video",
            str(video),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "1001 characters" in result.stderr


def test_submission_script_rejects_overlong_video(tmp_path: Path) -> None:
    readme = tmp_path / "README.txt"
    video = tmp_path / "demo.mp4"
    readme.write_text("项目说明", encoding="utf-8")
    video.write_bytes(_minimal_mp4(duration_seconds=121))

    result = subprocess.run(
        [
            sys.executable,
            "scripts/package_submission.py",
            "--check-only",
            "--readme",
            str(readme),
            "--video",
            str(video),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "121.00s" in result.stderr


def test_submission_script_rejects_path_like_name(tmp_path: Path) -> None:
    readme = tmp_path / "README.txt"
    video = tmp_path / "demo.mp4"
    readme.write_text("项目说明", encoding="utf-8")
    video.write_bytes(_minimal_mp4())

    result = subprocess.run(
        [
            sys.executable,
            "scripts/package_submission.py",
            "--name",
            "../测试姓名",
            "--readme",
            str(readme),
            "--video",
            str(video),
            "--output-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "plain filename" in result.stderr
    assert not (tmp_path.parent / "测试姓名.zip").exists()
