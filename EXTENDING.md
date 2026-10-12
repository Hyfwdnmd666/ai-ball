# 扩展 ai-ball

**给想改它的人看的。** `README.md` 讲「怎么用」，这里讲「怎么接」。

---

## 一句话心智模型

> **球是哑的。它不联网、不思考、不认识游戏。它只认文件。**

```
                    ┌──────────────────────────────────────┐
   你 ──打字──►     │  input.log      （你说的话）          │
                    │  monologue.log  （它说的话）          │
                    │  status.json    （它在什么状态）      │
                    │  context.md     （它该知道什么）      │
                    └──────────────────────────────────────┘
                                    ▲
                                    │ 只有读写文件
                    ┌───────────────┴───────────────┐
                    │            ball.py            │  ← 哑的
                    └───────────────────────────────┘

   谁去读 input.log、谁去写 monologue.log —— 球不关心。
   brain.py 只是一个默认实现。你可以整个换掉，球一行都不用改。
```

**这个设计的全部意义就是：接口是文件，不是 API。** 任何语言、任何进程都能接。

---

## 接口总表

| 文件 | 谁写 | 谁读 | 作用 |
|---|---|---|---|
| `monologue.log` | 任何东西 | **球** | 追加。球按**字节偏移**读增量，再逐字吐出来（打字机效果） |
| `input.log` | **球**（输入框）/ 任何东西 | 任何东西 | 追加。一行一条消息 |
| `status.json` | 任何东西 | **球** | **覆盖**写（追加会解析失败）。mood + 心跳 |
| `context.md` | 任何东西 | `brain.py` | 覆盖写。会拼进系统提示 |
| `themes.json` | 你 | **球** | 自定义主题（可选） |
| `config.json` | 控制面板 / 你 | **球** | 样式与参数。运行期状态，不进仓库 |
| `state.json` | **球** | **球** | 只存窗口位置和面板开合 |
| `persona.md` | 你 | `brain.py` | 人格设定 |

**「任何东西」= 你、游戏 mod、另一个脚本、手写。** 球不知道区别。

> ⚠️ **改文件名要改两处。** 球从 `config.json` 读 `stream.file` /
> `chat.input_file` / `status.file`，`brain.py` 从 `brain.json` 读
> `output_file` / `input_file` / `status_file`。只改一边的话，比如
> `status.json` 改了名 —— brain 还写旧名，**球会永远显示"脑子没了"**。
>
> 不改名就没这问题（默认值两边是对齐的）。

---

## 1. `monologue.log` —— 它说的话

**追加，UTF-8。** 球按**字节偏移**读增量，只消费到最后一个完整的 UTF-8 序列，
然后**逐字**吐出来（默认 2 字 / 40ms）。

> **为什么是字节而不是字符**：写入方分两次 flush 时，尾部可能只剩半个汉字。
> 按字符偏移 + `errors="replace"` 会把它解成 U+FFFD，而那是**永久**的 ——
> 等它补全，字符总数没变，那段内容再也读不出来。
>
> 球还会记住文件**开头 64 字节的指纹**来识破「整体重写」：只看文件变短是不够的，
> 被改写成更长的内容时，前面那段会被当成"已经读过"直接跳过。

```bash
echo "三实两空。别问了。" >> monologue.log
```

- **可以带换行**，球会照样处理
- **文件变短** = 球认为你重写了它 → 自动清屏并从头读。所以 `> monologue.log` 清空是安全的
- 球**不解析**内容，原样显示

> 想让它"说话慢一点/快一点"：控制面板里有「语速」，或改 `config.json` 的
> `stream.chars_per_tick` / `stream.reveal_ms`。

## 2. `input.log` —— 你说的话

**追加，一行一条。**

```bash
echo "你在干嘛" >> input.log
```

- 球只处理**完整行**（结尾有 `\n`）。半行会留在缓冲里等下一个换行 ——
  所以你**边写边追加不会把一条消息切成两半**
- 球的输入框、`feed.py --as you`、`brain.py` 都走这个文件
- 球读到新行会**立刻**插进面板，用强调色（`you_fg`），**不走打字机** ——
  打字机是「它在说话」才用的效果

## 3. `status.json` —— 它在什么状态

**覆盖写**（不是追加）。**任何进程**都能写：

```json
{"alive": 1791746122, "mood": "nervous", "panel": true, "panel_seq": 3}
```

| 字段 | 类型 | 作用 |
|---|---|---|
| `mood` | 字符串 | 球的边缘光换色，见下表 |
| `alive` | 数字 | unix 时间戳。**超过 `stale_s`（默认 20 秒）没更新 → 球变灰** |
| `panel` | 布尔 | 想让它显示 / 收起独白面板 |
| `panel_seq` | 数字 | `panel` 的**命令序号**，见下 |

### `panel` 是命令，不是状态

球只在 **`panel_seq` 变化**时动一次面板。没有 `panel_seq` 的 `panel` 一律不执行。

为什么不把它当状态：一个每几秒重写这个文件的进程（比如心跳）会让 `panel: true`
一直挂在那儿，于是**你手动收起面板，它下一拍又给你打开** —— 两个写者抢一个开关。

