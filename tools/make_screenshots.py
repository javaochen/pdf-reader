# -*- coding: utf-8 -*-
"""生成 README 用的界面截图（不需要显示器，也不会弹窗）。

两个关键点：
1. 不能用 QT_QPA_PLATFORM=offscreen —— 该平台拿不到系统中文字体，界面会渲染成方框。
   改用系统默认平台 + WA_DontShowOnScreen：字体正常且窗口不上屏（不打扰用户）。
2. 样例书页面用 Qt 的真实中文字体渲染成图片再嵌进 PDF，比 PyMuPDF 内置宋体的
   字距自然，也更像真实的扫描版数学书。样例文字为原创，无版权与个人信息。

用法（在 pdf_reader 目录下）：
    python tools/make_screenshots.py      # 生成到 docs/screenshots/
"""
import os
import shutil
import sys

os.environ.pop("QT_QPA_PLATFORM", None)      # 用系统默认平台
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)                  # pdf_reader/
TMP = os.path.join(PKG, "_shots_tmp")
OUT = os.path.join(PKG, "docs", "screenshots")
os.makedirs(TMP, exist_ok=True)
os.makedirs(OUT, exist_ok=True)
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
# 速记文件必须指到临时目录：否则截图会读到（并可能显示）用户真实的速记内容
os.environ["PDF_READER_NOTES_FILE"] = os.path.join(TMP, "sample-notes.md")
sys.path.insert(0, PKG)

# 截图里速记栏所示的样例内容（原创，正好演示"位置标记 + 自己的疑问"这种用法）
SAMPLE_NOTES = """# 第 3 章 中值定理

## 拉格朗日（定理 3.4）
- 条件：闭区间连续 + 开区间可导，两个条件缺一不可
- 结论：f(b) − f(a) = f'(ξ)(b − a)
- 它是罗尔定理的「倾斜版」：把弦转平就回到罗尔

--- sample-math-book.pdf · 第 1 页 · 2026-09-28 21:40 ---
疑问：ξ 唯一吗？→ 不唯一，只保证「至少存在一点」
反例：f(x) = x³ 在 [−1, 1] 上，ξ = 0 是唯一的

--- sample-math-book.pdf · 第 4 页 · 2026-09-28 21:58 ---
柯西中值定理把分母换成了 g(b) − g(a)
"""
with open(os.environ["PDF_READER_NOTES_FILE"], "w", encoding="utf-8") as _f:
    _f.write(SAMPLE_NOTES)

import fitz
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QImage, QPainter, QPen
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import reader as R

QMessageBox.critical = staticmethod(lambda *a, **k: None)
app = QApplication.instance() or QApplication(sys.argv)


def pick_cjk_font() -> str:
    families = set(QFontDatabase.families())
    for name in ("SimSun", "宋体", "Microsoft YaHei", "微软雅黑", "Noto Sans CJK SC", "SimHei"):
        if name in families:
            return name
    return QFont().family()


CJK_FONT = pick_cjk_font()
print(f"样例页使用字体: {CJK_FONT}")

# ---------------- 用 Qt 渲染一页"扫描版数学书" ----------------
PAGE_W, PAGE_H = 420.0, 595.0     # 页面尺寸（pt）
SCALE = 2000.0 / PAGE_W           # 渲染成 2000px 宽 → 约 4.76 px/pt（比 300 DPI 的目标还清晰）


