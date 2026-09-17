#!/usr/bin/env python3
"""
预合成固定话术音频（每 TTS 引擎 5 个文件）。
运行方式：
  cd 项目根目录
  python3 app/pregen_cached_audio.py

输出目录：data/audio/<provider>/welcome.slin（等 6 个文件）
"""

import sys
import os
import logging
from pathlib import Path

# 确保 app 目录可导入（SCRIPT_DIR = 项目根）
SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR / "app"))

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("pregen-cached-audio")

# ── 话术常量（与 audiosocket.py 保持一致）──
GREETING_TEXT = "您好，我是智话通，请问有什么可以帮您"
WAITING_TEXT = "好的请稍等"
CALLBACK_TEXT = "我需要查询一下，稍后给您回电，再见"
FAREWELL_TEXT = "祝您生活愉快，再见"
CALLBACK_INTRO_TEXT = "您好，我是智话通，根据您之前的问题，回复如下："
NO_SEARCH_RESULT_TEXT = "暂时无法查询，您可以稍后再试"
EMPTY_REPLY_TEXT = "这个问题我暂时无法回答，抱歉"

TEXTS = {
    "welcome": GREETING_TEXT,
    "query_ack": WAITING_TEXT,
    "callback": CALLBACK_TEXT,
    "goodbye": FAREWELL_TEXT,
    "callback_intro": CALLBACK_INTRO_TEXT,
    "no_search_result": NO_SEARCH_RESULT_TEXT,
    "empty_reply": EMPTY_REPLY_TEXT,
}


def _voice_slug(voice: str) -> str:
    """zh-CN-XiaoxiaoNeural → xiaoxiao（用于 edge 多音色预合成文件名后缀）。"""
    v = (voice or "").lower().replace("zh-cn-", "").replace("neural", "")
    return v.strip("-") or "default"


def main():
    # 读配置
    config_path = SCRIPT_DIR / "config.yaml"
    if not config_path.exists():
        logger.error(f"配置文件不存在: {config_path}")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    tts_config = config.get("tts", {})
    providers = ["matcha-zh-en-8k", "edge"]
    base_dir = SCRIPT_DIR / "data" / "audio"

    from engine.tts import create_tts

    for provider in providers:
        sub_config = tts_config.get(provider, {})
        if not sub_config:
            logger.warning(f"跳过 {provider}：配置为空")
            continue

        logger.info(f"─" * 50)
        logger.info(f"初始化 TTS 引擎: {provider} (speed={sub_config.get('speed','?')})")

        # 构造完整 tts 段配置（factory 按 provider + sub 实例化）
        engine_config = {"provider": provider, provider: sub_config}
        engine = create_tts(engine_config)

        out_dir = base_dir / provider
        out_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"输出目录: {out_dir}")

        # edge 多音色：文件名加 voice 后缀，避免切换音色后串音（复用旧 voice 缓存）
        voice_suffix = ""
        if provider == "edge":
            voice_suffix = "_" + _voice_slug(sub_config.get("voice", ""))

        for stem, text in TEXTS.items():
            out_path = out_dir / f"{stem}{voice_suffix}.slin"
            logger.info(f"  合成 {out_path.name} ← '{text[:20]}...' ({len(text)}字)")
            slin_data = engine.synthesize(text)
            if not slin_data:
                logger.error(f"  ❌ {out_path.name} 合成失败（返回空）")
                continue
            with open(out_path, "wb") as f:
                f.write(slin_data)
            duration_s = len(slin_data) / (8000 * 2)  # 16bit=2B/样本, 8kHz
            logger.info(f"  ✅ {out_path.name} → {len(slin_data):>6}B ({duration_s:.2f}s @ 8kHz)")

        logger.info(f"完成 {provider}: {out_dir}")

    logger.info(f"")
    logger.info(f"{'='*50}")
    logger.info(f"全部预合成完成。生成文件:")
    for provider in providers:
        dir_path = base_dir / provider
        if dir_path.exists():
            total = 0
            for f_path in sorted(dir_path.iterdir()):
                total += f_path.stat().st_size
            logger.info(f"  {provider}/: {total} 字节")
    logger.info(f"{'='*50}")


if __name__ == "__main__":
    main()
