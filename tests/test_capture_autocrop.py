"""reader.py 优化后的回归测试。

覆盖：
  1. 跨页截取 + 自动裁剪不再崩溃（旧实现把 QPixmap 当 QImage 用）
  2. clip 局部渲染与"整页渲染再裁剪"逐像素等价
  3. 内容框检测：不降采样时与旧实现完全一致；降采样误差 ≤4px
  4. 适应页面缩放：一次渲染即精确适配视口（旧实现每页 3 次栅格化）
  5. 页面缓存按字节限制；截取不再把 300 DPI 大位图塞进缓存
  6. 缩放防抖 / 阅读进度落盘防抖
  7. 截取范围上限（页数 / 输出像素）
  8. INITIAL_PAGE 配置项真正生效

运行：python tests/test_capture_autocrop.py   （使用 Qt offscreen，无需显示器）
测试把 PDF 目录与状态目录都指向包内临时目录，不触碰用户真实进度文件。
"""
import json
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
TMP = os.path.join(PKG, "_test_tmp")
shutil.rmtree(TMP, ignore_errors=True)   # 每次从干净目录开始，避免残留状态文件
os.makedirs(TMP, exist_ok=True)
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
sys.path.insert(0, PKG)

import fitz
import numpy as np
from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import reader as R

FAILS = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(name)


QMessageBox.critical = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
app = QApplication.instance() or QApplication(sys.argv)

OPEN_VIEWERS = []          # 结束时统一关闭文档句柄，否则 Windows 上临时目录删不掉


def make_pdf(name, pages=4, size=(595, 842), margin=60):
    """生成带明显白边的多页 PDF（白边让自动裁剪有实际效果）。"""
    path = os.path.join(TMP, name)
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((margin, margin + 20), f"page {i + 1} header", fontsize=14)
        for k in range(20):
            page.insert_text((margin, margin + 50 + k * 24),
                             "The quick brown fox jumps over the lazy dog 0123456789", fontsize=9)
        page.draw_rect(fitz.Rect(margin, size[1] - margin - 60, size[0] - margin, size[1] - margin))
    doc.save(path)
    doc.close()
    return path


def to_rgb888(img: QImage) -> QImage:
    return img if img.format() == QImage.Format_RGB888 else img.convertToFormat(QImage.Format_RGB888)


def images_equal_within_offset(a: QImage, b: QImage, max_off=2):
    """b 允许相对 a 有整数行偏移（取整差异），返回 (是否逐字节相等, 偏移)。"""
    a, b = to_rgb888(a), to_rgb888(b)
    if a.width() != b.width():
        return False, None
    ba = bytes(a.constBits())
    bb = bytes(b.constBits())
    row = a.width() * 3
    for off in range(-max_off, max_off + 1):
        if off == 0:
            seg_a = ba[: b.height() * row]
        elif off > 0:
            seg_a = ba[off * row: (off + b.height()) * row]
        else:
            seg_a = ba[: (b.height() + off) * row]
        seg_b = bb[: b.height() * row] if off <= 0 else bb[: (b.height() - off) * row]
        if len(seg_a) and len(seg_a) == len(seg_b) and seg_a == seg_b:
            return True, off
    return False, None


def raster_probe(viewer):
    """统计栅格化调用次数与总像素（同步路径）。

    后台预取也会调用 _render_raw_qimage，会把计数算脏；用 no_prefetch() 包住
    需要精确计数的段落。
    """
    stat = {"calls": 0, "px": 0}
    original = viewer._render_raw_qimage

    def wrapped(page_index, zoom):
        stat["calls"] += 1
        img = original(page_index, zoom)
        if img is not None and not img.isNull():
            stat["px"] += img.width() * img.height()
        return img

    viewer._render_raw_qimage = wrapped
    return stat


class no_prefetch:
    """测量期间关闭后台预取（只关新任务的调度，并等在途任务结束）。"""

    def __init__(self, viewer):
        self.viewer = viewer

    def __enter__(self):
        self._old = R.PREFETCH_PAGES
        R.PREFETCH_PAGES = 0
        self.viewer._prefetch_pool.waitForDone(5000)
        app.processEvents()
        return self

    def __exit__(self, *exc):
        R.PREFETCH_PAGES = self._old
        return False


pdf_path = make_pdf("_t_main.pdf", pages=6)
many_path = make_pdf("_t_many.pdf", pages=60)

