"""面向"啃数学书"的一轮优化（P0~P3）回归测试。

P0 截取分辨率按原生像素封顶（扫描件不做插值放大）
P1 内容框抗噪 + 自适应背景阈值（让"去白边"对扫描件真正生效）
P2 后台预取相邻页（消除冷页等待）
P3 适合宽度 / 拖拽平移 / 双击放大

运行：python tests/test_math_reading.py    （Qt offscreen，无需显示器）
测试把 PDF 目录与状态目录指向包内临时目录，不触碰用户真实文件。
"""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
TMP = os.path.join(PKG, "_test_tmp_math")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
sys.path.insert(0, PKG)

import fitz
import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import reader as R

FAILS = []
OPEN_VIEWERS = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


QMessageBox.critical = staticmethod(lambda *a, **k: None)
app = QApplication.instance() or QApplication(sys.argv)


def make_scan_like(name, page_pt, img_px, text_block=True):
    """造一个"扫描件"：页面框与内嵌位图像素尺寸一致（真实扫描 PDF 的典型形态）。

    这样 300 DPI 就等于把原图插值放大 page_pt/img_px 倍。
    """
    path = os.path.join(TMP, name)
    doc = fitz.open()
    page = doc.new_page(width=page_pt[0], height=page_pt[1])
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, img_px[0], img_px[1]))
    pix.set_rect(pix.irect, (255, 255, 255))
    if text_block:
        # 画一些"文字行"和一条细边框，模拟扫描内容
        for i in range(20):
            y0 = int(img_px[1] * (0.12 + i * 0.03))
            pix.set_rect(fitz.IRect(int(img_px[0] * 0.1), y0,
                                    int(img_px[0] * 0.9), y0 + 8), (30, 30, 30))
        pix.set_rect(fitz.IRect(int(img_px[0] * 0.9), int(img_px[1] * 0.1),
                                int(img_px[0] * 0.9) + 2, int(img_px[1] * 0.9)), (40, 40, 40))
    page.insert_image(page.rect, pixmap=pix)
    doc.save(path)
    doc.close()
    return path


def make_gray_scan(name, size=(900, 1200), bg=(230, 230, 230)):
    """灰底扫描件：纸面不是纯白，固定 245 阈值会把整页判成内容。"""
    path = os.path.join(TMP, name)
    doc = fitz.open()
    page = doc.new_page(width=size[0], height=size[1])
    page.draw_rect(fitz.Rect(0, 0, size[0], size[1]), color=None,
                   fill=(bg[0] / 255, bg[1] / 255, bg[2] / 255))
    for i in range(12):
        page.insert_text((size[0] * 0.15, size[1] * (0.2 + i * 0.05)), "Theorem 3.4 proof line",
                         fontsize=11)
    doc.save(path)
    doc.close()
    return path


def make_speck_page(name, size=(1400, 1900)):
    """白底页面：正文内缩，四角放孤立墨点（模拟扫描噪点）。"""
    path = os.path.join(TMP, name)
    doc = fitz.open()
    page = doc.new_page(width=size[0], height=size[1])
    page.insert_text((size[0] * 0.25, size[1] * 0.5), "content block", fontsize=20)
    for (x, y) in ((2, 2), (size[0] - 3, 3), (3, size[1] - 3), (size[0] - 4, size[1] - 4)):
        page.draw_rect(fitz.Rect(x, y, x + 1, y + 1), color=None, fill=(0, 0, 0))
    doc.save(path)
    doc.close()
    return path


def raster_probe(viewer):
    stat = {"calls": 0}

    def wrapped(page_index, zoom):
        stat["calls"] += 1
        return original(page_index, zoom)

    original = viewer._render_raw_qimage
    viewer._render_raw_qimage = wrapped
    return stat


def open_viewer(path, size=(1000, 820)):
    v = R.PdfViewer(status_callback=lambda m: None)
    OPEN_VIEWERS.append(v)
    v.resize(*size)
    # 必须 show()：未布局的窗口里 QScrollArea 不会更新滚动条范围，
    # 会让"能否滚到页面底部"这类断言假失败（真实使用中窗口总是显示的）
    v.show()
    v.open_pdf(path, 1)
    app.processEvents()
    return v


