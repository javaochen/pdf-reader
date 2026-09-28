"""reader.py 回归测试：截取模式应“进入后循环，再次按截取模式指令才关闭”。

运行：python tests/test_crop_loop.py   （自动使用 Qt offscreen 平台，无需显示器）


使用 offscreen 平台驱动真实代码路径：
  1. Ctrl+S 进入截取模式 → 保持开启
  2. 回车选择两条线并完成一次截取 → 复制到剪贴板，模式仍开启（循环）
  3. 连续第二次截取 → 仍然开启
  4. Esc → 只撤销分割线，不退出
  5. 再次触发“截取模式”指令 → 才关闭
"""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 允许直接运行本文件：把包根目录（reader.py 所在目录）加入模块搜索路径
PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PKG)
# PDF 目录与状态目录都指向包内临时目录，避免扫描/写入用户的真实下载目录
TMP = os.path.join(PKG, "_test_tmp_crop")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP

import fitz
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent

import reader as R

FAILS = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


# 屏蔽可能弹出的模态框，保证无人值守
QMessageBox.critical = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)

app = QApplication.instance() or QApplication(sys.argv)

# --- 造一个测试 PDF ---
tmp_pdf = os.path.join(TMP, "_tmp_crop_test.pdf")
doc = fitz.open()
for i in range(3):
    page = doc.new_page(width=400, height=600)
    page.insert_text((50, 100 + i * 10), f"page {i + 1} hello")
    page.insert_text((50, 300), "middle line")
doc.save(tmp_pdf)
doc.close()

statuses = []
win = R.MainWindow()
win.status.showMessage = lambda msg, *a, **k: statuses.append(msg)
win.viewer.status_callback = lambda msg: statuses.append(msg)
win.viewer.open_pdf(tmp_pdf, 1)
win.viewer.resize(900, 800)
win.show()
app.processEvents()

viewer = win.viewer
overlay = viewer.crop_overlay


def press(key):
    """把按键投递给焦点控件（模拟真实键盘输入）。"""
    ev = QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)
    app.sendEvent(app.focusWidget() or overlay, ev)
    app.processEvents()


def do_capture(y1, y2):
    """回车选两条分割线，完成一次循环截取。"""
    overlay.preview_y = y1
    press(Qt.Key_Return)
    overlay.preview_y = y2
    press(Qt.Key_Return)


# 1) 进入截取模式
win.toggle_crop_mode()
check("1. Ctrl+S 进入截取模式", viewer.crop_mode is True)
check("1. 覆盖层已激活并接管鼠标/键盘", overlay.active is True and overlay.isVisible())
check("1. 进入后立即循环（无自动退出）", viewer.crop_mode is True, statuses[-1])

# 2) 第一次截取
do_capture(30, 120)
check("2. 截取后仍处于截取模式（循环）", viewer.crop_mode is True,
      f"crop_mode={viewer.crop_mode}")
check("2. 覆盖层仍然激活", overlay.active is True)
check("2. 分割线已清空，可立即开始下一次",
      viewer.crop_marks_top is None and viewer.crop_marks_bottom is None
      and overlay.top_marker is None and overlay.bottom_marker is None)
check("2. 结果已复制到剪贴板",
      not QApplication.clipboard().pixmap().isNull(),
      f"{QApplication.clipboard().pixmap().width()}x{QApplication.clipboard().pixmap().height()}")
check("2. 状态提示说明可继续/如何退出",
      "截取模式继续" in statuses[-1] and "Ctrl+S" in statuses[-1], statuses[-1])

# 3) 第二次截取（循环验证）
first_size = (QApplication.clipboard().pixmap().width(),
              QApplication.clipboard().pixmap().height())
do_capture(200, 380)
second = QApplication.clipboard().pixmap()
check("3. 第二次截取成功且仍在截取模式", viewer.crop_mode is True)
check("3. 剪贴板内容已更新（新区域）",
      (second.width(), second.height()) != first_size,
      f"{second.width()}x{second.height()}")

