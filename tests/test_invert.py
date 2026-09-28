# -*- coding: utf-8 -*-
"""反色显示回归测试：只改显示配色，绝不污染截取输出与内容框探测。

不依赖显示器（Qt offscreen），全部写包内临时目录。

运行：python tests/test_invert.py
"""
import os
import shutil
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

TMP = os.path.join(PKG, "_test_tmp_invert")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
os.environ["PDF_READER_NOTES_FILE"] = os.path.join(TMP, "notes.md")

import fitz  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import reader as R  # noqa: E402

QMessageBox.critical = staticmethod(lambda *a, **k: None)
app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def pixel(pixmap, x, y):
    """取某个逻辑像素的 RGB（考虑 devicePixelRatio）。"""
    img = pixmap.toImage()
    dpr = pixmap.devicePixelRatio() or 1.0
    return img.pixelColor(int(x * dpr), int(y * dpr)).getRgb()[:3]


def mean_brightness(pixmap):
    """平均亮度。注意：qimage_to_rgb_array 返回的是 img 缓冲区的视图，
    转换结果必须先绑定到局部变量，否则临时对象被回收后会读到已释放内存（会崩）。"""
    img = pixmap.toImage()
    if img.format() != R.QImage.Format_RGB888:
        img = img.convertToFormat(R.QImage.Format_RGB888)
    return float(R.qimage_to_rgb_array(img).mean())


def dark_fraction(pixmap):
    """暗像素占比：白底黑字的原图应远小于黑底白字的反色图。"""
    img = pixmap.toImage()
    if img.format() != R.QImage.Format_RGB888:
        img = img.convertToFormat(R.QImage.Format_RGB888)
    return float((R.qimage_to_rgb_array(img).mean(axis=2) < 70).mean())


# 造一页"白纸 + 左上角黑块"的样本：反色后正好互换，便于逐点断言
pdf = os.path.join(TMP, "t.pdf")
doc = fitz.open()
for i in range(4):
    page = doc.new_page(width=400, height=600)
    page.draw_rect(fitz.Rect(20, 20, 120, 90), color=(0, 0, 0), fill=(0, 0, 0))
    page.insert_text((200, 300), f"page {i+1}")
doc.save(pdf)
doc.close()

print("=" * 78)
print("1. 配置与默认值")
print("=" * 78)
check("1.1 默认不反色（INVERT_DEFAULT=False）",
      R.INVERT_DEFAULT is False and R.INVERT_ENABLED is False,
      f"INVERT_DEFAULT={R.INVERT_DEFAULT} INVERT_ENABLED={R.INVERT_ENABLED}")
_bool_cases = [("true", True), ("1", True), ("yes", True), ("on", True), ("是", True),
               ("false", False), ("0", False), ("no", False), ("", False)]
bad = [(raw, R._cfg_bool("__probe__", raw)) for raw, _ in _bool_cases
       if R._cfg_bool("__probe__", raw) is not _]
check("1.2 布尔配置解析（true/1/yes/on/是/0/false/no 等）", not bad, f"异常={bad}")
check("1.3 真布尔值直接透传", R._cfg_bool("__probe__", True) is True
      and R._cfg_bool("__probe__", False) is False)

viewer = R.PdfViewer(status_callback=lambda m: None)
viewer.resize(900, 800)
viewer.show()
viewer.open_pdf(pdf, 1)
viewer.fit_page()
app.processEvents()
QTest.qWait(150)

print()
print("=" * 78)
print("2. 显示路径确实反色")
print("=" * 78)
check("2.1 初始状态不反色", viewer.invert_enabled is False)
base = viewer.render_page_to_pixmap(0)
corner_before = pixel(base, 300, 560)      # 页面右下角：白纸
check("2.2 未反色时纸面是白的", min(corner_before) > 200, f"右下角={corner_before}")

viewer.set_invert_enabled(True)
app.processEvents()
QTest.qWait(80)
inv = viewer.render_page_to_pixmap(0)
corner_after = pixel(inv, 300, 560)
block_after = pixel(inv, 40, 40)           # 原本的黑块，反色后应变白
check("2.3 反色后纸面变黑", max(corner_after) < 70, f"右下角={corner_after}")
check("2.4 反色后黑块变白", min(block_after) > 200, f"左上块={block_after}")
check("2.5 反色与未反色的像素确实不同（不是拿到旧缓存）",
      corner_before != corner_after and viewer.invert_enabled)

viewer.set_invert_enabled(False)
app.processEvents()
QTest.qWait(80)
back = viewer.render_page_to_pixmap(0)
check("2.6 关掉反色后恢复原样（缓存键含反色，不串味）",
      pixel(back, 300, 560) == corner_before and pixel(back, 40, 40) == pixel(base, 40, 40),
      f"{pixel(back, 300, 560)} vs {corner_before}")

check("2.7 状态栏显示反色指示", "反色" not in viewer.status_text(), viewer.status_text())
viewer.set_invert_enabled(True)
app.processEvents()
check("2.8 开启后状态栏标出反色", viewer.status_text().endswith("反色"), viewer.status_text())

