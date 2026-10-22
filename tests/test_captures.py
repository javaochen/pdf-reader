# -*- coding: utf-8 -*-
"""截取暂存（右侧栏「暂存」页）回归测试。

覆盖：落盘与缩略图、索引持久化、文件缺失容错、按条数/字节清理、面板交互
（点击复制、批量清理）、主窗口默认隐藏 + 快捷键开关、截取自动入暂存。

运行：python tests/test_captures.py
"""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

TMP = os.path.join(PKG, "_test_tmp_captures")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
CAPS = os.path.join(TMP, "captures")
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
os.environ["PDF_READER_NOTES_FILE"] = os.path.join(TMP, "notes.md")
os.environ["PDF_READER_CAPTURES_DIR"] = CAPS

import fitz  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import reader as R  # noqa: E402

QMessageBox.critical = staticmethod(lambda *a, **k: None)
app = QApplication.instance() or QApplication([])
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def make_pix(w=300, h=200, color="#3366cc"):
    pm = QPixmap(w, h)
    pm.fill(QColor(color))
    return pm


print("=" * 78)
print("1. 暂存库：落盘、缩略图、索引")
print("=" * 78)
store = R.CaptureStore(CAPS, max_items=10)
check("1.1 暂存目录自动创建", os.path.isdir(CAPS), CAPS)

e1 = store.add(make_pix(), "书A.pdf", 12, 100, 300)
check("1.2 新增返回条目且文件/缩略图都落盘",
      e1 is not None
      and (store.dir / e1["file"]).is_file()
      and (store.dir / e1["thumb"]).is_file(),
      f"file={e1 and e1['file']}")
check("1.3 条目记录了出处与尺寸",
      e1["book"] == "书A.pdf" and e1["page"] == 12 and e1["w"] == 300 and e1["h"] == 200,
      repr({k: e1[k] for k in ("book", "page", "w", "h")}))
check("1.4 索引文件已写出", os.path.isfile(os.path.join(CAPS, "index.json")))
check("1.5 缩略图比原图小", os.path.getsize(store.dir / e1["thumb"])
      < os.path.getsize(store.dir / e1["file"]))

store2 = R.CaptureStore(CAPS, max_items=10)
check("1.6 重新打开能读回索引", len(store2.entries) == 1
      and store2.entries[0]["file"] == e1["file"], f"{len(store2.entries)} 条")

# 文件被用户手工删掉 → 载入时跳过，不崩
e2 = store2.add(make_pix(), "书B.pdf", 3)
(store2.dir / e2["file"]).unlink()
store3 = R.CaptureStore(CAPS, max_items=10)
check("1.7 暂存文件被手工删除后载入跳过坏项",
      len(store3.entries) == 1 and store3.entries[0]["file"] == e1["file"],
      f"{len(store3.entries)} 条")

print()
print("=" * 78)
print("2. 批量清理与自动上限")
print("=" * 78)
store3.add(make_pix(), "书C.pdf", 7)
n = store3.remove([store3.entries[0]["file"]])
check("2.1 清理指定项", n == 1 and len(store3.entries) == 1, f"删除 {n} 条，剩 {len(store3.entries)}")

n = store3.clear()
check("2.2 全部清空", n == 1 and not store3.entries
      and not [f for f in os.listdir(CAPS) if f.endswith(".png")],
      f"删除 {n} 条，残留 {[f for f in os.listdir(CAPS) if f.endswith('.png')]}")

small = R.CaptureStore(os.path.join(TMP, "cap_small"), max_items=3)
for i in range(6):
    small.add(make_pix(), f"书{i}.pdf", i + 1)
files = sorted(f for f in os.listdir(small.dir) if f.endswith(".png"))
check("2.3 超出条数上限时自动删最旧的（只留 3 条）",
      len(small.entries) == 3 and len(files) == 6,
      f"条目 {len(small.entries)}，磁盘文件 {len(files)}（3 张原图 + 3 张缩略图）")
check("2.4 留下的是最新的三条", [e["page"] for e in small.entries] == [4, 5, 6],
      f"{[e['page'] for e in small.entries]}")
