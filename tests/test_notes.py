# -*- coding: utf-8 -*-
"""右侧速记栏（NotesPanel）回归测试：自动落盘、防抖、位置标记、失败不静默。

不依赖显示器（Qt offscreen），不碰用户真实速记文件——PDF/状态/速记全部指向包内临时目录。

运行：python tests/test_notes.py
"""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

TMP = os.path.join(PKG, "_test_tmp_notes")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
# 固定目录，避免导入时采用用户真实路径配置
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
os.environ["PDF_READER_NOTES_FILE"] = os.path.join(TMP, "notes.md")

import fitz  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import reader as R  # noqa: E402

QMessageBox.critical = staticmethod(lambda *a, **k: None)
app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


NOTES = os.path.join(TMP, "notes.md")

print("=" * 78)
print("1. 路径解析与配置")
print("=" * 78)
check("1.1 相对路径按程序目录解析", R.resolve_notes_path("notes.md") == R.APP_DIR / "notes.md",
      str(R.APP_DIR / "notes.md"))
check("1.2 绝对路径原样使用", str(R.resolve_notes_path("D:/x/y.md")).replace("\\", "/") == "D:/x/y.md",
      str(R.resolve_notes_path("D:/x/y.md")))
check("1.3 默认速记文件是 .md 且跟着程序目录走",
      R.resolve_notes_path(_default := R._cfg("notes_file", "notes.md")).suffix == ".md",
      f"默认 {_default}")
check("1.4 环境变量可改路径（本测试即指向临时目录）",
      os.path.normcase(str(R.NOTES_PATH)) == os.path.normcase(os.path.abspath(NOTES)),
      str(R.NOTES_PATH))

print()
print("=" * 78)
print("2. 落盘与防抖")
print("=" * 78)
panel = R.NotesPanel(NOTES, context_provider=lambda: ("拓扑学.pdf", 42))
check("2.1 新文件时编辑器为空且未标脏",
      panel.editor.toPlainText() == "" and not panel.is_dirty, panel.status.text())

panel.editor.setPlainText("第一条：链复形的定义\n")
check("2.2 输入后立即标脏", panel.is_dirty, panel.status.text())
check("2.3 防抖未到期时还没写盘", not os.path.exists(NOTES),
      f"防抖 {R.NOTES_SAVE_DEBOUNCE_MS} ms 内不应有文件")

QTest.qWait(R.NOTES_SAVE_DEBOUNCE_MS + 400)
app.processEvents()
check("2.4 防抖到期后自动落盘且内容一致",
      os.path.exists(NOTES) and read(NOTES) == "第一条：链复形的定义\n",
      f"文件存在={os.path.exists(NOTES)}")
check("2.5 落盘后脏标记清除", not panel.is_dirty, panel.status.text())

panel.editor.setPlainText("第二条：同调群")
check("2.6 二次输入再次标脏", panel.is_dirty)
check("2.7 flush() 立刻落盘（关窗兜底，不等防抖）",
      panel.flush() and read(NOTES) == "第二条：同调群", repr(read(NOTES)))

print()
print("=" * 78)
print("3. 位置标记")
print("=" * 78)
panel.editor.setPlainText("")
panel.editor.moveCursor(R.QTextCursor.End)
panel.insert_location_marker()
text = panel.editor.toPlainText()
check("3.1 空文档插入标记不带多余空行",
      text.startswith("--- 拓扑学.pdf · 第 42 页 ·") and not text.startswith("\n"),
      repr(text[:48]))
check("3.2 标记含书名与页码", "拓扑学.pdf" in text and "第 42 页" in text, repr(text[:60]))

panel.editor.moveCursor(R.QTextCursor.End)
panel.insert_location_marker()
# 同一分钟内书名页码也相同 → 两条标记文本可以完全一样，这里只校验格式与行数
lines = [ln for ln in panel.editor.toPlainText().splitlines() if ln.strip()]
check("3.3 连续插入两条标记各占独立行",
      len(lines) == 2 and all(ln.startswith("--- ") and ln.endswith(" ---") for ln in lines),
      f"行数={len(lines)}")

