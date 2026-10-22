# -*- coding: utf-8 -*-
"""voice.py — 让球真的出声。

球本身只认文件：它把 `monologue.log` 里的文字逐字打出来。这个进程做同一件事的**听觉版本**
——盯同一个文件，把新说的话念出来。

它和 `brain.py` 是同一个模式：**一个独立的进程，只认文件，不管是谁写的。**
所以球不需要改一行代码，你不想听就把它关掉。

用法：

    python voice.py                 # 跟着 monologue.log 念
    python voice.py --test          # 念一句话然后退出（先验这个）
    python voice.py --list          # 列出这台机器上有哪些声音
    python voice.py --say "文本"    # 念指定的文本

设计上的两个要点：

* **新的一句会打断旧的。** 真人不会把上一句说完才开始下一句——尤其在被吐槽的时候。
  所以每次说话前先杀掉上一个发声进程。不这么做的话，agent 写得快时会越拖越远，
  最后你在听三十秒前的话。

* **发声交给 PowerShell，不引 Python 包。** Windows 自带的 SAPI 是零依赖的；
  `pyttsx3` 之类要装包、要编译，而这个项目的立身之本是"解开就能跑"。
  代价是每句话启动一个 PowerShell（约 200~400 毫秒），换来的是**能被随时打断**——
  这两件事正好互相成全。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

try:
    import urllib.error
    import urllib.request
except Exception:  # pragma: no cover
    urllib = None

try:
    import winsound
except Exception:  # pragma: no cover - 非 Windows
    winsound = None

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    # 用哪个后端：sapi（Windows 自带，离线）/ minimax（在线，音质好）
    "backend": "minimax",
    "voice": "Microsoft Huihui Desktop",  # 仅 backend=sapi 时用
    "rate": 0,        # 仅 sapi：-10 ~ 10
    "volume": 100,    # 仅 sapi：0 ~ 100
    "poll_ms": 200,   # 盯 monologue.log 的频率
    "min_chars": 1,   # 短于这个字数的行不念
    "max_chars": 220, # 太长的段落截断，避免一口气念半分钟
    "minimax": {
        "endpoint": "https://api.minimaxi.com",  # 国内版；国际版是 api.minimax.io
        "model": "speech-2.8-hd",              # 2.8 系列才有语气词标签
        "voice_id": "Chinese (Mandarin)_Lyrical_Voice",
        "emotion": "",          # happy/sad/angry/fearful/disgusted/surprised/calm，留空=模型自选
        "speed": 1.0,           # 0.5 ~ 2
        "pitch": 0,             # -12 ~ 12
        "vol": 1.0,             # 0 ~ 10
        "sample_rate": 32000,
        "key_file": "_minimax-key.txt",
        "language_boost": "Chinese",
    },
}
CONFIG_PATH = os.path.join(HERE, "voice.json")
MONOLOGUE = os.path.join(HERE, "monologue.log")
LOG_PATH = os.path.join(HERE, "voice.log")

# Windows 上控制台是 GBK，中文直接 print 会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def log(msg: str) -> None:
    """写日志，顺便打到控制台。日志永远不会因为写不进去而崩。"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update({k: v for k, v in loaded.items() if k in DEFAULTS})
        except Exception as e:
            log(f"voice.json 读不了，用默认值：{e}")
    return cfg


def clean_for_speech(text: str) -> str:
    """把写给眼睛看的东西整理成给耳朵听的。

    球上的文字带排版记号：Markdown 的星号、代码反引号、括号里的动作描写。
    念出来会变成"星号星号"和"反引号"，很难听。

    **只去掉全角括号（……）里的动作描写，保留半角 (……)** ——
    因为 MiniMax 的语气词标签恰好是半角括号写的：`(sighs)` `(laughs)` `(snorts)`。
    把它们当动作描写删掉，情绪就没了。中文里动作描写习惯用全角，正好区分开。
    """
    s = text.strip()
    s = re.sub(r"```.*?```", " ", s, flags=re.S)   # 代码块整个不要
    s = re.sub(r"[*_`#>]+", "", s)                 # Markdown 记号
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s) # 链接只留文字
    s = re.sub(r"（[^）]*）", " ", s)                # 全角括号里的动作描写不念
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def ps_quote(s: str) -> str:
    """PowerShell 单引号字符串里的转义：只要把单引号翻倍。"""
    return s.replace("'", "''")


