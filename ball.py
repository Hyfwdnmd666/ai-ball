# -*- coding: utf-8 -*-
"""ai-ball — 一个不抢焦点的悬浮球，流式显示「内心独白」。

设计原则
--------
1. **零第三方依赖**：只用标准库（tkinter + ctypes）。
   PIL 有就用来把圆画平滑，没有也能跑。
2. **它是哑的**：不 import 游戏、不知道 AI、不知道管道。
   它只做一件事 —— tail 一个文本文件，把新内容流式飘出来。
   所以后面接真管道时，这个文件一行都不用改。
3. **绝不抢焦点**：WS_EX_NOACTIVATE。
   游戏不会因为你在看它而暂停 —— 这是整个方案的命根子。
   所有「调参」手势都绕开了键盘焦点。

操作
----
* 右键点球            弹出控制面板 —— 大小 / 不透明度 / 图片缩放 / 选图 /
                     更多设置 / 退出，全在里面
* 左键点球            收起 / 展开独白面板（菜单开着时先收菜单）
* 拖动球              移动整个窗口
* 滚轮在球上          调不透明度（快捷方式）
* Shift + 滚轮        调球的直径（快捷方式）
* 长按球 0.6 秒       打开控制面板
* 退出                控制面板底部的「退出 ai-ball」，或终端里 Ctrl+C

配置文件
--------
* config.json  —— 样式与参数。**运行期状态，不是出厂设置**：控制面板会实时
                  改写它，所以它在 .gitignore 里。真正的默认值见下面的 DEFAULTS。
* state.json   —— 只存窗口位置和面板开合这两个高频变动项
* themes.json  —— 自定义主题（可选，球会自动合并进来；见 load_themes）
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import font as tkfont

# ---------------------------------------------------------------- Windows API

try:
    import ctypes

    _user32 = ctypes.windll.user32
    _HAS_WIN32 = True
except Exception:  # 非 Windows 或不允许加载
    _user32 = None
    _HAS_WIN32 = False

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk

    _HAS_PIL = True
except Exception:
    _HAS_PIL = False


GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
VK_SHIFT = 0x10

# 虚拟屏幕（所有显示器合起来那块）。**不能用主屏尺寸代替** ——
# 主屏左/上方还可以挂第二块屏，那里是负坐标，只看主屏就永远拖不过去。
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# 被抠掉的颜色：既完全透明，又鼠标穿透。
#
# 为什么是「近黑」而不是常见的品红/亮绿？
# 色键是精确匹配，而抗锯齿的边缘像素是「球色 × 键色」的混合 —— 匹配不上，抠不掉，
# 就会在球周围留一圈键色的毛边。品红毛边在浅色背景上非常刺眼。
# 球的边缘本来就是深色描边，所以把键色选成近黑，那圈过渡就自然融进描边里，看不见了。
MAGIC = "#010203"
MAGIC_RGB = (1, 2, 3)

# 中文 Windows 控制台是 GBK，编不出的字符会让脚本直接崩。宁可打成问号。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")

DEFAULTS = {
    "ball": {
        "x": 60,
        "y": 60,
        "diameter": 88,
        "alpha": 0.92,
        "color": "#0891b2",
        "ring": "#a5f3fc",
        "ring_active": "#ffffff",
        "image": "",
        "image_gloss": True,
        "image_zoom": 1.0,
    },
    "ui": {"theme": "dark"},
    "panel": {
        "visible": True,
        "width": 430,
        "height": 280,
        "gap": 16,
        "side": "right",
        "padding": 12,
        "line_spacing": 4,
        "bg": "#1f1f1f",
        "fg": "#e8e8e8",
        "border": "#3a3a3a",
        "font_family": "Microsoft YaHei UI",
        "font_size": 11,
    },
    "stream": {
        "file": "monologue.log",
        "poll_ms": 120,
        "reveal_ms": 40,
        "chars_per_tick": 2,
    },
    "chat": {
        "input_file": "input.log",
    },
    "status": {
        "file": "status.json",
        "poll_ms": 1000,
        "stale_s": 20,
    },
    # 嗓子跟着球一起起、一起关。见 _start_voice / _stop_voice。
    # 关掉它不影响球：球只负责把话说出来，念不念是另一件事。
    "voice": {
        "enabled": True,
    },
}

# 点击 / 长按的判定
LONG_PRESS_MS = 600
DRAG_SLOP = 3
KEEP_VISIBLE = 44  # 拖动限位：球至少留这么多像素在屏幕内

# 呼吸：把一个完整明暗周期拆成 N 帧，每 BREATH_MS 走一帧。
# 已按要求关掉 —— 静止的球反而更干净，也更省 CPU。
BREATHING = False
N_BREATH = 8
BREATH_MS = 220

# 图片模式下高光要压暗，否则一块白斑正好糊在脸上
IMAGE_GLOSS_SCALE = 0.45

# 右键弹出的控制面板：尺寸和步进
MENU_W = 250
MENU_H = 238
# 二级菜单（「更多设置 ▸」展开的那块），跟主菜单同一套样式
FLYOUT_W = 220
FLYOUT_H = 393

# 面板相对球的位置
SIDE_ORDER = ["right", "bottom", "left", "top"]
SIDE_LABEL = {"right": "右", "bottom": "下", "left": "左", "top": "上"}

# status.json 里的 mood → 球的边缘光颜色。
# 这是留给别人的扩展点：游戏 mod / 大脑 / 随便什么进程，往那个文件写就行。
MOOD_RING = {
    "idle": None,          # None = 用配置里的默认色
    "thinking": "#a78bfa",
    "nervous": "#f87171",
    "smug": "#fbbf24",
    "broken": "#94a3b8",
}
OFFLINE_RING = "#4b5563"   # 心跳过期：球变灰，一眼看出「脑子没了」

# 两套内置调色板。菜单和面板共用同一套，切主题时一起换。
# **可以扩展**：放一个 themes.json 在同目录，里面的主题会合并进来，
# 同名的会覆盖内置的。见 EXTENDING.md。
THEMES = {
    "light": {
        "label": "浅色",
        "menu_bg": "#f9f9f9",
        "menu_border": "#e3e3e3",
        "menu_line": "#e5e5e5",
        "menu_text": "#1c1c1c",
        "menu_sub": "#6b6b6b",
        "menu_hover": "#eaeaea",
        "menu_danger": "#c42b1c",
        "panel_bg": "#f7f7f7",
        "panel_fg": "#1c1c1c",
        "panel_border": "#d8d8d8",
        "you_fg": "#1d4ed8",
    },
    "dark": {
        "label": "深色",
        "menu_bg": "#1f1f1f",
        "menu_border": "#3a3a3a",
        "menu_line": "#383838",
        "menu_text": "#ffffff",
        "menu_sub": "#a6a6a6",
        "menu_hover": "#333333",
        "menu_danger": "#ff99a4",
        "panel_bg": "#1f1f1f",
        "panel_fg": "#e8e8e8",
        "panel_border": "#3a3a3a",
        "you_fg": "#7dd3fc",
    },
}
STEP_DIAMETER = 8
STEP_ALPHA = 0.04
STEP_ZOOM = 0.05


# ---------------------------------------------------------------- DPI


def enable_dpi_awareness() -> float:
    """声明 DPI 感知。**必须在建任何窗口之前调用。**

    不调用的话，在 125% / 150% 缩放的屏幕上，Windows 会把整个窗口当成位图
    拉伸 —— 文字和球身图片全部发虚。这就是「看起来粗糙」的真正来源，
    跟配色和排版都没关系。返回系统缩放倍数。
    """
    if not _HAS_WIN32:
        return 1.0
    ok = False
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        ok = True
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # 老系统的退路
            ok = True
        except Exception:
            pass
    if not ok:
        return 1.0
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


# ---------------------------------------------------------------- 小工具


def as_int(value, default: int, lo=None, hi=None) -> int:
    """把可能被手改坏的配置值转成 int，坏值/None 用默认值。

    配置文件是给人手改的 —— 写个 null、或者把数字写成字符串，都不应该让球
    启动时崩在 `int(None)` 上。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def as_float(value, default: float, lo=None, hi=None) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def split_utf8(data: bytes):
    """把 bytes 切成 (能安全解码的部分, 尾巴上还不完整的序列)。

    UTF-8 一个字符最多 4 字节。写入方分两次 flush 时，尾部可能只剩前半截。
    直接 `errors="replace"` 会把它变成 U+FFFD —— 那是**永久性**的损坏：
    等它补全，那个位置已经被 U+FFFD 占掉了，内容再也读不出来。

    只对「尾部短得不完整」做保留；真的坏字节交给 replace ——
    不能因为一个坏字节把整个流卡住。
    """
    try:
        return data.decode("utf-8"), b""
    except UnicodeDecodeError as exc:
        tail = len(data) - exc.start
        if tail < 4 and (exc.reason == "unexpected end of data" or exc.end >= len(data)):
            return data[: exc.start].decode("utf-8", "replace"), data[exc.start :]
        return data.decode("utf-8", "replace"), b""


_ORB_BODY_CACHE = {}

# 面板里最多留多少行。Text 只增不减，挂一整天的独白会把内存和重排开销吃光。
# 3000 行已经远超「回看最近说了什么」的需要了。
MAX_TEXT_LINES = 3000


def orb_body_cached(d: int, color: str, S: int = 4):
    """球身底图 —— 纯函数，按 (d, color, S) 缓存。

    **为什么必须缓存**：画一帧球要调 4 次 `_orb_frame`（2 个状态 × 暗/亮），
    而这张 128×128 的逐像素光照**只跟颜色有关**。不缓存就是白算 3 遍 ——
    实测 4 次要 136ms，所以每拖一格「大小」Tk 事件循环就冻 ~0.14 秒。
    """
    key = (d, color, S)
    hit = _ORB_BODY_CACHE.get(key)
    if hit is not None:
        return hit

    n = 128
    c = hex_to_rgb(color)
    light = mix(c, (255, 255, 255), 0.58)
    dark = mix(c, (0, 0, 0), 0.60)

    small = Image.new("RGB", (n, n))
    px = small.load()
    cx = cy = (n - 1) / 2.0
    rad = n / 2.0
    for y in range(n):
        for x in range(n):
            dx = (x - cx) / rad
            dy = (y - cy) / rad
            dist = min(1.0, (dx * dx + dy * dy) ** 0.5)
            # 光源在左上方
            lx, ly = dx + 0.40, dy + 0.44
            lit = max(0.0, 1.0 - ((lx * lx + ly * ly) ** 0.5) / 0.98)
            t = min(1.0, 0.74 * (dist ** 1.7) + 0.32 * (1.0 - lit))
            px[x, y] = mix(light, dark, t)
    img = small.resize((d * S, d * S), Image.LANCZOS)

    if len(_ORB_BODY_CACHE) > 16:
        _ORB_BODY_CACHE.clear()
    _ORB_BODY_CACHE[key] = img
    return img


