# -*- coding: utf-8 -*-
"""OCR 子进程：把识别放到独立进程里跑，主界面只通过 JSON 行通信。

为什么要单独一个进程（而不是线程）：
  1. 模型常驻内存约 400MB —— 独立进程空闲一段时间后可以被直接杀掉，内存立刻归还；
  2. 识别再慢也不会冻结界面（主进程只读一行 JSON，永远不等）；
  3. 引擎崩了不会带走阅读器。

协议（stdin/stdout 各一行一个 JSON）：
  请求  {"cmd":"ocr","pdf":"...","page":0,"engine":"rapidocr","long_side":1600}
        {"cmd":"ping"} / {"cmd":"quit"}
  响应  {"ok":true,"page":0,"engine":"...","long_side":1600,
         "pw":1449.0,"ph":1339.0,"px_w":1600,"px_h":1479,
         "blocks":[[x0,y0,x1,y1,"文字"],...],"ms":2148}
        {"ok":false,"page":0,"error":"..."}  或  {"ok":false,"fatal":"..."}

坐标一律是**页面坐标（pt）**，因为缓存与显示缩放无关。

用法：python ocr_worker.py [--idle 120] [--threads 8]
"""
import argparse
import json
import os
import re
import sys
import threading
import time

# stdio 必须钉死 UTF-8：主进程发来的是 UTF-8 字节，而 Windows 上子进程默认按系统
# 代码页(中文系统是 cp936)解码 stdin —— 中文路径会被解成乱码，于是"文件名太长/找不到
# 文件"，某些字节组合还会把 JSON 里的 \\ 吃掉，报成 "Invalid \escape"。
# （这两个症状是同一个根因；开发时因为 shell 里设了 PYTHONIOENCODING=utf-8，子进程
#   继承了它，所以一直没暴露。）
for _stream, _kw in ((sys.stdin, {"errors": "replace"}),
                     (sys.stdout, {"newline": "\n"}),
                     (sys.stderr, {"errors": "replace"})):
    try:
        _stream.reconfigure(encoding="utf-8", **_kw)
    except Exception:
        pass

# 把线程数压到物理核的一半左右：实测 8 线程最优（16 线程反而更慢，48ms→123ms/批）
DEFAULT_THREADS = 8


def lower_priority():
    """把子进程降到低于正常优先级，尽量不跟界面抢 CPU（Windows）。"""
    try:
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
        return True
    except Exception:
        return False


# ---------------- 文字后处理 ----------------
_CJK = r"\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
_FULLWIDTH_PUNCT = "，。、；：？！（）《》〈〉「」【】·—…％"


def strip_cjk_spaces(text: str) -> str:
    """去掉中文字符之间被识别引擎塞进来的空格。

    Windows 自带 OCR 会输出"数 列 的 上 极 限"，直接复制没法用；但英文单词之间的
    空格必须保留，所以只在"有任一侧是中日韩字符或全角标点"时删空格，
    另外把"数字 . 数字"这种被拆开的标点重新粘起来。
    """
    left = f"[{_CJK}{re.escape(_FULLWIDTH_PUNCT)}]"
    right = f"[{_CJK}{re.escape(_FULLWIDTH_PUNCT)}]"
    prev = None
    while prev != text:
        prev = text
        text = re.sub(f"({left}) +", r"\1", text)
        text = re.sub(f" +({right})", r"\1", text)
        text = re.sub(r"(?<=[0-9]) +(?=[.,:;!?%)\]}])", "", text)   # 5 . 2 → 5.2
        text = re.sub(r"(?<=[(\[{.,:;!?%]) +(?=[0-9])", "", text)
    return text


def normalize_numbers(text: str) -> str:
    """全角数字与'．'归一（不动'，。'这类中文标点本身有意义的字符）。"""
    table = {ord("．"): ".", ord("－"): "-", ord("＋"): "+", ord("／"): "/"}
    for i in range(10):
        table[ord("０") + i] = ord("0") + i
    return text.translate(table)


