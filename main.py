from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.agent.message import Message
from astrbot.api import logger
from datetime import datetime, timedelta
import asyncio
import time
import re
import json
import os
import random
from collections import defaultdict

DEFAULT_CONFIG = {
    "admin_id": "",
    "enable_sue": True,
    "custom_error_message": "请有人告诉引灯续昼我的AI出现了问题",
    "enable_custom_error": True,
    "proactive_reply_enabled": False,
    "proactive_reply_start_hour": 9,
    "proactive_reply_end_hour": 23,
    "proactive_reply_min_interval": 1800,
    "proactive_reply_max_interval": 7200,
    "proactive_reply_timezone_offset": 8,
    "immersive_followup_enabled": False,
    "immersive_followup_timeout": 300,
    "immersive_followup_max_rounds": 3,
    "max_history_length": 50,
    "proactive_style_hint": "",
}

LLM_CALL_TIMEOUT = 120

_RE_ERROR_CODE = re.compile(r'Error code:\s*(\d+)')
_RE_JSON_BLOCK = re.compile(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}')
_RE_QUESTION = re.compile(r'[？?]')
_RE_ELLIPSIS = re.compile(r'[…]{2,}|\. {2,}|…')
_RE_CLOSING = re.compile(
    r'晚安|再见|拜拜|下次聊|先这样|那就这样|早点休息|注意休息|不用回了|没事了|好的$|嗯$|哦$'
)

# Openers that make a proactive message read like a passive answer instead of a
# real person starting a conversation ("嗯，那...", "行吧，...").
_RE_FILLER_OPENER = re.compile(
    r'^(?:嗯+|哦+|噢+|啊+|唉+|诶+|哎+|唔+|行吧|好吧|那好吧)\s*[，,。.、~～…\s]'
)

WEEKDAY_NAMES = "一二三四五六日"

# Shared stance rules for every message this plugin sends on its own initiative.
CONTACT_STYLE_RULES = """# 主动开口的说话方式

此刻是你在主动找人，不是在回复谁。你要像真人忽然想起对方一样，直接发出属于自己的那条消息。

必须做到：
- 第一个字就是内容本身。不要用"嗯/哦/啊/唉/诶/行吧/好吧/那好吧"这类垫话起头，也不要先复述对方说过的话。
- 说具体的东西：此刻你在做什么、刚看到或想到什么、一个具体的小问题、一句具体到细节的关心。避免"在吗""最近怎么样""想你了"这种空话。
- 长度像微信聊天：一到两句，二十到五十字。可以不带句号，可以用你的口癖、语气词和颜文字。
- 不要解释自己在做什么，别说"我来找你聊聊""我想主动和你说句话"。
- 不要把话说完说尽，留一点让对方能接的口子。

绝对不要：
- 以敷衍的承接词开头；
- 在对方没说话的情况下，重复或延续上一轮的话题细节；
- 输出括号里的动作描写、旁白、时间戳。"""

_FILLER_RETRY_HINT = (
    "\n\n（上一版开头用了「嗯/哦/啊」这类垫话，读起来像在被动接话。"
    "重写一条，第一个字必须就是内容本身。）"
)


def _day_part(hour: int) -> str:
    """Map a 24-hour clock hour to a colloquial part of the day.

    Args:
        hour: Hour of day in the range 0-23.

    Returns:
        A Chinese label such as 凌晨 / 上午 / 下午 / 深夜.
    """
    if 0 <= hour < 5:
        return "凌晨"
    if hour < 9:
        return "清晨"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


def _humanize_gap(seconds: float) -> str:
    """Render an elapsed duration as a short human-readable phrase.

    Args:
        seconds: Elapsed seconds; negative values are treated as zero.

    Returns:
        A phrase such as "23 分钟" or "9 小时 30 分钟".
    """
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {rest_minutes} 分钟" if rest_minutes else f"{hours} 小时"
    days, rest_hours = divmod(hours, 24)
    return f"{days} 天 {rest_hours} 小时" if rest_hours else f"{days} 天"


