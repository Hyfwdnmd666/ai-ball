# -*- coding: utf-8 -*-
"""selftest.py — 回归测试。每一条都对应一个**真实修过的 bug**。

改完代码跑一下，能立刻知道有没有把它们弄回去：

    python tools/selftest.py

不需要显示器（Tk 相关的会自己跳过）、不需要硬件、不需要网络、不花钱。
用一个迷你跑法，不引入 pytest —— 这个项目本体就是零依赖的。
"""

import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ball  # noqa: E402
import brain  # noqa: E402

try:
    from PIL import Image, ImageChops
except ImportError:  # 无 Pillow 时相关的用例会自己跳过
    Image = ImageChops = None

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ---------------------------------------------------------------- 工具


class FakeRoot:
    def __init__(self, w=1920, h=1080):
        self.w, self.h = w, h

    def winfo_screenwidth(self):
        return self.w

    def winfo_screenheight(self):
        return self.h

    def after(self, ms, fn=None):
        return "job"

    def after_cancel(self, job):
        pass


def bare_ball(panel_visible=True, menu_open=False, flyout_open=False,
              screen=(1920, 1080)):
    """一个不走 __init__ 的球 —— 只用来算几何，不建窗口。"""
    obj = ball.AiBall.__new__(ball.AiBall)
    obj.cfg = ball.deep_merge(ball.DEFAULTS, {})
    obj.root = FakeRoot(*screen)
    obj.panel_visible = panel_visible
    obj.menu_open = menu_open
    obj.flyout_open = flyout_open
    obj._ball_screen = None
    obj._mm_hover_jobs = {}
    return obj


def temp_path(name):
    return os.path.join(tempfile.gettempdir(), f"aiball_selftest_{name}")


# ---------------------------------------------------------------- 编码


@test
def test_split_utf8_half_character():
    """半个汉字不能被解成 U+FFFD —— 那是永久性的，补全后也读不回来。"""
    raw = "你好".encode("utf-8")           # 6 字节，「好」占 raw[3:6]
    assert len(raw) == 6
    head, tail = ball.split_utf8(raw[:5])  # 把「好」切成两半
    assert head == "你", repr(head)
    assert tail == raw[3:5], repr(tail)
    text, tail2 = ball.split_utf8(tail + raw[5:] + b"\n")
    assert text == "好\n", repr(text)
    assert tail2 == b"", repr(tail2)


@test
def test_split_utf8_bad_bytes_do_not_stall():
    """真坏字节要放过去（replace），不能把整个流卡住。"""
    text, tail = ball.split_utf8(b"ok\xff\xfe\xfd\xfc\x00end")
    assert tail == b"", "坏字节不该被当成'不完整序列'留住"
    assert text.startswith("ok"), repr(text)


@test
def test_tail_reads_bytes_and_keeps_partial_line():
    """brain 的 Tail：按字节读，半行留到下次。"""
    p = temp_path("tail.log")
    if os.path.exists(p):
        os.remove(p)
    open(p, "wb").write("你好".encode("utf-8")[:5])   # 半个「好」
    t = brain.Tail(p, skip_backlog=True)
    assert t.read_new_lines() == [], "没有完整行时不该吐东西"
    with open(p, "ab") as f:
        f.write("你好".encode("utf-8")[5:] + b"\n")
    lines = t.read_new_lines()
    assert lines == ["你好"], repr(lines)
    os.remove(p)


@test
def test_tail_detects_rewrite_even_when_longer():
    """文件被整体改写成**更长**的内容时也要发现，不能只判'变短'。"""
    p = temp_path("rewrite.log")
    open(p, "wb").write(b"ABCDEFGHIJ\n")
    t = brain.Tail(p, skip_backlog=True)
    assert t.read_new_lines() == []
    open(p, "wb").write(b"XXXXXXXXXXYYYYYYY\n")   # 更长，但开头变了
    lines = t.read_new_lines()
    assert lines == ["XXXXXXXXXXYYYYYYY"], repr(lines)
    os.remove(p)


# ---------------------------------------------------------------- 配置健壮性


