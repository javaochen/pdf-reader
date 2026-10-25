"""页面图像处理：纸面底色估计、内容框（去白边）探测、QImage 与 numpy 互转。

纯函数，不持有状态，可单独测试。
"""

from typing import Optional
from typing import Tuple

from app_config import CONTENT_BBOX_CHUNK_ROWS

import numpy as np
from PySide6.QtGui import QImage


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


def invert_qimage(img: QImage) -> QImage:
    """原地反色（每个通道取 255−x），保留 alpha。

    只用于**显示路径**：截取输出必须保持原样，否则发给 AI 的图会变成黑底白字。
    调用点都放在缩放下采样之后，这样处理的像素数最少。
    """
    if img is None or img.isNull():
        return img
    img.invertPixels(QImage.InvertRgb)
    return img


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
