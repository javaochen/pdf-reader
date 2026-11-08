# -*- coding: utf-8 -*-
"""置顶书单回归测试：启动顺序、缺失文件、标签上限、写回配置、保留其他键。

不依赖显示器（Qt offscreen），不碰用户真实配置——配置路径由
PDF_READER_CONFIG_PATH 指向包内临时目录。

运行：python tests/test_pinned.py
"""
import json
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

TMP = os.path.join(PKG, "_test_tmp_pinned")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
CONFIG = os.path.join(TMP, "reader_config.json")
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
os.environ["PDF_READER_NOTES_FILE"] = os.path.join(TMP, "notes.md")
os.environ["PDF_READER_CAPTURES_DIR"] = os.path.join(TMP, "captures")
os.environ["PDF_READER_CONFIG_PATH"] = CONFIG
os.environ["PDF_READER_MAX_TABS"] = "2"      # 方便验证"超过上限只开前 N 本"

import app_config  # noqa: E402,F401  # 必须先于 fitz / PySide6：补 Windows 运行库搜索路径
import logging  # noqa: E402
app_config.log.setLevel(logging.CRITICAL)   # 本套会故意写坏配置，别把预期内 warning 刷满屏
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


def settle(ms=60):
    app.processEvents()
    QTest.qWait(ms)
    app.processEvents()


def make_pdf(name, pages=3):
    path = os.path.join(TMP, name)
    doc = fitz.open()
    for i in range(pages):
        doc.new_page(width=400, height=600).insert_text((40, 80), f"{name} p{i + 1}")
    doc.save(path)
    doc.close()
    return path


def read_config():
    with open(CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)


def pins_in_config():
    return [os.path.normcase(p) for p in read_config().get("pinned_pdfs", [])]


def norm(path):
    return os.path.normcase(os.path.abspath(path))


BOOK_A = make_pdf("代数.pdf")
BOOK_B = make_pdf("拓扑学.pdf")
BOOK_C = make_pdf("分析.pdf")
MISSING = os.path.join(TMP, "暂时不在的盘.pdf")

CONFIG_DATA = {
    "max_tabs": 2,
    "custom_key": {"keep": True},
    "pinned_pdfs": [BOOK_A, MISSING, BOOK_B, BOOK_C],
}
with open(CONFIG, "w", encoding="utf-8") as f:
    json.dump(CONFIG_DATA, f, ensure_ascii=False)
# app_config 在 import 时已加载（当时还没有配置文件）；这里同步内存，模拟下次启动
app_config.CONFIG.clear()
app_config.CONFIG.update(CONFIG_DATA)

print("=" * 78)
print("1. 启动时打开置顶书：顺序、跳过缺失、标签上限")
print("=" * 78)
check("1.1 配置路径可用环境变量隔离", str(app_config.CONFIG_PATH) == CONFIG, str(app_config.CONFIG_PATH))
check("1.2 标签上限来自配置", R.MAX_TABS == 2, str(R.MAX_TABS))

win = R.MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.status.showMessage = lambda *a, **k: None
win.show()
settle()

check("1.3 置顶书按配置顺序打开", win.tabs.count() == 2, f"{win.tabs.count()} 个标签")
opened = [win.tabs.widget(i).current_path for i in range(win.tabs.count())]
check("1.4 前两本顺序正确",
      len(opened) == 2 and norm(opened[0]) == norm(BOOK_A) and norm(opened[1]) == norm(BOOK_B),
      str([os.path.basename(p or "") for p in opened]))
check("1.5 超过 MAX_TABS 的置顶书没有打开",
      all(norm(p) != norm(BOOK_C) for p in opened if p))
check("1.6 缺失的置顶文件被跳过且不新建空标签",
      win.tabs.count() == 2 and all(p is not None for p in opened))
check("1.7 启动后停在第一本置顶书", win.tabs.currentIndex() == 0
      and norm(win.viewer.current_path) == norm(BOOK_A))
check("1.8 内存中的置顶列表保留缺失项（不静默删配置）",
      any(norm(p) == norm(MISSING) for p in win.pinned), str(len(win.pinned)))

