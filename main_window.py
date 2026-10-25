"""主窗口：组装阅读区与侧栏，接快捷键、命令面板、文件打开与状态持久化。"""

from typing import List
from typing import Optional
import bisect
import os

from app_config import (
    CAPTURES_DIR,
    CAPTURES_MAX,
    INITIAL_PAGE,
    INITIAL_PDF_KEYWORD,
    MAX_TABS,
    NOTES_PATH,
    PDF_ROOT_DIR,
    PdfEntry,
    STATE_DIR,
    STATE_SAVE_DEBOUNCE_MS,
    log,
)
from stores import CaptureStore, HistoryStore, RecentStore
from ui_panels import (
    BookmarkCopyDialog,
    BookmarkPanel,
    CapturePanel,
    CommandDialog,
    HelpDialog,
    JumpPageDialog,
    NotesPanel,
    QuickSwitchDialog,
    ShortcutListDialog,
)
from viewer import PdfViewer

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFontMetrics, QGuiApplication, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTabWidget,
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

        self.bookmark_panel = BookmarkPanel(self.jump_to_page, self._nav_bookmark)
        self.notes_panel = NotesPanel(NOTES_PATH, context_provider=self.notes_context)

        # 右侧栏：两个选项（速记 / 截取暂存），默认不占地方，按需打开
        self.capture_store = CaptureStore(CAPTURES_DIR, CAPTURES_MAX, parent=self)
        self.capture_panel = CapturePanel(self.capture_store,
                                          on_copy=self._copy_capture,
                                          on_jump=self._jump_to_capture)
        self.side_panel = QTabWidget()
        self.side_panel.setDocumentMode(True)
        self.side_panel.addTab(self.notes_panel, "速记")
        self.side_panel.addTab(self.capture_panel, "暂存")
        self.side_panel.hide()

        # 阅读区：一个标签页 = 一个独立的 PdfViewer（各自持有文档、页码、缩放、
        # 截取状态与缓存）。self.viewer 始终指向当前标签页的 viewer，其余代码
        # 仍按"只有一个阅读区"来写，不必到处判断标签页是否存在。
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setElideMode(Qt.ElideMiddle)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.btn_new_tab = QPushButton("+")
        self.btn_new_tab.setToolTip(f"新建标签页并选择 PDF（Ctrl+T），最多 {MAX_TABS} 个")
        self.btn_new_tab.setFixedWidth(28)
        self.btn_new_tab.setFocusPolicy(Qt.NoFocus)
        self.btn_new_tab.clicked.connect(self.new_tab_dialog)
        self.tabs.setCornerWidget(self.btn_new_tab, Qt.TopRightCorner)

        self.viewer: PdfViewer = self._add_tab()

        self.splitter = QSplitter()
        self.splitter.addWidget(self.bookmark_panel)
        self.splitter.addWidget(self.tabs)
        self.splitter.addWidget(self.side_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 6)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([280, 1020, 0])

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
        ("Ctrl+O",         "快速切换 PDF",  "在本标签页内切换文件(候选里 Ctrl+Enter 可开新标签)", "quick_switch_dialog"),
        ("Ctrl+T",         "新标签页",      "选一本书在新标签页打开（对照多本）", "new_tab_dialog"),
        ("Ctrl+W",         "关闭标签页",    "关闭当前标签页；最后一个只清空",     "close_current_tab"),
        ("Ctrl+Tab",       "下一个标签页",  "切到右边的标签页",                  "next_tab"),
        ("Ctrl+Shift+Tab", "上一个标签页",  "切到左边的标签页",                  "prev_tab"),
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
        ("Ctrl+Shift+I",   "反色显示",      "白底黑字 ↔ 黑底白字（仅显示，不影响截取）", "toggle_invert_mode"),
        ("Ctrl+N",         "速记栏",        "显示/隐藏右侧速记（自动存 md）", "toggle_notes_panel"),
        ("Ctrl+Shift+X",   "截取暂存",      "显示/隐藏右侧暂存（截图缩略图，点一下复制）", "toggle_captures_panel"),
        ("Ctrl+/",         "快捷键列表",    "查看所有快捷键",                "show_shortcuts"),
    ]

    # 无快捷键的命令扩展 (名称, 描述, 回调)
    # 只放"没有快捷键"的命令：有键位的统一在 SKEY 里登记，否则命令面板会出现两条一样的
    EXTRA_COMMANDS = [
        ("打开文件",        "打开任意目录的PDF 文件",                    "open_dialog"),
        ("适应页面开关",    "在 适合页面/适合宽度 之间切换",     "toggle_fit_page_mode"),
        ("显示/隐藏书签栏", "切换左侧书签面板",                 "toggle_bookmark_panel"),
        ("插入位置标记",    "在速记里记下当前书名与页码",       "notes_insert_marker"),
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

    @staticmethod
    def _action_key(callback):
        """去重用的动作标识：绑定方法取底层函数（每次 getattr 都是新的绑定对象）。"""
        return getattr(callback, "__func__", callback)

    def _register_shortcut(self, key, callback):
        key_seq = key if isinstance(key, Qt.Key) else QKeySequence(key)
        QShortcut(key_seq, self, activated=callback)

    def _setup_shortcuts(self):
        for key, _name, _desc, cb_name in self.SKEY:
            self._register_shortcut(key, self._resolve(cb_name))
        # Alt+1…Alt+9 直接切标签页（Ctrl+1/2/0 已被缩放占用）
        for n in range(1, 10):
            self._register_shortcut(f"Alt+{n}", lambda idx=n - 1: self.select_tab(idx))
        # 小键盘回车与主回车等价（SKEY 里只登记主回车）
        self._register_shortcut(Qt.Key_Enter, self.crop_enter)

    @staticmethod
    def _tab_number_rows():
        return [(f"Alt+{n}", f"第 {n} 个标签页", "直接切到第 N 个标签页", None)
                for n in range(1, 10)]

    _NAV_NAMES = {"上一页", "下一页", "回到首页", "跳到末页", "上一书签", "下一书签"}

    # 单个标签标题最多占多少像素：长书名按像素中间省略，不把整条页签栏占满
    TAB_TITLE_MAX_PX = 200

    def get_command_actions(self):
        """命令面板的条目：EXTRA（无快捷键）→ 有快捷键的一般命令 → 翻页/导航。

        同一个动作只出现一次：EXTRA 里若写了已有快捷键的命令，一律跳过，改用带键位
        的那条。这是命令面板"看起来重复"的根因——SKEY 与 EXTRA 两张表都登记了同一
        个回调（适合页面/反色显示/侧栏开关都曾各出现两次）。
        """
        shortcut_keys = {}
        for key, _name, _desc, cb_name in self.SKEY:
            cb = self._resolve(cb_name)
            shortcut_keys.setdefault(self._action_key(cb),
                                     key if isinstance(key, str) else None)

        actions = []
        seen = set()

        def add(name, desc, callback, key):
            mark = self._action_key(callback)
            if mark in seen:
                return
            seen.add(mark)
            actions.append((name, desc, callback, key))

        # 1) 无快捷键命令优先（最常用的几个在顶部）
        for name, desc, cb_name in self.EXTRA_COMMANDS:
            cb = self._resolve(cb_name)
            if self._action_key(cb) in shortcut_keys:   # 留给带键位的那条
                continue
            add(name, desc, cb, None)

        # 2) 有快捷键的一般命令，3) 翻页/导航放到最后
        nav, rest = [], []
        for key, name, desc, cb_name in self.SKEY:
            key_str = key if isinstance(key, str) else None
            entry = (name, desc, self._resolve(cb_name), key_str)
            (nav if name in self._NAV_NAMES else rest).append(entry)
        for entry in rest + nav:
            add(*entry)
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
        self.pdf_entries = self.scan_pdf_files(PDF_ROOT_DIR)
        # 先落盘再重新读取，否则未写出的内存改动会被 load() 覆盖
        self._flush_state()
        self.history.load()
        self.recent.load()
        self.history.prune_not_exists()
        self.recent.prune_not_exists()

        self.status.showMessage(f"已刷新：扫描到 {len(self.pdf_entries)} 个 PDF")
        # 逐个标签页校验文件是否还在：在就原地重开（保留页码与缩放），不在就清空。
        # 只处理当前标签页的话，被删掉的书会在后台标签里继续占着旧句柄。
        for i in range(self.tabs.count()):
            viewer = self.tabs.widget(i)
            path = viewer.current_path
            if not path:
                continue
            if os.path.isfile(path):
                viewer.open_pdf(path, viewer.current_page_1_based())
            else:
                viewer.clear_pdf()
        # 全窗口一本都没打开时才兜底打开第一本（旧行为）；某个标签页的书被删掉
        # 就让它空着，别把另一本书悄悄塞进这个标签页——对照时很容易搞混
        if not any(self.tabs.widget(i).doc for i in range(self.tabs.count())) \
                and self.pdf_entries:
            self.open_pdf(self.pdf_entries[0].path)
        self._update_all_tab_titles()
        self.refresh_bookmarks()

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
        ShortcutListDialog(self.SKEY + self._tab_number_rows(), self).exec()
        self.restore_crop_focus()

    def quick_switch_dialog(self):
        dlg = QuickSwitchDialog(self.pdf_entries, self.recent, self)
        if dlg.exec() == QDialog.Accepted and dlg.selected_path:
            if dlg.open_in_new_tab:
                self.open_in_new_tab(dlg.selected_path)
            else:
                self.open_pdf(dlg.selected_path)   # 默认：替换当前标签页的文件
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
        """在当前标签页里打开/切换文件（Ctrl+O 走这条路）。"""
        self._open_in(self.viewer, path, explicit_page)

    def _open_in(self, viewer: PdfViewer, path: str,
                 explicit_page: Optional[int] = None) -> bool:
        viewer.stop_crop_mode()
        viewer.stop_cover_mode()
        abs_path = os.path.abspath(path)
        # 没有历史记录时用配置的初始页（此前 INITIAL_PAGE 定义了却从未被读取）
        page = self.history.get_last_page(abs_path, INITIAL_PAGE) \
            if explicit_page is None else explicit_page

        if not viewer.open_pdf(abs_path, page):
            # 打开失败：不记历史，也不让打不开的文件污染"最近打开"
            return False
        self._update_tab_title(viewer)
        if viewer is self.viewer:
            self.refresh_bookmarks()

        self.history.record_open(abs_path, page)
        self.recent.touch(abs_path)
        self._schedule_state_save()

        self.status.showMessage(f"已打开：{os.path.basename(abs_path)} | 第 {page} 页")
        return True

    # ---- 标签页 ----
    # 设计：一个标签页 = 一个 PdfViewer，页面/缩放/截取状态天然隔离；self.viewer
    # 只是"当前标签页"的指针（由 _on_tab_changed 统一维护），其余代码照旧写。
    def _new_viewer(self) -> PdfViewer:
        viewer = PdfViewer()
        # 后台标签页（刷新时会被重开）不该抢状态栏：只有当前标签页能改它
        viewer.status_callback = \
            lambda msg, v=viewer: self.status.showMessage(msg) if v is self.viewer else None
        # 页码变化要知道来自哪个标签页（更新标签标题），所以用默认参数把 viewer 绑进去
        viewer.page_changed_callback = \
            lambda path, page, v=viewer: self.on_page_changed(path, page, v)
        viewer.capture_hook = self._stage_capture
        return viewer

    def _add_tab(self) -> PdfViewer:
        viewer = self._new_viewer()
        index = self.tabs.addTab(viewer, "空白")
        self.tabs.setCurrentIndex(index)     # 触发 _on_tab_changed → 同步 self.viewer
        self._update_tab_title(viewer)
        return viewer

    def open_in_new_tab(self, path: str, explicit_page: Optional[int] = None) -> bool:
        """新开标签页打开 path；超出上限或打开失败返回 False。"""
        if self.tabs.count() >= MAX_TABS:
            self.status.showMessage(f"标签页已达上限 {MAX_TABS} 个：先关掉一个（Ctrl+W）再开")
            return False
        viewer = self._add_tab()
        if not self._open_in(viewer, path, explicit_page):
            self._close_tab(self.tabs.indexOf(viewer))   # 别留一个打不开的空标签
            return False
        return True

    def _close_tab(self, index: int):
        viewer = self.tabs.widget(index)
        if viewer is None:
            return
        name = os.path.basename(viewer.current_path) if viewer.current_path else "空白标签"
        viewer.stop_crop_mode()
        viewer.stop_cover_mode()
        if self.tabs.count() <= 1:
            # 最后一个标签只清空、不删除：阅读区不存在时 self.viewer 会是 None，
            # 而所有命令（翻页/缩放/截取）都默认它存在
            viewer.clear_pdf()
            self._update_tab_title(viewer)
            self.refresh_bookmarks()
            self.status.showMessage("已关闭最后一个标签页（保留空白标签，Ctrl+T 打开新书）")
            return
        viewer.clear_pdf()          # 显式关文档句柄：Windows 上不清会锁住 PDF 文件
        self.tabs.removeTab(index)
        viewer.deleteLater()
        self.status.showMessage(f"已关闭标签页：{name}")

    def close_current_tab(self):
        self._close_tab(self.tabs.currentIndex())

    def new_tab_dialog(self):
        """Ctrl+T：选一本书在新标签页里打开（对照多本时用这个）。"""
        if self.tabs.count() >= MAX_TABS:
            self.status.showMessage(f"标签页已达上限 {MAX_TABS} 个：先关掉一个（Ctrl+W）再开")
            return
        dlg = QuickSwitchDialog(self.pdf_entries, self.recent, self)
        dlg.setWindowTitle("在新标签页打开")
        if dlg.exec() == QDialog.Accepted and dlg.selected_path:
            self.open_in_new_tab(dlg.selected_path)
        self.restore_crop_focus()

    def _on_tab_changed(self, index: int):
        """切标签页：同步 self.viewer，并把被切走的标签占的位图还给系统。"""
        viewer = self.tabs.widget(index)
        if viewer is None:
            return
        self.viewer = viewer
        freed = 0
        for i in range(self.tabs.count()):
            other = self.tabs.widget(i)
            if other is None or other is viewer:
                continue
            # 非活动标签不该继续挂在截取/遮挡模式：焦点已经不在它身上，回车会落空
            other.stop_crop_mode()
            other.stop_cover_mode()
            freed += other.release_page_cache()
            freed += other.release_display()
        if freed:
            log.debug("切换标签页，释放位图 %.1f MB", freed / 1024 / 1024)
        # 切回来时显示位图已被释放，重新渲染一次（页码/缩放/滚动位置都还在）
        pixmap = viewer.image_label.pixmap()
        if viewer.doc and (pixmap is None or pixmap.isNull()):
            viewer.render_current_page()
        self.refresh_bookmarks()
        viewer.update_status()

    def _update_tab_title(self, viewer: PdfViewer):
        index = self.tabs.indexOf(viewer)
        if index < 0:
            return
        path = viewer.current_path
        if not path:
            self.tabs.setTabText(index, "空白")
            self.tabs.setTabToolTip(index, "未打开文件（Ctrl+T / Ctrl+O）")
            return
        name = os.path.splitext(os.path.basename(path))[0]
        page = viewer.current_page_1_based()
        # 标题带页码：同一本书开两个标签对照时，标题才分得清
        title = f"{name} · {page}"
        # 一个超长书名不该把整条页签栏占满：按像素宽度中间省略，
        # 开头（书名）与结尾（页码）都留着；完整路径和页数进 tooltip
        metrics = QFontMetrics(self.tabs.tabBar().font())
        self.tabs.setTabText(
            index, metrics.elidedText(title, Qt.ElideMiddle, self.TAB_TITLE_MAX_PX))
        self.tabs.setTabToolTip(
            index, f"{path}\n第 {page} / {viewer.total_pages()} 页")

    def _update_all_tab_titles(self):
        for i in range(self.tabs.count()):
            self._update_tab_title(self.tabs.widget(i))

    def next_tab(self):
        total = self.tabs.count()
        if total > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % total)

    def prev_tab(self):
        total = self.tabs.count()
        if total > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() - 1) % total)

    def select_tab(self, index: int):
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)

    def _flush_state(self):
        """把阅读进度/最近打开记录写盘（防抖超时或退出前调用）。"""
        self._save_timer.stop()
        self.history.save()
        self.recent.save()

    def _schedule_state_save(self):
        self._save_timer.start()

    def closeEvent(self, event):
        self._flush_state()
        self.notes_panel.flush()      # 速记未落盘的改动（防抖未触发）在关窗前补齐
        # 逐个标签页关文档：Qt 销毁控件时 PyMuPDF 的句柄是被动释放的，显式关掉
        # 更可靠（Windows 上句柄没放会锁住 PDF 文件）
        for i in range(self.tabs.count()):
            viewer = self.tabs.widget(i)
            if viewer is not None:
                viewer.clear_pdf()
        super().closeEvent(event)

    def on_page_changed(self, path: str, page_1_based: int,
                        viewer: Optional[PdfViewer] = None):
        self.history.record_progress(path, page_1_based)
        self._schedule_state_save()
        if viewer is not None:
            self._update_tab_title(viewer)

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

    # ---- 右侧栏（速记 / 截取暂存） ----
    def notes_context(self):
        """给速记面板提供"当前在哪"：书名 + 页码。"""
        path = getattr(self.viewer, "current_path", None)
        name = os.path.basename(path) if path else "未打开文件"
        page = self.viewer.current_page_1_based() if path else 0
        return name, page

    def show_side_panel(self, index: int):
        """打开右侧栏并切到指定页（0=速记，1=暂存）。"""
        self.side_panel.setCurrentIndex(index)
        if not self.side_panel.isVisible():
            self.side_panel.show()
            total = max(1, self.width() - 280)
            self.splitter.setSizes([280, int(total * 0.72), int(total * 0.28)])

    def toggle_side_panel(self, index: int) -> bool:
        """同一个页签再按一次就收起；返回操作后是否可见。"""
        if self.side_panel.isVisible() and self.side_panel.currentIndex() == index:
            self.side_panel.hide()
            return False
        self.show_side_panel(index)
        return True

    def toggle_notes_panel(self):
        if self.toggle_side_panel(0):
            self.notes_panel.editor.setFocus()

    def toggle_captures_panel(self):
        self.toggle_side_panel(1)
        self.capture_panel.refresh()

    def _stage_capture(self, pixmap, book: str, page: int,
                       y0: Optional[float] = None, y1: Optional[float] = None):
        """截取成功后调用：存进暂存目录并在右侧栏刷新（右侧栏没开就不打断你）。"""
        entry = self.capture_store.add(pixmap, book, page, y0, y1)
        if entry is not None and self.side_panel.isVisible() \
                and self.side_panel.currentIndex() == 1:
            self.capture_panel.refresh()

    def _copy_capture(self, entry: dict):
        path = self.capture_store.dir / str(entry.get("file", ""))
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.status.showMessage(f"暂存文件已不存在：{path.name}")
            return
        QApplication.clipboard().setPixmap(pixmap)
        self.status.showMessage(
            f"已复制暂存截图（{entry.get('book', '')} · 第 {entry.get('page', 0)} 页，"
            f"{pixmap.width()}×{pixmap.height()}）")

    def _jump_to_capture(self, entry: dict):
        """双击暂存项：跳到它来自的页面（书不同就先打开那本）。"""
        page = int(entry.get("page") or 0)
        if page <= 0:
            return
        book = str(entry.get("book") or "")
        cur = os.path.basename(self.viewer.current_path) if self.viewer.current_path else ""
        if book and book != cur:
            target = None
            for e in self.pdf_entries:
                if e.name == book:
                    target = e.path
                    break
            if target:
                self.open_pdf(target, page)
                return
            self.status.showMessage(f"找不到原书：{book}（可能已移动）")
            return
        self.jump_to_page(page)

    def notes_insert_marker(self):
        if not self.side_panel.isVisible():
            self.show_side_panel(0)
        self.notes_panel.insert_location_marker()
        self.status.showMessage(f"已插入位置标记（速记自动保存到 {NOTES_PATH}）")

    def toggle_invert_mode(self):
        self.viewer.toggle_invert_enabled()
        state = "开" if self.viewer.invert_enabled else "关"
        # 这条消息要放在渲染之后：渲染会刷新状态栏，先说的话会被覆盖
        self.status.showMessage(f"反色显示{state}（只改显示配色，截取输出仍是原样）")
