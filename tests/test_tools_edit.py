"""edit_test.go 的 Python 移植（标准库 unittest，零第三方依赖）。

覆盖 Go 版测过的边界：四级模糊匹配降级、多行替换重新缩进、歧义拒绝、
replace_all、删除、not found，以及 resolve_path 的沙箱越界拦截。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tiny_claw.tools.edit import EditError, MatchLevel, apply_edit
from tiny_claw.tools.fs import EscapeError, resolve_path


class TestApplyEditExact(unittest.TestCase):
    def test_exact(self):
        src = "line1\nline2\nline3\n"
        got, lvl, n = apply_edit(src, "line2", "LINE2", False, 0.82)
        self.assertEqual(got, "line1\nLINE2\nline3\n")
        self.assertEqual(lvl, MatchLevel.EXACT)
        self.assertEqual(n, 1)


class TestApplyEditToleratesIndent(unittest.TestCase):
    def test_trim(self):
        # 模型复制时丢了缩进——这是最高频的一类替换失败。
        src = "func a() {\n    if ok {\n        run()\n    }\n}\n"
        got, lvl, _ = apply_edit(src, "if ok {", "if !ok {", False, 0.82)
        self.assertEqual(got, "func a() {\n    if !ok {\n        run()\n    }\n}\n")
        self.assertEqual(lvl, MatchLevel.TRIM)


class TestApplyEditToleratesWhitespace(unittest.TestCase):
    def test_normalize(self):
        src = "a :=  f(1,   2)\n"
        got, lvl, _ = apply_edit(src, "a := f(1, 2)", "a := g(1, 2)", False, 0.82)
        self.assertEqual(got, "a := g(1, 2)\n")
        self.assertEqual(lvl, MatchLevel.NORMALIZE)


class TestApplyEditReindentsMultiLine(unittest.TestCase):
    def test_reindent(self):
        # 多行替换时，新内容必须整体对齐到原文缩进，且保留自己的相对层级。
        # 这是"逻辑对了但缩进错了"这类难查 bug 的护栏。
        src = "func a() {\n    if ok {\n        run()\n    }\n}\n"
        got, _, _ = apply_edit(src, "if ok {\n    run()\n}", "if !ok {\n    stop()\n}", False, 0.82)
        self.assertEqual(got, "func a() {\n    if !ok {\n        stop()\n    }\n}\n")


class TestApplyEditRejectsAmbiguous(unittest.TestCase):
    def test_ambiguous(self):
        with self.assertRaises(EditError) as ctx:
            apply_edit("x\nx\n", "x", "y", False, 0.82)
        self.assertIn("matched 2 locations", str(ctx.exception))


class TestApplyEditReplaceAll(unittest.TestCase):
    def test_replace_all(self):
        got, _, n = apply_edit("x\nx\nx\n", "x", "y", True, 0.82)
        self.assertEqual(got, "y\ny\ny\n")
        self.assertEqual(n, 3)


class TestApplyEditDeletion(unittest.TestCase):
    def test_deletion(self):
        got, _, _ = apply_edit("keep\nDROP\nkeep\n", "DROP\n", "", False, 0.82)
        self.assertEqual(got, "keep\nkeep\n")


class TestApplyEditNotFound(unittest.TestCase):
    def test_not_found(self):
        with self.assertRaises(EditError):
            apply_edit("abc\n", "zzz", "yyy", False, 0.82)


class TestSimilarity(unittest.TestCase):
    def test_similarity(self):
        self.assertEqual(_sim("abc", "abc"), 1)
        s = _sim("abc", "abd")
        self.assertTrue(0.6 < s < 0.7, f"abc/abd expected ~0.667, got {s}")
        self.assertEqual(_sim("abc", "xyz"), 0)


class TestResolvePathBlocksEscape(unittest.TestCase):
    def test_blocks_escape(self):
        root = tempfile.mkdtemp()
        with self.assertRaises(EscapeError):
            resolve_path(root, "../outside")
        # 工作区内的合法路径不应被拦截。
        self.assertTrue(resolve_path(root, os.path.join(root, "inner.go")))


def _sim(a: str, b: str) -> float:
    # 与 edit.py 内部 _similarity 同义，避免暴露私有符号。
    from tiny_claw.tools.edit import _similarity

    return _similarity(a, b)


if __name__ == "__main__":
    unittest.main()
