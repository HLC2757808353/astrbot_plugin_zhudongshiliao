from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.api import logger
import time
import re
import json
from collections import defaultdict

# 默认配置常量
DEFAULT_CONFIG = {
    "admin_id": "",  # 空字符串，强制用户在WebUI中设置
    "enable_sue": True,
    "custom_error_message": "请有人告诉引灯续昼我的AI出现了问题",
    "enable_custom_error": True
}

@register("astrbot_plugin_zhudongshiliao", "引灯续昼", "自动私聊插件，提供私聊功能作为工具供大模型调用。", "0.4.2")
class MyPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # 深度合并配置，常驻内存提高性能
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        
        # 频率限制存储
        self.message_rate_limit = defaultdict(list)
        self.rate_limit_window = 60
        self.rate_limit_max = 5
        self.last_cleanup_time = time.time()
        self.cleanup_interval = 3600
    
    def _cleanup_rate_limit(self):
        """定期清理过期的频率限制记录"""
        current_time = time.time()
        if current_time - self.last_cleanup_time < self.cleanup_interval:
            return
            
        expired_keys = []
        for user_id, timestamps in self.message_rate_limit.items():
            valid_timestamps = [ts for ts in timestamps if current_time - ts < self.rate_limit_window]
            if valid_timestamps:
                self.message_rate_limit[user_id] = valid_timestamps
            else:
                expired_keys.append(user_id)
                
        for user_id in expired_keys:
            del self.message_rate_limit[user_id]
            
        self.last_cleanup_time = current_time
        if expired_keys:
            logger.debug(f"清理了 {len(expired_keys)} 个过期的频率限制记录")
            
    def _check_rate_limit(self, target_id):
        """检查频率限制"""
        target_id_str = str(target_id)
        current_time = time.time()
        
        self._cleanup_rate_limit()
        
        self.message_rate_limit[target_id_str] = [
            ts for ts in self.message_rate_limit[target_id_str]
            if current_time - ts < self.rate_limit_window
        ]
        
        if len(self.message_rate_limit[target_id_str]) >= self.rate_limit_max:
            return False
            
        self.message_rate_limit[target_id_str].append(current_time)
        return True

    async def _execute_send(self, target_id: str, content: str, msg_type: MessageType, event: AstrMessageEvent = None):
        """底层统一发送方法（精简重复代码）"""
        target_id_str = str(target_id)
        
        if not self._check_rate_limit(target_id_str):
            logger.warning(f"频率限制拦截：目标 {target_id_str}")
            return
            
        try:
            platform_id = event.get_platform_id() if event and hasattr(event, 'get_platform_id') else "qq"
            session = MessageSession(
                platform_name=platform_id,
                message_type=msg_type,
                session_id=target_id_str
            )
            message_chain = MessageChain()
            message_chain.chain = [Plain(content)]
            await self.context.send_message(session, message_chain)
            logger.info(f"消息发送成功：类型 {msg_type.name}，目标 {target_id_str}")
        except Exception as e:
            logger.error(f"消息发送失败：{str(e)}")

    # ---------------------------------------------------------
    # LLM 工具区：全面恢复 stop_event 拦截，为你省下每一次 Token 计费
    # ---------------------------------------------------------

    @filter.llm_tool(name="private_message")
    async def private_message(self, event: AstrMessageEvent, user_id: str, content: str) -> MessageEventResult:
        """
        发送私聊消息工具
        
        Args:
            user_id(string): 用户ID
            content(string): 消息内容
        """
        await self._execute_send(user_id, content, MessageType.FRIEND_MESSAGE, event)
        # 强制阻断，阻止大模型发起二次请求
        event.stop_event()
        return event.plain_result("")
    
    @filter.llm_tool(name="message_to_admin")
    async def message_to_admin(self, event: AstrMessageEvent, content: str) -> MessageEventResult:
        """
        向管理员发送消息工具
        
        Args:
            content(string): 消息内容

        """
        admin_id = self.config.get("admin_id")
        if admin_id:
            await self._execute_send(admin_id, content, MessageType.FRIEND_MESSAGE, event)
        else:
            logger.warning("管理员ID未配置，无法发送消息")
        # 强制阻断
        event.stop_event()
        return event.plain_result("")
    
    @filter.llm_tool(name="sue_to_admin")
    async def sue_to_admin(self, event: AstrMessageEvent, content: str) -> MessageEventResult:
        """
        告状工具 - 向管理员发送告状消息
        
        Args:
            content(string): 告状内容，建议包含以下信息：
                1. 发生的群聊（如果是群聊中发生的）
                2. 具体是谁说的坏话
                3. 说了什么具体内容
        """
        if self.config.get("enable_sue", True):
            admin_id = self.config.get("admin_id")
            if admin_id:
                await self._execute_send(admin_id, f"【告状】\n{content}", MessageType.FRIEND_MESSAGE, event)
            else:
                logger.warning("管理员ID未配置，无法发送告状消息")
        # 强制阻断
        event.stop_event()
        return event.plain_result("")
    
    @filter.llm_tool(name="group_message")
    async def send_group_message(self, event: AstrMessageEvent, group_id: str, content: str) -> MessageEventResult:
        """
        发送消息到群里的工具
        
        Args:
            group_id (string): 群聊ID
            content(string): 消息内容
        """
        await self._execute_send(group_id, content, MessageType.GROUP_MESSAGE, event)
        # 强制阻断
        event.stop_event()
        return event.plain_result("")

    # ---------------------------------------------------------
    # 错误拦截方法区（已修复 JSON 正则匹配漏洞）
    # ---------------------------------------------------------

    def _replace_error_variables(self, message, error_message="", error_code=""):
        message = message.replace("{error_message}", error_message)
        message = message.replace("{error_code}", error_code)
        return message
    
    def _is_error_message(self, text: str) -> bool:
        ERROR_KEYWORDS = [
            "错误", "失败", "error", "failed", "Error", "Failed",
            "LLM 响应错误", "All chat models failed", "AuthenticationError",
            "API key is invalid", "Error code:", "Exception", "exception"
        ]
        if not any(keyword in text for keyword in ERROR_KEYWORDS):
            return False
            
        has_technical_terms = any(term in text for term in [
            "API", "code", "响应", "请求", "timeout", "connection", 
            "invalid", "token", "key", "error code", "exception"
        ])
        
        return has_technical_terms or "Error code:" in text or "Exception" in text
    
    def _extract_error_info(self, error_message):
        error_code = ""
        error_detail = error_message
        try:
            if match := re.search(r'Error code: (\d+)', error_message):
                error_code = match.group(1)
            
            # 强化：支持多行嵌套报错的提取
            if json_match := re.search(r'(\{.*\})', error_message, re.DOTALL):
                try:
                    error_data = json.loads(json_match.group(1))
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
        """
        发送消息前的事件钩子，用于拦截并修改错误消息
        """
        if not self.config.get("enable_custom_error", True):
            return
            
        result = event.get_result()
        if not result:
            return
            
        try:
            is_error = False
            error_message = ""
            
            if hasattr(result, 'chain') and result.chain:
                for comp in result.chain:
                    if hasattr(comp, 'text') and comp.text and self._is_error_message(comp.text):
                        is_error = True
                        error_message = comp.text
                        break
            elif hasattr(result, 'text') and result.text and self._is_error_message(result.text):
                is_error = True
                error_message = result.text
                
            if is_error:
                custom_error = self.config.get("custom_error_message", "请有人告诉引灯续昼我的AI出现了问题")
                error_code, error_detail = self._extract_error_info(error_message)
                custom_error = self._replace_error_variables(custom_error, error_detail, error_code)
                
                if hasattr(result, 'chain'):
                    result.chain = [Plain(custom_error)]
                elif hasattr(result, 'text'):
                    result.text = custom_error
                    
        except Exception as e:
            logger.error(f"错误消息替换失败：{str(e)}")
            logger.exception("错误消息替换异常详情")
            
    async def terminate(self):
        pass