print("=" * 78)
print("1. 跨页截取 + 自动裁剪（旧实现崩溃点）")
print("=" * 78)
viewer = R.PdfViewer(status_callback=lambda m: None)
OPEN_VIEWERS.append(viewer)
viewer.resize(900, 800)
viewer.open_pdf(pdf_path, 1)
app.processEvents()
viewer.set_fit_page_mode(True)
viewer.set_auto_crop_enabled(True)   # Ctrl+Shift+R 打开自动裁剪
viewer.goto_page(1)
app.processEvents()
check("1.1 只显示过第 1 页（后台预取可能已顺带探测下一页）",
      0 in viewer._content_box_cache and all(p < 3 for p in viewer._content_box_cache),
      f"已探测页={sorted(viewer._content_box_cache)}")

viewer.set_crop_marks((0, 100), (3, 400))   # 第 2、3 页从未显示过
try:
    viewer.copy_crop_to_clipboard()
    crashed = False
except Exception as e:  # noqa: BLE001
    crashed = True
    check("1.2 跨页截取不崩溃", False, f"{type(e).__name__}: {e}")
if not crashed:
    check("1.2 跨页截取不崩溃（旧实现 AttributeError）", True)
    check("1.3 结果已进剪贴板", not QGuiApplication.clipboard().pixmap().isNull())
    check("1.4 未显示过的中转页也能算出内容框", 1 in viewer._content_box_cache and 2 in viewer._content_box_cache)

print()
print("=" * 78)
print("2. clip 局部渲染 vs 整页渲染后裁剪（自动裁剪关闭时的默认路径）")
print("=" * 78)
viewer.set_auto_crop_enabled(False)
viewer.goto_page(1)
app.processEvents()
z = viewer._get_effective_zoom(0)


def reference_rows(page_index, y1, y2):
    """旧语义参考：整页 300 DPI 渲染 → 按行截取。"""
    scale = R.CROP_HIGH_RES_DPI / R.PDF_BASE_DPI
    zz = viewer._get_effective_zoom(page_index)
    page = viewer.doc.load_page(page_index)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
    y1_px = int(round(y1 * scale / zz))
    y2_px = img.height() if y2 is None else int(round(y2 * scale / zz))
    y2_px = max(y1_px, min(y2_px, img.height()))
    return img.copy(QRect(0, y1_px, img.width(), y2_px - y1_px))


for tag, (pa, y1), (pb, y2) in [("单页条带", (0, 80), (0, 300)), ("跨页 (0→2)", (0, 80), (2, 300))]:
    if pa == pb:
        new = viewer._crop_lossless_region(pa, y1, y2).toImage()
        ref = reference_rows(pa, y1, y2)
    else:
        new = viewer._crop_lossless_region(pa, y1, None).toImage()
        ref = reference_rows(pa, y1, None)
    ok, off = images_equal_within_offset(ref, new)
    check(f"2.1 {tag}：与整页裁剪逐像素一致", ok, f"行偏移={off} {new.width()}x{new.height()}")

print()
print("=" * 78)
print("3. 内容框检测：等价性与降采样误差")
print("=" * 78)


def bbox_reference(arr, white_threshold=245):
    """旧实现（float32 灰度 + np.where）作为对照。"""
    gray = R.rgb_to_gray(arr.astype(np.float32))
    mask = gray < white_threshold
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


probe = viewer._render_raw_qimage(0, 1.2)
img888 = probe.convertToFormat(QImage.Format_RGB888)
arr = R.qimage_to_rgb_array(img888)
exact = R.find_content_bbox_arr(arr, white_threshold=245)
ref_box = bbox_reference(arr, white_threshold=245)
check("3.1 分块+投影实现与旧实现完全一致", exact == ref_box, f"{exact} vs {ref_box}")

qimg_box = R.content_bbox_from_qimage(probe, 245)
check("3.2 content_bbox_from_qimage 与数组路径一致", qimg_box == exact, f"{qimg_box} vs {exact}")