print("=" * 80)
print("P0. 截取分辨率按原生像素封顶")
print("=" * 80)
# 模拟真实形态：3400x3400pt 的页面 + 3400x3400px 的内嵌图（原生 1.0 px/pt）
big_scan = make_scan_like("_t_bigscan.pdf", (3400, 3400), (3400, 3400))
v = open_viewer(big_scan)
native = v._native_px_per_pt(0)
scale = v._capture_scale(0)
check("P0.1 识别出原生分辨率 ≈1.0 px/pt", abs(native - 1.0) < 0.01, f"{native:.3f}")
check("P0.2 截取缩放被封顶（不再按 300 DPI 插值放大 4.17 倍）",
      abs(scale - R.CAPTURE_MIN_SCALE) < 1e-6, f"scale={scale:.3f}（300DPI 会是 4.167）")

target = R.CROP_HIGH_RES_DPI / R.PDF_BASE_DPI
full_native = (3400 * scale) * (3400 * scale)
full_300dpi = (3400 * target) * (3400 * target)
check("P0.3 整页输出从「超过上限」变成可行",
      full_300dpi > R.MAX_CAPTURE_PIXELS > full_native,
      f"原生 {full_native/1e6:.0f}MPx < 上限 {R.MAX_CAPTURE_PIXELS/1e6:.0f}MPx < 300DPI {full_300dpi/1e6:.0f}MPx")

v.set_view_mode(R.VIEW_FIT_PAGE)
app.processEvents()
band = 60  # 显示图像素高的窄条
part = v._crop_lossless_region(0, 100, 100 + band)
expected_w = int(round(3400 * scale))
check("P0.4 截取输出宽度 = 原生尺寸而非 300 DPI 尺寸",
      abs(part.width() - expected_w) <= 2 and part.width() < int(3400 * target),
      f"{part.width()} px（原生 {expected_w}，300DPI {int(3400*target)}）")

est = v._estimate_capture_pixels(0, 0, 0, band)
check("P0.5 尺寸估算与截取使用同一个缩放（不会误拦）",
      est < R.MAX_CAPTURE_PIXELS, f"估算 {est/1e6:.1f} MPx")

# 矢量页（无内嵌图）应保持 300 DPI 目标
vec = os.path.join(TMP, "_t_vector.pdf")
doc = fitz.open()
p = doc.new_page(width=595, height=842)
p.insert_text((60, 120), "vector page", fontsize=12)
doc.save(vec)
doc.close()
v_vec = open_viewer(vec)
check("P0.6 无内嵌图的矢量页仍按 300 DPI",
      abs(v_vec._capture_scale(0) - target) < 1e-6, f"{v_vec._capture_scale(0):.3f}")

print()
print("=" * 80)
print("P1. 内容框抗噪 + 自适应背景阈值")
print("=" * 80)
gray_scan = make_gray_scan("_t_gray.pdf")
vg = open_viewer(gray_scan)
vg.set_auto_crop_enabled(True)
app.processEvents()
box = vg._content_box_pt(0)
page_rect = vg.doc.load_page(0).rect
thr = vg._content_threshold(0)
check("P1.1 灰底页识别出偏灰的纸面", thr < 240, f"阈值={thr}（固定值 245）")
if box is None:
    check("P1.2 灰底页得到有效内容框", False, "box=None")
else:
    check("P1.2 内容框明显小于整页（固定 245 时会是整页）",
          (box[2] - box[0]) < page_rect.width * 0.8 and (box[3] - box[1]) < page_rect.height * 0.8,
          f"内容框 {box[2]-box[0]:.0f}x{box[3]-box[1]:.0f} / 页面 {page_rect.width:.0f}x{page_rect.height:.0f}")

speck = make_speck_page("_t_speck.pdf")
vs = open_viewer(speck)
vs.set_auto_crop_enabled(True)
app.processEvents()
box_s = vs._content_box_pt(0)
probe = vs._render_raw_qimage(0, 1.0)
img = probe.convertToFormat(QImage.Format_RGB888)
arr = R.qimage_to_rgb_array(img)
raw_no_filter = R.find_content_bbox_arr(arr, 245, min_fraction=0.0)
raw_filtered = R.find_content_bbox_arr(arr, 245, min_fraction=R.CONTENT_NOISE_MIN_FRACTION)
if raw_no_filter is None or raw_filtered is None or box_s is None:
    check("P1.3 抗噪内容框有效", False, f"{raw_no_filter} / {raw_filtered} / {box_s}")
else:
    w_px, h_px = arr.shape[1], arr.shape[0]
    area_all = (raw_no_filter[2] - raw_no_filter[0]) * (raw_no_filter[3] - raw_no_filter[1])
    area_flt = (raw_filtered[2] - raw_filtered[0]) * (raw_filtered[3] - raw_filtered[1])
    check("P1.3 孤立墨点不再把内容框撑满整页",
          area_flt < area_all * 0.6,
          f"不过滤={raw_no_filter} 过滤后={raw_filtered}（页面 {w_px}x{h_px}）")
    check("P1.4 抗噪不会误伤正文（仍框住文字行）",
          raw_filtered[2] - raw_filtered[0] > 80 and raw_filtered[1] <= h_px * 0.5 <= raw_filtered[3],
          f"过滤后框={raw_filtered}（文字行 x≈350..470, y≈940）")