def clean_text(text: str) -> str:
    # 先归一化全角数字/句点，再按"是否中文邻接"删空格，否则'5 ． 2'会留下'5 . 2'
    return strip_cjk_spaces(normalize_numbers(text))


# ---------------- 引擎：RapidOCR ----------------
def _patch_rapidocr_threads(threads: int):
    """只给 RapidOCR 的 ORT 会话补上线程数（它自己没设）。

    实测同一页（1600px 输入，35 个文字块）：
      自带的默认线程        4.83 s/页，常驻 ~360MB
      8 线程（本函数）       2.30 s/页，常驻 ~140MB
      8 线程 + 开 ORT 内存池 2.30 s/页，常驻 639MB，峰值 1021MB
    内存池只让"首轮预热"快一点，稳态速度完全一样却多吃 500MB+ —— 所以**不开**内存池。
    （这里曾经把两处改动放在一起测，得出"开内存池能提速"的错误结论，已隔离重测更正。）
    """
    import rapidocr_onnxruntime.utils as ru

    orig = ru.InferenceSession

    def tuned_session(path, sess_options=None, providers=None, **kw):
        if sess_options is not None:
            sess_options.intra_op_num_threads = threads
        return orig(path, sess_options=sess_options, providers=providers, **kw)

    ru.InferenceSession = tuned_session


class RapidEngine:
    name = "rapidocr"

    def __init__(self, threads: int):
        _patch_rapidocr_threads(threads)
        from rapidocr_onnxruntime import RapidOCR
        self.ocr = RapidOCR()

    def recognize(self, rgb_array):
        result, _elapse = self.ocr(rgb_array)
        blocks = []
        if result:
            for box, txt, _score in result:
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                blocks.append((min(xs), min(ys), max(xs), max(ys), txt))
        return blocks


# ---------------- 引擎：Windows 自带 OCR ----------------
class WindowsEngine:
    """零安装（系统自带 zh-cn 识别器），约 0.8s/页、峰值 ~135MB，但中文需要后处理。

    在 Windows 上作为"不想装模型"的快速档；质量不如 RapidOCR（数字标点常错）。
    """
    name = "windows"

    def __init__(self):
        import winrt.runtime
        from winrt.windows.media.ocr import OcrEngine
        winrt.runtime.init_apartment(winrt.runtime.ApartmentType.MULTI_THREADED)
        from winrt.windows.globalization import Language
        self._asyncio = __import__("asyncio")
        self.engine = (OcrEngine.try_create_from_language(Language("zh-Hans"))
                       or OcrEngine.try_create_from_user_profile_languages())
        if self.engine is None:
            raise RuntimeError("系统没有可用的 OCR 识别器（需安装 zh-cn OCR 语言包）")

    def recognize(self, rgb_array):
        import numpy as np
        import winrt.runtime
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.security.cryptography import CryptographicBuffer

        img = self._pil_from_array(rgb_array)
        arr = np.ascontiguousarray(np.array(img.convert("RGBA"))[:, :, [2, 1, 0, 3]])
        buf = CryptographicBuffer.create_from_byte_array(arr.tobytes())
        sb = SoftwareBitmap.create_copy_from_buffer(
            buf, BitmapPixelFormat.BGRA8, img.width, img.height)

        async def go():
            return await self.engine.recognize_async(sb)

        result = self._asyncio.run(go())
        blocks = []
        for line in result.lines:
            for w in line.words:
                r = w.bounding_rect
                blocks.append((r.x, r.y, r.x + r.width, r.y + r.height,
                               clean_text(w.text)))
        return blocks

    @staticmethod
    def _pil_from_array(rgb_array):
        from PIL import Image
        return Image.fromarray(rgb_array)


