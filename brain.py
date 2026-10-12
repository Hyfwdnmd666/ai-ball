# -*- coding: utf-8 -*-
"""brain.py — 让球真的会回话。

它是个常驻小回路：

    input.log  --轮询-->  brain.py  --调模型-->  monologue.log
    （你说的话）                                  （独白 / 回话）

**球那一侧完全不知道有这个进程** —— 它只认文件。
所以换大脑不用改球一行代码；以后接游戏状态，也只是往这个进程里塞东西。

用法
----
    python brain.py --brain echo        # 假大脑，只验证管道通不通（不花钱）
    python brain.py --brain api         # 真大脑，需要一个 key

key 的来源（按优先级）：
    --api-key 参数  >  环境变量 DEEPSEEK_API_KEY / OPENAI_API_KEY  >  brain.json

只依赖标准库（urllib），跟球保持一致：零第三方包。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

# 中文 Windows 的控制台默认是 GBK：遇到编不出的字符会抛 UnicodeEncodeError，
# 把整个脚本干掉（实测就是这么崩的，而且只有中文用户会撞上）。
# 宁可打成问号，也别崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    "brain": "echo",
    "input_file": "input.log",
    "output_file": "monologue.log",
    "persona_file": "persona.md",
    "context_file": "context.md",
    "history_file": "brain_history.json",
    "status_file": "status.json",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "api_key": "",
    "temperature": 1.1,
    "max_tokens": 600,
    "history_turns": 12,
    "interval": 0.4,
    "replay": False,
    "timeout": 60,
    # 心跳要不要连 mood 一起写。设成 false：只刷 alive，
    # 把 mood 让给游戏 mod / 别的进程 —— 否则它们设的颜色会被每 3 秒踩回去。
    "write_mood": True,
}


# ---------------------------------------------------------------- 配置


def load_config(args) -> dict:
    cfg = dict(DEFAULTS)
    path = os.path.join(HERE, "brain.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception as exc:
            print(f"[brain] brain.json 读不了（用默认值）：{exc}")

    # 命令行覆盖
    # 注意这里必须是 `is not None`：interval / replay 的 argparse 默认值得是
    # None。用 0.0 / False 当默认的话这里恒真，会把 brain.json 里的值统统覆盖掉
    # —— 实测后果是 interval=0 空转、每轮整读一遍文件，烧满一个核。
    for k in ("brain", "base_url", "model", "api_key", "interval", "replay"):
        v = getattr(args, k, None)
        if v is not None and v != "":
            cfg[k] = v

    if not cfg.get("api_key"):
        for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MOONSHOT_API_KEY"):
            if os.environ.get(env):
                cfg["api_key"] = os.environ[env]
                cfg["_key_from"] = env
                break
    return cfg


def read_text(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- 大脑


def brain_echo(_cfg, _messages, user_text):
    """假大脑：只证明管道是通的，不花钱。"""
    return (
        f"（管道测试）我听见了：「{user_text}」。\n"
        f"我是 echo 大脑，不会真的回答。想听真话就把 --brain 换成 api。"
    )


def brain_api(cfg, messages, _user_text):
    if not cfg.get("api_key"):
        raise RuntimeError(
            "没有 API key。设环境变量 DEEPSEEK_API_KEY，或写进 brain.json，"
            "或传 --api-key。"
        )
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": cfg["model"],
            "messages": messages,
            "temperature": float(cfg["temperature"]),
            "max_tokens": int(cfg["max_tokens"]),
            "stream": False,
        }
    ).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + cfg["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=float(cfg["timeout"])) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}：{detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连不上 {url}：{exc.reason}") from None

    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        raise RuntimeError(f"返回格式看不懂：{str(data)[:300]}") from None


BRAINS = {"echo": brain_echo, "api": brain_api}


# ---------------------------------------------------------------- 文件监听


class Tail:
    """按**字节**读一个只增文件，按完整行切。

    为什么是字节不是字符：写入方可能分两次 flush，把一行的尾巴和下一个
    多字节字符切在中间。按字符记偏移 + errors="replace" 会把半个汉字变成
    U+FFFD 永久吞掉 —— 实测：只写「你好」的前 5 字节、再补全，"好"就没了。

    另外用「开头 64 字节的指纹」识破**重写**。只看变短是不够的：
    文件被整体改写成更长的内容时，前面那段会被当成"已经读过"直接跳过，
    而且是以碎片形式喂给模型。
    """

    HEAD = 64

    def __init__(self, path: str, skip_backlog: bool = True) -> None:
        self.path = path
        self.buf = b""     # 还没成行的字节
        self.offset = 0    # 字节偏移
        self.head = None   # 开头指纹，用来识破重写
        if skip_backlog and os.path.exists(path):
            try:
                self.offset = os.path.getsize(path)
                with open(path, "rb") as f:
                    self.head = f.read(self.HEAD)
            except OSError:
                self.offset = 0

    def read_new_lines(self) -> list:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "rb") as f:
                head = f.read(self.HEAD)
                size = os.path.getsize(self.path)
                if size < self.offset or (self.head is not None and head != self.head):
                    # 变短 = 被清空；开头变了 = 被整体重写。两种都从头读。
                    self.offset = 0
                    self.buf = b""
                self.head = head
                if size <= self.offset:
                    return []
                f.seek(self.offset)
                raw = f.read()
            self.offset = size
        except OSError:
            return []

        self.buf += raw
        # 只消费到最后一个换行 —— 半行和半个多字节字符都留到下次
        cut = self.buf.rfind(b"\n")
        if cut < 0:
            return []
        block, self.buf = self.buf[: cut + 1], self.buf[cut + 1 :]
        text = block.decode("utf-8", "replace")
        return [ln.strip() for ln in text.splitlines() if ln.strip()]


def append_output(path: str, text: str) -> None:
    """追加到独白文件。球会按字符流式放出来。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n\n")
        f.flush()


