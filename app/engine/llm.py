"""
LLM 客户端 - OpenAI 兼容接口（DeepSeek / Qwen / 等）
"""

import os
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ai-backend.llm")


class OpenAILLM:
    """OpenAI 兼容接口的 LLM 客户端"""

    def __init__(self, config: dict):
        provider = config.get("provider", "openai_compat")
        self.provider = provider

        if provider == "local_llama":
            # 本地 llama-server：从 local_llama 子段取配置（不依赖外部 API）
            local_cfg = config.get("local_llama", {})
            self.api_base = local_cfg.get("api_base", "http://host.docker.internal:8081/v1")
            self.api_key = local_cfg.get("api_key", "")
            self.model = local_cfg.get("model", "unsloth/Qwen3.5-4B-MTP-GGUF")
            self.system_prompt = local_cfg.get("system_prompt", "")
            self.max_tokens = local_cfg.get("max_tokens", 200)
        else:
            # 外部 API（原有逻辑，默认 openai_compat）
            self.api_base = config.get("api_base", "https://api.deepseek.com/v1")
            self.api_key = config.get("api_key", "")
            self.model = config.get("model", "deepseek-chat")
            self.system_prompt = config.get("system_prompt", "")
            self.max_tokens = config.get("max_tokens", 200)

        # 公共参数
        self.max_turns = config.get("max_turns", 20)
        self.max_history = config.get("max_history", 10)
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=self.api_base,
                api_key=self.api_key,
                max_retries=0,  # 不重试：timeout 参数精确生效
            )
        return self._client

    def chat(self, messages: list, timeout: int = 30, system_extra: str = "", max_tokens: Optional[int] = None) -> str:
        """
        发送对话请求

        Args:
            messages: [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
            timeout: 超时秒数
            system_extra: 额外追加的 system 消息（如联网检索上下文），拼在主 system_prompt 之后

        Returns:
            str: LLM 回复文本。超时/API异常返回空字符串（不回退兜底文案，由上层决策）。
        """
        client = self._get_client()

        # 在 messages 前插入 system prompt（如果尚未有 system 消息）
        # 并自动追加当前日期时间，让 LLM 能回答"今天几号"等时间问题
        full_messages = []
        if self.system_prompt and not any(m.get("role") == "system" for m in messages):
            weekdays = ['一', '二', '三', '四', '五', '六', '日']
            now = datetime.now()
            prompt = (
                f"{self.system_prompt}\n"
                f"当前时间：{now.strftime('%Y年%m月%d日 %H:%M')}，"
                f"星期{weekdays[now.weekday()]}。"
            )
            full_messages.append({"role": "system", "content": prompt})
        if system_extra:
            full_messages.append({"role": "system", "content": system_extra})
        full_messages.extend(messages)

        try:
            mt = max_tokens if max_tokens is not None else self.max_tokens
            extra = {"max_tokens": mt} if mt > 0 else {}
            kwargs = {
                "model": self.model,
                "messages": full_messages,
                "timeout": timeout,
                "stream": False,
                **extra,
            }
            # thinking 禁用是 DeepSeek 专属参数，本地 llama-server 不传（避免未知字段报 400）
            if self.provider != "local_llama":
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            response = client.chat.completions.create(**kwargs)
            reply = response.choices[0].message.content.strip()
            logger.info(f"[LLM] 回复: {reply[:100]}...")
            return reply
        except Exception as e:
            logger.error(f"[LLM] 请求失败: {e}")
            return ""  # 超时/异常返回空，上层区分是否是回拨场景

    def chat_with_search(self, messages: list, ctx: str, timeout: int = 60) -> str:
        """联网搜索场景：system 追加检索上下文 + 约束，非流式。

        Args:
            messages: 完整对话消息（含历史）
            ctx: 检索上下文（build_ctx 输出的格式化文本）
            timeout: 超时秒数（联网请求容忍长回复，默认 60s）

        Returns:
            str: LLM 回复文本，异常返回空串。
        """
        system_extra = (
            "以下是联网检索结果，请基于这些信息回答用户问题。"
            "优先从检索结果中提炼可用信息直接回答，不要先说查不到。"
            "回答必须简短，用1-2句话概括最关键信息，不要罗列全部细节。"
            "只有当检索结果完全不含任何相关内容时，才说明「没有查到相关信息」，不要编造。\n\n"
            f"【联网检索结果】\n{ctx}"
        )
        reply = self.chat(messages, timeout=timeout, system_extra=system_extra, max_tokens=0)
        logger.info(f"[LLM][联网] 回复: {reply[:100]}..." if reply else "[LLM][联网] 回复为空")
        return reply

    def chat_stream(self, messages: list, timeout: int = 30):
        """流式对话（同步生成器，供 audiosocket 分句 TTS 使用）"""
        client = self._get_client()

        full_messages = []
        if self.system_prompt and not any(m.get("role") == "system" for m in messages):
            weekdays = ['一', '二', '三', '四', '五', '六', '日']
            now = datetime.now()
            prompt = (
                f"{self.system_prompt}\n"
                f"当前时间：{now.strftime('%Y年%m月%d日 %H:%M')}，"
                f"星期{weekdays[now.weekday()]}。"
            )
            full_messages.append({"role": "system", "content": prompt})
        full_messages.extend(messages)

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=full_messages,
                timeout=timeout,
                stream=True,
                max_tokens=self.max_tokens,
            )
            for chunk in response:
                content = chunk.choices[0].delta.content
                if content:
                    yield content
        except Exception as e:
            logger.error(f"[LLM] 流式请求失败: {e}")
            # 不 yield 兜底文案，直接抛异常让 _llm_producer 捕获放入 __error__ 标记，
            # 由 _llm_receiver 决定是否播"抱歉"（正常对话播，回拨场景不播，防串音）
            raise