def render_page(png_path, items, page_no):
    """items: (kind, text) —— kind ∈ {h1,h2,body,formula,box_start,box_end}"""
    img = QImage(int(PAGE_W * SCALE), int(PAGE_H * SCALE), QImage.Format_RGB888)
    img.fill(QColor(255, 255, 255))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    margin = 58 * SCALE
    avail = PAGE_W * SCALE - 2 * margin
    y = 40 * SCALE
    box_top = None

    def draw(text, pt, bold=False, indent=0.0, gap_top=0.0, color=QColor(0, 0, 0)):
        nonlocal y
        font = QFont(CJK_FONT)
        font.setPixelSize(int(pt * SCALE))
        font.setBold(bold)
        fm = QFontMetrics(font)
        p.setFont(font)
        p.setPen(QPen(color))
        y += gap_top * SCALE
        line_h = fm.lineSpacing()
        cur = ""
        for ch in text:
            if cur and fm.horizontalAdvance(cur + ch) > avail - indent * SCALE:
                p.drawText(QRectF(margin + indent * SCALE, y, avail - indent * SCALE, line_h),
                           Qt.AlignLeft | Qt.AlignVCenter, cur)
                y += line_h
                cur = ch
            else:
                cur += ch
        if cur:
            p.drawText(QRectF(margin + indent * SCALE, y, avail - indent * SCALE, line_h),
                       Qt.AlignLeft | Qt.AlignVCenter, cur)
        y += line_h

    for kind, text in items:
        if kind == "h1":
            draw(text, 15, bold=True, gap_top=6)
        elif kind == "h2":
            draw(text, 12.5, bold=True, gap_top=5)
        elif kind == "body":
            draw(text, 10.5, gap_top=4)
        elif kind == "formula":
            draw(text, 11, gap_top=3, indent=34)
        elif kind == "box_start":
            y += 8 * SCALE
            box_top = y - 6 * SCALE
        elif kind == "box_end":
            p.setPen(QPen(QColor(150, 150, 150), max(1.0, 0.8 * SCALE)))
            p.drawRect(QRectF(margin - 10 * SCALE, box_top,
                              avail + 20 * SCALE, y - box_top + 6 * SCALE))
                # 页眉与页脚
    p.setPen(QPen(QColor(120, 120, 120)))
    f = QFont(CJK_FONT)
    f.setPixelSize(int(8.5 * SCALE))
    p.setFont(f)
    p.drawText(QRectF(margin, 22 * SCALE, avail, 14 * SCALE), Qt.AlignLeft, "第 3 章  微分中值定理与导数的应用")
    p.drawLine(int(margin), int(38 * SCALE), int(PAGE_W * SCALE - margin), int(38 * SCALE))
    p.drawText(QRectF(0, PAGE_H * SCALE - 34 * SCALE, PAGE_W * SCALE, 20 * SCALE),
               Qt.AlignHCenter, f"— {page_no} —")
    p.end()
    img.save(png_path, "PNG")
    return png_path


def make_book(path, pages=12):
    doc = fitz.open()
    toc = [[1, "第 3 章  微分中值定理", 1], [2, "§3.2  拉格朗日中值定理", 1],
           [2, "§3.3  柯西中值定理", 2], [2, "§3.4  泰勒公式", 3],
           [1, "第 4 章  导数的应用", 4], [2, "§4.1  单调性与极值", 4],
           [2, "§4.2  曲线的凹凸性", 6], [1, "第 5 章  不定积分", 8]]
    for i in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        if i == 0:
            items = [
                ("h1", "§3.2  拉格朗日中值定理"),
                ("h2", "定理 3.4（拉格朗日中值定理）"),
                ("body", "设函数 f 在闭区间 [a, b] 上连续，在开区间 (a, b) 内可导，"
                         "则至少存在一点 ξ ∈ (a, b)，使得"),
                ("formula", "f(b) − f(a) = f′(ξ)(b − a)."),
                ("body", "证明　作辅助函数"),
                ("formula", "F(x) = f(x) − f(a) − (f(b) − f(a)) / (b − a) · (x − a),"),
                ("body", "则 F(a) = F(b) = 0，且 F 在 [a, b] 上满足罗尔定理的条件，"
                         "故存在 ξ ∈ (a, b) 使 F′(ξ) = 0，即得所证。"),
                ("box_start", ""),
                ("h2", "推论 3.5"),
                ("body", "若 f 在区间 I 上可导且 f′(x) ≡ 0，则 f 在 I 上为常数。"),
                ("h2", "推论 3.6（有限增量公式）"),
                ("formula", "f(x + Δx) − f(x) = f′(x + θΔx) · Δx,　0 < θ < 1."),
                ("box_end", ""),
                ("h2", "例 3.7"),
                ("body", "证明：当 x > 0 时，ln(1 + x) < x。"),
                ("body", "证明　令 f(t) = ln(1 + t)，在 [0, x] 上应用定理 3.4，得"),
                ("formula", "ln(1 + x) − ln 1 = x / (1 + ξ),　0 < ξ < x,"),
                ("body", "而 x / (1 + ξ) < x，故 ln(1 + x) < x。"),
            ]
        else:
            items = [("h1", f"§3.{i + 2}  习题")]
            for k in range(4):
                items.append(("body", f"{i * 4 + k + 1}. 设 f 在 [a, b] 上连续、在 (a, b) 内可导，"
                                      f"证明存在 ξ ∈ (a, b) 使 f′(ξ) = (f(b) − f(a)) / (b − a)。"))
        png = render_page(os.path.join(TMP, f"page{i}.png"), items, i * 2 + 14)
        page.insert_image(page.rect, filename=png)
    doc.set_toc(toc)
    doc.save(path)
    doc.close()
    return path