# ---- 心跳：让球能显示「脑子在不在线」 ----
#
# **必须跑在独立线程里。**
# 如果写心跳和调 API 都在主线程，一次慢请求（timeout 默认 60 秒）就会让
# `alive` 停摆超过球那边的 stale_s（默认 20 秒）—— 球会在脑子明明还活着的
# 时候变灰。而那正是这套心跳最想避免的假象。
#
# 也别想靠「把 timeout 调小」绕过：urlopen 的 timeout 是**单次 socket 超时**，
# DNS、握手、重传都能把总时长顶过 20 秒。
class Heartbeat:
    def __init__(self, cfg, period: float = 3.0) -> None:
        self.path = os.path.join(HERE, cfg.get("status_file", "status.json"))
        self.period = period
        # 要不要连 mood 一起写。设成 false 的话只刷心跳，把 mood 让给别人
        # （游戏 mod 想自己控制球的脸色时用这个）。
        self.write_mood = bool(cfg.get("write_mood", True))
        self.mood = "idle"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._write()
        self._thread.start()

    def set(self, mood: str) -> None:
        self.mood = mood
        self._write()

    def stop(self) -> None:
        self._stop.set()

    def _current_mood(self):
        """文件里现在是什么 mood（读不到就 None）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                cur = json.load(f)
            if isinstance(cur, dict):
                return cur.get("mood")
        except Exception:
            pass
        return None

    def _write(self) -> None:
        # **心跳是我们自己的，mood 不一定。**
        # 默认两个都写；但 write_mood=false 时只刷 alive、把文件里现有的
        # mood 原样留着 —— 否则别人（游戏 mod）设的颜色会被我们每 3 秒踩回去，
        # 「任何进程都能写状态通道」这句话就成了假的。
        mood = self.mood
        if not self.write_mood:
            mood = self._current_mood()
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                if mood is None:
                    json.dump({"alive": time.time()}, f)
                else:
                    json.dump({"alive": time.time(), "mood": mood}, f)
            os.replace(tmp, self.path)   # 原子替换：球每秒读一次，不能撞到写一半的文件
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop.wait(self.period):
            self._write()


# ---------------------------------------------------------------- 主循环


def load_history(path: str) -> list:
    """读会话历史。

    **必须是「消息数组」。** 写成对象/字符串时直接丢掉 ——
    不然 `msgs.extend(history)` 会把键名当消息发给模型，等回复回来
    `history.append` 就 AttributeError 把整个常驻回路干掉。
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception as exc:
        print(f"[brain] 历史读不了（当空的用）：{exc}")
        return []
    if not isinstance(loaded, list):
        print(f"[brain] {os.path.basename(path)} 不是数组，忽略它")
        return []
    return [
        m for m in loaded
        if isinstance(m, dict) and "role" in m and "content" in m
    ]


def build_messages(cfg, persona: str, context: str, history: list, user_text: str):
    system = persona or "你在用一个悬浮球跟人聊天。用简体中文，口语，短句。"
    if context:
        system += "\n\n---\n\n当前局势：\n" + context
    msgs = [{"role": "system", "content": system}]
    msgs.extend(history)
    msgs.append({"role": "user", "content": user_text})
    return msgs