@test
def test_deep_merge_ignores_non_dict():
    """配置被写成数组/字符串/数字时，不能崩在 .items() 上。"""
    base = {"a": 1, "b": {"c": 2}}
    for bad in ([], "x", 123, None):
        out = ball.deep_merge(base, bad)
        assert out == base, f"{bad!r} -> {out!r}"
    assert ball.deep_merge(base, {"b": {"c": 9}})["b"]["c"] == 9


@test
def test_as_int_as_float_survive_junk():
    """手改配置写个 null 进去，不该让球启动时崩。"""
    assert ball.as_int(None, 112, 36, 240) == 112
    assert ball.as_int("abc", 112) == 112
    assert ball.as_int("200", 112) == 200
    assert ball.as_int(9999, 112, 36, 240) == 240
    assert ball.as_float(None, 0.92, 0.15, 1.0) == 0.92
    assert ball.as_float(5.0, 0.92, 0.15, 1.0) == 1.0


@test
def test_load_json_rejects_non_object():
    """config.json 顶层是数组时，当空的用，别往下传。"""
    p = temp_path("bad.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    assert ball.load_json(p) == {}
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"x": 1}, f)
    assert ball.load_json(p) == {"x": 1}
    os.remove(p)


@test
def test_brain_config_keeps_json_interval():
    """brain.json 里的 interval / replay 不能被 argparse 的默认值盖掉。

    盖掉的后果是 interval=0 空转、每轮整读一遍文件，白烧一个核。
    判据：命令行的 None（没传）不能覆盖 json；传了值才覆盖。
    """
    class NoFlags:
        brain = base_url = model = api_key = replay = None
        interval = None

    class WithInterval:
        brain = base_url = model = api_key = replay = None
        interval = 0.37

    # brain.json 在 .gitignore 里 —— 全新 clone 时它不存在。
    # 不能因此报失败：这个是「有 json 就以 json 为准」，没有就用 DEFAULTS。
    want = brain.DEFAULTS["interval"]
    bj = os.path.join(ROOT, "brain.json")
    if os.path.exists(bj):
        with open(bj, "r", encoding="utf-8") as f:
            want = json.load(f).get("interval", want)


    assert brain.load_config(NoFlags())["interval"] == want, \
        "没传 --interval 时，json 里的值必须活下来"
    assert brain.load_config(WithInterval())["interval"] == 0.37, \
        "传了 --interval 就该覆盖 json"