# 探测框（低倍探测图换算到页面点）应与 300 DPI 精确框基本吻合
viewer.set_auto_crop_enabled(True)
probe_box_pt = viewer._content_box_pt(0)
scale = R.CROP_HIGH_RES_DPI / R.PDF_BASE_DPI
hi = viewer._render_raw_qimage(0, scale)
hi_box_px = R.content_bbox_from_qimage(hi, 245)
hi_box_pt = tuple(v / scale for v in hi_box_px)
probe_zoom = max(R.CONTENT_PROBE_MIN_ZOOM, min(1.0, (R.CONTENT_PROBE_TARGET_PX /
                (viewer.doc.load_page(0).rect.width * viewer.doc.load_page(0).rect.height)) ** 0.5))
tolerance = 2.0 / probe_zoom + 1.0
worst = max(abs(a - b) for a, b in zip(probe_box_pt, hi_box_pt))
check("3.3 低倍探测框与 300 DPI 精确框吻合", worst <= tolerance,
      f"最大差 {worst:.2f}pt ≤ {tolerance:.2f}pt 探测框={tuple(round(v, 1) for v in probe_box_pt)} "
      f"精确框={tuple(round(v, 1) for v in hi_box_pt)}")
viewer.set_auto_crop_enabled(False)

print()
print("=" * 78)
print("4. 适应页面：一次渲染即精确适配，内容框只探测一次")
print("=" * 78)
for autocrop in (False, True):
    viewer.set_auto_crop_enabled(autocrop)
    with no_prefetch(viewer):
        stat = raster_probe(viewer)
        viewer.goto_page(5)          # 冷页：内容框未知 + 无缓存位图
        app.processEvents()
        first_calls = stat["calls"]
        check(f"4.1 autocrop={autocrop} 冷页栅格化 ≤2 次（旧实现 3 次整页）", first_calls <= 2,
              f"calls={first_calls} px={stat['px'] / 1e6:.2f}M")
        stat2 = raster_probe(viewer)
        viewer.goto_page(6)          # 冷页
        app.processEvents()
        cold_calls = stat2["calls"]
        viewer.goto_page(5)          # 回到 4.1 已渲染过的页 → 应命中缓存，不新增栅格化
        app.processEvents()
        check(f"4.2 autocrop={autocrop} 回到已渲染页不额外栅格化",
              cold_calls <= 2 and stat2["calls"] == cold_calls,
              f"冷页={cold_calls} 回访后={stat2['calls']}")
    pix = viewer.image_label.pixmap()
    dpr = pix.devicePixelRatio() or 1.0
    lw, lh = pix.width() / dpr, pix.height() / dpr
    vp = viewer.scroll.viewport().size()
    check(f"4.3 autocrop={autocrop} 显示尺寸适配视口",
          lw <= vp.width() - 20 + 2 and lh <= vp.height() - 20 + 2,
          f"{lw:.0f}x{lh:.0f} 视口 {vp.width()}x{vp.height()}")
    check(f"4.4 autocrop={autocrop} 没有明显浪费空间",
          lw >= (vp.width() - 20) * 0.55 or lh >= (vp.height() - 20) * 0.55,
          f"{lw:.0f}x{lh:.0f}")

print()
print("=" * 78)
print("5. 页面缓存：按字节限制，截取不污染缓存")
print("=" * 78)
original_budget = R.PAGE_CACHE_MAX_BYTES
R.PAGE_CACHE_MAX_BYTES = 6 * 1024 * 1024
try:
    viewer.set_auto_crop_enabled(False)
    viewer.set_fit_page_mode(False)
    viewer.zoom = 1.0
    for p in range(1, 7):
        viewer.goto_page(p)
        viewer.zoom = 0.9 + 0.1 * p
        viewer.render_current_page()
    check("5.1 缓存字节数不超过预算", viewer._page_cache_bytes <= R.PAGE_CACHE_MAX_BYTES,
          f"{viewer._page_cache_bytes / 1e6:.1f}MB / {R.PAGE_CACHE_MAX_BYTES / 1e6:.1f}MB")
    check("5.2 字节计数与实际条目吻合",
          viewer._page_cache_bytes == sum(viewer._pixmap_bytes(v) for v in viewer.page_cache.values()))
finally:
    R.PAGE_CACHE_MAX_BYTES = original_budget

viewer.set_auto_crop_enabled(True)
viewer.set_fit_page_mode(True)
viewer.goto_page(0 + 1)
app.processEvents()
viewer._clear_page_cache()
viewer.set_crop_marks((0, 100), (0, 500))
viewer.copy_crop_to_clipboard()
check("5.3 截取不写入页面缓存（旧实现会留下 300 DPI 整页位图）",
      len(viewer.page_cache) == 0 and viewer._page_cache_bytes == 0,
      f"{len(viewer.page_cache)} 项")

