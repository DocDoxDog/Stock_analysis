# -*- coding: utf-8 -*-
"""
===================================
Telegram 平台适配器
===================================

处理 Telegram 机器人的 Webhook 回调（interactive Q&A）。

配置要求：
- TELEGRAM_BOT_TOKEN: 从 @BotFather 获取的 Bot Token
- TELEGRAM_CHAT_ID: 允许交互的 Chat ID（用于推送，非 webhook 必需）
- TELEGRAM_WEBHOOK_SECRET: 可选，Telegram 会在
  ``X-Telegram-Bot-Api-Secret-Token`` 请求头中回传该值，用于验证请求
  确实来自 Telegram（通过 setWebhook 的 secret_token 参数设置）。

Telegram Bot API 文档：
https://core.telegram.org/bots/api#setwebhook
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from bot.platforms.base import BotPlatform
from bot.models import BotMessage, BotResponse, ChatType, WebhookResponse

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramPlatform(BotPlatform):
    """
    Telegram 平台适配器（Webhook 模式）

    支持：
    - 私聊 / 群聊消息解析
    - Secret token 请求验证（可选但推荐）
    - 直接在 Webhook 响应中回复（sendMessage method 内联），
      分析耗时较长时通过 send_followup 主动调用 Bot API 补发
    """

    def __init__(self):
        from src.config import get_config
        config = get_config()

        self._bot_token = getattr(config, 'telegram_bot_token', None)
        self._webhook_secret = getattr(config, 'telegram_webhook_secret', None)

    @property
    def platform_name(self) -> str:
        return "telegram"

    def verify_request(self, headers: Dict[str, str], body: bytes) -> bool:
        """
        验证 Telegram Webhook 请求

        Telegram 本身不对请求签名，官方推荐的做法是在调用 setWebhook 时
        设置 secret_token，Telegram 会在每次回调时把它放进
        X-Telegram-Bot-Api-Secret-Token 请求头，服务端只需比对即可。
        """
        if not self._webhook_secret:
            logger.warning(
                "[Telegram] 未配置 TELEGRAM_WEBHOOK_SECRET，跳过请求来源验证"
                "（建议配置以避免任意第三方调用该端点）"
            )
            return True

        # HTTP 头大小写不敏感，调用方应传入已标准化的 dict（如 FastAPI headers）
        incoming_secret = (
            headers.get('x-telegram-bot-api-secret-token')
            or headers.get('X-Telegram-Bot-Api-Secret-Token')
            or ''
        )
        if incoming_secret != self._webhook_secret:
            logger.warning("[Telegram] secret token 校验失败，拒绝请求")
            return False
        return True

    def handle_challenge(self, data: Dict[str, Any]) -> Optional[WebhookResponse]:
        """Telegram 不需要 URL 验证挑战"""
        return None

    def parse_message(self, data: Dict[str, Any]) -> Optional[BotMessage]:
        """
        解析 Telegram Update 对象

        参考: https://core.telegram.org/bots/api#update
        仅处理普通文本消息（message.text），忽略编辑消息、频道帖子等其他更新类型。
        """
        msg_data = data.get('message') or data.get('edited_message')
        if not msg_data:
            logger.debug("[Telegram] 忽略非消息类型的 update（如 channel_post 等）")
            return None

        text = (msg_data.get('text') or '').strip()
        if not text:
            logger.debug("[Telegram] 忽略无文本内容的消息（如图片/贴纸）")
            return None

        from_user = msg_data.get('from') or {}
        chat = msg_data.get('chat') or {}

        user_id = str(from_user.get('id', ''))
        chat_id = str(chat.get('id', ''))
        if not user_id or not chat_id:
            return None

        user_name = (
            from_user.get('username')
            or from_user.get('first_name')
            or user_id
        )

        chat_type_raw = chat.get('type', '')
        if chat_type_raw == 'private':
            chat_type = ChatType.PRIVATE
        elif chat_type_raw in ('group', 'supergroup'):
            chat_type = ChatType.GROUP
        else:
            chat_type = ChatType.UNKNOWN

        # 群聊中 Telegram 用 entities 里的 mention 标记 @机器人，这里做一个宽松判断：
        # 私聊天然视为"已寻址"机器人，群聊则检查文本是否包含 @ 提及
        mentioned = chat_type == ChatType.PRIVATE or '@' in text

        try:
            timestamp = datetime.fromtimestamp(msg_data.get('date', 0))
        except (ValueError, OSError, OverflowError):
            timestamp = datetime.now()

        message_thread_id = msg_data.get('message_thread_id')

        return BotMessage(
            platform=self.platform_name,
            message_id=str(msg_data.get('message_id', '')),
            user_id=user_id,
            user_name=str(user_name),
            chat_id=chat_id,
            chat_type=chat_type,
            content=text,
            raw_content=text,
            mentioned=mentioned,
            mentions=[],
            timestamp=timestamp,
            raw_data={
                **data,
                '_message_thread_id': message_thread_id,
            },
        )

    def format_response(
        self,
        response: BotResponse,
        message: BotMessage,
    ) -> WebhookResponse:
        """
        格式化 Telegram 响应

        Telegram 支持在 Webhook 的 HTTP 响应体里直接内联一个 method 调用
        （如 sendMessage），无需再发起额外的出站请求，这是最快的回复方式。
        仅适用于处理耗时在 Telegram 请求超时时间内完成的场景；耗时更久的
        分析建议改走 send_followup 主动调用 Bot API。
        """
        if not response.text:
            return WebhookResponse.success()

        payload: Dict[str, Any] = {
            "method": "sendMessage",
            "chat_id": message.chat_id,
            "text": response.text,
        }
        if response.markdown:
            payload["parse_mode"] = "Markdown"

        message_thread_id = (message.raw_data or {}).get('_message_thread_id')
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        return WebhookResponse.success(payload)

    def send_followup(
        self,
        response: 'BotResponse',
        message: 'BotMessage',
    ) -> bool:
        """
        通过 Telegram Bot API 主动补发消息

        当命令处理耗时较长（例如需要调用 LLM 做实时分析）导致无法在
        Webhook 响应窗口内完成时，dispatcher 会先返回一个 ACK，
        再由后台线程调用本方法把最终结果推送给用户。
        """
        if not self._bot_token:
            logger.error("[Telegram] 未配置 TELEGRAM_BOT_TOKEN，无法主动发送消息")
            return False
        if not response.text:
            return False

        import requests

        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": message.chat_id,
            "text": response.text,
        }
        if response.markdown:
            payload["parse_mode"] = "Markdown"

        message_thread_id = (message.raw_data or {}).get('_message_thread_id')
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200 and resp.json().get('ok'):
                logger.info("[Telegram] 补发消息成功")
                return True
            logger.error(f"[Telegram] 补发消息失败: {resp.status_code} {resp.text[:300]}")
            return False
        except Exception as exc:
            logger.error(f"[Telegram] 补发消息异常: {exc}")
            return False
