import bisect
import json
import logging
import math
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import fitz  # PyMuPDF
import numpy as np
from PySide6.QtCore import Qt, QRect, QPoint, Signal, QEvent, QTimer, QThreadPool, QRunnable, Slot
from PySide6.QtGui import (
    QImage,
    QKeySequence,
    QPixmap,
    QShortcut,
    QPainter,
    QPen,
    QColor,
    QGuiApplication,
    QIcon,
    QFont,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ========== 日志 ==========
# 默认只输出 warning 以上；set PDF_READER_LOG=DEBUG 可看到被捕获异常的细节。
# 定义放在最前面，配置加载失败时也能报出来。
log = logging.getLogger("pdf_reader")
if os.environ.get("PDF_READER_LOG"):
    logging.basicConfig(
        level=os.environ["PDF_READER_LOG"].upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ========== 路径与配置（发布友好：不依赖任何机器绝对路径） ==========
# 目录结构：
#   pdf_reader/
#     reader.py           主程序
#     reader_config.json  可选配置（由 reader_config.example.json 复制而来）
#     assets/reader.ico   窗口图标
APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
CONFIG_PATH = APP_DIR / "reader_config.json"


def load_config() -> Dict[str, Any]:
    """读取同目录 reader_config.json；不存在或格式错误时返回空配置。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 配置写错时不能装作"没有配置"，否则用户改了半天不生效也不知道为什么
        log.warning("配置文件无法解析，已忽略：%s", CONFIG_PATH, exc_info=True)
        return {}


CONFIG = load_config()


def _cfg(key: str, default):
    """配置项取值：reader_config.json > 环境变量 > 默认值。"""
    env = os.environ.get("PDF_READER_" + key.upper())
    value = CONFIG.get(key, env)
    return default if value in (None, "") else value


def default_downloads_dir() -> Path:
    """默认 PDF 目录：系统认定的「下载」文件夹 → ~/Downloads → ~/Documents → 主目录。

    发布出去的程序不能写死任何机器路径，所以默认值在运行时算出来：Windows 先问
    注册表里的已知文件夹（用户在资源管理器里把"下载"搬家后，只有注册表知道新位置，
    ~/Downloads 可能根本不存在），拿不到再退回常见位置。非 Windows 直接用 ~/Downloads。
    """
    if sys.platform == "win32":
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as hkey:
                raw, _ = winreg.QueryValueEx(hkey, "{374DE290-123F-4565-9164-39C4925E467B}")
            # 注册表里常存 %USERPROFILE%\Downloads 这类未展开的值
            known = Path(os.path.expandvars(str(raw)))
            if known.is_dir():
                return known
        except Exception:
            log.debug("读取系统「下载」文件夹失败，退回 ~/Downloads", exc_info=True)
    for candidate in (Path.home() / "Downloads", Path.home() / "Documents"):
        if candidate.is_dir():
            return candidate
    return Path.home()


# PDF 扫描/打开目录：默认用户自己的「下载」文件夹（运行时计算，不含机器路径）
PDF_ROOT_DIR = str(_cfg("pdf_root_dir", default_downloads_dir()))
# 阅读进度等状态文件目录：默认与 PDF_ROOT_DIR 相同（保持旧行为）
STATE_DIR = str(_cfg("state_dir", PDF_ROOT_DIR))
INITIAL_PDF_KEYWORD = str(_cfg("initial_pdf_keyword", ""))
try:
    INITIAL_PAGE = max(1, int(_cfg("initial_page", 1)))
except (TypeError, ValueError):
    log.warning("initial_page 配置不是整数，已回退为 1")
    INITIAL_PAGE = 1
HISTORY_FILE_NAME = ".pdf_reader_history.json"
RECENT_FILE_NAME = ".pdf_reader_recent.json"
# 图标优先用包内 assets/reader.ico（随包发布）；缺失时返回空串，使用 Qt 默认图标
ICON_PATH = str(ASSETS_DIR / "reader.ico") if (ASSETS_DIR / "reader.ico").is_file() else ""
HISTORY_VERSION = 3


# ========== 自动裁剪参数
# 默认关闭：帮助文本、README 与这里的取值必须一致（曾出现"文档说默认开启"的旧问题）
AUTO_CROP_DEFAULT = False
AUTO_CROP_WHITE_THRESHOLD = 245
AUTO_CROP_MIN_SIZE = 8

# ========== 无损截取
# 截取时脱离屏幕显示分辨率按固定 DPI 独立渲染，但**绝不超过原图光学分辨率**：
# 扫描件的页面框常按 72 DPI 从像素尺寸建立，此时 300 DPI 等于把原图插值放大 4.17 倍
# （实测拓扑学：整页 231MPx/883MB，而原生只有 13MPx/51MB），既慢又更模糊。
CROP_HIGH_RES_DPI = 300   # 目标输出 DPI（上限，不是固定值）
PDF_BASE_DPI = 72         # PyMuPDF 默认渲染 DPI
CAPTURE_MIN_SCALE = 1.5   # 截取输出像素/pt 的下限，避免极低清扫描截出几十像素的图
MAX_CAPTURE_PAGES = 50            # 单次跨页截取的页数上限
MAX_CAPTURE_PIXELS = 200_000_000  # 单次截取输出像素上限（≈800MB 位图），超限直接拒绝

# ========== 渲染质量 / 效率
SS_SMALL_ZOOM = 1.5       # 缩小显示(z<1)时的超采样倍数：先高分辨率渲染再平滑缩小
PAGE_CACHE_MAX = 40               # 页面位图缓存条目上限
PAGE_CACHE_MAX_BYTES = 256 * 1024 * 1024   # 页面位图缓存内存上限（按字节逐出）
CONTENT_PROBE_TARGET_PX = 500_000          # 内容框探测渲染的目标像素数（按页面积反推探测缩放）
CONTENT_PROBE_MIN_ZOOM = 0.15
CONTENT_BBOX_CHUNK_ROWS = 256              # 灰度计算分块行数（峰值内存与图高无关）
CONTENT_NOISE_MIN_FRACTION = 0.002         # 行/列内容像素占比低于该值视为扫描噪点（滤孤立墨点）
MAX_TRIM_PIXELS = 8_000_000                # 超过该像素数的条带不再做精细边界收紧（探测框已足够准）
RENDER_DEBOUNCE_MS = 120                   # 缩放/窗口尺寸变化后的重渲染防抖
STATE_SAVE_DEBOUNCE_MS = 500               # 阅读进度落盘防抖
PREFETCH_PAGES = 1                         # 后台预取的相邻页数（前后各 N 页）
FIT_WIDTH_MAX_ZOOM = 8.0                   # 适合宽度时的缩放上限（超宽页不至于渲染出巨图）

# 视图模式
VIEW_FIT_PAGE = "page"      # 整页适配
VIEW_FIT_WIDTH = "width"    # 适合宽度（大页面/小字扫描件的主要阅读姿态）
VIEW_MANUAL = "manual"      # 用户手动缩放


@dataclass
class PdfEntry:
    name: str
    path: str


# ============================================================
# 内容框（去白边）检测
# ============================================================

def rgb_to_gray(arr: np.ndarray) -> np.ndarray:
    return 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]


def content_row_col_hits(arr_rgb: np.ndarray, white_threshold: int,
                         chunk_rows: int = CONTENT_BBOX_CHUNK_ROWS,
                         min_fraction: float = 0.0) -> Tuple[np.ndarray, np.ndarray]:
    """返回 (有内容的行掩码, 有内容的列掩码)。

    逐块计算灰度，峰值内存与图像高度无关：一张 300 DPI A4 页整图 float32
    灰度约为 104MB，而内容像素上的 np.where 索引还会再要十几到几十 MB。

    min_fraction>0 时要求该行/列的内容像素占比达到该比例才算内容，用来滤掉
    扫描件的孤立墨点：实测拓扑学扫描页最底 6 行每行只有 **1 个** 内容像素，
    却足以把内容框撑满整页，让"去白边"完全失效。min_fraction=0 时语义与
    旧实现（any）逐位一致。
    """
    h, w = arr_rgb.shape[:2]
    row_cnt = np.zeros(h, dtype=np.int32)
    col_cnt = np.zeros(w, dtype=np.int32)
    for y0 in range(0, h, chunk_rows):
        block = arr_rgb[y0:y0 + chunk_rows]
        gray = rgb_to_gray(block.astype(np.float32))
        mask = gray < white_threshold
        row_cnt[y0:y0 + block.shape[0]] = mask.sum(axis=1)
        col_cnt += mask.sum(axis=0)
    if min_fraction <= 0:
        # 与旧实现（any）逐位一致
        return row_cnt >= 1, col_cnt >= 1
    # 至少 2 个像素：单个墨点在"行占比"上永远只有 1，必须滤掉；
    # 而横线/竖线的行（列）内像素数远超 2，不会被误伤
    row_need = max(2, int(w * min_fraction))
    col_need = max(2, int(h * min_fraction))
    return row_cnt >= row_need, col_cnt >= col_need


def find_content_bbox_arr(arr_rgb: np.ndarray, white_threshold: int = 245,
                          chunk_rows: int = CONTENT_BBOX_CHUNK_ROWS,
                          min_fraction: float = 0.0) -> Optional[Tuple[int, int, int, int]]:
    """内容矩形框 (left, top, right, bottom)，坐标以传入数组的像素为准。

    与旧实现（整图 float32 灰度 + np.where）逐位等价，但：
      * 灰度按 256 行分块计算，峰值临时量从 104MB(A4@300DPI) 降到约 7.6MB；
      * 用行/列投影代替 np.where，省掉"内容像素数 × 16 字节"的索引数组。
    调用方若想更便宜，应该喂更小的图（本程序用低倍探测图），而不是降采样——
    实测逐通道 min 池化为了不漏细线要遍历全图，反而比精确计算更慢。
    """
    if arr_rgb.size == 0 or arr_rgb.ndim != 3 or arr_rgb.shape[2] < 3:
        return None
    h, w = arr_rgb.shape[:2]
    row_hit, col_hit = content_row_col_hits(arr_rgb, white_threshold, chunk_rows, min_fraction)
    rows = np.flatnonzero(row_hit)
    if rows.size == 0:
        return None
    cols = np.flatnonzero(col_hit)
    if cols.size == 0:
        return None
    return (int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)


def estimate_background_level(arr_rgb: np.ndarray, border_ratio: float = 0.04,
                              sample_limit: int = 200_000) -> int:
    """估计页面"纸面底色"（扫描件常是灰底而不是纯白）。

    取四边框区域像素灰度的 75 分位：比中位数更抗"扫描黑边/书脊阴影"这类
    局部极暗区域，又比最大值更抗白斑噪声。
    """
    h, w = arr_rgb.shape[:2]
    b = max(1, int(min(h, w) * border_ratio))
    border = np.concatenate([
        arr_rgb[:b].reshape(-1, 3), arr_rgb[-b:].reshape(-1, 3),
        arr_rgb[:, :b].reshape(-1, 3), arr_rgb[:, -b:].reshape(-1, 3),
    ])
    if border.size == 0:
        return 255
    if border.shape[0] > sample_limit:
        border = border[:: max(1, border.shape[0] // sample_limit)]
    # border 是 (N, 3) 的像素列表，不是图像，直接算亮度
    gray = (0.299 * border[:, 0] + 0.587 * border[:, 1] + 0.114 * border[:, 2])
    return int(np.percentile(gray, 75))


def adaptive_white_threshold(arr_rgb: np.ndarray, fixed_threshold: int = 245,
                             margin: int = 8, floor: int = 80) -> int:
    """按纸面底色自适应地给出"算作背景"的灰度阈值。

    固定 245 只对纯白纸面有效：灰底扫描件（实测有页面底边中位灰度 205、
    角点 167）整页都会被判成"有内容"，内容框于是等于整页、去白边失效。
    阈值 = min(固定值, 底色 − margin)，只在底色偏灰时才收紧，纯白纸面上
    结果与原来完全相同。
    """
    bg = estimate_background_level(arr_rgb)
    return int(max(floor, min(fixed_threshold, bg - margin)))


def qimage_to_rgb_array(img: QImage) -> np.ndarray:
    """QImage(RGB888) → (h, w, 3) uint8 视图（跳过行对齐填充）。

    返回的数组直接引用 img 的缓冲区，调用方必须保证 img 在数组使用期间存活。
    """
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()  # 每行字节数（含对齐填充）
    try:
        buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h)
    except (TypeError, ValueError):
        # 极端 Qt 绑定下 constBits() 不支持缓冲协议：退化为一次拷贝
        buf = np.frombuffer(bytes(img.constBits()), dtype=np.uint8, count=bpl * h)
    if bpl == w * 3:
        return buf.reshape((h, w, 3))
    return buf.reshape((h, bpl))[:, :w * 3].reshape((h, w, 3))


def content_bbox_from_qimage(qimg: QImage, white_threshold: int = 245,
                             min_fraction: float = 0.0) -> Optional[Tuple[int, int, int, int]]:
    """直接在 QImage 上检测内容框，返回该 QImage 像素坐标下的框。"""
    if qimg is None or qimg.isNull():
        return None
    img = qimg if qimg.format() == QImage.Format_RGB888 else qimg.convertToFormat(QImage.Format_RGB888)
    arr = qimage_to_rgb_array(img)  # 视图绑定 img，img 在本函数内保持存活
    return find_content_bbox_arr(arr, white_threshold=white_threshold, min_fraction=min_fraction)


def content_box_and_threshold_from_qimage(qimg: QImage, fixed_threshold: int = 245,
                                          min_fraction: float = 0.0):
    """一次性给出 (内容框, 自适应阈值)，供内容框探测路径使用。

    返回的阈值会被同一页后续的"条带收紧"复用，保证显示裁剪与截取收紧用的是
    同一套背景判定。
    """
    if qimg is None or qimg.isNull():
        return None, fixed_threshold
    img = qimg if qimg.format() == QImage.Format_RGB888 else qimg.convertToFormat(QImage.Format_RGB888)
    arr = qimage_to_rgb_array(img)
    thr = adaptive_white_threshold(arr, fixed_threshold)
    box = find_content_bbox_arr(arr, white_threshold=thr, min_fraction=min_fraction)
    return box, thr


def safe_crop_box(box, width, height, min_size=8):
    if box is None:
        left = max(0, (width - min_size) // 2)
        top = max(0, (height - min_size) // 2)
        right = min(width, left + min_size)
        bottom = min(height, top + min_size)
        return (left, top, right, bottom)

    left, top, right, bottom = box

    if right - left < min_size:
        mid = (left + right) // 2
        left = max(0, mid - min_size // 2)
        right = min(width, left + min_size)

    if bottom - top < min_size:
        mid = (top + bottom) // 2
        top = max(0, mid - min_size // 2)
        bottom = min(height, top + min_size)

    left = max(0, left)
    top = max(0, top)
    right = min(width, right)
    bottom = min(height, bottom)

    if right <= left:
        right = min(width, left + min_size)
    if bottom <= top:
        bottom = min(height, top + min_size)

    return (left, top, right, bottom)


class HistoryStore:
    """阅读进度存储。auto_save=False 时只更新内存，由调用方防抖后 save()。"""

    def __init__(self, root_dir: str, file_name: str = HISTORY_FILE_NAME, auto_save: bool = True):
        self.root_dir = root_dir
        self.path = os.path.join(root_dir, file_name)
        self.version = HISTORY_VERSION
        self.auto_save = auto_save
        self.dirty = False
        self.files: Dict[str, Dict[str, Any]] = {}
        self.load()

    def _normalize_record(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return {"last_page": max(1, int(value.get("last_page", 1) or 1))}
        return {"last_page": 1}

    def load(self):
        try:
            if not os.path.isfile(self.path):
                self.files = {}
                self.dirty = False
                return

            with open(self.path, "r", encoding="utf-8") as f:
                obj = json.load(f)

            if isinstance(obj, dict) and isinstance(obj.get("files"), dict):
                self.files = {str(k): self._normalize_record(v) for k, v in obj["files"].items()}
                self.dirty = False
                return

            if isinstance(obj, dict):
                # 旧格式（顶层直接是 路径→记录）：迁移后立即落盘
                self.files = {str(k): self._normalize_record(v)
                              for k, v in obj.items() if k not in ("version", "files")}
                self.dirty = True
                self.save()
                return

            self.files = {}
        except Exception:
            # 文件损坏：保留空集合但不清 dirty，避免把用户进度直接覆盖为空
            log.warning("阅读进度文件损坏，已忽略：%s", self.path, exc_info=True)
            self.files = {}

    def save(self):
        """仅在内存有变化时写盘。"""
        if not self.dirty:
            return
        try:
            os.makedirs(self.root_dir, exist_ok=True)
            payload = {"version": self.version, "files": self.files}
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            self.dirty = False
        except Exception:
            log.warning("保存阅读进度失败：%s", self.path, exc_info=True)

    def _ensure(self, key: str) -> Dict[str, Any]:
        key = os.path.abspath(key)
        if key not in self.files:
            self.files[key] = {"last_page": 1}
        else:
            self.files[key] = self._normalize_record(self.files[key])
        return self.files[key]

    def record_open(self, path: str, page: int):
        rec = self._ensure(path)
        rec["last_page"] = max(1, int(page))
        self.dirty = True
        if self.auto_save:
            self.save()

    def record_progress(self, path: str, page: int):
        if not path:
            return
        self.record_open(path, page)

    def get_last_page(self, path: str, default: int = 1) -> int:
        rec = self.files.get(os.path.abspath(path))
        if not rec:
            return default
        rec = self._normalize_record(rec)
        return max(1, int(rec.get("last_page", default) or default))

    def prune_not_exists(self) -> int:
        """剔除磁盘上已不存在的 PDF 记录，避免记录无限增长。"""
        before = len(self.files)
        self.files = {k: v for k, v in self.files.items() if os.path.isfile(k)}
        removed = before - len(self.files)
        if removed:
            self.dirty = True
            if self.auto_save:
                self.save()
        return removed


class RecentStore:
    """最近打开列表。auto_save=False 时只改内存，由调用方在合适时机 save()。"""

    def __init__(self, root_dir: str, file_name: str = RECENT_FILE_NAME, auto_save: bool = True):
        self.root_dir = root_dir
        self.path = os.path.join(root_dir, file_name)
        self.auto_save = auto_save
        self.dirty = False
        self.order: List[str] = []
        self.load()

    def load(self):
        try:
            if not os.path.isfile(self.path):
                self.order = []
                self.dirty = False
                return
            with open(self.path, "r", encoding="utf-8") as f:
                obj = json.load(f)

            if isinstance(obj, dict) and isinstance(obj.get("recent"), list):
                self.order = [os.path.abspath(str(p)) for p in obj["recent"] if isinstance(p, str)]
            elif isinstance(obj, list):
                self.order = [os.path.abspath(str(p)) for p in obj if isinstance(p, str)]
            else:
                self.order = []
            self.dirty = False
        except Exception:
            log.warning("最近打开记录损坏，已忽略：%s", self.path, exc_info=True)
            self.order = []

    def save(self):
        """仅在内存有变化时写盘。"""
        if not self.dirty:
            return
        try:
            os.makedirs(self.root_dir, exist_ok=True)
            payload = {"version": 1, "recent": self.order}
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            self.dirty = False
        except Exception:
            log.warning("保存最近打开记录失败：%s", self.path, exc_info=True)

    def touch(self, path: str):
        p = os.path.abspath(path)
        self.order = [x for x in self.order if os.path.abspath(x) != p]
        self.order.insert(0, p)
        self.dirty = True
        if self.auto_save:
            self.save()

    def rank(self, path: str) -> int:
        p = os.path.abspath(path)
        try:
            return self.order.index(p)
        except ValueError:
            return 10**9

    def rank_map(self) -> Dict[str, int]:
        """一次性构建 路径→名次 映射。

        快速切换列表每次按键都要给候选排序，逐条调用 rank() 会退化成
        O(候选数 × 最近记录数) 且每条都做一次 abspath。
        """
        return {os.path.abspath(p): i for i, p in enumerate(self.order)}

    def prune_not_exists(self) -> int:
        new_order = [p for p in self.order if os.path.isfile(p)]
        removed = len(self.order) - len(new_order)
        if removed:
            self.order = new_order
            self.dirty = True
            if self.auto_save:
                self.save()
        return removed


class ShortcutListDialog(QDialog):
    STYLE = """
        QHeaderView::section { background: #e8e8e8; font-weight: bold; padding: 4px 8px; border: 1px solid #ccc; }
        QTableWidget { gridline-color: #ddd; }
    """

    def __init__(self, shortcut_spec, parent=None):
        super().__init__(parent)
        self.setWindowTitle("快捷键列表")
        self.resize(520, 500)
        layout = QVBoxLayout(self)

        tbl = QTableWidget(len(shortcut_spec), 2)
        tbl.setHorizontalHeaderLabels(["按键", "功能"])
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        tbl.setSelectionBehavior(QTableWidget.SelectRows)
        tbl.verticalHeader().setVisible(False)
        tbl.setStyleSheet(self.STYLE)

        for row, (key, name, desc, _cb) in enumerate(shortcut_spec):
            key_str = key if isinstance(key, str) else self._describe_key(key)
            item_key = QTableWidgetItem(key_str)
            item_key.setFont(QFont("Consolas", 10, QFont.Bold))
            item_name = QTableWidgetItem(f"{name} — {desc}")
            tbl.setItem(row, 0, item_key)
            tbl.setItem(row, 1, item_name)

        tbl.resizeColumnsToContents()
        tbl.setColumnWidth(1, tbl.columnWidth(1) + 40)

        layout.addWidget(tbl)
        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        layout.addWidget(btn)

    @staticmethod
    def _describe_key(key_qt) -> str:
        mapping = {
            Qt.Key_Left:   "← (Left)",
            Qt.Key_Right:  "→ (Right)",
            Qt.Key_Up:     "↑ (Up)",
            Qt.Key_Down:   "↓ (Down)",
            Qt.Key_Home:   "Home",
            Qt.Key_End:    "End",
            Qt.Key_Escape: "Esc",
            Qt.Key_Return: "Enter",
            Qt.Key_Enter:  "小键盘 Enter",
            Qt.Key_Space:  "Space",
            Qt.Key_Backspace: "Backspace",
            Qt.Key_Delete: "Delete",
        }
        # 没有映射的键以前直接 str() 出枚举数字（16777220 之类），这里兜底成人能读的形式
        return mapping.get(key_qt, QKeySequence(key_qt).toString() or str(key_qt))


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("使用说明")
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(
            "PDF 阅读器使用说明\n"
            "================\n\n"
            "1. 打开文件\n"
            "2. 快速切换 PDF\n"
            "   - 按“最近打开”顺序优先显示（最新在前）\n"
            "   - 再次打开同一文件会移动到最前\n"
            "3. 命令面板\n"
            "4. 翻页\n"
            "5. 跳页\n"
            "6. 缩放 / 阅读姿态\n"
            "   - Ctrl+0 适合页面；Ctrl+1 适合宽度；Ctrl+2 原始大小\n"
            "   - Ctrl+滚轮缩放（连续滚动只渲染一次）\n"
            "   - 放大后按住左键拖动平移；双击在放大/适合宽度之间切换\n"
            "7. 截取（循环模式）\n"
            "   - Ctrl+S 进入截取模式；进入后可循环截取多次\n"
            "   - 鼠标移动定位，按回车确认线\n"
            "   - 第一条线为起始线，第二条线为结束线\n"
            "   - 每次截取完成后自动清空分割线，可立即开始下一次\n"
            "   - Esc 仅撤销分割线，不会退出截取模式\n"
            "   - 再次按 Ctrl+S（截取模式）才退出\n"
            "   - 输出不超过该页原图分辨率（扫描件不被插值放大），结果复制到剪贴板\n"
            "   - 跨页截取会自动纵向拼接\n"
            "8. 阅读进度\n"
            "   - 自动记录最后阅读页码\n"
            "   - 下次打开自动恢复\n"
            "   - 不再记录打开次数\n"
            "9. 自动裁剪渲染（默认关闭）\n"
            "   - 开启后渲染前自动切除白边与空白间距\n"
            "   - 含扫描墨点过滤与灰底自适应，Ctrl+Shift+R 或命令面板中开关\n"
        )
        layout.addWidget(text)
        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        layout.addWidget(btn)


class BookmarkCopyDialog(QDialog):
    """书签复制格式选择：← → 切换，Enter 确认，Esc 取消。"""

    CHOICES = [
        ("md-h1", "MD # 标题",     "顶层为 #"),
        ("md-h2", "MD ## 子标题",  "顶层为 ##"),
        ("ul",    "无序列表",       "缩进 - 列表"),
    ]
    HIGHLIGHT = "background: #2980b9; color: #fff; border-radius: 6px; font-weight: bold;"
    NORMAL    = "background: #eee; border-radius: 6px;"

    def __init__(self, toc, parent=None):
        super().__init__(parent)
        self.toc = toc
        self.idx = 0
        self.setWindowTitle("选择书签格式")
        self.setFixedSize(480, 200)

        root = QVBoxLayout(self)
        hint = QLabel("← → 切换格式    Enter 确认复制")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color: #888;")
        root.addWidget(hint)

        bar = QHBoxLayout()
        bar.setSpacing(12)
        self.cards = []
        for mode, title, desc in self.CHOICES:
            card = QVBoxLayout()
            lb = QLabel(title)
            lb.setAlignment(Qt.AlignCenter)
            lb.setMinimumHeight(36)
            lb.setStyleSheet(self.NORMAL)
            card.addWidget(lb)
            sub = QLabel(desc)
            sub.setAlignment(Qt.AlignCenter)
            sub.setStyleSheet("color: #666;")
            card.addWidget(sub)
            bar.addLayout(card)
            self.cards.append(lb)
        root.addLayout(bar)

        self._refresh_highlight()

    def _refresh_highlight(self):
        for i, card in enumerate(self.cards):
            if i == self.idx:
                card.setStyleSheet(self.HIGHLIGHT + "font-size: 15px;")
            else:
                card.setStyleSheet(self.NORMAL)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.idx = (self.idx - 1) % len(self.CHOICES)
            self._refresh_highlight()
        elif event.key() == Qt.Key_Right:
            self.idx = (self.idx + 1) % len(self.CHOICES)
            self._refresh_highlight()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()
        elif event.key() == Qt.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    def selected(self) -> str:
        return self.CHOICES[self.idx][0]


class QuickSwitchDialog(QDialog):
    def __init__(self, pdf_list: List[PdfEntry], recent: RecentStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("快速切换 PDF")
        self.resize(860, 560)
        self.pdf_list = pdf_list
        self.recent = recent
        self.selected_path: Optional[str] = None

        layout = QVBoxLayout(self)

        self.input = QLineEdit()
        self.input.setPlaceholderText("输入部分文件名")
        layout.addWidget(self.input)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)

        row = QHBoxLayout()
        self.open_btn = QPushButton("打开")
        self.cancel_btn = QPushButton("取消")
        row.addStretch(1)
        row.addWidget(self.open_btn)
        row.addWidget(self.cancel_btn)
        layout.addLayout(row)

        self.input.textChanged.connect(self.update_list)
        self.list_widget.itemClicked.connect(self.accept_selection)
        self.list_widget.itemDoubleClicked.connect(self.accept_selection)
        self.open_btn.clicked.connect(self.accept_selection)
        self.cancel_btn.clicked.connect(self.reject)
        self.input.returnPressed.connect(self.accept_selection)

        self.update_list("")
        self.input.setFocus()

    def update_list(self, text: str):
        self.list_widget.clear()
        q = text.strip().lower()

        # 每次按键都重建候选：名次映射只建一次，避免逐条 rank() 退化成 O(N²)
        ranks = self.recent.rank_map()
        items = []
        for entry in self.pdf_list:
            name_l = entry.name.lower()
            if not q or q in name_l:
                prefix_score = 0 if (q and name_l.startswith(q)) else 1
                recent_rank = ranks.get(os.path.abspath(entry.path), 10 ** 9)
                items.append((prefix_score, recent_rank, name_l, entry.name, entry.path))

        items.sort(key=lambda x: (x[0], x[1], x[2]))

        for _, _, _, name, path in items[:300]:
            item = QListWidgetItem(name)
            item.setToolTip(path)
            item.setData(Qt.UserRole, path)
            self.list_widget.addItem(item)

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def keyPressEvent(self, event):
        # 输入框有焦点时，↑/↓ 改为移动列表选中项（而非移动光标）
        if event.key() in (Qt.Key_Up, Qt.Key_Down) and self.input.hasFocus():
            total = self.list_widget.count()
            if total > 0:
                row = self.list_widget.currentRow()
                step = 1 if event.key() == Qt.Key_Down else -1
                self.list_widget.setCurrentRow(max(0, min(total - 1, row + step)))
            return
        # 列表有焦点时，回车直接打开
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.list_widget.hasFocus():
            self.accept_selection()
            return
        super().keyPressEvent(event)

    def accept_selection(self):
        item = self.list_widget.currentItem()
        if item:
            self.selected_path = item.data(Qt.UserRole)
            self.accept()


class JumpPageDialog(QDialog):
    def __init__(self, total_pages: int, current_page: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("跳页")
        self.resize(320, 120)
        self.page: Optional[int] = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"请输入页码（1 - {total_pages}）："))
        self.spin = QSpinBox()
        self.spin.setRange(1, max(1, total_pages))
        self.spin.setValue(current_page if total_pages > 0 else 1)
        layout.addWidget(self.spin)
        row = QHBoxLayout()
        ok_btn = QPushButton("确定")
        cancel_btn = QPushButton("取消")
        row.addStretch(1)
        row.addWidget(ok_btn)
        row.addWidget(cancel_btn)
        layout.addLayout(row)
        ok_btn.clicked.connect(self.accept_page)
        cancel_btn.clicked.connect(self.reject)
        QTimer.singleShot(0, self._select_all)

    def _select_all(self):
        line = self.spin.lineEdit()
        if line:
            line.selectAll()
            line.setFocus()

    def accept_page(self):
        self.page = self.spin.value()
        self.accept()


class CommandDialog(QDialog):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("命令")
        self.resize(860, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("点击命令即可执行"))
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)
        for name, desc, callback, key in actions:
            display = f"{name}      - {desc}" + (f" [{key}]" if key else "")
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, callback)
            self.list_widget.addItem(item)
        self.list_widget.itemDoubleClicked.connect(self.run_selected)
        self.list_widget.itemClicked.connect(self.run_selected)

    def run_selected(self, item):
        if not item:
            return
        callback = item.data(Qt.UserRole)
        self.accept()
        if callback:
            callback()


class BookmarkPanel(QWidget):
    def __init__(self, on_jump_to_page, on_nav_bookmark=None, parent=None):
        super().__init__(parent)
        self.on_jump_to_page = on_jump_to_page
        self.on_nav_bookmark = on_nav_bookmark
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.title = QLabel("书签")
        self.title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.title)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemClicked.connect(self.handle_click)
        # 只接一个信号：itemClicked / itemDoubleClicked / currentItemChanged 同时接，
        # 一次点击会触发 2~3 次跳页（每次都是重渲染 + 写进度）
        self.tree.installEventFilter(self)
        layout.addWidget(self.tree, 1)

    def eventFilter(self, obj, event):
        # 上下键交给主窗口，按“当前页”前后书签跳转，而非在树内移动选择
        if (self.on_nav_bookmark is not None and obj is self.tree
                and event.type() == QEvent.KeyPress):
            if event.key() == Qt.Key_Up:
                self.on_nav_bookmark(-1)
                return True
            if event.key() == Qt.Key_Down:
                self.on_nav_bookmark(1)
                return True
        return super().eventFilter(obj, event)

    def load_bookmarks(self, toc):
        self.tree.clear()
        if not toc:
            item = QTreeWidgetItem(["无书签"])
            self.tree.addTopLevelItem(item)
            return
        stack = []
        for level, title, page, _ in toc:
            item = QTreeWidgetItem([f"{title}  [{page}]"])
            item.setData(0, Qt.UserRole, page)
            while stack and stack[-1][0] >= level:
                stack.pop()
            if not stack:
                self.tree.addTopLevelItem(item)
            else:
                stack[-1][1].addChild(item)
            stack.append((level, item))
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)

    def handle_click(self, item, column):
        page = item.data(0, Qt.UserRole)
        if page:
            self.on_jump_to_page(page)


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
        改成先映射到共同祖先（视口）再做差。
        """
        label = self.viewer.image_label
        pixmap = label.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        pm_dpr = pixmap.devicePixelRatio() or 1.0
        pm_w = pixmap.width() / pm_dpr
        pm_h = pixmap.height() / pm_dpr
        viewport = self.viewer.scroll.viewport()
        origin = label.mapTo(viewport, QPoint(0, 0)) - self.mapTo(viewport, QPoint(0, 0))
        x0 = origin.x() + (label.width() - pm_w) / 2.0
        y0 = origin.y() + (label.height() - pm_h) / 2.0
        return x0, y0, pm_w, pm_h

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
            painter.setBrush(self.COVER_COLOR)
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

        # 重渲染防抖：滚轮连续缩放 / 窗口拖拽尺寸变化时合并为一次渲染
        self._pending_render = QTimer(self)
        self._pending_render.setSingleShot(True)
        self._pending_render.timeout.connect(self.render_current_page)

        # 拖拽平移
        self._drag_origin: Optional[QPoint] = None
        self._drag_scroll: Tuple[int, int] = (0, 0)

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
                self.auto_crop_white_threshold, self.auto_crop_min_size)

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

        exact=True：无损截取路径，精确输出、不超采样、不设 DPR。
        exact=False：显示路径，按 z*ss*dpr 渲染，再平滑缩回逻辑尺寸并设置 DPR。
        """
        render_z = z if exact else z * ss * dpr
        key = (
            self.current_path,
            page_index,
            round(render_z, 4),
            exact,
            self.view_mode,
            self.auto_crop_enabled,
            self.auto_crop_white_threshold,
            self.auto_crop_min_size,
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
        return (f"{os.path.basename(self.current_path)} | 第 {self.current_page_1_based()} / "
                f"{self.total_pages()} 页 | {fit_text} | {crop_text}")

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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python PDF Reader")
        self.resize(1280, 920)

        # 使窗口居中于可用屏幕区域内，避免被任务栏遮挡，顺便隐藏顶部框
        screen_geom = QApplication.primaryScreen().availableGeometry()
        w = min(self.width(), screen_geom.width())
        h = min(self.height(), screen_geom.height())
        x = screen_geom.x() + (screen_geom.width() - w) // 2
        y = screen_geom.y() + (screen_geom.height() - h) // 2
        self.setGeometry(x, y, w, h)

        # 存储只改内存，落盘由 _save_timer 防抖统一触发（翻页不再每次写盘）
        self.history = HistoryStore(STATE_DIR, auto_save=False)
        self.recent = RecentStore(STATE_DIR, auto_save=False)
        self.history.prune_not_exists()
        self.recent.prune_not_exists()

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(STATE_SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._flush_state)

        self.pdf_entries = self.scan_pdf_files(PDF_ROOT_DIR)

        self.status = self.statusBar()
        self.viewer = PdfViewer(
            status_callback=self.status.showMessage,
            page_changed_callback=self.on_page_changed,
        )

        self.bookmark_panel = BookmarkPanel(self.jump_to_page, self._nav_bookmark)

        self.splitter = QSplitter()
        self.splitter.addWidget(self.bookmark_panel)
        self.splitter.addWidget(self.viewer)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 4)
        self.splitter.setSizes([320, 960])

        self._build_ui()
        self._setup_shortcuts()

        if INITIAL_PDF_KEYWORD:
            p = self.pick_pdf_by_keyword(INITIAL_PDF_KEYWORD)
            if p:
                self.open_pdf(p)
        elif self.pdf_entries:
            self.open_pdf(self.pdf_entries[0].path)

    def scan_pdf_files(self, root_dir: str) -> List[PdfEntry]:
        entries = []
        if not os.path.isdir(root_dir):
            return entries
        for base, _, files in os.walk(root_dir):
            for fn in files:
                if fn.lower().endswith(".pdf"):
                    entries.append(PdfEntry(fn, os.path.join(base, fn)))
        entries.sort(key=lambda x: x.name.lower())
        return entries

    def pick_pdf_by_keyword(self, keyword: str) -> Optional[str]:
        q = keyword.lower().strip()
        matches = [e for e in self.pdf_entries if q in e.name.lower()]
        if not matches:
            return None
        matches.sort(key=lambda e: (0 if e.name.lower().startswith(q) else 1, self.recent.rank(e.path), e.name.lower()))
        return matches[0].path

    def _build_ui(self):
        self.btn_help = QPushButton("使用帮助")
        self.btn_open = QPushButton("打开 PDF")
        self.btn_command = QPushButton("命令")
        self.btn_help.clicked.connect(self.show_help)
        self.btn_open.clicked.connect(self.open_dialog)
        self.btn_command.clicked.connect(self.open_command_dialog)
        self.btn_help.setFocusPolicy(Qt.NoFocus)
        self.btn_open.setFocusPolicy(Qt.NoFocus)
        self.btn_command.setFocusPolicy(Qt.NoFocus)
        self.setCentralWidget(self.splitter)

    # ---- 快捷键盘集中定义 ----
    # 格式: (按键, 名称, 描述, 回调)
    # 只需修改这里，快捷键绑定和命令面板自动同步
    SKEY = [
        (Qt.Key_Left,      "上一页",        "翻到上一页",                    "viewer.prev_page"),
        (Qt.Key_Right,     "下一页",        "翻到下一页",                    "viewer.next_page"),
        ("Ctrl+O",         "快速切换 PDF",  "按文件名筛选切换(按最近打开排序)", "quick_switch_dialog"),
        ("Ctrl+I",         "跳页",          "输入页码跳转",                  "jump_dialog"),
        ("Ctrl+P",         "命令面板",      "打开命令面板",                  "open_command_dialog"),
        ("Ctrl+S",         "截取模式",      "进入循环截取模式；再次按下退出", "toggle_crop_mode"),
        ("Ctrl+D",         "遮挡模式",      "纯色遮挡光标下方区域",          "toggle_cover_mode"),
        ("Home",           "回到首页",      "跳到第一页",                    "jump_to_page_1"),
        ("End",            "跳到末页",      "跳到最后一页",                  "jump_to_last_page"),
        ("Ctrl+-",         "缩小",          "页面缩放缩小",                  "viewer.zoom_out"),
        ("Ctrl+=",         "放大",          "页面缩放放大",                  "viewer.zoom_in"),
        ("Ctrl+0",         "适合页面",      "整页适配窗口",                  "viewer.fit_page"),
        ("Ctrl+1",         "适合宽度",      "宽度铺满窗口(大页面/小字扫描件)", "viewer.fit_width"),
        ("Ctrl+2",         "原始大小",      "缩放到 100%",                   "viewer.zoom_to_100"),
        (Qt.Key_Up,        "上一书签",      "跳到当前页之前的书签",          "_nav_bookmark_up"),
        (Qt.Key_Down,      "下一书签",      "跳到当前页之后的书签",          "_nav_bookmark_down"),
        (Qt.Key_Return,    "确认分割线",    "截取/遮挡模式：确认当前分割线（不依赖焦点）", "crop_enter"),
        ("Esc",            "撤销/退出遮挡", "撤销分割线（截取模式不退出）或退出遮挡模式", "cancel_crop_mode"),
        ("Ctrl+Shift+C",   "复制路径",      "复制当前PDF路径到剪贴板",       "copy_current_path"),
        ("Ctrl+Shift+B",   "复制书签",      "复制当前PDF书签到剪贴板",       "copy_pdf_bookmarks"),
        ("F5",             "刷新文件",      "重新扫描PDF目录",               "refresh_files"),
        ("Ctrl+Shift+R",   "自动裁剪渲染",  "开关渲染前去白边/空白间距",     "toggle_auto_crop_mode"),
        ("Ctrl+/",         "快捷键列表",    "查看所有快捷键",                "show_shortcuts"),
    ]

    # 无快捷键的命令扩展 (名称, 描述, 回调)
    EXTRA_COMMANDS = [
        ("打开文件",        "打开任意目录的PDF 文件",                    "open_dialog"),
        ("适合页面",        "整页适配窗口",                     "viewer.fit_page"),
        ("适合宽度",        "宽度铺满窗口，适合大页面/小字扫描件", "viewer.fit_width"),
        ("原始大小",        "缩放到 100%",                      "viewer.zoom_to_100"),
        ("适应页面开关",    "在 适合页面/适合宽度 之间切换",     "toggle_fit_page_mode"),
        ("显示/隐藏书签栏", "切换左侧书签面板",                 "toggle_bookmark_panel"),
        ("使用帮助",        "查看使用说明",                     "show_help"),
    ]

    def _resolve(self, name: str):
        """将字符串名称解析为 MainWindow 的方法或属性。

        旧实现直接用 getattr，SKEY/EXTRA_COMMANDS 里写错一个名字只会在启动时
        抛出难懂的 AttributeError；这里补上明确的上下文。
        """
        if name == "jump_to_page_1":
            return lambda: self.jump_to_page(1)
        try:
            if "." in name:
                obj_name, attr = name.rsplit(".", 1)
                return getattr(getattr(self, obj_name), attr)
            return getattr(self, name)
        except AttributeError as e:
            raise AttributeError(f"快捷键/命令表里的回调名无效：{name!r}") from e

    def _register_shortcut(self, key, callback):
        key_seq = key if isinstance(key, Qt.Key) else QKeySequence(key)
        QShortcut(key_seq, self, activated=callback)

    def _setup_shortcuts(self):
        for key, _name, _desc, cb_name in self.SKEY:
            self._register_shortcut(key, self._resolve(cb_name))
        # 小键盘回车与主回车等价（SKEY 里只登记主回车）
        self._register_shortcut(Qt.Key_Enter, self.crop_enter)

    _NAV_NAMES = {"上一页", "下一页", "回到首页", "跳到末页", "上一书签", "下一书签"}

    def get_command_actions(self):
        # 1) 无快捷键命令优先
        actions = [(name, desc, self._resolve(cb_name), None)
                   for name, desc, cb_name in self.EXTRA_COMMANDS]

        nav, rest = [], []
        for key, name, desc, cb_name in self.SKEY:
            key_str = None if isinstance(key, Qt.Key) else str(key)
            entry = (name, desc, self._resolve(cb_name), key_str)
            (nav if name in self._NAV_NAMES else rest).append(entry)

        # 2) 有快捷键的一般命令
        actions.extend(rest)
        # 3) 翻页/导航命令放到最后
        actions.extend(nav)
        return actions

    def crop_enter(self):
        """回车确认分割线：不依赖焦点，覆盖层丢焦点时也能用。

        跳页/命令面板等模态对话框关闭后焦点可能变成 None，回车事件就到不了
        覆盖层；这条窗口级快捷键是兜底，人在截取/遮挡模式时直接转给覆盖层处理。
        """
        overlay = self.viewer.crop_overlay
        if overlay.active:
            overlay.handle_enter()

    def restore_crop_focus(self):
        """把键盘焦点还给截取/遮挡覆盖层（模态对话框关闭后调用）。"""
        if not self.viewer.crop_overlay.active:
            return
        self.activateWindow()          # 对话框关闭后把主窗口激活回来
        self.viewer._focus_crop_overlay()

    def open_command_dialog(self):
        CommandDialog(self.get_command_actions(), self).exec()
        self.restore_crop_focus()

    def copy_current_path(self):
        if not self.viewer.current_path:
            self.status.showMessage("当前没有打开 PDF")
            return
        path = os.path.abspath(self.viewer.current_path)
        QGuiApplication.clipboard().setText(path)
        self.status.showMessage(f"已复制路径：{path}")

    @staticmethod
    def _fmt_toc_ul(toc):
        lines = []
        for item in toc:
            if len(item) >= 3:
                level = max(0, int(item[0]) - 1)
                lines.append(f"{'  ' * level}- {item[1]}")
        return "\n".join(lines).strip()

    @staticmethod
    def _fmt_toc_md(toc, offset: int):
        """offset=0 → 顶层 # , offset=1 → 顶层 ##"""
        lines = []
        for item in toc:
            if len(item) >= 3:
                level = max(0, int(item[0]) - 1)
                prefix = "#" * (level + 1 + offset)
                lines.append(f"{prefix} {item[1]}")
        return "\n".join(lines).strip()

    def copy_pdf_bookmarks(self):
        if not self.viewer.doc:
            self.status.showMessage("当前没有打开 PDF")
            return

        toc = self.viewer.load_bookmarks()
        if not toc:
            self.status.showMessage("当前 PDF 没有书签")
            return

        dlg = BookmarkCopyDialog(toc, self)
        if dlg.exec() != QDialog.Accepted:
            return

        mode = dlg.selected()
        if mode == "md-h1":
            text = self._fmt_toc_md(toc, 0)
            tag = "MD 顶层 #"
        elif mode == "md-h2":
            text = self._fmt_toc_md(toc, 1)
            tag = "MD 顶层 ##"
        else:
            text = self._fmt_toc_ul(toc)
            tag = "无序列表"

        if not text:
            self.status.showMessage("书签为空")
            return

        QGuiApplication.clipboard().setText(text)
        self.status.showMessage(f"已复制书签 [{tag}]（{text.count(chr(10)) + 1} 条）")

    def refresh_files(self):
        current_path = self.viewer.current_path
        current_page = self.viewer.current_page_1_based()
        current_zoom = self.viewer.zoom

        self.pdf_entries = self.scan_pdf_files(PDF_ROOT_DIR)
        # 先落盘再重新读取，否则未写出的内存改动会被 load() 覆盖
        self._flush_state()
        self.history.load()
        self.recent.load()
        self.history.prune_not_exists()
        self.recent.prune_not_exists()

        self.status.showMessage(f"已刷新：扫描到 {len(self.pdf_entries)} 个 PDF")
        if current_path:
            if os.path.isfile(current_path):
                # 旧实现先 open_pdf（渲染一次）再设 zoom、清缓存、再渲染一次，
                # 一次刷新等于 2 次翻页的开销；先设好缩放再打开只需渲染一次
                self.viewer.zoom = current_zoom
                self.viewer.open_pdf(current_path, current_page)
                self.refresh_bookmarks()
            else:
                if self.pdf_entries:
                    self.open_pdf(self.pdf_entries[0].path)
                else:
                    self.viewer.clear_pdf()
                    self.refresh_bookmarks()
        elif self.pdf_entries and not self.viewer.doc:
            self.open_pdf(self.pdf_entries[0].path)

    def cancel_crop_mode(self):
        """Esc：遮挡模式直接退出；截取模式为循环模式，只撤销分割线。

        截取模式的唯一退出方式是再次触发“截取模式”指令（Ctrl+S）。
        """
        if self.viewer.crop_overlay.cover_mode:
            self.viewer.stop_cover_mode()
            self.status.showMessage("已退出遮挡模式")
            return
        if self.viewer.crop_mode:
            overlay = self.viewer.crop_overlay
            if overlay.bottom_marker is not None:
                self.viewer.set_crop_marks(overlay.top_marker, None)
                self.status.showMessage("已撤销结束线（截取模式继续，Ctrl+S 退出）")
            elif overlay.top_marker is not None:
                self.viewer.set_crop_marks(None, None)
                self.status.showMessage("已撤销起始线（截取模式继续，Ctrl+S 退出）")
            else:
                self.status.showMessage("截取模式进行中：再次按 Ctrl+S 退出")

    def toggle_crop_mode(self):
        if not self.viewer.doc:
            return
        if self.viewer.crop_mode:
            self.viewer.stop_crop_mode()
            self.status.showMessage("已退出截取模式")
        else:
            self.viewer.set_crop_mode(True)
            self.status.showMessage(
                "截取模式：移动鼠标定位，回车确认分割线；可循环多次截取，再次按 Ctrl+S 退出"
            )

    def toggle_cover_mode(self):
        if not self.viewer.doc:
            return
        if self.viewer.crop_overlay.cover_mode:
            self.viewer.stop_cover_mode()
            self.status.showMessage("已退出遮挡模式")
        else:
            self.viewer.set_cover_mode(True)
            self.status.showMessage("遮挡模式：移动鼠标定位，按回车确认/取消遮挡线；Esc 退出")

    def toggle_fit_page_mode(self):
        if self.viewer.doc:
            self.viewer.toggle_fit_page_mode()

    def toggle_auto_crop_mode(self):
        if not self.viewer.doc:
            return
        self.viewer.toggle_auto_crop_enabled()
        # 复用 viewer 的状态栏文本，避免在两处各写一遍"自动裁剪 xx%"
        self.status.showMessage(self.viewer.status_text())

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开 PDF", PDF_ROOT_DIR, "PDF Files (*.pdf)")
        if path:
            self.open_pdf(path)

    def show_help(self):
        HelpDialog(self).exec()
        self.restore_crop_focus()

    def show_shortcuts(self):
        ShortcutListDialog(self.SKEY, self).exec()
        self.restore_crop_focus()

    def quick_switch_dialog(self):
        dlg = QuickSwitchDialog(self.pdf_entries, self.recent, self)
        if dlg.exec() == QDialog.Accepted and dlg.selected_path:
            self.open_pdf(dlg.selected_path)
        self.restore_crop_focus()

    def jump_dialog(self):
        total = self.viewer.total_pages()
        if total <= 0:
            return
        dlg = JumpPageDialog(total, self.viewer.current_page_1_based(), self)
        if dlg.exec() == QDialog.Accepted and dlg.page:
            self.jump_to_page(dlg.page)
        # 模态对话框会拿走键盘焦点：把它还给截取/遮挡覆盖层，
        # 否则"跳页后回车没反应"（实测焦点会变成 None）
        self.restore_crop_focus()

    def open_pdf(self, path: str, explicit_page: Optional[int] = None):
        self.viewer.stop_crop_mode()
        self.viewer.stop_cover_mode()
        abs_path = os.path.abspath(path)
        # 没有历史记录时用配置的初始页（此前 INITIAL_PAGE 定义了却从未被读取）
        page = self.history.get_last_page(abs_path, INITIAL_PAGE) if explicit_page is None else explicit_page

        if not self.viewer.open_pdf(abs_path, page):
            # 打开失败：不记历史，也不让打不开的文件污染"最近打开"
            return
        self.refresh_bookmarks()

        self.history.record_open(abs_path, page)
        self.recent.touch(abs_path)
        self._schedule_state_save()

        self.status.showMessage(f"已打开：{os.path.basename(abs_path)} | 第 {page} 页")

    def _flush_state(self):
        """把阅读进度/最近打开记录写盘（防抖超时或退出前调用）。"""
        self._save_timer.stop()
        self.history.save()
        self.recent.save()

    def _schedule_state_save(self):
        self._save_timer.start()

    def closeEvent(self, event):
        self._flush_state()
        super().closeEvent(event)

    def on_page_changed(self, path: str, page_1_based: int):
        self.history.record_progress(path, page_1_based)
        self._schedule_state_save()

    def jump_to_page(self, page_1_based: int):
        self.viewer.goto_page(page_1_based)

    def _nav_bookmark(self, direction: int):
        """相对当前页，跳到前/后一书签。direction=-1 前一，+1 后一。"""
        toc = self.viewer.load_bookmarks()
        if not toc:
            self.status.showMessage("当前 PDF 没有书签")
            return
        pages = sorted({int(item[2]) for item in toc if len(item) >= 3 and item[2]})
        if not pages:
            return
        cur = self.viewer.current_page_1_based()
        # 二分查找：旧实现每次按键都要扫全表并多次 pages.index(cur)
        if direction < 0:
            i = bisect.bisect_left(pages, cur)
            target = pages[i - 1] if i > 0 else None
        else:
            i = bisect.bisect_right(pages, cur)
            target = pages[i] if i < len(pages) else None
        if target is not None:
            self.jump_to_page(target)

    def _nav_bookmark_up(self):
        self._nav_bookmark(-1)

    def _nav_bookmark_down(self):
        self._nav_bookmark(1)

    def jump_to_last_page(self):
        total = self.viewer.total_pages()
        if total > 0:
            self.jump_to_page(total)

    def refresh_bookmarks(self):
        self.bookmark_panel.load_bookmarks(self.viewer.load_bookmarks())

    def toggle_bookmark_panel(self):
        self.bookmark_panel.setVisible(not self.bookmark_panel.isVisible())
        if self.bookmark_panel.isVisible():
            self.refresh_bookmarks()


def main():
    app = QApplication(sys.argv)
    icon = QIcon(ICON_PATH) if ICON_PATH else QIcon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    win = MainWindow()
    if not icon.isNull():
        win.setWindowIcon(icon)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()