# ---------------- 引擎：fake（测试/自检用） ----------------
class FakeEngine:
    """不加载任何模型，返回固定几行框——用于测试协议与整条链路，秒级完成。"""
    name = "fake"

    def __init__(self, **_kw):
        pass

    def recognize(self, rgb_array):
        h, w = rgb_array.shape[:2]
        return [
            (0.05 * w, 0.10 * h, 0.90 * w, 0.16 * h, "第一行：数列的上极限"),
            (0.05 * w, 0.20 * h, 0.60 * w, 0.26 * h, "second line with latin"),
            (0.05 * w, 0.30 * h, 0.85 * w, 0.36 * h, "第三行：覆盖定理"),
        ]


ENGINES = {"rapidocr": RapidEngine, "windows": WindowsEngine, "fake": FakeEngine}


# ---------------- 主循环 ----------------
def build_engine(kind: str, threads: int):
    cls = ENGINES.get(kind)
    if cls is None:
        raise ValueError(f"未知引擎 {kind}（可用：{', '.join(ENGINES)}）")
    return cls(threads=threads) if cls is RapidEngine else cls()


def render_page_rgb(pdf_path: str, page_index: int, long_side: int):
    """按"长边 ≈ long_side"渲染一页为 RGB ndarray。

    实测输入不是越大越好：2800px 比 1600px 慢 40% 且质量没有提升。
    """
    import fitz
    import numpy as np
    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_index)
        rect = page.rect
        zoom = max(0.2, long_side / max(rect.width, rect.height))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        return arr[:, :, :3].copy(), rect.width, rect.height, zoom
    finally:
        doc.close()


def handle(req: dict, engines: dict) -> dict:
    page = int(req.get("page", 0))
    kind = req.get("engine", "rapidocr")
    long_side = int(req.get("long_side", 1600))
    threads = int(req.get("threads", DEFAULT_THREADS))
    t0 = time.time()
    try:
        if kind not in engines:
            engines[kind] = build_engine(kind, threads)
        rgb, pw, ph, zoom = render_page_rgb(req["pdf"], page, long_side)
        raw = engines[kind].recognize(rgb)
        # 像素坐标 → 页面坐标(pt)：与显示缩放、去白边都无关
        blocks = [[x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom, txt] for x0, y0, x1, y1, txt in raw]
        return {"ok": True, "page": page, "engine": kind, "long_side": long_side,
                "pw": pw, "ph": ph, "px_w": rgb.shape[1], "px_h": rgb.shape[0],
                "blocks": blocks, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:      # 失败必须可见，不能让主进程一直等
        import traceback
        return {"ok": False, "page": page, "engine": kind,
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-800:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle", type=float, default=0, help="空闲多少秒后自动退出（0=不退出）")
    ap.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    args = ap.parse_args()

    lower_priority()
    engines = {}
    # 空闲看门狗：主循环会被 stdin 阻塞，所以用后台线程来兜底退出。
    # 这样即使主界面崩了（stdin 没关干净），模型占的内存也不会一直挂着。
    activity = {"t": time.time()}

    def watchdog():
        while True:
            time.sleep(0.5)
            if args.idle > 0 and time.time() - activity["t"] > args.idle:
                os._exit(0)

    if args.idle > 0:
        threading.Thread(target=watchdog, daemon=True).start()

    for line in sys.stdin:
        activity["t"] = time.time()
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"请求不是合法 JSON: {e}"}), flush=True)
            continue
        cmd = req.get("cmd", "ocr")
        if cmd == "quit":
            break
        if cmd == "ping":
            print(json.dumps({"ok": True, "pong": True, "loaded": list(engines)}), flush=True)
            continue
        if cmd != "ocr":
            print(json.dumps({"ok": False, "error": f"未知命令 {cmd}"}), flush=True)
            continue
        req.setdefault("threads", args.threads)
        print(json.dumps(handle(req, engines), ensure_ascii=False), flush=True)
        activity["t"] = time.time()
    return 0


if __name__ == "__main__":
    sys.exit(main())