check("2.5 统计字节数可用", small.total_bytes() > 0, f"{small.total_bytes()} 字节")

print()
print("=" * 78)
print("3. 面板交互：点击复制、批量清理、出处显示")
print("=" * 78)
copied = []
jumped = []
panel_store = R.CaptureStore(os.path.join(TMP, "cap_panel"), max_items=10)
e_a = panel_store.add(make_pix(color="#aa3333"), "书A.pdf", 12)
e_b = panel_store.add(make_pix(color="#33aa33"), "书B.pdf", 34)
panel = R.CapturePanel(panel_store, on_copy=lambda e: copied.append(e),
                       on_jump=lambda e: jumped.append(e))
check("3.1 面板条目按「新的在上」排列", panel.list.count() == 2
      and panel.list.item(0).data(Qt.UserRole)["file"] == e_b["file"],
      f"{panel.list.count()} 项")
check("3.2 条目文本带书名与页码",
      "书B.pdf" in panel.list.item(0).text() and "34" in panel.list.item(0).text(),
      panel.list.item(0).text())

panel._on_item_clicked(panel.list.item(1))
check("3.3 点一下即触发复制（不要先选中）",
      len(copied) == 1 and copied[0]["file"] == e_a["file"], str(copied[:1]))
panel._on_item_double(panel.list.item(0))
check("3.4 双击触发跳转回调", len(jumped) == 1 and jumped[0]["page"] == 34)

panel.list.item(0).setSelected(True)
panel.list.item(1).setSelected(True)
panel._remove_selected()
check("3.5 清理选中支持多选", len(panel_store.entries) == 0, f"剩 {len(panel_store.entries)}")
check("3.6 清空后提示文字归零", "0 张" in panel.info.text(), panel.info.text())

print()
print("=" * 78)
print("4. 主窗口：默认隐藏、快捷键开关、截取自动入暂存")
print("=" * 78)
pdf = os.path.join(TMP, "书.pdf")
doc = fitz.open()
for i in range(3):
    doc.new_page(width=400, height=600).insert_text((40, 80), f"p{i+1}")
doc.save(pdf)
doc.close()

win = R.MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.show()
app.processEvents()
check("4.1 右侧栏默认隐藏（不占地方）", not win.side_panel.isVisible())
check("4.2 暂存目录落在程序子文件夹（默认 captures/）",
      str(win.capture_store.dir).endswith("captures"), str(win.capture_store.dir))

win.open_pdf(pdf, 1)
app.processEvents()
QTest.qWait(120)

win.toggle_captures_panel()
app.processEvents()
check("4.3 打开暂存页会切到第 2 个页签",
      win.side_panel.isVisible() and win.side_panel.currentIndex() == 1,
      f"可见={win.side_panel.isVisible()} 页签={win.side_panel.currentIndex()}")

before = len(win.capture_store.entries)
win.viewer.set_crop_marks((0, 100), (0, 300))
win.viewer.copy_crop_to_clipboard()
app.processEvents()
check("4.4 截取后自动暂存一条", len(win.capture_store.entries) == before + 1,
      f"{before} → {len(win.capture_store.entries)}")
if win.capture_store.entries:
    last = win.capture_store.entries[-1]
    check("4.5 暂存记录里带着书名与页码",
          last["book"] == "书.pdf" and last["page"] == 1,
          f"{last['book']} 第 {last['page']} 页")
    check("4.6 暂存面板同步刷新", win.capture_panel.list.count() >= 1,
          f"{win.capture_panel.list.count()} 项")
    check("4.7 截取仍然进了剪贴板（暂存不影响原有功能）",
          not QApplication.clipboard().pixmap().isNull())

win.toggle_captures_panel()
app.processEvents()
check("4.8 再按一次收起右侧栏", not win.side_panel.isVisible())

win.close()
app.processEvents()

print()
print("=" * 78)
print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
print("=" * 78)
for name in FAIL:
    print("  FAILED:", name)

for _ in range(5):
    shutil.rmtree(TMP, ignore_errors=True)
    if not os.path.exists(TMP):
        break
    QTest.qWait(100)
sys.exit(1 if FAIL else 0)
