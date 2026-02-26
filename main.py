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
from collections import defaultdict

# 默认配置常量
DEFAULT_CONFIG = {
    "admin_id": "",  # 空字符串，强制用户在WebUI中设置
    "enable_sue": True,
    "custom_error_message": "请有人告诉引灯续昼我的AI出现了问题",
    "enable_custom_error": True
}

@register("astrbot_plugin_zhudongshiliao", "引灯续昼", "自动私聊插件，提供私聊功能作为工具供大模型调用。", "0.3.9")
class MyPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        # 频率限制存储
        self.message_rate_limit = defaultdict(list)  # {user_id: [timestamp1, timestamp2, ...]}
        self.rate_limit_window = 60  # 时间窗口（秒）
        self.rate_limit_max = 5  # 时间窗口内最大消息数
    
    def _check_rate_limit(self, user_id):
        """
        检查频率限制
        
        Args:
            user_id: 用户ID
            
        Returns:
            bool: 是否通过频率限制
        """
        user_id_str = str(user_id)
        current_time = time.time()
        
        # 清理过期的时间戳
        self.message_rate_limit[user_id_str] = [
            timestamp for timestamp in self.message_rate_limit[user_id_str]
            if current_time - timestamp < self.rate_limit_window
        ]
        
        # 检查是否超过限制
        if len(self.message_rate_limit[user_id_str]) >= self.rate_limit_max:
            return False
        
        # 记录当前时间戳
        self.message_rate_limit[user_id_str].append(current_time)
        return True
    
    async def send_private_message(self, user_id, message, event=None):
        # 发送私聊消息
        user_id_str = str(user_id)
        
        # 检查频率限制
        if not self._check_rate_limit(user_id_str):
            logger.warning(f"私聊频率限制：用户 {user_id_str} 发送消息过于频繁")
            return
        
        # 审计日志
        logger.info(f"发送私聊消息：目标用户 {user_id_str}，消息长度 {len(message)}")
        
        # 尝试通过事件回复发送（仅当目标用户是当前事件发送者时）
        if event and hasattr(event, 'reply') and hasattr(event, 'user_id'):
            # 检查目标用户是否是当前事件发送者
            try:
                event_user_id = str(event.user_id)
                if event_user_id == user_id_str:
                    await event.reply(message, private=True)
                    return
            except Exception as e:
                # 如果获取user_id失败，继续使用其他发送方式
                logger.warning(f"获取事件用户ID失败：{str(e)}")
                pass
        # 尝试通过上下文发送
        if hasattr(self.context, 'send_private_message'):
            await self.context.send_private_message(user_id_str, message)
            return
        # 尝试通过消息会话发送
        platform_id = "qq"
        if event and hasattr(event, 'get_platform_id'):
            platform_id = event.get_platform_id()
        session = MessageSession(
            platform_name=platform_id,
            message_type=MessageType.FRIEND_MESSAGE,
            session_id=user_id_str
        )
        message_chain = MessageChain()
        message_chain.chain = [Plain(message)]
        await self.context.send_message(session, message_chain)
    
    @filter.llm_tool(name="private_message")
    async def private_message(self, event: AstrMessageEvent, user_id: str, content: str) -> MessageEventResult:
        """
        发送私聊消息工具
        
        Args:
            user_id(string): 用户ID
            content(string): 消息内容
        """
        await self.send_private_message(user_id, content, event)
        event.stop_event()
        return event.plain_result("")
    
    def _get_config(self):
        """
        获取最新配置，确保配置同步
        
        Returns:
            dict: 最新的配置字典
        """
        try:
            # 使用构造函数传入的插件配置
            config = self.config
            # 验证配置完整性
            if not isinstance(config, dict):
                # 如果配置不是字典，返回默认配置
                return DEFAULT_CONFIG
            # 确保所有必要的配置项都存在
            # 合并默认配置和实际配置
            for key, value in DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = value
            return config
        except Exception:
            # 发生异常时返回默认配置
            return DEFAULT_CONFIG
    
    @filter.llm_tool(name="message_to_admin")
    async def message_to_admin(self, event: AstrMessageEvent, content: str) -> MessageEventResult:
        """
        向管理员发送消息工具
        
        Args:
            content(string): 消息内容

        """
        # 获取最新配置
        config = self._get_config()
        admin_id = config.get("admin_id", "2757808353")
        await self.send_private_message(admin_id, content, event)
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
        # 获取最新配置
        config = self._get_config()
        if config.get("enable_sue", True):
            admin_id = config.get("admin_id", "2757808353")
            await self.send_private_message(admin_id, f"【告状】\n{content}", event)
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
        # 检查频率限制
        if not self._check_rate_limit(group_id):
            logger.warning(f"群消息频率限制：群 {group_id} 发送消息过于频繁")
            return event.plain_result("群消息发送失败：发送过于频繁，请稍后再试")
        
        # 审计日志
        logger.info(f"发送群消息：目标群 {group_id}，消息长度 {len(content)}")
        
        try:
            # 确保群ID是字符串
            group_id_str = str(group_id)
            
            # 使用平台特定的发送方式（aiocqhttp）
            if hasattr(event, 'bot') and hasattr(event.bot, 'send_group_msg'):
                await event.bot.send_group_msg(group_id=group_id_str, message=content)
                return event.plain_result("群消息发送成功")
            else:
                return event.plain_result("群消息发送失败：不支持的平台或方法")
                
        except Exception as e:
            # 详细异常写入日志
            logger.error(f"群消息发送失败：{str(e)}")
            # 对外返回通用失败文案
            return event.plain_result("群消息发送失败：系统暂时无法发送消息，请稍后再试")
    
    def _replace_error_variables(self, message, error_message="", error_code=""):
        """
        替换错误消息中的变量
        
        Args:
            message: 原始消息
            error_message: 系统错误消息
            error_code: 错误代码
            
        Returns:
            替换变量后的消息
        """
        message = message.replace("{error_message}", error_message)
        message = message.replace("{error_code}", error_code)
        return message
    
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """
        发送消息前的事件钩子，用于拦截并修改错误消息
        """
        # 获取最新配置，确保实时同步
        config = self._get_config()
        if not config.get("enable_custom_error", True):
            return
        
        # 获取当前结果
        result = event.get_result()
        if not result:
            return
        
        # 检查并修改错误消息
        try:
            # 检查结果是否是错误消息
            is_error = False
            error_message = ""
            
            # 定义错误关键词和模式
            ERROR_KEYWORDS = [
                "错误", "失败", "error", "failed", "Error", "Failed",
                "LLM 响应错误", "All chat models failed", "AuthenticationError",
                "API key is invalid", "Error code:", "Exception", "exception"
            ]
            
            # 检查不同格式的结果对象
            if hasattr(result, 'chain') and result.chain:
                # 检查消息链
                logger.debug(f"检查消息链，长度: {len(result.chain)}")
                for comp in result.chain:
                    if hasattr(comp, 'text') and comp.text:
                        text = comp.text
                        logger.debug(f"消息链组件文本: {text[:100]}...")
                        # 改进的错误识别逻辑
                        # 1. 检查是否包含错误关键词
                        has_error_keyword = any(keyword in text for keyword in ERROR_KEYWORDS)
                        logger.debug(f"是否包含错误关键词: {has_error_keyword}")
                        
                        if has_error_keyword:
                            # 2. 检查上下文约束，避免普通文本被误识别
                            # 例如，错误消息通常包含更多技术术语或特定格式
                            has_technical_terms = any(term in text for term in [
                                "API", "code", "响应", "请求", 
                                "timeout", "connection", "invalid", "token", "key",
                                "timeout", "error code", "exception", "failed to", "cannot", "unable"
                            ])
                            logger.debug(f"是否包含技术术语: {has_technical_terms}")
                            
                            # 3. 检查文本长度和结构
                            is_likely_error = False
                            if has_technical_terms:
                                is_likely_error = True
                            elif "Error code:" in text:
                                is_likely_error = True
                            elif "Exception" in text or "exception" in text:
                                is_likely_error = True
                            elif len(text) > 50 and ("错误" in text or "error" in text.lower()) and any(term in text for term in ["API", "code", "请求", "响应"]):
                                # 较长的包含错误关键词和技术术语的文本更可能是错误消息
                                is_likely_error = True
                            
                            logger.debug(f"是否可能是错误消息: {is_likely_error}")
                            
                            if is_likely_error:
                                is_error = True
                                error_message = text
                                logger.debug(f"识别为错误消息: {error_message[:100]}...")
                                break
            elif hasattr(result, 'text') and result.text:
                # 检查文本结果
                text = result.text
                logger.debug(f"检查文本结果: {text[:100]}...")
                # 改进的错误识别逻辑
                has_error_keyword = any(keyword in text for keyword in ERROR_KEYWORDS)
                logger.debug(f"是否包含错误关键词: {has_error_keyword}")
                
                if has_error_keyword:
                    # 同样的上下文约束检查
                    has_technical_terms = any(term in text for term in [
                        "API", "code", "响应", "请求", 
                        "timeout", "connection", "invalid", "token", "key",
                        "timeout", "error code", "exception", "failed to", "cannot", "unable"
                    ])
                    logger.debug(f"是否包含技术术语: {has_technical_terms}")
                    
                    is_likely_error = False
                    if has_technical_terms:
                        is_likely_error = True
                    elif "Error code:" in text:
                        is_likely_error = True
                    elif "Exception" in text or "exception" in text:
                        is_likely_error = True
                    elif len(text) > 50 and ("错误" in text or "error" in text.lower()) and any(term in text for term in ["API", "code", "请求", "响应"]):
                        # 较长的包含错误关键词和技术术语的文本更可能是错误消息
                        is_likely_error = True
                    
                    logger.debug(f"是否可能是错误消息: {is_likely_error}")
                    
                    if is_likely_error:
                        is_error = True
                        error_message = text
                        logger.debug(f"识别为错误消息: {error_message[:100]}...")
            
            # 如果是错误消息，替换为自定义报错
            if is_error:
                # 获取最新自定义报错消息
                custom_error = config.get("custom_error_message", "请有人告诉引灯续昼我的AI出现了问题")
                
                # 提取错误代码
                error_code = ""
                import re
                match = re.search(r'Error code: (\d+)', error_message)
                if match:
                    error_code = match.group(1)
                
                # 替换变量
                custom_error = self._replace_error_variables(custom_error, error_message, error_code)
                
                # 替换结果的消息链
                if hasattr(result, 'chain'):
                    result.chain = [Plain(custom_error)]
                elif hasattr(result, 'text'):
                    result.text = custom_error
                
        except Exception as e:
            # 记录异常，确保钩子不会崩溃
            logger.error(f"错误消息替换失败：{str(e)}")
            logger.exception("错误消息替换异常详情")
    
    async def terminate(self):
        pass