def deep_merge(base: dict, override) -> dict:
    """把 override 合进 base 的副本（只递归 dict）。

    override 不是 dict（文件被写成了数组/字符串/数字）时直接忽略 ——
    以前会在这里 `.items()` 抛 AttributeError，把启动整个干掉。
    """
    out = dict(base)
    if not isinstance(override, dict):
        return out
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[warn] {os.path.basename(path)} 读不了：{exc}")
        return {}
    if not isinstance(data, dict):
        print(f"[warn] {os.path.basename(path)} 顶层不是对象，忽略了")
        return {}
    return data


def write_json(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[warn] {os.path.basename(path)} 存不了：{exc}")


def load_themes() -> dict:
    """内置主题 + `themes.json` 里的自定义主题（同名的覆盖内置）。

    这是留给别人的扩展点之一：**加主题不用改代码**。
    """
    themes = {name: dict(t) for name, t in THEMES.items()}
    path = os.path.join(HERE, "themes.json")
    if not os.path.exists(path):
        return themes
    try:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f) or {}
    except Exception as exc:
        print(f"[warn] themes.json 读不了：{exc}")
        return themes
    if not isinstance(user, dict):
        print("[warn] themes.json 顶层不是对象，忽略了")
        return themes
    for name, t in user.items():
        if name.startswith("_") or not isinstance(t, dict):
            continue  # 下划线开头当注释用
        base = themes.setdefault(name, dict(THEMES["dark"]))
        base.update(t)
    return themes


def hex_to_rgb(h: str) -> tuple:
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (0, 0, 0)


def mix(a: tuple, b: tuple, t: float) -> tuple:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def cover_resize(im, size):
    """等比放大到铺满 size，再从中心裁 —— 非方形图也不会被拉扁。"""
    tw, th = size
    w, h = im.size
    if w <= 0 or h <= 0:
        return im.resize((tw, th), Image.LANCZOS)
    s = max(tw / w, th / h)
    nw, nh = max(tw, int(round(w * s))), max(th, int(round(h * s)))
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return im.crop((left, top, left + tw, top + th))


def import_ball_image(src: str) -> str:
    """把外部图片复制进项目，返回相对路径。

    为什么必须复制：QQ / 微信 / 浏览器下载目录里的图随时会被清掉。
    只记路径的话，某天球就变成一个红叉了。
    """
    if not _HAS_PIL:
        return ""
    try:
        im = Image.open(src).convert("RGBA")
    except Exception as exc:
        print(f"[warn] 图片读不了：{exc}")
        return ""
    d = os.path.join(HERE, "assets")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "ball.png")
    try:
        im.save(out)
    except Exception as exc:
        print(f"[warn] 图片存不了：{exc}")
        return ""
    return os.path.join("assets", "ball.png")


# ---------------------------------------------------------------- 悬浮球


