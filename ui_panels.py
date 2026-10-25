"""对话框与侧栏面板：快捷键表、帮助、书签格式选择、快速切换、跳页、命令面板，
以及书签 / 速记 / 截取暂存三个侧栏组件。只依赖回调，不直接操作 PDF。"""

from pathlib import Path
from typing import List
from typing import Optional
import os
import time

from app_config import (
    CAPTURE_THUMB_WIDTH,
    NOTES_MAX_BYTES,
    NOTES_SAVE_DEBOUNCE_MS,
    PdfEntry,
    log,
)
from stores import CaptureStore, RecentStore

from PySide6.QtCore import (
    QEvent,
    QSize,
    QTimer,
    QUrl,
    Qt,
)
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


class ShortcutListDialog(QDialog):
    STYLE = """
        QHeaderView::section { background: #e8e8e8; color: #222; font-weight: bold; padding: 4px 8px; border: 1px solid #ccc; }
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
            "2. 标签页（对照多本）\n"
            "   - Ctrl+T 选一本书在新标签页打开；Ctrl+O 只换当前标签页里的文件\n"
            "   - Ctrl+W 关闭当前标签页（关到最后一个只清空内容，保留空白标签）\n"
            "   - Ctrl+Tab / Ctrl+Shift+Tab 前后切换；Alt+1…Alt+9 直接切到第 N 个\n"
            "   - 每个标签页各自记住书、页码、缩放与自动裁剪状态；标题显示「书名 · 页码」\n"
            "   - 切走的标签页会释放页面位图缓存，切回来重新渲染（页码与白边判定都保留）\n"
            "3. 快速切换 PDF\n"
            "   - 按“最近打开”顺序优先显示（最新在前）\n"
            "   - 再次打开同一文件会移动到最前\n"
            "   - 候选列表里按 Ctrl+Enter 或点「新标签打开」= 在新标签页打开\n"
            "4. 命令面板\n"
            "5. 翻页\n"
            "6. 跳页\n"
            "7. 缩放 / 阅读姿态\n"
            "   - Ctrl+0 适合页面；Ctrl+1 适合宽度；Ctrl+2 原始大小\n"
            "   - Ctrl+滚轮缩放（连续滚动只渲染一次）\n"
            "   - 放大后按住左键拖动平移；双击在放大/适合宽度之间切换\n"
            "8. 截取（循环模式）\n"
            "   - Ctrl+S 进入截取模式；进入后可循环截取多次\n"
            "   - 鼠标移动定位，按回车确认线\n"
            "   - 第一条线为起始线，第二条线为结束线\n"
            "   - 每次截取完成后自动清空分割线，可立即开始下一次\n"
            "   - Esc 仅撤销分割线，不会退出截取模式\n"
            "   - 再次按 Ctrl+S（截取模式）才退出\n"
            "   - 输出不超过该页原图分辨率（扫描件不被插值放大），结果复制到剪贴板\n"
            "   - 跨页截取会自动纵向拼接\n"
            "9. 阅读进度\n"
            "   - 自动记录最后阅读页码\n"
            "   - 下次打开自动恢复\n"
            "   - 不再记录打开次数\n"
            "10. 自动裁剪渲染（默认关闭）\n"
            "   - 开启后渲染前自动切除白边与空白间距\n"
            "   - 含扫描墨点过滤与灰底自适应，Ctrl+Shift+R 或命令面板中开关\n"
            "11. 速记与截取暂存（右侧栏，默认不显示）\n"
            "   - Ctrl+N 显示/隐藏右侧栏的「速记」页；输入停止后自动存到 notes.md\n"
            "   - Ctrl+Shift+X 显示/隐藏「暂存」页：每次截取都会顺手存一张到 captures/\n"
            "   - 暂存页点缩略图即复制回剪贴板；双击跳到它来自的那一页；可批量清理\n"
            "12. 反色显示（夜间）\n"
            "   - Ctrl+Shift+I 在白底黑字与黑底白字之间切换\n"
            "   - 只改显示配色：自动裁剪判定、无损截取输出都保持原始像素\n"
            "   - 想每次启动就反色：配置里写 \"invert\": true\n"
            "   - 遮挡模式下遮挡块会自动换成浅色，避免在黑底上看不见\n"
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
    """按文件名筛选 PDF 并选择。

    默认结果是"在当前标签页里换掉文件"；`open_in_new_tab` 为 True 表示
    "在新标签页打开"（按钮或 Ctrl+Enter），由调用方决定怎么用。
    """

    def __init__(self, pdf_list: List[PdfEntry], recent: RecentStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("快速切换 PDF")
        self.resize(860, 560)
        self.pdf_list = pdf_list
        self.recent = recent
        self.selected_path: Optional[str] = None
        self.open_in_new_tab = False

        layout = QVBoxLayout(self)

        self.input = QLineEdit()
        self.input.setPlaceholderText("输入部分文件名（Ctrl+Enter = 在新标签页打开）")
        layout.addWidget(self.input)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)

        row = QHBoxLayout()
        self.open_btn = QPushButton("打开")
        self.new_tab_btn = QPushButton("新标签打开")
        self.cancel_btn = QPushButton("取消")
        row.addStretch(1)
        row.addWidget(self.open_btn)
        row.addWidget(self.new_tab_btn)
        row.addWidget(self.cancel_btn)
        layout.addLayout(row)

        self.input.textChanged.connect(self.update_list)
        self.list_widget.itemClicked.connect(self.accept_selection)
        self.list_widget.itemDoubleClicked.connect(self.accept_selection)
        self.open_btn.clicked.connect(self.accept_selection)
        self.new_tab_btn.clicked.connect(self.accept_in_new_tab)
        self.cancel_btn.clicked.connect(self.reject)
        self.input.returnPressed.connect(self.accept_selection)
        self.input.installEventFilter(self)   # Ctrl+Enter 要先于 returnPressed 截住

        self.update_list("")
        self.input.setFocus()

    def eventFilter(self, obj, event):
        # 输入框自己会吃掉回车（returnPressed），对话框的 keyPressEvent 收不到，
        # 所以 Ctrl+Enter 必须在这里先拦下来
        if obj is self.input and event.type() == QEvent.KeyPress \
                and event.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and event.modifiers() & Qt.ControlModifier:
            self.accept_in_new_tab()
            return True
        return super().eventFilter(obj, event)

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
        # Ctrl+Enter：在新标签页打开（键盘流：输名字 → Ctrl+Enter 即可对照）
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and event.modifiers() & Qt.ControlModifier:
            self.accept_in_new_tab()
            return
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
            self.open_in_new_tab = False
            self.accept()

    def accept_in_new_tab(self):
        item = self.list_widget.currentItem()
        if item:
            self.selected_path = item.data(Qt.UserRole)
            self.open_in_new_tab = True
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
    """命令面板：按名称/说明/快捷键搜索，↑↓ 选择、回车执行。

    动作表由 MainWindow.get_command_actions() 提供（已去重），这里只管展示与筛选。
    """

    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("命令面板")
        self.resize(900, 580)
        self.actions = list(actions)
        layout = QVBoxLayout(self)

        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "输入关键字搜索命令（名称 / 说明 / 快捷键），↑↓ 选择，回车执行")
        self.input.setClearButtonEnabled(True)
        layout.addWidget(self.input)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)

        self.hint = QLabel()
        self.hint.setStyleSheet("color: #888;")
        layout.addWidget(self.hint)

        self.input.textChanged.connect(self.apply_filter)
        self.input.installEventFilter(self)
        self.list_widget.itemClicked.connect(self.run_selected)
        self.list_widget.itemActivated.connect(self.run_selected)
        self.apply_filter("")
        self.input.setFocus()

    @staticmethod
    def _search_text(action) -> str:
        name, desc, _callback, key = action
        return f"{name} {desc} {key or ''}".lower()

    def apply_filter(self, text: str):
        """按空格分词，所有词都出现才算命中（"标签 关闭" 这类组合也能查）。"""
        tokens = [t for t in text.lower().split() if t]
        self.list_widget.clear()
        matched = 0
        for action in self.actions:
            haystack = self._search_text(action)
            if not all(t in haystack for t in tokens):
                continue
            name, desc, callback, key = action
            display = f"{name}　—　{desc}" + (f"　　[{key}]" if key else "")
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, callback)
            item.setToolTip(display)
            self.list_widget.addItem(item)
            matched += 1
        if matched:
            self.list_widget.setCurrentRow(0)
            self.hint.setText(f"{matched} / {len(self.actions)} 条命令")
        else:
            placeholder = QListWidgetItem("没有匹配的命令（换个关键字试试）")
            placeholder.setFlags(Qt.NoItemFlags)   # 不可选中、点了也不会执行
            self.list_widget.addItem(placeholder)
            self.hint.setText(f"0 / {len(self.actions)} 条命令")

    def eventFilter(self, obj, event):
        # 输入框吃掉方向键/回车，面板的 keyPressEvent 收不到，这里先拦
        if obj is self.input and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Up, Qt.Key_Down):
                self._move_selection(1 if key == Qt.Key_Down else -1)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self.run_selected(self.list_widget.currentItem())
                return True
        return super().eventFilter(obj, event)

    def _move_selection(self, step: int):
        total = self.list_widget.count()
        if total <= 0:
            return
        current = self.list_widget.currentRow()
        self.list_widget.setCurrentRow(max(0, min(total - 1, current + step)))

    def run_selected(self, item=None):
        if item is None:
            item = self.list_widget.currentItem()
        if item is None or not (item.flags() & Qt.ItemIsEnabled):
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


class NotesPanel(QWidget):
    """右侧速记栏：边看书边写，停止输入后自动落盘到一个 md 文件。

    刻意保持简单——**单个 md 文件、纯 markdown 源码、不做渲染**：先确认这个面板
    在日常阅读里真的会被用到，再考虑多速记文件或公式渲染。

    - 写盘用"临时文件 + 替换"，中途崩溃不会把原文件截断成半截
    - 写盘防抖（NOTES_SAVE_DEBOUNCE_MS），关窗时 flush 兜底，不会丢字
    - 写失败不静默：状态行变红并写日志（例如程序目录只读）
    """

    def __init__(self, path: Path, context_provider=None, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.context_provider = context_provider
        self._dirty = False
        self._last_error = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self.title = QLabel("速记")
        self.title.setStyleSheet("font-weight: bold;")
        self.title.setToolTip(f"自动保存到：{self.path}")
        layout.addWidget(self.title)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "在这里写速记，停止输入后自动保存到 md 文件。\n\n"
            "点「插入位置」记下当前书名与页码，方便回溯原文。"
        )
        self.editor.setFont(QFont("Consolas", 10))
        self.editor.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.editor, 1)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.btn_marker = QPushButton("插入位置")
        self.btn_marker.setFocusPolicy(Qt.NoFocus)
        self.btn_marker.clicked.connect(self.insert_location_marker)
        self.btn_open = QPushButton("打开文件")
        self.btn_open.setFocusPolicy(Qt.NoFocus)
        self.btn_open.clicked.connect(self._open_file_externally)
        row.addWidget(self.btn_marker)
        row.addWidget(self.btn_open)
        row.addStretch(1)
        layout.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(NOTES_SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self.save)

        self.load()

    # ---- 读写 ----
    def load(self):
        """把已有速记读进编辑器（超过上限则截断，避免超大文件卡住界面）。"""
        self.editor.blockSignals(True)
        try:
            if self.path.is_file():
                size = self.path.stat().st_size
                with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read(NOTES_MAX_BYTES + 1)
                truncated = len(text) > NOTES_MAX_BYTES
                self.editor.setPlainText(text[:NOTES_MAX_BYTES])
                if truncated:
                    self._set_status(f"文件超过 {NOTES_MAX_BYTES // 1024} KB，只载入了前一部分", error=True)
                else:
                    self._set_status(f"已载入 {size / 1024:.1f} KB")
            else:
                self.editor.setPlainText("")
                self._set_status("新文件，输入后自动创建")
            self._dirty = False
        except OSError as e:
            log.warning("读取速记文件失败：%s（%s）", self.path, e)
            log.debug("读取速记文件失败详情", exc_info=True)
            self._set_status(f"读取失败：{e}", error=True)
        finally:
            self.editor.blockSignals(False)

    def save(self, force: bool = False) -> bool:
        """写盘。默认只在有改动时写；force=True 用于关窗兜底。"""
        self._save_timer.stop()
        if not self._dirty and not force:
            return True
        text = self.editor.toPlainText()
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, self.path)          # 原子替换：崩溃不会留下半截文件
        except OSError as e:
            # 用户可见的一行 + 详细堆栈留给 DEBUG，避免无控制台的窗口应用刷一屏
            log.warning("写入速记文件失败：%s（%s）", self.path, e)
            log.debug("写入速记文件失败详情", exc_info=True)
            self._set_status(f"保存失败：{e}", error=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        self._dirty = False
        self._set_status(f"已保存 {time.strftime('%H:%M:%S')} · {len(text) / 1024:.1f} KB")
        return True

    def flush(self) -> bool:
        """关窗/退出前调用：立刻落盘，撤掉尚未触发的防抖。"""
        return self.save(force=self._dirty or self._save_timer.isActive())

    # ---- 交互 ----
    def insert_location_marker(self):
        """在光标处插入一行位置标记（书名 + 页码 + 时间），便于回溯原文。"""
        name, page = "未打开文件", 0
        if self.context_provider:
            try:
                name, page = self.context_provider()
            except Exception:
                log.debug("取速记上下文失败", exc_info=True)
        marker = f"\n--- {name} · 第 {page} 页 · {time.strftime('%Y-%m-%d %H:%M')} ---\n"
        cur = self.editor.textCursor()
        # 空文档或光标已在行首时不留空行；在行中间插入则先换行，让标记自成一段
        if not self.editor.toPlainText() or cur.positionInBlock() == 0:
            marker = marker.lstrip("\n")
        cur.insertText(marker)
        self.editor.setTextCursor(cur)
        self.editor.setFocus()

    def _open_file_externally(self):
        if not self.path.is_file():
            self.save(force=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path))):
            self._set_status(f"无法用系统程序打开：{self.path}", error=True)

    def _on_text_changed(self):
        if not self._dirty:
            self._dirty = True
            self._set_status("正在输入…")
        self._save_timer.start()

    def _set_status(self, text: str, error: bool = False):
        self._last_error = text if error else ""
        self.status.setText(text)
        self.status.setStyleSheet("color: #c0392b;" if error else "color: gray;")

    @property
    def is_dirty(self) -> bool:
        return self._dirty


class CapturePanel(QWidget):
    """右侧栏「暂存」页：截图的缩略图 + 出处；点一下就复制回剪贴板，支持批量清理。"""

    def __init__(self, store: CaptureStore, on_copy, on_jump=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.on_copy = on_copy
        self.on_jump = on_jump

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        head = QHBoxLayout()
        self.title = QLabel("截取暂存")
        self.title.setStyleSheet("font-weight: bold;")
        head.addWidget(self.title)
        head.addStretch(1)
        self.btn_dir = QPushButton("打开文件夹")
        self.btn_dir.setFocusPolicy(Qt.NoFocus)
        self.btn_dir.clicked.connect(self._open_dir)
        head.addWidget(self.btn_dir)
        layout.addLayout(head)

        self.list = QListWidget()
        self.list.setIconSize(QSize(CAPTURE_THUMB_WIDTH, 150))
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setWordWrap(True)
        self.list.itemClicked.connect(self._on_item_clicked)
        self.list.itemDoubleClicked.connect(self._on_item_double)
        layout.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_copy = QPushButton("复制选中")
        self.btn_copy.setFocusPolicy(Qt.NoFocus)
        self.btn_copy.clicked.connect(self._copy_selected)
        self.btn_del = QPushButton("清理选中")
        self.btn_del.setFocusPolicy(Qt.NoFocus)
        self.btn_del.clicked.connect(self._remove_selected)
        self.btn_clear = QPushButton("全部清空")
        self.btn_clear.setFocusPolicy(Qt.NoFocus)
        self.btn_clear.clicked.connect(self._clear_all)
        for b in (self.btn_copy, self.btn_del, self.btn_clear):
            row.addWidget(b)
        layout.addLayout(row)

        self.info = QLabel("")
        self.info.setStyleSheet("color: gray;")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        self.store.changed.connect(self.refresh)
        self.refresh()

    # ---- 刷新 ----
    def refresh(self):
        self.list.clear()
        for e in reversed(self.store.entries):        # 新的排在上面
            item = QListWidgetItem(QIcon(str(self.store.dir / e.get("thumb", ""))), "")
            where = f"第 {e.get('page', 0)} 页"
            item.setText(f"{e.get('book', '')} · {where} · {str(e.get('time', ''))[11:]}")
            item.setData(Qt.UserRole, e)
            item.setToolTip(f"{e.get('book', '')}\n第 {e.get('page', 0)} 页\n"
                            f"{e.get('w', 0)}×{e.get('h', 0)} px\n{e.get('time', '')}")
            self.list.addItem(item)
        mb = self.store.total_bytes() / 1048576
        self.info.setText(f"{len(self.store.entries)} 张 · {mb:.1f} MB"
                          f"（点缩略图即复制；选中后可批量清理）")

    def _selected_entries(self) -> List[dict]:
        return [it.data(Qt.UserRole) for it in self.list.selectedItems()]

    # ---- 交互 ----
    def _on_item_clicked(self, item):
        entry = item.data(Qt.UserRole)
        if entry:
            self.on_copy(entry)

    def _on_item_double(self, item):
        entry = item.data(Qt.UserRole)
        if entry and self.on_jump is not None:
            self.on_jump(entry)

    def _copy_selected(self):
        picked = self._selected_entries()
        if picked:
            self.on_copy(picked[0])

    def _remove_selected(self):
        picked = self._selected_entries()
        if not picked:
            return
        self.store.remove([e["file"] for e in picked])

    def _clear_all(self):
        if self.store.entries:
            self.store.clear()

    def _open_dir(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.dir)))