# 4) Esc 只撤销分割线，不退出
viewer.set_crop_marks((0, 20), (0, 60))
win.cancel_crop_mode()
check("4. Esc 撤销结束线，模式保持", viewer.crop_mode is True
      and viewer.crop_marks_top == (0, 20) and viewer.crop_marks_bottom is None,
      statuses[-1])
win.cancel_crop_mode()
check("4. Esc 再撤销起始线，模式保持", viewer.crop_mode is True
      and viewer.crop_marks_top is None, statuses[-1])
win.cancel_crop_mode()
check("4. 无线可撤销时 Esc 也不退出", viewer.crop_mode is True, statuses[-1])

# 5) 只有“截取模式”指令才关闭
win.toggle_crop_mode()
check("5. 再次触发截取模式 → 退出", viewer.crop_mode is False)
check("5. 覆盖层已隐藏并复位",
      overlay.active is False and not overlay.isVisible()
      and overlay.preview_y is None and overlay.top_marker is None)

# 6) 互斥：进入遮挡模式应退出截取模式
win.toggle_crop_mode()
check("6. 重新进入截取模式", viewer.crop_mode is True)
win.toggle_cover_mode()
check("6. 进入遮挡模式时截取模式关闭",
      viewer.crop_mode is False and overlay.cover_mode is True)
win.toggle_cover_mode()
check("6. 遮挡模式可正常退出", overlay.cover_mode is False)

# 7) 跨页循环截取（起始线与结束线在不同页）
win.toggle_crop_mode()
viewer.set_crop_marks((0, 100), (1, 200))
do_capture(400, 500)  # 追加一次，确认跨页状态未破坏模式
check("7. 跨页截取后仍在截取模式", viewer.crop_mode is True)

win.toggle_crop_mode()
check("7. 指令关闭截取模式", viewer.crop_mode is False)

# 8) 真实快捷键链路：覆盖层持有焦点时 Ctrl+S / Esc 仍由窗口快捷键处理
from PySide6.QtTest import QTest

QApplication.setActiveWindow(win)
app.processEvents()
overlay.setFocus()
app.processEvents()

QTest.keyClick(win, Qt.Key_S, Qt.ControlModifier)
app.processEvents()
check("8. Ctrl+S 快捷键进入截取模式", viewer.crop_mode is True, statuses[-1])

QTest.keyClick(win, Qt.Key_Escape)
app.processEvents()
check("8. Esc 快捷键不退出截取模式", viewer.crop_mode is True, statuses[-1])

QTest.keyClick(win, Qt.Key_S, Qt.ControlModifier)
app.processEvents()
check("8. Ctrl+S 快捷键退出截取模式", viewer.crop_mode is False, statuses[-1])

print()
print("=" * 78)
print("9. 跳页后截取仍可用（回归：焦点丢失 / 坐标映射错误）")
print("=" * 78)
from PySide6.QtCore import QTimer, QPoint  # noqa: E402

QApplication.setActiveWindow(win)
app.processEvents()
win.toggle_crop_mode()
app.processEvents()
check("9.1 进入截取模式", viewer.crop_mode is True)
check("9.2 覆盖层持有键盘焦点", app.focusWidget() is overlay,
      type(app.focusWidget()).__name__ if app.focusWidget() else "None")


