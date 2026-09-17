"""
引擎单例注册表（批次1修复 Bug A）

背景：audiosocket.py 原来用 `from main import get_asr` 拿引擎单例。
但容器入口是 `python3 app/main.py`，模块名是 __main__ 不是 main，
`from main import ...` 会重新加载第二个 main 模块实例（asr_engine=None），
导致每次通话首次调用时 ASR 模型重新加载 ~3 秒阻塞 event loop，
期间无保活帧 → Asterisk AudioSocket 2s 硬超时挂断（freepbx 日志大量
"Reached timeout after 2000 ms"）。

修复：main.py 初始化引擎后注册到这里，audiosocket.py 从这里取，
两个模块共享同一份单例，不再重复加载。
"""

_engines = {}


def set_engine(name: str, engine):
    _engines[name] = engine


def get_engine(name: str):
    return _engines.get(name)


def has_engine(name: str) -> bool:
    return name in _engines


def reset_engine(name: str):
    """中屏切换 TTS 引擎等场景：重置单例（批次2 用）"""
    _engines.pop(name, None)
