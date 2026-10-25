# -*- coding: utf-8 -*-
"""命令面板回归测试：动作表去重、搜索过滤、键盘选择与执行。

不依赖显示器（Qt offscreen），不碰用户真实数据。

运行：python tests/test_commands.py
"""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

TMP = os.path.join(PKG, "_test_tmp_cmd")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
os.environ["PDF_READER_NOTES_FILE"] = os.path.join(TMP, "notes.md")
os.environ["PDF_READER_CAPTURES_DIR"] = os.path.join(TMP, "captures")

import app_config  # noqa: E402,F401  # 必须先于 fitz / PySide6：补 Windows 运行库搜索路径
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

import reader as R  # noqa: E402

QMessageBox.critical = staticmethod(lambda *a, **k: None)
app = QApplication.instance() or QApplication([])
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def settle(ms=80):
    app.processEvents()
    QTest.qWait(ms)
    app.processEvents()


win = R.MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.status.showMessage = lambda *a, **k: None
win.show()
settle()

print("=" * 78)
print("1. 动作表：同一个回调只出现一次")
print("=" * 78)
actions = win.get_command_actions()
check("1.1 命令面板不是空的", len(actions) >= 30, f"{len(actions)} 条")

marks = [R.MainWindow._action_key(cb) for _n, _d, cb, _k in actions]
dups = sorted({m.__name__ for m in marks if marks.count(m) > 1})
check("1.2 没有重复的动作", not dups, f"重复：{dups}")

names = [n for n, *_ in actions]
dup_names = sorted({n for n in names if names.count(n) > 1})
check("1.3 没有同名命令", not dup_names, f"重复：{dup_names}")

fits = [a for a in actions if a[0] == "适合页面"]
check("1.4 适合页面只剩一条且带键位",
      len(fits) == 1 and fits[0][3] == "Ctrl+0", str(fits))
inverts = [a for a in actions if a[0] == "反色显示"]
check("1.5 反色显示只剩一条且带键位",
      len(inverts) == 1 and inverts[0][3] == "Ctrl+Shift+I", str(inverts))
check("1.6 只有少数命令没有键位（扩展命令）",
      sum(1 for a in actions if a[3] is None) <= 12,
      f"{sum(1 for a in actions if a[3] is None)} 条无键位")
for want, key in (("新标签页", "Ctrl+T"), ("关闭标签页", "Ctrl+W"),
                  ("下一个标签页", "Ctrl+Tab"), ("上一个标签页", "Ctrl+Shift+Tab"),
                  ("速记栏", "Ctrl+N"), ("截取暂存", "Ctrl+Shift+X")):
    got = [a for a in actions if a[0] == want]
    check(f"1.7 命令「{want}」唯一且键位为 {key}",
          len(got) == 1 and got[0][3] == key, str(got))

print()
print("=" * 78)
print("2. 命令面板：搜索、键盘、执行")
print("=" * 78)
dlg = R.CommandDialog(actions, win)
dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
dlg.show()
settle()
check("2.1 默认列出全部命令", dlg.list_widget.count() == len(actions),
      f"{dlg.list_widget.count()} / {len(actions)}")

dlg.input.setText("标签")
settle()
labels = [dlg.list_widget.item(i).text() for i in range(dlg.list_widget.count())]
check("2.2 输入关键词只留相关命令",
      0 < len(labels) < len(actions) and all("标签" in t for t in labels),
      f"{len(labels)} 条")
check("2.3 过滤后默认选中第一条", dlg.list_widget.currentRow() == 0)

dlg.input.setText("Ctrl+W")
settle()
texts = [dlg.list_widget.item(i).text() for i in range(dlg.list_widget.count())]
check("2.4 可以用快捷键搜（Ctrl+W → 关闭标签页）",
      len(texts) == 1 and texts[0].startswith("关闭标签页"), str(texts))

dlg.input.setText("标签 关闭")
settle()
texts = [dlg.list_widget.item(i).text() for i in range(dlg.list_widget.count())]
check("2.5 支持空格分词（标签 + 关闭）",
      len(texts) == 1 and texts[0].startswith("关闭标签页"), str(texts))

dlg.input.setText("缩略图")
settle()
texts = [dlg.list_widget.item(i).text() for i in range(dlg.list_widget.count())]
check("2.6 说明文字也能搜（缩略图 → 截取暂存）",
      len(texts) == 1 and texts[0].startswith("截取暂存"), str(texts))

dlg.input.setText("zzz-不可能匹配")
settle()
only = dlg.list_widget.item(0)
check("2.7 无匹配时给一条不可选的占位",
      dlg.list_widget.count() == 1 and not (only.flags() & Qt.ItemIsEnabled),
      only.text())
calls = []
dlg.run_selected(only)
check("2.8 占位项不会被执行、也不会关闭对话框",
      not calls and dlg.result() != QDialog.Accepted)

# ---- 用一个假动作表验证"选中 → 回车执行"，不真的去开文件 ----
fake = [
    ("假命令甲", "测试用", lambda: calls.append("甲"), "Ctrl+1"),
    ("假命令乙", "测试用", lambda: calls.append("乙"), "Ctrl+2"),
    ("假命令丙", "测试用", lambda: calls.append("丙"), None),
]
dlg2 = R.CommandDialog(fake, win)
dlg2.setAttribute(Qt.WA_DontShowOnScreen, True)
dlg2.show()
settle()
dlg2.input.setText("甲")
settle()
QTest.keyClick(dlg2.input, Qt.Key_Return)
settle()
check("2.9 输入框里回车执行当前选中项",
      calls == ["甲"] and dlg2.result() == QDialog.Accepted, f"calls={calls}")

dlg3 = R.CommandDialog(fake, win)
dlg3.setAttribute(Qt.WA_DontShowOnScreen, True)
dlg3.show()
settle()
QTest.keyClick(dlg3.input, Qt.Key_Down)
settle()
check("2.10 输入框里 ↓ 移动选中项", dlg3.list_widget.currentRow() == 1,
      f"row={dlg3.list_widget.currentRow()}")
QTest.keyClick(dlg3.input, Qt.Key_Return)
settle()
check("2.11 回车执行的是移动后的那条", calls == ["甲", "乙"], f"calls={calls}")

dlg4 = R.CommandDialog(fake, win)
dlg4.setAttribute(Qt.WA_DontShowOnScreen, True)
dlg4.show()
settle()
dlg4.list_widget.item(2).setSelected(True)
dlg4.list_widget.setCurrentRow(2)
dlg4.run_selected(dlg4.list_widget.currentItem())
settle()
check("2.12 点击/选中第三条执行的是第三条", calls == ["甲", "乙", "丙"], f"calls={calls}")

for d in (dlg, dlg2, dlg3, dlg4):
    d.close()
win.close()
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