def list_voices() -> list[str]:
    """问 Windows 有哪些声音可用。"""
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }; "
        "$s.Dispose()"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=20,
        )
        text = out.stdout.decode("mbcs", errors="replace")
        return [ln.strip() for ln in text.splitlines() if ln.strip()]
    except Exception as e:
        log(f"列声音失败：{e}")
        return []


def build_script(text: str, cfg: dict) -> str:
    """拼出念这句话的 PowerShell 脚本。

    文本从**文件**读，不拼进命令行 —— 中文、引号、换行塞进 -Command 里迟早出事。
    """
    voice = ps_quote(str(cfg.get("voice") or ""))
    rate = int(cfg.get("rate") or 0)
    volume = int(cfg.get("volume") or 100)
    return (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Volume = {}; ".format(volume)
        + "$s.Rate = {}; ".format(rate)
        + (f"try {{ $s.SelectVoice('{voice}') }} catch {{}}; "
           if voice else "")
        + f"$t = Get-Content -Raw -Encoding UTF8 '{{TEXTFILE}}'; "
        + "$s.Speak($t); $s.Dispose()"
    )


class Speaker:
    """一次念一句，新的打断旧的。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.text_file = os.path.join(HERE, f".speak-{os.getpid()}.txt")

    def stop(self) -> None:
        """掐掉正在念的那句。"""
        p = self.proc
        self.proc = None
        if p is None:
            return
        try:
            p.kill()
        except Exception:
            pass

    def say(self, text: str) -> bool:
        spoken = clean_for_speech(text)
        if len(spoken) < int(self.cfg.get("min_chars") or 1):
            return False
        limit = int(self.cfg.get("max_chars") or 220)
        if len(spoken) > limit:
            spoken = spoken[:limit] + "。"

        self.stop()  # 新的这句打断上一句

        try:
            with open(self.text_file, "w", encoding="utf-8") as f:
                f.write(spoken)
        except Exception as e:
            log(f"写临时文本失败：{e}")
            return False

        script = build_script(spoken, self.cfg).replace("{TEXTFILE}", self.text_file.replace("'", "''"))
        try:
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except Exception as e:
            log(f"启动发声失败：{e}")
            return False

    def cleanup(self) -> None:
        self.stop()
        try:
            if os.path.exists(self.text_file):
                os.remove(self.text_file)
        except Exception:
            pass


class MiniMaxVoice:
    """MiniMax 在线语音：合成 WAV，在内存里直接播。

    为什么比本地 SAPI 好：

    * 这台机器的本地语音引擎是坏的（中文引擎数据缺失），在线的不受影响。
    * `t2a_v2` 的 `audio_setting.format` 支持 `wav` —— 所以**播放不需要任何解码器**：
      `winsound.PlaySound(bytes, SND_MEMORY)` 直接吃内存里的 WAV，零依赖。
    * `SND_ASYNC` 立刻返回，`PlaySound(None)` 随时掐断 —— 打断是白送的。
    * `speech-2.8` 系列支持语气词标签 `(sighs)` `(laughs)` `(snorts)`，
      是真的情绪音效，不是念稿。

    延迟是真的：一次合成 1~2 秒，所以字会先出现在球上、声音后到。
    这也正是新的句子必须掐掉旧句子的原因 —— 不然排队会越拖越远。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mm = cfg.get("minimax") or {}
        self.key = self._load_key()
        # 异步播放必须走文件：winsound 不允许 SND_MEMORY + SND_ASYNC 同时用，
        # 会抛 "Cannot play asynchronously from memory"。所以合成结果先落盘再播。
        self.wav_path = os.path.join(HERE, f".speak-{os.getpid()}.wav")

    def _load_key(self) -> str:
        env = (os.environ.get("MINIMAX_API_KEY") or "").strip()
        if env:
            return env
        name = self.mm.get("key_file") or "_minimax-key.txt"
        path = name if os.path.isabs(name) else os.path.join(HERE, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                # 跳过 # 开头的说明行，取第一行真正的内容 —— 这样模板文件里可以写注释
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except Exception:
            pass
        return ""

    def available(self) -> bool:
        return bool(self.key) and urllib is not None and winsound is not None

    def describe(self) -> str:
        if not self.key:
            return "缺 API key（_minimax-key.txt 里没东西）"
        if urllib is None:
            return "urllib 不可用"
        if winsound is None:
            return "不是 Windows，没有 winsound"
        return f"就绪 model={self.mm.get('model')} voice={self.mm.get('voice_id')}"

    def _post(self, path: str, body: dict) -> dict | None:
        """往 MiniMax 发一个 JSON POST，返回解析后的 payload。"""
        if urllib is None:
            log("没有 urllib，无法联网")
            return None
        endpoint = str(self.mm.get("endpoint") or "https://api.minimaxi.com").rstrip("/")
        req = urllib.request.Request(
            endpoint + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            log(f"MiniMax HTTP {e.code}：{detail}")
        except Exception as e:
            log(f"MiniMax 请求失败：{e}")
        return None

    def list_system_voices(self) -> list[dict]:
        """列出这个账号能用的所有系统音色（含描述）。"""
        payload = self._post("/v1/get_voice", {"voice_type": "system"})
        if not payload:
            return []
        base = payload.get("base_resp") or {}
        if base.get("status_code") not in (0, None):
            log(f"列音色失败 {base.get('status_code')}：{base.get('status_msg')}")
            return []
        voices = payload.get("system_voice")
        return voices if isinstance(voices, list) else []

    def _synth(self, text: str) -> bytes | None:
        endpoint = str(self.mm.get("endpoint") or "https://api.minimaxi.com").rstrip("/")
        voice_setting = {
            "voice_id": self.mm.get("voice_id") or "Chinese (Mandarin)_Lyrical_Voice",
            "speed": float(self.mm.get("speed") or 1.0),
            "vol": float(self.mm.get("vol") or 1.0),
            "pitch": int(self.mm.get("pitch") or 0),
        }
        if self.mm.get("emotion"):
            voice_setting["emotion"] = self.mm["emotion"]

        body = {
            "model": self.mm.get("model") or "speech-2.8-hd",
            "text": text,
            "stream": False,
            "output_format": "hex",
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": int(self.mm.get("sample_rate") or 32000),
                "format": "wav",
                "channel": 1,
            },
        }
        if self.mm.get("language_boost"):
            body["language_boost"] = self.mm["language_boost"]

        payload = self._post("/v1/t2a_v2", body)
        if not payload:
            return None

        # 文档明确说 data 可能为 null，必须判空
        data = payload.get("data")
        if not isinstance(data, dict):
            base = payload.get("base_resp") or {}
            log(f"MiniMax 没有返回音频：{base.get('status_code')} {base.get('status_msg')}")
            return None
        base = payload.get("base_resp") or {}
        if base.get("status_code") not in (0, None):
            log(f"MiniMax 报错 {base.get('status_code')}：{base.get('status_msg')}")
            return None
        hexed = data.get("audio")
        if not hexed:
            log("MiniMax 返回的 audio 是空的")
            return None
        try:
            return bytes.fromhex(hexed)
        except ValueError:
            log("MiniMax 返回的不是合法十六进制")
            return None

    def stop(self) -> None:
        if winsound is None:
            return
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def say(self, text: str, blocking: bool = False) -> bool:
        spoken = clean_for_speech(text)
        if len(spoken) < int(self.cfg.get("min_chars") or 1):
            return False
        limit = int(self.cfg.get("max_chars") or 220)
        if len(spoken) > limit:
            spoken = spoken[:limit] + "。"

        self.stop()  # 新的这句打断上一句
        log(f"合成中…「{spoken[:24]}{'…' if len(spoken) > 24 else ''}」")
        wav = self._synth(spoken)
        if not wav:
            return False

        try:
            if blocking:
                # 阻塞路径可以直接吃内存（--test / --say 用这条）
                winsound.PlaySound(wav, winsound.SND_MEMORY)
                return True
            # 异步必须走文件，见 __init__ 里的说明
            with open(self.wav_path, "wb") as f:
                f.write(wav)
            winsound.PlaySound(
                self.wav_path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
            return True
        except Exception as e:
            log(f"播放失败：{e}")
            return False

    def cleanup(self) -> None:
        self.stop()
        try:
            if os.path.exists(self.wav_path):
                os.remove(self.wav_path)
        except Exception:
            pass


class Tail:
    """按字节跟文件，只吐完整行。

    和 brain.py 的 Tail 同一个纪律：游标只推进到最后一个换行。
    写方可能被撞见写到一半，把残缺的尾巴当内容解码会让半个 UTF-8 序列永久变成 U+FFFD。
    """

    def __init__(self, path: str):
        self.path = path
        self.offset = 0
        self.head = None
        self.first = True

    def read(self) -> list[str]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except Exception:
            return []

        head = data[:64]
        if self.head is not None and head[: len(self.head)] != self.head:
            self.offset = 0  # 文件被重写过
        self.head = head
        if len(data) < self.offset:
            self.offset = 0
        if len(data) == self.offset:
            return []

        chunk = data[self.offset:]
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return []

        complete = chunk[:cut]
        try:
            text = complete.decode("utf-8")
        except UnicodeDecodeError:
            text = complete.decode("utf-8", errors="replace")
        self.offset += cut + 1

        lines = [ln.strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]

        if self.first:
            # 启动时已有的内容一次性跳过：那是历史，不是刚说的话
            self.first = False
            return []
        return lines


def pick_speaker(cfg: dict):
    """按配置选后端；minimax 不可用时退回 sapi。"""
    backend = str(cfg.get("backend") or "minimax").lower()
    if backend == "minimax":
        mm = MiniMaxVoice(cfg)
        if mm.available():
            return mm, "minimax"
        log(f"MiniMax 用不了（{mm.describe()}），退回 Windows 本地语音")
    return Speaker(cfg), "sapi"


def save_voice_id(voice_id: str) -> None:
    """把选中的音色写回 voice.json，保留其它配置不动。"""
    data = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass
    mm = data.get("minimax")
    if not isinstance(mm, dict):
        mm = {}
        data["minimax"] = mm
    mm["voice_id"] = voice_id
    data.setdefault("backend", "minimax")
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log(f"已保存到 voice.json：{voice_id}")
    except Exception as e:
        log(f"写 voice.json 失败：{e}")


def interactive_pick(cfg: dict, mm: "MiniMaxVoice") -> int:
    """列音色 → 编号试听 → 选一个存下来。"""
    voices = mm.list_system_voices()
    if not voices:
        log("一个音色都没拿到")
        return 1

    # 中文优先，其余排后面 —— 这个球的台词是中文的
    def is_zh(v: dict) -> bool:
        blob = json.dumps(v, ensure_ascii=False)
        return ("Mandarin" in blob) or ("Chinese" in blob) or ("中文" in blob)

    rows = [v for v in voices if is_zh(v)] + [v for v in voices if not is_zh(v)]

    print(f"共 {len(rows)} 个系统音色。带描述的是官方说明，可以照着挑。")
    print("")
    for i, v in enumerate(rows, 1):
        vid = v.get("voice_id", "?")
        name = v.get("voice_name", "")
        desc = v.get("description") or []
        d = " ".join(desc) if isinstance(desc, list) else str(desc)
        mark = "  ← 当前" if vid == mm.mm.get("voice_id") else ""
        print(f"  [{i:>3}] {vid}{mark}")
        if name or d:
            print(f"         {name}  {d}")
    print("")
    print("  （建议先挑 3~5 个描述对得上「欠揍少女」「活泼」的来试听，")
    print("    每试听一句都会真的合成、按字符计费，别一次全点。）")
    print("")

    try:
        raw = input("试听哪几个？输入编号，空格分隔（直接回车退出）：").strip()
    except (EOFError, KeyboardInterrupt):
        print("")
        return 0
    if not raw:
        return 0

    picks = []
    for token in raw.replace(",", " ").split():
        if token.isdigit() and 1 <= int(token) <= len(rows):
            picks.append(rows[int(token) - 1])
    if not picks:
        log("没解析出编号")
        return 1

    # 一句能同时听出人设和语气词的话
    sample = "哼，(sighs)又输了。这局不算，我手滑。"
    saved = mm.mm.get("voice_id")
    print("")
    print(f"试听文本：{sample}")
    for v in picks:
        vid = v.get("voice_id", "")
        mm.mm["voice_id"] = vid
        print("")
        print(f"  ▶ {vid}   {v.get('voice_name', '')}")
        if mm.say(sample, blocking=True):
            print("    ✓ 播完")
        else:
            print("    ✗ 失败（看上面的报错）")
    mm.mm["voice_id"] = saved

    print("")
    try:
        choice = input("留哪个？输入编号（直接回车 = 不改）：").strip()
    except (EOFError, KeyboardInterrupt):
        print("")
        return 0
    if choice.isdigit() and 1 <= int(choice) <= len(rows):
        picked = rows[int(choice) - 1].get("voice_id", "")
        if picked:
            save_voice_id(picked)
            print("")
            print(f"  下次启动 voice.py 就会用这个声音：{picked}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="让球出声")
    ap.add_argument("--test", action="store_true", help="念一句测试然后退出")
    ap.add_argument("--list", action="store_true", help="列出可用声音")
    ap.add_argument("--say", metavar="文本", help="念指定的文本")
    ap.add_argument("--voices", nargs="?", const="", metavar="关键词",
                    help="列出 MiniMax 的所有系统音色（可按关键词过滤，如 Mandarin）")
    ap.add_argument("--audition", nargs="+", metavar="voice_id",
                    help="试听指定音色（会真的合成，按字符计费）")
    ap.add_argument("--pick", action="store_true",
                    help="交互式挑声音：列出来 → 选编号试听 → 存下来")
    args = ap.parse_args()

    cfg = load_config()
    backend = str(cfg.get("backend") or "minimax").lower()

    # ---- 交互式挑声音 ----
    if args.pick:
        mm = MiniMaxVoice(cfg)
        if not mm.available():
            log(f"MiniMax 用不了（{mm.describe()}）")
            return 1
        return interactive_pick(cfg, mm)

    # ---- 挑音色：列出来 ----
    if args.voices is not None:
        mm = MiniMaxVoice(cfg)
        if not mm.available():
            log(f"MiniMax 用不了（{mm.describe()}）")
            return 1
        voices = mm.list_system_voices()
        if not voices:
            log("一个音色都没拿到")
            return 1
        kw = (args.voices or "").lower()
        rows = [v for v in voices
                if not kw or kw in json.dumps(v, ensure_ascii=False).lower()]
        print(f"共 {len(voices)} 个系统音色，匹配「{kw or '全部'}」的有 {len(rows)} 个：")
        print("")
        for v in rows:
            vid = v.get("voice_id", "?")
            name = v.get("voice_name", "")
            desc = v.get("description") or []
            d = " ".join(desc) if isinstance(desc, list) else str(desc)
            mark = "   ← 当前在用" if vid == mm.mm.get("voice_id") else ""
            print(f"  {vid}{mark}")
            if name or d:
                print(f"      {name}  {d}")
        print("")
        print("试听（一次可以听好几个）：")
        print('  python voice.py --audition "Chinese (Mandarin)_Lyrical_Voice" ...')
        return 0

    # ---- 挑音色：试听 ----
    if args.audition:
        mm = MiniMaxVoice(cfg)
        if not mm.available():
            log(f"MiniMax 用不了（{mm.describe()}）")
            return 1
        # 选一句能同时听出人设和语气词的
        sample = "哼，(sighs)又输了。这局不算，我手滑。"
        saved = mm.mm.get("voice_id")
        print(f"试听文本：{sample}")
        for vid in args.audition:
            mm.mm["voice_id"] = vid
            print("")
            print(f"  ▶ {vid}")
            if mm.say(sample, blocking=True):
                print("    ✓ 播完")
            else:
                print("    ✗ 失败（看上面的报错）")
        mm.mm["voice_id"] = saved
        print("")
        print("选中哪个，就把它填进 voice.json 的 minimax.voice_id。")
        return 0

    if args.list:
        if backend == "minimax":
            mm = MiniMaxVoice(cfg)
            print("后端：MiniMax（在线）")
            print(f"  状态    : {mm.describe()}")
            print(f"  音色    : {mm.mm.get('voice_id')}")
            print(f"  模型    : {mm.mm.get('model')}   （2.8 系列才支持 (sighs)/(laughs) 这类语气词）")
            print(f"  端点    : {mm.mm.get('endpoint')}")
            print(f"  key 文件: {mm.mm.get('key_file')}")
            print("\n改 voice.json 里的 minimax.voice_id 换音色；")
            print("音色清单见 https://platform.minimax.io/faq/system-voice-id")
            return 0 if mm.available() else 1
        voices = list_voices()
        if not voices:
            log("一个声音都没有 —— 这台机器的语音组件可能没装全。")
            return 1
        print("这台机器可用的声音：")
        for v in voices:
            mark = "  <- 当前" if v == cfg["voice"] else ""
            print(f"  {v}{mark}")
        print('\n改 voice.json 里的 "voice" 换成别的。')
        return 0

    speaker, which = pick_speaker(cfg)

    if args.say or args.test:
        text = args.say or "测试一下。这是球的嗓子，听得见吗？"
        # --test 是给人验声音的，必须等它念完再退出
        if isinstance(speaker, MiniMaxVoice):
            ok = speaker.say(text, blocking=True)
            speaker.cleanup()
        else:
            ok = speaker.say(text)
            if speaker.proc is not None:
                try:
                    _, err = speaker.proc.communicate(timeout=60)
                    if err:
                        msg = err.decode("mbcs", errors="replace").strip()
                        if msg:
                            log(f"发声进程报错：{msg}")
                            speaker.cleanup()
                            return 1
                except subprocess.TimeoutExpired:
                    speaker.stop()
            speaker.cleanup()
        log(f"念完了（{which}）" if ok else "没念出来")
        return 0 if ok else 1

    log(f"开始跟 {os.path.basename(MONOLOGUE)}，后端={which}")
    if isinstance(speaker, MiniMaxVoice):
        log(f"  {speaker.describe()}")
    else:
        if not list_voices():
            log("警告：读不到任何声音。先跑 --list 看看，可能这台机器的语音组件不全。")

    tail = Tail(MONOLOGUE)
    # 连续失败就熔断：key 过期 / 断网 / 引擎坏了的时候，一直重试只会把日志刷满，
    # 而且球看起来"在说话"其实一声不吭 —— 那比直接停掉更难查。
    failures = 0
    max_failures = 5
    try:
        while True:
            for line in tail.read():
                if speaker.say(line):
                    failures = 0
                else:
                    failures += 1
                    if failures >= max_failures:
                        log(f"连续 {failures} 句没念出来，停下。先跑 --test 看具体报错。")
                        return 1
            time.sleep(max(0.05, int(cfg.get("poll_ms") or 200) / 1000))
    except KeyboardInterrupt:
        log("收工")
    finally:
        speaker.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
