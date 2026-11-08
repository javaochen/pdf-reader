"""配置、常量与运行库兼容处理（不依赖 PySide6 / PyMuPDF）。

任何模块都应先 import 本模块：它在导入时读取 reader_config.json，
并在 Windows 上把更新版 MSVC 运行库加入 DLL 搜索路径，
必须早于任何 `import fitz` 发生（否则 PyMuPDF 2.x 会在导入阶段崩溃）。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
import json
import logging
import os
import sys


# ========== 日志 ==========
# 默认只输出 warning 以上；set PDF_READER_LOG=DEBUG 可看到被捕获异常的细节。
# 定义放在第三方库导入之前：导入阶段出问题（下面这类运行库崩溃）也能报出来。
log = logging.getLogger("pdf_reader")
if os.environ.get("PDF_READER_LOG"):
    logging.basicConfig(
        level=os.environ["PDF_READER_LOG"].upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ========== 第三方库导入前的运行库兼容处理 ==========
# add_dll_directory 返回的句柄必须保持引用，否则该目录会被撤销
_MSVC_DLL_HANDLE = None


def _preload_newer_msvc_runtime() -> None:
    """把 shiboken6 自带的 MSVC 运行库加入 DLL 搜索路径（必须在 import fitz 之前）。

    2026-10 本机实测的启动崩溃：系统 System32 里的 MSVCP140.dll 只有 14.36，
    而 PyMuPDF 2.x 在 import 时会自动加载 pymupdf4llm / pymupdf.layout，
    进而导入用新版 MSVC 编译的 onnxruntime；旧运行库缺实现，进程直接
    access violation（事件日志 faulting module = MSVCP140.dll, 0xc0000005），
    外部表现就是「双击没反应 / 一闪就没」。

    shiboken6 是 PySide6 的伴生包（requirements.txt 里必然存在），自带更新的
    MSVCP140 / VCRUNTIME140。把该目录加入 DLL 搜索路径即可，无需管理员权限
    安装 VC++ 运行库；系统以后升级运行库也不会冲突（新版本向后兼容）。
    """
    global _MSVC_DLL_HANDLE
    if not hasattr(os, "add_dll_directory"):   # 非 Windows
        return
    try:
        import shiboken6
        dll_dir = Path(shiboken6.__file__).resolve().parent
        if not (dll_dir / "msvcp140.dll").is_file():
            return
        _MSVC_DLL_HANDLE = os.add_dll_directory(str(dll_dir))
        log.debug("已将 %s 加入 DLL 搜索路径", dll_dir)
    except Exception:
        # 拿不到就按原样继续：能跑就跑，真崩了也能从系统事件日志定位
        log.debug("加入 shiboken6 DLL 目录失败", exc_info=True)


_preload_newer_msvc_runtime()


# ========== 路径与配置（发布友好：不依赖任何机器绝对路径） ==========
# 目录结构：
#   pdf_reader/
#     reader.py           主程序
#     reader_config.json  可选配置（由 reader_config.example.json 复制而来）
#     assets/reader.ico   窗口图标
APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
# 测试/打包可用 PDF_READER_CONFIG_PATH 指向临时配置，避免改动用户真实配置
CONFIG_PATH = Path(os.environ.get("PDF_READER_CONFIG_PATH") or (APP_DIR / "reader_config.json"))


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


def _cfg_bool(key: str, default: bool) -> bool:
    """布尔配置：JSON 里写 true/false，环境变量里写 1/0、yes/no、on/off、是/否 都认。"""
    raw = _cfg(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y", "是", "开")


def save_config(**updates) -> bool:
    """把部分配置写回 reader_config.json，只改传入的键。

    先写同目录临时文件再原子替换：写失败返回 False，界面据此提示；
    配置文件存在但无法解析时拒绝写入，避免把用户手写的其他键覆盖掉。
    """
    data: Dict[str, Any] = {}
    if CONFIG_PATH.is_file():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            log.warning("配置文件无法解析，拒绝覆盖写入：%s", CONFIG_PATH, exc_info=True)
            return False
        if not isinstance(data, dict):
            log.warning("配置文件顶层不是对象，拒绝覆盖写入：%s", CONFIG_PATH)
            return False

    data.update(updates)
    tmp = CONFIG_PATH.with_name(f".{CONFIG_PATH.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        log.warning("写入配置失败：%s", CONFIG_PATH, exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    CONFIG.update(updates)
    return True


PINNED_KEY = "pinned_pdfs"


def pinned_pdf_paths(raw=None) -> List[str]:
    """规范置顶列表：去空、去重、保序；相对路径按程序目录解析。"""
    if raw is None:
        raw = CONFIG.get(PINNED_KEY, [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        p = Path(item.strip()).expanduser()
        if not p.is_absolute():
            p = APP_DIR / p
        abs_path = os.path.abspath(p)
        key = os.path.normcase(abs_path)
        if key in seen:
            continue
        seen.add(key)
        out.append(abs_path)
    return out


def load_pinned_pdfs() -> List[str]:
    """读取配置里的置顶书单（启动时按这个顺序打开）。"""
    return pinned_pdf_paths()


def save_pinned_pdfs(paths) -> bool:
    return save_config(**{PINNED_KEY: pinned_pdf_paths(paths)})


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


def resolve_notes_path(raw) -> Path:
    """速记文件路径：相对路径按程序目录解析，支持 ~ 展开。

    默认放在程序目录下（跟着包走，拷走整个文件夹速记也跟着走）；
    想放别处就在配置里给绝对路径或相对程序目录的路径。
    """
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (APP_DIR / p)


# 右侧速记栏（输入即存）
NOTES_PATH = resolve_notes_path(_cfg("notes_file", "notes.md"))
NOTES_SAVE_DEBOUNCE_MS = 800          # 停止输入多久后落盘
NOTES_MAX_BYTES = 2 * 1024 * 1024     # 加载上限：误指向超大文件时也不至于卡死界面


def _cfg_int(key: str, default: int, lo: int, hi: int) -> int:
    """整数配置，越界或写错时回退默认值并留日志（不静默改成奇怪的值）。"""
    raw = _cfg(key, default)
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        log.warning("配置 %s=%r 不是整数，已回退为 %s", key, raw, default)
        return default


# ========== 标签页
# 一个标签页 = 一个独立的 PdfViewer（各自的文档句柄、页码、缩放、截取状态）。
# 切走的标签会立即释放页面位图缓存（单页可达数百 MB，见 PAGE_CACHE_MAX_BYTES），
# 文档与内容框缓存保留；上限防止一次开太多本把内存与文件句柄吃满。
MAX_TABS = _cfg_int("max_tabs", 8, 2, 24)


# ========== 截取暂存（右侧栏「暂存」页）
# 每次截取顺便存一份到程序目录的 captures/：右侧能看缩略图与出处，点一下重新复制；
# 支持批量清理，并自动限制条数与总字节数（超出就清最旧的）。
CAPTURES_DIR = Path(str(_cfg("captures_dir", "captures")))
if not CAPTURES_DIR.is_absolute():
    CAPTURES_DIR = APP_DIR / CAPTURES_DIR
CAPTURES_MAX = _cfg_int("captures_max", 200, 10, 5000)
CAPTURE_THUMB_WIDTH = 220
CAPTURE_MAX_BYTES = 512 * 1024 * 1024

# 图标优先用包内 assets/reader.ico（随包发布）；缺失时返回空串，使用 Qt 默认图标
ICON_PATH = str(ASSETS_DIR / "reader.ico") if (ASSETS_DIR / "reader.ico").is_file() else ""

# Windows 任务栏按 AppUserModelID 分组。不设置时系统会按解释器（pythonw.exe）
# 猜应用身份，偶发显示 Python 默认图标、或同一程序被分成两组——窗口图标本身
# 其实已经设了（2026-11 排查：reader.ico 路径与尺寸均正常）。必须在创建
# QApplication 之前调用才生效。
APP_USER_MODEL_ID = "PdfReader.App.1"


def set_windows_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """设置 Windows 任务栏的应用身份；非 Windows 或失败返回 False，不影响启动。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        hr = shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        if hr != 0:
            log.warning("设置 AppUserModelID 失败：HRESULT=0x%08X", hr & 0xFFFFFFFF)
            return False
        return True
    except Exception:
        log.debug("设置 AppUserModelID 失败", exc_info=True)
        return False


HISTORY_VERSION = 3


# ========== 自动裁剪参数
# 默认关闭：帮助文本、README 与这里的取值必须一致（曾出现"文档说默认开启"的旧问题）
AUTO_CROP_DEFAULT = False
AUTO_CROP_WHITE_THRESHOLD = 245
AUTO_CROP_MIN_SIZE = 8

# ========== 反色显示（夜间看白底扫描件）
# 只是显示层面的配色，页面的内容框探测、自动裁剪判定、无损截取全部按原始像素走；
# 想要每次启动就反色，在 reader_config.json 里写 "invert": true。
INVERT_DEFAULT = False
INVERT_ENABLED = _cfg_bool("invert", INVERT_DEFAULT)

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