@test
def test_brain_history_rejects_object():
    """历史文件不是数组时丢掉 —— 否则第一次成功回复就 AttributeError 死掉。"""
    p = temp_path("hist.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"role": "user"}, f)
    assert brain.load_history(p) == []
    with open(p, "w", encoding="utf-8") as f:
        json.dump([{"role": "user", "content": "hi"}, {"bad": 1}], f)
    assert brain.load_history(p) == [{"role": "user", "content": "hi"}]
    os.remove(p)


# ---------------------------------------------------------------- 渲染


@test
def test_orb_body_is_cached():
    """同一颗球一帧要画 4 次，光照只跟颜色有关 —— 必须命中缓存。"""
    a = ball.orb_body_cached(112, "#0891b2", 4)
    b = ball.orb_body_cached(112, "#0891b2", 4)
    assert a is b, "同样的参数应该拿到同一个对象（缓存没生效）"
    t0 = time.time()
    for _ in range(4):
        ball.orb_body_cached(112, "#0891b2", 4)
    assert time.time() - t0 < 0.05, "4 次应该几乎不花时间"


@test
def test_mood_ring_mapping():
    """status.json 的 mood → 球的边缘光颜色。"""
    obj = bare_ball()
    obj._status = {"alive": True, "mood": "nervous"}
    assert obj._mood_ring() == ball.MOOD_RING["nervous"]
    obj._status = {"alive": True, "mood": "idle"}
    assert obj._mood_ring() is None, "idle 应该回落到配置里的默认色"
    obj._status = {"alive": True, "mood": "不认识的"}
    assert obj._mood_ring() is None
    obj._status = {"alive": False, "mood": "nervous"}
    assert obj._mood_ring() == ball.OFFLINE_RING, "心跳过期就该变灰，优先于 mood"


# ---------------------------------------------------------------- 几何


@test
def test_layout_all_four_sides():
    """四个方向：球和槽位不能重叠，而且都得在窗口里。"""
    for side in ball.SIDE_ORDER:
        obj = bare_ball(panel_visible=True)
        obj.cfg["panel"]["side"] = side
        obj._compute_geometry()
        ax0, ay0 = obj.ball_x, obj.ball_y
        ax1, ay1 = ax0 + obj.ball_d, ay0 + obj.ball_d
        bx0, by0 = obj.slot_x, obj.slot_y
        bx1, by1 = bx0 + obj.slot_w, by0 + obj.slot_h
        assert ax0 >= 0 and ay0 >= 0, f"{side}: 球跑到窗口外了"
        assert ax1 <= obj.win_w and ay1 <= obj.win_h, f"{side}: 球超出窗口"
        assert bx1 <= obj.win_w and by1 <= obj.win_h, f"{side}: 槽位超出窗口"
        overlap = ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1
        assert not overlap, f"{side}: 球和槽位重叠了"


@test
def test_pick_side_flips_when_no_room():
    """球贴着屏幕右边时，右边的面板放不下 —— 要自动翻到左边。"""
    ball._HAS_WIN32 = False   # 让 _virtual_screen 走 FakeRoot
    try:
        obj = bare_ball(panel_visible=True, screen=(1920, 1080))
        obj.cfg["panel"]["side"] = "right"

        obj._ball_screen = (200, 400)          # 屏幕中间
        obj._compute_geometry()
        assert obj.side == "right", obj.side

        obj._ball_screen = (1750, 400)         # 贴右边缘
        obj._compute_geometry()
        assert obj.side == "left", "右边放不下应该翻到左边"

        obj._ball_screen = (960, 60)           # 贴上边缘
        obj._compute_geometry()
        assert obj.side == "bottom", obj.side

        # 翻过去之后整窗必须真的放得下
        obj._ball_screen = (1750, 400)
        obj._compute_geometry()
        x = 1750 - obj.ball_x
        y = 400 - obj.ball_y
        assert x >= 0 and x + obj.win_w <= 1920, f"翻了还是放不下: x={x}"
        assert y >= 0 and y + obj.win_h <= 1080, f"翻了还是放不下: y={y}"
    finally:
        ball._HAS_WIN32 = True


@test
def test_clamp_keeps_ball_on_screen():
    """限位要保证**球**在屏幕里，不是只保证窗口原点。

    球是鼠标穿透的 —— 推出屏幕就再也点不到了，而且这个坐标还会被存进
    state.json，重启也回不来。
    """
    ball._HAS_WIN32 = False
    try:
        for side in ball.SIDE_ORDER:
            obj = bare_ball(panel_visible=True, screen=(1920, 1080))
            obj.cfg["panel"]["side"] = side
            obj._compute_geometry()
            for wild in (-5000, -100, 5000, 99999):
                x, y = obj._clamp_origin(wild, wild)
                bx = x + obj.ball_x
                by = y + obj.ball_y
                assert -obj.ball_d + ball.KEEP_VISIBLE <= bx <= 1920, \
                    f"{side} x={wild}: 球横向出屏 bx={bx}"
                assert -obj.ball_d + ball.KEEP_VISIBLE <= by <= 1080, \
                    f"{side} y={wild}: 球纵向出屏 by={by}"
    finally:
        ball._HAS_WIN32 = True


@test
def test_ball_anchor_survives_relayout():
    """开关面板 / 展开二级菜单时，球的屏幕位置不能变。

    锚点必须是球，不是窗口左上角 —— 否则右侧面板一收，球会瞬移上百像素。
    """
    ball._HAS_WIN32 = False
    try:
        obj = bare_ball(panel_visible=True, screen=(1920, 1080))
        obj._ball_screen = (900, 600)
        obj._compute_geometry()
        x, y = obj._anchored_origin((900, 600))
        assert (x + obj.ball_x, y + obj.ball_y) == (900, 600)

        # 面板收起来 -> 窗口变小 -> 球必须还在原地
        obj.panel_visible = False
        obj._compute_geometry()
        x, y = obj._anchored_origin((900, 600))
        after = (x + obj.ball_x, y + obj.ball_y)
        assert after == (900, 600), f"球从 (900,600) 跳到了 {after}"

        # 展开二级菜单也一样
        obj.panel_visible = True
        obj.menu_open = True
        obj.flyout_open = True
        obj._compute_geometry()
        x, y = obj._anchored_origin((900, 600))
        after = (x + obj.ball_x, y + obj.ball_y)
        assert after == (900, 600), f"展开二级菜单后球跳到了 {after}"
    finally:
        ball._HAS_WIN32 = True


@test
def test_mood_visible_without_pil():
    """没装 Pillow 时 mood / 离线也必须看得见 —— 而且要走**真实调用路径**。

    这条是补的：原来只测了 `_apply_ball_frame()`（它确实读 `_mood_ring`，
    是好的），但 `_relayout` 走的是 `_show_ball()`，而 `_show_ball` 自己
    又写了一套「按配置设 outline」的分支 —— **单测过了，实际用不了。**
    所以这里专门从 `_show_ball` 进。
    """
    import tkinter as tk
    root = tk_root()
    saved = ball._HAS_PIL
    ball._HAS_PIL = False
    try:
        obj = bare_ball()
        obj.canvas = tk.Canvas(root, width=100, height=100)
        obj.ball_item = obj.canvas.create_oval(0, 0, 50, 50, outline="#000000")

        obj._status = {"alive": True, "mood": "nervous"}
        obj._show_ball("idle")
        assert obj.canvas.itemcget(obj.ball_item, "outline") == ball.MOOD_RING["nervous"], \
            "无 PIL 时 mood 没生效（_show_ball 把颜色覆盖回配置了？）"

        obj._status = {"alive": False, "mood": "nervous"}
        obj._show_ball("idle")
        assert obj.canvas.itemcget(obj.ball_item, "outline") == ball.OFFLINE_RING, \
            "心跳过期应该变灰"

        obj._status = {"alive": True, "mood": "idle"}
        obj._show_ball("active")
        assert obj.canvas.itemcget(obj.ball_item, "outline") == obj.cfg["ball"]["ring_active"], \
            "没有 mood 时应该回落到说话中的环色"
    finally:
        ball._HAS_PIL = saved
        root.destroy()


@test
def test_menu_sync_survives_null_config():
    """手改 config.json 写个 null，菜单同步也不能崩。

    几何路径早就用 as_int 保护了，漏的是「只是显示」的那几处。
    """
    import tkinter as tk
    root = tk_root()
    try:
        obj = bare_ball()
        for k in ("diameter", "alpha", "image_zoom"):
            obj.cfg["ball"][k] = None
        for k in ("font_size", "width", "height", "gap", "padding", "line_spacing"):
            obj.cfg["panel"][k] = None
        obj.cfg["stream"]["chars_per_tick"] = None

        class FakeCanvas:
            def __init__(self):
                self.texts = {}

            def itemconfigure(self, item, **kw):
                self.texts[item] = kw.get("text")

        obj.menu = FakeCanvas()
        obj.flyout = FakeCanvas()
        obj._mi_vals = {"diameter": "d", "alpha": "a", "zoom": "z"}
        obj._mi_panel_val = "p"
        obj._fi_vals = {k: k for k in ("font_size", "width", "height",
                                       "chars_per_tick", "gap", "padding",
                                       "line_spacing")}
        obj._fi_side = "s"
        obj._fi_gloss = "g"
        obj._fi_theme = "t"
        obj.menu.itemconfigure("p", text="隐藏")
        for name, fn in (("_menu_sync", obj._menu_sync),
                         ("_flyout_sync", obj._flyout_sync)):
            try:
                fn()
            except Exception as exc:
                raise AssertionError(f"{name} 崩了：{type(exc).__name__}: {exc}")
    finally:
        root.destroy()


@test
def test_heartbeat_can_leave_mood_alone():
    """write_mood=false 时心跳只刷 alive，把 mood 让给别人。

    不这么做的话，游戏 mod 设的 `nervous` 会被 brain 每 3 秒踩回 `idle` ——
    而 README / EXTENDING 都在说「任何进程都能写状态通道」。
    """
    p = temp_path("status.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"alive": 1.0, "mood": "nervous"}, f)

    class Cfg(dict):
        pass

    # 让别人的 mood 活着
    hb = brain.Heartbeat.__new__(brain.Heartbeat)
    hb.path = p
    hb.period = 3.0
    hb.write_mood = False
    hb.mood = "idle"
    hb._write()
    with open(p, "r", encoding="utf-8") as f:
        got = json.load(f)
    assert got.get("mood") == "nervous", f"别人的 mood 被踩掉了: {got}"
    assert got.get("alive", 0) > 1.0, "心跳没刷新"

    # 默认行为：连 mood 一起写
    hb.write_mood = True
    hb._write()
    with open(p, "r", encoding="utf-8") as f:
        got = json.load(f)
    assert got.get("mood") == "idle", f"默认应该写自己的 mood: {got}"
    os.remove(p)


@test
def test_mood_renders_even_with_gloss_off():
    """关掉「玻璃高光」不能把 mood 一起关掉。

    图片球上边缘光是 mood **唯一**的落点，而它原来和光斑一起被 `image_gloss` 控制 ——
    于是关掉高光就等于关掉整条 mood 通道，而且毫无提示：`status.json` 里改了 mood，
    球纹丝不动（实测两个 mood 渲染出来的图逐像素相同）。

    **mood 是信息，玻璃高光是装饰。** 关装饰不该把信息也关掉。
    """
    if not ball._HAS_PIL:
        raise ImportError("需要 Pillow")

    # 造一张球身图片：只有 use_image 为真，才会走到出问题的那条渲染路径
    img = temp_path("ballimg.png")
    Image.new("RGB", (64, 64), (120, 90, 60)).save(img)

    obj = bare_ball()
    obj.cfg = ball.deep_merge(
        ball.DEFAULTS, {"ball": {"image": img, "image_gloss": False}}
    )
    obj._img_cache = {}
    obj._status = {}
    base = obj._orb_frame(64, obj.cfg["ball"]["color"], obj.cfg["ball"]["ring"], 0.45)

    obj._status = {"alive": True, "mood": "nervous"}
    ring = obj._mood_ring()
    assert ring, "nervous 应该给出一个 ring"
    mood = obj._orb_frame(64, obj.cfg["ball"]["color"], ring, 0.45)

    diff = ImageChops.difference(base, mood)
    assert diff.getbbox() is not None, \
        "关掉玻璃高光之后 mood 渲染不出来（信息被装饰关掉了）"

    # 反过来也要成立：没有 mood 时，关掉高光就该真的什么都不画
    obj._status = {}
    plain = obj._orb_frame(64, obj.cfg["ball"]["color"], obj.cfg["ball"]["ring"], 0.45)
    assert ImageChops.difference(base, plain).getbbox() is None, \
        "没有 mood 时不该偷偷画边缘光"
    os.remove(img)


@test
def test_panel_command_is_sequenced_not_a_level():
    """`status.json` 的 panel 是**带序号的命令**，不是"当前该是什么状态"。

    如果把它当状态，一个每几秒重写文件的插件就会和用户抢开关：
    你手动收起面板，它下一拍又给你打开。序号让「同一个请求」只执行一次，
    而你没让位的时候，你的手动操作一直算数。
    """
    obj = bare_ball()
    obj._closing = False
    obj._status = {}
    obj._status_sig = ("idle", True)   # 和下面的内容一致 -> 不会触发 _relayout
    obj._panel_seq = None
    obj.panel_visible = False          # 这样第一条「打开面板」才算一个变化
    calls = []

    def fake_set(v):
        # 真的 _set_panel_visible 会更新 panel_visible；替身也必须更新，
        # 否则下一条命令的比较基准就是错的（这个测试第一版就栽在这）
        calls.append(v)
        obj.panel_visible = v

    obj._set_panel_visible = fake_set
    p = temp_path("panel.json")
    obj.status_path = p

    def write(panel, seq):
        data = {"alive": time.time(), "mood": "idle"}
        if panel is not None:
            data["panel"] = panel
        if seq is not None:
            data["panel_seq"] = seq
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)

    write(True, 1)
    obj._poll_status()
    assert calls == [True], f"第一次的命令没执行: {calls}"

    write(True, 1)
    obj._poll_status()
    assert calls == [True], f"同一个序号被重复执行了: {calls}"

    write(False, 2)
    obj._poll_status()
    assert calls == [True, False], f"新序号没执行: {calls}"

    write(True, 2)
    obj._poll_status()
    assert calls == [True, False], "序号没变却又执行了一遍（会跟用户抢开关）"

    # 没有序号的 panel 只是一句愿望，不该被当成命令
    write(True, None)
    obj._poll_status()
    assert calls == [True, False], "没有序号的 panel 被当成命令执行了"

    os.remove(p)


# ---------------------------------------------------------------- 有窗口才测


def tk_root():
    import tkinter as tk
    r = tk.Tk()
    r.geometry("200x200+5000+5000")   # 挪到屏幕外，别干扰用户
    r.withdraw()
    return r


@test
def test_text_is_capped():
    """面板 Text 只增不减会吃光内存 —— 必须砍到 MAX_TEXT_LINES。"""
    import tkinter as tk
    root = tk_root()
    try:
        obj = bare_ball()
        obj.text = tk.Text(root)
        obj.text.insert("end", "\n".join(f"line {i}" for i in range(ball.MAX_TEXT_LINES + 2000)))
        obj._trim_text()
        total = int(obj.text.index("end-1c").split(".")[0])
        assert total <= ball.MAX_TEXT_LINES + 1, f"trim 之后还有 {total} 行"
    finally:
        root.destroy()


@test
def test_menu_hover_deferred_undo():
    """同一行的底框和文字共用一个 tag，Tk 会发 LEAVE+ENTER 一对。

    直接响应 Leave 会把高亮改回去 —— 白重绘；顺序反过来高亮还会丢。
    """
    import tkinter as tk
    root = tk_root()
    try:
        obj = bare_ball()
        obj.M_BG = "#1f1f1f"
        obj.M_HOVER = "#333333"
        c = tk.Canvas(root, width=100, height=100)
        a = c.create_rectangle(0, 0, 50, 50, fill=obj.M_BG)
        b = c.create_rectangle(50, 50, 100, 100, fill=obj.M_BG)
        root.update()

        # 同一行内部滑动：LEAVE 紧跟 ENTER -> 高亮必须留着
        obj._mm_hover_off(c, a)
        obj._mm_hover_on(c, a)
        root.update()
        time.sleep(0.03)
        root.update()
        assert c.itemcget(a, "fill") == obj.M_HOVER, "同一行内部滑动把高亮抹掉了"

        # A 行 -> B 行：A 必须正常熄灭
        obj._mm_hover_off(c, a)
        obj._mm_hover_on(c, b)
        root.update()
        time.sleep(0.03)
        root.update()
        assert c.itemcget(a, "fill") == obj.M_BG, "移到别的行之后 A 没熄灭"
        assert c.itemcget(b, "fill") == obj.M_HOVER

        # 直接移出菜单：只有 LEAVE，也必须熄灭
        obj._mm_hover_off(c, b)
        root.update()
        time.sleep(0.03)
        root.update()
        assert c.itemcget(b, "fill") == obj.M_BG, "移出菜单后没熄灭"
    finally:
        root.destroy()


# ---------------------------------------------------------------- 跑


def main() -> int:
    print(f"ai-ball 回归测试（{len(TESTS)} 项）\n")
    passed = failed = skipped = 0
    for fn in TESTS:
        name = fn.__name__[5:].replace("_", " ")
        try:
            fn()
        except ImportError as exc:
            skipped += 1
            print(f"  [--] {name}（跳过：{exc}）")
        except Exception as exc:
            failed += 1
            print(f"  [x]  {name}")
            print(f"       {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  [ok] {name}")

    print(f"\n通过 {passed}  失败 {failed}  跳过 {skipped}")
    if failed:
        print("**这些是踩过的坑，别把它们弄回去。**")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