def hover(ratio):
    """在覆盖层上移动鼠标（换位置才会产生 MouseMove 事件）。"""
    QTest.mouseMove(overlay, QPoint(overlay.width() // 2, int(overlay.height() * ratio)))
    app.processEvents()


# 记录第一条分割线
hover(0.3)
viewer.crop_overlay.top_marker = (viewer.current_page_index, viewer.crop_overlay.preview_y)
check("9.3 鼠标定位得到预览线", viewer.crop_overlay.preview_y is not None,
      f"preview_y={viewer.crop_overlay.preview_y}")

# 真实跳页对话框路径（用定时器在事件循环里填页码并确定）
page_before = viewer.current_page_1_based()


def fill_jump_dialog():
    for w in app.topLevelWidgets():
        if isinstance(w, R.JumpPageDialog):
            w.spin.setValue(page_before + 2)
            w.accept_page()
            return


QTimer.singleShot(0, fill_jump_dialog)
win.jump_dialog()
app.processEvents()
QTest.qWait(30)
app.processEvents()
check("9.4 跳页生效", viewer.current_page_1_based() == page_before + 2,
      f"第 {viewer.current_page_1_based()} 页")
check("9.5 跳页后覆盖层重新拿到焦点（旧实现焦点变 None）",
      app.focusWidget() is overlay,
      type(app.focusWidget()).__name__ if app.focusWidget() else "None")
check("9.6 换页后旧预览线被清空（避免用上一页的 y 截错位置）",
      viewer.crop_overlay.preview_y is None, f"preview_y={viewer.crop_overlay.preview_y}")

# 跳页后重新定位并确认第二条线 → 应真正完成一次截取
hover(0.5)
QApplication.clipboard().clear()
app.processEvents()
QTest.keyClick(app.focusWidget() or win, Qt.Key_Return)
app.processEvents()
pm = QApplication.clipboard().pixmap()
check("9.7 跳页后回车完成截取", not pm.isNull() and pm.height() > 0,
      f"剪贴板 {pm.width()}x{pm.height()}")

# 坐标映射正确性（旧实现 label.mapTo(overlay) 用错了父控件，坐标是错的）
# 不变式：滚动多少，页面原点就移动多少（旧实现原点是死值，不随滚动变化）
viewer.fit_page()
app.processEvents()
hover(0.4)
hbar = viewer.scroll.horizontalScrollBar()
vbar = viewer.scroll.verticalScrollBar()
if hbar.maximum() > 0:
    bar, axis, axis_name = hbar, 0, "水平"
else:
    bar, axis, axis_name = vbar, 1, "垂直"
origin_before = overlay._label_page_origin()
bar.setValue(min(bar.maximum(), 100))
app.processEvents()
origin_after = overlay._label_page_origin()
if origin_before and origin_after and bar.value() > 0:
    shifted = origin_before[axis] - origin_after[axis]
    check(f"9.8 页面原点随{axis_name}滚动正确移动", abs(shifted - bar.value()) <= 2,
          f"原点位移 {shifted:.0f} / 滚动 {bar.value()}")
else:
    check("9.8 页面原点随滚动正确移动", False,
          f"不可滚动：max={bar.maximum()} value={bar.value()}")

check("9.9 覆盖层与页面标签是兄弟控件（必须经视口换算，旧实现直接互相 mapTo）",
      overlay.parent() is viewer.scroll.viewport()
      and viewer.image_label.parent() is viewer.scroll.viewport(),
      f"overlay.parent={type(overlay.parent()).__name__} "
      f"label.parent={type(viewer.image_label.parent()).__name__}")

# 窗口级回车：焦点在别处时也应能确认分割线
viewer.crop_overlay.top_marker = None
viewer.crop_overlay.bottom_marker = None
win.bookmark_panel.setFocus()
app.processEvents()
overlay.preview_y = 30
QTest.keyClick(win, Qt.Key_Return)
app.processEvents()
check("9.10 焦点不在覆盖层时，窗口级回车仍能确认分割线",
      viewer.crop_overlay.top_marker is not None,
      f"marks={viewer.crop_overlay.top_marker, viewer.crop_overlay.bottom_marker}")

# 没有定位就回车 → 给出提示而不是静默失败
statuses.clear()
viewer.crop_overlay.top_marker = None
viewer.crop_overlay.bottom_marker = None
viewer.crop_overlay.preview_y = None
win.crop_enter()
check("9.11 未定位就回车会给出提示", any("请先把鼠标" in s for s in statuses),
      str(statuses[-1:]))

win.toggle_crop_mode()
try:
    if viewer.doc is not None:
        viewer.doc.close()
        viewer.doc = None
    win.close()
finally:
    # 关掉窗口会触发进度落盘；先落盘再删临时目录
    shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1)
print("ALL CHECKS PASSED")