带序号之后：

- 你没改变主意 → 序号不变 → 用户的手动开关**一直算数**
- 你改变主意 → 序号加一 → 球再执行一次
- **每次调用都应该加一**，哪怕你想要的值没变 —— 否则"他关掉了，我要它再显示"
  就成了空操作

```python
# 一个最小实现：想要什么 + 一个只增的序号
seq += 1
write_status({"alive": time.time(), "mood": mood, "panel": want, "panel_seq": seq})
```

| mood | 颜色 | 建议用在哪 |
|---|---|---|
| `idle` | 用配置默认色 | 闲着 |
| `thinking` | 紫 | 正在调模型/思考 |
| `nervous` | 红 | 紧张、血量低 |
| `smug` | 金 | 得瑟 |
| `broken` | 灰 | 出错 |
| *(未知值)* | 用配置默认色 | 安全兜底 |

**心跳是这里最有用的部分。** 写 `alive` 之后，如果你的进程被 `kill -9` 掉，
**球会自己发现并变灰** —— 不需要谁去通知它，也不会有"看起来在跑其实死了"的状态。

`brain.py` 就是这么做的，每几秒写一次。**接游戏也一样**：游戏进程活着就写心跳。

> ⚠️ **只有一个写者。**
>
> 如果 `brain.py` 在跑，**它是每 3 秒重写整个文件的那个**（心跳是它的）。
> 你在它旁边写 `mood`，3 秒内就会被踩回 `idle`。
>
> 想自己控制球的脸色，二选一：
> - `brain.json` 里 **`"write_mood": false`** —— 它只刷 `alive`，
>   文件里现有的 `mood` 原样保留（推荐，改一个键就行）
> - 或者干脆不跑 `brain.py`，自己当那个唯一写者

## 4. `context.md` —— 它该知道什么

**覆盖写，纯文本。** `brain.py` 每次请求前读一遍，拼到系统提示的最后：

```
当前局势：
血量 3/4，对面 2/4
弹仓：三实两空
对面手上还有手铐
```

**这就是留给游戏接口的那道缝。** 你的游戏 mod / 读屏脚本只要往这个文件写状态，
`brain.py` 和球**都不用改**。

文件不存在或为空 → 不拼。所以你可以随时删掉它。

---

## 扩展场景 A：加一套主题

复制 `themes.example.json` 成 `themes.json`，加一个键：

```json
{
  "mytheme": {
    "label": "我的",
    "menu_bg": "#1a1a2e",
    "menu_border": "#2d2d4a",
    "menu_line": "#2d2d4a",
    "menu_text": "#eaeaea",
    "menu_sub": "#8a8aa0",
    "menu_hover": "#26264a",
    "menu_danger": "#ff99a4",
    "panel_bg": "#1a1a2e",
    "panel_fg": "#eaeaea",
    "panel_border": "#2d2d4a",
    "you_fg": "#7dd3fc"
  }
}
```

重启球 → 右键 → 更多设置 → **「主题」** 点一下就能切到它。

- 下划线开头的键会被忽略（拿来写注释）
- 跟内置同名的会**覆盖**内置
- **不用改任何代码**

## 扩展场景 B：换大脑

`brain.py` 只是默认实现。**契约只有一条**：

> 轮询 `input.log` 的新行 → 想 → 把回复追加到 `monologue.log`。

写一个 30 行的脚本就能换掉它：

```python
import time, pathlib

IN, OUT = pathlib.Path("input.log"), pathlib.Path("monologue.log")
# 注意：偏移按**字节**记。用字符数配 st_size 会让中文全错，
# 而且「文件变短=重写」这种判断也不够 —— 见上面 monologue.log 那一节。
off = IN.stat().st_size if IN.exists() else 0

while True:
    if IN.exists():
        size = IN.stat().st_size
        if size < off:              # 被清空 / 重写了
            off = 0
        if size > off:
            with IN.open("rb") as f:
                f.seek(off)
                raw = f.read()
            off = size
            # 只处理完整行：尾部半行留到下次，否则会把一条消息切碎
            if raw.endswith(b"\n"):
                for line in raw.decode("utf-8", "replace").splitlines():
                    if line.strip():
                        with OUT.open("a", encoding="utf-8") as f:
                            f.write(f"我听见了：「{line}」\n\n")
            else:
                off -= len(raw)     # 退回去，等它补全
    time.sleep(0.4)
```

**球那一侧一个字都不用动。**

想接本地模型、接别的 API、接规则引擎、接一个真人 —— 都是这一件事。

## 扩展场景 C：接游戏

三条路，从易到难：

| 路线 | 做法 | 需要什么 |
|---|---|---|
| **读存档 / 内存** | 直接拿真实数据，写进 `context.md` | 逆向（每个游戏不同） |
| **读屏幕** | 截图 → 视觉模型 → 写 `context.md` | 你有个能看图的模型 |
| **写状态** | 游戏 mod 直接写 `status.json` | 游戏支持 mod |

