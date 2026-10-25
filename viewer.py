"""阅读区：页面渲染、缩放与平移、内容框裁剪、反色、截取框选、后台预取。"""

from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
import math
import os
import threading

from app_config import (
    AUTO_CROP_DEFAULT,
    AUTO_CROP_MIN_SIZE,
    AUTO_CROP_WHITE_THRESHOLD,
    CAPTURE_MIN_SCALE,
    CONTENT_NOISE_MIN_FRACTION,
    CONTENT_PROBE_MIN_ZOOM,
    CONTENT_PROBE_TARGET_PX,
    CROP_HIGH_RES_DPI,
    FIT_WIDTH_MAX_ZOOM,
    INVERT_ENABLED,
    MAX_CAPTURE_PAGES,
    MAX_CAPTURE_PIXELS,
    MAX_TRIM_PIXELS,
    PAGE_CACHE_MAX,
    PAGE_CACHE_MAX_BYTES,
    PDF_BASE_DPI,
    PREFETCH_PAGES,
    RENDER_DEBOUNCE_MS,
    SS_SMALL_ZOOM,
    VIEW_FIT_PAGE,
    VIEW_FIT_WIDTH,
    VIEW_MANUAL,
    log,
)
from imaging import (
    content_bbox_from_qimage,
    content_box_and_threshold_from_qimage,
    invert_qimage,
    safe_crop_box,
)

import fitz  # PyMuPDF
from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRect,
    QRunnable,
    QThreadPool,
    QTimer,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class CropOverlay(QWidget):
    crop_ready = Signal()

    COVER_COLOR = QColor(0, 0, 0, 255)

    def __init__(self, viewer, parent=None):
        super().__init__(parent)
        self.viewer = viewer
        self.active = False
        self.preview_y: Optional[int] = None
        self.top_marker: Optional[Tuple[int, int]] = None
        self.bottom_marker: Optional[Tuple[int, int]] = None
        # -- 遮挡模式 --
        self.cover_mode = False
        self.cover_y: Optional[int] = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setStyleSheet("background: transparent;")
        self.hide()

    def start(self):
        self.active = True
        self.preview_y = None
        self.raise_()
        self.show()
        self.setFocus()
        self.update()

    def stop(self):
        self.active = False
        self.preview_y = None
        self.cover_mode = False
        self.cover_y = None
        self.hide()
        self.update()

    def reset_marks(self):
        self.preview_y = None
        self.top_marker = None
        self.bottom_marker = None
        self.cover_y = None
        self.update()

    def start_cover(self):
        self.cover_mode = True
        self.top_marker = None
        self.bottom_marker = None
        self.start()

    def stop_cover(self):
        self.cover_mode = False
        self.cover_y = None
        self.stop()

    def _label_page_origin(self):
        """返回页面像素图左上角在本覆盖层坐标系中的位置。

        使用逻辑尺寸（physical/dpr），并考虑标签在滚动区中的偏移与居中。

        旧写法 `label.mapTo(self, ...)` 里的 self（覆盖层）并不是标签的祖先——
        标签的父级是滚动区视口、覆盖层也是视口的子控件，二者是兄弟关系。
        Qt 在这种情况下会打印 "QWidget::mapTo(): parent must be in parent hierarchy"
        并返回错误坐标（实测 441 而非 236），于是分割线画偏、滚动后记录的位置也错。
        改成先映射到共同祖先（视口）再做差。基准算法现在放在 viewer 里，
        与选字层共用一份（避免两处各算一遍、迟早不一致）。
        """
        info = self.viewer.display_pixmap_rect_in_viewport()
        if info is None:
            return None
        x0, y0, pm_w, pm_h = info
        vp_origin = self.mapTo(self.viewer.scroll.viewport(), QPoint(0, 0))
        return x0 - vp_origin.x(), y0 - vp_origin.y(), pm_w, pm_h

    def _map_to_page_y(self, pos):
        # 水平截取线只关心鼠标的纵向位置落在页面范围内。
        # 不校验 x：页面居中时两侧有留白，线不应因此消失。
        info = self._label_page_origin()
        if info is None:
            return None
        _, y0, _, pm_h = info
        y = pos.y() - y0
        if y < 0 or y >= pm_h:
            return None
        return y

    def mouseMoveEvent(self, event):
        if self.active:
            old_y = self.preview_y
            self.preview_y = self._map_to_page_y(event.position().toPoint())
            self._update_line_bands(old_y, self.preview_y)

    def _update_line_bands(self, *ys):
        """只重绘预览线附近的细条，避免鼠标每动一像素就重绘整个覆盖层。"""
        info = self._label_page_origin()
        if info is None:
            self.update()
            return
        _, y0, _, _ = info
        dirty = QRect()
        for y in ys:
            if y is None:
                continue
            band = QRect(0, int(y0 + y) - 2, self.width(), 5)
            dirty = band if dirty.isNull() else dirty.united(band)
        if dirty.isNull():
            self.update()
        else:
            self.update(dirty)

    def mousePressEvent(self, event):
        if self.active:
            self.setFocus()
            self.preview_y = self._map_to_page_y(event.position().toPoint())
            self.update()

    def handle_enter(self) -> bool:
        """回车确认当前定位（截取模式的起始/结束线，遮挡模式的遮挡线）。

        独立成方法是为了让主窗口的回车快捷方式也能调用：跳页对话框关闭后
        覆盖层可能丢掉键盘焦点，此时回车事件根本到不了它的 keyPressEvent，
        用户看到的就是"截取失效"。返回是否处理了这次回车。
        """
        if not self.active:
            return False
        if self.cover_mode:
            if self.cover_y is not None:
                self.cover_y = None
            elif self.preview_y is not None:
                self.cover_y = self.preview_y
            self.update()
            return True
        if self.preview_y is None:
            # 不静默失败：明确告诉用户要先定位（换页后预览线会被清空）
            if self.viewer.status_callback:
                self.viewer.status_callback("请先把鼠标移到页面上定位分割线，再按回车确认")
            return True
        page_index = self.viewer.current_page_index
        if self.top_marker is None:
            self.top_marker = (page_index, self.preview_y)
            self.viewer.set_crop_marks(self.top_marker, None)
        else:
            self.bottom_marker = (page_index, self.preview_y)
            self.viewer.set_crop_marks(self.top_marker, self.bottom_marker)
            self.crop_ready.emit()
        self.update()
        return True

    def keyPressEvent(self, event):
        if not self.active:
            return super().keyPressEvent(event)

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.handle_enter()
        elif event.key() == Qt.Key_Escape:
            if self.cover_mode:
                self.stop_cover()
                self.viewer.set_cover_mode(False)
                return
            if self.bottom_marker is not None:
                self.bottom_marker = None
                self.viewer.set_crop_marks(self.top_marker, None)
                self.update()
            elif self.top_marker is not None:
                self.top_marker = None
                self.viewer.set_crop_marks(None, None)
                self.update()
            else:
                # 截取模式为循环模式：Esc 只撤销分割线，不退出模式；
                # 退出请再次触发“截取模式”指令（Ctrl+S）。
                if self.viewer.status_callback:
                    self.viewer.status_callback(
                        "截取模式进行中：按 Ctrl+S 退出（Esc 仅撤销分割线）"
                    )
        else:
            super().keyPressEvent(event)

    def _visible_marker_y(self, marker) -> Optional[float]:
        """标记在本页可见时返回它的页面内纵坐标，否则 None。

        历史上 paintEvent 直接解包 (page, y) 就算 y，一旦拿到 y=None 的脏数据
        会在绘制里抛 TypeError（Qt 覆盖方法里抛异常不会中断程序，但会刷一堆
        "active painter" 报错并让覆盖层画不出来），所以这里统一做防御。
        """
        if not marker or len(marker) != 2:
            return None
        page_index, y = marker
        if y is None or page_index != self.viewer.current_page_index:
            return None
        return y

    def paintEvent(self, event):
        if not self.active:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        info = self._label_page_origin()
        if info is None:
            return
        x0, y0, pm_w, pm_h = info

        if self.preview_y is not None:
            # 预览线横贯整个视口，始终跟随鼠标的纵向位置
            painter.setPen(QPen(QColor(0, 255, 0, 200), 2, Qt.DashLine))
            painter.drawLine(0, y0 + self.preview_y, self.width(), y0 + self.preview_y)

        top_y = self._visible_marker_y(self.top_marker)
        if top_y is not None:
            painter.setPen(QPen(QColor(255, 0, 0, 220), 2))
            painter.drawLine(x0, y0 + top_y, x0 + pm_w, y0 + top_y)

        bottom_y = self._visible_marker_y(self.bottom_marker)
        if bottom_y is not None:
            painter.setPen(QPen(QColor(0, 0, 255, 220), 2))
            painter.drawLine(x0, y0 + bottom_y, x0 + pm_w, y0 + bottom_y)

        # ----- 遮挡区域 -----
        if self.cover_y is not None:
            painter.setPen(Qt.NoPen)
            # 反色后页面是黑底，纯黑遮挡块会"隐身"——此时改用浅色块，让遮挡看得出来
            painter.setBrush(QColor(255, 255, 255, 255) if self.viewer.invert_enabled
                             else self.COVER_COLOR)
            painter.drawRect(0, y0 + self.cover_y, self.width(), self.height() - y0 - self.cover_y)


