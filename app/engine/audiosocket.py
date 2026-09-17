"""
AudioSocket Server - 与 FreePBX/Asterisk 之间的实时音频流
协议: 每帧 323 字节（1byte type + 2byte len + 320byte slin16）

简化链路: 收音频→ASR→LLM→TTS→播放

v2 流水线（plan-通话体验优化v2 §2.3/§4.2 双缓冲 FIFO 三协程）:
  LLM token → [Segment Builder] → TTS队列 → RapidSpeech TTS合成 → PCM FIFO → 20ms定时播放
  三协程并行：_llm_receiver / _tts_worker / _playback_worker
  解决分段 TTS 段间静音（每段单独合成 ~0.5-1s 网络延迟 → 听感断续）。
"""

import asyncio
import collections
import logging
import socket
import time
from pathlib import Path

import numpy as np

from engine.asr import _resample_8k_to_16k

logger = logging.getLogger("ai-backend.audiosocket")

TYPE_UUID   = b'\x01'
TYPE_AUDIO  = b'\x10'
TYPE_HANGUP = b'\x00'
TYPE_ERROR  = b'\xff'

FRAME_SIZE = 320          # 320 bytes = 160 samples = 20ms @ 8kHz
PCM_SIZE   = FRAME_SIZE.to_bytes(2, 'big')
SILENCE    = b'\x00' * FRAME_SIZE
FRAME_MS   = 20           # 每帧 20ms（AudioSocket 协议是 8kHz）

# ── 双缓冲 FIFO 流水线参数（plan v2 §4.2）──
PCM_FIFO_MAXLEN       = 5000     # PCM 帧 FIFO 上限（约 100s @ 8kHz）
PLAYBACK_INTERVAL     = 0.02     # 播放协程节奏：20ms/帧
TTS_BEAT_INTERVAL     = 0.5      # TTS 合成期间每 500ms 发一帧静音帧（§6.2 兜底，防 Asterisk 2s 硬限制）
SEGMENT_COMMA_MIN     = 15       # 逗号分句阈值：缓冲 ≥15 字才切
SEGMENT_HARD_MAX      = 15       # 无标点强制分句：超过 15 字即切
SEGMENT_FLUSH_TIMEOUT = 3.0      # 3 秒无句尾 → 强制分句
TTS_SYNTH_TIMEOUT     = 60.0     # 单段合成超时上限

GREETING_TEXT = "您好，我是智话通，请问有什么可以帮您"
WAITING_TEXT  = "好的请稍等"
CALLBACK_TEXT = "我需要查询一下，稍后给您回电，再见"
FAREWELL_TEXT = "祝您生活愉快，再见"
CALLBACK_INTRO_TEXT = "您好，我是智话通，根据您之前的问题，回复如下："
NO_SEARCH_RESULT_TEXT = "暂时无法查询，您可以稍后再试"
EMPTY_REPLY_TEXT = "这个问题我暂时无法回答，抱歉"

# 预合成缓存：_cache[provider][name] = bytes
# provider ∈ {"melo8k", "melo", "zh-ll", "piper-zh"}
# name ∈ {"greeting", "waiting", "callback", "farewell", "callback_intro"}
_cache = {}
_CACHED_NAMES = {
    "greeting": "welcome.slin",
    "waiting": "query_ack.slin",
    "callback": "callback.slin",
    "farewell": "goodbye.slin",
    "callback_intro": "callback_intro.slin",
    "no_search_result": "no_search_result.slin",
}

# VAD 参数默认值（AudioSocket 8kHz：每帧20ms，600ms静音=30帧，300ms最小≈4800字节）
# 运行时可被 config.yaml 的 vad 段覆盖（plan v2 §3.3）
VAD_THRESHOLD = 500        # 能量阈值
SILENCE_TIMEOUT = 30        # 30帧 × 20ms = 600ms 静音 → 说话结束
MIN_SPEECH_BYTES = 4800     # 300ms 有效语音（≈4800字节@8kHz）
MAX_SPEECH_FRAMES = 2500    # 最大 50 秒（2500帧 × 20ms）
PRE_BUFFER_FRAMES = 5       # 预缓冲 5 帧（100ms），减少话头丢失（plan v2 §3.2 问题2）

# 回电标记（LLM 输出中包含此标记 → 触发回拨，不播 TTS）
CALLBACK_MARKER = "[CALLBACK]"


def _is_english_segment(text: str) -> bool:
    """判断文本是否以英文为主（ASCII 字母占比 > 50%）。

    用于 Bug D 修复：英文段不按 SEGMENT_HARD_MAX 硬切（避免"Once there was a/
    little cat. She"被切碎），改为按英文句尾标点（. ! ?）自然分句；中文段仍硬切。
    """
    if not text:
        return False
    alpha = sum(1 for c in text if c.isascii() and c.isalpha())
    return alpha / len(text) > 0.5


def _voice_slug(voice: str) -> str:
    """zh-CN-XiaoxiaoNeural → xiaoxiao（edge 多音色预合成文件名后缀，与 pregen 脚本一致）。"""
    v = (voice or "").lower().replace("zh-cn-", "").replace("neural", "")
    return v.strip("-") or "default"


def load_cached_audio(tts_config: dict = None):
    """按当前 TTS 配置加载预合成音频（data/audio/<provider>/ 下 5 个文件）到 _cache[provider]。

    edge 多音色：文件名带 voice 后缀（welcome_xiaoxiao.slin），切音色后加载对应后缀，
    避免复用旧 voice 缓存的固定话术（开场白/结束语等）。

    文件缺失时对应条目置 None（_get_cached_audio 会回退到 _play_tts 实时合成）。
    """
    global _cache
    tts_config = tts_config or {}
    provider = tts_config.get("provider", "matcha-zh-en-8k")
    base = Path(__file__).parent.parent.parent  # ai-backend/
    cached_dir = base / "data" / "audio" / provider

    # edge 多音色：文件名加 voice 后缀
    voice_suffix = ""
    if provider == "edge":
        edge_cfg = tts_config.get("edge", {}) or {}
        voice_suffix = "_" + _voice_slug(edge_cfg.get("voice", ""))

    _cache[provider] = {}
    for name, filename in _CACHED_NAMES.items():
        if voice_suffix:
            filename = filename[:-5] + voice_suffix + ".slin"  # welcome.slin → welcome_xiaoxiao.slin
        path = cached_dir / filename
        if path.exists():
            with open(path, "rb") as f:
                data = f.read()
            if len(data) < FRAME_SIZE:  # Bug E：0字节或不足一帧 → 按缺失处理，触发 fallback 实时合成
                logger.warning(f"[{provider}] {name}音频文件为空或过小({len(data)}字节)，按缺失处理")
                _cache[provider][name] = None
            else:
                logger.info(f"已加载[{provider}] {name}音频: {len(data)}字节 ({len(data)//FRAME_SIZE}帧)")
                _cache[provider][name] = data
        else:
            logger.warning(f"[{provider}] {name}音频文件不存在: {path}")
            _cache[provider][name] = None


