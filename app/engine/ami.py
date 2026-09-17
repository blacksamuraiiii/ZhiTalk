"""
AMI Controller - 通过 AMI 协议控制 FreePBX 外呼
封装了 pyst2 库的 ManagerMsg API
"""

import logging
from typing import Optional

logger = logging.getLogger("ai-backend.ami")


class AMIController:
    """FreePBX AMI 管理器"""

    def __init__(self, config: dict):
        freepbx = config["freepbx"]
        self.host = freepbx["host"]
        self.port = freepbx["ami_port"]
        self.username = freepbx["ami_user"]
        self.secret = freepbx["ami_secret"]
        self.callback_channel = freepbx["callback_channel"]
        self.callback_context = freepbx["callback_context"]
        self.callback_exten = freepbx["callback_exten"]
        self.callback_caller_id = freepbx["callback_caller_id"]
        self.mgr = None

    def connect(self):
        """连接 FreePBX AMI"""
        try:
            from asterisk.manager import Manager
            self.mgr = Manager()
            self.mgr.connect(self.host, self.port)
            self.mgr.login(self.username, self.secret)
            logger.info(f"AMI 已连接: {self.host}:{self.port}")
        except ImportError:
            logger.error("pyst2 未安装: uv pip install pyst2")
            raise
        except Exception as e:
            logger.error(f"AMI 连接失败: {e}")
            raise

    def status(self) -> dict:
        """获取系统状态"""
        if not self.mgr:
            self.connect()
        r = self.mgr.send_action({"Action": "CoreStatus"})
        return {
            "connected": r.get_header("Response") == "Success",
            "version": r.get_header("AsteriskVersion") or "unknown",
            "core_started": r.get_header("CoreStarted") or "unknown",
        }

    def originate_call(self, channel: str = None, context: str = None,
                       exten: str = None, caller_id: str = None,
                       timeout: int = 30000) -> dict:
        """
        发起外呼
        """
        if not self.mgr:
            self.connect()

        action = {
            "Action": "Originate",
            "Channel": channel or self.callback_channel,
            "Context": context or self.callback_context,
            "Exten": exten or self.callback_exten,
            "Priority": 1,
            "CallerID": caller_id or self.callback_caller_id,
            "Timeout": timeout,
        }

        r = self.mgr.send_action(action)
        return {
            "response": r.get_header("Response"),
            "message": r.get_header("Message"),
        }

    def command(self, cmd: str) -> str:
        """直接执行 Asterisk CLI 命令"""
        if not self.mgr:
            self.connect()
        r = self.mgr.send_action({"Action": "Command", "Command": cmd})
        return r.get_header("Output") or r.get_header("Message") or ""

    def show_endpoints(self) -> dict:
        """列出所有 PJSIP 端点（返回确认消息，端点列表通过事件推送）"""
        if not self.mgr:
            self.connect()
        r = self.mgr.send_action({"Action": "PJSIPShowEndpoints"})
        # 返回的只是确认消息，端点列表通过事件推送
        return {
            "response": r.get_header("Response"),
            "message": r.get_header("Message"),
        }

    def disconnect(self):
        """断开 AMI 连接"""
        if self.mgr:
            try:
                self.mgr.logoff()
                logger.info("AMI 已断开")
            except Exception:
                pass
