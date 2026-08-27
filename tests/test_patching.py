from __future__ import annotations

import pytest

from traceforge.patching import PatchError, apply_file_patch, parse_unified_diff


def test_apply_unified_diff_update() -> None:
    patch = """--- a/example.py
+++ b/example.py
@@ -1,2 +1,2 @@
 value = 1
-print(value)
+print(value + 1)
"""
    [file_patch] = parse_unified_diff(patch)
    assert apply_file_patch("value = 1\nprint(value)\n", file_patch) == (
        "value = 1\nprint(value + 1)\n"
    )


def test_apply_unified_diff_new_file() -> None:
    patch = """--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
+world
"""
    [file_patch] = parse_unified_diff(patch)
    assert file_patch.old_path is None
    assert apply_file_patch("", file_patch) == "hello\nworld\n"


def test_apply_patch_is_strict_about_context() -> None:
    patch = """--- a/example.txt
+++ b/example.txt
@@ -1 +1 @@
-expected
+replacement
"""
    [file_patch] = parse_unified_diff(patch)
    with pytest.raises(PatchError, match="context mismatch"):
        apply_file_patch("actual\n", file_patch)