panel.editor.setPlainText("正文")
cur = panel.editor.textCursor()
cur.setPosition(2)          # 行中间插入：标记前应补换行，避免与正文粘在一行
panel.editor.setTextCursor(cur)
panel.insert_location_marker()
body = panel.editor.toPlainText()
check("3.4 行中间插入时标记自成一行（前置换行）",
      body.startswith("正文\n--- ") and "正文---" not in body, repr(body[:40]))

print()
print("=" * 78)
print("4. 载入、上限与失败处理")
print("=" * 78)
with open(NOTES, "w", encoding="utf-8") as f:
    f.write("# 已有速记\n- 第一条\n- 第二条\n")
panel2 = R.NotesPanel(NOTES, context_provider=lambda: ("a.pdf", 1))
check("4.1 启动时载入既有内容", panel2.editor.toPlainText().startswith("# 已有速记"),
      panel2.status.text())
check("4.2 载入不算改动（不会被立刻回写）", not panel2.is_dirty, panel2.status.text())

big = os.path.join(TMP, "big.md")
with open(big, "w", encoding="utf-8") as f:
    f.write("x" * (R.NOTES_MAX_BYTES + 5000))
panel3 = R.NotesPanel(big)
check("4.3 超大文件按上限截断载入（不卡界面）",
      len(panel3.editor.toPlainText()) == R.NOTES_MAX_BYTES and "只载入了前一部分" in panel3.status.text(),
      f"载入 {len(panel3.editor.toPlainText())} 字符 / 上限 {R.NOTES_MAX_BYTES}")
panel3._dirty = False

# 写入失败：目标路径的父级是"文件"，必须不崩且状态可见
blocked = os.path.join(NOTES, "impossible", "notes.md")
panel4 = R.NotesPanel(blocked)
panel4.editor.setPlainText("写不进去")
ok = panel4.save()
check("4.4 写入失败时不抛异常且明确提示", (not ok) and "保存失败" in panel4.status.text(),
      panel4.status.text())

print()
print("=" * 78)
print("5. 原子写与主窗口集成")
print("=" * 78)
leftover = [f for f in os.listdir(TMP) if f.endswith(".tmp")]
check("5.1 保存过程不留 .tmp 残留", not leftover, f"残留 {leftover}")

pdf = os.path.join(TMP, "t.pdf")
doc = fitz.open()
for i in range(3):
    doc.new_page(width=400, height=600).insert_text((40, 80), f"page {i+1}")
doc.save(pdf)
doc.close()

win = R.MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.show()
win.open_pdf(pdf, 1)
app.processEvents()
QTest.qWait(150)

check("5.2 主窗口带速记栏且默认可见",
      win.notes_panel.isVisible() and win.splitter.count() == 3,
      f"分隔数={win.splitter.count()} 可见={win.notes_panel.isVisible()}")
win.toggle_notes_panel()
hidden = not win.notes_panel.isVisible()
win.toggle_notes_panel()
check("5.3 切换方法能隐藏/再显示", hidden and win.notes_panel.isVisible())

name, page = win.notes_context()
check("5.4 位置上下文取到当前书名与页码", name == "t.pdf" and page == 1, f"{name} / 第 {page} 页")

win.notes_panel.editor.setPlainText("关窗前未到防抖时间的字")
win.close()                   # closeEvent 里 flush
app.processEvents()
saved = read(NOTES)
check("5.5 关窗时未落盘的改动被补齐（不丢字）", saved == "关窗前未到防抖时间的字", repr(saved))
check("5.6 速记栏不会被写进用户真实路径",
      os.path.normcase(str(win.notes_panel.path)) == os.path.normcase(os.path.abspath(NOTES)),
      str(win.notes_panel.path))

print()
print("=" * 78)
print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
print("=" * 78)
for name in FAIL:
    print("  FAILED:", name)

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
