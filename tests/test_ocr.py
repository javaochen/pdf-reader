# -*- coding: utf-8 -*-
"""OCR 选字回归测试。

用 fake 引擎（不加载模型，返回固定三行）跑完整链路：子进程协议、缓存、
防抖调度、坐标映射、拖选与复制、关闭时的零开销。

运行：python tests/test_ocr.py
"""
import json
import os
import shutil
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

# 故意清掉 PYTHONIOENCODING：真实启动（run.bat / launcher）没有这个变量，而它一旦存在
# 就会掩盖"子进程 stdio 按 cp936 解码"的整类问题（中文路径变乱码 → 找不到文件）。
os.environ.pop("PYTHONIOENCODING", None)
NO_IOENC = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}

TMP = os.path.join(PKG, "_test_tmp_ocr")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["PDF_READER_PDF_ROOT_DIR"] = TMP
os.environ["PDF_READER_STATE_DIR"] = TMP
os.environ["PDF_READER_NOTES_FILE"] = os.path.join(TMP, "notes.md")
# 关键：让 OCR 在导入时就处于开启状态，并用 fake 引擎（测试要快、要确定性）
os.environ["PDF_READER_OCR_ENABLED"] = "true"
os.environ["PDF_READER_OCR_ENGINE"] = "fake"
os.environ["PDF_READER_OCR_CACHE_DIR"] = os.path.join(TMP, "ocr")
os.environ["PDF_READER_OCR_DEBOUNCE_MS"] = "200"

import fitz  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import reader as R  # noqa: E402
import ocr_worker as W  # noqa: E402

QMessageBox.critical = staticmethod(lambda *a, **k: None)
app = QApplication.instance() or QApplication([])

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def wait_for(predicate, timeout_ms=8000, step=100):
    waited = 0
    while waited < timeout_ms:
        if predicate():
            return True
        QTest.qWait(step)
        app.processEvents()
        waited += step
    return bool(predicate())


# 造一本 4 页小书：**文件名故意用中文 + 全角括号**——这正是当初让"子进程 stdio 编码"
# 问题暴露的场景（全 ASCII 路径的测试抓不到它）
pdf = os.path.join(TMP, "测试（中文）书.pdf")
doc = fitz.open()
for i in range(20):        # 页数要超过 OCR_MEMORY_PAGES，才能测出"内存里有上限"
    page = doc.new_page(width=400, height=600)
    page.insert_text((60, 90), f"page {i+1}")
    page.draw_rect(fitz.Rect(50, 60, 350, 500), color=(0, 0, 0), width=1)
doc.save(pdf)
doc.close()

print("=" * 78)
print("1. 文字后处理与拼接规则（纯函数）")
print("=" * 78)
check("1.1 Windows 引擎的中文字间空格被去掉",
      W.clean_text("数 列 的 上 极 限") == "数列的上极限",
      repr(W.clean_text("数 列 的 上 极 限")))
check("1.2 拉丁词之间的空格必须保留",
      W.clean_text("COMPLEX ANALYSIS") == "COMPLEX ANALYSIS")
check("1.3 数字与标点被拆开时重新粘起来",
      W.clean_text("5 ． 2 ， 2") == "5.2，2", repr(W.clean_text("5 ． 2 ， 2")))
check("1.4 中文与拉丁混排：中文侧不空格、拉丁侧保留",
      W.clean_text("PROFESSOR SMITH 讲 课") == "PROFESSOR SMITH讲课")

b = [[0, 0, 100, 10, "数"], [101, 0, 200, 10, "列"],
     [0, 20, 200, 30, "second"], [201, 20, 300, 30, "line"]]
check("1.5 同一行内的中文块直接相连", R.join_blocks(b[:2]) == "数列", repr(R.join_blocks(b[:2])))
check("1.6 同一行内的拉丁块补空格", R.join_blocks(b[2:]) == "second line",
      repr(R.join_blocks(b[2:])))
check("1.7 换行才断行", R.join_blocks([[0, 0, 100, 10, "第一行"], [0, 40, 100, 50, "第二行"]])
      == "第一行\n第二行")

