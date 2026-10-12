# -*- coding: utf-8 -*-
"""orb_preview.py — 把球渲染成一张对照图，不开窗口就能调外观。

上半：程序画的玻璃球（四套配色 × 三种呼吸强度）
下半：球身图片（三种缩放 × 三种呼吸强度），用的就是 config.json 里的图
每块都在三种底色上试：深色 / 浅色 / 彩色 —— 用来检查剪影会不会糊掉。

    python orb_preview.py        # 写出 orb_preview.png

只用到球的渲染函数，不建窗口、不碰文件监听，也没有事件循环。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from PIL import Image, ImageChops
except ImportError:
    print("这个脚本依赖 Pillow：pip install Pillow", file=sys.stderr)
    raise SystemExit(1)

try:
    import ball
except ImportError as exc:
    # ball.py 顶层 import tkinter。没有 Tk 的 Python 也能画图，
    # 但拿不到球的渲染代码，所以这里给一句人话而不是 traceback。
    print(f"导入 ball.py 失败：{exc}\n（需要一个带 tkinter 的 Python 3.10+）", file=sys.stderr)
    raise SystemExit(1)
except Exception as exc:  # tkinter 存在但起不来（极少见）
    print(f"导入 ball.py 出错：{exc}", file=sys.stderr)
    raise SystemExit(1)

SIZE = 120
PAD = 22
COLORS = [
    ("#0891b2", "#a5f3fc"),
    ("#22d3ee", "#cffafe"),
    ("#7c3aed", "#ddd6fe"),
    ("#f59e0b", "#fef3c7"),
]
GLOWS = [0.0, 0.5, 1.0]
ZOOMS = [1.0, 1.3, 2.0]


def cut_out(orb):
    """把 MAGIC 底色变成真透明。阈值要小：键色近黑，大了会把深色描边一起抠掉。"""
    diff = ImageChops.difference(
        orb, Image.new("RGB", orb.size, ball.MAGIC_RGB)
    )
    alpha = diff.convert("L").point(lambda v: 0 if v < 8 else 255)
    out = orb.convert("RGBA")
    out.putalpha(alpha)
    return out


def main() -> int:
    cfg = ball.deep_merge(ball.DEFAULTS, ball.load_json(ball.CONFIG_PATH))
    dummy = ball.AiBall.__new__(ball.AiBall)
    dummy.cfg = cfg
    dummy._img_cache = {}

    img_rel = (cfg["ball"].get("image") or "").strip()
    has_img = bool(img_rel) and os.path.exists(
        img_rel if os.path.isabs(img_rel) else os.path.join(HERE, img_rel)
    )

    backdrops = [
        Image.new("RGB", (SIZE, SIZE), (24, 30, 44)),
        Image.new("RGB", (SIZE, SIZE), (226, 232, 240)),
        Image.new("RGB", (SIZE, SIZE), (120, 60, 40)),
    ]

    cell = SIZE + PAD
    rows_per_group = len(backdrops)
    cols = max(len(COLORS), len(ZOOMS) if has_img else 0)

    # 图片段前面会多空一行（下面那个 y += PAD），**高度必须算进去** ——
    # 不算的话最后一行会被画布底边切掉 22px（之前就是这个毛病）。
    total_rows = rows_per_group * len(GLOWS) * (2 if has_img else 1)
    width = PAD + cols * cell
    height = PAD + total_rows * cell + (PAD if has_img else 0)

    def paste_orb(sheet, col, y, color, ring, glow):
        orb = cut_out(dummy._orb_frame(SIZE, color, ring, glow))
        for bi, backdrop in enumerate(backdrops):
            tile = backdrop.copy()
            tile.paste(orb, (0, 0), orb)
            sheet.paste(tile, (PAD + col * cell, y + bi * cell))

    sheet = Image.new("RGB", (width, height), (14, 18, 28))

    # ---- 上半：程序画的球（临时清掉图片，否则四列全变成同一张脸）
    saved = cfg["ball"].get("image")
    cfg["ball"]["image"] = ""
    dummy._img_cache = {}

    y = PAD
    for glow in GLOWS:
        for ci, (color, ring) in enumerate(COLORS):
            paste_orb(sheet, ci, y, color, ring, glow)
        y += rows_per_group * cell

    # ---- 下半：球身图片，三种缩放
    if has_img:
        cfg["ball"]["image"] = saved
        cfg["ball"]["image_gloss"] = True
        y += PAD
        for glow in GLOWS:
            for zi, z in enumerate(ZOOMS):
                cfg["ball"]["image_zoom"] = z
                dummy._img_cache = {}
                paste_orb(sheet, zi, y, cfg["ball"]["color"], cfg["ball"]["ring"], glow)
            y += rows_per_group * cell

    out = os.path.join(HERE, "orb_preview.png")
    sheet.save(out)
    print(f"wrote {out}  {sheet.size}  image={img_rel or '(none)'}")
    print(f"内容底沿 {y} / 画布高 {height}  {'OK' if y <= height else '被切了!'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
