from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.agent.message import Message
from astrbot.api import logger
import asyncio
import time
import re
import json
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
    "immersive_followup_enabled": False,
    "immersive_followup_timeout": 300,
    "immersive_followup_max_rounds": 3,
}


@register("astrbot_plugin_zhudongshiliao", "引灯续昼", "自动私聊插件，提供私聊、AI主动回复和沉浸式对话延续功能。", "0.6.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self.message_rate_limit = defaultdict(list)
        self.rate_limit_window = 60
        self.rate_limit_max = 5
        self.last_cleanup_time = time.time()
        self.cleanup_interval = 3600
        
        self._proactive_reply_task = None
        self._last_user_activity_time = None
        self._proactive_scheduled_time = None
        
        self._followup_state = {}
        
        self._lock = asyncio.Lock()
        
        self._admin_umo = None

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
        if expired_keys:
            logger.debug(f"清理了 {len(expired_keys)} 个过期的频率限制记录")

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
        
        self.message_rate_limit[user_id_str].append(current_time)
        return True

    def _get_first_platform_id(self) -> str | None:
        """获取第一个可用的平台适配器ID"""
        try:
            if self.context.platform_manager and self.context.platform_manager.platform_insts:
                return self.context.platform_manager.platform_insts[0].meta().id
        except Exception as e:
            logger.error(f"获取平台ID失败: {e}")
        return None

    async def send_private_message(self, user_id, message, event=None, source_context=None):
        if not user_id or not message:
            return False
            
        user_id_str = str(user_id).strip()
        if not user_id_str:
            return False
        
        if not self._check_rate_limit(user_id_str):
            logger.warning(f"私聊频率限制：用户 {user_id_str} 发送消息过于频繁")
            return False
        
        logger.info(f"发送私聊消息：目标用户 {user_id_str}，消息长度 {len(message)}")
        
        try:
            platform_id = None
            if event and hasattr(event, 'get_platform_id'):
                platform_id = event.get_platform_id()
            
            if not platform_id:
                platform_id = self._get_first_platform_id()
            
            if not platform_id:
                logger.error("无法获取平台ID，消息发送失败")
                return False
            
            if source_context and self.context.conversation_manager:
                await self._inject_cross_session_context(
                    target_user_id=user_id_str,
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
            logger.info(f"私聊消息发送成功：目标用户 {user_id_str}")
            return True
        except Exception as e:
            logger.error(f"发送私聊消息失败：{str(e)}")
            logger.exception("发送私聊消息失败详情")
            return False

    async def _inject_cross_session_context(
        self,
        target_user_id: str,
        platform_id: str,
        source_context: dict,
        message_content: str,
    ):
        if not self.context.conversation_manager:
            return
        
        try:
            target_umo = f"{platform_id}:friend:{target_user_id}"
            
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(target_umo)
            
            if not conv_id:
                conv_id = await self.context.conversation_manager.new_conversation(
                    unified_msg_origin=target_umo,
                    platform_id=platform_id,
                )
                logger.debug(f"为目标用户 {target_user_id} 创建新会话: {conv_id}")
            
            conv = await self.context.conversation_manager.get_conversation(target_umo, conv_id)
            
            history = []
            if conv and conv.history:
                history = json.loads(conv.history)
            
            source_type = source_context.get("source_type", "群聊")
            source_name = source_context.get("source_name", "未知")
            sender_name = source_context.get("sender_name", "某人")
            recent_messages = source_context.get("recent_messages", [])
            
            context_parts = []
            if recent_messages:
                for msg in recent_messages[-10:]:
                    role = "用户" if msg.get("role") == "user" else "AI"
                    content = msg.get("content", "")[:500]
                    if content:
                        context_parts.append(f"{role}: {content}")
            
            recent_context = "\n".join(context_parts) if context_parts else "（无最近对话）"
            
            system_content = f"""【跨会话消息通知 - 重要上下文】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 消息来源: {source_type}「{source_name}」
👤 发送者: {sender_name}
💬 发送的消息内容:
{message_content}

📜 来源会话的最近对话记录（共{len(context_parts)}条）:
{recent_context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【重要说明】
上面的消息是由「{sender_name}」在「{source_name}」中让你发送给当前用户的。
这是真实的对话上下文，请记住这些内容。

如果当前用户问起：
- "谁让你发的？" → 告诉他是「{sender_name}」让你发的
- "为什么发这个？" → 根据上面的对话记录解释原因
- "发生什么事了？" → 根据来源会话的上下文回答

请像真正参与了那边的对话一样，自然地回应当前用户。"""

            system_message = {
                "role": "system",
                "content": system_content,
            }
            
            history.append(system_message)
            
            if recent_messages:
                for msg in recent_messages[-6:]:
                    if msg.get("role") in ("user", "assistant"):
                        history.append({
                            "role": msg["role"],
                            "content": msg.get("content", ""),
                        })
            
            await self.context.conversation_manager.update_conversation(
                unified_msg_origin=target_umo,
                conversation_id=conv_id,
                history=history,
            )
            
            logger.info(f"已向目标会话 {target_umo} 注入跨会话上下文（含{len(context_parts)}条对话记录）")
            
        except Exception as e:
            logger.error(f"注入跨会话上下文失败: {e}")
            logger.exception("注入跨会话上下文失败详情")

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
                        message_type_str = "group" if message_type == MessageType.GROUP_MESSAGE else "friend"
                        umo = f"{platform_id}:{message_type_str}:{session_id}"
                        
                        conv_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
                        if conv_id:
                            conv = await self.context.conversation_manager.get_conversation(umo, conv_id)
                            if conv and conv.history:
                                history = json.loads(conv.history)
                                source_context["recent_messages"] = history[-10:]
                    except Exception as e:
                        logger.debug(f"获取来源会话上下文失败: {e}")
        
        except Exception as e:
            logger.debug(f"构建来源上下文失败: {e}")
        
        return source_context

    def _get_config(self):
        try:
            config = self.config
            if not isinstance(config, dict):
                return DEFAULT_CONFIG.copy()
            config = config.copy()
            for key, value in DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = value
            return config
        except Exception:
            return DEFAULT_CONFIG.copy()

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
            logger.warning("管理员ID未配置，无法发送告状消息")
            return
        
        source_context = await self._build_source_context(event)
        source_context["is_sue"] = True
        
        success = await self.send_private_message(admin_id, f"【告状】\n{content}", event, source_context)
        if success:
            logger.info(f"告状消息已静默发送给管理员: {admin_id}")

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
            logger.warning(f"群消息频率限制：群 {group_id} 发送消息过于频繁")
            return "群消息发送失败：发送过于频繁，请稍后再试"
        
        logger.info(f"发送群消息：目标群 {group_id}，消息长度 {len(content)}")
        
        try:
            group_id_str = str(group_id)
            
            platform_id = None
            if event and hasattr(event, 'get_platform_id'):
                platform_id = event.get_platform_id()
            
            if not platform_id:
                platform_id = self._get_first_platform_id()
            
            if not platform_id:
                logger.error("无法获取平台ID，群消息发送失败")
                return "群消息发送失败：无法获取平台ID"
            
            source_context = await self._build_source_context(event)
            source_context["target_type"] = "群聊"
            
            if self.context.conversation_manager:
                await self._inject_cross_session_context_to_group(
                    target_group_id=group_id_str,
                    platform_id=platform_id,
                    source_context=source_context,
                    message_content=content,
                )
            
            session = MessageSession(
                platform_name=platform_id,
                message_type=MessageType.GROUP_MESSAGE,
                session_id=group_id_str
            )
            message_chain = MessageChain()
            message_chain.chain = [Plain(content)]
            await self.context.send_message(session, message_chain)
            logger.info(f"群消息发送成功：目标群 {group_id_str}")
            return "群消息发送成功"
            
        except (AttributeError, TypeError) as e:
            logger.error(f"群消息发送失败：{str(e)}")
            logger.exception("群消息发送失败详情")
            return "群消息发送失败：系统暂时无法发送消息，请稍后再试"
        except Exception as e:
            logger.error(f"群消息发送失败：{str(e)}")
            logger.exception("群消息发送失败详情")
            return "群消息发送失败：系统暂时无法发送消息，请稍后再试"

    async def _inject_cross_session_context_to_group(
        self,
        target_group_id: str,
        platform_id: str,
        source_context: dict,
        message_content: str,
    ):
        if not self.context.conversation_manager:
            return
        
        try:
            target_umo = f"{platform_id}:group:{target_group_id}"
            
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(target_umo)
            
            if not conv_id:
                conv_id = await self.context.conversation_manager.new_conversation(
                    unified_msg_origin=target_umo,
                    platform_id=platform_id,
                )
                logger.debug(f"为目标群 {target_group_id} 创建新会话: {conv_id}")
            
            conv = await self.context.conversation_manager.get_conversation(target_umo, conv_id)
            
            history = []
            if conv and conv.history:
                history = json.loads(conv.history)
            
            source_type = source_context.get("source_type", "群聊")
            source_name = source_context.get("source_name", "未知")
            sender_name = source_context.get("sender_name", "某人")
            recent_messages = source_context.get("recent_messages", [])
            
            context_parts = []
            if recent_messages:
                for msg in recent_messages[-10:]:
                    role = "用户" if msg.get("role") == "user" else "AI"
                    content = msg.get("content", "")[:500]
                    if content:
                        context_parts.append(f"{role}: {content}")
            
            recent_context = "\n".join(context_parts) if context_parts else "（无最近对话）"
            
            system_content = f"""【跨会话消息通知 - 重要上下文】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 消息来源: {source_type}「{source_name}」
👤 发送者: {sender_name}
💬 发送的消息内容:
{message_content}

📜 来源会话的最近对话记录（共{len(context_parts)}条）:
{recent_context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【重要说明】
上面的消息是由「{sender_name}」在「{source_name}」中让你发送到当前群聊的。
这是真实的对话上下文，请记住这些内容。

如果群里有人问起：
- "谁让你发的？" → 告诉他是「{sender_name}」让你发的
- "为什么发这个？" → 根据上面的对话记录解释原因
- "发生什么事了？" → 根据来源会话的上下文回答

请像真正参与了那边的对话一样，自然地回应群里的成员。"""

            system_message = {
                "role": "system",
                "content": system_content,
            }
            
            history.append(system_message)
            
            if recent_messages:
                for msg in recent_messages[-6:]:
                    if msg.get("role") in ("user", "assistant"):
                        history.append({
                            "role": msg["role"],
                            "content": msg.get("content", ""),
                        })
            
            await self.context.conversation_manager.update_conversation(
                unified_msg_origin=target_umo,
                conversation_id=conv_id,
                history=history,
            )
            
            logger.info(f"已向目标群会话 {target_umo} 注入跨会话上下文（含{len(context_parts)}条对话记录）")
            
        except Exception as e:
            logger.error(f"注入群会话跨会话上下文失败: {e}")
            logger.exception("注入群会话跨会话上下文失败详情")

    def _replace_error_variables(self, message, error_message="", error_code=""):
        message = message.replace("{error_message}", error_message)
        message = message.replace("{error_code}", error_code)
        return message

    def _is_error_message(self, text: str) -> bool:
        STRICT_ERROR_PATTERNS = [
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
        ]
        
        for pattern in STRICT_ERROR_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        
        has_error_keyword = any(kw in text for kw in [
            "LLM 响应错误", "All chat models failed",
            "AstrBot 请求失败", "错误类型"
        ])
        if has_error_keyword:
            return True
        
        has_exception_marker = (
            ("Exception" in text or "exception" in text or "Traceback" in text)
            and any(term in text for term in ["File ", "line ", "at ", "raise "])
        )
        if has_exception_marker:
            return True
        
        return False

    def _extract_error_info(self, error_message):
        error_code = ""
        error_detail = error_message
        
        try:
            outer_match = re.search(r'Error code: (\d+)', error_message)
            if outer_match:
                error_code = outer_match.group(1)
            
            if '{' in error_message:
                start_pos = error_message.find('{')
                brace_count = 1
                end_pos = start_pos + 1
                
                for char in error_message[end_pos:]:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            break
                    end_pos += 1
                
                if brace_count == 0:
                    json_str = error_message[start_pos:end_pos + 1]
                    try:
                        error_data = json.loads(json_str)
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
            logger.error(f"错误消息替换失败：{str(e)}")
            logger.exception("错误消息替换异常详情")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        config = self._get_config()
        if not config.get("enable_custom_error", True):
            return
        
        if response.role != "err":
            return
        
        logger.info(f"检测到 LLM 错误响应: {response.completion_text[:100] if response.completion_text else '无内容'}...")
        
        custom_error = config.get("custom_error_message", "请有人告诉引灯续昼我的AI出现了问题")
        
        error_code, error_detail = self._extract_error_info(response.completion_text or "")
        custom_error = self._replace_error_variables(custom_error, error_detail, error_code)
        
        response.completion_text = custom_error
        
        logger.info(f"已将 LLM 错误响应替换为: {custom_error}")

    # ==================== LLM 调用层 ====================

    def _get_admin_umo(self) -> str | None:
        config = self._get_config()
        admin_id = config.get("admin_id", "")
        if not admin_id:
            return None
        
        platform_id = self._get_first_platform_id()
        if not platform_id:
            logger.warning("无法获取平台ID，无法构建管理员UMO")
            return None
        
        return f"{platform_id}:friend:{admin_id}"

    async def _get_admin_conversation_contexts(self, max_messages: int = 30) -> list[Message]:
        umo = self._get_admin_umo()
        if not umo:
            return []
        
        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not conv_id:
                logger.debug(f"管理员会话 {umo} 没有找到对话记录")
                return []
            
            conv = await self.context.conversation_manager.get_conversation(umo, conv_id)
            if not conv or not conv.history:
                return []
            
            history = json.loads(conv.history)
            
            recent_history = history[-max_messages:] if len(history) > max_messages else history
            
            contexts = []
            for msg in recent_history:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ("user", "assistant", "system") and content:
                    contexts.append(Message(role=role, content=content))
            
            logger.debug(f"获取到管理员会话上下文: {len(contexts)} 条消息")
            return contexts
        except Exception as e:
            logger.error(f"获取管理员会话上下文失败: {e}")
            return []

    def _get_llm_provider_id(self) -> str | None:
        providers = self.context.get_all_providers()
        if providers:
            chosen = providers[0]
            logger.info(f"自动选择 LLM Provider: {chosen.meta().id} ({chosen.meta().model})")
            return chosen.meta().id
        
        logger.error("没有找到可用的 LLM Provider，无法生成AI回复")
        return None

    async def _get_persona_prompt(self, umo: str, conv) -> str:
        """获取当前会话的人格设定"""
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
        except Exception as e:
            logger.debug(f"获取人格设定失败: {e}")
        
        try:
            default_persona = await self.context.persona_manager.get_default_persona_v3(umo)
            if default_persona and default_persona.get("prompt"):
                return default_persona["prompt"]
        except Exception as e:
            logger.debug(f"获取默认人格设定失败: {e}")
        
        return ""

    async def _call_llm_for_proactive(self) -> str | None:
        umo = self._admin_umo
        if not umo:
            logger.warning("尚未获取到管理员的会话UMO，无法生成主动回复。请先与管理员私聊一次。")
            return None
        
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo)
        except Exception as e:
            logger.warning(f"无法获取当前会话的聊天模型ID: {e}，尝试自动选择")
            provider_id = self._get_llm_provider_id()
        
        if not provider_id:
            logger.error("无法获取聊天模型ID，无法生成主动回复")
            return None
        
        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not conv_id:
                logger.warning("未找到管理员的会话，无法生成主动回复")
                return None
            
            conv = await self.context.conversation_manager.get_conversation(umo, conv_id)
            history = []
            if conv and conv.history:
                history = json.loads(conv.history)
            
            persona_prompt = await self._get_persona_prompt(umo, conv)
            
            already_reached_out = False
            for msg in history[-3:]:
                if msg.get("role") == "assistant":
                    already_reached_out = True
                    break
            
            if already_reached_out:
                system_prompt = persona_prompt + "\n\n系统提示：你之前主动联系了对方，但对方还是没有理你。你可以再试试，或者换个方式，也可以选择不再打扰。保持你自己的说话风格。"
            else:
                system_prompt = persona_prompt + "\n\n系统提示：对方已经很久没有搭理你了，你现在要主动去找一下对方。保持你自己的说话风格，自然地发一条消息就行。"
            
            contexts = []
            for msg in history:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    contexts.append(Message(role=role, content=content))
            
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt="主动联系对方",
                contexts=contexts,
                system_prompt=system_prompt,
            )
            
            if response.completion_text:
                text = response.completion_text.strip()
                
                silence_markers = ["保持沉默", "不说话", "不打扰", "安静", "[SILENCE]", "[QUIET]"]
                if any(marker in text for marker in silence_markers):
                    logger.info("AI选择保持沉默，本次不发送主动回复")
                    return None
                
                history.append({
                    "role": "assistant",
                    "content": text,
                })
                
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=umo,
                    conversation_id=conv_id,
                    history=history,
                )
                
                logger.info(f"LLM 生成主动回复成功（含{len(contexts)}条上下文），长度: {len(text)}")
                return text
            else:
                logger.warning("LLM 生成的主动回复为空")
                return None
        except Exception as e:
            logger.error(f"调用 LLM 生成主动回复失败: {e}")
            logger.exception("调用 LLM 生成主动回复失败详情")
            return None

    async def _call_llm_for_dialogue_continuation(self, last_ai_message: str, current_round: int, max_rounds: int) -> str | None:
        umo = self._admin_umo
        if not umo:
            logger.warning("尚未获取到管理员的会话UMO，无法生成对话延续")
            return None
        
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo)
        except Exception as e:
            logger.warning(f"无法获取当前会话的聊天模型ID: {e}，尝试自动选择")
            provider_id = self._get_llm_provider_id()
        
        if not provider_id:
            logger.error("无法获取聊天模型ID，无法生成对话延续")
            return None
        
        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not conv_id:
                return None
            
            conv = await self.context.conversation_manager.get_conversation(umo, conv_id)
            history = []
            if conv and conv.history:
                history = json.loads(conv.history)
            
            persona_prompt = await self._get_persona_prompt(umo, conv)
            
            system_prompt = persona_prompt + f"\n\n系统提示：对方没有接你的话，你要不要再说点什么？这是第{current_round}次追问（最多{max_rounds}次），越往后越简短。保持你自己的说话风格，自然就好。不想说就回复'保持沉默'。"
            
            contexts = []
            for msg in history:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    contexts.append(Message(role=role, content=content))
            
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt="接着说点什么",
                contexts=contexts,
                system_prompt=system_prompt,
            )
            
            if response.completion_text:
                text = response.completion_text.strip()
                
                silence_markers = ["保持沉默", "不说话", "不打扰", "安静", "[SILENCE]", "[QUIET]", "不再打扰", "算了"]
                if any(marker in text for marker in silence_markers):
                    logger.info(f"对话延续 [{current_round}/{max_rounds}]: AI选择保持沉默")
                    return None
                
                history.append({
                    "role": "assistant",
                    "content": text,
                })
                
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=umo,
                    conversation_id=conv_id,
                    history=history,
                )
                
                logger.info(f"LLM 生成对话延续 [{current_round}/{max_rounds}] 成功（含{len(contexts)}条上下文），长度: {len(text)}")
                return text
            else:
                logger.warning(f"LLM 生成的对话延续 [{current_round}/{max_rounds}] 为空")
                return None
        except Exception as e:
            logger.error(f"调用 LLM 生成对话延续失败: {e}")
            logger.exception("调用 LLM 生成对话延续失败详情")
            return None

    # ==================== 主动回复模块 ====================
    
    async def initialize(self):
        config = self._get_config()
        if config.get("proactive_reply_enabled", False):
            self._start_proactive_reply()

    def _is_in_active_time_window(self):
        from datetime import datetime
        now = datetime.now().hour
        config = self._get_config()
        start_hour = int(config.get("proactive_reply_start_hour", 9))
        end_hour = int(config.get("proactive_reply_end_hour", 23))
        
        if start_hour <= end_hour:
            return start_hour <= now < end_hour
        else:
            return now >= start_hour or now < end_hour

    def _get_next_proactive_interval(self):
        config = self._get_config()
        min_interval = int(config.get("proactive_reply_min_interval", 1800))
        max_interval = int(config.get("proactive_reply_max_interval", 7200))
        return random.randint(min_interval, max_interval)

    def _schedule_next_proactive(self):
        interval = self._get_next_proactive_interval()
        self._proactive_scheduled_time = time.time() + interval
        logger.info(f"主动回复已调度，将在 {interval // 60} 分钟后触发")

    async def _execute_proactive_reply(self):
        config = self._get_config()
        admin_id = config.get("admin_id", "")
        
        if not admin_id:
            logger.warning("主动回复跳过：管理员ID未配置")
            return
        
        if not self._is_in_active_time_window():
            logger.info("当前不在活跃时间窗口内，跳过本次主动回复")
            self._schedule_next_proactive()
            return
        
        umo = self._admin_umo
        if not umo:
            logger.warning("尚未获取到管理员的会话UMO，跳过主动回复")
            return
        
        logger.info("正在调用 LLM 生成主动回复内容...")
        ai_message = await self._call_llm_for_proactive()
        
        if not ai_message:
            logger.error("LLM 未能生成主动回复内容，跳过本次")
            return
        
        try:
            message_chain = MessageChain().message(ai_message)
            await self.context.send_message(umo, message_chain)
            logger.info(f"AI主动回复已发送给管理员: {admin_id}")
            self._schedule_next_proactive()
        except Exception as e:
            logger.error(f"AI主动回复发送失败: {e}")

    async def _proactive_reply_loop(self):
        while True:
            try:
                config = self._get_config()
                if not config.get("proactive_reply_enabled", False):
                    await asyncio.sleep(60)
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
                
                await asyncio.sleep(60)
                
            except asyncio.CancelledError:
                logger.info("主动回复任务已取消")
                break
            except Exception as e:
                logger.error(f"主动回复循环异常: {e}")
                await asyncio.sleep(60)

    def _start_proactive_reply(self):
        if self._proactive_reply_task and not self._proactive_reply_task.done():
            return
        
        self._last_user_activity_time = time.time()
        self._schedule_next_proactive()
        
        self._proactive_reply_task = asyncio.create_task(self._proactive_reply_loop())
        logger.info("主动回复模块已启动（AI自主生成模式）")

    def _stop_proactive_reply(self):
        if self._proactive_reply_task and not self._proactive_reply_task.done():
            self._proactive_reply_task.cancel()
            try:
                self._proactive_reply_task.result()
            except (asyncio.CancelledError, Exception):
                pass
            logger.info("主动回复模块已停止")

    # ==================== 沉浸式对话延续模块 ====================

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
            
            should_trigger_followup = self._should_trigger_followup(text_content)
            
            if should_trigger_followup:
                timeout = int(config.get("immersive_followup_timeout", 300))
                max_rounds = int(config.get("immersive_followup_max_rounds", 3))
                
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
                    try:
                        current_state["timer_task"].result()
                    except Exception:
                        pass
                
                current_state["round"] = 0
                current_state["last_ai_text"] = text_content
                
                async def followup_callback():
                    await asyncio.sleep(timeout)
                    
                    state = self._followup_state.get(state_key)
                    if not state:
                        return
                    
                    if state["round"] >= max_rounds:
                        return
                    
                    state["round"] += 1
                    round_num = state["round"]
                    
                    logger.info(f"沉浸式对话延续 [{round_num}/{max_rounds}] 触发，正在调用 LLM 生成...")
                    
                    continuation_text = await self._call_llm_for_dialogue_continuation(
                        last_ai_message=state["last_ai_message"],
                        current_round=round_num,
                        max_rounds=max_rounds,
                    )
                    
                    if continuation_text:
                        await self.send_private_message(admin_id, continuation_text)
                        state["last_ai_text"] = continuation_text
                    
                    if state["round"] < max_rounds:
                        new_timeout = int(config.get("immersive_followup_timeout", 300)) * (1 + round_num * 0.5)
                        state["timer_task"] = asyncio.create_task(followup_callback())
                
                current_state["timer_task"] = asyncio.create_task(followup_callback())
                self._followup_state[state_key] = current_state
                
        except Exception as e:
            logger.error(f"沉浸式对话延续处理异常: {e}")

    def _should_trigger_followup(self, ai_response_text: str) -> bool:
        question_patterns = [
            r'[？?]$',
            r'呢[？?\s,，。!！]*$',
            r'吗[？?\s,，。!！]*$',
            r'你觉得',
            r'你认为',
            r'怎么说',
            r'想不想',
            r'好不好',
            r'可以吗',
            r'是吧',
            r'对吧',
            r'对不对',
            r'是不是',
            r'有没有',
            r'能不能',
            r'会不会',
            r'还记得',
            r'知道吗',
            r'猜猜',
            r'你觉得呢',
            r'你怎么看',
            r'你的想法',
            r'告诉我',
            r'说说看',
            r'聊聊',
        ]
        
        text_stripped = ai_response_text.rstrip(' \n\r\t。！？')
        
        for pattern in question_patterns:
            if re.search(pattern, text_stripped, re.IGNORECASE):
                return True
        
        emotional_patterns = [
            r'(我有点|我觉得|我感觉|我其实)',
            r'(你知道吗|跟你说|说实话)',
            r'(有时候|其实|话说回来)',
            r'(突然想到|说起来)',
            r'(有点|蛮|挺|很).*?(难过|开心|担心|害怕|兴奋|感动|失落|期待)',
        ]
        
        for pattern in emotional_patterns:
            if re.search(pattern, ai_response_text, re.IGNORECASE):
                return len(ai_response_text) > 20
        
        return False

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
        """用户发消息时重置主动回复倒计时"""
        config = self._get_config()
        if not config.get("proactive_reply_enabled", False):
            return
        
        admin_id = config.get("admin_id", "")
        if not admin_id:
            return
        
        sender_id = str(event.get_sender_id() or "")
        if sender_id != admin_id:
            return
        
        if event.is_private_chat():
            self._admin_umo = event.unified_msg_origin
        
        self._last_user_activity_time = time.time()
        self._schedule_next_proactive()
        
        state_key = f"{admin_id}_followup"
        if state_key in self._followup_state:
            state = self._followup_state[state_key]
            if state.get("timer_task") and not state["timer_task"].done():
                state["timer_task"].cancel()
                try:
                    state["timer_task"].result()
                except Exception:
                    pass
            self._followup_state[state_key]["round"] = 0
            logger.debug("管理员发消息，主动回复倒计时已重置，对话延续计时器已重置")

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event: AstrMessageEvent, tool, tool_args):
        config = self._get_config()
        if config.get("proactive_reply_enabled", False):
            self._last_user_activity_time = time.time()
            
            admin_id = config.get("admin_id", "")
            if admin_id:
                state_key = f"{admin_id}_followup"
                if state_key in self._followup_state:
                    state = self._followup_state[state_key]
                    if state.get("timer_task") and not state["timer_task"].done():
                        state["timer_task"].cancel()
                        try:
                            state["timer_task"].result()
                        except Exception:
                            pass
                    self._followup_state[state_key]["round"] = 0
                    logger.debug(f"管理员有新活动，对话延续计时器已重置")

    async def terminate(self):
        self._stop_proactive_reply()
        self._followup_state.clear()
        logger.info("主动私聊插件已停止，所有后台任务已清理")
