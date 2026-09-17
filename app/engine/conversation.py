"""
对话处理器 - 状态机 + 历史管理
"""

import logging
from typing import Optional

logger = logging.getLogger("ai-backend.conversation")


class ConversationHandler:
    """单次通话的对话管理"""

    def __init__(self, config: dict):
        self.config = config
        self.max_history = config["llm"]["max_history"]
        self.history: list[dict] = []  # [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]

    def get_messages(self, user_text: str) -> list[dict]:
        """
        获取 LLM 需要的 messages 数组

        Args:
            user_text: 当前轮用户的输入

        Returns:
            list: 含历史的 messages 列表
        """
        # 追加本轮用户输入
        self.history.append({"role": "user", "content": user_text})

        # 截断保留最近 N 轮
        truncated = self.history[-(self.max_history * 2):]

        return truncated

    def add_turn(self, user_text: str, assistant_text: str):
        """保存一轮对话记录（LLM 已回复后调用）"""
        self.history.append({"role": "assistant", "content": assistant_text})

        # 控制历史长度
        if len(self.history) > self.max_history * 2:
            self.history = self.history[-(self.max_history * 2):]

    def get_summary(self) -> str:
        """获取对话摘要（供通话日志记录）"""
        lines = []
        for msg in self.history[-10:]:  # 最近 10 条
            role = "用户" if msg["role"] == "user" else "AI"
            lines.append(f"{role}: {msg['content'][:50]}")
        return "\n".join(lines)

    def to_json(self) -> str:
        """序列化对话历史为 JSON（供回拨上下文传递）"""
        import json
        return json.dumps(self.history, ensure_ascii=False)

    @classmethod
    def from_json(cls, config: dict, json_str: str) -> "ConversationHandler":
        """从 JSON 反序列化恢复对话历史（回拨会话加载）"""
        import json
        handler = cls(config)
        try:
            handler.history = json.loads(json_str)
        except Exception:
            pass
        return handler