class _PrefetchTask(QRunnable):
    """后台预取一页的显示位图，只产出 QImage（QPixmap 不能跨线程）。

    缩放按目标页自己算；视口尺寸与 DPR 由主线程采样传入，worker 不碰 Qt 控件。
    """

    def __init__(self, viewer, gen: int, page_index: int,
                 viewport_size: Tuple[int, int], dpr: float):
        super().__init__()
        self.viewer = viewer
        self.gen = gen
        self.page_index = page_index
        self.viewport_size = viewport_size
        self.dpr = dpr
        self.setAutoDelete(True)

    @Slot()
    def run(self):
        v = self.viewer
        try:
            z = v._get_effective_zoom(self.page_index, self.viewport_size)
            if z <= 0:
                v._prefetch_ready.emit(self.gen, None, self.page_index, None, self.dpr)
                return
            ss = SS_SMALL_ZOOM if z < 1.0 else 1.0
            render_z = z * ss * self.dpr
            key = v._prefetch_key(self.page_index, render_z)

            img = v._render_raw_qimage(self.page_index, render_z)
            if img is None or img.isNull():
                v._prefetch_ready.emit(self.gen, key, self.page_index, None, self.dpr)
                return
            if v.auto_crop_enabled:
                img = v._crop_to_content(img, self.page_index, render_z)
            tw = max(1, round(img.width() / ss))
            th = max(1, round(img.height() / ss))
            if (tw, th) != (img.width(), img.height()):
                img = img.scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            if v.invert_enabled:
                # 与显示路径一致地反色：否则预取回来的页会在翻页时"闪"一下原色
                img = invert_qimage(img)
            v._prefetch_ready.emit(self.gen, key, self.page_index, img, self.dpr)
        except Exception:
            log.debug("预取失败 page=%s", self.page_index, exc_info=True)
            try:
                v._prefetch_ready.emit(self.gen, None, self.page_index, None, self.dpr)
            except Exception:
                pass