print()
print("=" * 78)
print("3. 截取与内容框探测必须保持原始像素（反色前的关键不变量）")
print("=" * 78)
cap = viewer._crop_lossless_region(0, 10, 300)      # 含左上角黑块的条带
cap_bright = mean_brightness(cap)
cap_dark = dark_fraction(cap)
check("3.1 反色开启时无损截取仍是白底黑字", cap_bright > 200,
      f"平均亮度={cap_bright:.1f}（>200 表示纸面仍是白的）")
check("3.2 截取结果里仍有黑色内容（没有被一起反色）",
      0.002 < cap_dark < 0.35,
      f"暗像素占比={cap_dark:.4f}（原图的黑块；若被反色会变成大面积黑底）")

exact = viewer.render_page_to_pixmap(0, zoom=1.0)     # exact=True 路径
check("3.3 精确渲染路径（zoom 明确给出）不反色",
      min(pixel(exact, 300, 560)) > 200, f"纸面={pixel(exact, 300, 560)}")

viewer.set_auto_crop_enabled(True)
app.processEvents()
QTest.qWait(100)
box = viewer._content_box_pt(0)
check("3.4 反色时内容框探测仍按原始像素工作（能框住黑块）",
      box is not None and box[0] < 30 and box[1] < 30 and box[2] > 100,
      f"内容框={None if box is None else tuple(round(v) for v in box)}")
viewer.set_auto_crop_enabled(False)
app.processEvents()

print()
print("=" * 78)
print("4. 后台预取也带反色（否则翻页会闪一下原色）")
print("=" * 78)
def wait_for(predicate, timeout_ms=5000, step=100):
    """轮询等待（不假设固定耗时：预取完成时间受机器负载影响）。"""
    waited = 0
    while waited < timeout_ms:
        if predicate():
            return True
        QTest.qWait(step)
        app.processEvents()
        waited += step
    return bool(predicate())


def prefetched_page_two():
    return [k for k in viewer.page_cache if k[1] == 1 and k[3] is False]


viewer.goto_page(1)
app.processEvents()
ok = wait_for(lambda: bool(prefetched_page_two()))
prefetched = [(k, viewer.page_cache[k]) for k in prefetched_page_two()]

check("4.1 预取的第 2 页进了缓存", ok and bool(prefetched),
      f"缓存条目={len(viewer.page_cache)}")
if prefetched:
    key, pm = prefetched[0]
    check("4.2 预取结果与当前反色状态一致（同样是黑底）",
          key[-1] is True and max(pixel(pm, 300, 560)) < 70,
          f"键里的反色标记={key[-1]} 纸面={pixel(pm, 300, 560)}")
    check("4.3 预取键包含反色标记（切换后不会命中旧配色）", key[-1] is True)

print()
print("=" * 78)
print("5. 主窗口集成：快捷键与命令面板")
print("=" * 78)
win = R.MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.show()
win.open_pdf(pdf, 1)
app.processEvents()
QTest.qWait(150)
before_state = win.viewer.invert_enabled
# 说明：Qt 只在"窗口处于激活状态"时派发 WindowShortcut 类快捷键，而离屏平台 +
# WA_DontShowOnScreen 的窗口永远不是 active（实测 isActiveWindow()=False，所有
# Ctrl 类快捷键都不派发）。所以这里直接驱动该 QShortcut 的 activated 信号，
# 验证的是"Ctrl+Shift+I 已注册且接到了反色切换"这两件事本身。
scs = [s for s in win.findChildren(QShortcut) if s.key().toString() == "Ctrl+Shift+I"]
check("5.1 Ctrl+Shift+I 已注册为窗口快捷键",
      len(scs) == 1 and scs[0].isEnabled()
      and scs[0].context() == Qt.WindowShortcut,
      f"命中 {len(scs)} 个")
if scs:
    scs[0].activated.emit()
    app.processEvents()
    QTest.qWait(80)
check("5.2 触发该快捷键确实切换反色", win.viewer.invert_enabled != before_state,
      f"{before_state} → {win.viewer.invert_enabled}")
check("5.3 状态栏提示只改显示、不影响截取",
      "截取输出仍是原样" in win.status.currentMessage(), win.status.currentMessage())

names = [c[0] for c in R.MainWindow.EXTRA_COMMANDS]
keys = [k for k, *_ in R.MainWindow.SKEY if isinstance(k, str)]
check("5.4 命令面板里有「反色显示」", "反色显示" in names, f"命令数={len(names)}")
check("5.5 快捷键表里有 Ctrl+Shift+I", "Ctrl+Shift+I" in keys)
win.close()
app.processEvents()

print()
print("=" * 78)
print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
print("=" * 78)
for name in FAIL:
    print("  FAILED:", name)

# 清理临时目录：Windows 上文档句柄没释放会让 rmtree 静默失败，所以先关文档再重试
for v in (viewer, win.viewer):
    try:
        if v.doc is not None:
            v.clear_pdf()
    except Exception:
        pass
for _ in range(5):
    shutil.rmtree(TMP, ignore_errors=True)
    if not os.path.exists(TMP):
        break
    time.sleep(0.2)
sys.exit(1 if FAIL else 0)
