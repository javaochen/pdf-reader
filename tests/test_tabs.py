# -*- coding: utf-8 -*-
"""标签页回归测试：一个标签页一个独立阅读区、标签内换书、快捷键、缓存释放、上限。

不依赖显示器（Qt offscreen），不碰用户真实数据——PDF/状态/速记/暂存全部指向包内临时目录。

运行：python tests/test_tabs.py
"""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

TMP = os.path.join(PKG, "_test_tmp_tabs")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
os.environ["PDF_READER_NOTES_FILE"] = os.path.join(TMP, "notes.md")
os.environ["PDF_READER_CAPTURES_DIR"] = os.path.join(TMP, "captures")

import app_config  # noqa: E402,F401  # 必须先于 fitz / PySide6：补 Windows 运行库搜索路径
import fitz  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics, QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import reader as R  # noqa: E402

QMessageBox.critical = staticmethod(lambda *a, **k: None)
app = QApplication.instance() or QApplication([])
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def make_pdf(name, pages):
    path = os.path.join(TMP, name)
    doc = fitz.open()
    for i in range(pages):
        doc.new_page(width=400, height=600).insert_text((40, 80), f"{name} p{i + 1}")
    doc.save(path)
    doc.close()
    return path


def settle(ms=120):
    app.processEvents()
    QTest.qWait(ms)
    app.processEvents()


# 书名排序：代 < 拓，所以启动后自动打开的是「代数.pdf」
BOOK_A = make_pdf("代数.pdf", 4)
BOOK_B = make_pdf("拓扑学.pdf", 5)
BOOK_C = make_pdf("分析.pdf", 3)

win = R.MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.status.showMessage = lambda *a, **k: None   # 状态栏文字不参与断言
win.show()
settle()

print("=" * 78)
print("1. 启动：默认只有一个标签页")
print("=" * 78)
check("1.1 启动只有一个标签页", win.tabs.count() == 1, f"{win.tabs.count()} 个")
check("1.2 自动打开目录里第一本书",
      win.viewer.current_path == os.path.abspath(BOOK_A),
      os.path.basename(win.viewer.current_path or ""))
check("1.3 标签标题含书名与页码",
      win.tabs.tabText(0) == "代数 · 1", win.tabs.tabText(0))
check("1.4 self.viewer 指向当前标签页的阅读区",
      win.viewer is win.tabs.widget(win.tabs.currentIndex()))
check("1.5 标签页可关闭、可拖动重排",
      win.tabs.tabsClosable() and win.tabs.isMovable())

print()
print("=" * 78)
print("2. 标签页内快速切换文件（Ctrl+O 的行为）")
print("=" * 78)
first_viewer = win.viewer
win.viewer.goto_page(2)
settle()
win.open_pdf(BOOK_B)            # 等同于在 Ctrl+O 里选中另一本书
settle()
check("2.1 换书后标签页数量不变", win.tabs.count() == 1, f"{win.tabs.count()} 个")
check("2.2 仍是同一个阅读区（没有新建标签页）", win.viewer is first_viewer)
check("2.3 文件确实换成了新书",
      win.viewer.current_path == os.path.abspath(BOOK_B),
      os.path.basename(win.viewer.current_path or ""))
check("2.4 标签标题跟着换", win.tabs.tabText(0).startswith("拓扑学"), win.tabs.tabText(0))
win.open_pdf(BOOK_A, 1)
settle()

print()
print("=" * 78)
print("3. 新标签页：独立状态，可对照两本书")
print("=" * 78)
check("3.1 新标签打开成功", win.open_in_new_tab(BOOK_B, 3))
settle()
check("3.2 标签页数量 +1", win.tabs.count() == 2, f"{win.tabs.count()} 个")
tab1, tab2 = win.tabs.widget(0), win.tabs.widget(1)
check("3.3 两个标签页是两个独立阅读区",
      tab1 is not tab2 and tab1.doc is not None and tab2.doc is not None)
