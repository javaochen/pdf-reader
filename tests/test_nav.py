# -*- coding: utf-8 -*-
"""书签相对导航（_nav_bookmark）回归测试

运行：python tests/test_nav.py
"""
import sys
import os
# 包根目录（reader.py 所在目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 本测试不读写 PDF，但仍固定目录，避免导入时采用用户真实路径配置
os.environ.setdefault("PDF_READER_PDF_ROOT_DIR", os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PDF_READER_STATE_DIR", os.path.dirname(os.path.abspath(__file__)))
import reader  # noqa: E402


class FakeViewer:
    def __init__(self, pages, cur):
        self._pages, self._cur = pages, cur

    def load_bookmarks(self):
        return [(1, f"b{p}", p, None) for p in self._pages]

    def current_page_1_based(self):
        return self._cur


class Recorder:
    calls = []

    @staticmethod
    def jump(p):
        Recorder.calls.append(p)


mw = reader.MainWindow.__new__(reader.MainWindow)
mw.viewer = FakeViewer([3, 8, 12], 5)
mw.jump_to_page = Recorder.jump
mw.status = type("S", (), {"showMessage": staticmethod(lambda *a, **k: None)})()

cases = [
    (5, -1, 3),   # 之间 → 前一书签 3
    (5, 1, 8),    # 之间 → 后一书签 8
    (3, -1, None),  # 恰好在首书签 → 无前
    (3, 1, 8),    # 恰好在首书签 → 后 8
    (8, -1, 3), (8, 1, 12),
    (12, -1, 8), (12, 1, None),  # 末书签 → 无后
    (1, -1, None), (1, 1, 3),    # 最前页之前 → 无前；后 3
    (20, -1, 12), (20, 1, None), # 书签之后 → 前 12；无后
]
for cur, d, exp in cases:
    mw.viewer._cur = cur
    Recorder.calls.clear()
    mw._nav_bookmark(d)
    got = Recorder.calls[0] if Recorder.calls else None
    print(f"cur={cur:2d} dir={d:+d} → {got}  (期望 {exp})  {'OK' if got == exp else 'FAIL'}")
