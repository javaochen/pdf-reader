"""持久化：阅读进度、最近打开列表、截取暂存（索引 + 缩略图）。

全部走"先写临时文件再原子替换"或带防抖的保存，写失败只记日志不抛给界面。
"""

from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
import json
import os
import time
import uuid

from app_config import (
    CAPTURES_MAX,
    CAPTURE_MAX_BYTES,
    CAPTURE_THUMB_WIDTH,
    HISTORY_FILE_NAME,
    HISTORY_VERSION,
    RECENT_FILE_NAME,
    log,
)

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPixmap


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


class CaptureStore(QObject):
    """把每次截取的图暂存到程序子文件夹，并维护索引（缩略图 + 出处 + 时间）。

    - 文件名只用时间戳，避免书名里的怪字符进文件名；书名/页码存索引
    - 自动清理：超出 CAPTURES_MAX 条或 CAPTURE_MAX_BYTES 字节时，从最旧的删
    - 索引损坏/文件被用户删掉时只降级（跳过坏项），不让面板崩
    """

    changed = Signal()

    def __init__(self, dir_path: Path, max_items: int = CAPTURES_MAX, parent=None):
        super().__init__(parent)
        self.dir = Path(dir_path)
        self.index_path = self.dir / "index.json"
        self.max_items = int(max_items)
        self.entries: List[dict] = []
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("暂存目录不可用（%s）：%s", self.dir, e)
        self._load()
        self._sweep_orphans()       # 自愈：上一轮手工删文件留下的缩略图不留在磁盘上

    # ---- 索引 ----
    def _load(self):
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            log.warning("暂存索引损坏，已忽略：%s", self.index_path, exc_info=True)
            return
        kept = []
        for e in raw if isinstance(raw, list) else []:
            if not isinstance(e, dict) or not e.get("file"):
                continue
            if (self.dir / e["file"]).is_file():
                kept.append(e)
        self.entries = kept

    def _save(self):
        try:
            self.index_path.write_text(json.dumps(self.entries, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        except OSError:
            log.warning("写暂存索引失败：%s", self.index_path, exc_info=True)

    # ---- 增删 ----
    def add(self, pixmap: QPixmap, book: str, page: int,
            y0: Optional[float] = None, y1: Optional[float] = None) -> Optional[dict]:
        if pixmap is None or pixmap.isNull():
            return None
        name = f"cap_{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
        thumb_name = name[:-4] + "_thumb.png"
        try:
            if not pixmap.save(str(self.dir / name), "PNG"):
                raise OSError("保存失败")
            thumb = pixmap.scaledToWidth(CAPTURE_THUMB_WIDTH, Qt.SmoothTransformation)
            thumb.save(str(self.dir / thumb_name), "PNG")
        except Exception:
            log.warning("保存暂存截图失败：%s", name, exc_info=True)
            return None
        entry = {"file": name, "thumb": thumb_name, "book": book, "page": int(page),
                 "y0": y0, "y1": y1, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "w": pixmap.width(), "h": pixmap.height()}
        self.entries.append(entry)
        self._prune()
        self._save()
        self.changed.emit()
        return entry

    def _delete_entries(self, doomed: List[dict]) -> int:
        removed = 0
        for e in doomed:
            for key in ("file", "thumb"):
                p = self.dir / str(e.get(key) or "")
                if e.get(key):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        log.debug("删除暂存文件失败：%s", p, exc_info=True)
            if e in self.entries:
                self.entries.remove(e)
                removed += 1
        return removed

    def _prune(self):
        """超条数或超字节就删最旧的（至少留一条，避免用户刚截就没了）。"""
        while len(self.entries) > self.max_items:
            self._delete_entries([self.entries[0]])
        while len(self.entries) > 1 and self.total_bytes() > CAPTURE_MAX_BYTES:
            self._delete_entries([self.entries[0]])

    def _sweep_orphans(self) -> int:
        """清掉目录里没有被索引引用的 cap_*.png。

        用户手工删了原图（或索引损坏）时，缩略图会变成孤儿；不清的话"全部清空"
        之后磁盘上仍留着垃圾文件。
        """
        keep = set()
        for e in self.entries:
            for key in ("file", "thumb"):
                if e.get(key):
                    keep.add(os.path.basename(str(e[key])))
        removed = 0
        try:
            names = os.listdir(self.dir)
        except OSError:
            return 0
        for name in names:
            if not name.lower().endswith(".png") or not name.startswith("cap_"):
                continue
            if name in keep:
                continue
            try:
                (self.dir / name).unlink()
                removed += 1
            except OSError:
                log.debug("删除孤儿暂存文件失败：%s", name, exc_info=True)
        return removed

    def remove(self, files: List[str]) -> int:
        doomed = [e for e in self.entries if e.get("file") in set(files)]
        n = self._delete_entries(doomed)
        if n:
            self._save()
            self.changed.emit()
        return n

    def clear(self) -> int:
        n = self._delete_entries(list(self.entries))
        if n:
            self._save()
            self.changed.emit()
        # 顺手清孤儿（例如用户手工删过原图留下的缩略图）
        self._sweep_orphans()
        return n

    def total_bytes(self) -> int:
        total = 0
        for e in self.entries:
            for key in ("file", "thumb"):
                try:
                    total += (self.dir / e[key]).stat().st_size
                except (OSError, KeyError, TypeError):
                    pass
        return total
