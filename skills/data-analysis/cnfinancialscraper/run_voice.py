# -*- coding: utf-8 -*-
"""
cn-financial-scraper 语音交互入口 v7.7.0

用法：
  python run_voice.py                    # 语音对话循环（STT 不可用自动降级文字输入）
  python run_voice.py --no-tts           # 不播报，仅识别+文字输出
  python run_voice.py --once "贵州茅台最近7天的舆情"
  python run_voice.py --say "你好"        # TTS 自检
"""
import sys
import os
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")


def main():
    # run_voice.py 剩余参数透传给 voice_interface CLI
    from voice_interface import VoiceChat, speak, stt_available

    argv = sys.argv[1:]
    if "--say" in argv:
        text = argv[argv.index("--say") + 1] if len(argv) > argv.index("--say") + 1 else ""
        sys.exit(0 if speak(text) else 1)

    tts = "--no-tts" not in argv
    if "--once" in argv:
        i = argv.index("--once")
        text = argv[i + 1] if len(argv) > i + 1 else ""
        print(VoiceChat(tts=tts).handle_text(text))
        return

    print()
    print("  cn-financial-scraper 语音交互 v7.7.0")
    print("  " + "-" * 42)
    if not stt_available():
        print("  提示：未检测到语音识别能力（Windows SAPI/vosk），")
        print("        将使用文字输入；安装 vosk 可启用离线语音识别：")
        print("        pip install vosk pyaudio")
    print()
    VoiceChat(tts=tts).run()


if __name__ == "__main__":
    main()