class AiBall:
    def __init__(self) -> None:
        self.cfg = deep_merge(DEFAULTS, load_json(CONFIG_PATH))
        # state.json 只管两个高频项：窗口位置、面板开合
        state = load_json(STATE_PATH)
        if "ball" in state:
            for k in ("x", "y"):
                if k in state["ball"]:
                    self.cfg["ball"][k] = state["ball"][k]
        if "panel" in state and "visible" in state["panel"]:
            self.cfg["panel"]["visible"] = state["panel"]["visible"]

        self._apply_theme()  # 必须在 _build_panel 之前：面板底色由主题提供

        self.panel_visible = bool(self.cfg["panel"]["visible"])
        self.menu_open = False
        self.flyout_open = False
        # 调试用：AIBALL_OPEN_MENU=1 摊开控制面板，=2 连二级菜单一起摊开，
        # 方便截图检查外观
        _open = os.environ.get("AIBALL_OPEN_MENU")
        if _open:
            self.menu_open = True
            if _open == "2":
                self.flyout_open = True
        self._dragging = False
        self._drag_moved = False
        self._press_job = None
        self._long_fired = False
        self._ball_frames = {"idle": [], "active": []}
        self._ball_state = "idle"
        self._breath_i = 0
        self._rendered_key = None
        self._img_cache = {}
        # 球的屏幕坐标 —— 布局的锚点。None = 还没布局过（用 cfg 里的窗口坐标）
        self._ball_screen = None
        # 菜单悬停高亮的「延迟撤销」任务，按 canvas item 记
        self._mm_hover_jobs = {}
        # 状态通道的缓存 ——— 必须在 _create_ball_item() 之前初始化，
        # 因为它会调 _render_ball_frames() → _mood_ring() → 读这两个
        self._status = {}
        self._status_sig = None
        # 上一次见到的 panel 命令序号。None = 还没见过任何命令（那就不执行）
        self._panel_seq = None
        self._compute_geometry()  # 必须先算，后面 _create_ball_item 要用 ball_x

        # ---- 窗口
        # Tk 建窗那一下会把自己变成前台窗口。所以先把原来那个记下来，
        # 等 WS_EX_NOACTIVATE 设好了、窗口也显示出来了，再把前台还回去。
        # 不这么做的话，「不抢焦点」在**启动那一刻**就是破的 —— 实测确实破了：
        # 跑完测试发现球自己已经是前台窗口，测试因此不算数。
        prev_fg = 0
        if _HAS_WIN32:
            try:
                prev_fg = _user32.GetForegroundWindow()
            except Exception:
                prev_fg = 0

        self.root = tk.Tk()
        self.root.withdraw()  # 先藏起来，等样式设完再露脸
        self.root.title("ai-ball")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=MAGIC)
        self._apply_colorkey()
        self.root.update_idletasks()
        self._apply_noactivate()
        self.root.deiconify()
        if prev_fg:
            try:
                _user32.SetForegroundWindow(prev_fg)
            except Exception:
                pass

        # ---- 画布
        self.canvas = tk.Canvas(self.root, bg=MAGIC, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0)
        self.ball_item = self._create_ball_item()

        # 命中测试挂在球这个 item 上。角落的 MAGIC 像素在 OS 层已经穿透，
        # 所以实际点得到的只有圆身。
        self.canvas.tag_bind("ball", "<Button-1>", self._press_ball)
        # 右键（和中键）= 弹出控制面板。
        # 面板是自绘的、长在同一个 NOACTIVATE 窗口里，所以连菜单都不抢焦点。
        self.canvas.tag_bind("ball", "<Button-2>", lambda e: self._toggle_menu())
        self.canvas.tag_bind("ball", "<Button-3>", lambda e: self._toggle_menu())
        # 拖动 / 松手绑在画布上：绑到 item 的话，鼠标一离开球就不触发了
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

        # ---- 独白面板 / 右键控制面板
        self._build_panel()
        self._build_menu()
        self._build_flyout()
        self._menu_sync()

        # ---- 流式读文件
        # 偏移一律按**字节**记，见 _read_new_text 的说明
        f = self.cfg["stream"]["file"]
        self.stream_path = f if os.path.isabs(f) else os.path.join(HERE, f)
        self.byte_offset = 0      # monologue.log 读到的字节位置
        self.tail_bytes = b""     # 尾部被切断的多字节序列，留到下次补全
        self.head = None          # 开头指纹，用来识破「整体重写」
        self.pending = ""
        self.speaking = False
        self._trim_count = 0
        self._closing = False
        self._save_job = None

        # ---- 对话：你说的话走另一个文件，跟独白分开
        g = self.cfg["chat"]["input_file"]
        self.input_path = g if os.path.isabs(g) else os.path.join(HERE, g)
        self.input_offset = 0
        self._input_buf = b""
        self._input_tail = b""
        self._input_head = None
        self._prev_fg = 0
        self._noactivate_on = True

        # ---- 状态通道：任何人往这个文件写 mood / 心跳，球就跟着变
        h = self.cfg["status"]["file"]
        self.status_path = h if os.path.isabs(h) else os.path.join(HERE, h)
        # 已有的输入历史不重放（没有时间戳，混进独白里顺序会乱）
        if os.path.exists(self.input_path):
            try:
                self.input_offset = os.path.getsize(self.input_path)
                with open(self.input_path, "rb") as fp:
                    self._input_head = fp.read(64)
            except OSError:
                pass

        self._relayout()
        self._prime_existing()
        self._poll_file()
        self._reveal_tick()
        self._breath_tick()
        self._poll_status()

        print(
            f"[ai-ball] 窗口 {self.win_w}x{self.win_h}"
            f"+{int(self.cfg['ball']['x'])}+{int(self.cfg['ball']['y'])}"
        )
        print(f"[ai-ball] 独白文件: {self.stream_path}")
        print(f"[ai-ball] 输入文件: {self.input_path}")
        print(
            f"[ai-ball] 布局: 球 x={self.ball_x} y={self.ball_y} d={self.ball_d}"
            f" | 窗口 {self.win_w}x{self.win_h}"
            f" | 槽位 x={self.ball_d + self.gap if self.slot_w else '-'}"
            f" {self.slot_w}x{self.slot_h}"
        )
        self.root.update_idletasks()
        for name, w in (("canvas", self.canvas), ("panel", self.panel), ("menu", self.menu)):
            print(
                f"[ai-ball]   {name}: x={w.winfo_x()} y={w.winfo_y()}"
                f" {w.winfo_width()}x{w.winfo_height()}"
            )
        print(
            f"[ai-ball] 不抢焦点: {'on' if _HAS_WIN32 else 'unavailable'}"
            f" | 平滑圆: {'PIL' if _HAS_PIL else 'tk 原生'}"
        )

    # ------------------------------------------------------------ 窗口属性

    def _apply_colorkey(self) -> None:
        try:
            self.root.attributes("-transparentcolor", MAGIC)
        except tk.TclError as exc:
            print(f"[warn] -transparentcolor 不可用：{exc}")

    def _apply_alpha(self, a: float) -> None:
        a = max(0.15, min(1.0, float(a)))
        try:
            self.root.attributes("-alpha", a)
        except tk.TclError as exc:
            print(f"[warn] -alpha 不可用：{exc}")

    def _effective_alpha(self) -> float:
        """窗口当前该用的不透明度。

        只要有卡片（独白面板 / 右键菜单）在显示，就**必须实心**。
        半透明的卡片看着不像「有背景」—— 底下的窗口会透出来，一眼就脏。
        alpha 是整窗属性，没法只给某一块设置，所以有卡片时整窗不透明，
        只有「光一颗球」的状态才用配置里的透明度。
        """
        if self.menu_open or self.panel_visible:
            return 1.0
        return as_float(self.cfg["ball"].get("alpha"), 0.92, 0.15, 1.0)

    def _apply_theme(self) -> None:
        """把选中主题的颜色灌进菜单和面板。

        菜单颜色是**画上去的**（Canvas item），所以换主题必须重画；
        面板颜色是控件属性，改完调一次 `_apply_panel_style` 就行。

        主题来自 `load_themes()`：内置两套 + `themes.json` 里的自定义。
        """
        self._themes = load_themes()
        name = (self.cfg.get("ui") or {}).get("theme", "dark")
        if name not in self._themes:
            name = "dark" if "dark" in self._themes else next(iter(self._themes))
        self._theme_name = name
        t = self._themes[name]

        self.M_BG = t["menu_bg"]
        self.M_BORDER = t["menu_border"]
        self.M_LINE = t["menu_line"]
        self.M_TEXT = t["menu_text"]
        self.M_SUB = t["menu_sub"]
        self.M_HOVER = t["menu_hover"]
        self.M_DANGER = t["menu_danger"]

        p = self.cfg["panel"]
        p["bg"] = t["panel_bg"]
        p["fg"] = t["panel_fg"]
        p["border"] = t["panel_border"]

    def _theme_label(self) -> str:
        t = getattr(self, "_themes", THEMES).get(
            getattr(self, "_theme_name", "dark"), {}
        )
        return t.get("label") or getattr(self, "_theme_name", "dark")

    def _cycle_theme(self) -> None:
        """在主菜单里循环切换主题。

        做成"循环"而不是"开关"，是为了让 `themes.json` 里加的主题也能选到 ——
        开关只能表达两套。
        """
        names = [n for n in getattr(self, "_themes", THEMES)]
        if not names:
            return
        cur = getattr(self, "_theme_name", "dark")
        nxt = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
        self.cfg.setdefault("ui", {})["theme"] = nxt
        self._apply_theme()
        self._apply_panel_style()
        self._save_config()
        # 推迟一帧再重画：现在正在跑的就是马上要被删掉那个 item 的回调
        self.root.after(1, self._redraw_menus)

    def _redraw_menus(self) -> None:
        self._draw_menu()
        self._draw_flyout()

    def _apply_noactivate(self) -> None:
        """命根子：让窗口被点击但从不激活，游戏因此不失焦。"""
        if not _HAS_WIN32:
            return
        try:
            hwnd = self.root.winfo_id()
            parent = _user32.GetParent(hwnd)
            if parent:
                hwnd = parent
            self._hwnd = hwnd
            ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            _user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )
            self._noactivate_on = True
            self._prev_fg = 0
        except Exception as exc:
            print(f"[warn] WS_EX_NOACTIVATE 设置失败：{exc}")

    def _set_noactivate(self, on: bool) -> None:
        """临时开关「绝不激活」。

        只有点输入框、你明确要打字的那一下才关掉 —— 打完立刻打开并还焦点。
        """
        if not _HAS_WIN32 or not getattr(self, "_hwnd", 0):
            return
        try:
            ex = _user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
            ex = (ex | WS_EX_NOACTIVATE) if on else (ex & ~WS_EX_NOACTIVATE)
            _user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, ex)
            self._noactivate_on = on
        except Exception as exc:
            print(f"[warn] 切换 NOACTIVATE 失败：{exc}")

    @staticmethod
    def _shift_down() -> bool:
        """全局读 Shift 状态 —— 不抢焦点也能知道修饰键。"""
        if not _HAS_WIN32:
            return False
        try:
            return bool(_user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
        except Exception:
            return False

    # ------------------------------------------------------------ 球的外观

    def _create_ball_item(self):
        b = self.cfg["ball"]
        if _HAS_PIL:
            self._render_ball_frames()
            return self.canvas.create_image(
                self.ball_x,
                self.ball_y,
                anchor="nw",
                image=self._ball_frames["idle"][0],
                tags=("ball",),
            )
        x, y = self.ball_x, self.ball_y
        d = int(b["diameter"])
        return self.canvas.create_oval(
            x + 1, y + 1, x + d - 1, y + d - 1,
            fill=b["color"],
            # 没有 Pillow 时也要能看出 mood / 在线状态 —— 否则 status.json
            # 那条通道在没装 Pillow 的机器上就是个摆设（只有颜色变了才算看得见）
            outline=self._mood_ring() or b["ring"],
            width=3, tags=("ball",),
        )

    # ---- 玻璃球渲染 ----------------------------------------------------
    #
    # 色键透明意味着「像素要么全透、要么全不透」，所以做不出外发光。
    # 那就把光全画进球里面：径向渐变 + 左上高光 + 右下边缘光。
    #
    # 只渲染两个极端（最暗/最亮），中间帧用 Image.blend 插值。
    # **但这不等于便宜**：一帧球是 2 个状态 × 暗/亮 = 4 次 _orb_frame，
    # 而那张 128×128 的逐像素光照只跟颜色有关。曾经因为没缓存，
    # 拖一格「大小」要冻 Tk 事件循环 ~140ms（实测）。
    # 现在 _orb_body 按 (直径, 颜色) 缓存 —— 别再把它绕过去。

    def _orb_body(self, d: int, color: str, S: int = 4):
        """球身底图。真正的计算在 orb_body_cached 里，按 (d, color, S) 缓存 ——
        一帧球要画 4 次，不缓存就是白算 3 遍。"""
        return orb_body_cached(d, color, S)

    def _source_layer(self, N: int, d: int, color: str, S: int):
        """球身底图：设了图片就用图，否则程序画一个玻璃球。"""
        path = (self.cfg["ball"].get("image") or "").strip()
        if not path or not _HAS_PIL:
            return self._orb_body(d, color, S).convert("RGBA")

        full = path if os.path.isabs(path) else os.path.join(HERE, path)
        zoom = as_float(self.cfg["ball"].get("image_zoom"), 1.0, 1.0, 4.0)
        try:
            # zoom 必须在缓存键里 —— 否则调缩放时命中旧图，菜单数字在动、
            # 球纹丝不动（之前就是这个毛病）
            stamp = (full, os.path.getmtime(full), N, zoom)
        except OSError:
            return self._orb_body(d, color, S).convert("RGBA")

        if self._img_cache.get("key") == stamp:
            return self._img_cache["img"]
        try:
            raw = Image.open(full).convert("RGBA")
        except Exception as exc:
            print(f"[warn] 球身图片读不了：{exc}")
            return self._orb_body(d, color, S).convert("RGBA")
        img = cover_resize(raw, (N, N))

        # 把图片自己的透明区压成不透明。**球形的透明只该由圆形遮罩决定** ——
        # 不压的话，透明像素最后会落成色键色：既透明又鼠标穿透，
        # 球会变成一个筛子（用透明背景的头像图必然撞上）。
        try:
            if img.getextrema()[3][0] < 255:
                base = Image.new("RGBA", (N, N), hex_to_rgb(color) + (255,))
                base.alpha_composite(img)
                img = base
        except Exception:
            pass

        # 放大：从中心裁一块再拉回来。用来把脸框进圆里。
        if zoom > 1.001:
            c = max(2, int(round(N / zoom)))
            off = (N - c) // 2
            img = img.crop((off, off, off + c, off + c)).resize((N, N), Image.LANCZOS)

        self._img_cache = {"key": stamp, "img": img}
        return img

    def _rim_layer(self, N: int, ring: str, glow: float, scale: float = 1.0):
        """右下那道边缘光。玻璃感基本全靠它。"""
        rim = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        w = max(1, int(N * 0.032))
        ImageDraw.Draw(rim).arc(
            (w, w, N - 1 - w, N - 1 - w),
            start=4,
            end=156,
            fill=hex_to_rgb(ring) + (int((55 + 190 * glow) * scale),),
            width=w,
        )
        return rim.filter(ImageFilter.GaussianBlur(N * 0.024))

    def _gloss_layer(self, N: int, glow: float, scale: float = 1.0):
        """主光斑 + 副光点。副光点才是「玻璃」和「塑料」的分界线。"""
        hi = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hi)
        hx, hy, hr = N * 0.335, N * 0.265, N * 0.150
        hd.ellipse(
            (hx - hr, hy - hr * 0.80, hx + hr, hy + hr * 0.80),
            fill=(255, 255, 255, int((120 + 90 * glow) * scale)),
        )
        sx, sy, sr = N * 0.262, N * 0.203, N * 0.042
        hd.ellipse(
            (sx - sr, sy - sr, sx + sr, sy + sr),
            fill=(255, 255, 255, int((70 + 70 * glow) * scale)),
        )
        return hi.filter(ImageFilter.GaussianBlur(N * 0.026))

    def _orb_frame(self, d: int, color: str, ring: str, glow: float):
        """glow: 0 = 最暗，1 = 最亮。"""
        S = 4
        N = d * S
        use_image = bool((self.cfg["ball"].get("image") or "").strip())

        mask = Image.new("L", (N, N), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, N - 1, N - 1), fill=255)

        out = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        out.paste(self._source_layer(N, d, color, S), (0, 0), mask)

        # 图片靠整体压暗来呼吸；程序画的球靠边缘光呼吸
        if use_image:
            k = 0.16 * (1.0 - glow)
            if k > 0.002:
                r, g, b, a = out.split()
                dimmed = Image.blend(
                    Image.merge("RGB", (r, g, b)),
                    Image.new("RGB", (N, N), (0, 0, 0)),
                    k,
                )
                out = Image.merge("RGBA", (*dimmed.split(), a))

        # 极薄的暗边，定义剪影 —— 不然贴在浅色背景上会糊成一团
        edge = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        ImageDraw.Draw(edge).ellipse(
            (0, 0, N - 1, N - 1),
            outline=mix(hex_to_rgb(color), (0, 0, 0), 0.74) + (190,),
            width=max(1, int(N * 0.012)),
        )
        out = Image.alpha_composite(out, edge)

        # 图片模式可以关掉玻璃光；程序画的球必须有，不然就是个盘子
        gloss = bool(self.cfg["ball"].get("image_gloss", True)) if use_image else True
        gs = IMAGE_GLOSS_SCALE if use_image else 1.0
        # **mood 是信息，玻璃高光是装饰。**
        # 图片球上边缘光是 mood 唯一能落脚的地方，而它原来和光斑一起被 gloss 开关
        # 控制着 —— 于是「关掉玻璃高光」顺手把 mood 通道整个关掉了，而且毫无提示：
        # status.json 里改了 mood，球纹丝不动，看起来像那条通道坏了（实测两个 mood
        # 渲染出来的图逐像素相同）。
        # 现在有 mood 时无论如何都画边缘光；说话/安静那种呼吸感仍然听 gloss 的。
        if gloss or self._mood_ring():
            out = Image.alpha_composite(out, self._rim_layer(N, ring, glow, gs))
        if gloss:
            out = Image.alpha_composite(out, self._gloss_layer(N, glow, gs))

        out = out.resize((d, d), Image.LANCZOS)

        # 压回 MAGIC 底 —— 色键才能把圆外抠掉
        flat = Image.new("RGB", (d, d), MAGIC_RGB)
        flat.paste(out, (0, 0), out)
        return flat

    def _mood_ring(self):
        """按 status.json 决定球此刻的边缘光颜色。None = 用配置里的默认色。"""
        st = self._status or {}
        if "alive" in st and not st["alive"]:
            return OFFLINE_RING
        return MOOD_RING.get(st.get("mood") or "", None)

    def _render_ball_frames(self) -> None:
        b = self.cfg["ball"]
        d = int(b["diameter"])
        self._ball_frames = {}
        for state, ring in (("idle", b["ring"]), ("active", b["ring_active"])):
            ring = self._mood_ring() or ring
            dim = self._orb_frame(d, b["color"], ring, 0.0)
            bright = self._orb_frame(d, b["color"], ring, 1.0)
            if not BREATHING:
                # 不呼吸：取中间亮度，只留一帧
                self._ball_frames[state] = [
                    ImageTk.PhotoImage(Image.blend(dim, bright, 0.45))
                ]
                continue
            self._ball_frames[state] = [
                ImageTk.PhotoImage(
                    Image.blend(
                        dim,
                        bright,
                        0.5 - 0.5 * math.cos(2 * math.pi * i / N_BREATH),
                    )
                )
                for i in range(N_BREATH)
            ]
        self._breath_i = 0

    def _apply_ball_frame(self) -> None:
        """把球画成当前状态。**PIL 和非 PIL 两条路都走这里。**"""
        if not _HAS_PIL:
            # 没有 Pillow 就没有「帧」可换，但 mood / 在线状态 / 说话中都得看得见
            b = self.cfg["ball"]
            ring = self._mood_ring() or (
                b["ring_active"] if self._ball_state == "active" else b["ring"]
            )
            try:
                self.canvas.itemconfigure(
                    self.ball_item,
                    outline=ring,
                    width=4 if self._ball_state == "active" else 3,
                )
            except Exception:
                pass
            return
        frames = self._ball_frames.get(self._ball_state) or []
        if frames:
            self.canvas.itemconfigure(
                self.ball_item, image=frames[self._breath_i % len(frames)]
            )

    def _breath_tick(self) -> None:
        """静止的球看着像控件，会呼吸的球看着像活的 —— 但会被嫌烦，所以做成了开关。"""
        if self._closing or not BREATHING:
            return
        frames = self._ball_frames.get(self._ball_state) or []
        if len(frames) > 1:
            self._breath_i = (self._breath_i + 1) % len(frames)
            self._apply_ball_frame()
        self.root.after(BREATH_MS, self._breath_tick)

    def _show_ball(self, which: str) -> None:
        """idle = 安静；active = 正在说话。

        **只改状态，然后统一交给 `_apply_ball_frame`。**

        原来这里自己写了一套「非 PIL 时按配置设 outline」的分支 —— 结果
        `_relayout` 每次都会调到这里，把 mood / 离线颜色**覆盖回配置里的
        `ring`**，`status.json` 那条通道在没有 Pillow 的机器上等于没有。
        更坑的是：单独测 `_apply_ball_frame()` 是好的（它确实读 `_mood_ring`），
        所以单测会过 —— 因为测的不是真实路径。
        """
        self._ball_state = which
        self._apply_ball_frame()

    # ------------------------------------------------------------ 布局

    def _compute_geometry(self) -> None:
        """球 + 一个「槽位」，槽位可以放在球的四个方向。

        槽位里放独白面板或右键控制面板；两个都不显示时窗口缩到只剩球。
        """
        b, p = self.cfg["ball"], self.cfg["panel"]
        # 全部走 as_int：配置文件是给人手改的，写个 null 或字符串进去
        # 不该让球在启动时崩在 int(None) 上
        # 兜底值直接引用 DEFAULTS，别在这儿再写一个字面量 ——
        # 之前这里写的是 112，而 DEFAULTS 是 88，同一个东西两个默认值
        self.ball_d = as_int(
            b.get("diameter"), DEFAULTS["ball"]["diameter"], 36, 240
        )
        self.panel_w = as_int(p.get("width"), 430, 200, 900)
        self.panel_h = as_int(p.get("height"), 280, 100, 720)
        self.gap = as_int(p.get("gap"), 16, 0, 200)

        if self.menu_open:
            self.slot_w = MENU_W + (4 + FLYOUT_W if self.flyout_open else 0)
            self.slot_h = max(MENU_H, FLYOUT_H if self.flyout_open else 0)
        elif self.panel_visible:
            self.slot_w, self.slot_h = self.panel_w, self.panel_h
        else:
            self.slot_w = self.slot_h = 0

        # 方向是**算出来**的：配置里写的是偏好，放不下就自动换到对面。
        # 不换的话，球靠屏幕右边时菜单有一半在屏幕外，而球是鼠标穿透的，
        # 伸出去的项目永远点不到。
        self.side = self._pick_side(p.get("side", "right"))
        lay = self._layout_for(self.side)
        self.win_w = lay["win_w"]
        self.win_h = lay["win_h"]
        self.ball_x = lay["ball_x"]
        self.ball_y = lay["ball_y"]
        self.slot_x = lay["slot_x"]
        self.slot_y = lay["slot_y"]

    def _layout_for(self, side: str) -> dict:
        """算出「槽位放在 side 这一侧」时的窗口尺寸和球 / 槽位坐标。

        **纯计算，不碰 self** —— 这样 _pick_side 可以拿它试几个方向再定。
        """
        if not getattr(self, "slot_w", 0):
            return {"win_w": self.ball_d, "win_h": self.ball_d,
                    "ball_x": 0, "ball_y": 0, "slot_x": 0, "slot_y": 0}
        if side in ("left", "right"):
            win_w = self.ball_d + self.gap + self.slot_w
            win_h = max(self.ball_d, self.slot_h)
            ball_y = (win_h - self.ball_d) // 2
            slot_y = (win_h - self.slot_h) // 2
            if side == "left":
                slot_x, ball_x = 0, self.slot_w + self.gap
            else:
                ball_x, slot_x = 0, self.ball_d + self.gap
        else:
            win_h = self.ball_d + self.gap + self.slot_h
            win_w = max(self.ball_d, self.slot_w)
            ball_x = (win_w - self.ball_d) // 2
            slot_x = (win_w - self.slot_w) // 2
            if side == "top":
                slot_y, ball_y = 0, self.slot_h + self.gap
            else:
                ball_y, slot_y = 0, self.ball_d + self.gap
        return {"win_w": win_w, "win_h": win_h, "ball_x": ball_x,
                "ball_y": ball_y, "slot_x": slot_x, "slot_y": slot_y}

    def _pick_side(self, prefer: str) -> str:
        """挑一个「整个窗口都放得下」的方向，优先用配置里那个。

        候选顺序：配置的方向 → 正对面 → 剩下两个。一个都放不下就用配置的
        （比如面板比屏幕还大，那怎么放都放不下）。
        """
        if not getattr(self, "slot_w", 0):
            return prefer
        anchor = getattr(self, "_ball_screen", None)
        if not anchor:
            return prefer
        vx, vy, vw, vh = self._virtual_screen()
        opposite = {"right": "left", "left": "right",
                    "top": "bottom", "bottom": "top"}
        cands = [prefer, opposite.get(prefer, "right")]
        cands += [s for s in SIDE_ORDER if s not in cands]
        bx, by = anchor
        for s in cands:
            lay = self._layout_for(s)
            x = bx - lay["ball_x"]
            y = by - lay["ball_y"]
            if (x >= vx and y >= vy
                    and x + lay["win_w"] <= vx + vw
                    and y + lay["win_h"] <= vy + vh):
                return s
        return prefer

    def _place_slot(self) -> None:
        self.panel.place_forget()
        self.menu.place_forget()
        self.flyout.place_forget()
        if not self.slot_w:
            return
        if self.menu_open:
            self.menu.place(
                x=self.slot_x, y=self.slot_y, width=MENU_W, height=MENU_H
            )
            if self.flyout_open:
                self.flyout.place(
                    x=self.slot_x + MENU_W + 4, y=self.slot_y,
                    width=FLYOUT_W, height=FLYOUT_H,
                )
        else:
            self.panel.place(
                x=self.slot_x, y=self.slot_y, width=self.slot_w, height=self.slot_h
            )

    def _virtual_screen(self) -> tuple:
        """所有显示器合起来那块区域 (x, y, w, h)。

        用虚拟屏幕而不是主屏：主屏左/上方还可以挂第二块屏，那里是负坐标，
        只看主屏就永远拖不过去、也算不对「放不放得下」。
        """
        if _HAS_WIN32:
            try:
                return (
                    _user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
                    _user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                    _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
                    _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
                )
            except Exception:
                pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _clamp_origin(self, x: int, y: int) -> tuple:
        """把窗口原点夹到合法范围。

        **两件事必须同时成立**：球不能被推出屏幕（它鼠标穿透，出屏就再也点不到，
        而且这个坐标还会被 state.json 存下去，重启也回不来）；面板/菜单也尽量
        别跑到屏幕外。

        规则：整窗放得下就夹整窗；放不下就保证球至少露 KEEP_VISIBLE 像素。
        注意球的坐标要加上 ball_x/ball_y —— 球在窗口里的位置随面板方向变
        （左侧面板时球在右边、上面板时球在下边），只看窗口原点会算错。
        """
        vx, vy, vw, vh = self._virtual_screen()

        if self.win_w <= vw:
            x = max(vx, min(x, vx + vw - self.win_w))
        else:
            bx = max(vx - self.ball_d + KEEP_VISIBLE,
                     min(x + self.ball_x, vx + vw - KEEP_VISIBLE))
            x = bx - self.ball_x

        if self.win_h <= vh:
            y = max(vy, min(y, vy + vh - self.win_h))
        else:
            by = max(vy - self.ball_d + KEEP_VISIBLE,
                     min(y + self.ball_y, vy + vh - KEEP_VISIBLE))
            y = by - self.ball_y

        return int(x), int(y)

    def _anchored_origin(self, old_ball) -> tuple:
        """用「布局前球的屏幕位置」反推窗口原点，并夹到合法范围。

        **锚点是球，不是窗口左上角。** 球在窗口里的位置随面板方向/开合而变
        （左侧面板时球在右、上面板时球在下）。钉住窗口原点的话，开关一次面板
        就会让鼠标底下的球瞬移几百像素 —— 甚至被顶出屏幕。实测过：
        side=left 时点球收面板，球跳 446px。

        单独提出来是为了能测：`_relayout` 里还要碰一堆真控件。
        """
        b = self.cfg["ball"]
        if old_ball is not None:
            b["x"] = int(old_ball[0]) - self.ball_x
            b["y"] = int(old_ball[1]) - self.ball_y
        return self._clamp_origin(int(b["x"]), int(b["y"]))

    def _relayout(self) -> None:
        """配置变了就重算一切几何。

        锚点见 `_anchored_origin` —— 一句话：**球不动，窗口围着它变形。**
        """
        b = self.cfg["ball"]
        old_ball = getattr(self, "_ball_screen", None)
        self._compute_geometry()

        x, y = self._anchored_origin(old_ball)
        b["x"], b["y"] = x, y
        self._ball_screen = (x + self.ball_x, y + self.ball_y)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{x}+{y}")

        self.canvas.place_configure(width=self.win_w, height=self.win_h)
        self.canvas.configure(width=self.win_w, height=self.win_h)

        if _HAS_PIL:
            # 只有外观项变了才重绘 —— 否则连续调「大小」会反复重画整组帧
            # （球身那张光照现在按颜色缓存了，但 4 次 _orb_frame 的合成开销还在）
            img = (b.get("image") or "").strip()
            try:
                stamp = (
                    os.path.getmtime(
                        img if os.path.isabs(img) else os.path.join(HERE, img)
                    )
                    if img
                    else 0
                )
            except OSError:
                stamp = 0
            key = (
                self.ball_d,
                b["color"],
                b["ring"],
                b["ring_active"],
                img,
                stamp,
                bool(b.get("image_gloss", True)),
                float(b.get("image_zoom", 1.0) or 1.0),
                self._status_sig,
            )
            if key != self._rendered_key:
                self._render_ball_frames()
                self._rendered_key = key
            self.canvas.coords(self.ball_item, self.ball_x, self.ball_y)
            self._apply_ball_frame()
        else:
            self.canvas.coords(
                self.ball_item,
                self.ball_x + 1,
                self.ball_y + 1,
                self.ball_x + self.ball_d - 1,
                self.ball_y + self.ball_d - 1,
            )
            self.canvas.itemconfigure(self.ball_item, fill=b["color"])
            self._show_ball("active" if self.speaking else "idle")

        self._place_slot()
        self._apply_alpha(self._effective_alpha())
        self._apply_panel_style()

    # ------------------------------------------------------------ 独白面板

    def _build_panel(self) -> None:
        p = self.cfg["panel"]
        self.panel = tk.Frame(self.root, bd=0, highlightthickness=1)
        self.mono_font = tkfont.Font(
            family=p["font_family"], size=int(p["font_size"])
        )

        # ---- 底部输入行：先 pack，才能稳稳占住底部
        self.input_row = tk.Frame(self.panel, bd=0, highlightthickness=0)
        self.input_row.pack(side="bottom", fill="x")
        self.entry = tk.Entry(
            self.input_row, relief="flat", bd=0, font=self.mono_font
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(10, 8), pady=7)
        self._ph_text = "说点什么，回车发送 · Esc 取消"
        self._ph_on = False
        self._set_placeholder()
        self.entry.bind("<Button-1>", self._entry_grab, add="+")
        self.entry.bind("<Return>", self._entry_send)
        self.entry.bind("<Escape>", self._entry_cancel)
        self.entry.bind("<FocusIn>", self._entry_focus_in)
        self.entry.bind("<FocusOut>", self._entry_focus_out)

        # ---- 正文区（把手挂在这一层，免得压住输入行）
        self.body = tk.Frame(self.panel, bd=0, highlightthickness=0)
        self.body.pack(side="top", fill="both", expand=True)

        self.text = tk.Text(
            self.body,
            wrap="word",
            relief="flat",
            bd=0,
            font=self.mono_font,
            cursor="arrow",
        )
        # 自绘的细滚动条。tkinter 自带的那个灰粗条，在一身深色里非常出戏。
        self.scroll = tk.Canvas(
            self.body, width=4, highlightthickness=0, bd=0
        )
        self.scroll.pack(side="right", fill="y", padx=(0, 6), pady=10)
        self.text.pack(side="left", fill="both", expand=True)
        self.scroll_thumb = self.scroll.create_rectangle(
            0, 0, 4, 40, fill="#5a5a5a", outline=""
        )
        self.scroll.bind("<Button-1>", self._scroll_jump)
        self.scroll.bind("<B1-Motion>", self._scroll_jump)
        self.text.configure(yscrollcommand=self._on_text_scroll)

        # 右下角的拖拽把手：直接拉就能改面板尺寸，不用进菜单点数字。
        self.grip = tk.Canvas(
            self.body, width=15, height=15,
            highlightthickness=0, bd=0, cursor="sizing",
        )
        for off in (3, 7, 11):
            self.grip.create_line(15 - off, 14, 14, 15 - off, fill="#8a8a8a")
        self.grip.place(relx=1.0, rely=1.0, anchor="se", x=-2, y=-2)
        self.grip.bind("<Button-1>", self._grip_press)
        self.grip.bind("<B1-Motion>", self._grip_drag)
        self.grip.bind("<ButtonRelease-1>", self._grip_release)

        # 正文只读：独白是流进来的，不该手改
        self.text.bind("<Key>", lambda e: "break")
        self.text.configure(state="disabled")

    def _apply_panel_style(self) -> None:
        p = self.cfg["panel"]
        pad = int(p.get("padding", 12))
        ls = int(p.get("line_spacing", 4))
        theme = getattr(self, "_themes", THEMES).get(
            getattr(self, "_theme_name", "dark"), THEMES["dark"]
        )
        self.panel.configure(bg=p["bg"], highlightbackground=p["border"])
        self.input_row.configure(bg=p["bg"])
        self.body.configure(bg=p["bg"])
        self.entry.configure(
            bg=p["bg"],
            fg=p["fg"],
            insertbackground=p["fg"],
            font=self.mono_font,
            disabledbackground=p["bg"],
        )
        self.text.configure(
            bg=p["bg"],
            fg=p["fg"],
            insertbackground=p["fg"],
            font=self.mono_font,
            padx=max(0, pad),
            pady=max(0, pad - 2),
            spacing1=max(0, ls // 2),
            spacing3=max(0, ls),
        )
        # 「你」说的话用强调色，跟独白区分开
        self.text.tag_configure(
            "you",
            foreground=theme["you_fg"],
            spacing1=max(4, ls),
            spacing3=max(4, ls),
            lmargin1=max(0, pad - 4),
            lmargin2=max(0, pad - 4),
        )
        # 滚动条和把手也是 Canvas —— 底色得跟着面板走，
        # 不然会在深色卡片里留一条浅缝
        self.scroll.configure(bg=p["bg"])
        self.grip.configure(bg=p["bg"])
        self.mono_font.configure(
            family=p["font_family"], size=int(p["font_size"])
        )
        # 上面把输入框 fg 设成了正常文字色。如果此刻正显示占位符，
        # 得补回"灰的" —— 否则切完主题，占位符看着就像你真的打了字。
        if getattr(self, "_ph_on", False):
            self.entry.configure(fg=self.M_SUB)

    # ---- 输入框：默认一个字都打不进去，点了才临时借焦点 ----

    def _set_placeholder(self) -> None:
        self.entry.delete(0, "end")
        self.entry.insert(0, self._ph_text)
        self.entry.configure(fg=self.M_SUB)
        self._ph_on = True

    def _clear_placeholder(self) -> None:
        if self._ph_on:
            self.entry.delete(0, "end")
            self.entry.configure(fg=self.cfg["panel"]["fg"])
            self._ph_on = False

    def _entry_focus_in(self, _event=None) -> None:
        self._clear_placeholder()

    def _entry_focus_out(self, _event=None) -> None:
        if not self.entry.get().strip():
            self._set_placeholder()
        # **焦点一离开输入框，标志必须立刻恢复。**
        # 之前只在回车/Esc 里恢复，于是「点输入框 → 打字 → 直接点回游戏」
        # 会让 WS_EX_NOACTIVATE 一直是关的 —— 之后任何一次点球都会真的激活窗口，
        # 游戏失焦暂停。整条命根子就这么断了。
        #
        # 注意：这里只还标志，**绝不能**调 _release_focus() —— 那会把用户
        # 硬拽回上一个窗口，而他明明是自己点走的。
        self._set_noactivate(True)
        self._prev_fg = 0

    def _entry_grab(self, _event=None) -> None:
        """点输入框 = 你明确要打字。只有这一下才借焦点。"""
        if not _HAS_WIN32:
            self.entry.focus_set()
            return
        hwnd = getattr(self, "_hwnd", 0)
        if not hwnd:
            self.entry.focus_set()
            return
        try:
            fg = _user32.GetForegroundWindow()
            # 只记「别人」。连续点两次输入框时，第二次的前台已经是我们自己，
            # 记下来就会在回车时把焦点「还」给自己，原来那个应用再也回不去。
            if fg and fg != hwnd and not self._prev_fg:
                self._prev_fg = fg
            self._set_noactivate(False)
            _user32.SetForegroundWindow(hwnd)
        except Exception as exc:
            print(f"[warn] 借焦点失败：{exc}")
        self.entry.focus_set()

    def _release_focus(self) -> None:
        """打完就把焦点还回去，并恢复「绝不激活」。"""
        self._set_noactivate(True)
        prev = getattr(self, "_prev_fg", 0)
        self._prev_fg = 0
        if not _HAS_WIN32 or not prev:
            return
        try:
            _user32.SetForegroundWindow(prev)
        except Exception:
            pass

    def _entry_send(self, _event=None) -> str:
        text = "" if self._ph_on else self.entry.get().strip()
        if text:
            try:
                with open(self.input_path, "a", encoding="utf-8") as f:
                    f.write(text + "\n")
            except Exception as exc:
                print(f"[warn] 写 {os.path.basename(self.input_path)} 失败：{exc}")
        self._set_placeholder()
        self._release_focus()
        return "break"

    def _entry_cancel(self, _event=None) -> str:
        self._set_placeholder()
        self._release_focus()
        return "break"

    def _on_text_scroll(self, first, last) -> None:
        """滚动条随文本位置更新；内容不长时整个藏起来。"""
        h = self.scroll.winfo_height() or 1
        f, l = float(first), float(last)
        if l - f >= 0.999:
            self.scroll.itemconfigure(self.scroll_thumb, state="hidden")
        else:
            self.scroll.itemconfigure(self.scroll_thumb, state="normal")
            self.scroll.coords(self.scroll_thumb, 0, f * h, 4, max(f * h + 10, l * h))

    def _scroll_jump(self, event) -> None:
        h = self.scroll.winfo_height() or 1
        frac = max(0.0, min(1.0, event.y / h))
        self.text.yview_moveto(max(0.0, min(1.0, frac - 0.08)))

    # ---- 面板右下角的拖拽把手：直接拉就能改尺寸 ----

    def _grip_press(self, event) -> None:
        p = self.cfg["panel"]
        self._grip0 = (
            event.x_root,
            event.y_root,
            int(p["width"]),
            int(p["height"]),
        )
        self._grip_moved = False

    def _grip_drag(self, event) -> None:
        if not getattr(self, "_grip0", None):
            return
        x0, y0, w0, h0 = self._grip0
        self._grip_moved = True
        # 窗口原点被钉住了，所以**加大尺寸永远让面板的外缘往右/往下长** ——
        # 把手就在那个外缘上，因此位移方向恒为 +1，跟面板在哪一侧无关。
        # （之前按「在左/上就取反」写，实测把手会跟鼠标反向、而且两倍速逃。）
        p = self.cfg["panel"]
        p["width"] = max(200, min(900, w0 + (event.x_root - x0)))
        p["height"] = max(100, min(720, h0 + (event.y_root - y0)))
        self._relayout()
        self._flyout_sync()

    def _grip_release(self, _event) -> None:
        self._grip0 = None
        if getattr(self, "_grip_moved", False):
            self._save_config()

    def _cycle_side(self) -> None:
        p = self.cfg["panel"]
        cur = p.get("side", "right")
        nxt = SIDE_ORDER[(SIDE_ORDER.index(cur) + 1) % 4] if cur in SIDE_ORDER else "right"
        p["side"] = nxt
        self._relayout()
        self._save_config()
        self._flyout_sync()

    def _set_panel_visible(self, visible: bool) -> None:
        self.panel_visible = bool(visible)
        self.cfg["panel"]["visible"] = self.panel_visible
        self._save_state()
        self._relayout()

    def _save_state(self) -> None:
        write_json(
            STATE_PATH,
            {
                "ball": {
                    "x": int(self.cfg["ball"]["x"]),
                    "y": int(self.cfg["ball"]["y"]),
                },
                "panel": {"visible": bool(self.panel_visible)},
            },
        )

    def _save_config(self) -> None:
        write_json(CONFIG_PATH, self.cfg)

    def _save_config_soon(self) -> None:
        """去抖地存配置。

        连续滚滚轮时每一格都写一次盘是没必要的 —— 手一动几十格就是几十次写。
        手停了再落盘；退出时 `_flush_save()` 会兜一次，所以不会丢。
        """
        if self._save_job:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.root.after(600, self._save_config_now)

    def _save_config_now(self) -> None:
        self._save_job = None
        self._save_config()

    def _flush_save(self) -> None:
        """把还没落盘的去抖写入立刻落盘（退出时用）。"""
        if self._save_job:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_job = None
        self._save_config()

    # ------------------------------------------------------------ 右键控制面板
    #
    # 自绘的、长在同一个 NOACTIVATE 窗口里 —— 所以连菜单都不抢焦点。
    # 顺手把「藏起来的手势」变成看得见的东西：
    # 滚轮调透明度、Shift+滚轮调大小这些，没人猜得到，现在全摆在这儿。

    def _build_menu(self) -> None:
        self.menu = tk.Canvas(self.root, bg=MAGIC, highlightthickness=0, bd=0)
        self._draw_menu()

    @staticmethod
    def _round_rect(c, x1, y1, x2, y2, r, **kw):
        """Canvas 没有圆角矩形，用平滑多边形凑一个。

        真正的圆角效果其实来自色键：四角落在 MAGIC 上会被抠成透明。
        """
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return c.create_polygon(pts, smooth=True, **kw)

    # Windows 11 深色右键菜单的配色
    M_BG = "#1f1f1f"
    M_BORDER = "#3a3a3a"
    M_LINE = "#383838"
    M_TEXT = "#ffffff"
    M_SUB = "#a6a6a6"
    M_HOVER = "#333333"
    M_DANGER = "#ff99a4"
    M_PAD = 5
    M_IH = 28
    M_FONT = ("Microsoft YaHei UI", 9)
    M_FONT_V = ("Consolas", 9, "bold")

    # ---- 菜单绘制原件：主菜单和二级菜单共用，保证两块钱风格完全一致 ----

    def _ntag(self, prefix: str) -> str:
        self._mseq += 1
        return f"{prefix}{self._mseq}"

    def _mm_frame(self, c, W: int, H: int) -> None:
        """清空重画底板。圆角来自色键，不是画出来的。"""
        self._mseq = 0
        self._mm_hover_jobs.clear()   # 旧 item 要没了，挂起的撤销任务全部作废
        c.delete("all")
        self._round_rect(
            c, 0, 0, W - 1, H - 1, 9,
            fill=self.M_BG, outline=self.M_BORDER, width=1,
        )

    def _mm_cancel_hover(self, c, item) -> None:
        job = self._mm_hover_jobs.pop(item, None)
        if job:
            try:
                c.after_cancel(job)
            except Exception:
                pass

    def _mm_undo_hover(self, c, item) -> None:
        self._mm_hover_jobs.pop(item, None)
        try:
            c.itemconfigure(item, fill=self.M_BG)
        except Exception:
            pass  # item 已经被 delete("all") 干掉了

    def _mm_hover_on(self, c, item) -> None:
        self._mm_cancel_hover(c, item)
        try:
            c.itemconfigure(item, fill=self.M_HOVER)
        except Exception:
            pass

    def _mm_hover_off(self, c, item) -> None:
        self._mm_cancel_hover(c, item)
        self._mm_hover_jobs[item] = c.after(
            1, lambda: self._mm_undo_hover(c, item)
        )

    def _mm_paint(self, c, tag, bg_item, cmd) -> None:
        """整行悬停高亮 + 整行可点。

        **为什么「取消高亮」要延迟一拍**：同一行的底框和文字（还有箭头、数值）
        共用这一个 tag，而 Tk 在鼠标从底框滑到文字上时会发一对
        `<Leave>` + `<Enter>`（实测：ENTER, LEAVE, ENTER, LEAVE）。
        直接响应 Leave 就会把底色改回去 —— 白重绘，顺序反过来还会丢掉高亮。
        推迟 1ms，紧跟其后的 Enter 会把它取消掉。

        延迟任务按 **item** 分别记，所以从 A 行移到 B 行时 A 照样会正常熄灭。
        """
        c.tag_bind(tag, "<Button-1>", lambda e, f=cmd: f())
        c.tag_bind(tag, "<Enter>", lambda e, i=bg_item: self._mm_hover_on(c, i))
        c.tag_bind(tag, "<Leave>", lambda e, i=bg_item: self._mm_hover_off(c, i))

    def _mm_item(self, c, W, y, text, cmd, value="", danger=False, chevron=False):
        """整行可点 —— 这才是右键菜单该有的手感。"""
        tg = self._ntag("mi")
        bg = self._round_rect(
            c, self.M_PAD, y - self.M_IH / 2, W - self.M_PAD, y + self.M_IH / 2, 5,
            fill=self.M_BG, outline="", tags=(tg,),
        )
        c.create_text(
            self.M_PAD + 11, y, text=text, font=self.M_FONT, anchor="w", tags=(tg,),
            fill=self.M_DANGER if danger else self.M_TEXT,
        )
        if chevron:
            c.create_text(
                W - self.M_PAD - 12, y, text="›", anchor="e", tags=(tg,),
                font=("Microsoft YaHei UI", 12), fill=self.M_SUB,
            )
        vid = c.create_text(
            W - self.M_PAD - 11, y, text=value, font=self.M_FONT, anchor="e",
            fill=self.M_SUB, tags=(tg,),
        )
        self._mm_paint(c, tg, bg, cmd)
        return vid

    def _mm_stepper(self, c, vals, W, y, label, key, less, more) -> None:
        c.create_text(
            self.M_PAD + 11, y, text=label, font=self.M_FONT,
            anchor="w", fill=self.M_TEXT,
        )
        vals[key] = c.create_text(
            W - 78, y, text="", font=self.M_FONT_V, anchor="e", fill=self.M_SUB
        )
        for cx, txt, cmd in ((W - 54, "−", less), (W - 26, "+", more)):
            tg = self._ntag("mb")
            bg = self._round_rect(
                c, cx - 10, y - 10, cx + 10, y + 10, 5,
                fill=self.M_BG, outline="", tags=(tg,),
            )
            c.create_text(
                cx, y, text=txt, font=self.M_FONT, tags=(tg,), fill=self.M_TEXT
            )
            self._mm_paint(c, tg, bg, cmd)

    def _mm_sep(self, c, W, y) -> None:
        c.create_line(self.M_PAD + 6, y, W - self.M_PAD - 6, y, fill=self.M_LINE)

    def _draw_menu(self) -> None:
        c = self.menu
        W, H, IH = MENU_W, MENU_H, self.M_IH
        self._mm_frame(c, W, H)
        self._mi_vals = {}

        y = 6 + IH / 2
        self._mi_panel_val = self._mm_item(c, W, y, "独白面板", self._menu_toggle_panel)
        y += IH
        self._mm_stepper(c, self._mi_vals, W, y, "大小", "diameter",
                         lambda: self._bump_diameter(-STEP_DIAMETER),
                         lambda: self._bump_diameter(STEP_DIAMETER))
        y += IH
        self._mm_stepper(c, self._mi_vals, W, y, "不透明度", "alpha",
                         lambda: self._bump_alpha(-STEP_ALPHA),
                         lambda: self._bump_alpha(STEP_ALPHA))
        y += IH
        self._mm_stepper(c, self._mi_vals, W, y, "图片缩放", "zoom",
                         lambda: self._bump_zoom(-STEP_ZOOM),
                         lambda: self._bump_zoom(STEP_ZOOM))
        y += IH / 2 + 5
        self._mm_sep(c, W, y)
        y += 10 + IH / 2
        self._mm_item(c, W, y, "选图片…", self._pick_image)
        y += IH
        self._mm_item(c, W, y, "更多设置", self._toggle_flyout, chevron=True)
        y += IH / 2 + 5
        self._mm_sep(c, W, y)
        y += 10 + IH / 2
        self._mm_item(c, W, y, "退出 ai-ball", self._quit_app, danger=True)

        self._menu_sync()

    def _build_flyout(self) -> None:
        self.flyout = tk.Canvas(self.root, bg=MAGIC, highlightthickness=0, bd=0)
        self._draw_flyout()

    def _draw_flyout(self) -> None:
        """二级菜单。低频项放这儿，主菜单才不会被撑成一长条。"""
        c = self.flyout
        W, H, IH = FLYOUT_W, FLYOUT_H, self.M_IH
        self._mm_frame(c, W, H)
        self._fi_vals = {}

        y = 6 + IH / 2
        self._fi_theme = self._mm_item(c, W, y, "主题", self._cycle_theme)
        y += IH / 2 + 5
        self._mm_sep(c, W, y)
        y += 10 + IH / 2

        self._mm_stepper(c, self._fi_vals, W, y, "字号", "font_size",
                         lambda: self._bump("panel", "font_size", -1, 8, 22),
                         lambda: self._bump("panel", "font_size", 1, 8, 22))
        y += IH
        self._mm_stepper(c, self._fi_vals, W, y, "面板宽", "width",
                         lambda: self._bump("panel", "width", -20, 200, 900),
                         lambda: self._bump("panel", "width", 20, 200, 900))
        y += IH
        self._mm_stepper(c, self._fi_vals, W, y, "面板高", "height",
                         lambda: self._bump("panel", "height", -20, 100, 720),
                         lambda: self._bump("panel", "height", 20, 100, 720))
        y += IH
        self._fi_side = self._mm_item(c, W, y, "面板位置", self._cycle_side)
        y += IH
        self._mm_stepper(c, self._fi_vals, W, y, "间距", "gap",
                         lambda: self._bump("panel", "gap", -4, 0, 80),
                         lambda: self._bump("panel", "gap", 4, 0, 80))
        y += IH
        self._mm_stepper(c, self._fi_vals, W, y, "内边距", "padding",
                         lambda: self._bump("panel", "padding", -2, 0, 36),
                         lambda: self._bump("panel", "padding", 2, 0, 36))
        y += IH
        self._mm_stepper(c, self._fi_vals, W, y, "行距", "line_spacing",
                         lambda: self._bump("panel", "line_spacing", -2, 0, 24),
                         lambda: self._bump("panel", "line_spacing", 2, 0, 24))
        y += IH
        self._mm_stepper(c, self._fi_vals, W, y, "语速", "chars_per_tick",
                         lambda: self._bump("stream", "chars_per_tick", -1, 1, 10),
                         lambda: self._bump("stream", "chars_per_tick", 1, 1, 10))
        y += IH / 2 + 5
        self._mm_sep(c, W, y)
        y += 10 + IH / 2
        self._fi_gloss = self._mm_item(c, W, y, "玻璃高光", self._toggle_gloss)
        y += IH
        self._mm_item(c, W, y, "清除图片", self._clear_image)
        y += IH / 2 + 5
        self._mm_sep(c, W, y)
        y += 10 + IH / 2
        self._mm_item(c, W, y, "恢复默认值", self._reset_all)

        self._flyout_sync()

    def _menu_sync(self) -> None:
        if not getattr(self, "_mi_vals", None):
            return
        b = self.cfg["ball"]
        # 一律走 as_int / as_float：手改 config.json 写个 null 进来，
        # 这里会在启动时抛 TypeError。几何那几条路径早就用 as_int 保护好了，
        # 漏的恰恰是这些「只是显示」的地方。
        self.menu.itemconfigure(
            self._mi_vals["diameter"],
            text=f"{as_int(b.get('diameter'), 88, 36, 240)}",
        )
        self.menu.itemconfigure(
            self._mi_vals["alpha"],
            text=f"{int(round(as_float(b.get('alpha'), 0.92, 0.15, 1.0) * 100))}%",
        )
        self.menu.itemconfigure(
            self._mi_vals["zoom"],
            text=f"{as_float(b.get('image_zoom'), 1.0, 1.0, 3.0):.2f}",
        )
        self.menu.itemconfigure(
            self._mi_panel_val, text="隐藏" if self.panel_visible else "显示"
        )

    def _flyout_sync(self) -> None:
        if not getattr(self, "_fi_vals", None):
            return
        c = self.flyout
        p, s = self.cfg["panel"], self.cfg["stream"]
        # 同样全部走 as_int —— 理由见 _menu_sync
        c.itemconfigure(self._fi_vals["font_size"], text=f"{as_int(p.get('font_size'), 11, 8, 22)}")
        c.itemconfigure(self._fi_vals["width"], text=f"{as_int(p.get('width'), 430, 200, 900)}")
        c.itemconfigure(self._fi_vals["height"], text=f"{as_int(p.get('height'), 280, 100, 720)}")
        c.itemconfigure(
            self._fi_vals["chars_per_tick"],
            text=f"{as_int(s.get('chars_per_tick'), 2, 1, 10)}",
        )
        c.itemconfigure(self._fi_vals["gap"], text=f"{as_int(p.get('gap'), 16, 0, 80)}")
        c.itemconfigure(
            self._fi_vals["padding"], text=f"{as_int(p.get('padding'), 12, 0, 36)}"
        )
        c.itemconfigure(
            self._fi_vals["line_spacing"],
            text=f"{as_int(p.get('line_spacing'), 4, 0, 24)}",
        )
        c.itemconfigure(
            self._fi_side, text=SIDE_LABEL.get(p.get("side", "right"), "右")
        )
        c.itemconfigure(
            self._fi_gloss,
            text="开" if self.cfg["ball"].get("image_gloss", True) else "关",
        )
        c.itemconfigure(self._fi_theme, text=self._theme_label())

    def _toggle_flyout(self) -> None:
        self.flyout_open = not self.flyout_open
        self._relayout()

    def _bump(self, where: str, key: str, delta: float, lo: float, hi: float) -> None:
        bucket = self.cfg["ball"] if where == "ball" else self.cfg[where]
        # 用 as_float 而不是 float()：键被手改成 null 时这里会 KeyError/TypeError，
        # 而它跑在 Tk 回调里 —— 程序不会死，只是按钮点了没反应，很难查
        v = as_float(bucket.get(key), lo, lo, hi) + delta
        bucket[key] = int(round(max(lo, min(hi, v))))
        self._relayout()
        self._save_config_soon()
        self._menu_sync()
        self._flyout_sync()

    def _toggle_gloss(self) -> None:
        b = self.cfg["ball"]
        b["image_gloss"] = not bool(b.get("image_gloss", True))
        self._relayout()
        self._save_config()
        self._flyout_sync()

    def _reset_all(self) -> None:
        """恢复默认值，但保留几样东西：球的位置、球身图片、文件路径、主题。

        * 位置和图片是「内容」不是「样式」—— 重置样式不该把你的脸也换掉。
        * 文件路径（stream/chat/status）也是内容：重置后 `self.stream_path`
          这些是启动时算一次的，**不重算**。如果 cfg 被重置成默认文件名、
          路径却不跟着变，就会出现「cfg 说 monologue.log、球还在 tail 老文件」。
        * 主题保留 + 重算配色：不然面板换了底色、菜单还是旧色，要再点一次
          「主题」才同步（第一次点看着像"没反应"）。
        """
        b = self.cfg["ball"]
        keep_pos = (b["x"], b["y"])
        keep_img = b.get("image", "")
        keep_stream = dict(self.cfg.get("stream") or {})
        keep_chat = dict(self.cfg.get("chat") or {})
        keep_status = dict(self.cfg.get("status") or {})
        keep_theme = (self.cfg.get("ui") or {}).get("theme", "dark")

        self.cfg = deep_merge(DEFAULTS, {})
        b = self.cfg["ball"]
        b["x"], b["y"] = keep_pos
        b["image"] = keep_img
        self.cfg["stream"] = keep_stream
        self.cfg["chat"] = keep_chat
        self.cfg["status"] = keep_status
        self.cfg.setdefault("ui", {})["theme"] = keep_theme
        self.cfg["panel"]["visible"] = self.panel_visible

        self._img_cache = {}
        self._rendered_key = None
        self._apply_theme()          # 重算 M_* 和面板配色
        self._relayout()
        self._apply_panel_style()
        self._save_config()
        self._menu_sync()
        self._flyout_sync()
        self.root.after(1, self._redraw_menus)

    def _toggle_menu(self) -> None:
        self.menu_open = not self.menu_open
        if not self.menu_open:
            self.flyout_open = False  # 收起时把二级菜单也收掉，下次打开是干净的
        self._menu_sync()
        self._relayout()

    def _menu_toggle_panel(self) -> None:
        """从菜单开关面板：顺手收起菜单，让你立刻看到结果。"""
        self.menu_open = False
        self.flyout_open = False  # 二级菜单也要收，否则下次右键它自己冒出来
        self._set_panel_visible(not self.panel_visible)

    def _bump_diameter(self, delta: int) -> None:
        b = self.cfg["ball"]
        b["diameter"] = as_int(
            b.get("diameter"), DEFAULTS["ball"]["diameter"], 36, 240
        ) + delta
        b["diameter"] = max(36, min(240, b["diameter"]))
        self._relayout()
        self._save_config_soon()
        self._menu_sync()

    def _bump_alpha(self, delta: float) -> None:
        b = self.cfg["ball"]
        b["alpha"] = round(as_float(b.get("alpha"), 0.92, 0.15, 1.0) + delta, 3)
        b["alpha"] = round(max(0.15, min(1.0, b["alpha"])), 3)
        self._relayout()
        self._save_config_soon()
        self._menu_sync()

    def _bump_zoom(self, delta: float) -> None:
        b = self.cfg["ball"]
        z = as_float(b.get("image_zoom"), 1.0, 1.0, 3.0) + delta
        b["image_zoom"] = round(max(1.0, min(3.0, z)), 2)
        # 球身图片是按尺寸缓存过的，缩放变了必须让缓存失效 ——
        # 不然菜单数字在动、球纹丝不动（之前就是这个毛病）。
        self._img_cache = {}
        self._relayout()
        self._save_config_soon()
        self._menu_sync()

    def _pick_image(self) -> None:
        src = filedialog.askopenfilename(
            parent=None,
            title="选一张球身图片",
            filetypes=[
                ("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                ("所有文件", "*.*"),
            ],
        )
        if not src:
            return
        rel = import_ball_image(src)
        if not rel:
            # 这里原来是直接 return —— 点了菜单、选了文件、然后什么都没发生。
            # 静默失败是最难查的一类问题，宁可弹个框说清楚。
            if not _HAS_PIL:
                messagebox.showwarning(
                    "选不了图片",
                    "这台机器上没有 Pillow，球身图片用不了。\n\n"
                    "装上就有了：pip install Pillow\n\n"
                    "（球本身不依赖它，没它也能跑 —— 只是只能画程序生成的玻璃球。）",
                )
            else:
                messagebox.showwarning("选不了图片", f"这张图读不了：\n{src}")
            return
        self.cfg["ball"]["image"] = rel
        self._img_cache = {}
        self._relayout()
        self._save_config()
        self._menu_sync()

    def _clear_image(self) -> None:
        self.cfg["ball"]["image"] = ""
        self._img_cache = {}
        self._relayout()
        self._save_config()
        self._menu_sync()

    def _quit_app(self) -> None:
        if not messagebox.askyesno("退出 ai-ball", "关掉这颗球？"):
            return
        # 置这个标志，让还在排队的 after 回调直接返回。
        # 现在 destroy() 之后 mainloop 立刻返回、进程随即退出，挂起的定时器
        # 本来也跑不到；但这是个**隐式**依赖 —— 谁哪天把 destroy() 改成
        # quit()、或者让 mainloop 继续跑，就会满地 "invalid command name"。
        self._closing = True
        self._stop_voice()
        self._save_state()
        self._flush_save()
        self.root.destroy()

    # ------------------------------------------------------------ 交互

    def _press_ball(self, event) -> None:
        """按下球：可能变成拖动、单击、或长按。"""
        self._dragging = True
        self._drag_moved = False
        self._long_fired = False
        self._win_x0 = self.root.winfo_x()
        self._win_y0 = self.root.winfo_y()
        self._drag_origin = (event.x_root, event.y_root)
        self._press_job = self.root.after(LONG_PRESS_MS, self._long_press)

    def _long_press(self) -> None:
        """按住不动 → 打开设置。和拖动 / 单击都不冲突。"""
        if not self._dragging or self._drag_moved:
            return
        self._long_fired = True
        if not self.menu_open:
            self._toggle_menu()

    def _on_drag(self, event) -> None:
        if not self._dragging:
            return
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        if abs(dx) + abs(dy) > DRAG_SLOP:
            if not self._drag_moved:
                self._drag_moved = True
                self._cancel_press_job()
        x, y = self._win_x0 + dx, self._win_y0 + dy
        x, y = self._clamp_origin(int(x), int(y))
        self.root.geometry(f"+{x}+{y}")
        self.cfg["ball"]["x"], self.cfg["ball"]["y"] = x, y
        self._ball_screen = (x + self.ball_x, y + self.ball_y)

    def _on_release(self, _event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._cancel_press_job()
        if self._long_fired:
            return
        if self._drag_moved:
            self._save_state()
        elif self.menu_open:
            self._toggle_menu()  # 菜单开着 → 点球收回菜单
        else:
            self._set_panel_visible(not self.panel_visible)

    def _cancel_press_job(self) -> None:
        if self._press_job is not None:
            try:
                self.root.after_cancel(self._press_job)
            except Exception:
                pass
            self._press_job = None

    def _on_wheel(self, event) -> None:
        """滚轮 = 透明度；Shift + 滚轮 = 直径。全程不需要键盘焦点。"""
        step = 1 if event.delta > 0 else -1
        if self._shift_down():
            self.cfg["ball"]["diameter"] = max(
                36,
                min(240, int(self.cfg["ball"]["diameter"]) + step * STEP_DIAMETER),
            )
            self._relayout()
        else:
            self.cfg["ball"]["alpha"] = round(
                max(
                    0.15,
                    min(1.0, float(self.cfg["ball"]["alpha"]) + step * STEP_ALPHA),
                ),
                3,
            )
            self._apply_alpha(self._effective_alpha())
        self._save_config_soon()
        self._menu_sync()


    # ------------------------------------------------------------ 流式显示

    def _poll_status(self) -> None:
        """读状态通道。

        `status.json` 是留给别人的扩展点，长这样：

            {"alive": 1699999999.0, "mood": "nervous", "panel": true, "panel_seq": 3}

        * `mood`       → 球的边缘光换色（见 MOOD_RING）
        * `alive`      → unix 时间戳；超过 stale_s 秒没更新就当成「脑子没了」，球变灰
        * `panel`      → 想让它显示 / 收起独白面板
        * `panel_seq`  → **命令序号**，见下

        球**不关心是谁写的** —— 游戏 mod、大脑进程、你手写，都一样。

        ### 为什么 panel 要带一个序号

        如果 `panel` 只表示「当前该是什么状态」，那么一个每几秒重写一次文件的
        插件就会和你抢这个开关：你手动收起面板，它下一拍又给你打开。

        所以 `panel` 是**命令**，`panel_seq` 是它的序号 —— 球只在序号变化时执行一次。
        你没让位的时候，你的手动操作一直算数；插件改变主意时，它把序号加一，
        球就再执行一次。
        """
        if self._closing:
            return
        st = {}
        if self.status_path and os.path.exists(self.status_path):
            try:
                with open(self.status_path, "r", encoding="utf-8", errors="replace") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    st = loaded
            except Exception:
                st = {}

        if st:
            ts = st.get("alive")
            if ts is None:
                st["alive"] = True
            else:
                try:
                    fresh = (time.time() - float(ts)) < float(
                        self.cfg["status"]["stale_s"]
                    )
                except Exception:
                    fresh = True
                st["alive"] = fresh

        self._status = st

        # panel 命令：只在序号变化时执行一次，绝不跟用户抢开关。
        # 第一次读到某个序号也算「变化」—— 否则球一重启，插件的下一个命令就没人理，
        # 而那正是「agent 说了话但没人看得见」这个失效模式本身。
        seq = st.get("panel_seq")
        if isinstance(seq, (int, float)) and not isinstance(seq, bool):
            if seq != self._panel_seq:
                want = st.get("panel")
                if isinstance(want, bool) and want != self.panel_visible:
                    self._set_panel_visible(want)
                self._panel_seq = seq
        # 没有 panel_seq 的 panel 一律不执行。没有序号的「panel: true」无法区分
        # 「你刚关掉」和「它一直这么说」，照做就会变成跟用户抢开关。

        sig = (st.get("mood"), st.get("alive"))
        if sig != self._status_sig:
            self._status_sig = sig
            self._rendered_key = None  # 强制重画球
            self._relayout()

        self.root.after(
            int(self.cfg["status"]["poll_ms"]), self._poll_status
        )

    def _prime_existing(self) -> None:
        """启动时把已有内容一次性铺上，不做动画。

        「说话」只该发生在启动之后写入的新内容上。
        否则每次重启都要把整段历史重播一遍 —— 那不是活着，那是录像带。
        """
        if not os.path.exists(self.stream_path):
            return
        try:
            with open(self.stream_path, "rb") as f:
                raw = f.read()
        except OSError:
            return
        if not raw:
            return
        # 尾部可能是被切断的多字节字符，留着等它补全
        text, self.tail_bytes = split_utf8(raw)
        self.byte_offset = len(raw) - len(self.tail_bytes)
        if not text:
            return
        # 启动时别把 MB 级的日志一次塞进 Text：既卡启动，也很容易超上限
        lines = text.splitlines(keepends=True)
        if len(lines) > MAX_TEXT_LINES:
            text = "……（更早的内容省略）\n\n" + "".join(lines[-MAX_TEXT_LINES:])
        self.text.configure(state="normal")
        self.text.insert("end", text)
        self.text.configure(state="disabled")
        self.text.see("end")

    def _read_new_text(self) -> str:
        """按**字节**偏移读新内容，只消费到最后一个完整的 UTF-8 序列。

        **不能按字符偏移 + errors="replace"。** 写入方分两次 flush 时，
        尾部那半个汉字会被解成 U+FFFD 并永久占掉一个字符位 —— 等它补全，
        字符总数没变，`len(content) > offset` 不成立，那段内容就再也读不
        出来了。实测：「你好」的前 5 个字节先写进去，"好"永远丢失。

        顺便用开头指纹识破「整体重写」：只看变短是不够的，被改写成更长
        内容时前面那段会被当成已读直接跳过。
        """
        if not os.path.exists(self.stream_path):
            return ""
        try:
            with open(self.stream_path, "rb") as f:
                head = f.read(64)
                size = os.path.getsize(self.stream_path)
                if size < self.byte_offset or (
                    self.head is not None and head != self.head
                ):
                    # 变短 = 被清空；开头变了 = 被整体重写
                    self.byte_offset = 0
                    self.tail_bytes = b""
                    self.pending = ""
                    self._clear_text()
                self.head = head
                if size <= self.byte_offset:
                    return ""
                f.seek(self.byte_offset)
                raw = f.read()
            self.byte_offset = size
        except OSError:
            return ""

        raw = self.tail_bytes + raw
        text, self.tail_bytes = split_utf8(raw)
        return text

    def _poll_file(self) -> None:
        if self._closing:
            return
        new = self._read_new_text()
        if new:
            self.pending += new
        self._poll_input()
        self.root.after(int(self.cfg["stream"]["poll_ms"]), self._poll_file)

    def _poll_input(self) -> None:
        """看 input.log 有没有新消息。

        跟独白一样按字节读，只处理**完整行**（结尾有换行）——
        否则半行会被当成一条消息切碎。
        """
        if not os.path.exists(self.input_path):
            return
        try:
            with open(self.input_path, "rb") as f:
                head = f.read(64)
                size = os.path.getsize(self.input_path)
                if size < self.input_offset or (
                    self._input_head is not None and head != self._input_head
                ):
                    self.input_offset = 0
                    self._input_buf = b""
                    self._input_tail = b""
                self._input_head = head
                if size <= self.input_offset:
                    return
                f.seek(self.input_offset)
                raw = f.read()
            self.input_offset = size
        except OSError:
            return

        chunk, self._input_tail = split_utf8(self._input_tail + raw)
        if not chunk:
            return
        self._input_buf += chunk.encode("utf-8", "replace")
        # 只消费到最后一个换行 —— 半行留到下次
        cut = self._input_buf.rfind(b"\n")
        if cut < 0:
            return
        block, self._input_buf = self._input_buf[: cut + 1], self._input_buf[cut + 1 :]
        for line in block.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line:
                self._append_you(line)

    def _append_you(self, text: str) -> None:
        """你说的话：立刻出现，不走打字机（那是「我」在说话才用的）。"""
        at_bottom = self.text.yview()[1] >= 0.999
        self.text.configure(state="normal")
        self.text.insert("end", f"\n你：{text}\n", ("you",))
        self._trim_text()
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def _reveal_tick(self) -> None:
        if self._closing:
            return
        n = max(1, int(self.cfg["stream"]["chars_per_tick"]))
        if self.pending:
            chunk, self.pending = self.pending[:n], self.pending[n:]
            self._append_text(chunk)
            if not self.speaking:
                self.speaking = True
                self._show_ball("active")
        elif self.speaking:
            self.speaking = False
            self._show_ball("idle")
        self.root.after(int(self.cfg["stream"]["reveal_ms"]), self._reveal_tick)

    def _append_text(self, s: str) -> None:
        at_bottom = self.text.yview()[1] >= 0.999
        self.text.configure(state="normal")
        self.text.insert("end", s)
        # 打字机每 40ms 才吐几个字，每一下都数行数太浪费 —— 隔一阵查一次
        self._trim_count += 1
        if self._trim_count >= 40:
            self._trim_count = 0
            self._trim_text()
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def _trim_text(self) -> None:
        """**必须在 state="normal" 时调用。**

        面板只保留最后 MAX_TEXT_LINES 行 —— 挂一整天的独白不该把内存吃光，
        也不该让每次插入都越来越慢。
        """
        try:
            total = int(self.text.index("end-1c").split(".")[0])
        except Exception:
            return
        if total > MAX_TEXT_LINES:
            self.text.delete("1.0", f"{total - MAX_TEXT_LINES}.0")

    def _clear_text(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    # ------------------------------------------------------------ 嗓子

    def _start_voice(self) -> None:
        """把 voice.py 拉起来，让它跟着球一起活。

        为什么让球自己起，而不是只写进 start.bat：`start.bat` 只是**一种**启动方式。
        agent 通过插件拉球、你双击 bat、你直接 `python ball.py` —— 三条路都该有声，
        所以嗓子归球管。

        起不来也绝不拖累球：球是主体，嗓子是配件。
        """
        self._voice_proc = None
        if not bool((self.cfg.get("voice") or {}).get("enabled", True)):
            return
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(here, "voice.py")
        if not os.path.exists(script):
            return
        try:
            self._voice_proc = subprocess.Popen(
                [sys.executable, "-u", script],
                cwd=here,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            print(f"[ai-ball] 嗓子没起来（球照常）：{exc}")

    def _stop_voice(self) -> None:
        """球关掉，嗓子跟着关 —— 不然会留一个孤儿进程对着空气说话。"""
        p = getattr(self, "_voice_proc", None)
        self._voice_proc = None
        if p is None:
            return
        try:
            p.terminate()
        except Exception:
            pass

    # ------------------------------------------------------------ 主循环

    def run(self) -> None:
        self._start_voice()
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop_voice()
            self._save_state()
            self._flush_save()


# ---------------------------------------------------------------- 入口


def main() -> int:
    scale = enable_dpi_awareness()
    print(
        f"[ai-ball] DPI 缩放 = {scale:.2f}"
        f"（{'已声明 DPI 感知，窗口不会被系统拉伸' if scale != 1.0 else '100%，无需处理'}）"
    )
    print("[ai-ball] 启动。右键球 = 控制面板（大小 / 透明度 / 图片 / 更多设置 / 退出）。")
    AiBall().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