print()
print("=" * 80)
print("P2. 后台预取相邻页")
print("=" * 80)
multi = os.path.join(TMP, "_t_multi.pdf")
doc = fitz.open()
for i in range(8):
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((60, 120), f"page {i+1}", fontsize=14)
doc.save(multi)
doc.close()
vp = open_viewer(multi)
vp.set_view_mode(R.VIEW_FIT_PAGE)
vp.goto_page(3)
app.processEvents()
vp._prefetch_pool.waitForDone(5000)
app.processEvents()
cached_pages = {k[1] for k in vp.page_cache}
check("P2.1 相邻页进入缓存（后台预取生效）",
      {vp.current_page_index - 1, vp.current_page_index + 1}.issubset(cached_pages),
      f"缓存页={sorted(cached_pages)}")

stat = raster_probe(vp)
vp.goto_page(4)          # 下一页已预取
app.processEvents()
check("P2.2 翻到已预取的页无需栅格化", stat["calls"] == 0, f"calls={stat['calls']}")

vp._clear_page_cache()
vp._prefetch_pool.waitForDone(5000)
app.processEvents()
QTest.qWait(50)
check("P2.3 清空缓存后，在途预取结果不会回填",
      len(vp.page_cache) <= 2, f"缓存 {len(vp.page_cache)} 项")

print()
print("=" * 80)
print("P3. 适合宽度 / 拖拽平移 / 双击放大")
print("=" * 80)
# P3 用**小页面**：大页面放大 4 倍会渲染出上百 MPx（实测峰值 1.8GB），
# 在内存紧张的机器上会卡死，而本组检查只需要"内容超出视口"这一个前提
zoom_pdf = os.path.join(TMP, "_t_zoompdf.pdf")
doc = fitz.open()
for i in range(3):
    pg = doc.new_page(width=400, height=600)
    pg.insert_text((40, 80), f"page {i+1}", fontsize=12)
doc.save(zoom_pdf)
doc.close()
vw = open_viewer(zoom_pdf)
vw.fit_width()
app.processEvents()
pix = vw.image_label.pixmap()
dpr = pix.devicePixelRatio() or 1.0
lw = pix.width() / dpr
vp_size = vw.scroll.viewport().size()
check("P3.1 适合宽度：渲染宽度铺满视口",
      abs(lw - (vp_size.width() - 20)) <= 20 and lw >= (vp_size.width() - 20) * 0.97,
      f"图宽 {lw:.0f} / 视口 {vp_size.width()}（容差=一个滚动条宽度："
      f"先按无滚动条算宽度，垂直滚动条出现后视口会窄一点）")
check("P3.2 状态栏显示适合宽度", "适合宽度" in vw.status_text(), vw.status_text())

vw.zoom_to_100()
app.processEvents()
check("P3.3 Ctrl+2 原始大小", vw.view_mode == R.VIEW_MANUAL and abs(vw.zoom - 1.0) < 1e-9,
      f"mode={vw.view_mode} zoom={vw.zoom}")

vw.fit_page()
app.processEvents()
check("P3.4 Ctrl+0 适合页面", vw.view_mode == R.VIEW_FIT_PAGE and "适应页面" in vw.status_text(),
      vw.status_text())

# P3.8/P3.9 回归：页面大于视口时必须能滚到页面底部
# 旧实现把标签最小尺寸写死 800x600，widgetResizable 会把标签压回视口大小，
# 于是高于视口的页面被居中裁掉上下两端，且滚动条范围是 0（滚不到、看不全）。
vw.fit_width()
app.processEvents()
QTest.qWait(120)          # 等布局落定
# 离屏平台下滚动条范围有时要等到下一次渲染才更新（真实平台即时更新），
# 这里显式重渲一次，让断言不依赖平台时机
vw.render_current_page()
app.processEvents()
QTest.qWait(120)
app.processEvents()
pix_w = vw.image_label.pixmap()
dpr_w = pix_w.devicePixelRatio() or 1.0
pm_h = pix_w.height() / dpr_w
viewport_h = vw.scroll.viewport().height()
vbar_w = vw.scroll.verticalScrollBar()
check("P3.8 标签长到页面尺寸（否则整页滚不到）",
      vw.image_label.height() + 1 >= pm_h,
      f"标签高 {vw.image_label.height()} / 页面高 {pm_h:.0f} / 视口 {viewport_h}")