def main() -> int:
    ap = argparse.ArgumentParser(description="让球会回话的常驻回路")
    ap.add_argument("--brain", choices=sorted(BRAINS), help="用哪个大脑")
    ap.add_argument("--api-key", dest="api_key", default="")
    ap.add_argument("--base-url", dest="base_url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--interval", type=float, default=None, help="轮询间隔（秒）")
    ap.add_argument("--replay", action="store_true", default=None,
                    help="连历史消息一起回（默认跳过）")
    ap.add_argument("--test", action="store_true",
                    help="只发一条测试消息，验证 key 和网络，然后退出")
    args = ap.parse_args()

    cfg = load_config(args)
    in_path = os.path.join(HERE, cfg["input_file"])
    out_path = os.path.join(HERE, cfg["output_file"])
    persona = read_text(os.path.join(HERE, cfg["persona_file"]))
    hist_path = os.path.join(HERE, cfg["history_file"])

    fn = BRAINS.get(cfg["brain"])
    if fn is None:
        print(f"[brain] 不认识的大脑：{cfg['brain']}", file=sys.stderr)
        return 2

    if args.test:
        print(f"[brain] 测试中：{cfg['brain']}"
              + (f" / {cfg['model']} @ {cfg['base_url']}" if cfg["brain"] == "api" else ""))
        if cfg["brain"] == "api" and not cfg.get("api_key"):
            print("[brain] [x] 没有 API key。三个来源任选：\n"
                  "        --api-key sk-xxx\n"
                  "        环境变量 DEEPSEEK_API_KEY\n"
                  "        brain.json 里的 \"api_key\"")
            return 1
        msgs = build_messages(cfg, persona, "", [], "喂，听得见吗？")
        t0 = time.time()
        try:
            reply = fn(cfg, msgs, "喂，听得见吗？")
        except Exception as exc:
            print(f"[brain] [x] 失败：{exc}")
            return 1
        print(f"[brain] [ok] 通了（{time.time() - t0:.1f} 秒）。它回的是：\n")
        print(reply)
        print("\n[brain] 这一步过了，去掉 --test 正常启动就行。")
        return 0

    history = load_history(hist_path)

    tail = Tail(in_path, skip_backlog=not cfg["replay"])

    print(f"[brain] 大脑 = {cfg['brain']}"
          + (f"（{cfg['model']} @ {cfg['base_url']}）" if cfg["brain"] == "api" else ""))
    if cfg["brain"] == "api" and not cfg.get("api_key"):
        print("[brain] [!]  没有 API key —— 收到消息时会报错。"
              "设 DEEPSEEK_API_KEY 或写进 brain.json。")
    if cfg.get("_key_from"):
        print(f"[brain] key 来自环境变量 {cfg['_key_from']}")
    print(f"[brain] 监听 {os.path.basename(in_path)} -> 写 {os.path.basename(out_path)}")
    print(f"[brain] 人格 {os.path.basename(cfg['persona_file'])}"
          f"（{len(persona)} 字）")
    if cfg["replay"]:
        print("[brain] --replay：会连历史消息一起回")
    # 配置先转成强类型，坏值给默认 —— 别让一个手抖的 brain.json 把回路弄死
    try:
        interval = max(0.05, float(cfg["interval"]))
    except (TypeError, ValueError):
        interval = 0.4
        print(f"[brain] interval 不是数字，改用 {interval}")
    try:
        keep_n = max(2, int(cfg["history_turns"]) * 2)
    except (TypeError, ValueError):
        keep_n = 24
        print(f"[brain] history_turns 不是整数，改用 {keep_n // 2} 轮")

    beat = Heartbeat(cfg)
    beat.start()
    print(f"[brain] 心跳 每 {beat.period:.0f} 秒 -> {os.path.basename(beat.path)}"
          f"（独立线程，慢请求也不会让球变灰）")
    print(f"[brain] 轮询间隔 {interval:.2f} 秒")
    print("[brain] 就绪。Ctrl+C 退出。")

    try:
        while True:
            # 外层兜底：常驻回路绝不能因为某一轮出错就死掉 ——
            # 死了球会变灰，而终端里可能连一行提示都没有。
            try:
                for text in tail.read_new_lines():
                    print(f"[brain] <- {text}")
                    beat.set("thinking")
                    context = read_text(os.path.join(HERE, cfg["context_file"]))
                    msgs = build_messages(cfg, persona, context, history, text)
                    try:
                        reply = fn(cfg, msgs, text)
                    except Exception as exc:
                        print(f"[brain] [x] 调模型失败：{exc}")
                        # **让失败在球上也看得见。**
                        # 不说的话：Tail 的偏移已经前进了（这条消息不会再来一遍），
                        # 终端又可能根本没人看 —— 用户那边的体验就是
                        # 「打了字，然后什么也没发生」，最难查的一类问题。
                        try:
                            append_output(
                                out_path,
                                f"（脑子卡住了：{exc}）\n\n（这句我没接上，你再说一次。）",
                            )
                        except Exception:
                            pass
                        beat.set("broken")
                        time.sleep(3.0)
                        beat.set("idle")
                        continue
                    if not reply:
                        print("[brain] （空回复，跳过）")
                        beat.set("idle")
                        continue
                    append_output(out_path, reply)
                    print(f"[brain] -> {reply[:60]}{'...' if len(reply) > 60 else ''}")
                    history.append({"role": "user", "content": text})
                    history.append({"role": "assistant", "content": reply})
                    history[:] = history[-keep_n:]
                    try:
                        with open(hist_path, "w", encoding="utf-8") as f:
                            json.dump(history, f, ensure_ascii=False, indent=1)
                    except Exception as exc:
                        print(f"[brain] 历史存不了：{exc}")
                    beat.set("idle")
            except Exception as exc:
                print(f"[brain] [x] 这一轮出错，继续跑：{exc}")
                beat.set("broken")
                time.sleep(1.0)
                beat.set("idle")

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[brain] 停了。")
    finally:
        beat.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
