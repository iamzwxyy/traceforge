from __future__ import annotations

import re
import subprocess
from pathlib import Path

PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "provider token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "credential assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\b\s*[:=]\s*"
        r"[\"']([^\"']{12,})[\"']"
    ),
}
SAFE_MARKERS = ("example", "placeholder", "redacted", "test-")


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(content):
                value = match.group(1) if match.lastindex else match.group(0)
                if any(marker in value.lower() for marker in SAFE_MARKERS):
                    continue
                line = content.count("\n", 0, match.start()) + 1
                findings.append(f"{path}:{line}: possible {label}")
    if findings:
        print("\n".join(findings))
        return 1
    print("No credential-like values found in tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
