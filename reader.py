"""Python PDF Reader 入口。

代码按职责拆在同目录模块里：
    app_config.py   配置、常量、运行库兼容（必须最先导入）
    imaging.py      图像处理与内容框检测
    stores.py       阅读进度 / 最近打开 / 截取暂存
    ui_panels.py    对话框与侧栏面板
    viewer.py       阅读区（渲染、缩放、截取、预取）
    main_window.py  主窗口

本文件只负责启动，并把公开名字重新导出，保持
`import reader; reader.PdfViewer` 这类旧用法可用。
运行方式不变：python reader.py
"""
import sys

# 先导入 app_config：它负责读配置，并在 Windows 上补 DLL 搜索路径，
# 必须早于任何 import fitz（PyMuPDF 2.x 在导入阶段会加载 onnxruntime）。
from app_config import (
    log,
    APP_DIR,
    ASSETS_DIR,
    CONFIG,
    CONFIG_PATH,
    load_config,
    _cfg,
    _cfg_bool,
    default_downloads_dir,
    PDF_ROOT_DIR,
    STATE_DIR,
    INITIAL_PAGE,
    INITIAL_PDF_KEYWORD,
    HISTORY_FILE_NAME,
    RECENT_FILE_NAME,
    resolve_notes_path,
    NOTES_PATH,
    NOTES_SAVE_DEBOUNCE_MS,
    NOTES_MAX_BYTES,
    CAPTURES_DIR,
    CAPTURES_MAX,
    MAX_TABS,
    CAPTURE_THUMB_WIDTH,
    CAPTURE_MAX_BYTES,
    ICON_PATH,
    HISTORY_VERSION,
    AUTO_CROP_DEFAULT,
    AUTO_CROP_WHITE_THRESHOLD,
    AUTO_CROP_MIN_SIZE,
    INVERT_DEFAULT,
    INVERT_ENABLED,
    CROP_HIGH_RES_DPI,
    PDF_BASE_DPI,
    CAPTURE_MIN_SCALE,
    MAX_CAPTURE_PAGES,
    MAX_CAPTURE_PIXELS,
    SS_SMALL_ZOOM,
    PAGE_CACHE_MAX,
    PAGE_CACHE_MAX_BYTES,
    CONTENT_PROBE_TARGET_PX,
    CONTENT_PROBE_MIN_ZOOM,
    CONTENT_BBOX_CHUNK_ROWS,
    CONTENT_NOISE_MIN_FRACTION,
    MAX_TRIM_PIXELS,
    RENDER_DEBOUNCE_MS,
    STATE_SAVE_DEBOUNCE_MS,
    PREFETCH_PAGES,
    FIT_WIDTH_MAX_ZOOM,
    VIEW_FIT_PAGE,
    VIEW_FIT_WIDTH,
    VIEW_MANUAL,
    PdfEntry,
)
from imaging import (
    rgb_to_gray,
    content_row_col_hits,
    find_content_bbox_arr,
    estimate_background_level,
    adaptive_white_threshold,
    qimage_to_rgb_array,
    invert_qimage,
    content_bbox_from_qimage,
    content_box_and_threshold_from_qimage,
    safe_crop_box,
)
from stores import (
    HistoryStore,
    RecentStore,
    CaptureStore,
)
from ui_panels import (
    ShortcutListDialog,
    HelpDialog,
    BookmarkCopyDialog,
    QuickSwitchDialog,
    JumpPageDialog,
    CommandDialog,
    BookmarkPanel,
    NotesPanel,
    CapturePanel,
)
from viewer import (
    CropOverlay,
    PdfViewer,
)
from main_window import MainWindow

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication


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
