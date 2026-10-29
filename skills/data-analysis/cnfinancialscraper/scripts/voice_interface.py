# -*- coding: utf-8 -*-
"""
语音交互引擎 v7.7.0 (voice_interface.py)

为金融爬虫增加语音交互：说一句话 → 语音识别 → 对话式舆情引擎（chat_handle）
→ 回复文本 + 语音播报。

v7.7 新增：
  - 知识库语音指令：「总结到知识库/同步到 ima/负面总结进 dify/
    查一下知识库里的XX/用知识库校对」直接路由到 knowledge_sync（无需打字）
  - 「命令/帮助」播报指令清单；TTS 数字归一化（百分比/小数朗读友好）

零依赖优先（Windows 内置 SAPI，经 Windows PowerShell 5.1 调用）：
  - TTS：System.Speech.Synthesis（可选升级 pyttsx3）
  - STT：System.Speech.Recognition 听写语法（可选升级 vosk）

用法：
    from scripts.voice_interface import VoiceChat
    VoiceChat().run()                 # 语音循环（STT 不可用时自动降级文本输入）

    from scripts.voice_interface import speak, listen_once, summarize_for_speech
    speak("爬取完成，共 32 条舆情")
    text = listen_once(timeout=8)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

IS_WINDOWS = sys.platform.startswith("win")
# System.Speech 位于 .NET Framework，必须走 Windows PowerShell 5.1（pwsh 7 无此程序集）
_PS51 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

try:
    from .sentiment_chat import chat_handle
except ImportError:
    try:
        from sentiment_chat import chat_handle  # type: ignore
    except ImportError:
        chat_handle = None  # type: ignore


# ── TTS 语音播报 ───────────────────────────────────────────────

def speak(text: str, engine: str = "auto", rate: int = 0) -> bool:
    """把文本转语音播报。成功返回 True。

    engine: auto / pyttsx3 / sapi / off
    """
    if not text or engine == "off":
        return False
    if engine in ("auto", "pyttsx3"):
        if _speak_pyttsx3(text, rate):
            return True
        if engine == "pyttsx3":
            return False
    return _speak_sapi(text, rate)


def _speak_pyttsx3(text: str, rate: int = 0) -> bool:
    try:
        import pyttsx3  # type: ignore
        eng = pyttsx3.init()
        if rate:
            r = eng.getProperty("rate")
            eng.setProperty("rate", max(80, min(300, r + rate * 20)))
        eng.say(text)
        eng.runAndWait()
        return True
    except Exception:
        return False


def _ps51_available() -> bool:
    return IS_WINDOWS and os.path.exists(_PS51)


def _speak_sapi(text: str, rate: int = 0) -> bool:
    """Windows SAPI 播报（经 Windows PowerShell 5.1，零依赖）。"""
    if not _ps51_available():
        return False
    # 文本经临时文件传递，避免命令行转义问题
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="voice_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate = {max(-5, min(5, rate))}; "
            f"$txt = Get-Content -Raw -Encoding UTF8 '{tmp}'; "
            "$s.Speak($txt)"
        )
        subprocess.run([_PS51, "-NoProfile", "-Command", script],
                       timeout=max(30, len(text) // 5 + 30),
                       capture_output=True)
        return True
    except Exception:
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── STT 语音识别 ───────────────────────────────────────────────

def listen_once(timeout: float = 8.0, engine: str = "auto") -> Optional[str]:
    """录一次音并识别为文本；失败/超时返回 None。

    engine: auto / vosk / sapi
    """
    if engine in ("auto", "vosk"):
        text = _listen_vosk(timeout)
        if text:
            return text
        if engine == "vosk":
            return None
    return _listen_sapi(timeout)


def _listen_vosk(timeout: float) -> Optional[str]:
    try:
        from vosk import Model, KaldiRecognizer  # type: ignore
        import pyaudio  # type: ignore
        import json as _json
        model = Model()
        rec = KaldiRecognizer(model, 16000)
        pa = pyaudio.PyAudio()
        stream = pa.open(rate=16000, channels=1, format=pyaudio.paInt16,
                         input=True, frames_per_buffer=8000)
        stream.start_stream()
        try:
            import time
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                data = stream.read(4000, exception_on_overflow=False)
                if rec.AcceptWaveform(data):
                    res = _json.loads(rec.Result())
                    if res.get("text"):
                        return res["text"]
            res = _json.loads(rec.FinalResult())
            return res.get("text") or None
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
    except Exception:
        return None


def _listen_sapi(timeout: float) -> Optional[str]:
    """Windows SAPI 听写（需系统装有中文语音识别；零依赖）。"""
    if not _ps51_available():
        return None
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$r = New-Object System.Speech.Recognition.SpeechRecognitionEngine; "
        "try { $r.SetInputToDefaultAudioDevice() } catch { exit 2 }; "
        "$r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar)); "
        f"$res = $r.Recognize([TimeSpan]::FromSeconds({max(1.0, float(timeout))})); "
        "if ($res) { Write-Output $res.Text }"
    )
    try:
        p = subprocess.run([_PS51, "-NoProfile", "-Command", script],
                           timeout=timeout + 15, capture_output=True)
        out = (p.stdout or b"").decode("utf-8", "ignore").strip()
        return out or None
    except Exception:
        return None


def stt_available() -> bool:
    """STT 是否可用（vosk 已装或 Windows SAPI 可调用）。"""
    try:
        import vosk  # noqa: F401
        return True
    except ImportError:
        pass
    return _ps51_available()


# ── 回复压缩（适合语音播报） ───────────────────────────────────

_MARKDOWN_RE = re.compile(r"(\*\*|##|`|\||\r)+")
_URL_RE = re.compile(r"https?://\S+")


def summarize_for_speech(reply: str, max_chars: int = 300) -> str:
    """把对话引擎的长回复压缩成适合朗读的短文。

    规则：去 Markdown 符号/URL → 按行取要点 → 截断到句尾。
    """
    if not reply:
        return ""
    text = _URL_RE.sub("", reply)
    text = _MARKDOWN_RE.sub(" ", text)
    lines = [l.strip(" -•·*>") for l in text.splitlines()]
    lines = [l for l in lines if l.strip()]
    out = "。".join(lines)
    out = re.sub(r"\s+", "", out) if _is_cjk(out) else re.sub(r"\s+", " ", out)
    if len(out) > max_chars:
        cut = out[:max_chars]
        for p in ("。", "；", "，"):
            idx = cut.rfind(p)
            if idx > max_chars // 2:
                cut = cut[:idx + 1]
                break
        out = cut + "（更多内容见终端输出）"
    return _normalize_numbers_for_tts(out)


def _is_cjk(text: str) -> bool:
    if not text:
        return False
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return cjk > len(text) * 0.2


def _normalize_numbers_for_tts(text: str) -> str:
    """v7.7 TTS 朗读归一化：82.5% → 百分之82.5；去掉数字前的 + 号。"""
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"百分之\1", text)
    text = re.sub(r"\+(\d)", r"\1", text)
    return text


# ── v7.7 知识库语音指令 ───────────────────────────────────

VOICE_HELP = (
    "我可以帮您："
    "一，爬取舆情，例如：爬一下贵州茅台最近7天的舆情；"
    "二，总结进知识库，例如：总结到知识库、同步到ima、负面总结进dify；"
    "三，检索知识库，例如：查一下知识库里的招商银行；"
    "四，校对数据，例如：用知识库校对；"
    "五，说“命令”可随时查看本清单。"
)

_KB_SYNC_TAIL = re.compile(
    r"(总结|同步|写入|推送|入库).{0,12}(知识库|ima|dify|obsidian|本地)",
    re.IGNORECASE)
_KB_TARGET_NAME = re.compile(
    r"总结(.{2,12}?)(?:到|进|入|至)(知识库|ima|dify|obsidian|本地)",
    re.IGNORECASE)
_KB_SEARCH_PAT = re.compile(
    r"(?:查一下|查询|查查|搜索|检索|搜一下|找一下|找)(?:一下)?"
    r"(?:知识库|库)(?:里|中|内)?(?:的|有没有|有无)(.+)$")
_SYNC_FILLER = {"一下", "数据", "结果", "舆情", "全部", "总体"}


def _kb_import():
    """懒加载 knowledge_sync（避免无依赖环境导入失败拖累语音主链路）。"""
    try:
        try:
            from . import knowledge_sync as ks
        except ImportError:
            import knowledge_sync as ks  # type: ignore
        return ks
    except Exception:
        return None


def _kb_target_from_text(text: str) -> Optional[str]:
    t = text.lower()
    if "ima" in t:
        return "ima"
    if "dify" in t:
        return "dify"
    if "obsidian" in t:
        return "obsidian"
    if "本地" in t:
        return "local"
    return None          # 未指名 → 用配置默认目标


def _kb_cmd_sync(target, mode, target_name):
    ks = _kb_import()
    if ks is None:
        return "知识库模块未加载，请先运行 python setup_env.py 检查环境"
    try:
        res = ks.sync_to_knowledge_base(target=target, mode=mode,
                                        target_name=target_name)
    except Exception as exc:
        return f"知识库同步失败：{exc}"
    if not res.get("pushed") and not res.get("updated") and not res.get("failed"):
        return res.get("message") or "没有可总结的数据，请先爬取舆情快照"
    parts = [f"已同步到 {res.get('target') or target or 'local'} 知识库："
             f"新增 {res.get('pushed', 0)} 篇"]
    if res.get("updated"):
        parts.append(f"更新 {res['updated']} 篇")
    if res.get("skipped"):
        parts.append(f"重复跳过 {res['skipped']} 篇")
    if res.get("failed"):
        parts.append(f"失败 {len(res['failed'])} 篇")
    return "，".join(parts)


def _kb_cmd_search(keyword: str):
    ks = _kb_import()
    if ks is None:
        return "知识库模块未加载，请先运行 python setup_env.py 检查环境"
    try:
        hits = ks.get_adapter().search(keyword, limit=5)
    except Exception as exc:
        return f"知识库检索失败：{exc}"
    if not hits:
        return f"知识库里没有找到「{keyword}」相关内容"
    lines = [f"知识库中找到 {len(hits)} 条与「{keyword}」相关的记录："]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. {h.get('title') or (h.get('snippet') or '')[:30]}")
    return "\n".join(lines)


def _kb_cmd_verify():
    ks = _kb_import()
    if ks is None:
        return "知识库模块未加载，请先运行 python setup_env.py 检查环境"
    try:
        items = (ks.aggregate_snapshots() or {}).get("articles") or []
        if not items:
            return "没有可校对的爬取数据（快照为空）"
        report = ks.verify_against_knowledge_base(items[:50])
    except Exception as exc:
        return f"知识库校对失败：{exc}"
    return (f"校对完成：共 {report['total']} 条，一致 {report['verified']} 条，"
            f"冲突 {report['conflicts']} 条，库中无记录 {report['no_match']} 条")


def handle_knowledge_command(text: str) -> Optional[str]:
    """v7.7 知识库语音指令路由；非知识库指令返回 None（交给对话引擎）。

    优先级：校对 → 总结同步 → 检索。
    """
    text = (text or "").strip()
    if not re.search(r"知识库|ima|dify|obsidian", text, re.IGNORECASE):
        return None
    if re.search(r"(校对|核对|校验|比对)", text):
        return _kb_cmd_verify()
    m = _KB_TARGET_NAME.search(text)
    if m or _KB_SYNC_TAIL.search(text):
        mode, target_name = "overall", ""
        name = m.group(1).strip() if m else ""
        if "负面" in text or "负面" in name:
            mode = "negative"
        elif name and name not in _SYNC_FILLER:
            mode, target_name = "target", name
        return _kb_cmd_sync(_kb_target_from_text(text), mode, target_name)
    sm = _KB_SEARCH_PAT.search(text)
    if sm:
        return _kb_cmd_search(sm.group(1).strip())
    return None


# ── 语音对话循环 ───────────────────────────────────────────────

class VoiceChat:
    """语音对话会话：听 → 识别 → chat_handle → 播报。

    全部回调可注入（listen/speak/chat），便于离线测试。
    """

    def __init__(self,
                 listen: Optional[Callable[..., Optional[str]]] = None,
                 speak_fn: Optional[Callable[[str], bool]] = None,
                 chat: Optional[Callable[[str], dict]] = None,
                 tts: bool = True,
                 listen_timeout: float = 8.0):
        self.listen = listen or listen_once
        self.speak_fn = speak_fn or (speak if tts else lambda t: False)
        self.chat = chat or chat_handle
        self.tts = tts
        self.listen_timeout = listen_timeout
        self.history = []

    def handle_text(self, text: str) -> str:
        """处理一条文本（语音或键入），返回回复。

        v7.7 路由优先级：命令清单 → 知识库指令 → 舆情对话引擎。
        """
        text = (text or "").strip()
        if not text:
            return ""
        if re.fullmatch(r"(命令|帮助|指令|你能做什么|有哪些功能)[\?？!！]?", text):
            reply = VOICE_HELP
        else:
            reply = handle_knowledge_command(text)   # v7.7 知识库语音指令
            if reply is None:
                if self.chat is None:
                    return "对话引擎未加载，请先运行 python setup_env.py"
                result = self.chat(text)
                reply = (result or {}).get("reply", "无法处理该请求")
        self.history.append({"user": text, "reply": reply})
        if self.tts and self.speak_fn:
            try:
                self.speak_fn(summarize_for_speech(reply))
            except Exception:
                pass
        return reply

    def run(self, max_turns: int = 0) -> None:
        """主循环。max_turns=0 为无限轮次；Ctrl+C 退出。"""
        can_listen = stt_available()
        print("🎙️  cn-financial-scraper 语音交互 v7.7.0")
        if can_listen:
            print("    直接说话即可下达指令（如：爬一下贵州茅台最近7天的舆情）")
            print("    新增：总结到知识库 / 同步到 ima / 查一下知识库里的XX / 用知识库校对")
        else:
            print("    ⚠️ 未检测到可用语音识别（可 pip install vosk 增强），"
                  "已切换为文字输入")
        print("    输入 quit 退出")
        turns = 0
        while True:
            try:
                if can_listen:
                    print("🎤 正在聆听…（不说话可直接键入文字）")
                    heard = self.listen(timeout=self.listen_timeout)
                    if heard:
                        print(f"   识别到: {heard}")
                        user_input = heard
                    else:
                        user_input = input("> ").strip()
                else:
                    user_input = input("> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n再见！")
                break
            if user_input.lower() in ("quit", "exit", "q", "退出"):
                print("再见！")
                break
            if not user_input:
                continue
            reply = self.handle_text(user_input)
            print()
            print(reply)
            print()
            turns += 1
            if max_turns and turns >= max_turns:
                break


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="v7.7.0 语音交互")
    ap.add_argument("--once", default="", help="处理一句话后退出")
    ap.add_argument("--no-tts", action="store_true", help="不播报语音")
    ap.add_argument("--say", default="", help="仅播报一段文本（TTS 自检）")
    args = ap.parse_args()
    if args.say:
        sys.exit(0 if speak(args.say) else 1)
    if args.once:
        vc = VoiceChat(tts=not args.no_tts)
        print(vc.handle_text(args.once))
    else:
        VoiceChat(tts=not args.no_tts).run()