class PdfViewer(QWidget):
    # 后台预取完成（gen, cache_key, page_index, QImage, dpr）；QPixmap 必须在 GUI 线程创建
    _prefetch_ready = Signal(int, object, int, object, float)

    def __init__(self, status_callback=None, page_changed_callback=None, parent=None):
        super().__init__(parent)
        self.status_callback = status_callback
        self.page_changed_callback = page_changed_callback
        self.doc: Optional[fitz.Document] = None
        self.current_path: Optional[str] = None
        self.current_page_index = 0
        self.zoom = 1.6
        self.page_cache = {}
        self._page_cache_bytes = 0
        # 各页内容框（页面坐标 pt）。内容框与渲染缩放无关，因此显示与无损截取
        # 共用同一份缓存；未显示过的页也能按需算出来（跨页截取不再依赖显示历史）。
        self._content_box_cache: Dict[int, Optional[Tuple[float, float, float, float]]] = {}
        self._content_thr_cache: Dict[int, int] = {}   # 各页自适应背景阈值
        self._native_scale_cache: Dict[int, float] = {}  # 各页原生像素/pt（扫描件真实分辨率）
        self._toc_cache: Optional[list] = None
        self.render_id = 0
        self.crop_mode = False
        # 视图模式：fit_page（整页）/ fit_width（适合宽度）/ manual（用户缩放）
        self.view_mode = VIEW_FIT_PAGE

        # 自动裁剪配置（默认关闭，见 AUTO_CROP_DEFAULT）
        self.auto_crop_enabled = AUTO_CROP_DEFAULT
        self.auto_crop_white_threshold = AUTO_CROP_WHITE_THRESHOLD
        self.auto_crop_min_size = AUTO_CROP_MIN_SIZE
        self._last_crop_percent: Optional[int] = None

        # 反色显示（只影响显示路径，见 INVERT_DEFAULT）
        self.invert_enabled = INVERT_ENABLED

        # 重渲染防抖：滚轮连续缩放 / 窗口拖拽尺寸变化时合并为一次渲染
        self._pending_render = QTimer(self)
        self._pending_render.setSingleShot(True)
        self._pending_render.timeout.connect(self.render_current_page)

        # 拖拽平移
        self._drag_origin: Optional[QPoint] = None
        self._drag_scroll: Tuple[int, int] = (0, 0)
        # 截取成功后回调（主窗口用来暂存到右侧栏）
        self.capture_hook = None

        # 后台预取：worker 线程只产出 QImage，QPixmap 必须回 GUI 线程创建
        self._doc_lock = threading.RLock()
        self._prefetch_gen = 0
        self._prefetch_inflight_pages: set = set()
        self._prefetch_pool = QThreadPool(self)
        self._prefetch_pool.setMaxThreadCount(1)   # MuPDF 侧串行，避免争抢文档锁
        self._prefetch_ready.connect(self._on_prefetch_ready)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.image_label = QLabel("请打开一个 PDF")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("background: #202020; color: white;")
        # 占位下限；有页面时按页面尺寸抬高（见 _fit_label_to_pixmap）。
        # 旧代码写死 800x600：页面比视口大时标签不跟着长，QScrollArea 得不到可滚动
        # 范围，页面被居中裁掉两端且滚不到（适合宽度/放大阅读都会踩到）。
        self.image_label.setMinimumSize(320, 240)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidget(self.image_label)
        layout.addWidget(self.scroll)

        self.crop_overlay = CropOverlay(self, self.scroll.viewport())
        self.crop_overlay.crop_ready.connect(self.copy_crop_to_clipboard)
        self.scroll.viewport().installEventFilter(self)
        self.scroll.viewport().setMouseTracking(True)

    # ---- 截取标记：唯一事实来源是 CropOverlay，viewer 只做代理 ----
    # 旧实现把标记同时存在 viewer 和 overlay 两处，5 个地方各清一遍，极易不同步。
    @property
    def crop_marks_top(self) -> Optional[Tuple[int, int]]:
        return self.crop_overlay.top_marker

    @crop_marks_top.setter
    def crop_marks_top(self, value: Optional[Tuple[int, int]]):
        self.crop_overlay.top_marker = value

    @property
    def crop_marks_bottom(self) -> Optional[Tuple[int, int]]:
        return self.crop_overlay.bottom_marker

    @crop_marks_bottom.setter
    def crop_marks_bottom(self, value: Optional[Tuple[int, int]]):
        self.crop_overlay.bottom_marker = value

    def _focus_crop_overlay(self):
        """把键盘焦点交给截取/遮挡覆盖层。

        模态对话框（跳页等）关闭后，Qt 要在**下一轮事件循环**才把窗口激活和焦点
        落定，所以同步 setFocus() 常会失败（实测焦点保持 None，回车就完全没反应）。
        这里立刻试一次，再延后一拍补一次。
        """
        overlay = self.crop_overlay
        if not overlay.active:
            return
        overlay.setFocus(Qt.OtherFocusReason)
        QTimer.singleShot(
            0, lambda o=overlay: o.setFocus(Qt.OtherFocusReason) if o.active else None)

    def _sync_crop_overlay(self):
        self.crop_overlay.setGeometry(self.scroll.viewport().rect())
        self.crop_overlay.raise_()
        self.crop_overlay.update()
        # 截取/遮挡模式的"回车确认线"依赖覆盖层持有键盘焦点：跳页等模态对话框
        # 关闭后焦点会丢，回车事件就到不了覆盖层——用户看到的就是"跳页后截取失效"。
        self._focus_crop_overlay()

    def _schedule_render(self, delay_ms: int = RENDER_DEBOUNCE_MS):
        """把重渲染推迟到 delay_ms 后（同一时间只会有一次真正渲染）。"""
        self._pending_render.start(max(0, int(delay_ms)))

    def eventFilter(self, obj, event):
        # 对象销毁期间 Qt 仍可能把事件派发到已安装的过滤器，此时属性字典已空，
        # self.scroll 会解析成 QWidget.scroll 这个方法，直接判空退出更稳。
        scroll = self.__dict__.get("scroll")
        if scroll is None:
            return False
        if obj == scroll.viewport():
            et = event.type()
            if et in (QEvent.Resize, QEvent.Show):
                self._sync_crop_overlay()
                if self.view_mode in (VIEW_FIT_PAGE, VIEW_FIT_WIDTH) and self.doc:
                    # 窗口拖拽会连续触发 Resize：防抖，否则每像素都整页重渲染
                    self._schedule_render(0 if et == QEvent.Show else RENDER_DEBOUNCE_MS)
            elif et == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                if self._can_drag_pan():
                    self._drag_origin = event.position().toPoint()
                    self._drag_scroll = (self.scroll.horizontalScrollBar().value(),
                                         self.scroll.verticalScrollBar().value())
                    self.scroll.viewport().setCursor(Qt.ClosedHandCursor)
                    return True
            elif et == QEvent.MouseMove and self._drag_origin is not None:
                pos = event.position().toPoint()
                dx = pos.x() - self._drag_origin.x()
                dy = pos.y() - self._drag_origin.y()
                self.scroll.horizontalScrollBar().setValue(self._drag_scroll[0] - dx)
                self.scroll.verticalScrollBar().setValue(self._drag_scroll[1] - dy)
                return True
            elif et == QEvent.MouseButtonRelease and self._drag_origin is not None:
                self._drag_origin = None
                self.scroll.viewport().unsetCursor()
                return True
            elif et == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                self._zoom_toggle_at(event.position().toPoint())
                return True
        return super().eventFilter(obj, event)

    def _can_drag_pan(self) -> bool:
        """内容尺寸超过视口时才允许拖拽平移；截取/遮挡模式优先。"""
        if self.crop_mode or self.crop_overlay.cover_mode or not self.doc:
            return False
        hbar = self.scroll.horizontalScrollBar()
        vbar = self.scroll.verticalScrollBar()
        return hbar.maximum() > hbar.minimum() or vbar.maximum() > vbar.minimum()

    def _zoom_toggle_at(self, viewport_pos: QPoint):
        """双击：小图时以点击位置为中心放大；已放大时回到适合宽度。"""
        if not self.doc:
            return
        current = self._get_effective_zoom(self.current_page_index)
        if current > 1.2 and self.view_mode == VIEW_MANUAL:
            self.set_view_mode(VIEW_FIT_WIDTH)
            return
        target = min(6.0, max(1.0, current * 2.5))
        self.set_zoom_centered(target, viewport_pos)

    def set_zoom_centered(self, value: float, viewport_pos: QPoint):
        """缩放到 value，并让视口里"点击处对应的页面位置"尽量留在原处。"""
        label = self.image_label
        pixmap = label.pixmap()
        hbar = self.scroll.horizontalScrollBar()
        vbar = self.scroll.verticalScrollBar()
        frac = (0.5, 0.5)
        if pixmap is not None and not pixmap.isNull() and label.width() > 0 and label.height() > 0:
            try:
                origin = label.mapTo(self.scroll.viewport(), QPoint(0, 0))
                pm_w = pixmap.width() / (pixmap.devicePixelRatio() or 1.0)
                pm_h = pixmap.height() / (pixmap.devicePixelRatio() or 1.0)
                left = origin.x() + (label.width() - pm_w) / 2.0
                top = origin.y() + (label.height() - pm_h) / 2.0
                fx = (viewport_pos.x() - left) / pm_w if pm_w > 0 else 0.5
                fy = (viewport_pos.y() - top) / pm_h if pm_h > 0 else 0.5
                frac = (min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy)))
            except Exception:
                log.debug("计算缩放中心失败", exc_info=True)
        self._set_zoom(value)
        # 渲染后按同样的相对位置回填滚动条
        self._pending_scroll_frac = frac
        QTimer.singleShot(0, self._apply_pending_scroll)

    def _apply_pending_scroll(self):
        frac = getattr(self, "_pending_scroll_frac", None)
        if not frac:
            return
        self._pending_scroll_frac = None
        label = self.image_label
        pixmap = label.pixmap()
        if pixmap is None or pixmap.isNull():
            return
        pm_w = pixmap.width() / (pixmap.devicePixelRatio() or 1.0)
        pm_h = pixmap.height() / (pixmap.devicePixelRatio() or 1.0)
        vp = self.scroll.viewport()
        origin = label.mapTo(vp, QPoint(0, 0))
        left = origin.x() + (label.width() - pm_w) / 2.0
        top = origin.y() + (label.height() - pm_h) / 2.0
        # 目标：页面比例点 frac 落在视口中心
        self.scroll.horizontalScrollBar().setValue(
            int(left + frac[0] * pm_w - vp.width() / 2))
        self.scroll.verticalScrollBar().setValue(
            int(top + frac[1] * pm_h - vp.height() / 2))

    # ---- 视图模式 ----
    def set_view_mode(self, mode: str):
        if mode not in (VIEW_FIT_PAGE, VIEW_FIT_WIDTH, VIEW_MANUAL):
            return
        if mode == VIEW_MANUAL and self.view_mode != VIEW_MANUAL:
            # 从适配模式进入手动模式时，沿用以当前页面算出的有效缩放
            self.zoom = self._get_effective_zoom(self.current_page_index)
        self.view_mode = mode
        self._clear_page_cache()
        self.render_current_page()

    def set_fit_page_mode(self, enabled: bool):
        """兼容旧接口：True → 整页适配；False → 手动缩放。"""
        self.set_view_mode(VIEW_FIT_PAGE if enabled else VIEW_MANUAL)

    def set_fit_width_mode(self):
        self.set_view_mode(VIEW_FIT_WIDTH)

    def fit_page(self):
        self.set_view_mode(VIEW_FIT_PAGE)

    def fit_width(self):
        self.set_view_mode(VIEW_FIT_WIDTH)

    def zoom_to_100(self):
        self._set_zoom(1.0)

    def toggle_fit_page_mode(self):
        self.set_view_mode(VIEW_FIT_WIDTH if self.view_mode == VIEW_FIT_PAGE else VIEW_FIT_PAGE)

    def set_auto_crop_enabled(self, enabled: bool):
        self.auto_crop_enabled = enabled
        self._clear_page_cache()
        self._content_box_cache.clear()
        self._content_thr_cache.clear()
        self._last_crop_percent = None
        self.render_current_page()

    def toggle_auto_crop_enabled(self):
        self.set_auto_crop_enabled(not self.auto_crop_enabled)

    def set_invert_enabled(self, enabled: bool):
        """反色显示开关。只影响显示：内容框探测、自动裁剪、无损截取都按原始像素走。"""
        self.invert_enabled = bool(enabled)
        # 缓存里存的是另一种配色，必须整体作废（同时让在途预取结果失效）
        self._clear_page_cache()
        self.render_current_page()

    def toggle_invert_enabled(self):
        self.set_invert_enabled(not self.invert_enabled)

    def _content_box_pt(self, page_index: int) -> Optional[Tuple[float, float, float, float]]:
        """页面内容框（页面坐标，单位 pt），带缓存；自动裁剪关闭时返回 None。

        用固定的低倍渲染探测（目标约 CONTENT_PROBE_TARGET_PX 像素），因此结果
        与显示缩放无关：显示与无损截取共用同一个框，跨页截取的中转页也能按需
        算出来——旧实现只在"该页被显示过"时记录裁剪框，未显示过的页会走进
        一条把 QPixmap 当 QImage 用的坏分支直接崩溃。
        """
        if not self.auto_crop_enabled or not self.doc:
            return None
        if page_index in self._content_box_cache:
            return self._content_box_cache[page_index]

        box_pt: Optional[Tuple[float, float, float, float]] = None
        try:
            with self._doc_lock:
                rect = self.doc.load_page(page_index).rect
            if rect.width > 0 and rect.height > 0:
                probe_zoom = max(CONTENT_PROBE_MIN_ZOOM,
                                 min(1.0, math.sqrt(CONTENT_PROBE_TARGET_PX / (rect.width * rect.height))))
                probe = self._render_raw_qimage(page_index, probe_zoom)
                if probe is not None and not probe.isNull():
                    # 自适应背景阈值 + 抗噪（滤掉扫描件边缘的孤立墨点）
                    raw, thr = content_box_and_threshold_from_qimage(
                        probe, self.auto_crop_white_threshold,
                        min_fraction=CONTENT_NOISE_MIN_FRACTION)
                    self._content_thr_cache[page_index] = thr
                    if raw is not None:
                        left, top, right, bottom = safe_crop_box(
                            raw, probe.width(), probe.height(), min_size=self.auto_crop_min_size)
                        box_pt = (
                            max(0.0, min(rect.width, left / probe_zoom)),
                            max(0.0, min(rect.height, top / probe_zoom)),
                            max(0.0, min(rect.width, right / probe_zoom)),
                            max(0.0, min(rect.height, bottom / probe_zoom)),
                        )
                        if box_pt[2] - box_pt[0] <= 0 or box_pt[3] - box_pt[1] <= 0:
                            box_pt = None
        except Exception:
            log.debug("内容框探测失败 page=%s", page_index, exc_info=True)
            box_pt = None

        self._content_box_cache[page_index] = box_pt
        return box_pt

    def _content_threshold(self, page_index: int) -> int:
        """该页的背景阈值（由内容框探测时算出并缓存）。"""
        return self._content_thr_cache.get(page_index, self.auto_crop_white_threshold)

    def _native_px_per_pt(self, page_index: int) -> float:
        """页面 1pt 对应多少原生图像像素（= 扫描件真实光学分辨率）。

        扫描件的页面框常直接按像素尺寸以 72 DPI 建立，此时"300 DPI"就是
        把原图插值放大 4.17 倍：实测拓扑学整页 231MPx/883MB，而原生只有
        13MPx/51MB——体积暴涨、更慢，而且并不更清晰。返回 0 表示该页没有
        内嵌位图（矢量/数字版 PDF），此时按目标 DPI 正常渲染。
        """
        if page_index in self._native_scale_cache:
            return self._native_scale_cache[page_index]
        best = 0.0
        try:
            with self._doc_lock:
                page = self.doc.load_page(page_index)
                for item in page.get_images(full=True):
                    xref = item[0]
                    try:
                        info = self.doc.extract_image(xref)
                    except Exception:
                        continue
                    iw, ih = info.get("width") or 0, info.get("height") or 0
                    if iw <= 0 or ih <= 0:
                        continue
                    for r in page.get_image_rects(xref):
                        if r.width > 0 and r.height > 0:
                            best = max(best, iw / r.width, ih / r.height)
        except Exception:
            log.debug("读取原生分辨率失败 page=%s", page_index, exc_info=True)
        self._native_scale_cache[page_index] = best
        return best

    def _capture_scale(self, page_index: int) -> float:
        """截取输出使用的像素/pt：目标 300 DPI 封顶，绝不超过原图分辨率。

        下限定为 CAPTURE_MIN_SCALE，避免遇到 72 DPI 的低清扫描时截出只有
        几十像素的图（那种情况下略微上采样比给 AI 一张小图更实用）。
        """
        target = CROP_HIGH_RES_DPI / PDF_BASE_DPI
        native = self._native_px_per_pt(page_index)
        if native <= 0:
            return target
        return max(CAPTURE_MIN_SCALE, min(target, native))

    def _page_content_size_pt(self, page_index: int) -> Optional[Tuple[float, float]]:
        """显示该页时的内容尺寸（pt）：自动裁剪开启且探测成功时用内容框。"""
        if not self.doc:
            return None
        box = self._content_box_pt(page_index)
        if box is not None:
            return (max(1e-6, box[2] - box[0]), max(1e-6, box[3] - box[1]))
        try:
            with self._doc_lock:
                rect = self.doc.load_page(page_index).rect
        except Exception:
            log.debug("读取页面尺寸失败 page=%s", page_index, exc_info=True)
            return None
        if rect.width <= 0 or rect.height <= 0:
            return None
        return (rect.width, rect.height)

    def _get_effective_zoom(self, page_index: int,
                            viewport_size: Optional[Tuple[int, int]] = None) -> float:
        """当前视图模式下的有效缩放（闭式解，一次到位）。

        旧实现用"渲染→量尺寸→再修正"的两步迭代，且量尺寸时绕过页面缓存，
        于是每次翻页要多做 2 次整页栅格化（实测 2.8 次/页）。内容框以页面
        坐标缓存后，缩放可以直接由内容尺寸算出，不再需要任何探测渲染。

        viewport_size 由后台预取线程显式传入（避免在工作线程里访问 Qt 控件）。
        """
        if not self.doc or self.view_mode == VIEW_MANUAL:
            return self.zoom
        try:
            size = self._page_content_size_pt(page_index)
            if not size:
                return self.zoom
            content_w, content_h = size
            if viewport_size is None:
                vw, vh = self._viewport_size()
            else:
                vw, vh = viewport_size
            margin = 20
            avail_w = max(100, vw - margin)
            avail_h = max(100, vh - margin)
            if self.view_mode == VIEW_FIT_WIDTH:
                # 适合宽度：只看宽度（超出视口高度时垂直滚动，这是大页面阅读的常态）
                return max(0.1, min(FIT_WIDTH_MAX_ZOOM, avail_w / content_w))
            return max(0.1, min(6.0, min(avail_w / content_w, avail_h / content_h)))
        except Exception:
            log.debug("适应页面缩放计算失败，退回用户缩放", exc_info=True)
            return self.zoom

    def set_crop_mode(self, enabled: bool):
        self.crop_mode = enabled
        if enabled:
            # 截取与遮挡互斥，避免两个覆盖状态叠加
            self.crop_overlay.cover_mode = False
            self.crop_overlay.cover_y = None
            self._sync_crop_overlay()
            self.crop_overlay.start()
        else:
            self.crop_overlay.stop()
            self.crop_overlay.reset_marks()
            self.crop_marks_top = None
            self.crop_marks_bottom = None

    def stop_crop_mode(self):
        self.set_crop_mode(False)

    def set_crop_marks(self, top_marker, bottom_marker):
        """写入截取标记。标记的唯一存放处是 CropOverlay，避免两份状态不同步。"""
        self.crop_overlay.top_marker = top_marker
        self.crop_overlay.bottom_marker = bottom_marker
        self.crop_overlay.update()

    def set_cover_mode(self, enabled: bool):
        if enabled:
            # 遮挡与截取互斥：进入遮挡即关闭截取模式
            self.crop_mode = False
            self.crop_marks_top = None
            self.crop_marks_bottom = None
            self._sync_crop_overlay()
            self.crop_overlay.start_cover()
        else:
            self.crop_overlay.stop_cover()

    def stop_cover_mode(self):
        self.set_cover_mode(False)

    def clear_pdf(self):
        self.render_id += 1
        self._clear_page_cache()
        self._content_box_cache.clear()
        self._content_thr_cache.clear()
        self._native_scale_cache.clear()
        self._toc_cache = None
        if self.doc is not None:
            try:
                with self._doc_lock:
                    self.doc.close()
            except Exception:
                # 关不掉通常意味着文件句柄被占用（Windows 上会锁文件），要能看见
                log.warning("关闭 PDF 失败，文件句柄可能未释放", exc_info=True)
        self.doc = None
        self.current_path = None
        self.current_page_index = 0
        self.image_label.setPixmap(QPixmap())
        # 没有页面了，缩回占位下限（否则上一次的页面尺寸会撑出多余滚动条）
        self.image_label.setMinimumSize(320, 240)
        self.image_label.resize(320, 240)
        self.image_label.setText("请打开一个 PDF")
        self.update_status()

    def open_pdf(self, path: str, page_1_based: int = 1) -> bool:
        """打开 PDF，返回是否成功（调用方据此决定要不要记入历史/最近）。"""
        self.clear_pdf()
        try:
            with self._doc_lock:
                self.doc = fitz.open(path)
            self.current_path = os.path.abspath(path)
            self.goto_page(page_1_based)
            return True
        except Exception as e:
            log.error("打开 PDF 失败：%s", path, exc_info=True)
            QMessageBox.critical(self, "打开失败", f"无法打开 PDF：\n{path}\n\n{e}")
            self.clear_pdf()
            return False

    def total_pages(self) -> int:
        return self.doc.page_count if self.doc else 0

    def current_page_1_based(self) -> int:
        return self.current_page_index + 1

    def _render_raw_qimage(self, page_index: int, zoom: float) -> Optional[QImage]:
        """栅格化一页为 QImage。

        可以在后台预取线程里调用（QImage 允许跨线程，QPixmap 不允许），
        因此所有 MuPDF 文档访问都用 _doc_lock 串行化——PyMuPDF 的 Document
        不支持多线程并发访问。
        """
        if not self.doc:
            return None
        try:
            with self._doc_lock:
                page = self.doc.load_page(page_index)
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                return QImage(pix.samples, pix.width, pix.height, pix.stride,
                              QImage.Format_RGB888 if pix.n < 4 else QImage.Format_RGBA8888).copy()
        except Exception:
            log.warning("页面栅格化失败 page=%s zoom=%.3f", page_index, zoom, exc_info=True)
            return None

    def _crop_to_content(self, qimg: QImage, page_index: int, render_zoom: float) -> QImage:
        """按缓存的内容框裁剪渲染结果（页面坐标 × 渲染缩放 = 像素），并更新裁剪比例。

        与旧实现相比省掉了整幅图的 numpy 灰度检测：内容框已在低倍探测时以
        页面坐标缓存，这里只剩一次 QImage.copy。
        """
        box = self._content_box_pt(page_index)
        if box is None or render_zoom <= 0 or qimg.isNull():
            return qimg
        w, h = qimg.width(), qimg.height()
        left = max(0, min(int(round(box[0] * render_zoom)), w - 1))
        top = max(0, min(int(round(box[1] * render_zoom)), h - 1))
        right = max(left + 1, min(int(round(box[2] * render_zoom)), w))
        bottom = max(top + 1, min(int(round(box[3] * render_zoom)), h))
        if (right - left) * (bottom - top) > w * h * 0.999:
            return qimg
        self._last_crop_percent = int((1 - (right - left) * (bottom - top) / (w * h)) * 100)
        return qimg.copy(QRect(left, top, right - left, bottom - top))

    @staticmethod
    def _pixmap_bytes(pixmap: Optional[QPixmap]) -> int:
        if pixmap is None or pixmap.isNull():
            return 0
        return pixmap.width() * pixmap.height() * 4

    def _clear_page_cache(self):
        self.page_cache.clear()
        self._page_cache_bytes = 0
        # 在途的预取结果对应旧视图，作废掉，避免重新填回缓存
        self._invalidate_prefetch()

    def release_page_cache(self) -> int:
        """释放页面位图缓存，返回释放的字节数（标签页被切走时调用）。

        缓存上限是按**单个** viewer 算的（PAGE_CACHE_MAX_BYTES），N 个标签页叠加会
        成倍上涨；而整页位图正是最大的一项（实测单页可达数百 MB）。非活动标签只要
        保住文档、页码、缩放与内容框缓存即可：切回来时会自动重新渲染，白边不用重算。
        正在显示的位图由 QLabel 持有，这里不动（见 release_display）。
        """
        freed = self._page_cache_bytes
        self._clear_page_cache()
        return freed

    def release_display(self) -> int:
        """丢掉当前显示的位图，返回释放的字节数（非活动标签页调用）。

        大页面放大阅读时单张显示位图可达数百 MB，按标签页叠加会吃满内存。
        只清位图、**不动标签尺寸**：滚动范围与滚动位置因此保持不变，切回来重渲染
        不会跳回页面顶部。
        """
        pixmap = self.image_label.pixmap()
        if pixmap is None or pixmap.isNull():
            return 0
        freed = self._pixmap_bytes(pixmap)
        self.image_label.setPixmap(QPixmap())
        return freed

    def _cache_put(self, key, pixmap):
        """写入页面缓存：条目数与总字节数双重限制，超出时逐出最旧项。

        旧实现只限条目数（40），而 300 DPI 整页位图单张就可达数百 MB
        （实测 95 MPx 页 ≈ 362MB），缓存填满会把内存吃到 GB 级。
        """
        old = self.page_cache.pop(key, None)
        if old is not None:
            self._page_cache_bytes -= self._pixmap_bytes(old)
        self.page_cache[key] = pixmap
        self._page_cache_bytes += self._pixmap_bytes(pixmap)
        while self.page_cache and (len(self.page_cache) > PAGE_CACHE_MAX
                                   or self._page_cache_bytes > PAGE_CACHE_MAX_BYTES):
            victim = next(iter(self.page_cache))
            self._page_cache_bytes -= self._pixmap_bytes(self.page_cache.pop(victim))
        self._page_cache_bytes = max(0, self._page_cache_bytes)

    def _display_render_params(self, z: float) -> Tuple[float, float]:
        """显示路径的渲染参数：超采样倍数、设备像素比。

        缩小显示时先按更高分辨率渲染再平滑缩小，弥补小字号锯齿；
        z>=1 时不超采样，控制开销。
        """
        dpr = self.devicePixelRatioF() or 1.0
        ss = SS_SMALL_ZOOM if z < 1.0 else 1.0
        return ss, dpr

    # ---- 后台预取相邻页 ----
    # 实测冷页栅格化 71~196 ms、命中缓存 0.1 ms：把前后各一页提前渲染好，
    # 连续翻页的等待就消失了。worker 只做 QImage（QPixmap 必须在 GUI 线程建），
    # 且只往缓存里放结果，绝不碰 UI 状态。
    def _schedule_prefetch(self):
        """把前后各 N 页交给后台渲染。

        注意：每页在 fit 模式下都有自己的有效缩放，预取必须按**目标页自己的**
        缩放渲染，否则缓存键与显示路径对不上，预取等于白做（实测修正前翻页仍有
        20~27ms 的冷渲染残留）。缩放所需的视口尺寸/DPR 在主线程采样后传进去，
        worker 里不碰任何 Qt 控件。
        """
        if not self.doc or PREFETCH_PAGES <= 0:
            return
        viewport_size = self._viewport_size()
        dpr = self.devicePixelRatioF() or 1.0
        for delta in (-PREFETCH_PAGES, PREFETCH_PAGES):
            page_index = self.current_page_index + delta
            if page_index < 0 or page_index >= self.total_pages():
                continue
            if page_index in self._prefetch_inflight_pages:
                continue
            self._prefetch_inflight_pages.add(page_index)
            self._prefetch_pool.start(
                _PrefetchTask(self, self._prefetch_gen, page_index, viewport_size, dpr))

    def _viewport_size(self) -> Tuple[int, int]:
        size = self.scroll.viewport().size()
        return (size.width(), size.height())

    def _prefetch_key(self, page_index: int, render_z: float) -> tuple:
        return (self.current_path, page_index, round(render_z, 4), False,
                self.view_mode, self.auto_crop_enabled,
                self.auto_crop_white_threshold, self.auto_crop_min_size,
                bool(self.invert_enabled))

    def _on_prefetch_ready(self, gen: int, key, page_index: int,
                           image: Optional[QImage], dpr: float):
        self._prefetch_inflight_pages.discard(page_index)
        if gen != self._prefetch_gen or image is None or image.isNull():
            return
        if not self.doc or key[0] != self.current_path or key in self.page_cache:
            return
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(dpr or 1.0)
        self._cache_put(key, pixmap)

    def _invalidate_prefetch(self):
        """文档/视图变化：让在途的预取结果作废。"""
        self._prefetch_gen += 1
        self._prefetch_inflight_pages.clear()

    def _render_at_scale(self, page_index: int, z: float, ss: float, dpr: float, exact: bool) -> QPixmap:
        """渲染页面并按显示参数加工。

        exact=True：无损截取路径，精确输出、不超采样、不设 DPR、**不反色**。
        exact=False：显示路径，按 z*ss*dpr 渲染，再平滑缩回逻辑尺寸并设置 DPR。
        """
        render_z = z if exact else z * ss * dpr
        # 反色只作用于显示路径；它进缓存键，否则切换反色会拿到另一种配色的旧缓存
        invert = bool(self.invert_enabled) and not exact
        key = (
            self.current_path,
            page_index,
            round(render_z, 4),
            exact,
            self.view_mode,
            self.auto_crop_enabled,
            self.auto_crop_white_threshold,
            self.auto_crop_min_size,
            invert,
        )
        cached = self.page_cache.get(key)
        if cached is not None:
            return cached

        qimg = self._render_raw_qimage(page_index, render_z)
        if qimg is None or qimg.isNull():
            return QPixmap()

        if self.auto_crop_enabled and not exact:
            # 显示路径：按缓存的页面级内容框裁剪（不再逐次做整图灰度检测）
            qimg = self._crop_to_content(qimg, page_index, render_z)

        if not exact:
            # 先高分辨率渲染，再平滑缩回。物理像素 = 逻辑尺寸 × dpr：
            # render_z = z*ss*dpr，除以 ss 后物理宽 = z*dpr*页宽，
            # 配合 setDevicePixelRatio(dpr) 正好以逻辑尺寸 z*页宽 显示。
            tw = max(1, round(qimg.width() / ss))
            th = max(1, round(qimg.height() / ss))
            if (tw, th) != (qimg.width(), qimg.height()):
                qimg = qimg.scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            if invert:
                # 放在缩放下采样之后：处理的像素最少（反色与缩放可交换，结果一致）
                qimg = invert_qimage(qimg)

        pixmap = QPixmap.fromImage(qimg)
        if not exact:
            pixmap.setDevicePixelRatio(dpr)

        self._cache_put(key, pixmap)
        return pixmap

    def render_page_to_pixmap(self, page_index: int, zoom: Optional[float] = None) -> QPixmap:
        if not self.doc:
            return QPixmap()

        if zoom is not None:
            # 精确路径（无损截取等）：给定 zoom 原样渲染
            return self._render_at_scale(page_index, zoom, 1.0, 1.0, exact=True)

        z = self._get_effective_zoom(page_index)
        ss, dpr = self._display_render_params(z)
        return self._render_at_scale(page_index, z, ss, dpr, exact=False)

    def _notify_page_changed(self):
        if self.page_changed_callback and self.current_path:
            self.page_changed_callback(self.current_path, self.current_page_1_based())

    def _apply_page_change(self):
        """页面变化后的统一收尾（渲染 + 通知）。

        render_current_page 自己会刷新状态栏，旧实现在 goto/next/prev 三处
        又各调一次 update_status()，是纯粹的重复工作。
        """
        if self.crop_mode and self.crop_overlay.preview_y is not None:
            # 换页后旧的预览线属于上一页，必须清掉：否则用户不移动鼠标就按回车，
            # 会用上一页的 y 去截取 → 区域无效或抓到错误位置
            self.crop_overlay.preview_y = None
            self.crop_overlay.update()
        self.render_current_page()
        self._notify_page_changed()

    def goto_page(self, page_1_based: int):
        if not self.doc:
            return
        page_1_based = max(1, min(page_1_based, self.doc.page_count))
        self.current_page_index = page_1_based - 1
        self._apply_page_change()

    def next_page(self):
        if self.doc and self.current_page_index < self.doc.page_count - 1:
            self.current_page_index += 1
            self._apply_page_change()

    def prev_page(self):
        if self.doc and self.current_page_index > 0:
            self.current_page_index -= 1
            self._apply_page_change()

    def _set_zoom(self, value: float, debounce: bool = False):
        self.view_mode = VIEW_MANUAL
        self.zoom = max(0.1, min(6.0, value))
        if debounce:
            # 滚轮连续缩放：先更新数值与状态栏，渲染合并到防抖定时器后一次完成
            self.update_status()
            self._schedule_render()
            return
        # 不清空整个页面缓存：缓存键含 render_z，不同缩放互不冲突，
        # 逐出交给字节预算即可，否则每按一下 Ctrl+= 都会丢掉相邻页缓存
        self.render_current_page()
        self._sync_crop_overlay()

    def zoom_in(self, debounce: bool = False):
        self._set_zoom(self._current_zoom_value() + 0.1, debounce)

    def zoom_out(self, debounce: bool = False):
        self._set_zoom(self._current_zoom_value() - 0.1, debounce)

    def _current_zoom_value(self) -> float:
        """手动缩放的起点：适配模式下以当前有效缩放为准，避免"跳一下"。"""
        if self.view_mode == VIEW_MANUAL or not self.doc:
            return self.zoom
        return self._get_effective_zoom(self.current_page_index)

    def update_status(self):
        if self.status_callback:
            self.status_callback(self.status_text())

    def status_text(self) -> str:
        """状态栏文本的唯一来源（旧实现散落在 viewer / MainWindow 三处）。"""
        if not (self.current_path and self.doc):
            return "未打开 PDF"
        if self.view_mode == VIEW_FIT_PAGE:
            fit_text = "适应页面"
        elif self.view_mode == VIEW_FIT_WIDTH:
            fit_text = "适合宽度"
        else:
            fit_text = f"{self.zoom:.1f}x"
        if not self.auto_crop_enabled:
            crop_text = "自动裁剪关"
        elif self._last_crop_percent is not None:
            crop_text = f"自动裁剪 {self._last_crop_percent}%"
        else:
            crop_text = "自动裁剪开"
        invert_text = " | 反色" if self.invert_enabled else ""
        parts = [f"{os.path.basename(self.current_path)} | 第 {self.current_page_1_based()} / "
                 f"{self.total_pages()} 页 | {fit_text} | {crop_text}"]
        if invert_text:
            parts.append("反色")
        return " | ".join(parts)

    def display_pixmap_rect_in_viewport(self):
        """页面位图在滚动区视口坐标系里的位置与逻辑尺寸（覆盖层共用的坐标基准）。

        CropOverlay 与选字层都要用它把"页面坐标"换成屏幕坐标，逻辑只留这一份。
        """
        label = self.image_label
        pixmap = label.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        dpr = pixmap.devicePixelRatio() or 1.0
        pm_w, pm_h = pixmap.width() / dpr, pixmap.height() / dpr
        viewport = self.scroll.viewport()
        origin = label.mapTo(viewport, QPoint(0, 0))
        return (origin.x() + (label.width() - pm_w) / 2.0,
                origin.y() + (label.height() - pm_h) / 2.0,
                pm_w, pm_h)

    def _fit_label_to_pixmap(self, pixmap: QPixmap):
        """把页面标签的最小尺寸设为页面逻辑尺寸，让 QScrollArea 能滚动整页。

        widgetResizable=True 会把标签压回视口大小，于是"比视口大的页面"会被居中
        裁掉两端、滚动条范围是 0 —— 滚不到、看不全。把最小尺寸抬到页面尺寸后，
        滚动条才会给出正确的可滚动范围（页面比视口小时标签仍是视口大小，居中不变）。
        """
        if pixmap is None or pixmap.isNull():
            return
        dpr = pixmap.devicePixelRatio() or 1.0
        w = max(1, int(round(pixmap.width() / dpr)))
        h = max(1, int(round(pixmap.height() / dpr)))
        self.image_label.setMinimumSize(w, h)
        self.image_label.resize(w, h)

    def render_current_page(self):
        if not self.doc:
            return

        self.render_id += 1
        current_render_id = self.render_id

        try:
            pixmap = self.render_page_to_pixmap(self.current_page_index)
            if current_render_id != self.render_id:
                return
            if pixmap.isNull():
                self.image_label.setText("渲染失败：无法生成图像")
                self.update_status()
                return
            self.image_label.setPixmap(pixmap)
            self._fit_label_to_pixmap(pixmap)
            self._sync_crop_overlay()
            self.update_status()
            # 当前页显示完就把相邻页丢到后台渲染，下一页翻页即命中缓存
            self._schedule_prefetch()
        except Exception as e:
            log.error("渲染当前页失败 page=%s", self.current_page_index, exc_info=True)
            self.image_label.setText(f"渲染失败：{e}")
            self.update_status()

    def load_bookmarks(self):
        if not self.doc:
            return []
        if self._toc_cache is None:
            try:
                with self._doc_lock:
                    self._toc_cache = self.doc.get_toc(simple=False) or []
            except Exception:
                # 书签损坏时不能伪装成“没有书签”，至少要在日志里留下原因
                log.warning("读取书签目录失败：%s", self.current_path, exc_info=True)
                self._toc_cache = []
        return self._toc_cache

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in(debounce=True)
            else:
                self.zoom_out(debounce=True)
            event.accept()
        else:
            super().wheelEvent(event)

    def _render_page_region(self, page_index: int, clip: fitz.Rect, scale: float) -> QPixmap:
        """按页面坐标区域做高分辨率渲染（不写入页面缓存）。

        直接让 PyMuPDF 只渲染需要的区域：相比"整页 300 DPI 渲染再裁掉",
        大页面上实测时间 5.5x、内存 6.7x 更优，且逐像素等价（已核对 0 字节差异）。
        截取结果不缓存——单张 300 DPI 整页位图可达数百 MB，留在缓存里毫无意义。
        """
        if not self.doc:
            return QPixmap()
        try:
            with self._doc_lock:
                page = self.doc.load_page(page_index)
                clip = fitz.Rect(clip) & page.rect
                if clip.is_empty or clip.width <= 0 or clip.height <= 0:
                    return QPixmap()
                mat = fitz.Matrix(scale, scale)
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                fmt = QImage.Format_RGB888 if pix.n < 4 else QImage.Format_RGBA8888
                return QPixmap.fromImage(QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy())
        except Exception:
            log.warning("区域渲染失败 page=%s clip=%s", page_index, clip, exc_info=True)
            return QPixmap()

    def _trim_region(self, pixmap: QPixmap, page_index: int, trim_bottom: bool) -> QPixmap:
        """把已渲染的高分辨率条带收紧到真实内容边界（水平方向，必要时含底边）。

        y2=None 表示"截到该页内容结束"，底边也要按内容收；否则底边由用户的分割线
        决定，不能动。阈值用该页探测时算出的自适应背景阈值，保证与显示裁剪一致。
        """
        if pixmap.isNull():
            return pixmap
        if pixmap.width() * pixmap.height() > MAX_TRIM_PIXELS:
            # 巨大条带（例如跨页截取的中转页）不做精细收紧：探测框的误差只有
            # 约 1 个探测像素，远小于为它再扫一遍全图的代价
            return pixmap
        try:
            img = pixmap.toImage()
            box = content_bbox_from_qimage(img, self._content_threshold(page_index),
                                           min_fraction=CONTENT_NOISE_MIN_FRACTION)
            if box is None:
                return pixmap
            left, _, right, bottom = safe_crop_box(box, img.width(), img.height(),
                                                   min_size=self.auto_crop_min_size)
            width = right - left
            height = min(img.height(), bottom) if trim_bottom else img.height()
            if width <= 0 or height <= 0:
                return pixmap
            if width >= img.width() and height >= img.height():
                return pixmap
            return pixmap.copy(QRect(left, 0, width, height))
        except Exception:
            log.debug("条带收紧失败", exc_info=True)
            return pixmap

    def _crop_lossless_region(self, page_index: int, y1: int, y2: Optional[int]) -> QPixmap:
        """按固定高 DPI 只渲染并裁剪需要的页面区域。

        y1/y2 是屏幕显示图（已按内容框裁剪）的逻辑像素坐标；y2=None 表示到该页底部。
        坐标换算：显示逻辑像素 ÷ 显示缩放 z = 相对内容框顶部的页面点偏移，
        因此可以直接算出要渲染的矩形，无需先渲染整页。
        """
        if not self.doc:
            return QPixmap()
        try:
            with self._doc_lock:
                page_rect = self.doc.load_page(page_index).rect
        except Exception:
            log.warning("读取页面失败 page=%s", page_index, exc_info=True)
            return QPixmap()

        z = self._get_effective_zoom(page_index)
        if z <= 0:
            return QPixmap()

        box = self._content_box_pt(page_index)
        if box is not None:
            x0, top, x1, content_bottom = box
        else:
            rect = page_rect
            x0, top, x1, content_bottom = rect.x0, rect.y0, rect.x1, rect.y1

        top_pt = top + y1 / z
        bottom_pt = content_bottom if y2 is None else top + y2 / z
        bottom_pt = max(top_pt + 1e-3, min(bottom_pt, content_bottom))

        pix = self._render_page_region(page_index, fitz.Rect(x0, top_pt, x1, bottom_pt),
                                       self._capture_scale(page_index))
        if pix.isNull():
            return pix
        if self.auto_crop_enabled:
            # y2=None 时底边也收到内容结束处（与旧行为一致）；有明确分割线时只收水平方向
            pix = self._trim_region(pix, page_index, trim_bottom=(y2 is None))
        return pix

    def _capture_scales(self, top_page: int, bottom_page: int) -> float:
        """跨页截取用统一的输出分辨率：取各页中最小者，绝不插值放大任何一页。"""
        scales = [self._capture_scale(p) for p in range(top_page, bottom_page + 1)]
        return min(scales) if scales else CROP_HIGH_RES_DPI / PDF_BASE_DPI

    def _estimate_capture_pixels(self, top_page: int, top_y: int,
                                 bottom_page: int, bottom_y: int) -> int:
        """估算截取输出像素量，用于在拼图之前拦下过大范围。

        换算与 _crop_lossless_region 保持一致（显示像素 ÷ 显示缩放 = 页面点偏移，
        并裁到内容框内），否则会比实际输出高估很多、把合法截取也挡掉。
        分辨率也必须是同一个（按原生封顶后的）缩放，否则会把合法截取挡掉。
        """
        scale = self._capture_scales(top_page, bottom_page)
        total = 0.0
        for page_index in range(top_page, bottom_page + 1):
            z = self._get_effective_zoom(page_index)
            if z <= 0:
                continue
            box = self._content_box_pt(page_index)
            if box is not None:
                w_pt = box[2] - box[0]
                top_pt, content_bottom = box[1], box[3]
            else:
                try:
                    with self._doc_lock:
                        rect = self.doc.load_page(page_index).rect
                except Exception:
                    log.debug("估算时无法读取页面 page=%s", page_index, exc_info=True)
                    continue
                w_pt = rect.width
                top_pt, content_bottom = rect.y0, rect.y1
            content_h_px = (content_bottom - top_pt) * z

            if page_index == top_page and page_index == bottom_page:
                band_px = bottom_y - top_y
            elif page_index == top_page:
                band_px = content_h_px - top_y
            elif page_index == bottom_page:
                band_px = bottom_y
            else:
                band_px = content_h_px
            band_px = max(0.0, min(band_px, content_h_px))
            total += (w_pt * scale) * (band_px / z * scale)
        # 估算用的是内容框尺寸与显示坐标换算，与实际渲染会有几个百分点的出入，
        # 乘一个安全系数，保证"上限"是真的上限
        return int(total * 1.15)

    def _stitch_vertical(self, pixmaps: List[QPixmap]) -> QPixmap:
        valid = [p for p in pixmaps if p and not p.isNull()]
        if not valid:
            return QPixmap()
        width = max(p.width() for p in valid)
        height = sum(p.height() for p in valid)
        result = QPixmap(width, height)
        result.fill(Qt.white)
        painter = QPainter(result)
        y = 0
        for p in valid:
            painter.drawPixmap(0, y, p)
            y += p.height()
        painter.end()
        return result

    def copy_crop_to_clipboard(self):
        if not self.doc or self.crop_marks_top is None or self.crop_marks_bottom is None:
            return

        (top_page, top_y) = self.crop_marks_top
        (bottom_page, bottom_y) = self.crop_marks_bottom

        if (top_page > bottom_page) or (top_page == bottom_page and top_y > bottom_y):
            (top_page, top_y), (bottom_page, bottom_y) = (bottom_page, bottom_y), (top_page, top_y)

        span = bottom_page - top_page + 1
        if span > MAX_CAPTURE_PAGES:
            self.status_callback(
                f"截取跨页过多（{span} 页，上限 {MAX_CAPTURE_PAGES} 页）：请缩小范围后重试"
            )
            return

        estimate = self._estimate_capture_pixels(top_page, top_y, bottom_page, bottom_y)
        if estimate > MAX_CAPTURE_PIXELS:
            self.status_callback(
                f"截取范围过大（约 {estimate / 1e6:.0f} MPx，上限 "
                f"{MAX_CAPTURE_PIXELS / 1e6:.0f} MPx）：请缩小跨页范围后重试"
            )
            return

        parts: List[QPixmap] = []

        if top_page == bottom_page:
            part = self._crop_lossless_region(top_page, top_y, bottom_y)
            if not part.isNull():
                parts.append(part)
        else:
            part1 = self._crop_lossless_region(top_page, top_y, None)
            if not part1.isNull():
                parts.append(part1)

            for page_index in range(top_page + 1, bottom_page):
                mid = self._crop_lossless_region(page_index, 0, None)
                if not mid.isNull():
                    parts.append(mid)

            part2 = self._crop_lossless_region(bottom_page, 0, bottom_y)
            if not part2.isNull():
                parts.append(part2)

        merged = self._stitch_vertical(parts)
        if merged.isNull():
            self.status_callback("截取失败：所选区域无效，请重新定位分割线")
            return

        QApplication.clipboard().setPixmap(merged)

        # 顺便暂存一份：右侧栏「暂存」页能看到缩略图与出处，点一下可重新复制
        if self.capture_hook is not None:
            try:
                self.capture_hook(merged, os.path.basename(self.current_path) if self.current_path else "",
                                  top_page + 1, top_y, bottom_y)
            except Exception:
                log.debug("暂存截取图失败", exc_info=True)

        # 循环截取：复制成功后不退出截取模式，只清空分割线，立刻可以开始下一次截取。
        # 截取模式只有在再次触发“截取模式”指令（Ctrl+S）时才关闭。
        self.set_crop_marks(None, None)
        if self.crop_mode:
            self._sync_crop_overlay()
            self.crop_overlay.setFocus()
        scale = self._capture_scales(top_page, bottom_page)
        dpi = scale * PDF_BASE_DPI
        native = min((self._native_px_per_pt(p) for p in range(top_page, bottom_page + 1)),
                     default=0.0)
        if 0 < native < CROP_HIGH_RES_DPI / PDF_BASE_DPI and scale <= native + 1e-6:
            res_text = f"原生 {dpi:.0f} DPI"
        elif 0 < native < CROP_HIGH_RES_DPI / PDF_BASE_DPI:
            res_text = f"{dpi:.0f} DPI（低清扫描限幅）"
        else:
            res_text = f"{dpi:.0f} DPI"
        self.status_callback(
            f"已复制无损截图（{res_text}，{merged.width()}×{merged.height()}）；"
            f"截取模式继续，再次按 Ctrl+S 退出"
        )
