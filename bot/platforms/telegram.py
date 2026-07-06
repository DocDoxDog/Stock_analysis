import logging
from typing import Dict, Any, Optional
from .base import BotPlatform
from ..models import BotMessage, BotResponse, PlatformType

logger = logging.getLogger(__name__)

class TelegramPlatform(BotPlatform):
    @property
    def platform_name(self) -> str:
        return PlatformType.TELEGRAM.value # หรือ return "telegram" ตามที่ enum ตั้งไว้

    async def verify_request(self, request: Any) -> bool:
        # Telegram Webhook ไม่มี Signature header ให้ตรวจเหมือน DingTalk
        # วิธีที่ปลอดภัยที่สุดคือการใส่ Secret Token ไว้ใน URL path ของ Webhook 
        # (เช่น /webhook/telegram/<SECRET>) และตรวจที่ระดับ FastAPI Router
        # ดังนั้นในระดับ platform อาจจะ return True ไปก่อน หรือเช็คโครงสร้างเบื้องต้น
        return True 

    async def parse_message(self, request: Any) -> Optional[BotMessage]:
        try:
            # สมมติว่า request คือ JSON payload ที่ parse มาแล้ว
            payload = request if isinstance(request, dict) else await request.json()
            
            # Telegram จะส่งข้อมูลมาในก้อน 'message'
            if 'message' not in payload:
                return None
                
            msg_data = payload['message']
            
            # ดึงข้อมูลที่จำเป็น
            text = msg_data.get('text', '').strip()
            user_id = str(msg_data.get('from', {}).get('id', ''))
            chat_id = str(msg_data.get('chat', {}).get('id', ''))
            
            if not text or not user_id:
                return None

            return BotMessage(
                platform=PlatformType.TELEGRAM,
                message_id=str(msg_data.get('message_id', '')),
                user_id=user_id,
                chat_id=chat_id,
                content=text,
                raw_data=payload
            )
        except Exception as e:
            logger.error(f"Error parsing Telegram message: {e}")
            return None

    async def format_response(self, response: BotResponse) -> Dict[str, Any]:
        # วิธีที่ง่ายที่สุดสำหรับ Telegram Webhook คือการ return JSON กลับไปตรงๆ 
        # ใน HTTP Response เดียวกันเลย (ไม่ต้องแยกไปยิง API sendMessage ใหม่)
        
        telegram_payload = {
            "method": "sendMessage",
            "chat_id": response.chat_id,
            "text": response.content,
            "parse_mode": "Markdown" # หากคุณต้องการส่ง Markdown
        }
        
        return telegram_payload