check("3.4 两个标签页各自的书不同",
      os.path.basename(tab1.current_path) == "代数.pdf"
      and os.path.basename(tab2.current_path) == "拓扑学.pdf",
      f"{os.path.basename(tab1.current_path)} / {os.path.basename(tab2.current_path)}")
check("3.5 页码互不干扰（代数=1，拓扑学=3）",
      tab1.current_page_1_based() == 1 and tab2.current_page_1_based() == 3,
      f"{tab1.current_page_1_based()} / {tab2.current_page_1_based()}")

tab1.zoom = 2.5                 # 缩放也是每个标签页自己的
win.tabs.setCurrentIndex(0)
settle()
check("3.6 切回第一个标签页，页码与缩放都还在",
      win.viewer is tab1 and win.viewer.current_page_1_based() == 1
      and abs(win.viewer.zoom - 2.5) < 1e-6,
      f"第 {win.viewer.current_page_1_based()} 页 zoom={win.viewer.zoom}")
check("3.7 切换后标签标题仍是各自的页码",
      win.tabs.tabText(1) == "拓扑学 · 3", win.tabs.tabText(1))

win.open_in_new_tab(BOOK_B, 5)  # 同一本书开第二个标签（前后对照）
settle()
check("3.8 同一本书可以开两个标签页", win.tabs.count() == 3, f"{win.tabs.count()} 个")
check("3.9 同名书靠页码区分标题",
      win.tabs.tabText(1) != win.tabs.tabText(2),
      f"{win.tabs.tabText(1)} / {win.tabs.tabText(2)}")

print()
print("=" * 78)
print("4. 切换标签页会释放非活动标签的位图（缓存 + 当前显示那张）")
print("=" * 78)
tab_a, tab_c = win.tabs.widget(0), win.tabs.widget(2)      # tab_c 当前是活动页
win.tabs.setCurrentIndex(0)     # tab_a 变活动，tab_c 被切走
settle()
check("4.1 被切走的标签页位图缓存已清空",
      tab_c._page_cache_bytes == 0 and not tab_c.page_cache,
      f"{tab_c._page_cache_bytes} 字节 / {len(tab_c.page_cache)} 条")
check("4.2 被切走的标签页当前显示位图也已释放",
      tab_c.image_label.pixmap() is None or tab_c.image_label.pixmap().isNull())
check("4.3 释放位图不影响它的页码与文档",
      tab_c.current_page_1_based() == 5 and tab_c.doc is not None,
      f"第 {tab_c.current_page_1_based()} 页")

# 在活动页滚到一个非顶部位置，随后切走再切回，位置不能丢（release_display 不动标签尺寸）
tab_a.goto_page(2)
tab_a.fit_width()
settle()
vb = tab_a.scroll.verticalScrollBar()
vb.setValue(min(vb.maximum(), 200))
y_before = vb.value()
check("4.4 页面可滚动（用于验证切换后滚动位置不丢）", y_before > 0, f"y={y_before}")

win.tabs.setCurrentIndex(2)     # 切回来：tab_c 应自动重渲染
settle()
pm = tab_c.image_label.pixmap()
check("4.5 切回来的标签页自动重新渲染",
      pm is not None and not pm.isNull() and tab_c._page_cache_bytes > 0,
      f"{tab_c._page_cache_bytes / 1024:.0f} KB")
check("4.6 被切走的 tab_a 位图已释放", not tab_a.page_cache and (
    tab_a.image_label.pixmap() is None or tab_a.image_label.pixmap().isNull()))
win.tabs.setCurrentIndex(0)
settle()
check("4.7 切回来滚动位置没有跳回顶部", vb.value() == y_before,
      f"{vb.value()} / {y_before}")

print()
print("=" * 78)
print("5. 关闭标签页与快捷键")
print("=" * 78)
count_before = win.tabs.count()
win.close_current_tab()
settle()
check("5.1 Ctrl+W 关闭当前标签页", win.tabs.count() == count_before - 1,
      f"{count_before} → {win.tabs.count()}")
