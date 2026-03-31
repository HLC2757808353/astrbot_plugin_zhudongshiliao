from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse
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

@register("astrbot_plugin_zhudongshiliao", "引灯续昼", "自动私聊插件，提供私聊功能作为工具供大模型调用。", "0.3.9")
class MyPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        # 频率限制存储
        self.message_rate_limit = defaultdict(list)  # {user_id: [timestamp1, timestamp2, ...]}
        self.rate_limit_window = 60  # 时间窗口（秒）
        self.rate_limit_max = 5  # 时间窗口内最大消息数
        # 上次清理时间
        self.last_cleanup_time = time.time()
        self.cleanup_interval = 3600  # 清理间隔（秒）
    
    def _cleanup_rate_limit(self):
        """
        定期清理过期的频率限制记录
        """
        current_time = time.time()
        # 检查是否需要清理
        if current_time - self.last_cleanup_time < self.cleanup_interval:
            return
        
        # 清理过期的记录
        expired_keys = []
        for user_id, timestamps in self.message_rate_limit.items():
            # 过滤出未过期的时间戳
            valid_timestamps = [
                timestamp for timestamp in timestamps
                if current_time - timestamp < self.rate_limit_window
            ]
            if valid_timestamps:
                self.message_rate_limit[user_id] = valid_timestamps
            else:
                expired_keys.append(user_id)
        
        # 删除空记录
        for user_id in expired_keys:
            del self.message_rate_limit[user_id]
        
        # 更新上次清理时间
        self.last_cleanup_time = current_time
        if expired_keys:
            logger.debug(f"清理了 {len(expired_keys)} 个过期的频率限制记录")

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
        
        # 定期清理过期记录（全局清理）
        if current_time - self.last_cleanup_time >= self.cleanup_interval:
            self._cleanup_rate_limit()
        
        # 只清理当前用户的过期时间戳
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
        
        # 统一使用标准化方法发送消息
        try:
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
            logger.info(f"私聊消息发送成功：目标用户 {user_id_str}")
        except Exception as e:
            logger.error(f"发送私聊消息失败：{str(e)}")
            logger.exception("发送私聊消息失败详情")

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
            # 为避免直接修改原始配置字典，创建副本
            config = config.copy()
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
        admin_id = config.get("admin_id")
        if admin_id:
            await self.send_private_message(admin_id, content, event)
        else:
            logger.warning("管理员ID未配置，无法发送消息")
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
            admin_id = config.get("admin_id")
            if admin_id:
                await self.send_private_message(admin_id, f"【告状】\n{content}", event)
            else:
                logger.warning("管理员ID未配置，无法发送告状消息")
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
            
            # 统一使用标准化方法发送消息
            platform_id = "qq"
            if event and hasattr(event, 'get_platform_id'):
                platform_id = event.get_platform_id()
            session = MessageSession(
                platform_name=platform_id,
                message_type=MessageType.GROUP_MESSAGE,
                session_id=group_id_str
            )
            message_chain = MessageChain()
            message_chain.chain = [Plain(content)]
            await self.context.send_message(session, message_chain)
            logger.info(f"群消息发送成功：目标群 {group_id_str}")
            return event.plain_result("群消息发送成功")
            
        except (AttributeError, TypeError) as e:
            # 详细异常写入日志
            logger.error(f"群消息发送失败：{str(e)}")
            logger.exception("群消息发送失败详情")
            # 对外返回通用失败文案
            return event.plain_result("群消息发送失败：系统暂时无法发送消息，请稍后再试")
        except Exception as e:
            # 捕获其他异常
            logger.error(f"群消息发送失败：{str(e)}")
            logger.exception("群消息发送失败详情")
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

    def _is_error_message(self, text: str) -> bool:
        """
        检测文本是否为错误消息
        
        Args:
            text: 要检测的文本
            
        Returns:
            bool: 是否为错误消息
        """
        # 定义错误关键词和模式
        ERROR_KEYWORDS = [
            "错误", "失败", "error", "failed", "Error", "Failed",
            "LLM 响应错误", "All chat models failed", "AuthenticationError",
            "API key is invalid", "Error code:", "Exception", "exception"
        ]
        
        # 1. 检查是否包含错误关键词
        has_error_keyword = any(keyword in text for keyword in ERROR_KEYWORDS)
        if not has_error_keyword:
            return False
        
        # 2. 检查上下文约束，避免普通文本被误识别
        has_technical_terms = any(term in text for term in [
            "API", "code", "响应", "请求", 
            "timeout", "connection", "invalid", "token", "key",
            "timeout", "error code", "exception", "failed to", "cannot", "unable"
        ])
        
        # 3. 检查文本长度和结构
        is_likely_error = False
        if has_technical_terms:
            is_likely_error = True
        elif "Error code:" in text:
            is_likely_error = True
        elif "Exception" in text or "exception" in text:
            is_likely_error = True
        elif len(text) > 50 and ("错误" in text or "error" in text.lower()) and any(term in text for term in ["API", "code", "请求", "响应"]):
            is_likely_error = True
        
        return is_likely_error

    def _extract_error_info(self, error_message):
        """
        从错误消息中提取错误代码和详细信息
        
        Args:
            error_message: 原始错误消息
            
        Returns:
            tuple: (error_code, error_detail)
        """
        error_code = ""
        error_detail = error_message
        
        # 尝试从多种格式中提取错误信息
        try:
            # 格式1: Error code: 400 - {'code': 20012, 'message': 'Model does not exist. Please check it carefully.', 'data': None}
            # 提取外层错误代码
            outer_match = re.search(r'Error code: (\d+)', error_message)
            if outer_match:
                error_code = outer_match.group(1)
            
            # 提取内层JSON中的错误信息
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
            
            # 检查不同格式的结果对象
            if hasattr(result, 'chain') and result.chain:
                # 检查消息链
                logger.debug(f"检查消息链，长度: {len(result.chain)}")
                for comp in result.chain:
                    if hasattr(comp, 'text') and comp.text:
                        text = comp.text
                        logger.debug(f"消息链组件文本: {text[:100]}...")
                        if self._is_error_message(text):
                            is_error = True
                            error_message = text
                            logger.debug(f"识别为错误消息: {error_message[:100]}...")
                            break
            elif hasattr(result, 'text') and result.text:
                # 检查文本结果
                text = result.text
                logger.debug(f"检查文本结果: {text[:100]}...")
                if self._is_error_message(text):
                    is_error = True
                    error_message = text
                    logger.debug(f"识别为错误消息: {error_message[:100]}...")
            
            # 如果是错误消息，替换为自定义报错
            if is_error:
                # 获取最新自定义报错消息
                custom_error = config.get("custom_error_message", "请有人告诉引灯续昼我的AI出现了问题")
                
                # 提取错误代码和详细信息
                error_code, error_detail = self._extract_error_info(error_message)
                
                # 替换变量
                custom_error = self._replace_error_variables(custom_error, error_detail, error_code)
                
                # 替换结果的消息链
                if hasattr(result, 'chain'):
                    # 只替换包含错误消息的组件，保留其他组件
                    new_chain = []
                    for comp in result.chain:
                        if hasattr(comp, 'text') and comp.text and self._is_error_message(comp.text):
                            # 替换错误消息组件
                            new_chain.append(Plain(custom_error))
                        else:
                            # 保留其他组件
                            new_chain.append(comp)
                    result.chain = new_chain
                elif hasattr(result, 'text'):
                    result.text = custom_error
                
        except Exception as e:
            # 记录异常，确保钩子不会崩溃
            logger.error(f"错误消息替换失败：{str(e)}")
            logger.exception("错误消息替换异常详情")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """
        拦截 LLM 错误响应
        当 LLM 调用失败时（如网络错误、流式响应中断等），role 会是 "err"
        """
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

    async def terminate(self):
        pass