**推荐顺序：先写 `context.md`（让大脑知道局势），再考虑写 `status.json`（让球变色）。**

两条都做完，就只剩"谁来操作游戏"了 —— 那是另一个问题，跟球无关。

---

## 开发约定：踩过的坑

**这些不是风格偏好，是实测出来的。改之前先读。**

### 1. `WS_EX_NOACTIVATE` 必须**在建窗后立刻**补上，并把前台还回去

```python
prev = user32.GetForegroundWindow()   # 建窗之前先记
root = tk.Tk()
root.withdraw()                        # 先藏着
... 设 WS_EX_NOACTIVATE ...
root.deiconify()
user32.SetForegroundWindow(prev)       # 还回去
```

**不这么做的话，"不抢焦点"在启动那一刻就是破的。** `tk.Tk()` 建窗那一下
窗口就已经变成前台了 —— 光看代码完全看不出来（标志明明设上了）。
这个 bug 是靠实测抓出来的，见 `README.md` 里的 `tools/foctest.py`。

### 2. 色键必须选**近黑**，不能选品红/亮绿

色键是**精确匹配**。抗锯齿的边缘像素是「球色 × 键色」的混合，匹配不上、抠不掉，
就会在球周围留一圈键色的毛边 —— 品红毛边在浅色背景上非常刺眼。

球的边缘本来就是深色描边，所以键色选近黑（`#010203`），那圈过渡自然融进描边里。

### 3. 不声明 DPI 感知 = 整个界面发虚

```python
ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 建窗之前
```

不调用的话，在 125% / 150% 缩放的屏幕上，**Windows 会把整个窗口当位图拉伸**。
字全是虚的。这一条曾经被误以为"配色问题"。

### 4. 别用 `tk.Menu` 做右键菜单

原生弹出菜单会短暂成为前台窗口 → 游戏失焦暂停 → 整套设计的目的就没了。

自己画在 Canvas 上。**四角落在 MAGIC 色上会被色键抠掉，所以圆角是白得的。**

### 5. 中文 Windows 控制台是 GBK

打印 `✗ ★ → …` 这类字符会抛 `UnicodeEncodeError`，**把整个脚本干掉**。
而且只有中文用户会撞上。

现在的做法：装饰符号全用 ASCII + `sys.stdout.reconfigure(errors="replace")` 兜底。
`start.bat` 里加了 `chcp 65001`。

### 6. 零第三方依赖，只有 PIL 是可选的

`PIL` 有就用它把球画平滑（4 倍超采样再缩回来），没有就退化成原生椭圆（有锯齿）。
**核心功能不受影响。** 别引入新的硬依赖 —— 这是这个项目能被随便跑起来的原因。

---

## 文件清单

```
ball.py                悬浮球本体。哑的，只认文件
brain.py               默认大脑：轮询 input.log → 调模型 → 写 monologue.log
persona.md             人格设定（brain.py 读）
feed.py                手动喂字，也是管道的手动替身
start.bat              一键：找 Python → 自检脑子 → 拉起球和回路
orb_preview.py         把球渲染成对照图，不开窗口就能调外观
requirements.txt       本体零依赖；Pillow 可选
LICENSE                MIT
.gitignore             把 key / 运行期状态 / 你的球身图片挡在仓库外

config.json            样式与参数（控制面板会写）—— 运行期状态，不进仓库
state.json             窗口位置 / 面板开合（球自己写）
themes.json            自定义主题（从 themes.example.json 复制）
themes.example.json    主题样例，含两套可直接用的配色
brain.json             key 和模型配置（从 brain.example.json 复制，已被 .gitignore）
brain.example.json     带注释的配置模板
context.md             游戏局势，给大脑看的（可空）
status.json            状态通道（心跳 + mood）
input.log              你说的话
monologue.log          它说的话
demo_monologue.txt     feed.py --demo 用的样本独白
assets/                你的球身图片（.gitignore 挡着）

tools/selftest.py      回归测试：19 项，每项对应一个修过的 bug，不需要硬件
tools/foctest.py       「点球不抢焦点」的自动测试（需要硬件 HID，或手动点）
tools/jumpcheck.py     「开关面板时球会不会跳」的自动测试（需要硬件 HID）
```

## 改完先跑这个

```bash
python tools/selftest.py
```

**每一条都对应一个真实修过的 bug**（半个汉字被吞、配置写成 null 就崩、
历史文件不是数组就把回路干掉、球开关面板时瞬移、无 PIL 时 mood 不生效……）。

它不需要显示器、不需要串口、不联网、不花钱，一秒跑完。
改完代码先跑它 —— 上面那些坑，光靠读代码是看不出来的（其中好几个
我自己写的时候还很确信是对的）。

> 有一个坑值得单独说：**测了但测的不是真实路径 = 没测。**
> 「无 PIL 时 mood 变色」那一项，我第一版只测了 `_apply_ball_frame()`（它是对的），
> 但实际流程走的是 `_show_ball()`，而它自己又按配置把颜色设了回去 ——
> 单测全绿，功能全废。所以那个用例现在**专门从 `_show_ball` 进**。