print()
print("=" * 78)
print("6. 防抖：缩放与进度落盘")
print("=" * 78)
viewer.set_auto_crop_enabled(False)
viewer.set_fit_page_mode(False)
viewer.zoom = 1.0
viewer.render_current_page()
with no_prefetch(viewer):
    stat = raster_probe(viewer)
    for _ in range(10):
        viewer.zoom_in(debounce=True)
    check("6.1 滚轮连续缩放 10 档期间不渲染", stat["calls"] == 0, f"calls={stat['calls']}")
    QTest.qWait(R.RENDER_DEBOUNCE_MS + 120)
    check("6.2 防抖到期后只渲染 1 次", stat["calls"] == 1, f"calls={stat['calls']}")
check("6.3 缩放数值已累加", abs(viewer.zoom - 2.0) < 1e-6, str(viewer.zoom))

hist_file = os.path.join(TMP, R.HISTORY_FILE_NAME)
for f in (hist_file, os.path.join(TMP, R.RECENT_FILE_NAME)):
    if os.path.exists(f):
        os.remove(f)

win = R.MainWindow()
OPEN_VIEWERS.append(win.viewer)
win.status.showMessage = lambda *a, **k: None
app.processEvents()
win.on_page_changed(pdf_path, 5)
check("6.4 翻页后不立即写盘", not os.path.exists(hist_file))
win._flush_state()
check("6.5 防抖落盘后文件存在", os.path.exists(hist_file))
if os.path.exists(hist_file):
    data = json.load(open(hist_file, encoding="utf-8"))
    rec = data.get("files", {}).get(os.path.abspath(pdf_path), {})
    check("6.6 进度内容正确", rec.get("last_page") == 5, str(rec))

print()
print("=" * 78)
print("7. 截取范围上限")
print("=" * 78)
batch = R.PdfViewer(status_callback=lambda m: None)
OPEN_VIEWERS.append(batch)
batch.resize(900, 800)
batch.open_pdf(many_path, 1)
app.processEvents()
messages = []
batch.status_callback = messages.append
batch.set_crop_marks((0, 100), (R.MAX_CAPTURE_PAGES + 5, 300))
batch.copy_crop_to_clipboard()
check("7.1 跨页过多被拦下", any("跨页过多" in m for m in messages), str(messages[-1:] if messages else []))

original_max_px = R.MAX_CAPTURE_PIXELS
R.MAX_CAPTURE_PIXELS = 1000
try:
    messages.clear()
    batch.set_crop_marks((0, 100), (0, 300))
    batch.copy_crop_to_clipboard()
    check("7.2 输出像素超限被拦下", any("范围过大" in m for m in messages), str(messages[-1:] if messages else []))
finally:
    R.MAX_CAPTURE_PIXELS = original_max_px

print()
print("=" * 78)
print("8. 配置项 INITIAL_PAGE 生效")
print("=" * 78)
original_initial = R.INITIAL_PAGE
try:
    R.INITIAL_PAGE = 4
    fresh = R.MainWindow()          # 构造时自动打开的页会进内存历史
    OPEN_VIEWERS.append(fresh.viewer)
    fresh.status.showMessage = lambda *a, **k: None
    app.processEvents()
    target = make_pdf("_t_initial.pdf")   # 构造之后才创建 → 历史里没有记录
    fresh.open_pdf(target)
    check("8.1 无历史时使用 INITIAL_PAGE", fresh.viewer.current_page_1_based() == 4,
          f"第 {fresh.viewer.current_page_1_based()} 页")
    fresh.close()
finally:
    R.INITIAL_PAGE = original_initial

win.close()
if "fresh" in dir():
    fresh.close()

# 关闭所有文档句柄后再删临时目录（Windows 上被占用的文件删不掉）
for v in OPEN_VIEWERS:
    try:
        if v.doc is not None:
            v.doc.close()
            v.doc = None
    except Exception:  # noqa: BLE001
        pass
QApplication.clipboard().clear()
shutil.rmtree(TMP, ignore_errors=True)
if os.path.isdir(TMP):
    print(f"[warn] 临时目录未完全清理：{TMP}")

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1)
print("ALL CHECKS PASSED")