print()
print("=" * 78)
print("2. 子进程协议（真实 QProcess，一轮请求-响应）")
print("=" * 78)
# 关键：env 里**不含** PYTHONIOENCODING（真实启动就是这样），路径又是中文——
# 子进程若按 cp936 解码 stdin，这里就会报"找不到文件 / Invalid \escape"
proc = subprocess.Popen(
    [sys.executable, "-u", os.path.join(PKG, "ocr_worker.py"), "--idle", "3"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    cwd=PKG, text=True, encoding="utf-8", env=NO_IOENC)

proc.stdin.write(json.dumps({"cmd": "ping"}) + "\n")
proc.stdin.flush()
line = proc.stdout.readline()
check("2.1 ping 有响应", json.loads(line).get("pong") is True, line.strip()[:60])

proc.stdin.write(json.dumps({"cmd": "ocr", "pdf": pdf, "page": 0,
                             "engine": "fake", "long_side": 1200}) + "\n")
proc.stdin.flush()
msg = json.loads(proc.stdout.readline())
check("2.2 fake 引擎返回 3 个文字块", msg.get("ok") and len(msg.get("blocks", [])) == 3,
      f"ok={msg.get('ok')} blocks={len(msg.get('blocks', []))}")
check("2.3 返回坐标已换算成页面坐标(pt)而非像素",
      msg["blocks"][0][2] <= msg["pw"] + 1 and msg["px_w"] > msg["pw"],
      f"块右边界={msg['blocks'][0][2]:.0f}pt 页宽={msg['pw']:.0f}pt 渲染宽={msg['px_w']}px")
# 中文+全角括号路径必须原样打开（子进程 stdio 若不是 UTF-8，这里会变成找不到文件）
check("2.3b 子进程按原样打开了中文路径的文件（页尺寸来自该文件）",
      msg.get("ok") is True and abs(float(msg.get("pw", 0)) - 400) < 1,
      f"pw={msg.get('pw')} ph={msg.get('ph')}")

proc.stdin.write(json.dumps({"cmd": "ocr", "pdf": pdf, "page": 0,
                             "engine": "不存在的引擎"}) + "\n")
proc.stdin.flush()
err = json.loads(proc.stdout.readline())
check("2.4 未知引擎返回可见错误而不是沉默", err.get("ok") is False and "error" in err,
      str(err.get("error"))[:70])

proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
proc.stdin.flush()
proc.wait(timeout=5)
check("2.5 quit 后子进程退出", proc.returncode == 0, f"returncode={proc.returncode}")

idle = subprocess.Popen([sys.executable, "-u", os.path.join(PKG, "ocr_worker.py"), "--idle", "1"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=PKG, text=True)
t0 = time.time()
idle.wait(timeout=15)
check("2.6 空闲超时后子进程自己退出（归还内存）",
      time.time() - t0 < 12, f"{time.time() - t0:.1f}s")

# 主界面崩溃时：stdin 关闭 → 子进程必须跟着退出，不能留孤儿进程占着模型内存
orphan = subprocess.Popen([sys.executable, "-u", os.path.join(PKG, "ocr_worker.py")],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=PKG, text=True)
orphan.stdin.close()
t0 = time.time()
try:
    orphan.wait(timeout=10)
    exited = orphan.returncode is not None
except subprocess.TimeoutExpired:
    orphan.kill()
    exited = False
check("2.7 父进程断开后子进程立刻退出（不留孤儿进程）", exited,
      f"{time.time() - t0:.1f}s returncode={orphan.returncode}")

print()
print("=" * 78)
print("3. 缓存库（SQLite）")
print("=" * 78)
# 用独立的缓存目录，避免这里的写入影响后面主窗口那一段（否则"是否真的识别过"会被掩盖）
STORE_DIR = os.path.join(TMP, "store_only")
store = R.OcrStore(STORE_DIR, pdf)
check("3.1 缓存库建立成功", store.conn is not None, store.disabled_reason or str(store.path))
blocks = [[10, 20, 100, 40, "缓存测试"]]
store.put_page(0, 400.0, 600.0, "fake", 1600, blocks)
got = store.get_page(0)
check("3.2 写入后能读回同样的框", got is not None and got["blocks"] == blocks,
      str(got)[:80])
check("3.3 已完成页集合包含该页", 0 in store.load_page_set())
check("3.4 不存在的页返回 None", store.get_page(3) is None)
other = R.OcrStore(STORE_DIR, pdf + ".bak")
check("3.5 不同文件对应不同的缓存库", other.key != store.key,
      f"{store.key} vs {other.key}")
store.close()
other.close()

print()
print("=" * 78)
print("4. 主窗口链路：渲染 → 防抖 → 后台识别 → 拖选 → Ctrl+C")
print("=" * 78)
win = R.MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.show()
app.processEvents()
check("4.1 OCR 状态由配置开启", win.ocr.enabled is True and win.viewer.ocr_enabled is True)
check("4.2 打开书之前没有子进程", win.ocr.process is None)

win.open_pdf(pdf, 1)
app.processEvents()
QTest.qWait(150)
check("4.3 打开书后立刻有识别任务在等（防抖定时器已启动）",
      win.ocr.is_pending(0) or win.ocr._timer.isActive(),
      f"timer={win.ocr._timer.isActive()} pending={win.ocr.is_pending(0)}")

ok = wait_for(lambda: win.ocr.has_page(0) and not win.ocr.is_pending(0), timeout_ms=15000)
check("4.4 后台识别真的跑过并入了缓存（不是读到的旧缓存）",
      ok and len(win.ocr.blocks(0) or []) == 3,
      f"块数={len(win.ocr.blocks(0) or [])} 已识别页={sorted(win.ocr._done)}")
check("4.5 子进程确实起了（模型在独立进程里）", win.ocr.process is not None)
check("4.6 识别完成后选字层可见",
      win.viewer.text_overlay.isVisible() and len(win.viewer.text_overlay.blocks) == 3,
      f"visible={win.viewer.text_overlay.isVisible()} blocks={len(win.viewer.text_overlay.blocks)}")

rects = win.viewer.text_overlay.block_rects()
check("4.7 框能换算成屏幕矩形且落在页面内", len(rects) == 3 and all(r.width() > 5 for r in rects),
      f"首个框={rects[0].getRect() if rects else None}")

# 回归：覆盖层曾经从没被设置过几何尺寸（默认 100x30 的小方块）→ 真实鼠标落不到它上面，
# 拖选完全无效；而 QTest.mousePress(overlay, ...) 直接把事件发给控件，绕过了几何命中。
# 所以这里断言几何铺满视口、且每个文字框都落在覆盖层内。
vp_rect = win.viewer.scroll.viewport().rect()
check("4.7b 选字层几何铺满整个视口",
      win.viewer.text_overlay.geometry() == vp_rect,
      f"覆盖层={win.viewer.text_overlay.geometry().getRect()} 视口={vp_rect.getRect()}")
check("4.7c 每个文字框都落在选字层几何范围内（真实鼠标才能命中）",
      all(vp_rect.contains(r.center()) and win.viewer.text_overlay.rect().contains(r.center())
          for r in rects),
      f"框中心示例={rects[0].center().toTuple() if rects else None}")

# 拖选：从第一行拖到最后一行
ov = win.viewer.text_overlay
QTest.mousePress(ov, Qt.LeftButton, Qt.NoModifier, rects[0].center())
QTest.mouseMove(ov, rects[1].center())
app.processEvents()
QTest.mouseMove(ov, rects[2].center())
app.processEvents()
QTest.mouseRelease(ov, Qt.LeftButton, Qt.NoModifier, rects[2].center())
app.processEvents()
rng = ov.selected_range()
check("4.8 拖选得到连续的三块", rng == (0, 2), f"range={rng}")

text = win.viewer.copy_ocr_selection()
check("4.9 Ctrl+C 拿到的文本按行拼接正确",
      text.count("\n") == 2 and "数列的上极限" in text and "覆盖定理" in text,
      repr(text))
check("4.10 剪贴板内容与复制结果一致",
      QApplication.clipboard().text() == text, repr(QApplication.clipboard().text()[:40]))

# 双击选一行
ov.clear_selection()
QTest.mouseDClick(ov, Qt.LeftButton, Qt.NoModifier, rects[1].center())
app.processEvents()
check("4.11 双击选中一整行（而不是整页）", ov.selected_range() == (1, 1),
      f"range={ov.selected_range()}")

# 覆盖层现在铺满视口，必须确认它不会把滚轮吞掉（否则会用一个 bug 换另一个 bug）。
# 注意：真实滚轮的"向上冒泡"发生在 Qt 的平台投递路径里，app.sendEvent 绕过它，
# 所以这里断言的是契约本身——事件必须保持"未接受"，真实路径才会交给滚动区。
win.viewer._set_zoom(2.0)          # 放大到内容高于视口，才有可滚动范围
app.processEvents()
QTest.qWait(120)
vbar = win.viewer.scroll.verticalScrollBar()
vbar.setValue(0)
app.processEvents()
from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
wheel = QWheelEvent(QPointF(60, 60), QPointF(60, 60), QPoint(0, 0), QPoint(0, -120),
                    Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
app.sendEvent(ov, wheel)
app.processEvents()
check("4.11b 选字层不吞滚轮（事件保持未接受，页面才滚得动）",
      vbar.maximum() > 0 and not wheel.isAccepted(),
      f"vbar 范围 0~{vbar.maximum()} 事件被接受={wheel.isAccepted()}")

# 缓存命中：翻走再翻回来，不应重复提交识别请求
win.viewer.goto_page(2)                     # 第 2 页（索引 1），之前没识别过
app.processEvents()
hits = wait_for(lambda: win.ocr.has_page(1), timeout_ms=15000)
win.viewer.goto_page(1)                     # 回到索引 0：已在缓存里
app.processEvents()
QTest.qWait(600)
check("4.12 新页会被识别", hits and 1 in win.ocr.requests, f"请求记录={win.ocr.requests}")
# 用"提交次数"而不是"已完成集合是否增长"来断言：预取会让你等待时继续识别别的页，
# 集合本来就会变大，但那不是重复识别
check("4.13 回到已识别页不重复提交识别请求",
      win.ocr.requests.count(0) == 1 and win.ocr.blocks(0) is not None,
      f"第 0 页被提交 {win.ocr.requests.count(0)} 次；请求记录={win.ocr.requests}")

# 预取：换到另一本书（换库、状态清零）后，停在某页时会自动把下一页也识别好
pdf2 = os.path.join(TMP, "t2.pdf")
shutil.copyfile(pdf, pdf2)
win.open_pdf(pdf2, 1)
app.processEvents()
check("4.14 换书后 OCR 状态清空（缓存按文件隔离）",
      not win.ocr.has_page(0) and win.ocr.has_page(0) is False,
      f"已识别页={sorted(win.ocr._done)}")
first_ok = wait_for(lambda: win.ocr.has_page(0), timeout_ms=20000)
prefetched = wait_for(lambda: win.ocr.has_page(1) and win.viewer.current_page_index == 0,
                      timeout_ms=20000)
check("4.15 停在某页时会自动预取下一页（翻过去即可直接选字）",
      first_ok and prefetched, f"已识别页={sorted(win.ocr._done)}")

# 无界增长回归：一路识别下去，内存里的文字框字典必须有上限
# （曾经识别结果那条路忘了清理，300 页就堆 300 条）
for p in range(1, 26):
    win.viewer.goto_page(p)
    app.processEvents()
    deadline = time.time() + 2
    while time.time() < deadline:
        app.processEvents()
        if win.ocr.has_page(win.viewer.current_page_index):
            break
        QTest.qWait(10)
check("4.16 内存里保留的文字框页数有上限（不会随识别页数无限增长）",
      len(win.ocr._blocks) <= R.OCR_MEMORY_PAGES + 1,
      f"已识别 {len(win.ocr._done)} 页，内存里只留 {len(win.ocr._blocks)} 页"
      f"（上限 {R.OCR_MEMORY_PAGES}）")
check("4.17 被挤出内存的页仍能从缓存取回文字框",
      win.ocr.blocks(0) is not None and len(win.ocr.blocks(0)) == 3,
      f"第 1 页取回 {len(win.ocr.blocks(0) or [])} 块")

print()
print("=" * 78)
print("5. 快速翻页不干扰：只有停稳的那一页被提交")
print("=" * 78)
# 连翻 4 页，间隔远小于防抖时间
for p in (1, 2, 3, 4, 5):
    if p <= 4:
        win.viewer.goto_page(p)
        app.processEvents()
        QTest.qWait(30)
pending_snapshot = set(win.ocr._pending)
check("5.1 连翻时不会为每一页都排队（防抖生效）", len(pending_snapshot) <= 1,
      f"排队中={sorted(pending_snapshot)}")
settled = wait_for(lambda: not win.ocr._timer.isActive(), timeout_ms=3000)
check("5.2 停下后防抖定时器会自己到期", settled)

print()
print("=" * 78)
print("6. 彻底关闭 = 零开销")
print("=" * 78)
win.toggle_ocr_mode()
app.processEvents()
check("6.1 关闭后子进程被回收", win.ocr.process is None)
check("6.2 关闭后选字层隐藏", not win.viewer.text_overlay.isVisible())
check("6.3 关闭后不再调度（定时器不启动）",
      not win.ocr._timer.isActive() and not win.ocr._idle_timer.isActive())

done_before_off = set(win.ocr._done)
missing = next((p for p in range(4) if p not in done_before_off), None)
if missing is not None:
    win.viewer.goto_page(missing + 1)
    app.processEvents()
    QTest.qWait(500)
check("6.4 关闭后翻到未识别页也不会产生任何请求",
      set(win.ocr._done) == done_before_off and win.ocr.process is None,
      f"已识别页={sorted(win.ocr._done)}（应保持 {sorted(done_before_off)}）")
check("6.5 关闭后拖选不会复制到文字",
      win.viewer.copy_ocr_selection() == "")

win.toggle_ocr_mode()
app.processEvents()
check("6.6 重新打开后恢复调度", win.ocr.enabled is True)

print()
print("=" * 78)
print("7. 速记栏的 Ctrl+C 不被抢（选字层不能在文本框里截胡）")
print("=" * 78)
win.notes_panel.editor.setPlainText("速记里的文字")
win.notes_panel.editor.selectAll()
win.notes_panel.editor.setFocus()
app.processEvents()
QTest.keyClick(win.notes_panel.editor, Qt.Key_C, Qt.ControlModifier)
app.processEvents()
check("7.1 速记框内 Ctrl+C 复制的是速记内容",
      QApplication.clipboard().text() == "速记里的文字",
      repr(QApplication.clipboard().text()[:30]))

print()
print("=" * 78)
print("8. 状态栏与命令注册")
print("=" * 78)
win.viewer.update_status()
check("8.1 状态栏标出 OCR 状态", "OCR" in win.viewer.status_text(), win.viewer.status_text())
keys = [k for k, *_ in R.MainWindow.SKEY if isinstance(k, str)]
names = [c[0] for c in R.MainWindow.EXTRA_COMMANDS]
check("8.2 快捷键表含 Ctrl+Shift+O 与 Ctrl+C",
      "Ctrl+Shift+O" in keys and "Ctrl+C" in keys, f"keys={keys[-6:]}")
check("8.3 命令面板含 OCR 相关命令",
      {"OCR 选字开关", "识别本页文字", "复制选中文字"} <= set(names))

win.close()
app.processEvents()

print()
print("=" * 78)
print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
print("=" * 78)
for name in FAIL:
    print("  FAILED:", name)

for _ in range(5):
    shutil.rmtree(TMP, ignore_errors=True)
    if not os.path.exists(TMP):
        break
    time.sleep(0.2)
sys.exit(1 if FAIL else 0)
