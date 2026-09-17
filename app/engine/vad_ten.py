"""
TenVAD - Silero-style VAD using ten-vad.int8.onnx
输入: 16kHz float32 PCM → 40维 mel filterbank + 1 pitch(0) = 41维 × 3帧上下文 → ONNX推理

注意: 本引擎工作在 16kHz 域。AudioSocket 原始帧是 8k int16 320B/frame(20ms)，
由调用方重采样后传入。VAD 索引（帧号）也在 16k 域，切回原始 8k 字节位置须 ÷2。
"""

import logging
import onnxruntime as ort
import numpy as np
from pathlib import Path

logger = logging.getLogger("ai-backend.vad_ten")

# ── 常量 ──
SAMPLE_RATE = 16000          # 模型输入采样率
WINDOW_LEN = 768             # 768 samples = 48ms @16kHz（对应 window metadata）
HOP_LEN = 256                # 256 samples = 16ms @16kHz（Silero VAD 标准 hop）
N_MEL = 40                   # mel 滤波器组数量
N_FFT = 512                  # FFT size
N_FEATURES = 41              # 40 mel + 1 pitch
CONTEXT = 3                  # 3 帧上下文

# 默认参数（被 config.yaml vad.tenvad 覆盖）
DEFAULT_MIN_SILENCE_DURATION = 2.0   # 秒
DEFAULT_THRESHOLD = 0.5
DEFAULT_MIN_SPEECH_DURATION = 0.1    # 秒


