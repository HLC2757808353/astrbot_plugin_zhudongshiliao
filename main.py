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
from collections import defaultdict, deque
from typing import Dict, Deque, Optional

# 默认配置常量
DEFAULT_CONFIG = {
    "admin_id": "",  # 空字符串，强制用户在WebUI中设置
    "enable_sue": True,
    "custom_error_message": "请有人告诉引灯续昼我的AI出现了问题",
    "enable_custom_error": True,
    "enable_tool_feedback": True  # 是否向大模型反馈工具执行结果
}

@register("astrbot_plugin_zhudongshiliao", "引灯续昼", "自动私聊插件，提供私聊功能作为工具供大模型调用。", "0.4.3")
class MyPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)
        # 深度合并配置，常驻内存提高性能
        self.config: Dict = {**DEFAULT_CONFIG, **(config or {})}
        
        # 频率限制存储
        self.message_rate_limit: Dict[str, Deque[float]] = defaultdict(deque)
        self.rate_limit_window: int = 60
        self.rate_limit_max: int = 5
        self.last_cleanup_time: float = time.time()
        self.cleanup_interval: int = 3600

    def _cleanup_rate_limit(self) -> None:
        """定期清理过期的频率限制记录"""
        current_time = time.time()
        if current_time - self.last_cleanup_time < self.cleanup_interval:
            return
            
        expired_keys: list[str] = []
        for user_id, timestamps in self.message_rate_limit.items():
            # 从队列左侧移除过期的时间戳
            while timestamps and current_time - timestamps[0] >= self.rate_limit_window:
                timestamps.popleft()
            
            if not timestamps:
                expired_keys.append(user_id)
                
        for user_id in expired_keys:
            del self.message_rate_limit[user_id]
            
        self.last_cleanup_time = current_time
        if expired_keys:
            logger.debug(f"清理了 {len(expired_keys)} 个过期的频率限制记录")

    def _check_rate_limit(self, target_id: str | int) -> bool:
        """检查频率限制"""
        target_id_str = str(target_id)
        current_time = time.time()
        
        self._cleanup_rate_limit()
        
        # 移除过期的时间戳
        timestamps = self.message_rate_limit[target_id_str]
        while timestamps and current_time - timestamps[0] >= self.rate_limit_window:
            timestamps.popleft()
        
        if len(timestamps) >= self.rate_limit_max:
            return False
            
        timestamps.append(current_time)
        return True

    async def _execute_send(self, target_id: str, content: str, msg_type: MessageType, event: AstrMessageEvent = None):
        """底层统一发送方法"""
        target_id_str = str(target_id)
        
        if not self._check_rate_limit(target_id_str):
            logger.warning(f"频率限制拦截：目标 {target_id_str}")
            return
            
        try:
            if not event or not hasattr(event, 'get_platform_id'):
                logger.error("无法获取平台 ID，消息发送失败")
                return
            
            platform_id = event.get_platform_id()
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
    # LLM 工具区：拦截大模型二次请求，节约费用
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
        if self.config.get("enable_tool_feedback", True):
            return event.plain_result(f"私聊消息已发送给用户 {user_id}")
        else:
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
            if self.config.get("enable_tool_feedback", True):
                return event.plain_result("消息已发送给管理员")
            else:
                return event.plain_result("")
        else:
            logger.warning("管理员ID未配置，无法发送消息")
            if self.config.get("enable_tool_feedback", True):
                return event.plain_result("管理员ID未配置，无法发送消息")
            else:
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
                if self.config.get("enable_tool_feedback", True):
                    return event.plain_result("告状消息已发送给管理员")
                else:
                    return event.plain_result("")
            else:
                logger.warning("管理员ID未配置，无法发送告状消息")
                if self.config.get("enable_tool_feedback", True):
                    return event.plain_result("管理员ID未配置，无法发送告状消息")
                else:
                    return event.plain_result("")
        else:
            if self.config.get("enable_tool_feedback", True):
                return event.plain_result("告状功能已禁用")
            else:
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
        if self.config.get("enable_tool_feedback", True):
            return event.plain_result(f"群消息已发送到群 {group_id}")
        else:
            return event.plain_result("")

    # ---------------------------------------------------------
    # 错误拦截方法区（按照 Code Review 意见深度重构）
    # ---------------------------------------------------------

    def _replace_error_variables(self, message: str, error_message: str = "", error_code: str = "") -> str:
        message = message.replace("{error_message}", error_message)
        message = message.replace("{error_code}", error_code)
        return message

    def _is_error_message(self, text: str) -> bool:
        """
        基于分级策略检测文本是否为报错信息
        """
        # 1. 强特征：只要包含这些词汇，直接判定为报错
        STRONG_KEYWORDS = [
            "LLM 响应错误", "All chat models failed", "AuthenticationError",
            "API key is invalid", "Error code:", "Exception:"
        ]
        if any(keyword in text for keyword in STRONG_KEYWORDS):
            return True
            
        # 2. 弱特征：日常用语中也会出现的错误词汇
        WEAK_KEYWORDS = ["错误", "失败", "error", "failed", "Exception", "exception"]
        has_weak_error = any(keyword in text for keyword in WEAK_KEYWORDS)
        
        if not has_weak_error:
            return False
            
        # 3. 上下文约束：包含弱特征的前提下，必须包含技术词汇才判定为报错
        TECH_TERMS = ["API", "code", "响应", "请求", "timeout", "connection", "invalid", "token", "key"]
        has_technical_terms = any(term in text for term in TECH_TERMS)
        
        return has_technical_terms

    def _extract_error_info(self, error_message: str) -> tuple[str, str]:
        """
        提取错误代码和详细信息，彻底规避正则性能陷阱
        """
        error_code: str = ""
        error_detail: str = error_message
        
        try:
            # 1. 提取错误代码 (轻量正则，无性能风险)
            if match := re.search(r'Error code:\s*(\d+)', error_message, re.IGNORECASE):
                error_code = match.group(1)
            
            # 2. 提取 JSON 数据 (使用大括号平衡法，确保提取完整的 JSON 对象)
            if '{' in error_message:
                # 找到第一个 '{' 作为 JSON 开始
                start_pos = error_message.find('{')
                brace_count = 1
                end_pos = start_pos + 1
                
                # 平衡大括号，找到匹配的结束位置
                for char in error_message[end_pos:]:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            break
                    end_pos += 1
                
                # 确保找到了匹配的大括号
                if brace_count == 0:
                    json_str = error_message[start_pos:end_pos + 1]
                    try:
                        error_data = json.loads(json_str)
                        if isinstance(error_data, dict):
                            # 如果外层没有找到 code，尝试从字典内部读取
                            error_code = str(error_data.get('code', error_code))
                            error_detail = error_data.get('message', error_detail)
                    except json.JSONDecodeError:
                        # JSON 解析失败属于正常情况（可能是无关大括号），用 debug 级别静默记录
                        logger.debug("报错文本中的 JSON 解析失败，跳过提取。", exc_info=True)
                    
        except Exception as e:
            # 移除裸 pass，将非预期的解析异常显式记录下来，避免掩盖潜在的 TypeError 等问题
            logger.debug(f"提取报错详细信息时发生异常: {e}", exc_info=True)
            
        return error_code, error_detail

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
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

    async def terminate(self) -> None:
        pass