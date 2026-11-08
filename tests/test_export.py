# -*- coding: utf-8 -*-
"""导出页码范围回归测试：子 PDF 内容、非法范围、路径处理、覆盖确认、对话框默认值。

不依赖显示器（Qt offscreen），不碰用户真实数据。

运行：python tests/test_export.py
"""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

TMP = os.path.join(PKG, "_test_tmp_export")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
os.environ["PDF_READER_NOTES_FILE"] = os.path.join(TMP, "notes.md")
os.environ["PDF_READER_CAPTURES_DIR"] = os.path.join(TMP, "captures")

import app_config  # noqa: E402,F401  # 必须先于 fitz / PySide6：补 Windows 运行库搜索路径
import fitz  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

import reader as R  # noqa: E402

app = QApplication.instance() or QApplication([])
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def settle(ms=50):
    app.processEvents()
    QTest.qWait(ms)
    app.processEvents()


def make_pdf(name, pages):
    path = os.path.join(TMP, name)
    doc = fitz.open()
    for i in range(pages):
        doc.new_page(width=400, height=600).insert_text((40, 80), f"{name} p{i + 1}")
    doc.save(path)
    doc.close()
    return path


def page_texts(path):
    doc = fitz.open(path)
    try:
        return [doc.load_page(i).get_text().strip() for i in range(doc.page_count)]
    finally:
        doc.close()


def no_part_files(folder):
    return not [n for n in os.listdir(folder) if n.endswith(".part")]


BOOK = make_pdf("代数.pdf", 6)
OUT_DIR = os.path.join(TMP, "out")
os.makedirs(OUT_DIR, exist_ok=True)

viewer = R.PdfViewer()
assert viewer.open_pdf(BOOK, 1)

print("=" * 78)
print("1. 基本导出：只包含指定连续页")
print("=" * 78)
OUT1 = os.path.join(OUT_DIR, "第2到4页.pdf")
ok, msg = viewer.export_pages(2, 4, OUT1)
check("1.1 导出返回成功", ok, msg)
check("1.2 目标文件已生成", os.path.isfile(OUT1))
texts = page_texts(OUT1) if os.path.isfile(OUT1) else []
check("1.3 导出页数正确（3 页）", len(texts) == 3, f"{len(texts)} 页")
check("1.4 内容对应第 2-4 页",
      len(texts) == 3 and all(f"p{n}" in texts[n - 2] for n in (2, 3, 4)),
      str(texts))
check("1.5 不包含范围外的页", all("p1" not in t and "p5" not in t for t in texts))
check("1.6 不会留下临时文件", no_part_files(OUT_DIR))
check("1.7 原书没有被改动", fitz.open(BOOK).page_count == 6)

print()
print("=" * 78)
print("2. 路径处理：自动补 .pdf、支持 ~、覆盖已有目标")
print("=" * 78)
OUT2 = os.path.join(OUT_DIR, "没有后缀")
ok, _ = viewer.export_pages(1, 1, OUT2)
check("2.1 没写后缀会自动补 .pdf", ok and os.path.isfile(OUT2 + ".pdf"))
check("2.2 不会生成没有后缀的文件", not os.path.exists(OUT2))
ok, msg = viewer.export_pages(5, 6, OUT1)      # 覆盖第一次导出的文件
check("2.3 允许覆盖已存在的目标", ok, msg)
check("2.4 覆盖后内容确实是新范围",
      [t for t in page_texts(OUT1) if "p5" in t or "p6" in t],
      str(page_texts(OUT1)))

print()
print("=" * 78)
print("3. 非法输入：拒绝并说明原因")
print("=" * 78)
bad_cases = [
    ("3.1 起始页为 0", viewer.export_pages(0, 2, os.path.join(OUT_DIR, "a.pdf"))),
    ("3.2 结束页超过总页数", viewer.export_pages(1, 99, os.path.join(OUT_DIR, "b.pdf"))),
    ("3.3 起始页大于结束页", viewer.export_pages(4, 2, os.path.join(OUT_DIR, "c.pdf"))),
    ("3.4 路径与正在阅读的原文件相同", viewer.export_pages(1, 2, BOOK)),
]
for name, (ok, msg) in bad_cases:
    check(name, ok is False and bool(msg), msg)
check("3.5 非法输入不会在磁盘留下文件",
      not any(os.path.exists(os.path.join(OUT_DIR, n)) for n in ("a.pdf", "b.pdf", "c.pdf")))
ok, msg = viewer.export_pages(1, 2, os.path.join(OUT_DIR, "不存在的子目录", "x.pdf"))
check("3.6 目录不存在时拒绝", ok is False and "目录" in msg, msg)

empty_viewer = R.PdfViewer()
ok, msg = empty_viewer.export_pages(1, 1, os.path.join(OUT_DIR, "d.pdf"))
check("3.7 没有打开 PDF 时拒绝", ok is False and "PDF" in msg, msg)

