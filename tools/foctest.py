# -*- coding: utf-8 -*-
"""foctest.py — 用硬件 HID 做决定性的「点球会不会抢焦点」测试。

为什么非要用硬件 HID：它发的是**相对位移**，配上「读回真实光标 → 补差值」的
反馈回路，就完全不需要知道 DPI 缩放是多少 —— 目标和光标读数在同一个坐标空间，
自洽。用 SendInput 那条路我在这上面栽过两次（拿别的进程的坐标去点，永远对不上）。

用法：
    python tools/foctest.py            # 默认 COM5
    python tools/foctest.py COM7       # 或指定串口
    set AIBALL_PORT=COM7

**这个脚本会真的移动你的鼠标**，跑完（哪怕是出错或 Ctrl+C）都会挪回原位。

判据是硬的：点球会开关面板，所以**窗口宽度变化 = 确实点中了**；
在"确实点中"的前提下看前台窗口有没有变。点不中就不算数 —— 宁可报失败。

没有硬件板子也能做同样的测试：用自己的手点球，看记事本的光标闪不闪。
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes

import serial

# 中文 Windows 控制台是 GBK，编不出的字符会让脚本直接崩（U+2717 就不在 GBK 里）。
# 宁可打成问号，也别崩 —— 跟 ball/brain/feed 保持一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 复用球自己的几何代码。**不要在这里重写一遍布局**：直径、面板方向、
# 面板宽高都是可调的，写死必然对不上（第一版就写死了 ball_r = 45，
# 只要把直径调离 90、或者把面板换到左边，点击就落空，
# 然后还会把"点空了"误报成"不抢焦点成立"）。
import ball as ballmod  # noqa: E402

user32 = ctypes.windll.user32

BAUD = 115200


class RECT(ctypes.Structure):
    _fields_ = [("L", ctypes.c_long), ("T", ctypes.c_long),
                ("R", ctypes.c_long), ("B", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("X", ctypes.c_long), ("Y", ctypes.c_long)]


# ---------------------------------------------------------------- 窗口


def find_ball():
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        title = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        user32.GetWindowTextW(hwnd, title, 256)
        if cls.value.startswith("Tk") and "ai-ball" in title.value:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def rect_of(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.L, r.T, r.R - r.L, r.B - r.T


def fg_title():
    h = user32.GetForegroundWindow()
    t = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(h, t, 256)
    return h, t.value


def cursor():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.X, p.Y


def set_cursor(x, y):
    user32.SetCursorPos(int(x), int(y))


# ---------------------------------------------------------------- 几何


def load_model():
    """按球自己的规则（config + state）算出当前布局。"""
    cfg = ballmod.deep_merge(ballmod.DEFAULTS, ballmod.load_json(ballmod.CONFIG_PATH))
    state = ballmod.load_json(ballmod.STATE_PATH)
    if isinstance(state.get("ball"), dict):
        for k in ("x", "y"):
            if k in state["ball"]:
                cfg["ball"][k] = state["ball"][k]
    if isinstance(state.get("panel"), dict) and "visible" in state["panel"]:
        cfg["panel"]["visible"] = state["panel"]["visible"]

    obj = ballmod.AiBall.__new__(ballmod.AiBall)
    obj.cfg = cfg
    obj.menu_open = False
    obj.flyout_open = False
    obj.panel_visible = bool(cfg["panel"]["visible"])
    obj._ball_screen = None  # 没锚点 -> _pick_side 直接用配置里的方向
    obj._compute_geometry()
    return obj


def ball_center(hwnd, model):
    """球心在屏幕上的坐标（foctest 自己的坐标空间）+ 实测窗口尺寸。

    窗口矩形来自 GetWindowRect、光标来自 GetCursorPos —— **同一个进程**，
    所以它们必然在同一个坐标空间里，不需要知道 DPI 缩放是多少。

    球在窗口内的偏移取**比例**（模型偏移 / 模型窗口尺寸），再乘实测窗口尺寸。
    这样即使这个进程的 DPI 感知状态和球不同，也算得对。
    """
    L, T, W, H = rect_of(hwnd)
    sx = W / model.win_w if model.win_w else 1.0
    sy = H / model.win_h if model.win_h else 1.0
    cx = L + (model.ball_x + model.ball_d / 2.0) * sx
    cy = T + (model.ball_y + model.ball_d / 2.0) * sy
    return int(round(cx)), int(round(cy)), W, H


# ---------------------------------------------------------------- 串口


def hid(ser, cmd, timeout=None):
    """发一条命令，返回板子的回应（固件每条命令都回 ok / err ...）。"""
    if timeout is not None:
        ser.timeout = timeout
    ser.write((cmd + "\n").encode())
    ser.flush()
    return ser.readline().decode("utf-8", "replace").strip()


def wait_ready(ser, tries=6):
    """等板子启动完。

    打开串口时 DTR 会拉一下，Pro Micro 因此复位、跑一遍 setup()。
    不等它就发命令，字节会丢 —— 结果表现为"怎么挪都挪不到位"，
    然后被误诊成"你在动鼠标"。
    """
    for i in range(tries):
        time.sleep(1.0)
        ser.reset_input_buffer()
        try:
            reply = hid(ser, "p", timeout=1.5)
        except Exception:
            continue
        if reply == "ok":
            return True
        print(f"    等板子… 第 {i + 1} 次回应: {reply!r}")
    return False


def move_to(ser, target, tries=80):
    """相对位移 + 反馈，把光标挪到 target。返回是否到位。

    关键是**小步走**：一次跳 1500 像素会被 Windows 的指针加速放大到飞掉，
    实测直接跑到屏幕另一头。每步不超过 40px、而且只走剩余距离的一半，
    就能几何收敛，加速度也吃不出大偏差。
    """
    def step(d):
        s = int(d / 2)
        if s == 0 and d != 0:
            s = 1 if d > 0 else -1
        return max(-40, min(40, s))

    for _ in range(tries):
        cx, cy = cursor()
        dx, dy = target[0] - cx, target[1] - cy
        if abs(dx) <= 2 and abs(dy) <= 2:
            return True
        reply = hid(ser, f"m {step(dx)} {step(dy)}", timeout=3.0)
        if reply.startswith("err"):
            print(f"    板子报错: {reply}")
            return False
        time.sleep(0.03)
    cx, cy = cursor()
    return abs(cx - target[0]) <= 4 and abs(cy - target[1]) <= 4


# ---------------------------------------------------------------- 主流程


def run(ser):
    hwnd = find_ball()
    if not hwnd:
        print("球没在跑。")
        return 1

    model = load_model()
    cx, cy, W, H = ball_center(hwnd, model)
    print(f"球的布局: 方向={model.side} 直径={model.ball_d} "
          f"面板={'开' if model.panel_visible else '关'} "
          f"槽位={model.slot_w}x{model.slot_h}")
    print(f"窗口(实测) {W}x{H}   球心 ({cx},{cy})")

    save = cursor()
    print(f"光标原位置 {save}")

    ok = move_to(ser, (cx, cy))
    got = cursor()
    print(f"移动到球心: {'到位' if ok else '没到位'}  实际 ({got[0]},{got[1]})")
    if not ok:
        print("（你刚才在动鼠标的话，重跑一次；或者你亲手点一下最快）")
        return 1

    fg1, t1 = fg_title()
    hid(ser, "c 1", timeout=3.0)
    time.sleep(0.6)
    fg2, t2 = fg_title()
    _, _, W2, _ = rect_of(hwnd)

    print(f"窗口宽 {W} -> {W2}")
    print(f"点击前前台: {t1!r}")
    print(f"点击后前台: {t2!r}")
    print("-" * 48)

    if W2 == W:
        print("窗口宽度没变 -> 这一下没点中，**结果不算数**")
        return 1

    # 点中了。点回去，恢复原状
    move_to(ser, (cx, cy))
    hid(ser, "c 1", timeout=3.0)
    time.sleep(0.4)

    if fg1 == fg2:
        print("[OK] 命中球，且焦点没变 —— 「不抢焦点」成立")
        return 0
    print("[x] 命中球，但焦点变了 —— 球抢焦点了")
    print(f"   {t1!r} -> {t2!r}")
    return 2


def main():
    if not find_ball():
        print("球没在跑。先启动它，再跑这个测试。")
        return 1

    # 光标要在开串口**之前**记 —— 开串口会等板子复位，期间不该有位移
    save = cursor()

    port = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AIBALL_PORT", "COM5")
    print(f"串口: {port}")
    try:
        ser = serial.Serial(port, BAUD, timeout=1.5)
    except Exception as exc:
        print(f"打不开串口: {exc}")
        print("（没接硬件 HID 板子就跑不了这个测试 —— 手动点一下球是一样的。）")
        return 1

    try:
        if not wait_ready(ser):
            print("板子一直没回应 ok。检查接线 / 是不是别的程序占着串口。")
            return 1
        print("板子就绪。")
        return run(ser)
    except KeyboardInterrupt:
        print("\n中断了。")
        return 130
    finally:
        # 无论怎么退出，光标都要还回去、串口都要关 ——
        # 不然光标会停在屏幕中间，端口也不释放，下次跑直接失败
        try:
            move_to(ser, save)
        except Exception:
            try:
                set_cursor(*save)
            except Exception:
                pass
        try:
            ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