@register("astrbot_plugin_zhudongshiliao", "引灯续昼", "自动私聊插件，提供私聊、AI主动回复和沉浸式对话延续功能。", "0.9.8")
class MyPlugin(Star):
    COMPILED_ERROR_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
        r'Error code:\s*\d+',
        r'All chat models failed',
        r'AuthenticationError',
        r'API key is invalid',
        r'API key is incorrect',
        r'Rate limit exceeded',
        r'quota exceeded',
        r'timeout.*error',
        r'connection.*error',
        r'network.*error',
        r'500 Internal Server Error',
        r'502 Bad Gateway',
        r'503 Service Unavailable',
        r'Model does not exist',
        r'context length exceeded',
        r'max_tokens exceeded',
        r'invalid api',
        r'请求失败.*错误',
        r'AstrBot 请求失败',
    ]]

    ERROR_KEYWORDS = ["LLM 响应错误", "All chat models failed", "AstrBot 请求失败", "错误类型"]
    EXCEPTION_TERMS = ["File ", "line ", "at ", "raise "]

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._config_cache = None
        self._config_cache_time = 0
        self._config_cache_ttl = 60
        self.message_rate_limit = defaultdict(list)
        self.rate_limit_window = 60
        self.rate_limit_max = 5
        self.last_cleanup_time = time.time()
        self.cleanup_interval = 3600

        self._proactive_reply_task = None
        self._last_user_activity_time = None
        self._proactive_scheduled_time = None
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5

        self._followup_state = {}

        self._admin_umo = None
        self._umo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "_admin_umo.json")
        os.makedirs(os.path.dirname(self._umo_file), exist_ok=True)
        self._load_admin_umo()

    def _load_admin_umo(self):
        try:
            if os.path.exists(self._umo_file):
                with open(self._umo_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._admin_umo = data.get("umo")
        except Exception:
            pass

    def _save_admin_umo(self):
        try:
            with open(self._umo_file, "w", encoding="utf-8") as f:
                json.dump({"umo": self._admin_umo}, f)
        except Exception:
            pass

    def _cleanup_rate_limit(self):
        current_time = time.time()
        if current_time - self.last_cleanup_time < self.cleanup_interval:
            return

        expired_keys = []
        for user_id, timestamps in self.message_rate_limit.items():
            valid_timestamps = [
                timestamp for timestamp in timestamps
                if current_time - timestamp < self.rate_limit_window
            ]
            if valid_timestamps:
                self.message_rate_limit[user_id] = valid_timestamps
            else:
                expired_keys.append(user_id)

        for user_id in expired_keys:
            del self.message_rate_limit[user_id]

        self.last_cleanup_time = current_time

    def _check_rate_limit(self, user_id):
        user_id_str = str(user_id)
        current_time = time.time()

        if current_time - self.last_cleanup_time >= self.cleanup_interval:
            self._cleanup_rate_limit()

        self.message_rate_limit[user_id_str] = [
            timestamp for timestamp in self.message_rate_limit[user_id_str]
            if current_time - timestamp < self.rate_limit_window
        ]

        if len(self.message_rate_limit[user_id_str]) >= self.rate_limit_max:
            return False

        return True

    def _record_rate_limit(self, user_id):
        self.message_rate_limit[str(user_id)].append(time.time())

    def _get_first_platform_id(self) -> str | None:
        try:
            if self.context.platform_manager and self.context.platform_manager.platform_insts:
                return self.context.platform_manager.platform_insts[0].meta().id
        except Exception as e:
            logger.error(f"获取平台ID失败: {e}")
        return None

    async def _get_history_list(self, umo: str):
        if not self.context.conversation_manager:
            return [], None, None
        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not conv_id:
                return [], None, None
            conv = await self.context.conversation_manager.get_conversation(umo, conv_id)
            if not conv:
                return [], conv_id, None
            history = json.loads(conv.history) if conv.history else []
            return history, conv_id, conv
        except Exception:
            return [], None, None

    async def send_private_message(self, user_id, message, event=None, source_context=None):
        if not user_id or not message:
            return False

        user_id_str = str(user_id).strip()
        if not user_id_str:
            return False

        if not self._check_rate_limit(user_id_str):
            logger.warning(f"私聊频率限制：用户 {user_id_str}")
            return False

        try:
            platform_id = None
            if event and hasattr(event, 'get_platform_id'):
                platform_id = event.get_platform_id()

            if not platform_id:
                platform_id = self._get_first_platform_id()

            if not platform_id:
                logger.error("无法获取平台ID")
                return False

            self._record_rate_limit(user_id_str)

            if source_context and self.context.conversation_manager:
                await self._inject_cross_session_context(
                    target_id=user_id_str,
                    platform_id=platform_id,
                    source_context=source_context,
                    message_content=message,
                )

            session = MessageSession(
                platform_name=platform_id,
                message_type=MessageType.FRIEND_MESSAGE,
                session_id=user_id_str
            )
            message_chain = MessageChain()
            message_chain.chain = [Plain(message)]
            await self.context.send_message(session, message_chain)

            if source_context and self.context.conversation_manager:
                await self._inject_source_confirmation(
                    source_context=source_context,
                    target_type="私聊",
                    target_name=f"用户{user_id_str}",
                    message_content=message,
                )

            return True
        except Exception as e:
            logger.error(f"发送私聊消息失败：{e}")
            return False

    async def _inject_cross_session_context(
        self,
        target_id: str,
        platform_id: str,
        source_context: dict,
        message_content: str,
        is_group: bool = False,
    ):
        if not self.context.conversation_manager:
            return

        msg_type = "GroupMessage" if is_group else "FriendMessage"
        target_umo = f"{platform_id}:{msg_type}:{target_id}"

        try:
            history, conv_id, _ = await self._get_history_list(target_umo)
            if not conv_id:
                return

            source_type = source_context.get("source_type", "群聊")
            source_name = source_context.get("source_name", "未知")
            sender_name = source_context.get("sender_name", "某人")
            recent_messages = source_context.get("recent_messages", [])

            context_parts = []
            for msg in recent_messages[-6:]:
                role = "用户" if msg.get("role") == "user" else "AI"
                content = str(msg.get("content", "") or "")[:300]
                if content.strip():
                    context_parts.append(f"{role}: {content}")

            recent_summary = "\n".join(context_parts) if context_parts else "（无最近对话）"
            target_label = "群" if is_group else "用户"
            context_content = (
                f"[跨会话通知] {sender_name}在{source_type}「{source_name}」让你给当前{target_label}发了消息：「{message_content}」。"
                f"以下是来源对话的最近内容，帮助你理解前因后果：\n{recent_summary}"
            )

            history.append({"role": "system", "content": context_content})
            history = self._trim_history(history)

            await self.context.conversation_manager.update_conversation(
                unified_msg_origin=target_umo,
                conversation_id=conv_id,
                history=history,
            )
        except Exception as e:
            logger.error(f"注入跨会话上下文失败: {e}")

    async def _inject_source_confirmation(
        self,
        source_context: dict,
        target_type: str,
        target_name: str,
        message_content: str,
    ):
        if not self.context.conversation_manager:
            return

        source_umo = source_context.get("source_umo")
        if not source_umo:
            return

        try:
            history, conv_id, _ = await self._get_history_list(source_umo)
            if not conv_id:
                return

            confirm_content = f"[你已成功向{target_type}「{target_name}」发送了消息：「{message_content}」。如果对方回复，你会知道。]"

            history.append({"role": "system", "content": confirm_content})
            history = self._trim_history(history)

            await self.context.conversation_manager.update_conversation(
                unified_msg_origin=source_umo,
                conversation_id=conv_id,
                history=history,
            )
        except Exception as e:
            logger.error(f"注入来源确认失败: {e}")

    @filter.llm_tool(name="private_message")
    async def private_message(self, event: AstrMessageEvent, user_id: str, content: str) -> str:
        """
        发送私聊消息工具。消息会静默发送给目标用户，不会在当前聊天中显示。

        Args:
            user_id(string): 目标用户的ID
            content(string): 要发送的消息内容

        Returns:
            str: 发送结果描述
        """
        if not user_id or not content:
            return "参数错误：用户ID和消息内容不能为空"

        source_context = await self._build_source_context(event)
        success = await self.send_private_message(user_id, content, event, source_context)
        if success:
            return f"已向用户 {user_id} 发送私聊消息"
        else:
            return "私聊消息发送失败"

    async def _build_source_context(self, event: AstrMessageEvent) -> dict:
        source_context = {
            "source_type": "私聊",
            "source_name": "未知",
            "sender_name": "某人",
            "source_umo": None,
            "recent_messages": [],
        }

        try:
            if event:
                session_id = event.get_session_id()
                message_type = event.get_message_type()

                if message_type == MessageType.GROUP_MESSAGE:
                    source_context["source_type"] = "群聊"
                    source_context["source_name"] = f"群{session_id}"
                elif message_type == MessageType.FRIEND_MESSAGE:
                    source_context["source_type"] = "私聊"
                    source_context["source_name"] = f"用户{session_id}"

                sender_id = event.get_sender_id()
                if sender_id:
                    source_context["sender_name"] = f"用户{sender_id}"

                if self.context.conversation_manager:
                    try:
                        platform_id = event.get_platform_id() if hasattr(event, 'get_platform_id') else "qq"
                        message_type_str = "GroupMessage" if message_type == MessageType.GROUP_MESSAGE else "FriendMessage"
                        umo = f"{platform_id}:{message_type_str}:{session_id}"
                        source_context["source_umo"] = umo

                        history, _, _ = await self._get_history_list(umo)
                        source_context["recent_messages"] = [
                            msg for msg in history[-10:]
                            if msg.get("role") in ("user", "assistant") and str(msg.get("content", "") or "").strip()
                        ]
                    except Exception:
                        pass
        except Exception:
            pass

        return source_context

    def _get_config(self):
        current_time = time.time()
        if self._config_cache and (current_time - self._config_cache_time) < self._config_cache_ttl:
            return self._config_cache

        try:
            config = self.config
            if not isinstance(config, dict):
                result = DEFAULT_CONFIG.copy()
            else:
                result = config.copy()
                for key, value in DEFAULT_CONFIG.items():
                    if key not in result:
                        result[key] = value

            self._config_cache = result
            self._config_cache_time = current_time
            return result
        except Exception:
            return DEFAULT_CONFIG.copy()

    def _trim_history(self, history: list) -> list:
        config = self._get_config()
        max_len = int(config.get("max_history_length", 50))
        if len(history) > max_len:
            return history[-max_len:]
        return history

    @filter.llm_tool(name="message_to_admin")
    async def message_to_admin(self, event: AstrMessageEvent, content: str) -> str:
        """
        向管理员发送消息工具。用于AI主动联系管理员。

        Args:
            content(string): 要发送给管理员的消息内容

        Returns:
            str: 发送结果
        """
        config = self._get_config()
        admin_id = config.get("admin_id", "")
        if not admin_id:
            return "管理员ID未配置，无法发送消息"

        source_context = await self._build_source_context(event)
        success = await self.send_private_message(admin_id, content, event, source_context)
        if success:
            return "已向管理员发送消息"
        else:
            return "发送管理员消息失败"

    @filter.llm_tool(name="sue_to_admin")
    async def sue_to_admin(self, event: AstrMessageEvent, content: str):
        """
        告状工具 - 静默向管理员发送告状消息。

        注意：此操作完全静默执行，不会在当前对话中显示任何反馈，
        就像悄悄报警一样，不会让"歹徒"知道你要报警。

        Args:
            content(string): 告状内容，建议包含：
                1. 发生的群聊（如果是群聊中发生的）
                2. 具体是谁说的坏话
                3. 说了什么具体内容
        """
        config = self._get_config()

        if not config.get("enable_sue", True):
            return

        admin_id = config.get("admin_id", "")
        if not admin_id:
            logger.debug("管理员ID未配置，无法发送告状消息")
            return

        source_context = await self._build_source_context(event)
        source_context["is_sue"] = True

        success = await self.send_private_message(admin_id, f"【告状】\n{content}", event, source_context)
        if success:
            logger.debug(f"告状消息已发送给管理员: {admin_id}")

    @filter.llm_tool(name="group_message")
    async def send_group_message(self, event: AstrMessageEvent, group_id: str, content: str) -> str:
        """
        发送消息到群里的工具

        Args:
            group_id (string): 群聊ID
            content(string): 消息内容

        Returns:
            str: 发送结果
        """
        if not group_id or not content:
            return "参数错误：群ID和消息内容不能为空"

        if not self._check_rate_limit(group_id):
            logger.warning(f"群消息频率限制：群 {group_id}")
            return "群消息发送失败：发送过于频繁，请稍后再试"

        try:
            group_id_str = str(group_id)

            platform_id = None
            if event and hasattr(event, 'get_platform_id'):
                platform_id = event.get_platform_id()

            if not platform_id:
                platform_id = self._get_first_platform_id()

            if not platform_id:
                logger.error("无法获取平台ID")
                return "群消息发送失败：无法获取平台ID"

            self._record_rate_limit(group_id_str)

            source_context = await self._build_source_context(event)
            source_context["target_type"] = "群聊"

            if self.context.conversation_manager:
                await self._inject_cross_session_context(
                    target_id=group_id_str,
                    platform_id=platform_id,
                    source_context=source_context,
                    message_content=content,
                    is_group=True,
                )

            session = MessageSession(
                platform_name=platform_id,
                message_type=MessageType.GROUP_MESSAGE,
                session_id=group_id_str
            )
            message_chain = MessageChain()
            message_chain.chain = [Plain(content)]
            await self.context.send_message(session, message_chain)

            if source_context and self.context.conversation_manager:
                await self._inject_source_confirmation(
                    source_context=source_context,
                    target_type="群聊",
                    target_name=f"群{group_id_str}",
                    message_content=content,
                )

            return "群消息发送成功"

        except Exception as e:
            logger.error(f"群消息发送失败：{e}")
            return "群消息发送失败：系统暂时无法发送消息，请稍后再试"

    def _replace_error_variables(self, message, error_message="", error_code=""):
        message = message.replace("{error_message}", error_message)
        message = message.replace("{error_code}", error_code)
        return message

    def _is_error_message(self, text: str) -> bool:
        for pattern in self.COMPILED_ERROR_PATTERNS:
            if pattern.search(text):
                return True

        if any(kw in text for kw in self.ERROR_KEYWORDS):
            return True

        has_exception_marker = (
            ("Exception" in text or "exception" in text or "Traceback" in text)
            and any(term in text for term in self.EXCEPTION_TERMS)
        )
        return has_exception_marker

    def _extract_error_info(self, error_message):
        error_code = ""
        error_detail = error_message

        try:
            m = _RE_ERROR_CODE.search(error_message)
            if m:
                error_code = m.group(1)

            m = _RE_JSON_BLOCK.search(error_message)
            if m:
                try:
                    error_data = json.loads(m.group(0))
                    if isinstance(error_data, dict):
                        if 'code' in error_data:
                            error_code = str(error_data['code'])
                        if 'message' in error_data:
                            error_detail = error_data['message']
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass

        return error_code, error_detail

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        config = self._get_config()
        if not config.get("enable_custom_error", True):
            return

        result = event.get_result()
        if not result:
            return

        if not hasattr(result, 'is_llm_result') or not result.is_llm_result():
            return

        try:
            is_error = False
            error_message = ""

            if hasattr(result, 'chain') and result.chain:
                for comp in result.chain:
                    if hasattr(comp, 'text') and comp.text:
                        text = comp.text
                        if self._is_error_message(text):
                            is_error = True
                            error_message = text
                            break
            elif hasattr(result, 'text') and result.text:
                text = result.text
                if self._is_error_message(text):
                    is_error = True
                    error_message = text

            if is_error:
                custom_error = config.get("custom_error_message", "请有人告诉引灯续昼我的AI出现了问题")
                error_code, error_detail = self._extract_error_info(error_message)
                custom_error = self._replace_error_variables(custom_error, error_detail, error_code)

                if hasattr(result, 'chain'):
                    new_chain = []
                    for comp in result.chain:
                        if hasattr(comp, 'text') and comp.text and self._is_error_message(comp.text):
                            new_chain.append(Plain(custom_error))
                        else:
                            new_chain.append(comp)
                    result.chain = new_chain
                elif hasattr(result, 'text'):
                    result.text = custom_error

        except Exception as e:
            logger.error(f"错误消息替换失败：{e}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        config = self._get_config()
        if not config.get("enable_custom_error", True):
            return

        if response.role != "err":
            return

        custom_error = config.get("custom_error_message", "请有人告诉引灯续昼我的AI出现了问题")
        error_code, error_detail = self._extract_error_info(response.completion_text or "")
        custom_error = self._replace_error_variables(custom_error, error_detail, error_code)
        response.completion_text = custom_error

    # ==================== LLM 调用层 ====================

    def _get_llm_provider_id(self) -> str | None:
        providers = self.context.get_all_providers()
        if providers:
            chosen = providers[0]
            logger.debug(f"自动选择 LLM Provider: {chosen.meta().id} ({chosen.meta().model})")
            return chosen.meta().id
        logger.error("没有找到可用的 LLM Provider")
        return None

    async def _get_persona_prompt(self, umo: str, conv) -> str:
        try:
            persona_id = None
            if conv and hasattr(conv, 'persona_id') and conv.persona_id:
                persona_id = conv.persona_id

            platform_id = self._get_first_platform_id() or "default"
            _, persona, _, _ = await self.context.persona_manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=persona_id,
                platform_name=platform_id,
            )

            if persona and persona.get("prompt"):
                return persona["prompt"]
        except Exception:
            pass

        try:
            default_persona = await self.context.persona_manager.get_default_persona_v3(umo)
            if default_persona and default_persona.get("prompt"):
                return default_persona["prompt"]
        except Exception:
            pass

        return ""

    async def _call_llm_with_context(
        self,
        umo: str,
        prompt: str,
        round_label: str = "",
        preloaded_history: list | None = None,
        extra_system: str = "",
        style_guard: bool = False,
    ) -> str | None:
        if not umo:
            logger.debug("会话UMO为空")
            return None

        try:
            provider_id = await self.context.get_current_chat_provider_id(umo)
        except Exception:
            provider_id = self._get_llm_provider_id()

        if not provider_id:
            logger.error("无法获取聊天模型ID")
            return None

        if preloaded_history is not None:
            history = list(preloaded_history)
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
            conv = None
            if conv_id:
                try:
                    conv = await self.context.conversation_manager.get_conversation(umo, conv_id)
                except Exception:
                    pass
        else:
            history, conv_id, conv = await self._get_history_list(umo)

        if not conv_id:
            logger.debug("未找到会话")
            return None

        persona_prompt = await self._get_persona_prompt(umo, conv)
        system_prompt = "\n\n".join(
            part for part in (persona_prompt, extra_system) if part
        )

        contexts = []
        for msg in history[-20:]:
            role = msg.get("role", "")
            content = str(msg.get("content", "") or "").strip()
            if role in ("user", "assistant") and content:
                contexts.append(Message(role=role, content=content))

        if not contexts:
            logger.debug("无有效上下文")
            return None

        # One retry is allowed when a proactive message still opens with a
        # filler word; nothing is written to history until an attempt passes.
        current_prompt = prompt
        text = ""
        for attempt in range(2):
            try:
                response = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=current_prompt,
                        contexts=contexts,
                        system_prompt=system_prompt,
                    ),
                    timeout=LLM_CALL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error(f"LLM调用超时{round_label}（{LLM_CALL_TIMEOUT}秒）")
                return None
            except Exception as e:
                logger.error(f"LLM调用失败{round_label}: {e}")
                return None

            if not response.completion_text:
                logger.debug(f"LLM回复为空{round_label}")
                return None

            text = response.completion_text.strip()
            if "[SILENCE]" in text or len(text) < 2:
                logger.debug(f"AI选择沉默{round_label}")
                return None

            if style_guard and attempt == 0 and _RE_FILLER_OPENER.match(text):
                logger.debug(f"主动开口用了垫话，重写一次{round_label}")
                current_prompt = prompt + _FILLER_RETRY_HINT
                continue
            break

        history.append({"role": "assistant", "content": text})
        history = self._trim_history(history)

        await self.context.conversation_manager.update_conversation(
            unified_msg_origin=umo,
            conversation_id=conv_id,
            history=history,
        )

        logger.debug(f"LLM回复成功{round_label}（{len(contexts)}条上下文，{len(text)}字）")
        return text

    # ==================== 主动回复模块 ====================

    async def initialize(self):
        config = self._get_config()
        if config.get("proactive_reply_enabled", False):
            self._start_proactive_reply()

    def _get_local_now(self) -> datetime:
        """Return the configured wall-clock time used by the plugin.

        Returns:
            Server time shifted by proactive_reply_timezone_offset.
        """
        offset = int(self._get_config().get("proactive_reply_timezone_offset", 8))
        return datetime.now() + timedelta(hours=offset)

    def _is_in_active_time_window(self):
        config = self._get_config()
        current_hour = self._get_local_now().hour
        start_hour = int(config.get("proactive_reply_start_hour", 9))
        end_hour = int(config.get("proactive_reply_end_hour", 23))

        if start_hour <= end_hour:
            return start_hour <= current_hour < end_hour
        else:
            return current_hour >= start_hour or current_hour < end_hour

    def _get_next_proactive_interval(self):
        config = self._get_config()
        min_interval = int(config.get("proactive_reply_min_interval", 1800))
        max_interval = int(config.get("proactive_reply_max_interval", 7200))
        return random.randint(min_interval, max_interval)

    def _schedule_next_proactive(self):
        interval = self._get_next_proactive_interval()
        self._proactive_scheduled_time = time.time() + interval

    async def _execute_proactive_reply(self):
        umo = self._admin_umo
        if not umo:
            logger.debug("尚未有管理员私聊过")
            return

        if not self._is_in_active_time_window():
            self._schedule_next_proactive()
            return

        history, _, _ = await self._get_history_list(umo)

        # Inspect the history tail: how many messages we already sent without an
        # answer, and what the other side said last.
        unanswered = 0
        last_user_text = ""
        for msg in reversed(history):
            role = msg.get("role")
            if role == "assistant":
                unanswered += 1
            elif role == "user":
                last_user_text = str(msg.get("content", "") or "").strip()
                break

        # A real person stops talking to a silent chat instead of stacking
        # messages, so give up after two unanswered messages.
        if unanswered >= 2:
            logger.debug("对方连续未回复，本轮主动联系跳过")
            self._schedule_next_proactive()
            return

        config = self._get_config()
        local_now = self._get_local_now()
        gap = time.time() - (self._last_user_activity_time or time.time())

        time_line = (
            f"现在是 {local_now.month} 月 {local_now.day} 日 "
            f"周{WEEKDAY_NAMES[local_now.weekday()]} {local_now:%H:%M}"
            f"（{_day_part(local_now.hour)}），"
            f"距对方上次说话已经过去 {_humanize_gap(gap)}。"
        )
        last_line = (
            f"对方最后发来的一句话：「{last_user_text[:120]}」"
            if last_user_text
            else "你们还没有互相说过话。"
        )

        if unanswered:
            situation = (
                "你上一条消息发出去后，对方至今没回。真人遇到这种情况通常会闭嘴，"
                "或者换个完全不相干的新由头轻轻碰一下，绝不会接着上一条往下说。"
            )
            silence_hint = "除非此刻真有值得一说的事，否则只输出 [SILENCE]。"
        elif gap >= 3 * 3600:
            situation = (
                "隔了这么久，你想像重新开口一样找对方说句话。之前的话题已经过去了，"
                "要说就说起新鲜的事，或者只是顺口问一句，不要接着上一轮的细节继续。"
            )
            silence_hint = "没什么想说的就只输出 [SILENCE]。"
        else:
            situation = "这段时间对方没说话，你想主动开口找对方说句什么。"
            silence_hint = "没什么想说的就只输出 [SILENCE]。"

        prompt = (
            "（舞台提示：这不是对方发来的消息，不要回应、复述或解释它。"
            "下面是此刻的情境，你直接写出你要发给对方的那条消息。）\n"
            f"{time_line}\n"
            f"{last_line}\n"
            f"{situation}\n"
            f"{silence_hint}"
        )

        extra_system = CONTACT_STYLE_RULES
        style_hint = str(config.get("proactive_style_hint", "") or "").strip()
        if style_hint:
            extra_system = f"{extra_system}\n\n# 额外要求\n{style_hint}"

        ai_message = await self._call_llm_with_context(
            umo,
            prompt,
            round_label=" [主动]",
            preloaded_history=history,
            extra_system=extra_system,
            style_guard=True,
        )

        if not ai_message:
            self._schedule_next_proactive()
            return

        # Never repeat the previous outgoing message; talking to a silent chat
        # with the same line twice is the loudest "not a person" signal.
        previous_text = ""
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                previous_text = str(msg.get("content", "") or "").strip()
                break
        if previous_text and (
            ai_message == previous_text
            or ai_message in previous_text
            or previous_text in ai_message
        ):
            logger.debug("主动回复与上一条雷同，跳过")
            self._schedule_next_proactive()
            return

        try:
            message_chain = MessageChain().message(ai_message)
            await self.context.send_message(umo, message_chain)
            self._schedule_next_proactive()
        except Exception as e:
            logger.error(f"主动回复发送失败: {e}")
            self._schedule_next_proactive()

    async def _proactive_reply_loop(self):
        while True:
            try:
                config = self._get_config()
                if not config.get("proactive_reply_enabled", False):
                    await asyncio.sleep(60)
                    continue

                if self._consecutive_failures >= self._max_consecutive_failures:
                    logger.warning(f"连续失败 {self._consecutive_failures} 次，暂停10分钟")
                    await asyncio.sleep(600)
                    self._consecutive_failures = 0
                    continue

                if not self._is_in_active_time_window():
                    await asyncio.sleep(300)
                    continue

                if self._last_user_activity_time is None:
                    self._last_user_activity_time = time.time()
                    self._schedule_next_proactive()
                    await asyncio.sleep(60)
                    continue

                now = time.time()
                scheduled = self._proactive_scheduled_time or (now + 3600)

                wait_time = scheduled - now
                if wait_time > 0:
                    await asyncio.sleep(min(wait_time, 300))
                    continue

                await self._execute_proactive_reply()
                self._consecutive_failures = 0

                await asyncio.sleep(60)

            except asyncio.CancelledError:
                logger.info("主动回复任务已取消")
                break
            except Exception as e:
                logger.error(f"主动回复循环异常: {e}")
                self._consecutive_failures += 1
                backoff_time = min(60 * (2 ** self._consecutive_failures), 300)
                await asyncio.sleep(backoff_time)

    def _start_proactive_reply(self):
        if self._proactive_reply_task and not self._proactive_reply_task.done():
            return

        self._last_user_activity_time = time.time()
        self._schedule_next_proactive()

        self._proactive_reply_task = asyncio.create_task(self._proactive_reply_loop())
        logger.info("主动回复模块已启动")

    def _stop_proactive_reply(self):
        if self._proactive_reply_task and not self._proactive_reply_task.done():
            self._proactive_reply_task.cancel()

    # ==================== 沉浸式对话延续模块 ====================

    def _should_trigger_followup(self, ai_text: str, history: list) -> bool:
        if not ai_text or len(ai_text.strip()) < 2:
            return False

        has_question = bool(_RE_QUESTION.search(ai_text))
        is_short_reply = len(ai_text.strip()) < 15 and not has_question

        if is_short_reply:
            return False

        if has_question or _RE_ELLIPSIS.search(ai_text):
            return True

        if _RE_CLOSING.search(ai_text):
            return False

        if history:
            recent_user_msgs = [
                msg for msg in history[-6:]
                if msg.get("role") == "user" and str(msg.get("content", "") or "").strip()
            ]
            if len(recent_user_msgs) >= 2:
                return True

        return False

    def _get_followup_prompt(self, round_num: int, max_rounds: int, ai_text: str) -> str:
        local_now = self._get_local_now()
        head = (
            "（舞台提示：这不是对方发来的消息，不要回应或复述它，"
            "直接写出你要发的下一条消息。）\n"
            f"现在是 {local_now:%H:%M}（{_day_part(local_now.hour)}），对方还没回你。\n"
            f"你刚才说的是：「{ai_text[:120]}」\n"
        )

        if round_num >= max_rounds:
            tail = (
                "这是最后一次机会，对方一直没回。真人这时候基本会停下来，"
                "没什么非说不可的就只输出 [SILENCE]。"
            )
        elif round_num == 1:
            tail = (
                "你可以补一句更具体的、换个话题、说说自己在干嘛，或者干脆不说。"
                "但不要重复刚才那句话的意思，也不要把那个话题继续往下讲。"
                "没什么想说的就只输出 [SILENCE]。"
            )
        else:
            tail = (
                "对方还是没回。真人这时候多半会放弃，"
                "除非真有全新的事，否则只输出 [SILENCE]。"
            )

        return head + tail

    def _reset_followup_state(self, admin_id: str):
        state_key = f"{admin_id}_followup"
        if state_key in self._followup_state:
            state = self._followup_state[state_key]
            if state.get("timer_task") and not state["timer_task"].done():
                state["timer_task"].cancel()
            self._followup_state[state_key]["round"] = 0

    @filter.on_decorating_result()
    async def on_decorating_result_dialogue_continuation(self, event: AstrMessageEvent):
        config = self._get_config()
        if not config.get("immersive_followup_enabled", False):
            return

        admin_id = config.get("admin_id", "")
        if not admin_id:
            return

        result = event.get_result()
        if not result or not result.is_llm_result():
            return

        sender_id = event.get_sender_id()
        if not sender_id:
            return

        if sender_id != admin_id:
            return

        try:
            text_content = ""
            if hasattr(result, 'chain') and result.chain:
                for comp in result.chain:
                    if hasattr(comp, 'text') and comp.text:
                        text_content += comp.text
            elif hasattr(result, 'text') and result.text:
                text_content = result.text

            if not text_content or len(text_content.strip()) < 2:
                return

            umo = self._admin_umo
            history, _, _ = await self._get_history_list(umo) if umo else ([], None, None)

            if not self._should_trigger_followup(text_content, history):
                return

            timeout = int(config.get("immersive_followup_timeout", 300))
            max_rounds = int(config.get("immersive_followup_max_rounds", 3))
            captured_timeout = timeout
            captured_max_rounds = max_rounds
            captured_ai_text = text_content

            state_key = f"{admin_id}_followup"
            current_state = self._followup_state.get(state_key, {
                "round": 0,
                "timer_task": None,
                "last_ai_text": ""
            })

            if current_state["round"] >= max_rounds:
                return

            if current_state["timer_task"] and not current_state["timer_task"].done():
                current_state["timer_task"].cancel()

            current_state["round"] = 0
            current_state["last_ai_text"] = text_content

            async def followup_callback():
                await asyncio.sleep(captured_timeout)

                if not self._get_config().get("immersive_followup_enabled", False):
                    return

                state = self._followup_state.get(state_key)
                if not state:
                    return

                if state["round"] >= captured_max_rounds:
                    return

                state["round"] += 1
                round_num = state["round"]

                umo = self._admin_umo

                latest_ai_text = state.get("last_ai_text") or captured_ai_text
                followup_prompt = self._get_followup_prompt(
                    round_num=round_num,
                    max_rounds=captured_max_rounds,
                    ai_text=latest_ai_text,
                )

                round_label = f" [延续{round_num}/{captured_max_rounds}]"
                continuation_text = await self._call_llm_with_context(
                    umo=umo,
                    prompt=followup_prompt,
                    round_label=round_label,
                    extra_system=CONTACT_STYLE_RULES,
                    style_guard=True,
                )

                if continuation_text:
                    if not self._should_trigger_followup(continuation_text, []):
                        logger.debug(f"对话延续{round_label}: AI回复不满足继续追问条件，停止")
                        return

                    try:
                        message_chain = MessageChain().message(continuation_text)
                        await self.context.send_message(umo, message_chain)
                        state["last_ai_text"] = continuation_text
                    except Exception as e:
                        logger.error(f"对话延续发送失败: {e}")
                        return

                if state["round"] < captured_max_rounds:
                    state["timer_task"] = asyncio.create_task(followup_callback())

            current_state["timer_task"] = asyncio.create_task(followup_callback())
            self._followup_state[state_key] = current_state

        except Exception as e:
            logger.error(f"沉浸式对话延续处理异常: {e}")

    # ==================== 管理命令 ====================

    @filter.command("zhudong")
    async def zhudong_command(self, event: AstrMessageEvent):
        msg = event.message_str.lower().strip()

        if "状态" in msg or "status" in msg:
            config = self._get_config()
            proactive_status = "开启" if config.get("proactive_reply_enabled") else "关闭"
            followup_status = "开启" if config.get("immersive_followup_enabled") else "关闭"

            active_window = f"{config.get('proactive_reply_start_hour', 9)}:00-{config.get('proactive_reply_end_hour', 23)}:00"
            provider_id = config.get("llm_provider_id", "") or "(自动检测)"

            info = (
                f"📊 主动私聊插件状态\n"
                f"━━━━━━━━━━━━━━\n"
                f"🔔 主动回复(AI自生成+上下文): {proactive_status}\n"
                f"   时间窗口: {active_window}\n"
                f"   LLM模型: {provider_id}\n"
                f"💬 沉浸式对话延续(AI自生成+上下文): {followup_status}\n"
                f"   延续超时: {config.get('immersive_followup_timeout', 300)}秒\n"
                f"   最大轮次: {config.get('immersive_followup_max_rounds', 3)}\n"
                f"━━━━━━━━━━━━━━\n"
                f"管理员ID: {config.get('admin_id', '未配置')}\n"
                f"告状功能: {'开启' if config.get('enable_sue') else '关闭'}\n"
                f"自定义报错: {'开启' if config.get('enable_custom_error') else '关闭'}"
            )
            yield event.plain_result(info)
        elif "测试" in msg or "test" in msg:
            admin_id = self._get_config().get("admin_id", "")
            if admin_id:
                success = await self.send_private_message(admin_id, "🧪 这是一条来自主动私聊插件的测试消息")
                if success:
                    yield event.plain_result("✅ 测试消息已发送给管理员")
                else:
                    yield event.plain_result("❌ 测试消息发送失败")
            else:
                yield event.plain_result("❌ 管理员ID未配置，无法发送测试消息")
        elif "对话延续" in msg or "延续" in msg or "followup" in msg:
            if "开" in msg or "启" in msg or "on" in msg:
                yield event.plain_result("ℹ️ 沉浸式对话延续请在 WebUI 中通过配置开关控制\n命令仅支持查看状态")
            elif "关" in msg or "停" in msg or "off" in msg:
                yield event.plain_result("ℹ️ 沉浸式对话延续请在 WebUI 中通过配置开关控制\n命令仅支持查看状态")
            else:
                yield event.plain_result("💬 使用方法: /zhudong 对话延续on/off 或 /zhudong 状态")
        else:
            help_text = (
                "📌 主动私聊插件命令\n"
                "━━━━━━━━━━━━━━\n"
                "/zhudong 状态     查看所有功能状态\n"
                "/zhudong 测试     向管理员发送测试消息\n"
                "/zhudong 对话延续on/off  提示配置对话延续开关\n"
                "\n"
                "💡 功能说明:\n"
                "• 🔔 主动回复: 定时调用LLM让AI基于完整会话上下文自己决定说什么\n"
                "• 💬 沉浸式对话延续: AI发言后超时则基于完整上下文自己组织延续语言（不限于追问）\n"
                "• 🔒 所有新功能均锁定管理员ID，不会对其他人触发\n"
                "• 🧠 AI拥有完整对话记忆，知道你们聊过什么\n"
                "• 🤫 AI可以选择'保持沉默'而不发送消息\n"
                "\n"
                "⚙️ 详细配置请在 WebUI 中修改"
            )
            yield event.plain_result(help_text)

    # ==================== 用户活动监听 ====================

    @filter.on_decorating_result()
    async def on_user_message_reset_proactive(self, event: AstrMessageEvent):
        config = self._get_config()
        proactive_enabled = config.get("proactive_reply_enabled", False)
        followup_enabled = config.get("immersive_followup_enabled", False)

        if not proactive_enabled and not followup_enabled:
            return

        admin_id = config.get("admin_id", "")
        if not admin_id:
            return

        sender_id = str(event.get_sender_id() or "")
        if sender_id != admin_id:
            return

        if event.is_private_chat():
            self._admin_umo = event.unified_msg_origin
            self._save_admin_umo()

        if proactive_enabled:
            self._last_user_activity_time = time.time()
            self._schedule_next_proactive()

        if followup_enabled:
            self._reset_followup_state(admin_id)

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event: AstrMessageEvent, tool, tool_args):
        config = self._get_config()
        proactive_enabled = config.get("proactive_reply_enabled", False)
        followup_enabled = config.get("immersive_followup_enabled", False)

        if proactive_enabled:
            self._last_user_activity_time = time.time()

        if followup_enabled:
            admin_id = config.get("admin_id", "")
            if admin_id:
                self._reset_followup_state(admin_id)

    async def terminate(self):
        self._stop_proactive_reply()
        self._followup_state.clear()
        self.message_rate_limit.clear()
        self._config_cache = None
        logger.info("主动私聊插件已停止")