print()
print("=" * 78)
print("2. 置顶标记：标题与 tooltip")
print("=" * 78)
check("2.1 置顶标签标题以 ★ 开头", win.tabs.tabText(0).startswith("★ "), win.tabs.tabText(0))
check("2.2 tooltip 说明已置顶", "已置顶" in win.tabs.tabToolTip(0),
      win.tabs.tabToolTip(0).replace("\n", " | "))

print()
print("=" * 78)
print("3. 取消置顶 / 重新置顶：写回配置且保留其他键")
print("=" * 78)
win.toggle_pin_current()
settle()
check("3.1 取消置顶后配置里没有这本书", norm(BOOK_A) not in pins_in_config(),
      str([os.path.basename(p) for p in read_config()["pinned_pdfs"]]))
check("3.2 其他置顶项原样保留",
      norm(BOOK_B) in pins_in_config() and norm(MISSING) in pins_in_config())
check("3.3 配置里的其他键没有被覆盖", read_config().get("custom_key") == {"keep": True})
check("3.4 标签标题去掉 ★", not win.tabs.tabText(0).startswith("★ "), win.tabs.tabText(0))
check("3.5 只去掉被取消那本的 ★，其他置顶书标记保留",
      not win.tabs.tabText(0).startswith("★ ")
      and win.tabs.tabText(1).startswith("★ "),
      f"{win.tabs.tabText(0)} / {win.tabs.tabText(1)}")

win.toggle_pin_current()
settle()
check("3.6 再次执行恢复置顶", norm(BOOK_A) in pins_in_config())
check("3.7 恢复后 ★ 回来", win.tabs.tabText(0).startswith("★ "), win.tabs.tabText(0))
check("3.8 新置顶项追加在末尾",
      pins_in_config()[-1] == norm(BOOK_A), str([os.path.basename(p) for p in read_config()["pinned_pdfs"]]))

check("3.9 写回时去重", app_config.save_pinned_pdfs([BOOK_A, BOOK_A, BOOK_B])
      and len(app_config.load_pinned_pdfs()) == 2,
      str(len(app_config.load_pinned_pdfs())))

print()
print("=" * 78)
print("4. 没有可打开的置顶书时退回默认逻辑")
print("=" * 78)
win.close()
settle()
app_config.CONFIG["pinned_pdfs"] = []
win2 = R.MainWindow()
win2.setAttribute(Qt.WA_DontShowOnScreen, True)
win2.status.showMessage = lambda *a, **k: None
win2.show()
settle()
check("4.1 置顶为空时仍自动打开目录里第一本书",
      win2.tabs.count() == 1 and win2.viewer.doc is not None,
      f"{win2.tabs.count()} 个标签")
win2.close()
settle()

app_config.CONFIG["pinned_pdfs"] = [MISSING]
win3 = R.MainWindow()
win3.setAttribute(Qt.WA_DontShowOnScreen, True)
win3.status.showMessage = lambda *a, **k: None
win3.show()
settle()
check("4.2 置顶文件全缺失时退回默认逻辑，不崩",
      win3.tabs.count() == 1 and win3.viewer.doc is not None)
win3.close()
settle()

print()
print("=" * 78)
print("5. 配置损坏时拒绝写入，避免覆盖用户手写内容")
print("=" * 78)
with open(CONFIG, "w", encoding="utf-8") as f:
    f.write("{ 这不是合法 JSON")
before = open(CONFIG, "r", encoding="utf-8").read()
check("5.1 配置文件损坏时 save_config 返回 False",
      app_config.save_config(pinned_pdfs=[BOOK_A]) is False)
check("5.2 损坏的原文没有被覆盖",
      open(CONFIG, "r", encoding="utf-8").read() == before)

print()
print("=" * 78)
print("6. 命令面板接入")
print("=" * 78)
win4 = R.MainWindow()
win4.setAttribute(Qt.WA_DontShowOnScreen, True)
win4.status.showMessage = lambda *a, **k: None
win4.show()
settle()
names = [a[0] for a in win4.get_command_actions()]
check("6.1 命令面板里有置顶命令", "置顶/取消置顶当前书" in names)
check("6.2 回调可解析", callable(win4._resolve("toggle_pin_current")))
win4.viewer.clear_pdf()
win4.toggle_pin_current()
check("6.3 没有打开 PDF 时不会崩溃", True)
win4.close()

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
