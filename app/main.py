"""
固话智连 AI 后端 - 入口
Flask 中屏（管理 Web）+ AudioSocket Server（AI 对话）
"""

import os
import sys
import asyncio
import logging
from pathlib import Path

# 确保 app 目录在可导入路径
sys.path.insert(0, str(Path(__file__).parent))

import yaml
from flask import Flask
from flask_cors import CORS

# ── 加载配置 ──
SCRIPT_DIR = Path(__file__).parent.parent  # ai-backend/
_env_config = os.environ.get("CONFIG_PATH")
if _env_config:
    CONFIG_PATH = _env_config
else:
    # 开发时找项目根目录的 config.yaml
    _dev_path = SCRIPT_DIR / "config.yaml"
    CONFIG_PATH = str(_dev_path) if _dev_path.exists() else "/app/config.yaml"


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


config = load_config(CONFIG_PATH)

# ── 日志配置 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ai-backend")

# ── Flask 应用 ──
app = Flask(__name__)
CORS(app)

# 注册中屏路由
from web.routes import register_routes
register_routes(app, config)


# ── 日志队列（中屏实时日志，与 web/routes.py 共享同一个 list）──
from web.routes import _global_log_queue as log_queue

# ── 初始化各引擎（懒加载，首次调用时初始化）──
asr_engine = None
llm_client = None
search_client = None
tts_engine = None
scheduler = None


def get_asr():
    """获取 ASR 引擎单例（工厂化：create_asr 按 config['asr']['provider'] 实例化对应引擎）。

    引擎单例统一注册到 engine.registry；注册表为空（中屏切换 provider 时
    routes.py 调 registry.reset_engine("asr")）→ 丢弃旧全局引用并重建，
    确保下次通话用新引擎。
    """
    global asr_engine
    from engine.registry import get_engine, set_engine
    registered = get_engine("asr")
    if registered is not None:
        asr_engine = registered
        return registered
    if asr_engine is not None:
        # 注册表被重置（provider 已切换）→ 强制重建，丢弃旧引擎引用
        asr_engine = None
    from engine.asr import create_asr
    asr_engine = create_asr(config.get("asr", {}))
    set_engine("asr", asr_engine)
    logger.info(f"ASR 引擎初始化完成 ({config['asr'].get('provider', '?')})")
    return asr_engine


def get_llm():
    global llm_client
    from engine.registry import get_engine, set_engine
    registered = get_engine("llm")
    if registered is not None:
        llm_client = registered
        return registered
    if llm_client is not None:
        # 注册表被重置（中屏切了 provider）→ 强制重建，丢弃旧引擎引用
        llm_client = None
    from engine.llm import OpenAILLM
    llm_client = OpenAILLM(config.get("llm", {}))
    set_engine("llm", llm_client)
    logger.info(f"LLM 客户端初始化完成 (provider={config['llm'].get('provider', '?')})")
    return llm_client


def get_search():
    """获取搜索客户端单例（工厂化：reset_engine("search") 后重建读新配置）。"""
    global search_client
    from engine.registry import get_engine, set_engine
    registered = get_engine("search")
    if registered is not None:
        search_client = registered
        return registered
    if search_client is not None:
        # 注册表被重置（中屏改了搜索配置）→ 强制重建，丢弃旧实例
        search_client = None
    from engine.search import SearchClient
    search_client = SearchClient(config.get("search", {}))
    set_engine("search", search_client)
    enabled = "启用" if search_client.enabled else "禁用"
    logger.info(f"搜索客户端初始化完成（{enabled}）")
    return search_client


def get_tts():
    """获取 TTS 引擎单例（工厂化：create_tts 按 config['tts']['provider'] 实例化对应引擎）。

    与 get_asr 相同：注册表为空（中屏切换 provider）→ 重建，下次通话用新引擎。
    """
    global tts_engine
    from engine.registry import get_engine, set_engine
    registered = get_engine("tts")
    if registered is not None:
        tts_engine = registered
        return registered
    if tts_engine is not None:
        tts_engine = None
    from engine.tts import create_tts
    tts_engine = create_tts(config.get("tts", {}))
    set_engine("tts", tts_engine)
    logger.info(f"TTS 引擎初始化完成 ({config['tts'].get('provider', '?')})")
    return tts_engine


