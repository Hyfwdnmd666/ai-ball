# -*- coding: utf-8 -*-
"""_jumpcheck.py — 实测「开关面板时球会不会跳」。

道理很简单：球的屏幕坐标 = 窗口原点 + 球在窗口内的偏移。
点一下球会开关面板（窗口尺寸变），如果锚点是对的，球的屏幕坐标不该动。

用硬件 HID 点，因为合成点击在这个 DPI 空间里对不上。
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes

import serial

user32 = ctypes.windll.user32


class RECT(ctypes.Structure):
    _fields_ = [("L", ctypes.c_long), ("T", ctypes.c_long),
                ("R", ctypes.c_long), ("B", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("X", ctypes.c_long), ("Y", ctypes.c_long)]


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


def rect(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.L, r.T, r.R - r.L, r.B - r.T


def cursor():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.X, p.Y


def hid(ser, cmd):
    ser.write((cmd + "\n").encode())
    ser.flush()
    return ser.readline().decode("utf-8", "replace").strip()


def move_to(ser, target, tries=80):
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
        hid(ser, f"m {step(dx)} {step(dy)}")
        time.sleep(0.03)
    cx, cy = cursor()
    return abs(cx - target[0]) <= 4 and abs(cy - target[1]) <= 4


def ball_pos(hwnd):
    """球在屏幕上的中心。球是 90x90（=112/1.25），水平位置看窗口宽度：
    面板收起时窗口就是球本身，展开时球仍在最左、垂直居中。"""
    L, T, W, H = rect(hwnd)
    r = 45
    # 球要么贴左（面板在右），要么贴右（面板在左）。用窗口宽度判断哪一侧。
    # 这里只处理「面板在右 / 收起」两种情况就够了 —— 测的就是默认布局。
    cx = L + r
    cy = T + (H - r * 2) // 2 + r
    return cx, cy, W, H


def main():
    hwnd = find_ball()
    if not hwnd:
        print("球没在跑")
        return 1

    port = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AIBALL_PORT", "COM5")
    try:
        ser = serial.Serial(port, 115200, timeout=1.5)
    except Exception as exc:
        print(f"打不开串口 {port}: {exc}")
        return 1
    time.sleep(1.0)
    ser.reset_input_buffer()
    hid(ser, "p")

    save = cursor()
    before = ball_pos(hwnd)
    print(f"点击前: 窗口 {before[2]}x{before[3]}  球心 ({before[0]},{before[1]})")

    ok = move_to(ser, (before[0], before[1]))
    if not ok:
        print(f"没挪到位（实际 {cursor()}）—— 你是不是在动鼠标？")
        move_to(ser, save)
        ser.close()
        return 1

    hid(ser, "c 1")
    time.sleep(0.6)

    after = ball_pos(hwnd)
    print(f"点击后: 窗口 {after[2]}x{after[3]}  球心 ({after[0]},{after[1]})")

    if after[2] == before[2]:
        print("\n窗口尺寸没变 → 这一下没点中，结果不算数")
    else:
        dx = abs(after[0] - before[0])
        dy = abs(after[1] - before[1])
        print(f"\n球移动了 dx={dx} dy={dy}")
        if dx == 0 and dy == 0:
            print("*** 球纹丝不动 —— 锚点修对了 ***")
        elif dx + dy <= 4:
            print("*** 基本没动（<=4px，可以接受）***")
        else:
            print("*** 球跳了 —— 锚点还是不对 ***")
        # 点回去，恢复原状
        move_to(ser, (after[0], after[1]))
        hid(ser, "c 1")
        time.sleep(0.4)

    move_to(ser, save)
    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