print()
print("=" * 78)
print("4. 导出对话框：默认范围与路径自动跟随")
print("=" * 78)
dlg = R.ExportPdfDialog(total_pages=6, current_page=3, base_path=BOOK)
check("4.1 起页默认当前页", dlg.page_range()[0] == 3, str(dlg.page_range()))
check("4.2 结束页默认不超过总页数", dlg.page_range()[1] == 6, str(dlg.page_range()))
check("4.3 默认路径在原书旁边",
      os.path.dirname(dlg.path_edit.text()) == os.path.dirname(BOOK)
      and dlg.path_edit.text().endswith("_p3-6.pdf"), dlg.path_edit.text())
check("4.4 提示文字给出页数与导出页数",
      "共 6 页" in dlg.hint.text() and "共 4 页" in dlg.hint.text(), dlg.hint.text())

dlg.spin_start.setValue(1)
settle()
check("4.5 结束页下限跟随起始页", dlg.spin_end.minimum() == 1)
check("4.6 未手动改路径时，文件名跟着范围更新",
      dlg.path_edit.text().endswith("_p1-6.pdf"), dlg.path_edit.text())

manual = os.path.join(OUT_DIR, "我自己的名字.pdf")
dlg.path_edit.setText(manual)
dlg.spin_end.setValue(2)
settle()
check("4.7 手动改过路径后，不再被范围变化覆盖",
      dlg.path_edit.text() == manual, dlg.path_edit.text())

print()
print("=" * 78)
print("5. 对话框确认：后缀、目录、原文件、覆盖询问")
print("=" * 78)
warnings, questions = [], []


def fake_warning(*a, **k):
    warnings.append(a[2] if len(a) > 2 else "")
    return QMessageBox.Ok


answer_box = {"answer": QMessageBox.No}


def fake_question(*a, **k):
    return answer_box["answer"]


QMessageBox.warning = staticmethod(fake_warning)
QMessageBox.question = staticmethod(fake_question)

dlg2 = R.ExportPdfDialog(6, 1, BOOK)
target = os.path.join(OUT_DIR, "确认后缀")
dlg2.path_edit.setText(target)
dlg2.accept()
check("5.1 确认时补上 .pdf 后缀", dlg2.output_path() == target + ".pdf", dlg2.output_path())
check("5.2 合法路径直接接受", dlg2.result() == QDialog.Accepted)

dlg3 = R.ExportPdfDialog(6, 1, BOOK)
dlg3.path_edit.setText("")
dlg3.accept()
check("5.3 空路径被拒绝并有提示",
      dlg3.result() != QDialog.Accepted and warnings, warnings[-1] if warnings else "")

dlg4 = R.ExportPdfDialog(6, 1, BOOK)
dlg4.path_edit.setText(os.path.join(OUT_DIR, "没有这个目录", "x.pdf"))
dlg4.accept()
check("5.4 不存在的目录被拒绝",
      dlg4.result() != QDialog.Accepted and "目录不存在" in (warnings[-1] if warnings else ""))

dlg5 = R.ExportPdfDialog(6, 1, BOOK)
dlg5.path_edit.setText(BOOK)
dlg5.accept()
check("5.5 不允许覆盖正在阅读的原文件",
      dlg5.result() != QDialog.Accepted
      and "原文件" in (warnings[-1] if warnings else ""))

existing = os.path.join(OUT_DIR, "已经存在.pdf")
open(existing, "wb").close()
dlg6 = R.ExportPdfDialog(6, 1, BOOK)
dlg6.path_edit.setText(existing)
answer_box["answer"] = QMessageBox.No
dlg6.accept()
check("5.6 覆盖前会询问；选「否」则不导出",
      dlg6.result() != QDialog.Accepted and dlg6.output_path() == "")
dlg6b = R.ExportPdfDialog(6, 1, BOOK)
dlg6b.path_edit.setText(existing)
answer_box["answer"] = QMessageBox.Yes
dlg6b.accept()
check("5.7 选「是」才接受覆盖", dlg6b.result() == QDialog.Accepted)

print()
print("=" * 78)
print("6. 主窗口接入：命令面板与状态栏")
print("=" * 78)
win = R.MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.status.showMessage = lambda *a, **k: None
win.show()
settle()
names = [a[0] for a in win.get_command_actions()]
check("6.1 命令面板里有「导出页码范围」", "导出页码范围" in names)
check("6.2 回调可解析", callable(win._resolve("export_pdf_dialog")))
check("6.3 当前书已打开，导出方法可用", bool(win.viewer.doc))
ok, msg = win.viewer.export_pages(2, 3, os.path.join(OUT_DIR, "从主窗口导出.pdf"))
check("6.4 主窗口当前阅读区可导出", ok and os.path.isfile(os.path.join(OUT_DIR, "从主窗口导出.pdf")), msg)

for d in (dlg, dlg2, dlg3, dlg4, dlg5, dlg6, dlg6b):
    d.close()
win.close()
viewer.close()
empty_viewer.close()
settle()

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
    QTest.qWait(120)
sys.exit(1 if FAIL else 0)
