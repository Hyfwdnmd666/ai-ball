# -*- coding: utf-8 -*-
"""feed.py — 往球里喂字。

球读两个文件，这个脚本是它们的手动替身：

    python feed.py "我操，三发实弹还没出"      # 「我」的独白  → monologue.log
    python feed.py --as you "你在干嘛"          # 「你」说的话  → input.log
    python feed.py --demo                       # 播放内置样本（模拟真人语速）
    python feed.py --reset                      # 清空独白
    python feed.py --reset --as you             # 清空对话
    python feed.py --stdin                      # 从管道读，逐行追加
"""

from __future__ import annotations

import argparse
import locale
import os
import random
import sys
import time

# 中文 Windows 控制台是 GBK，编不出的字符会让脚本直接崩。宁可打成问号。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
MONOLOGUE = os.path.join(HERE, "monologue.log")
INPUT = os.path.join(HERE, "input.log")
DEMO = os.path.join(HERE, "demo_monologue.txt")

# 在这些标点后面多停一下，读起来才像人说话
PAUSE = {
    "。": 0.34, "，": 0.16, "？": 0.34, "！": 0.34,
    "…": 0.30, "：": 0.18, "；": 0.20, "\n": 0.28,
}


def read_stdin_lines():
    """逐行读管道，自己决定编码。

    **不要直接用 sys.stdin 迭代。** 管道场景下它的编码未必是 UTF-8：
    实测 PowerShell 在 chcp 65001 下按 UTF-8 推管道，而管道里
    sys.stdin.encoding 是 gbk —— 解出孤立代理，写文件时直接
    UnicodeEncodeError 崩掉（exit 1，文件被建出来但是 0 字节）。

    所以按字节读，先按 UTF-8 试，失败再退回系统编码，最后才 replace。
    中文在两种编码之间几乎不会互相"碰巧合法"，这个顺序很稳。
    """
    try:
        data = sys.stdin.buffer.read()
    except Exception:
        return
    encodings = ["utf-8"]
    try:
        pref = locale.getpreferredencoding(False)
        if pref and pref.lower() not in ("utf-8", "utf8"):
            encodings.append(pref)
    except Exception:
        pass
    for enc in encodings:
        try:
            text = data.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = data.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.strip():
            yield line


def append(text: str, path: str = MONOLOGUE) -> None:
    with open(path, "a", encoding="utf-8", errors="replace") as f:
        f.write(text)
        f.flush()


def reset(path: str = MONOLOGUE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("")


def speak(text: str, cps: float = 22.0, path: str = MONOLOGUE) -> None:
    """按「字/秒」的节奏把 text 拆成小块吐出去，标点处多停。"""
    i = 0
    n = len(text)
    while i < n:
        chunk_len = max(1, int(random.gauss(3.2, 1.4)))
        chunk = text[i : i + chunk_len]
        i += chunk_len
        append(chunk, path)
        base = chunk_len / cps
        time.sleep(base + PAUSE.get(chunk[-1:], 0.0) * 0.5)


def main() -> int:
    ap = argparse.ArgumentParser(description="往球里喂字")
    ap.add_argument("text", nargs="*", help="要追加的文字")
    ap.add_argument(
        "--as", dest="role", default="me", choices=["me", "you"],
        help="me = 独白（monologue.log），you = 你说的话（input.log）",
    )
    ap.add_argument("--demo", action="store_true", help="播放内置样本（只对 me 有效）")
    ap.add_argument("--demo-file", default=DEMO)
    ap.add_argument("--reset", action="store_true", help="清空目标文件")
    ap.add_argument("--stdin", action="store_true", help="从标准输入逐行追加")
    ap.add_argument("--cps", type=float, default=22.0, help="语速：字/秒")
    ap.add_argument("--fast", action="store_true", help="不按语速，直接整段写入")
    args = ap.parse_args()

    path = INPUT if args.role == "you" else MONOLOGUE
    who = "你" if args.role == "you" else "我"
    name = os.path.basename(path)

    if args.reset:
        reset(path)
        print(f"[feed] 已清空 {name}")
        if not (args.demo or args.text or args.stdin):
            return 0

    if args.demo:
        if args.role == "you":
            print("[feed] --demo 是「我」的独白样本，--as you 时忽略", file=sys.stderr)
            return 0
        if not os.path.exists(args.demo_file):
            print(f"[feed] 找不到样本：{args.demo_file}", file=sys.stderr)
            return 2
        if args.reset:
            reset(path)
        else:
            # 别顺手清掉正在球上显示、以及 brain 刚写进去的内容 ——
            # 清空是个破坏性动作，只该在明确要求时发生。
            print(f"[feed] 保留 {name} 已有内容（要清空加 --reset）")
        with open(args.demo_file, "r", encoding="utf-8") as f:
            body = f.read()
        print(f"[feed] 播放样本，{len(body)} 字，语速 {args.cps} 字/秒…")
        if args.fast:
            append(body, path)
        else:
            speak(body, args.cps, path)
        print("[feed] 播完了。")
        return 0

    if args.stdin:
        for line in read_stdin_lines():
            append(line + "\n", path)
            print(f"[feed] {who} + {line}")
        return 0

    if args.text:
        msg = " ".join(args.text)
        if not msg.endswith("\n"):
            msg += "\n"
        append(msg, path)
        print(f"[feed] {who} + {msg.rstrip()}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