check("5.2 关掉一个后当前阅读区仍然有效",
      win.viewer is win.tabs.widget(win.tabs.currentIndex()) and win.viewer.doc is not None)

while win.tabs.count() > 1:     # 收回到一个标签页，验证"最后一个只清空"
    win.close_current_tab()
    settle()
win.close_current_tab()
settle()
check("5.3 关闭最后一个标签页不会把窗口清空", win.tabs.count() == 1, f"{win.tabs.count()} 个")
check("5.4 最后一页只清空内容并显示占位",
      win.viewer.doc is None and win.tabs.tabText(0) == "空白", win.tabs.tabText(0))
win.open_pdf(BOOK_C)
settle()
check("5.5 空白标签页仍能打开书",
      win.viewer.doc is not None and win.viewer.current_path == os.path.abspath(BOOK_C))

scs = {s.key().toString(): s for s in win.findChildren(QShortcut)}
for seq in ("Ctrl+T", "Ctrl+W", "Ctrl+Tab", "Ctrl+Shift+Tab", "Alt+1", "Alt+9"):
    check(f"5.6 快捷键 {seq} 已注册为窗口快捷键",
          seq in scs and scs[seq].context() == Qt.WindowShortcut,
          f"命中 {seq in scs}")
keys = [k for k, *_ in R.MainWindow.SKEY if isinstance(k, str)]
check("5.7 快捷键表里能看到标签页相关按键",
      {"Ctrl+T", "Ctrl+W", "Ctrl+Tab", "Ctrl+Shift+Tab"} <= set(keys),
      str(sorted(k for k in keys if "Tab" in k or k in ("Ctrl+T", "Ctrl+W"))))
names = [c[0] for c in win.get_command_actions()]
check("5.8 命令面板里有标签页命令",
      {"新标签页", "关闭标签页", "下一个标签页", "上一个标签页"} <= set(names),
      str(names[:6]))
check("5.9 快捷键列表包含 Alt+1…9 切标签",
      len(R.MainWindow._tab_number_rows()) == 9)

win.open_in_new_tab(BOOK_A, 1)
win.open_in_new_tab(BOOK_B, 1)
settle()
win.tabs.setCurrentIndex(0)
win.next_tab()
check("5.10 下一个标签页", win.tabs.currentIndex() == 1, f"index={win.tabs.currentIndex()}")
win.prev_tab()
check("5.11 上一个标签页", win.tabs.currentIndex() == 0, f"index={win.tabs.currentIndex()}")
win.select_tab(2)
check("5.12 Alt+N 直接切到第 N 个标签页", win.tabs.currentIndex() == 2,
      f"index={win.tabs.currentIndex()}")
win.tabs.setCurrentIndex(0)
scs["Ctrl+Tab"].activated.emit()
check("5.13 触发 Ctrl+Tab 快捷键确实换页签", win.tabs.currentIndex() == 1,
      f"index={win.tabs.currentIndex()}")

print()
print("=" * 78)
print("6. 上限与失败回滚")
print("=" * 78)
while win.tabs.count() > 1:
    win.close_current_tab()
    settle()
while win.tabs.count() < R.MAX_TABS:
    if not win.open_in_new_tab(BOOK_A, 1):
        break
settle()
check("6.1 可以一直开到上限", win.tabs.count() == R.MAX_TABS, f"{win.tabs.count()}/{R.MAX_TABS}")
check("6.2 超过上限被拒绝", win.open_in_new_tab(BOOK_B) is False)
check("6.3 拒绝后标签页数量不变", win.tabs.count() == R.MAX_TABS, f"{win.tabs.count()} 个")

while win.tabs.count() > 1:
    win.close_current_tab()
    settle()