class TenVAD:
    """TenVAD 引擎——基于 ONNX 的 Silero 风格语音活动检测。

    用法::
        vad = TenVAD(model_path, config_sub)
        vad.reset_state()
        for frame_16k in ...:
            speech_prob = vad.process_frame(frame_16k)
            if vad.is_speech(speech_prob):
                ...

    关键参数来自 config.yaml vad.tenvad 子段:
        - threshold:      VAD 概率阈值（默认 0.5）
        - min_silence_duration: 尾部静音判定秒数（默认 2.0）
        - min_speech_duration:  最短语音段秒数（默认 0.1）

    方法:
        - process_frame(samples_16k: np.ndarray) -> float
        - reset_state()
        - is_speech(prob: float) -> bool
    """

    def __init__(self, model_path: str, config_sub: dict = None):
        if config_sub is None:
            config_sub = {}

        # ── 加载 ONNX 模型 ──
        model_path = str(model_path)
        if not Path(model_path).exists():
            raise FileNotFoundError(f"TenVAD 模型文件不存在: {model_path}")

        self._session = ort.InferenceSession(
            model_path,
            providers=ort.get_available_providers(),
        )

        # ── 解析模型 metadata ──
        meta = self._session.get_modelmeta()
        custom = meta.custom_metadata_map if hasattr(meta, 'custom_metadata_map') else {}

        self._window = np.array(
            [float(x) for x in custom.get('window', '').split(',') if x.strip()],
            dtype=np.float32
        )
        self._mean = np.array(
            [float(x) for x in custom.get('mean', '').split(',') if x.strip()],
            dtype=np.float32
        )
        self._inv_stddev = np.array(
            [float(x) for x in custom.get('inv_stddev', '').split(',') if x.strip()],
            dtype=np.float32
        )

        # 验证 metadata 完整性
        if len(self._window) != WINDOW_LEN:
            logger.warning(f"TenVAD window 长度 {len(self._window)} ≠ 预期 {WINDOW_LEN}")
        if len(self._mean) != N_FEATURES:
            logger.warning(f"TenVAD mean 长度 {len(self._mean)} ≠ 预期 {N_FEATURES}")
        if len(self._inv_stddev) != N_FEATURES:
            logger.warning(f"TenVAD inv_stddev 长度 {len(self._inv_stddev)} ≠ 预期 {N_FEATURES}")

        # ── 重采样：8k→16k soxr（调用方使用 _resample_8k_to_16k）──
        # 注意：resample 由 audiosocket.py 调用方完成，本类不包含重采样逻辑

        # ── mel 滤波器组（16kHz）──
        self._mel_filterbank = self._create_mel_filterbank(
            sr=SAMPLE_RATE, n_fft=N_FFT, n_mels=N_MEL
        )

        # ── 滑动窗口环形缓冲区 ——
        # 攒满 WINDOW_LEN 个 16k 样本才做 STFT
        self._ring = np.zeros(WINDOW_LEN, dtype=np.float32)
        self._ring_pos = 0

        # ── 3 帧上下文 FIFO ──
        self._feat_fifo = np.zeros((CONTEXT, N_FEATURES), dtype=np.float32)

        # ── LSTM 状态 ──
        self._h1 = np.zeros((1, 64), dtype=np.float32)
        self._c1 = np.zeros((1, 64), dtype=np.float32)
        self._h2 = np.zeros((1, 64), dtype=np.float32)
        self._c2 = np.zeros((1, 64), dtype=np.float32)

        # ── 配置参数 ──
        self.threshold = float(config_sub.get('threshold', DEFAULT_THRESHOLD))
        self.min_silence_duration = float(config_sub.get(
            'min_silence_duration', DEFAULT_MIN_SILENCE_DURATION))
        self.min_speech_duration = float(config_sub.get(
            'min_speech_duration', DEFAULT_MIN_SPEECH_DURATION))

        logger.info(
            f"TenVAD 初始化完成 | model={Path(model_path).name} | "
            f"threshold={self.threshold} | "
            f"min_silence={self.min_silence_duration}s | "
            f"min_speech={self.min_speech_duration}s"
        )

    # ────────────────────────────────────────────────────────────────
    # mel filterbank
    # ────────────────────────────────────────────────────────────────
    @staticmethod
    def _create_mel_filterbank(sr: int, n_fft: int, n_mels: int) -> np.ndarray:
        """构造 mel 滤波器组矩阵 (n_mels, n_fft // 2 + 1)。"""
        # 标准 mel 刻度
        low_mel = 0.0
        high_mel = 2595 * np.log10(1 + sr / 2 / 700)
        mel_points = np.linspace(low_mel, high_mel, n_mels + 2)
        hz_points = 700 * (10 ** (mel_points / 2595) - 1)
        bin = np.floor((n_fft + 1) * hz_points / sr).astype(int)
        bin = np.clip(bin, 0, n_fft // 2)

        fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
        for m in range(1, n_mels + 1):
            left = bin[m - 1]
            center = bin[m]
            right = bin[m + 1]
            if left != center:
                fb[m - 1, left:center] = np.linspace(0, 1, center - left)
            if center != right:
                fb[m - 1, center:right] = np.linspace(1, 0, right - center)
        return fb

    # ────────────────────────────────────────────────────────────────
    # 状态管理
    # ────────────────────────────────────────────────────────────────
    def reset_state(self):
        """重置所有状态（新一句话开始前调用）。"""
        self._ring.fill(0.0)
        self._ring_pos = 0
        self._feat_fifo.fill(0.0)
        self._h1.fill(0.0)
        self._c1.fill(0.0)
        self._h2.fill(0.0)
        self._c2.fill(0.0)

    # ────────────────────────────────────────────────────────────────
    # 核心：逐帧处理
    # ────────────────────────────────────────────────────────────────
    def process_frame(self, samples_16k: np.ndarray) -> float:
        """喂入一帧 16kHz float32 PCM，返回 VAD 概率。

        Args:
            samples_16k: 16kHz float32 [-1, 1] 音频块，长度应为 HOP_LEN(256)。
                8k 帧到 16k 域后一帧 = 320 样本（原 160 样本 × 2 重采样）。
                每 16ms（256 样本）算一个 VAD 帧。

        Returns:
            当前帧的 VAD 概率 [0, 1]。音频不足一帧时返回 0.0。
        """
        if len(samples_16k) == 0:
            return 0.0

        # ── 逐 sample 喂入环形缓冲区 ──
        for s in samples_16k:
            self._ring[self._ring_pos] = s
            self._ring_pos = (self._ring_pos + 1) % WINDOW_LEN

        # ── 如果窗口未满，无法做 STFT ──
        if self._ring_pos == 0 and not np.any(samples_16k):
            # 刚初始化，全是零
            if np.allclose(samples_16k, 0.0):
                return 0.0

        # ── 计算当前帧的 41 维特征 ──
        feat = self._compute_feature()

        # ── 更新 3 帧上下文 FIFO ──
        self._feat_fifo = np.roll(self._feat_fifo, -1, axis=0)
        self._feat_fifo[-1, :] = feat

        # ── 前 2 帧不推理（上下文不足 3 帧）──
        # 用 fifo 第一帧填充度来判断：只要第0帧还是初始全零就跳过
        if np.allclose(self._feat_fifo[0], 0.0):
            # 但是不能一直跳过——我们需要有3帧才能推理
            # 简单判断：如果第一帧是全零且不是最后一帧直接被覆盖掉
            if np.allclose(self._feat_fifo[0], 0.0) and np.allclose(self._feat_fifo[1], 0.0):
                return 0.5  # 不确定状态

        # ── ONNX 推理 ──
        input_feat = self._feat_fifo[np.newaxis, :, :]  # (1, 3, 41)
        outputs = self._session.run(
            ['output_1', 'output_2', 'output_3', 'output_6', 'output_7'],
            {
                'input_1': input_feat,
                'input_2': self._h1,
                'input_3': self._c1,
                'input_6': self._h2,
                'input_7': self._c2,
            }
        )
        prob = float(outputs[0][0, 0, 0])

        # ── 更新 LSTM 状态 ──
        self._h1 = outputs[1]
        self._c1 = outputs[2]
        self._h2 = outputs[3]
        self._c2 = outputs[4]

        return prob

    # ────────────────────────────────────────────────────────────────
    # 特征提取
    # ────────────────────────────────────────────────────────────────
    def _compute_feature(self) -> np.ndarray:
        """从环形缓冲区当前数据计算 41 维特征向量。

        特征 = [40 mel_log_energy, 0(pitch)]
        标准化: (feat - mean) * inv_stddev
        """
        # ── STFT ──
        # 从 ring 缓冲区重建连续窗口（保持缓存友好）
        windowed = np.empty(WINDOW_LEN, dtype=np.float32)
        pos = self._ring_pos
        windowed[:WINDOW_LEN - pos] = self._ring[pos:]
        windowed[WINDOW_LEN - pos:] = self._ring[:pos]
        windowed = windowed * self._window

        # FFT (512 点)
        spectrum = np.fft.rfft(windowed, n=N_FFT)
        power = np.abs(spectrum) ** 2  # (257,)

        # ── mel filterbank ──
        mel_energy = self._mel_filterbank @ power  # (40,)
        mel_energy = np.maximum(mel_energy, 1e-10)
        mel_log = np.log(mel_energy)  # (40,)

        # ── 拼接 40 mel + 1 pitch (0) ──
        feat = np.zeros(N_FEATURES, dtype=np.float32)
        feat[:N_MEL] = mel_log

        # ── 标准化 ──
        feat = (feat - self._mean) * self._inv_stddev

        return feat

    # ────────────────────────────────────────────────────────────────
    # 辅助判断
    # ────────────────────────────────────────────────────────────────
    @staticmethod
    def is_speech(prob: float) -> bool:
        """概率大于阈值即为语音（由调用方自行判断，保持 stateless）。"""
        return prob > 0.5  # 阈值由调用方传参决定


def create_vad(config: dict) -> TenVAD:
    """工厂函数：从 config 创建 TenVAD 实例。

    config 形如:
        {
            "vad": {
                "vad_engine": "tenvad",
                "tenvad": {
                    "model": "models/VAD/ten-vad.int8.onnx",
                    "threshold": 0.5,
                    "min_silence_duration": 2.0,
                    "min_speech_duration": 0.1
                }
            }
        }
    路径相对于 ai-backend/。
    """
    vad_cfg = config.get("vad", {})
    tenvad_cfg = vad_cfg.get("tenvad", {})

    model_path = tenvad_cfg.get("model", "models/VAD/ten-vad.int8.onnx")
    # 相对路径 → 相对于 ai-backend/
    p = Path(model_path)
    if not p.is_absolute():
        base = Path(__file__).parent.parent.parent  # ai-backend/
        model_path = str(base / p)

    return TenVAD(model_path, tenvad_cfg)
