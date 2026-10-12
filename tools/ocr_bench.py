# -*- coding: utf-8 -*-
"""OCR 引擎基准：对同一页跑不同引擎，量耗时与子进程峰值内存。

走的是真实链路（ocr_worker.py 子进程），所以数字与阅读器里的一致。

用法（在 pdf_reader 目录下）：
    python tools/ocr_bench.py 书.pdf 100 --engines rapidocr windows --long-side 1600
    python tools/ocr_bench.py 书.pdf 100 --engines fake          # 不加载模型，验证工具本身

需要 psutil 才能采样内存（pip install psutil）；没有它也能跑，只是不显示内存列。
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
WORKER = os.path.join(PKG, "ocr_worker.py")

try:
    import psutil
except ImportError:
    psutil = None


def run_one(engine: str, pdf: str, page: int, long_side: int, idle: int):
    """跑一次识别，返回 (耗时秒, 峰值MB, 词块数, 文本前若干字)。

    内存必须在**等待响应的过程中**采样：识别中的峰值一旦结束就被释放，
    等收到结果再去看只能看到余量（实测低报了 3 倍：126MB vs 真实 371MB）。
    """
    proc = subprocess.Popen([sys.executable, "-u", WORKER, "--idle", str(idle)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=PKG, text=True, encoding="utf-8")
    peak = [0]
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            if psutil is not None:
                try:
                    me = psutil.Process(proc.pid)
                    peak[0] = max(peak[0], me.memory_info().rss)
                    for ch in me.children(recursive=True):
                        try:
                            peak[0] = max(peak[0], ch.memory_info().rss)
                        except psutil.Error:
                            pass
                except psutil.Error:
                    pass
            time.sleep(0.05)

    thread = threading.Thread(target=sampler, daemon=True)
    thread.start()

    t0 = time.time()
    proc.stdin.write(json.dumps({"cmd": "ocr", "pdf": pdf, "page": page,
                                 "engine": engine, "long_side": long_side}) + "\n")
    proc.stdin.flush()
    msg = None
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            msg = json.loads(line)
        except Exception:
            continue
        break
    dt = time.time() - t0
    stop.set()
    thread.join(timeout=1)

    proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
    proc.stdin.flush()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    mb = peak[0] / 1048576
    if not msg:
        err = (proc.stderr.read() or "").strip().splitlines()
        return dt, mb, 0, "失败：" + (err[-1] if err else "没有输出")
    if not msg.get("ok"):
        return dt, mb, 0, "失败：" + str(msg.get("error"))[:80]
    blocks = msg.get("blocks") or []
    head = " / ".join(b[4] for b in blocks[:2])[:60]
    return dt, mb, len(blocks), head


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("page", type=int, help="页码（1 起）")
    ap.add_argument("--engines", nargs="+", default=["rapidocr", "windows"])
    ap.add_argument("--long-side", type=int, default=1600)
    ap.add_argument("--idle", type=int, default=30)
    args = ap.parse_args()

    page0 = max(0, args.page - 1)
    print(f"文件: {os.path.basename(args.pdf)}  第 {args.page} 页  "
          f"输入长边 {args.long_side}px")
    if psutil is None:
        print("提示: 未安装 psutil，不显示内存列（pip install psutil）")
    print(f"\n{'引擎':<10}{'耗时(s)':>9}{'峰值MB':>8}{'词块':>6}  文本开头")
    print("-" * 78)
    for engine in args.engines:
        dt, mb, n, head = run_one(engine, args.pdf, page0, args.long_side, args.idle)
        print(f"{engine:<10}{dt:>9.1f}{mb:>8.0f}{n:>6}  {head}")


if __name__ == "__main__":
    main()