check("P3.9 可滚动范围覆盖整页",
      vbar_w.maximum() + 2 >= pm_h - viewport_h,
      f"滚动范围 0~{vbar_w.maximum()} / 需要 {pm_h - viewport_h:.0f}")
vbar_w.setValue(vbar_w.maximum())
app.processEvents()
QTest.qWait(80)
app.processEvents()
origin_w = vw.crop_overlay._label_page_origin()
bottom_visible = bool(origin_w) and origin_w[1] + origin_w[3] <= vw.crop_overlay.height() + 2
check("P3.10 滚到底后页面底部可达", bottom_visible,
      f"原点 y0={origin_w[1]:.0f} 页高={origin_w[3]:.0f} 覆盖层高={vw.crop_overlay.height()}"
      if origin_w else "取不到页面原点")

# P3.11 非法标记不能让绘制崩溃（y=None / 页号不符 / 标记本身为空）
ov_w = vw.crop_overlay
guard_ok = (ov_w._visible_marker_y(None) is None
            and ov_w._visible_marker_y((vw.current_page_index, None)) is None
            and ov_w._visible_marker_y((vw.current_page_index + 5, 10)) is None
            and ov_w._visible_marker_y((vw.current_page_index, 10)) == 10)
ov_w.top_marker = (vw.current_page_index, None)   # 脏数据：曾经会让 paintEvent 抛 TypeError
ov_w.bottom_marker = ("bad", "data")
ov_w.preview_y = 20
painted = ov_w.grab()
ov_w.top_marker = ov_w.bottom_marker = None
ov_w.preview_y = None
check("P3.11 非法标记下绘制不崩溃", guard_ok and not painted.isNull(),
      f"guard={guard_ok} 绘制结果={painted.width()}x{painted.height()}")

# 双击放大（先放大到内容超出视口，才可能滚动）
vw._set_zoom(4.0)
app.processEvents()
zoom_before = vw.zoom
vp_widget = vw.scroll.viewport()
center = QPointF(vp_widget.width() / 2, vp_widget.height() / 2)
dbl = QMouseEvent(QEvent.MouseButtonDblClick, center, vp_widget.mapToGlobal(center.toPoint()),
                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
app.sendEvent(vp_widget, dbl)
app.processEvents()
check("P3.5 双击在放大状态下回到适合宽度", vw.view_mode == R.VIEW_FIT_WIDTH,
      f"mode={vw.view_mode} zoom 前={zoom_before:.2f} 后={vw.zoom:.2f}")

# 拖拽平移
vw._set_zoom(4.0)
app.processEvents()
hbar = vw.scroll.horizontalScrollBar()
vbar = vw.scroll.verticalScrollBar()
can_pan = hbar.maximum() > hbar.minimum() or vbar.maximum() > vbar.minimum()
check("P3.6 放大后内容超出视口（可平移）", can_pan,
      f"hbar {hbar.minimum()}~{hbar.maximum()} vbar {vbar.minimum()}~{vbar.maximum()}")
if can_pan:
    hbar.setValue(min(hbar.maximum(), 100))
    vbar.setValue(min(vbar.maximum(), 100))
    start = QPoint(200, 150)
    press = QMouseEvent(QEvent.MouseButtonPress, QPointF(start), vp_widget.mapToGlobal(start),
                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    app.sendEvent(vp_widget, press)
    move = QMouseEvent(QEvent.MouseMove, QPointF(start.x() - 60, start.y() - 40),
                       vp_widget.mapToGlobal(QPoint(start.x() - 60, start.y() - 40)),
                       Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
    app.sendEvent(vp_widget, move)
    release = QMouseEvent(QEvent.MouseButtonRelease, QPointF(start.x() - 60, start.y() - 40),
                          vp_widget.mapToGlobal(QPoint(start.x() - 60, start.y() - 40)),
                          Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    app.sendEvent(vp_widget, release)
    app.processEvents()
    check("P3.7 拖拽平移改变了滚动位置",
          hbar.value() > 100 or vbar.value() > 100,
          f"hbar={hbar.value()} vbar={vbar.value()}")
else:
    check("P3.7 拖拽平移改变了滚动位置", False, "内容未超出视口")

for v_ in OPEN_VIEWERS:
    try:
        if v_.doc is not None:
            v_.doc.close()
            v_.doc = None
    except Exception:  # noqa: BLE001
        pass
shutil.rmtree(TMP, ignore_errors=True)
if os.path.isdir(TMP):
    print(f"[warn] 临时目录未完全清理：{TMP}")

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1)
print("ALL CHECKS PASSED")