def get_scheduler():
    """获取定时任务调度器单例（APScheduler 后台线程）。"""
    global scheduler
    from engine.registry import get_engine, set_engine
    registered = get_engine("scheduler")
    if registered is not None:
        scheduler = registered
        return registered
    if scheduler is not None:
        return scheduler
    from engine.scheduler import SchedulerManager
    scheduler = SchedulerManager(config)
    set_engine("scheduler", scheduler)
    logger.info("定时任务调度器初始化完成")
    return scheduler


# ── AudioSocket Server（异步）──
from engine.audiosocket import AudioSocketServer


async def start_audiosocket():
    """启动 AudioSocket TCP 服务器"""
    asc = config["network"]
    server = AudioSocketServer(
        host=asc["audiosocket_host"],
        port=asc["audiosocket_port"],
        config=config,
        log_queue=log_queue,
        get_asr=get_asr,
        get_tts=get_tts,
        get_llm=get_llm,
    )
    await server.start()
    logger.info(f"AudioSocket 服务端已启动 ({asc['audiosocket_host']}:{asc['audiosocket_port']})")


# ── 启动入口 ──
def main():
    flask_cfg = config["network"]
    logger.info("=" * 50)
    logger.info("固话智连 AI 后端启动")
    logger.info(f"中屏管理: http://{flask_cfg['flask_host']}:{flask_cfg['flask_port']}")
    logger.info(f"AudioSocket: {flask_cfg['audiosocket_host']}:{flask_cfg['audiosocket_port']}")
    logger.info("=" * 50)

    # ── 预加载各引擎（启动时加载，不等用户打电话才初始化）──
    try:
        logger.info("预加载 ASR 引擎...")
        get_asr()
        logger.info("预加载 ASR 完成")
    except Exception as e:
        logger.error(f"ASR 预加载失败: {e}")
        import traceback
        logger.error(traceback.format_exc()[-300:])
    try:
        logger.info("预加载 LLM 客户端...")
        get_llm()
        logger.info("预加载 LLM 完成")
    except Exception as e:
        logger.warning(f"LLM 预加载失败（不影响通话）: {e}")
    try:
        logger.info("预加载搜索客户端...")
        get_search()
    except Exception as e:
        logger.warning(f"搜索客户端预加载失败（不影响通话）: {e}")
    try:
        logger.info("预加载 TTS 引擎...")
        get_tts()
        logger.info("预加载 TTS 完成")
    except Exception as e:
        logger.warning(f"TTS 预加载失败（不影响通话）: {e}")

    # 预合成开场白和占位语（按 tts 配置从对应目录加载，edge 含 voice 后缀）
    from engine.audiosocket import load_cached_audio
    try:
        load_cached_audio(config.get("tts", {}))
    except Exception as e:
        logger.warning(f"加载预合成音频失败（不影响启动）: {e}")

    # 定时任务调度器（APScheduler 后台线程，读 scheduler 配置）
    try:
        logger.info("初始化定时任务调度器...")
        get_scheduler().reload()
        logger.info("定时任务调度器就绪")
    except Exception as e:
        logger.warning(f"定时任务调度器初始化失败（不影响通话）: {e}")

    # Flask 在线程跑（不阻塞主线程）
    import threading
    flask_thread = threading.Thread(
        target=app.run,
        kwargs={
            "host": flask_cfg["flask_host"],
            "port": flask_cfg["flask_port"],
            "debug": False,
            "use_reloader": False,
        },
        daemon=True,
    )
    flask_thread.start()

    # 主线程跑 asyncio 事件循环（AudioSocket）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(start_audiosocket())
    except KeyboardInterrupt:
        logger.info("服务关闭")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