book = make_book(os.path.join(TMP, "sample-math-book.pdf"))

# ---------------- 截图 ----------------
win = R.MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)   # 正常布局/绘制，但不上屏
win.resize(1500, 900)
win.show()
win.splitter.setSizes([190, 900, 410])       # 左：书签　中：页面　右：速记
win.open_pdf(book, 1)
app.processEvents()
QTest.qWait(250)


def shoot(widget, name, scale_to=None):
    app.processEvents()
    QTest.qWait(180)
    pm = widget.grab()
    if scale_to and pm.width() > scale_to:
        pm = pm.scaledToWidth(scale_to, Qt.SmoothTransformation)
    path = os.path.join(OUT, name)
    pm.save(path, "PNG")
    print(f"  {name}: {pm.width()}x{pm.height()}  {os.path.getsize(path) / 1024:.0f} KB")
    return path


print("生成截图：")


def to_top():
    """适合宽度时页面高于视口，回到页面顶部再截图（避免标题被切一半）。"""
    win.viewer.scroll.verticalScrollBar().setValue(0)
    win.viewer.scroll.horizontalScrollBar().setValue(0)
    app.processEvents()


win.viewer.fit_width()
to_top()
shoot(win, "01-reading.png", 1200)

win.toggle_crop_mode()
app.processEvents()
QTest.qWait(150)
to_top()
ov = win.viewer.crop_overlay
ov.top_marker = (win.viewer.current_page_index, 150)
ov.bottom_marker = (win.viewer.current_page_index, 372)
ov.preview_y = 262
ov.setFocus()
ov.update()
win.status.showMessage("已复制无损截图（原生 300 DPI，1210×1330）；截取模式继续，再次按 Ctrl+S 退出")
shoot(win, "02-capture-mode.png", 1200)

win.toggle_crop_mode()
app.processEvents()
dlg = R.ShortcutListDialog(win.SKEY, win)
dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
dlg.resize(560, 620)
dlg.show()
app.processEvents()
QTest.qWait(200)
shoot(dlg, "03-shortcuts.png")
dlg.close()

win.viewer.goto_page(1)
win.viewer._set_zoom(3.0)
app.processEvents()
QTest.qWait(200)
# 滚到"定理 3.4"的公式附近，展示放大后的细节
vb = win.viewer.scroll.verticalScrollBar()
vb.setValue(min(vb.maximum(), 260))
app.processEvents()
QTest.qWait(120)
shoot(win, "04-zoom-formula.png", 1200)

win.close()
shutil.rmtree(TMP, ignore_errors=True)
print("完成 →", OUT)
