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


def test_repairs_incorrect_unified_diff_hunk_counts() -> None:
    patch = """--- a/example.py
+++ b/example.py
@@ -1,99 +1,42 @@
 value = 1
-print(value)
+print(value + 1)
"""
    [file_patch] = parse_unified_diff(patch)

    assert file_patch.hunks[0].old_count == 2
    assert file_patch.hunks[0].new_count == 2
    assert apply_file_patch("value = 1\nprint(value)\n", file_patch) == (
        "value = 1\nprint(value + 1)\n"
    )


def test_accepts_begin_update_file_patch_with_context_search() -> None:
    patch = """*** Begin Patch
*** Update File: example.py
@@
 def calculate(value):
-    return value
+    return value + 1
*** End Patch
"""
    [file_patch] = parse_unified_diff(patch)

    assert apply_file_patch(
        "heading = 'kept'\n\ndef calculate(value):\n    return value\n", file_patch
    ) == "heading = 'kept'\n\ndef calculate(value):\n    return value + 1\n"


def test_rejects_ambiguous_context_in_begin_update_file_patch() -> None:
    patch = """*** Begin Patch
*** Update File: example.txt
@@
-same
+changed
*** End Patch
"""
    [file_patch] = parse_unified_diff(patch)

    with pytest.raises(PatchError, match="ambiguous"):
        apply_file_patch("same\nmiddle\nsame\n", file_patch)


def test_accepts_begin_add_file_patch_without_hunk_header() -> None:
    patch = """*** Begin Patch
*** Add File: new.txt
+hello
+world
*** End Patch
"""
    [file_patch] = parse_unified_diff(patch)

    assert file_patch.old_path is None
    assert apply_file_patch("", file_patch) == "hello\nworld\n"