class AudioSocketSession:
    def __init__(self, reader, writer, config: dict, log_queue: list = None):
        self.reader = reader
        self.writer = writer
        self.config = config if config is not None else {}
        self.call_id = None
        self.peer = writer.get_extra_info("peername")
        self._conversation = None
        self._log_queue = log_queue if log_queue is not None else []
        self._caller_number = None
        self._is_callback = False
        self._callback_pending = False  # 回电挂机后是否需回拨
        self._callback_reply = ""       # 保存 LLM 完整回复（供回拨用）
        self._is_speaking = False        # AI 正在说话（TTS 播放中）
        self._connected = True            # AudioSocket 连接是否存活
        self._idle_since = 0.0            # AI 说完开始等待用户的时间戳
        self._interrupted = False         # Barge-in 打断标记（§2.4，Demo 仅占位）
        self._has_search = False          # 本轮是否使用了联网搜索（指标采集）
        self._farewell = False            # 是否正常道别结束（指标采集）
        self._start_ts = 0.0              # 通话开始时间戳（指标采集）
        self._outcome = "hangup"          # 通话结束原因（call_history 用，默认挂机）
        self._turn_metrics: list[dict] = []  # 每轮时延数组 → 结束组装 transcript
        self._turn_start_ts = 0.0         # 当前轮 ASR endpoint 时刻（ttft 用）
        self._first_audio_ts = 0.0        # 播放协程首次写音频帧时刻（ttft 用）
        self._llm_first_token_ms = 0.0    # 当前轮 LLM 首字延时（ms）
        self._llm_total_ms = 0.0          # 当前轮 LLM 总耗时（ms）
        self._tts_total_ms = 0.0          # 当前轮 TTS 合成总耗时（ms）
        self._last_asr_ms = 0.0           # 上一轮 ASR 转写耗时（ms）

        # ── 双缓冲 FIFO 流水线（plan v2 §2.3/§4.2）──
        self._tts_queue = asyncio.Queue()                      # TTS 分段任务队列
        self._pcm_fifo = collections.deque(maxlen=PCM_FIFO_MAXLEN)  # PCM 帧缓冲

        # ── VAD 配置（config.yaml vad 段，默认值兼容旧参数）──
        vad_cfg = self.config.get("vad", {}) if isinstance(self.config, dict) else {}
        self._vad_base_threshold = float(vad_cfg.get("threshold", VAD_THRESHOLD))
        self._adapt_threshold = bool(vad_cfg.get("adapt_threshold", True))
        self._vad_threshold = self._vad_base_threshold
        silence_ms = int(vad_cfg.get("silence_timeout", SILENCE_TIMEOUT * FRAME_MS))
        self._silence_timeout_frames = max(1, silence_ms // FRAME_MS)
        min_speech_ms = int(vad_cfg.get("min_speech", MIN_SPEECH_BYTES * 1000 // (8000 * 2)))
        self._min_speech_bytes = max(160, min_speech_ms * 8000 * 2 // 1000)
        max_speech_s = float(vad_cfg.get("max_speech", MAX_SPEECH_FRAMES * FRAME_MS / 1000))
        self._max_speech_frames = int(max_speech_s * 1000 / FRAME_MS)
        self._pre_buffer_frames = int(vad_cfg.get("pre_buffer_frames", PRE_BUFFER_FRAMES))
        # 方案B：静音压缩——字间保留 max_keep_silence 毫秒静音（短停顿保留、长停顿抽掉），默认200ms
        keep_silence_ms = int(vad_cfg.get("max_keep_silence_ms", 200))
        self._max_keep_silence_frames = max(1, keep_silence_ms // FRAME_MS)

        # ── VAD 引擎选择 ──
        vad_engine_name = vad_cfg.get("vad_engine", "energy")
        if vad_engine_name == "tenvad":
            from engine.vad_ten import create_vad
            self._vad_ten = create_vad(self.config)
            # 16k VAD hop=256 samples, 16000 / 256 = 62.5 frames/s ≈ 16ms/frame
            self._vad_silence_frames_16k = int(
                self._vad_ten.min_silence_duration * 16000 / 256)
            self._vad_min_speech_frames_16k = int(
                self._vad_ten.min_speech_duration * 16000 / 256)
            self._vad_threshold_tenvad = self._vad_ten.threshold
            # 16k VAD 域重采样缓冲：每次攒够 256 个 16k 样本跑一次 VAD
            self._vad_resample_buf = bytearray()
            self._log(f"VAD 引擎: tenvad | threshold={self._vad_threshold_tenvad} | "
                      f"min_silence={self._vad_ten.min_silence_duration}s | "
                      f"min_speech={self._vad_ten.min_speech_duration}s")
        else:
            self._vad_ten = None
            self._log(f"VAD 引擎: energy | threshold={self._vad_threshold}")


    def _log(self, msg: str):
        logger.info(msg)
        if self._log_queue is not None:
            self._log_queue.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        # 双写 call_logs（SQLite 持久化，供事后排查）
        try:
            from engine.call_history import insert_log
            _call_id = getattr(self, "call_id", None) or None
            insert_log(_call_id, "INFO", msg, None)
        except Exception:
            pass  # 日志写入失败不抛异常、不阻塞主流程

    # ── 获取主叫号码 + 检测回拨模式 ──
    async def _get_caller_info(self):
        """通过 AMI 获取主叫号码 + 当前 exten（判断是否回拨模式）"""
        try:
            from asterisk.manager import Manager
            mgr = Manager()
            mgr.connect(self.config["freepbx"]["host"],
                        self.config["freepbx"]["ami_port"])
            mgr.login(self.config["freepbx"]["ami_user"],
                      self.config["freepbx"]["ami_secret"])

            r = mgr.send_action({"Action": "Command",
                                "Command": "core show channels concise"})
            output = r.get_header("Output") or ""

            caller = None
            current_exten = None
            for line in output.split("\n"):
                if not line.strip():
                    continue
                parts = line.split("!")
                if len(parts) < 5:
                    continue
                state = parts[4]
                channel = parts[0]
                if state != "Up":
                    continue
                # 跳过 AudioSocket 通道（200 分机）和 Local 通道
                if "200-" in channel or "Local/" in channel:
                    continue
                # 提取 PJSIP/XXX 格式的主叫号码
                if "PJSIP/" in channel:
                    user_part = channel.split("PJSIP/")[1].split("-")[0]
                    if user_part.isdigit() and len(user_part) >= 2:
                        caller = user_part
                        # concise 格式: 通道!上下文!分机!优先级!状态!...
                        if len(parts) > 2:
                            current_exten = parts[2]
                        break

            try:
                mgr.logoff()
            except Exception:
                pass

            # 回拨模式判定：200（回拨入口 Playback+AudioSocket）/ 201 / 202 都是回拨相关分机
            # 注意：0 是 AI 入口（正常模式），200 是回拨入口（Playback→AudioSocket）
            is_callback = (current_exten in ("200", "201", "202"))
            if caller:
                self._log(f"主叫号码: {caller}, exten={current_exten}, 回拨模式={is_callback}")
            else:
                self._log(f"未获取到主叫号码 (exten={current_exten})")
            return caller, is_callback

        except Exception as e:
            self._log(f"获取主叫信息失败: {e}")
            return None, False

    # ── 生成回拨音频（合并合成，不拼接）──
    async def _generate_callback_audio(self) -> bool:
        """一次性 TTS 合成完整回拨话术（CALLBACK_INTRO_TEXT + reply 合并，不拼接）。
        
        返回 True 表示生成成功，False 表示失败（应阻止回拨）。
        文件名含 session_id + 时间戳防串。
        """
        try:
            reply = self._callback_reply or ""
            if not reply.strip():
                self._log("回拨音频跳过：LLM 回复为空（收集超时或 API 异常）")
                return False
            full_text = CALLBACK_INTRO_TEXT + "\n" + reply
            tts = self._get_tts()
            slin_data = await tts.synthesize_async(full_text)
            if not slin_data:
                self._log("回拨音频合成失败（空输出）")
                return False
            # 文件名含主叫号+时间戳，防止跨会话串音
            ts = self.call_id[:16] if self.call_id else time.strftime("%Y%m%d_%H%M%S")
            self._callback_audio_var = f"{self._caller_number}_{ts}"
            callback_audio_path = Path("/app/shared") / f"callback_{self._callback_audio_var}.sln"
            callback_audio_path.parent.mkdir(parents=True, exist_ok=True)
            callback_audio_path.write_bytes(slin_data)
            self._callback_audio_name = callback_audio_path.name  # 传给 _callback() 用
            self._log(f"回拨音频已生成(合并合成): {callback_audio_path} ({len(slin_data)}字节)")
            return True
        except Exception as e:
            self._log(f"生成回拨音频失败: {e}")
            return False

    # ── 上下文持久化（回拨会话加载）──
    def _save_conversation_context(self):
        """超时回拨时把当前会话历史序列化到共享文件，供回拨会话加载。"""
        try:
            if self._conversation is None:
                return
            json_str = self._conversation.to_json()
            context_path = Path("/app/shared") / f"context_{self._caller_number}.json"
            context_path.write_text(json_str, encoding="utf-8")
            self._log(f"会话上下文已保存: {context_path} ({len(self._conversation.history)}条)")
        except Exception as e:
            self._log(f"保存上下文失败: {e}")

    def _load_conversation_context(self):
        """回拨会话启动时加载上一通会话的上下文。"""
        try:
            context_path = Path("/app/shared") / f"context_{self._caller_number}.json"
            if context_path.exists():
                from engine.conversation import ConversationHandler
                json_str = context_path.read_text(encoding="utf-8")
                self._conversation = ConversationHandler.from_json(self.config, json_str)
                self._log(f"会话上下文已加载: {len(self._conversation.history)}条历史")
            else:
                self._log("无上下文文件（首次回拨或无历史）")
        except Exception as e:
            self._log(f"加载上下文失败: {e}")

    # ── AMI 外呼回电 ──
    async def _callback(self, retry_on_busy=True):
        if not self._caller_number:
            self._log("未获取到主叫号码，无法回电")
            return

        try:
            from asterisk.manager import Manager
            mgr = Manager()
            mgr.connect(self.config["freepbx"]["host"],
                        self.config["freepbx"]["ami_port"])
            mgr.login(self.config["freepbx"]["ami_user"],
                      self.config["freepbx"]["ami_secret"])

            # 回拨：直接 Playback 回拨音频（不走 200 分机，避免 ATA 401 认证失败）。
            # 回拨音频 callback_<主叫号>_<timestamp>.sln 在共享目录（FreePBX custom sounds）。
            channel = f"PJSIP/{self._caller_number}"
            cid = self.config["freepbx"]["callback_caller_id"]
            audio_var = getattr(self, '_callback_audio_var', self._caller_number)
            audio_file = f"custom/callback_{audio_var}"

            action = {
                "Action": "Originate",
                "Channel": channel,
                "Application": "Playback",
                "Data": audio_file,
                "CallerID": cid,
                "Timeout": 30000,
            }
            r = mgr.send_action(action)
            resp = r.get_header("Response")
            msg = r.get_header("Message")
            self._log(f"[回电] Originate {channel} Playback={audio_file}: {resp} - {msg}")

            try:
                mgr.logoff()
            except Exception:
                pass

            # 占线重试（10 秒后重试一次）
            if retry_on_busy and resp == "Error" and msg and "busy" in msg.lower():
                self._log("[回电] 用户占线，10秒后重试...")
                await asyncio.sleep(10)
                await self._callback(retry_on_busy=False)

        except Exception as e:
            self._log(f"[回电] AMI 外呼失败: {e}")

    # ── 发送一帧音频 ──
    async def _send_frame(self, chunk: bytes):
        """发送一帧音频（TYPE_AUDIO）。连接断开时抛 ConnectionError。"""
        if not self._connected:
            raise ConnectionError("连接已断开")
        self.writer.write(TYPE_AUDIO + PCM_SIZE + chunk)
        await asyncio.wait_for(self.writer.drain(), timeout=0.3)

    # ── 读一帧（带应用层保活）──
    async def _read_frame_with_keepalive(self, step=0.5):
        """读一帧 AudioSocket 数据（323B: 1B type + 2B len + 320B slin16）。

        step 秒内无帧 → 发送一帧 SILENCE 保活（修复 Asterisk AudioSocket
        应用层 MAX_WAIT_TIMEOUT_MSEC=2000 硬超时导致的莫名挂断）。

        Returns:
          payload    : TYPE_AUDIO 帧的 320B 音频数据
          "HANGUP"   : 收到挂机/错误帧，或连接断开
          None       : step 秒内无帧（已发送保活静音帧）
        """
        while True:
            try:
                data = await asyncio.wait_for(
                    self.reader.readexactly(323), timeout=step)
            except asyncio.TimeoutError:
                # 应用层保活：发一帧静音，防 Asterisk 2s 硬超时断开
                try:
                    self.writer.write(TYPE_AUDIO + PCM_SIZE + SILENCE)
                    await self.writer.drain()
                except (BrokenPipeError, ConnectionResetError, ConnectionError):
                    self._connected = False
                    return "HANGUP"
                return None
            except (asyncio.IncompleteReadError, ConnectionError,
                    ConnectionResetError):
                self._connected = False
                return "HANGUP"

            kind = data[:1]
            payload = data[3:]
            if kind in (TYPE_HANGUP, TYPE_ERROR):
                return "HANGUP"
            if kind != TYPE_AUDIO:
                continue  # 其他类型帧，跳过继续读
            return payload

    # ── 播放期间读帧丢弃（应用层回声兜底）──
    async def _discard_upstream_frame(self) -> bool:
        """播放 TTS 期间读一帧用户上行帧并丢弃。

        AudioSocket 全双工：AI 播放 TTS/开场白时，Asterisk 仍持续把用户上行帧
        （含座机听筒漏出的 TTS 回声）转发给后端。若播放期间不读，积压帧会在
        播放结束后被 _read_user_speech 一次性读到 → TTS 回声被 ASR 识别成用户语音。
        每个播放方法在每发一帧 TTS 后调用本方法：
          - 5ms 超时无积压帧 → 返回 True（继续播放，无额外延迟感知）
          - 读到 HANGUP/ERROR 帧 → _connected=False，返回 False（调用方退出播放）
          - 其他类型帧（AUDIO 回声等）→ 直接丢弃，返回 True
        wait_for 超时会 cancel readexactly 协程，但 Py3.11 的 readexactly 在
        缓冲攒满 n 字节前不消费缓冲（仅 await），取消时已到达的字节留在内部
        缓冲，下次 readexactly 继续读，不会丢帧/错位。

        Returns:
          True  : 无积压帧或已丢弃（可继续播放）
          False : 连接断开/挂机（调用方应退出播放）
        """
        try:
            data = await asyncio.wait_for(
                self.reader.readexactly(323), timeout=0.005)
        except asyncio.TimeoutError:
            return True  # 无积压帧，继续播放
        except (asyncio.IncompleteReadError, ConnectionError,
                ConnectionResetError):
            self._connected = False
            return False
        if data[:1] in (TYPE_HANGUP, TYPE_ERROR):
            self._connected = False
            return False
        return True  # AUDIO 帧（回声/用户语音）→ 丢弃

    # ── 播放结束清尾音（应用层回声兜底 2）──
    async def _drain_upstream_tail(self, window_ms: int = 100):
        """播放结束后清空 TTS 尾音回声残帧。

        播放最后一帧 TTS 后，Asterisk 仍会把 ~100-300ms 的听筒回声上行帧
        转发过来。若不清理，这些残帧会在下一轮 _read_user_speech 开头被
        ASR 识别成用户语音（日志实证：AI 说"问别的问题"→ 下一轮识别出
        "一个问题"）。窗口默认 300ms（约 15 帧 @20ms），只清尾音不吞用户话头
        （用户听完 AI 说话有反应时间，300ms 内不会开口）。

        Returns:
          True  : 清理完成（可能已读到挂机帧 → _connected 已置 False）
        """
        deadline = time.time() + window_ms / 1000
        while time.time() < deadline:
            try:
                data = await asyncio.wait_for(
                    self.reader.readexactly(323), timeout=0.02)
            except asyncio.TimeoutError:
                # 20ms 无帧 → 尾音已清完，提前返回
                return True
            except (asyncio.IncompleteReadError, ConnectionError,
                    ConnectionResetError):
                self._connected = False
                return True
            if data[:1] in (TYPE_HANGUP, TYPE_ERROR):
                self._connected = False
                return True
            # AUDIO 残帧 → 丢弃，继续清
        return True

    # ── 播放 TTS ──
    async def _play_tts(self, text: str):
        tts = self._get_tts()
        self._is_speaking = True
        try:
            slin_data = await tts.synthesize_async(text)
        except Exception as e:
            self._log(f"TTS合成错误: {e}")
            self._is_speaking = False
            return
        try:
            frame_count = len(slin_data) // FRAME_SIZE
            for i in range(0, len(slin_data), FRAME_SIZE):
                chunk = slin_data[i:i + FRAME_SIZE]
                if len(chunk) < FRAME_SIZE:
                    chunk = chunk + b'\x00' * (FRAME_SIZE - len(chunk))
                await self._send_frame(chunk)
                await asyncio.sleep(0.02)
                # 播放期间读帧丢弃（应用层回声兜底）：读掉 Asterisk 转发的上行帧
                # （含 TTS 回声），防积压 → 播放结束被 _read_user_speech 识别成用户语音
                if not await self._discard_upstream_frame():
                    break
            self._log(f"TTS已播放: {frame_count}帧 ({frame_count * FRAME_MS}ms)")
        except Exception as e:
            self._log(f"TTS错误: {e}")
            if isinstance(e, (BrokenPipeError, ConnectionResetError, ConnectionError)):
                self._connected = False
        finally:
            await self._drain_upstream_tail()  # 清 TTS 尾音残帧，防下一轮 ASR 误录
            self._is_speaking = False

    # ── 预合成缓存音频 ──
    def _get_cached_audio(self, text: str):
        """返回预合成缓存音频 bytes（开场白/占位语/回电语/结束语/回拨开场）；未命中返回 None。"""
        # 查当前 provider 的缓存（_cache 字典由 load_cached_audio 填充）
        provider = self._get_current_tts_provider()
        if provider not in _cache:
            return None
        name_map = {
            GREETING_TEXT: "greeting",
            WAITING_TEXT: "waiting",
            CALLBACK_TEXT: "callback",
            FAREWELL_TEXT: "farewell",
            CALLBACK_INTRO_TEXT: "callback_intro",
            NO_SEARCH_RESULT_TEXT: "no_search_result",
            EMPTY_REPLY_TEXT: "empty_reply",
        }
        name = name_map.get(text)
        if name:
            return _cache[provider].get(name)
        return None

    def _get_current_tts_provider(self) -> str:
        """从 config 读取当前 TTS provider"""
        try:
            return self.config.get("tts", {}).get("provider", "matcha-zh-en-8k")
        except Exception:
            return "matcha-zh-en-8k"

    async def _play_cached(self, text: str, label: str = ""):
        """播放预合成缓存音频。label 非空时 log「播放{label}: {text}」，空则不 log（调用方自行 log）。"""
        audio = self._get_cached_audio(text)
        if audio is None:
            await self._play_tts(text)
            return
        if label:
            self._log(f"播放{label}: {text}")
        frame_count = len(audio) // FRAME_SIZE
        self._is_speaking = True
        for i in range(0, len(audio), FRAME_SIZE):
            chunk = audio[i:i + FRAME_SIZE]
            if len(chunk) < FRAME_SIZE:
                chunk = chunk + b'\x00' * (FRAME_SIZE - len(chunk))
            await self._send_frame(chunk)
            await asyncio.sleep(0.02)
            # 播放期间读帧丢弃（应用层回声兜底）：开场白/结束语回声不积压、不进 ASR
            if not await self._discard_upstream_frame():
                break
        await self._drain_upstream_tail()  # 清 TTS 尾音残帧，防下一轮 ASR 误录
        self._is_speaking = False

    # ── 分句标点 ──
    _SENTENCE_END = set("。！？；\n!?;.")   # 含英文句号 .（Bug D：英文句尾分句）
    _CLAUSE_END = set("，、")

    # ══════════════════════════════════════════════════════════════
    # 三协程流水线（plan v2 §2.3 / §4.2）
    #   _llm_receiver   : LLM token → Segment Builder 分句 → _tts_queue
    #   _tts_worker     : _tts_queue → RapidSpeech TTS 合成 → 320B 帧入 _pcm_fifo
    #   _playback_worker: 每 20ms 从 _pcm_fifo 取帧写 AudioSocket（空则静音帧）
    # 结束链: _tts_queue 收 None → worker 排空剩余段 → FIFO 收 None → 播放排空退出
    # ══════════════════════════════════════════════════════════════

    async def _llm_receiver(self, llm, messages) -> tuple:
        """协程1：接收 LLM 流式 token → Segment Builder 分句 → 推 TTS 队列。

        同时承载超时驱动（与旧 _handle_llm_response 语义一致）：
          - first_token_timeout（config llm.first_token_timeout，默认5s）无首字 → 播占位语
          - total_timeout（默认10s）仍无首字 → 播回电语 → timed_out=True → 后台收集回复供回拨
        分句规则（§4.2）：
          - 句尾（。！？；!?;\n）→ 分句
          - 逗号（，、）且缓冲 ≥15 字 → 分句
          - 无标点累计超过 15 字 → 强制分句
          - 3 秒无句尾 → 强制分句
        Returns: (reply_text, timed_out)
        """
        import threading
        loop = asyncio.get_event_loop()
        token_queue = asyncio.Queue()
        full_reply = []
        first_token_timeout = self.config["llm"].get("first_token_timeout", 5)
        total_timeout = self.config["llm"].get("total_timeout", 10)
        # 非流式 LLM 请求超时（回拨场景）：给 deepseek 足够时间完整生成。
        # 正常对话 total_timeout 先触发转回拨；回拨场景后台收集继续等此请求完成。
        # max_retries=0 后 timeout 精确生效。
        callback_timeout = self.config["llm"].get("callback_timeout", 60)
        stream_timeout = callback_timeout

        def _llm_producer():
            """子线程：非流式 LLM 请求。先判断是否需要联网搜索，
            需要则先搜索拼进 system prompt 再回答。"""
            try:
                # 1) 提取本轮用户文本
                user_text = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        user_text = m.get("content", "")
                        break

                # 2) 判断是否需联网搜索（MCP 垂直域路由）
                search_client = self._get_search()
                need, route = search_client.need_search(user_text)
                if need:
                    self._has_search = True
                    from engine.search import build_query
                    q = build_query(user_text, messages)

                    # MCP 垂直域路由：finance.quote / extract / general
                    ctx = None
                    _t_search = time.time()
                    self._log(f"[联网搜索] 关键词「{q}」({route})")
                    if route == "finance":
                        # 大盘/指数 → 区分 A股上证 vs 美股 ETF（finance.quote 不支持 IXIC/DJI 直接查询）
                        import re as _re4
                        if _re4.search(search_client.index_keywords, user_text, _re4.I):
                            # 美股指数 → ETF 替代
                            if _re4.search(r"标普|纳斯达克|纳指|道琼斯|道指|SPX|IXIC|DJI", user_text, _re4.I):
                                if _re4.search(r"纳斯达克|纳指|IXIC", user_text, _re4.I):
                                    ftype, fparam = "stock", "QQQ"
                                elif _re4.search(r"道琼斯|道指|DJI", user_text, _re4.I):
                                    ftype, fparam = "stock", "DIA"
                                else:
                                    ftype, fparam = "stock", "SPY"
                            else:
                                # A股大盘
                                ftype, fparam = "index", "000001.SH"
                        else:
                            ftype, fparam = search_client.lookup_stock(user_text)
                            if not ftype:
                                symbol, cn_code = self._extract_finance(user_text)
                                if symbol:
                                    ftype, fparam = "stock", symbol
                                elif cn_code:
                                    ftype, fparam = "stock", cn_code
                                else:
                                    ftype, fparam = None, None
                        ctx = search_client.search(q, route, finance_type=ftype, finance_param=fparam)
                    elif route == "weather":
                        # 天气直接走通用搜索：用户原话(q)发 anysearch general（实测 1-1.4s，返回含今/明/后天）
                        ctx = search_client.search(q, "general")
                        route = "general"
                    else:
                        ctx = search_client.search(q, route)

                    if ctx:
                        self._log(f"[联网搜索] 完成 耗时{time.time()-_t_search:.1f}s({len(ctx)}字符)")
                        _t_llm = time.time()
                        reply = llm.chat_with_search(messages, ctx, timeout=stream_timeout)
                        self._log(f"[LLM] 生成耗时{time.time()-_t_llm:.1f}s")
                        if not reply:
                            # LLM 返回空内容（如被内容过滤），播兜底话术
                            self._log("[LLM空回复] 搜索结果有内容但LLM拒绝回答，播兜底话术")
                            loop.call_soon_threadsafe(self._tts_queue.put_nowait, EMPTY_REPLY_TEXT)
                            reply = ""
                    else:
                        self._log("[联网搜索] 失败，播放兜底话术（预合成）")
                        # 搜索失败：直接播预合成兜底话术（整段命中缓存），不走 LLM + 不分句
                        loop.call_soon_threadsafe(self._tts_queue.put_nowait, NO_SEARCH_RESULT_TEXT)
                        reply = ""  # 跳过后续分句逻辑
                else:
                    _t_llm = time.time()
                    reply = llm.chat(messages, timeout=stream_timeout)
                    self._log(f"[LLM] 生成耗时{time.time()-_t_llm:.1f}s")
                    if not reply:
                        # LLM 返回空内容（无搜索路径），播兜底话术
                        self._log("[LLM空回复] 无搜索路径LLM返回空，播兜底话术")
                        loop.call_soon_threadsafe(self._tts_queue.put_nowait, EMPTY_REPLY_TEXT)
                        reply = ""

                if reply:
                    # 按句尾标点（保留标点）切分，让 _llm_receiver 的分句逻辑处理
                    import re
                    segments = re.split(r'(?<=[。！？；!?;\n])', reply)
                    for seg in segments:
                        s = seg.strip()
                        if s:
                            loop.call_soon_threadsafe(token_queue.put_nowait, s)
            except Exception as e:
                loop.call_soon_threadsafe(token_queue.put_nowait, ("__error__", str(e)))
            finally:
                loop.call_soon_threadsafe(token_queue.put_nowait, None)

        thread = threading.Thread(target=_llm_producer, daemon=True)
        thread.start()

        t0 = time.time()
        got_first_token = False
        timed_out = False
        placeholder_played = False
        sentence_buffer = ""
        last_flush_time = t0

        async def _flush_segment():
            nonlocal sentence_buffer, last_flush_time
            sentence = sentence_buffer.strip()
            sentence_buffer = ""
            if sentence:
                await self._tts_queue.put(sentence)
                last_flush_time = time.time()

        try:
            while True:
                elapsed = time.time() - t0

                # 首字超时 → 播占位语（走 TTS 队列，worker 命中缓存音频）
                if not got_first_token and not placeholder_played and elapsed > first_token_timeout:
                    self._log(f"首字超时({elapsed:.1f}s)，播放占位语: {WAITING_TEXT}")
                    await self._tts_queue.put(WAITING_TEXT)
                    placeholder_played = True

                # 总超时 → 播回电语 → 转回拨
                if not got_first_token and elapsed > total_timeout:
                    self._log(f"总超时({elapsed:.1f}s)，LLM无回复，转回拨")
                    timed_out = True
                    await self._tts_queue.put(CALLBACK_TEXT)
                    break

                # 100ms 轮询 token
                try:
                    token = await asyncio.wait_for(token_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    # 3 秒无句尾 → 强制分句（§4.2）
                    if sentence_buffer.strip() and (time.time() - last_flush_time) > SEGMENT_FLUSH_TIMEOUT:
                        self._log(f"3秒无句尾，强制分句: {sentence_buffer.strip()[:20]}")
                        await _flush_segment()
                    continue

                if token is None:
                    # LLM 结束
                    break
                if isinstance(token, tuple) and token[0] == "__error__":
                    self._log(f"LLM流式错误: {token[1]}")
                    if not got_first_token:
                        await self._tts_queue.put(NO_SEARCH_RESULT_TEXT)
                    break

                if not got_first_token:
                    got_first_token = True
                    self._llm_first_token_ms = (time.time() - t0) * 1000

                full_reply.append(token)
                sentence_buffer += token

                # ── Segment Builder 分句 ──
                if any(ch in self._SENTENCE_END for ch in token):
                    await _flush_segment()
                elif any(ch in self._CLAUSE_END for ch in token) and len(sentence_buffer) >= SEGMENT_COMMA_MIN:
                    await _flush_segment()
                elif ' ' in token and not _is_english_segment(sentence_buffer):
                    # Bug D：中文中的空格（LLM 用空格替代标点）→ 当句尾分句
                    await _flush_segment()
                elif len(sentence_buffer) > SEGMENT_HARD_MAX:
                    # Bug D：英文段不硬切（等英文句尾标点 .!? 自然分句），中文段仍硬切
                    if not _is_english_segment(sentence_buffer):
                        await _flush_segment()
        finally:
            # 剩余 buffer + 结束标记（保证 TTS worker 一定能收尾）
            if not timed_out and sentence_buffer.strip():
                await _flush_segment()
            await self._tts_queue.put(None)

        if timed_out:
            # Bug修复：回拨语已播完，LLM回复收集移入后台协程不阻塞挂机
            self._callback_pending = True
            self._log("启动后台收集 LLM 回复供回拨...")
            async def _collect_callback_reply():
                try:
                    # 第一轮：从 token_queue 收集 LLM 回复（等非流式 _llm_producer 完整返回）
                    while True:
                        try:
                            token = await asyncio.wait_for(token_queue.get(), timeout=callback_timeout + 5)
                        except asyncio.TimeoutError:
                            self._log(f"回拨回复收集超时({callback_timeout + 5}s)")
                            break
                        if token is None:
                            break
                        if isinstance(token, tuple) and token[0] == "__error__":
                            break
                        full_reply.append(token)
                    reply = "".join(full_reply).strip()

                    # 重试：如果收集为空，用非流式 chat 重新调 LLM（大阈值，等完整回复）
                    if not reply:
                        self._log("LLM 回复为空，重试一次（非流式，大阈值）...")
                        loop = asyncio.get_event_loop()
                        try:
                            reply = await loop.run_in_executor(
                                None, lambda: llm.chat(messages, timeout=callback_timeout)
                            )
                            self._log(f"LLM 重试结果: {reply[:80] or '(仍为空)'}")
                        except Exception as e:
                            self._log(f"LLM 重试异常: {e}")

                    self._callback_reply = reply
                    self._log(f"回拨回复收集完成: {reply[:80] or '(空)'}")
                except Exception as e:
                    self._log(f"回拨回复收集异常: {e}")
            self._bg_collect_task = asyncio.create_task(_collect_callback_reply())
            self._llm_total_ms = (time.time() - t0) * 1000
            return "", True  # 立即返回空 reply，不阻塞挂机

        reply = "".join(full_reply).strip()
        self._llm_total_ms = (time.time() - t0) * 1000
        return reply, False

    async def _tts_worker(self):
        """协程2：TTS 合成 worker —— 从队列取 segment，RapidSpeech 合成后切 320B 帧入 FIFO。

        合成期间每 500ms 发一帧静音帧（§6.2 兜底，防 Asterisk 2s 硬限制断连）。
        已知缓存文本（占位语/回电语等）直接入 FIFO，不重复合成。
        """
        _tts_t0 = time.time()
        while self._connected:
            segment = await self._tts_queue.get()
            if segment is None:
                self._pcm_fifo.append(None)  # 结束标记
                break
            cached = self._get_cached_audio(segment)
            if cached is not None:
                self._push_slin_frames(cached)
                continue
            try:
                slin_data = await self._synthesize_with_keepalive(segment)
            except (BrokenPipeError, ConnectionResetError, ConnectionError):
                self._connected = False
                break
            except Exception as e:
                self._log(f"TTS合成错误: {e}")
                continue
            self._push_slin_frames(slin_data)
        self._tts_total_ms = (time.time() - _tts_t0) * 1000

    async def _synthesize_with_keepalive(self, segment: str) -> bytes:
        """调 RapidSpeech TTS 合成；合成期间每 500ms 发一帧静音帧保持连接（§6.2）。

        最长 TTS_SYNTH_TIMEOUT(60s) 兜底。
        """
        self._log(f"[TTS合成中] {segment}")
        tts = self._get_tts()
        synth_task = asyncio.create_task(tts.synthesize_async(segment))
        deadline = time.time() + TTS_SYNTH_TIMEOUT
        last_beat = time.time()
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._log(f"TTS合成超时({TTS_SYNTH_TIMEOUT:.0f}s): {segment[:20]}")
                    synth_task.cancel()
                    return b''
                done, _ = await asyncio.wait(
                    {synth_task}, timeout=min(TTS_BEAT_INTERVAL, remaining))
                if done:
                    return synth_task.result()
                # 合成仍在进行：每 500ms 发一帧静音帧
                if time.time() - last_beat >= TTS_BEAT_INTERVAL:
                    try:
                        await self._send_frame(SILENCE)
                    except (BrokenPipeError, ConnectionResetError, ConnectionError):
                        self._connected = False
                        raise
                    last_beat = time.time()
        finally:
            if not synth_task.done():
                synth_task.cancel()

    def _push_slin_frames(self, slin_data: bytes):
        """把 slin16 数据切成 320B 帧推入 FIFO（不足 320 字节补零）。"""
        for i in range(0, len(slin_data), FRAME_SIZE):
            chunk = slin_data[i:i + FRAME_SIZE]
            if len(chunk) < FRAME_SIZE:
                chunk = chunk + b'\x00' * (FRAME_SIZE - len(chunk))
            self._pcm_fifo.append(chunk)

    async def _playback_worker(self):
        """协程3：匀速播放 —— 每 20ms 从 FIFO 取一帧写入 AudioSocket。

        FIFO 空时发静音帧（保持连接 + 等待下一段 TTS 合成完毕），播放恒定无缝隙。
        """
        while self._connected:
            # TODO(§2.4 Barge-in 打断)：完整实现需在播放期间并行读用户帧做 VAD 检测，
            # 检测到说话 → self._interrupted=True → 清 FIFO → 切 LISTENING。
            # Demo 阶段未实现（固话天然全双工，可后续加），仅保留标志位与清空逻辑。
            if self._interrupted:
                self._pcm_fifo.clear()
                self._log("打断：清空TTS FIFO")
                break
            if self._pcm_fifo:
                chunk = self._pcm_fifo.popleft()
                if chunk is None:  # 结束标记
                    break
            else:
                chunk = SILENCE  # FIFO 空 → 静音帧（保活 + 无缝衔接下一段）
            # 首次写非静音帧 → 记首包出音时刻（ttft 终点）
            if self._first_audio_ts == 0.0 and chunk != SILENCE:
                self._first_audio_ts = time.time()
            try:
                await self._send_frame(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionError):
                self._connected = False
                break
            except Exception:
                break
            # 播放期间读帧丢弃（应用层回声兜底）：FIFO 播放帧与静音保活帧分支都读，
            # 上行回声帧（含 TTS 回声）不积压、不进 ASR
            if not await self._discard_upstream_frame():
                break
            await asyncio.sleep(PLAYBACK_INTERVAL)
        await self._drain_upstream_tail()  # 清 TTS 尾音残帧，防下一轮 ASR 误录

    async def _run_llm_tts_pipeline(self, llm, messages) -> tuple:
        """启动三协程流水线（§4.2），返回 (reply, timed_out)。

        自然收尾链：_llm_receiver 结束 → _tts_queue 收 None → _tts_worker 排空剩余段
        → _pcm_fifo 收 None → _playback_worker 排空播放 → 三协程全部退出。
        """
        self._tts_queue = asyncio.Queue()
        self._pcm_fifo = collections.deque(maxlen=PCM_FIFO_MAXLEN)
        self._interrupted = False
        self._is_speaking = True
        # 每轮重置时延采集字段（call_history 用）
        self._first_audio_ts = 0.0
        self._llm_first_token_ms = 0.0
        self._llm_total_ms = 0.0
        self._tts_total_ms = 0.0

        llm_task = asyncio.create_task(self._llm_receiver(llm, messages))
        tts_task = asyncio.create_task(self._tts_worker())
        pb_task = asyncio.create_task(self._playback_worker())
        try:
            reply, timed_out = await llm_task
        finally:
            self._is_speaking = False
            # 等 TTS/播放协程自然收尾（llm_receiver 的 finally 已保证放入 None 哨兵）
            await asyncio.gather(tts_task, pb_task, return_exceptions=True)
        return reply, timed_out

    # ── VAD 自适应阈值（plan v2 §3.2 问题1）──
    async def _calibrate_vad(self, sample_frames: int = 30):
        """采样前 N 帧静音基准，阈值 = median×2 + 300（下限 200）。

        开场白播放后、进入监听前调用。若检测到用户已开口（能量>800）则提前结束，
        不吞用户语音帧（剩余帧留给 _read_user_speech 读取）。
        """
        if not self._adapt_threshold:
            self._log(f"VAD 使用固定阈值 {self._vad_threshold:.0f}（adapt_threshold=false）")
            return
        energies = []
        for _ in range(sample_frames):
            try:
                data = await self.reader.readexactly(323)
            except (asyncio.IncompleteReadError, ConnectionError, ConnectionResetError):
                self._connected = False
                return
            kind, payload = data[:1], data[3:]
            if kind in (TYPE_HANGUP, TYPE_ERROR):
                return
            if kind != TYPE_AUDIO:
                continue
            energy = float(np.abs(np.frombuffer(payload, dtype=np.int16)).mean())
            if energy > 800:  # 用户已开始说话 → 提前结束校准
                self._log(f"VAD校准：检测到语音(能量{energy:.0f})，提前结束")
                break
            energies.append(energy)
        if len(energies) >= 10:
            median = float(np.median(energies))
            new_threshold = max(200.0, median * 2 + 300)
            self._vad_threshold = new_threshold
            self._log(f"VAD自适应阈值: 基准median={median:.0f} → 阈值={new_threshold:.0f}")
        else:
            self._vad_threshold = self._vad_base_threshold
            self._log(f"VAD校准样本不足({len(energies)}帧)，回退固定阈值 {self._vad_threshold:.0f}")

    # ── 读取用户语音（带起始超时）──
    async def _read_user_speech(self, start_timeout=None):
        """读取用户语音。按 ASR 引擎类型分流（is_streaming）：

          - 流式引擎（xasr/zipformer）→ _read_user_speech_streaming：
            ASR endpoint 检测判\"说完了\"（替代旧能量 VAD 判完逻辑）
          - 离线引擎（sensevoice/telespeech）→ _read_user_speech_offline：
            能量 VAD 收集完整句音频 + 尾部静音判完 → transcribe 一次性转写

        返回（两路径契约一致）:
          (text, audio_bytes): 识别出文本 → (识别文本, 原始8k音频字节)
          False: start_timeout 内用户一直没说话
          None: 用户挂机/连接断开

        保活：等待期间每 0.5s 无帧自动发 SILENCE（_read_frame_with_keepalive 处理），
        修复 Asterisk AudioSocket 应用层 2s 硬超时断连。

        时延采集（call_history 用）：识别成功时记录 self._last_asr_ms 和
        self._turn_start_ts（ASR endpoint 时刻，ttft 起点）。
        """
        _t0 = time.time()
        asr = self._get_asr()
        if not asr.is_streaming:
            result = await self._read_user_speech_offline(start_timeout)
        else:
            result = await self._read_user_speech_streaming(start_timeout)
        # 识别成功（返回 tuple）时记录 ASR 耗时 + endpoint 时刻
        if isinstance(result, tuple) and len(result) >= 2:
            self._last_asr_ms = (time.time() - _t0) * 1000
            self._turn_start_ts = time.time()
        return result

    # ── 流式 ASR 路径（xasr/zipformer）：endpoint 检测判完 ──
    async def _read_user_speech_streaming(self, start_timeout=None):
        """流式 ASR 读取用户语音：攒批重采样 feed → endpoint → 取结果。"""
        asr = self._get_asr()
        stream = asr.create_stream()

        audio_buffer = bytearray()       # 原始 8k 音频（返回给上层）
        resample_buffer = bytearray()    # 8k→16k 重采样积累（每帧 160 样本=320B）
        has_speech = False               # 能量 VAD 仅用于"是否开始说话"（超时判定），不用于判完
        frame_count = 0
        fed_once = False
        _wait_start = time.time()
        _abs_start = time.time()          # 绝对超时基准（永不重置），防残留帧反复误判无限延长等待

        def _flush_resample(force=False):
            """把积累的 8k 样本批量重采样为 16k 并 feed ASR。

            攒满 800 样本（100ms）批量处理；force=True 时残余样本也喂（尾部语音不丢）。
            """
            nonlocal resample_buffer, fed_once
            if not resample_buffer:
                return
            if not force and len(resample_buffer) < 800 * 2:  # 800样本×2B=1600B=100ms
                return
            samples_8k = (
                np.frombuffer(bytes(resample_buffer), dtype=np.int16)
                .astype(np.float32) / 32768.0
            )
            resample_buffer = bytearray()
            samples_16k = _resample_8k_to_16k(samples_8k)
            asr.feed(stream, samples_16k)
            fed_once = True

        while True:
            # 绝对超时兜底：无论 has_speech 如何误判，总等待不超过 start_timeout + 10s
            if start_timeout is not None and (time.time() - _abs_start) >= start_timeout + 10:
                return False
            # 未说话超时（start_timeout 内用户一直没开口 → False）
            if not has_speech and start_timeout is not None:
                elapsed = time.time() - _wait_start
                if elapsed >= start_timeout:
                    return False

            # 说话总时长保护（防长句失控，max_speech 默认 50s → 强制截断返回当前结果）
            if has_speech and frame_count > self._max_speech_frames:
                self._log(f"达到最大说话时长({self._max_speech_frames}帧)，强制截断")
                _flush_resample(force=True)
                text = asr.get_result_with_tail_silence(stream)
                return (text, bytes(audio_buffer))

            data = await self._read_frame_with_keepalive(0.5)
            if data == "HANGUP":
                return None
            if data is None:
                # 0.5s 无帧（保活帧已发）：喂残余样本后再查 endpoint（语音尾部不丢）
                _flush_resample(force=True)
                if fed_once and asr.is_endpoint(stream):
                    # endpoint 触发后补静音再取结果：X-ASR 960ms chunk 的尾部
                    # token 可能未解码完（如"功能"只出"功"），补 400ms 静音驱动
                    t_ep = time.time()
                    text = asr.get_result_with_tail_silence(stream)
                    t_res = time.time()

                    if text:
                        return (text, bytes(audio_buffer))
                    # 空文本（纯静音误判）→ 清状态继续等
                    asr.reset(stream)
                    has_speech = False
                    audio_buffer.clear()
                    resample_buffer.clear()
                    frame_count = 0
                    _wait_start = time.time()
                continue

            payload = data
            samples = np.frombuffer(payload, dtype=np.int16)
            energy = np.abs(samples).mean()

            # 能量 VAD 仅标记"用户是否开始说话"（超时判定用），不再作为"说完了"依据
            if energy > self._vad_threshold:
                has_speech = True

            audio_buffer.extend(payload)
            resample_buffer.extend(payload)
            frame_count += 1

            # 攒满 100ms 批量重采样 feed
            _flush_resample(force=False)
            if not resample_buffer and fed_once:
                # 每 100ms 批量 feed 后检查 endpoint
                if asr.is_endpoint(stream):
                    # Bug F：常规 endpoint 路径也补尾静音取结果（原 get_result 漏尾部轻声字）
                    t_ep = time.time()
                    text = asr.get_result_with_tail_silence(stream)
                    t_res = time.time()

                    if text:
                        return (text, bytes(audio_buffer))
                    # 空文本（纯静音/未识别）→ 清状态继续等
                    asr.reset(stream)
                    has_speech = False
                    audio_buffer.clear()
                    resample_buffer.clear()
                    frame_count = 0
                    _wait_start = time.time()

        # 理论不可达（循环内必然 return / 截断返回）
        return False

    # ── 离线 ASR 路径（sensevoice/telespeech）：TenVAD VAD（如配置）或能量 VAD ──
    async def _read_user_speech_offline(self, start_timeout=None):
        """离线 ASR 读取用户语音。

        VAD 引擎由 config.yaml vad.vad_engine 决定（tenvad / energy）：
          - tenvad: ONNX Silero 风格 VAD，在 16k 域运行。喂入 8k→16k 重采样后的
            音频，每 256 个 16k 样本输出一帧概率。VAD 索引在 16k 域，切回 8k 字节
            时除以 2。
          - energy: 原有能量 VAD（int16 绝对值均值阈值），8k 域逐帧判断。

        通用契约：
          (text, audio_bytes) | False（超时未开口）| None（挂机/断开）
        """
        asr = self._get_asr()
        audio_buffer = bytearray()
        has_speech = False
        silence_frames = 0
        frame_count = 0
        _wait_start = time.time()

        # TenVAD 使用 16k 域重采样
        use_tenvad = self._vad_ten is not None
        if use_tenvad:
            self._vad_ten.reset_state()
            self._vad_resample_buf.clear()
            vad_silence_16k = 0
            vad_speech_16k = 0

        while True:
            # 未说话超时（start_timeout 内用户一直没开口 → False）
            if not has_speech and start_timeout is not None:
                if time.time() - _wait_start >= start_timeout:
                    return False

            # 说话总时长保护
            if has_speech and frame_count > self._max_speech_frames:
                self._log(f"达到最大说话时长({self._max_speech_frames}帧)，强制截断")
                t_ep = time.time()
                text = asr.transcribe(bytes(audio_buffer), 8000)
                t_res = time.time()

                return (text, bytes(audio_buffer))

            data = await self._read_frame_with_keepalive(0.5)
            if data == "HANGUP":
                return None
            if data is None:
                continue  # 0.5s 无帧（保活帧已发），等下一帧

            samples_8k = np.frombuffer(data, dtype=np.int16)  # 160 samples

            if use_tenvad:
                # ── TenVAD 路径（16k 域）──
                # 8k int16 → 16k float32 重采样
                samples_8k_f32 = samples_8k.astype(np.float32) / 32768.0
                samples_16k_f32 = _resample_8k_to_16k(samples_8k_f32)

                # 分批喂入 16k 缓冲（每次攒够 256 个 16k 样本跑一次 VAD）
                self._vad_resample_buf.extend(samples_16k_f32.tobytes())

                VAD_HOP = 256  # 16ms @ 16kHz
                while len(self._vad_resample_buf) >= VAD_HOP * 4:  # 4 bytes per float32
                    chunk_16k = np.frombuffer(
                        bytes(self._vad_resample_buf[:VAD_HOP * 4]),
                        dtype=np.float32
                    )
                    self._vad_resample_buf = self._vad_resample_buf[VAD_HOP * 4:]

                    prob = self._vad_ten.process_frame(chunk_16k)

                    is_active = prob > self._vad_threshold_tenvad
                    if is_active:
                        vad_speech_16k += 1
                        vad_silence_16k = 0
                        if not has_speech and vad_speech_16k >= self._vad_min_speech_frames_16k:
                            has_speech = True
                    elif has_speech:
                        vad_silence_16k += 1

                    # 尾部静音达到阈值（16k VAD 帧）→ 判"说完了"
                    if has_speech and vad_silence_16k >= self._vad_silence_frames_16k:
                        t_ep = time.time()
                        text = asr.transcribe(bytes(audio_buffer), 8000)
                        t_res = time.time()

                        if text:
                            return (text, bytes(audio_buffer))
                        # 空结果 → 清状态继续等
                        has_speech = False
                        vad_speech_16k = 0
                        vad_silence_16k = 0
                        audio_buffer.clear()
                        frame_count = 0
                        _wait_start = time.time()
                        self._vad_ten.reset_state()

            else:
                # ── 能量 VAD 路径（8k 域，原始逻辑）──
                energy = float(np.abs(samples_8k).mean())

                if energy > self._vad_threshold:
                    has_speech = True
                    silence_frames = 0
                elif has_speech:
                    silence_frames += 1

            # 始终累加原始 8k 音频
            audio_buffer.extend(data)
            frame_count += 1

            # 能量 VAD 尾部静音判定（仅 energy 模式）
            if not use_tenvad and has_speech and silence_frames >= self._silence_timeout_frames:
                t_ep = time.time()
                text = asr.transcribe(bytes(audio_buffer), 8000)
                t_res = time.time()

                if text:
                    return (text, bytes(audio_buffer))
                has_speech = False
                silence_frames = 0
                audio_buffer.clear()
                frame_count = 0
                _wait_start = time.time()

    # ── 主循环 ──
    def _warm_llama(self):
        """会话开始后台预热 llama-server：max_tokens=1 最小请求，吃掉冷槽重建 ~1.4s 开销。

        在开场白播放 + 用户开口期间并行完成，用户提问时走暖槽。失败静默，不影响通话。
        """
        try:
            if self.config.get("llm", {}).get("provider") != "local_llama":
                return
            cfg = self.config.get("llm", {}).get("local_llama", {})
            base = cfg.get("api_base", "http://host.docker.internal:8081/v1")
            model = cfg.get("model", "unsloth/Qwen3.5-4B-MTP-GGUF")
            import requests as _req
            _req.post(base + "/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "stream": False,
            }, timeout=15)
        except Exception as e:
            logger.warning(f"[WARM] llama-server 预热失败(忽略): {e}")

    async def run(self):
        self._log(f"AudioSocket 会话开始: {self.peer}")
        self._start_ts = time.time()

        # 会话开始后台预热 llama-server（开场白播放/用户开口期间完成，用户无感知）
        try:
            import threading
            threading.Thread(target=self._warm_llama, daemon=True).start()
        except Exception:
            pass

        try:
            # 读 UUID 帧
            header = await self.reader.readexactly(3)
            kind = header[:1]
            length = int.from_bytes(header[1:3], 'big')
            payload = await self.reader.readexactly(length) if length > 0 else b''
            if kind == TYPE_UUID:
                self.call_id = payload.hex()
                self._log(f"Call UUID: {self.call_id}")

            # 获取主叫号码 + 回拨模式
            self._caller_number, self._is_callback = await self._get_caller_info()

            # TCP 内核保活（plan v2 §2.1）：不再发应用层静音帧，只依赖内核探活
            sock = self.writer.get_extra_info('socket')
            if sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)  # 60s（原1s太激进）
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._log("TCP内核保活已启用 (KEEPIDLE=60s)")

            from engine.conversation import ConversationHandler
            self._conversation = ConversationHandler(self.config)

            if self._is_callback:
                # 回拨模式：跳过开场白（回拨音频已由 dialplan Playback 播放）
                # 但加载上下文：定时任务/超时回拨的播报内容存 context_{caller}.json，供追问 + 详情显示
                self._log("回拨模式：跳过开场白，直接进入对话")
                self._load_conversation_context()
            else:
                # 正常模式：播放开场白
                await self._play_cached(GREETING_TEXT, label="开场白")

            # 能量 VAD 不再是"说完了"判据（改用 ASR endpoint 检测），
            # 不再调用 _calibrate_vad（避免吞帧 + 阈值自适应无意义）

            # ── 等用户开口，最多 5 秒 ──
            user_audio = await self._read_user_speech(start_timeout=5.0)
            if user_audio is None:
                self._log("用户挂机")
                return
            if user_audio is False:
                self._log(f"用户5秒未说话，播放结束语: {FAREWELL_TEXT}")
                self._outcome = "greeting_timeout"
                await self._play_cached(FAREWELL_TEXT, label=None)
                return
            user_text, user_audio_bytes = user_audio

            # ── 对话循环 ──
            turn = 0
            reply = ""
            max_turns = self.config["llm"]["max_turns"]

            while turn < max_turns:
                # user_audio: (text, audio_bytes) | False | None（流式 ASR 已识别，无需再 transcribe）
                if user_audio is None:
                    self._log("用户挂机")
                    return
                if user_audio is False:
                    self._log(f"用户5秒未说话，播放结束语: {FAREWELL_TEXT}")
                    self._outcome = "idle_timeout"
                    await self._play_cached(FAREWELL_TEXT, label=None)
                    return
                user_text, user_audio_bytes = user_audio

                # 无有效语音（噪声/空转/截断空文本）→ 重置计时继续等（修复：被过滤语音不加速超时）
                if not user_text.strip() or len(user_audio_bytes) < self._min_speech_bytes:
                    self._log(
                        f"无有效语音(音频{len(user_audio_bytes)}B, 文本'{user_text.strip()[:10]}')，重置计时继续等"
                    )
                    self._idle_since = time.time()
                    user_audio = await self._read_user_speech(start_timeout=5.0)
                    continue

                # 道别检测：用户说再见/拜拜等 → 直接播放结束语挂机，不回复
                # ⚠ 必须在 ≤3字过滤之前！"再见"只有2个字，先被语气词过滤就永远检测不到道别
                _farewell_words = {"再见", "拜拜", "bye bye", "goodbye", "byebye", "bye"}
                if user_text.strip().lower() in _farewell_words or any(w in user_text.strip().lower() for w in ["再见", "拜拜", "bye"]):
                    self._log(f"检测到道别: '{user_text.strip()}'，播放结束语: {FAREWELL_TEXT}")
                    self._farewell = True
                    self._outcome = "farewell"
                    await self._play_cached(FAREWELL_TEXT, label=None)
                    return

                # 语气词过滤：字符数≤3（如"诶/哦/好"）不送 LLM，继续听（重置计时）
                if len(user_text.strip()) <= 3:
                    self._log(f"语气词过滤(≤3字): '{user_text.strip()}'，继续听")
                    self._idle_since = time.time()
                    await self._drain_upstream_tail()  # 清残留帧，防下轮 ASR 误判 has_speech 跳过超时
                    user_audio = await self._read_user_speech(start_timeout=5.0)
                    continue

                turn += 1
                self._log(f"第{turn}轮 [用户] {user_text}")

                # LLM 超时驱动管道 → 三协程流水线（plan v2 §4.2）
                try:
                    llm = self._get_llm()
                    t0 = time.time()
                    reply, timed_out = await self._run_llm_tts_pipeline(
                        llm, self._conversation.get_messages(user_text)
                    )

                except Exception as e:
                    self._log(f"LLM/TTS错误: {e}")
                    reply = NO_SEARCH_RESULT_TEXT
                    await self._play_cached(reply)
                    timed_out = False

                self._log(f"[AI] {reply[:80]}")
                self._conversation.add_turn(user_text, reply)

                # 组装本轮 turn dict（call_history transcript 用）
                # ttft_ms = 说完(endpoint) → 首包出音；turn_ms = 说完 → 合成完成
                _ttft_ms = ((self._first_audio_ts - self._turn_start_ts) * 1000
                            if (self._first_audio_ts and self._turn_start_ts) else 0.0)
                _turn_ms = (self._last_asr_ms + self._llm_total_ms + self._tts_total_ms)
                self._turn_metrics.append({
                    "turn": turn,
                    "user_text": user_text,
                    "asr_ms": round(self._last_asr_ms, 1),
                    "ai_text": reply,
                    "llm_first_token_ms": round(self._llm_first_token_ms, 1),
                    "llm_total_ms": round(self._llm_total_ms, 1),
                    "tts_ms": round(self._tts_total_ms, 1),
                    "ttft_ms": round(_ttft_ms, 1),
                    "turn_ms": round(_turn_ms, 1),
                    "searched": self._has_search,
                    "search_domain": "",
                })

                # 超时触发回拨 → 回拨语已播，立即挂机（音频后台生成，上下文在收集完成后保存）
                if timed_out:
                    self._log("超时回拨：回拨语已播，立即挂机（音频+回拨在会话结束后处理）")
                    self._outcome = "timed_out"
                    return

                # AI 回复含道别词 → TTS 已播完，等 1 秒自动挂机（不等待用户下一句）
                _farewell_kw = ("再见", "拜拜", "bye bye", "goodbye", "byebye", "bye")
                if any(w in reply.lower() for w in _farewell_kw):
                    self._log(f"AI 回复含道别词，1秒后自动挂机")
                    self._outcome = "farewell"
                    await asyncio.sleep(1)
                    return

                # 等用户下一句话（AI 说完重置计时，完整 5 秒）
                self._idle_since = time.time()
                user_audio = await self._read_user_speech(start_timeout=5.0)
                if user_audio is None:
                    self._log("用户挂机")
                    return
                if user_audio is False:
                    self._log(f"用户5秒未说话，播放结束语: {FAREWELL_TEXT}")
                    self._outcome = "idle_timeout"
                    await self._play_cached(FAREWELL_TEXT, label=None)
                    return

            # 达到最大轮次
            self._outcome = "max_turns"
            await self._play_cached(FAREWELL_TEXT, label="结束语")

        except asyncio.IncompleteReadError:
            self._log("连接断开")
            self._connected = False
        except ConnectionError:
            self._log("连接已断开")
            self._connected = False
        except Exception as e:
            self._log(f"异常: {e}")
            self._connected = False
            self._outcome = "error"
            import traceback
            self._log(traceback.format_exc()[-200:])
        finally:
            self.writer.close()
            self._log("会话结束")
            # 记录通话记录 + 聚合指标（结束后写 SQLite call_records）
            try:
                from engine.call_history import insert_record
                asr_provider = self.config.get("asr", {}).get("provider", "")
                tts_provider = self.config.get("tts", {}).get("provider", "")
                # llm 模型名取实际生效值：local_llama 模式走子段（顶层 model 可能是旧引擎残留死值）
                _llm_cfg = self.config.get("llm", {})
                if _llm_cfg.get("provider") == "local_llama":
                    llm_model = _llm_cfg.get("local_llama", {}).get("model", "")
                else:
                    llm_model = _llm_cfg.get("model", "")
                vad_engine = self.config.get("vad", {}).get("vad_engine", "")
                # 回拨/定时任务场景：从 conversation.history 补 LLM 播报内容（即使有追问也补）
                turn_metrics = self._turn_metrics
                if self._is_callback and self._conversation and getattr(self._conversation, "history", None):
                    for _m in self._conversation.history:
                        if _m.get("role") == "assistant" and _m.get("content"):
                            # 插入到 turn_metrics 最前面作为系统播报
                            _sys_turn = {
                                "turn": 0,
                                "user_text": "",
                                "ai_text": _m["content"],
                                "searched": False,
                            }
                            turn_metrics = [_sys_turn] + (turn_metrics or [])
                            break
                # 回拨/定时任务场景：caller=系统(callback_caller_id)，callee=101(实际接听方)
                # 正常场景：caller=101，callee=AI助手
                if self._is_callback:
                    _caller = self.config.get("freepbx", {}).get("callback_caller_id", "AI助手")
                    _callee = self._caller_number or ""
                else:
                    _caller = self._caller_number or ""
                    _callee = self.config.get("freepbx", {}).get("callee_number", "AI助手")
                insert_record(
                    call_id=self.call_id or "",
                    caller=_caller,
                    callee=_callee,
                    is_callback=1 if self._is_callback else 0,
                    start_ts=self._start_ts,
                    end_ts=time.time(),
                    asr=asr_provider,
                    tts=tts_provider,
                    llm=llm_model,
                    vad=vad_engine,
                    outcome=self._outcome,
                    has_search=1 if self._has_search else 0,
                    search_domains="",  # 预留，Phase 2 未采集
                    turn_metrics=turn_metrics,
                )
            except Exception as e:
                self._log(f"[CALLHISTORY] 记录通话异常: {e}")


# 在类上补方法（引擎单例统一走 registry，避免 from main 加载第二个模块实例）
# Bug A 修复：registry 空时优先用入口模块（__main__）注入的工厂函数，
# 而非 `from main import`（会加载第二个 main 模块实例，其 config 是旧快照）。
def _get_asr(self):
    from engine.registry import get_engine
    engine = get_engine("asr")
    if engine is None:
        fn = getattr(self, "_get_asr_fn", None)
        if fn is not None:
            engine = fn()
        else:
            from main import get_asr
            engine = get_asr()
    return engine
def _get_llm(self):
    from engine.registry import get_engine
    engine = get_engine("llm")
    if engine is None:
        fn = getattr(self, "_get_llm_fn", None)
        if fn is not None:
            engine = fn()
        else:
            from main import get_llm
            engine = get_llm()
    return engine
def _get_search(self):
    from engine.registry import get_engine
    engine = get_engine("search")
    if engine is None:
        from main import get_search
        engine = get_search()
    return engine
def _extract_finance(self, user_text: str):
    """用 LLM 提取股票标的。返回 (symbol, cn_code)。

    美股（英伟达/苹果/特斯拉）→ symbol=NVDA/AAPL/TSLA；
    A股（中国电信/贵州茅台/工商银行）→ cn_code=601728.SH/...。
    失败返回 ("", "")。
    """
    try:
        llm = self._get_llm()
        prompt = (
            "从用户输入提取股票标的，返回 JSON：\n"
            '{"symbol": "美股代码", "cn_code": "A股代码"}\n'
            "- 美股（英伟达/苹果/特斯拉）→ symbol=NVDA/AAPL/TSLA\n"
            "- A股（中国电信/贵州茅台/工商银行）→ cn_code=601728.SH/600519.SH/601398.SH\n"
            "- 不确定 → 两个都空字符串\n"
            "只输出 JSON。严禁输出'空/无/none/未知'等占位符。"
        )
        result = llm.chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_text},
        ], timeout=5)
        result = (result or "").strip()
        if not result:
            return "", ""
        import re as _re5
        import json as _json5
        m = _re5.search(r"\{[^{}]*\}", result)
        if m:
            try:
                data = _json5.loads(m.group())
                return data.get("symbol") or "", data.get("cn_code") or ""
            except Exception:
                pass
        # 非 JSON 兜底：裸代码（"NVDA" / "601728.SH"）
        if "{" not in result and "}" not in result:
            if _re5.search(r"\.(SH|SZ|BJ)$", result, _re5.I):
                return "", result
            if _re5.fullmatch(r"[A-Z]{1,5}", result):
                return result, ""
        return "", ""
    except Exception:
        return "", ""
def _get_tts(self):
    from engine.registry import get_engine
    engine = get_engine("tts")
    if engine is None:
        fn = getattr(self, "_get_tts_fn", None)
        if fn is not None:
            engine = fn()
        else:
            from main import get_tts
            engine = get_tts()
    return engine
AudioSocketSession._get_asr = _get_asr
AudioSocketSession._get_llm = _get_llm
AudioSocketSession._get_search = _get_search
AudioSocketSession._extract_finance = _extract_finance
AudioSocketSession._get_tts = _get_tts


class AudioSocketServer:
    def __init__(self, host="0.0.0.0", port=9090, config=None, log_queue=None,
                 get_asr=None, get_tts=None, get_llm=None):
        self.host = host
        self.port = port
        self.config = config or {}
        self.log_queue = log_queue if log_queue is not None else []
        self._get_asr_fn = get_asr
        self._get_tts_fn = get_tts
        self._get_llm_fn = get_llm

    async def start(self):
        server = await asyncio.start_server(
            self._handle_connection, self.host, self.port
        )
        addr = server.sockets[0].getsockname()
        logger.info(f"AudioSocket 监听: {addr[0]}:{addr[1]}")
        async with server:
            await server.serve_forever()

    async def _handle_connection(self, reader, writer):
        # 每通电话按当前 tts 配置重载预合成音频（中屏切引擎后无需重启容器；edge 含 voice 后缀）
        try:
            load_cached_audio(self.config.get("tts", {}))
        except Exception:
            pass
        session = AudioSocketSession(reader, writer, self.config, self.log_queue)
        # 注入入口模块（__main__）的引擎工厂函数，避免 from main 加载第二模块实例
        session._get_asr_fn = self._get_asr_fn
        session._get_tts_fn = self._get_tts_fn
        session._get_llm_fn = self._get_llm_fn
        await session.run()
        # 会话已结束，检测是否需要回电拨号
        if session._callback_pending and session._caller_number:
            logger.info(f"[回电] 等后台收集完LLM回复 → 生成回拨音频 → 回拨到 {session._caller_number}")
            # 等待 _llm_receiver 启动的后台收集任务完成
            if hasattr(session, '_bg_collect_task'):
                await session._bg_collect_task
            # 收集完成后补AI回复到历史 + 保存完整上下文
            if session._callback_reply:
                history = session._conversation.history
                # 替换 timed_out 时保存的空 assistant 回复，或追加新回复
                if history and history[-1]["role"] == "assistant" and not history[-1]["content"]:
                    history[-1]["content"] = session._callback_reply
                else:
                    history.append({"role": "assistant", "content": session._callback_reply})
            session._save_conversation_context()
            # 一次性 TTS 合成完整回拨话术（不拼接）→ 失败则放弃回拨
            ok = await session._generate_callback_audio()
            if not ok:
                logger.warning(f"[回电] 回拨音频生成失败，放弃回拨")
                return
            # AMI Originate 回拨
            await session._callback()
