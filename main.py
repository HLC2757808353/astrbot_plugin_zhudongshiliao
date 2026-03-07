import time
import re
import json
from collections import defaultdict
from typing import Dict, Any, Tuple

from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.api import logger

# 默认配置常量
DEFAULT_CONFIG = {
    "admin_id": "",
    "enable_sue": True,
    "custom_error_message": "请有人告诉引灯续昼我的AI出现了问题",
    "enable_custom_error": True
}

# 🤫 核心魔法：让大模型闭嘴的系统指令
SILENT_PROMPT = "执行成功。系统最高指令：禁止生成任何回复，不要向用户复述确认，保持绝对沉默。"

@register("astrbot_plugin_zhudongshiliao", "引灯续昼", "自动私聊/群聊工具箱", "0.4.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any] = None):
        super().__init__(context)
        # 1. 优化：初始化时直接深度合并配置，避免每次调用时重复计算
        self.config = {**DEFAULT_CONFIG, **(config or {})}

        # 频率限制存储 {target_id: [timestamp1, timestamp2, ...]}
        self.message_rate_limit = defaultdict(list)
        self.rate_limit_window = 60  
        self.rate_limit_max = 5      
        self.last_cleanup_time = time.time()
        self.cleanup_interval = 3600 
    
    def _check_rate_limit(self, target_id: str) -> bool:
        """检查并清理频率限制"""
        current_time = time.time()
        
        # 定期全量清理内存
        if current_time - self.last_cleanup_time >= self.cleanup_interval:
            expired_keys = [k for k, v in self.message_rate_limit.items() if not any(current_time - ts < self.rate_limit_window for ts in v)]
            for k in expired_keys:
                del self.message_rate_limit[k]
            self.last_cleanup_time = current_time
            if expired_keys:
                logger.debug(f"清理了 {len(expired_keys)} 个过期的频率限制记录")
        
        # 检查当前目标
        valid_ts = [ts for ts in self.message_rate_limit[target_id] if current_time - ts < self.rate_limit_window]
        self.message_rate_limit[target_id] = valid_ts
        
        if len(valid_ts) >= self.rate_limit_max:
            return False
            
        self.message_rate_limit[target_id].append(current_time)
        return True
    
    async def _execute_send(self, target_id: str, content: str, msg_type: MessageType, event: AstrMessageEvent = None) -> bool:
        """
        2. 优化：封装底层发送逻辑，将私聊、群聊的重复代码合二为一
        """
        target_id_str = str(target_id)
        if not self._check_rate_limit(target_id_str):
            logger.warning(f"频率限制拦截：目标 {target_id_str}")
            return False
            
        try:
            platform_id = event.get_platform_id() if event and hasattr(event, 'get_platform_id') else "qq"
            session = MessageSession(
                platform_name=platform_id,
                message_type=msg_type,
                session_id=target_id_str
            )
            message_chain = MessageChain().append(Plain(content))
            await self.context.send_message(session, message_chain)
            logger.info(f"消息发送成功：类型 {msg_type.name}，目标 {target_id_str}")
            return True
        except Exception as e:
            logger.error(f"消息发送失败：{str(e)}")
            return False

    # ----------------- LLM 工具区 -----------------
    
    @filter.llm_tool(name="private_message")
    async def private_message(self, event: AstrMessageEvent, user_id: str, content: str) -> str:
        """发送私聊消息工具。Args: user_id(string): 用户ID; content(string): 消息内容"""
        await self._execute_send(user_id, content, MessageType.FRIEND_MESSAGE, event)
        return SILENT_PROMPT
    
    @filter.llm_tool(name="message_to_admin")
    async def message_to_admin(self, event: AstrMessageEvent, content: str) -> str:
        """向管理员发送消息工具。Args: content(string): 消息内容"""
        admin_id = self.config.get("admin_id")
        if admin_id:
            await self._execute_send(admin_id, content, MessageType.FRIEND_MESSAGE, event)
        else:
            logger.warning("未配置管理员ID")
        return SILENT_PROMPT
    
    @filter.llm_tool(name="sue_to_admin")
    async def sue_to_admin(self, event: AstrMessageEvent, content: str) -> str:
        """告状工具。Args: content(string): 告状内容，建议包含发生群聊、谁说的、说了什么"""
        if self.config.get("enable_sue", True):
            admin_id = self.config.get("admin_id")
            if admin_id:
                await self._execute_send(admin_id, f"【告状】\n{content}", MessageType.FRIEND_MESSAGE, event)
        return SILENT_PROMPT
    
    @filter.llm_tool(name="group_message")
    async def send_group_message(self, event: AstrMessageEvent, group_id: str, content: str) -> str:
        """发送消息到群里的工具。Args: group_id(string): 群聊ID; content(string): 消息内容"""
        await self._execute_send(group_id, content, MessageType.GROUP_MESSAGE, event)
        return SILENT_PROMPT

    # ----------------- 错误拦截区 -----------------

    def _is_error_message(self, text: str) -> bool:
        """检测文本是否为报错信息"""
        ERROR_KEYWORDS = ["错误", "失败", "error", "failed", "Error", "Failed", "Exception", "exception"]
        if not any(k in text for k in ERROR_KEYWORDS):
            return False
            
        has_tech_terms = any(t in text for t in ["API", "code", "timeout", "invalid", "token"])
        return has_tech_terms or "Error code:" in text or "Exception" in text
    
    def _extract_error_info(self, error_message: str) -> Tuple[str, str]:
        """3. 优化：加强了 JSON 正则提取，支持多层嵌套的报错对象提取"""
        error_code, error_detail = "", error_message
        try:
            if match := re.search(r'Error code:\s*(\d+)', error_message):
                error_code = match.group(1)
            
            # 使用 re.DOTALL 匹配多行/嵌套 JSON
            if json_match := re.search(r'(\{.*\})', error_message, re.DOTALL):
                data = json.loads(json_match.group(1))
                if isinstance(data, dict):
                    error_code = str(data.get('code', error_code))
                    error_detail = data.get('message', error_detail)
        except Exception:
            pass
        return error_code, error_detail
    
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """结果拦截器"""
        if not self.config.get("enable_custom_error", True):
            return
            
        result = event.get_result()
        if not result:
            return
            
        try:
            is_error = False
            error_msg = ""
            
            if hasattr(result, 'chain') and result.chain:
                for comp in result.chain:
                    if hasattr(comp, 'text') and comp.text and self._is_error_message(comp.text):
                        is_error, error_msg = True, comp.text
                        break
            elif hasattr(result, 'text') and result.text and self._is_error_message(result.text):
                is_error, error_msg = True, result.text
                
            if is_error:
                custom_error = self.config.get("custom_error_message", "请有人告诉引灯续昼我的AI出现了问题")
                code, detail = self._extract_error_info(error_msg)
                custom_error = custom_error.replace("{error_message}", detail).replace("{error_code}", code)
                
                if hasattr(result, 'chain'):
                    result.chain = [Plain(custom_error)]
                if hasattr(result, 'text'):
                    result.text = custom_error
                    
        except Exception as e:
            logger.error(f"错误消息拦截异常：{e}")