before = win.tabs.count()
check("6.4 打开不存在的文件失败", win.open_in_new_tab(os.path.join(TMP, "没有这本.pdf")) is False)
settle()
check("6.5 失败不会留下空标签页", win.tabs.count() == before, f"{win.tabs.count()} 个")

print()
print("=" * 78)
print("7. F5 刷新：多个标签页各自校验文件")
print("=" * 78)
win.open_pdf(BOOK_A, 1)
win.open_in_new_tab(BOOK_B, 2)
settle()
# 模拟"书在外部被删掉"：Windows 上被 MuPDF 打开的文件删不掉，所以先释放句柄，
# 再把路径写回阅读区——刷新时走的仍是"文件已不存在"那条分支
dead = os.path.join(TMP, "已删除.pdf")
shutil.copy(BOOK_A, dead)
win.open_in_new_tab(dead, 1)
settle()
win.viewer.clear_pdf()
win.viewer.current_path = dead
os.remove(dead)
win.refresh_files()
settle()
paths = [win.tabs.widget(i).current_path for i in range(win.tabs.count())]
check("7.1 还在的书保持打开", os.path.abspath(BOOK_A) in paths and os.path.abspath(BOOK_B) in paths,
      str([os.path.basename(p) if p else None for p in paths]))
check("7.2 被删掉的书在后台标签页里也被清掉",
      None in paths and win.tabs.count() == 3,
      str([os.path.basename(p) if p else None for p in paths]))
check("7.3 刷新后标签标题仍然正确",
      win.tabs.tabText(1) == "拓扑学 · 2", win.tabs.tabText(1))

win.open_pdf(BOOK_C, 1)
settle()
check("7.4 空白标签页刷新后仍可正常使用",
      win.viewer.doc is not None and win.viewer.current_path == os.path.abspath(BOOK_C))

print()
print("=" * 78)
print("8. 标签标题：长书名不把页签栏占满")
print("=" * 78)
LONG_NAME = "扫描版数学分析讲义_第二版_重排校勘本_含习题解答_2019.pdf"
BOOK_LONG = make_pdf(LONG_NAME, 3)
check("8.1 长书名的新标签打开成功", win.open_in_new_tab(BOOK_LONG, 2))
settle()
idx = win.tabs.count() - 1
title = win.tabs.tabText(idx)
metrics = QFontMetrics(win.tabs.tabBar().font())
width = metrics.horizontalAdvance(title)
check("8.2 标题宽度被限制在 TAB_TITLE_MAX_PX 内",
      width <= R.MainWindow.TAB_TITLE_MAX_PX,
      f"{width}px / 上限 {R.MainWindow.TAB_TITLE_MAX_PX}px：{title}")
check("8.3 省略号在中间，书名的开头还在",
      "…" in title and title.startswith("扫描版"), title)
check("8.4 省略后仍看得到页码", title.endswith("· 2"), title)
tip = win.tabs.tabToolTip(idx)
check("8.5 tooltip 给出完整路径与页数",
      os.path.abspath(BOOK_LONG) in tip and "第 2 / 3 页" in tip, tip.replace("\n", " | "))
check("8.6 短书名不受影响（不加省略号）", "…" not in win.tabs.tabText(0), win.tabs.tabText(0))
win.viewer.goto_page(3)
settle()
check("8.7 翻页后标签标题跟着更新", win.tabs.tabText(idx).endswith("· 3"),
      win.tabs.tabText(idx))

win.close()
settle()

print()
print("=" * 78)
print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
print("=" * 78)
for name in FAIL:
    print("  FAILED:", name)

# 关文档句柄后再删临时目录（Windows 上被占用的文件删不掉）
for i in range(win.tabs.count()):
    v = win.tabs.widget(i)
    try:
        if v.doc is not None:
            v.clear_pdf()
    except Exception:
        pass
for _ in range(5):
    shutil.rmtree(TMP, ignore_errors=True)
    if not os.path.exists(TMP):
        break
    QTest.qWait(150)
sys.exit(1 if FAIL else 0)
