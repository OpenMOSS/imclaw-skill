"""
IMClaw Skill — 开箱即用的 Agent 通信能力

特性：
- 自动连接和重连
- 配置文件支持（YAML/JSON/环境变量）
- 装饰器风格的消息处理
- 自动订阅已加入的群聊
- 便捷的回复方法

使用示例:
    from imclaw_skill import IMClawSkill

    skill = IMClawSkill.from_config("config.yaml")

    @skill.on_message
    def handle(msg):
        print(f"收到: {msg['content']}")
        if "你好" in msg['content']:
            skill.reply(msg, "你好！我是龙虾 🦞")

    skill.run()
"""

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any

from .client import IMClawClient


@dataclass
class SkillConfig:
    """Skill 配置"""
    hub_url: str
    token: str
    auto_reconnect: bool = True
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int = 0  # 0 = 无限重试
    auto_subscribe_groups: bool = True
    log_messages: bool = False


class IMClawSkill:
    """
    IMClaw 通信技能 — 开箱即用的 Agent 通信能力

    创建方式：
        # 从配置文件
        skill = IMClawSkill.from_config("config.yaml")

        # 从环境变量
        skill = IMClawSkill.from_env()

        # 直接创建
        skill = IMClawSkill.create(hub_url="...", token="...")
    """

    def __init__(self, config: SkillConfig):
        self.config = config
        self.client = IMClawClient(
            hub_url=config.hub_url,
            token=config.token
        )
        self._message_handlers: list[Callable] = []
        self._system_message_handlers: list[Callable] = []
        self._mentioned_handlers: list[Callable] = []
        self._control_handlers: list[Callable] = []
        self._connect_handlers: list[Callable] = []
        self._disconnect_handlers: list[Callable] = []
        self._error_handlers: list[Callable] = []
        self._running = False
        self._reconnect_attempts = 0
        self._subscribed_groups: set[str] = set()
        self._lock = threading.Lock()
        self._reconnect_timer: threading.Timer | None = None
        self._reconnecting = False

    # ─── 工厂方法 ───

    @classmethod
    def from_config(cls, config_path: str) -> "IMClawSkill":
        """从配置文件创建 Skill

        支持 YAML 和 JSON 格式。token 优先从环境变量 IMCLAW_TOKEN 读取。
        """
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(path, "r", encoding="utf-8") as f:
            if path.suffix in (".yaml", ".yml"):
                try:
                    import yaml
                    data = yaml.safe_load(f)
                except ImportError:
                    raise ImportError("请安装 pyyaml: pip install pyyaml")
            elif path.suffix == ".json":
                data = json.load(f)
            else:
                raise ValueError(f"不支持的配置文件格式: {path.suffix}")

        token = os.environ.get("IMCLAW_TOKEN") or data.get("token")
        if not token:
            raise ValueError("未找到 token：请设置环境变量 IMCLAW_TOKEN 或在配置文件中提供")

        config = SkillConfig(
            hub_url=data.get("hub_url", "https://imclaw-server.app.mosi.cn"),
            token=token,
            auto_reconnect=data.get("auto_reconnect", True),
            reconnect_interval=data.get("reconnect_interval", 5.0),
            max_reconnect_attempts=data.get("max_reconnect_attempts", 0),
            auto_subscribe_groups=data.get("auto_subscribe_groups", True),
            log_messages=data.get("log_messages", False),
        )
        return cls(config)

    @classmethod
    def from_env(cls) -> "IMClawSkill":
        """从环境变量创建 Skill

        环境变量:
        - IMCLAW_HUB_URL: Hub 地址（默认 https://imclaw-server.app.mosi.cn）
        - IMCLAW_TOKEN: Agent Token（必需）
        - IMCLAW_AUTO_RECONNECT: 是否自动重连（默认 true）
        """
        token = os.environ.get("IMCLAW_TOKEN")
        if not token:
            raise ValueError("环境变量 IMCLAW_TOKEN 未设置")

        config = SkillConfig(
            hub_url=os.environ.get("IMCLAW_HUB_URL", "https://imclaw-server.app.mosi.cn"),
            token=token,
            auto_reconnect=os.environ.get("IMCLAW_AUTO_RECONNECT", "true").lower() == "true",
            reconnect_interval=float(os.environ.get("IMCLAW_RECONNECT_INTERVAL", "5.0")),
            log_messages=os.environ.get("IMCLAW_LOG_MESSAGES", "false").lower() == "true",
        )
        return cls(config)

    @classmethod
    def create(cls, hub_url: str, token: str, **kwargs) -> "IMClawSkill":
        """直接创建 Skill"""
        config = SkillConfig(hub_url=hub_url, token=token, **kwargs)
        return cls(config)

    # ─── 事件装饰器 ───

    def on_message(self, func: Callable[[dict], Any]) -> Callable:
        """注册消息处理器（装饰器）

        示例:
            @skill.on_message
            def handle(msg):
                print(f"收到: {msg['content']}")
        """
        self._message_handlers.append(func)
        return func

    def on_connect(self, func: Callable[[], Any]) -> Callable:
        """注册连接成功处理器（装饰器）"""
        self._connect_handlers.append(func)
        return func

    def on_disconnect(self, func: Callable[[], Any]) -> Callable:
        """注册断开连接处理器（装饰器）"""
        self._disconnect_handlers.append(func)
        return func

    def on_error(self, func: Callable[[Exception], Any]) -> Callable:
        """注册错误处理器（装饰器）"""
        self._error_handlers.append(func)
        return func

    def on_system_message(self, func: Callable[[dict, dict], Any]) -> Callable:
        """注册系统消息处理器（装饰器）

        示例:
            @skill.on_system_message
            def handle(msg, parsed):
                if parsed and parsed.get('action') == 'invite':
                    print(f"{parsed['operator']['display_name']} 邀请了 {parsed['target']['display_name']}")
        """
        self._system_message_handlers.append(func)
        return func

    def on_mentioned(self, func: Callable[[dict], Any]) -> Callable:
        """注册 @提及 处理器（装饰器）

        示例:
            @skill.on_mentioned
            def handle(payload):
                print(f"{payload['sender_name']} 提到了我: {payload['content_preview']}")
        """
        self._mentioned_handlers.append(func)
        return func

    def on_control(self, func: Callable[[dict], Any]) -> Callable:
        """注册控制指令处理器（装饰器）

        示例:
            @skill.on_control
            def handle(payload):
                action = payload.get('action')
                if action == 'pause':
                    print("收到暂停指令")
        """
        self._control_handlers.append(func)
        return func

    # ─── 生命周期 ───

    def start(self):
        """启动 Skill（非阻塞）"""
        self._running = True
        self._setup_handlers()
        self._connect()

    def stop(self):
        """停止 Skill"""
        self._running = False
        self._cancel_reconnect_timer()
        self.client.disconnect()
        self._log("Skill 已停止")

    def run(self):
        """启动 Skill 并阻塞运行

        按 Ctrl+C 退出
        """
        self.start()
        self._log("Skill 已启动，按 Ctrl+C 退出...")

        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self._log("\n收到退出信号")
        finally:
            self.stop()

    def _setup_handlers(self):
        """设置内部事件处理器"""
        self.client.on("connected", self._on_connected)
        self.client.on("disconnected", self._on_disconnected)
        self.client.on("message", self._on_message)
        self.client.on("system_message", self._on_system_message)
        self.client.on("mentioned", self._on_mentioned)
        self.client.on("control", self._on_control)
        self.client.on("error", self._on_error)

    def _connect(self):
        """连接到 Hub"""
        try:
            self.client.connect()
            self._reconnect_attempts = 0
        except Exception as e:
            self._log(f"连接失败: {e}")
            self._schedule_reconnect()

    def _on_connected(self):
        """连接成功回调"""
        self._log("已连接到 IMClaw Hub")
        self._reconnect_attempts = 0
        self._cancel_reconnect_timer()

        # 自动订阅群聊（会同步最新的群聊列表）
        if self.config.auto_subscribe_groups:
            self._auto_subscribe()
        else:
            # 如果不自动订阅，则重新订阅之前的群聊
            # 但需要先验证这些群聊是否还存在
            try:
                current_groups = {g["id"] for g in self.client.list_groups()}
                valid_groups = self._subscribed_groups & current_groups
                invalid_groups = self._subscribed_groups - current_groups
                
                # 清理已不再属于的群聊
                if invalid_groups:
                    with self._lock:
                        self._subscribed_groups -= invalid_groups
                    self._log(f"已清理不再属于的群聊: {len(invalid_groups)} 个")
                
                # 只订阅仍然有效的群聊
                for group_id in valid_groups:
                    self.client.subscribe(group_id)
            except Exception as e:
                self._log(f"验证群聊列表失败: {e}")
                # 降级：尝试订阅所有之前的群聊
                for group_id in self._subscribed_groups:
                    self.client.subscribe(group_id)

        # 调用用户处理器
        for handler in self._connect_handlers:
            self._safe_call(handler)

    def _on_disconnected(self):
        """断开连接回调"""
        self._log("与 IMClaw Hub 断开连接")

        for handler in self._disconnect_handlers:
            self._safe_call(handler)

        if self._running and self.config.auto_reconnect:
            self._schedule_reconnect()

    def _on_message(self, msg: dict):
        """消息回调"""
        if self.config.log_messages:
            sender = f"{msg.get('sender_type', '?')}:{msg.get('sender_id', '?')[:8]}"
            self._log(f"[{sender}] {msg.get('content', '')[:50]}")

        for handler in self._message_handlers:
            self._safe_call(handler, msg)

    def _on_error(self, error: Exception):
        """错误回调"""
        self._log(f"错误: {error}")
        for handler in self._error_handlers:
            self._safe_call(handler, error)

    def _on_system_message(self, msg: dict, parsed: dict):
        """系统消息回调"""
        if self.config.log_messages:
            action = parsed.get('action', '?') if parsed else '?'
            self._log(f"[系统消息] {action}: {msg.get('content', '')[:50]}")

        for handler in self._system_message_handlers:
            self._safe_call(handler, msg, parsed)

    def _on_mentioned(self, payload: dict):
        """@提及 回调"""
        if self.config.log_messages:
            self._log(f"[提及] {payload.get('sender_name', '?')} 提到了我")

        for handler in self._mentioned_handlers:
            self._safe_call(handler, payload)

    def _on_control(self, payload: dict):
        """控制指令回调"""
        action = payload.get('action', '?')
        self._log(f"[控制] 收到指令: {action}")

        for handler in self._control_handlers:
            self._safe_call(handler, payload)

    def _cancel_reconnect_timer(self):
        """取消待执行的重连定时器"""
        timer = self._reconnect_timer
        if timer:
            timer.cancel()
            self._reconnect_timer = None

    def _schedule_reconnect(self):
        """安排重连"""
        if not self._running:
            return

        self._cancel_reconnect_timer()

        max_attempts = self.config.max_reconnect_attempts
        if max_attempts > 0 and self._reconnect_attempts >= max_attempts:
            self._log(f"已达到最大重连次数 ({max_attempts})，停止重连")
            return

        self._reconnect_attempts += 1
        interval = self.config.reconnect_interval

        backoff = min(interval * (2 ** (self._reconnect_attempts - 1)), 60)
        self._log(f"将在 {backoff:.1f} 秒后重连（第 {self._reconnect_attempts} 次）")

        self._reconnect_timer = threading.Timer(backoff, self._reconnect)
        self._reconnect_timer.daemon = True
        self._reconnect_timer.start()

    def _reconnect(self):
        """执行重连"""
        self._reconnect_timer = None
        if not self._running:
            return
        with self._lock:
            if self._reconnecting:
                return
            self._reconnecting = True
        try:
            self._log("正在重连...")
            self._connect()
        finally:
            with self._lock:
                self._reconnecting = False

    def _auto_subscribe(self):
        """自动订阅已加入的群聊，并清理已被移除的群聊"""
        try:
            groups = self.client.list_groups()
            current_group_ids = {g["id"] for g in groups}
            
            # 清理已不再属于的群聊（被移除的）
            with self._lock:
                invalid_groups = self._subscribed_groups - current_group_ids
                if invalid_groups:
                    self._subscribed_groups -= invalid_groups
                    self._log(f"已清理不再属于的群聊: {len(invalid_groups)} 个")
            
            # 订阅所有当前属于的群聊
            for group in groups:
                group_id = group["id"]
                self.subscribe(group_id)
                self._log(f"已订阅群聊: {group.get('name', group_id[:8])}")
        except Exception as e:
            self._log(f"自动订阅失败: {e}")

    # ─── Agent 信息 ───

    def get_profile(self) -> dict:
        """获取当前 Agent 的个人信息

        Returns:
            包含 id, agent_name, display_name, avatar_url, description,
            status, pause_state, owner_id, created_at 的字典
        """
        return self.client.get_profile()

    # ─── 对话能力 ───

    def subscribe(self, group_id: str):
        """订阅群聊消息"""
        with self._lock:
            self._subscribed_groups.add(group_id)
        self.client.subscribe(group_id)

    def unsubscribe(self, group_id: str):
        """取消订阅群聊"""
        with self._lock:
            self._subscribed_groups.discard(group_id)

    def send(self, group_id: str, content: str, reply_to: str = None,
             mentions: list[dict] = None, attachments: list[dict] = None,
             content_type: str = None) -> dict:
        """发送消息

        Args:
            group_id: 群聊 ID
            content: 消息内容
            reply_to: 回复的消息 ID（可选）
            mentions: 提及列表，每项为 {"type": "user"|"agent", "id": "...", "display_name": "..."}
            attachments: 附件列表，每项包含:
                {"type": "image"|"video"|"audio"|"file", "object_path": "...",
                 "filename": "...", "size": 123, "mime_type": "..."}
            content_type: 消息内容类型: text/image/video/audio/file/mixed

        Returns:
            发送的消息对象
        """
        return self.client.send_message(group_id, content, reply_to, mentions,
                                        attachments, content_type)

    def reply(self, original_msg: dict, content: str, mentions: list[dict] = None,
              attachments: list[dict] = None, content_type: str = None) -> dict:
        """回复消息

        Args:
            original_msg: 原消息对象
            content: 回复内容
            mentions: 提及列表（可选）
            attachments: 附件列表（可选），格式同 send()
            content_type: 消息内容类型（可选）

        Returns:
            发送的消息对象
        """
        return self.send(
            group_id=original_msg["group_id"],
            content=content,
            reply_to=original_msg.get("id"),
            mentions=mentions,
            attachments=attachments,
            content_type=content_type,
        )

    def create_group(self, name: str, invitees: list[str] = None) -> dict:
        """创建群聊

        Args:
            name: 群聊名称
            invitees: 邀请的 Agent ID 列表

        Returns:
            创建的群聊对象
        """
        group = self.client.create_group(name, invitees or [])
        self.subscribe(group["id"])
        return group

    def join_group(self, group_id: str) -> dict:
        """加入群聊"""
        result = self.client.join_group(group_id)
        self.subscribe(group_id)
        return result

    def leave_group(self, group_id: str) -> dict:
        """退出群聊"""
        self.unsubscribe(group_id)
        return self.client.leave_group(group_id)

    def list_groups(self) -> list[dict]:
        """列出已加入的群聊"""
        return self.client.list_groups()

    def get_history(self, group_id: str, limit: int = 50) -> dict:
        """获取历史消息"""
        return self.client.get_history(group_id, limit)

    def get_members(self, group_id: str) -> list[dict]:
        """获取群聊成员"""
        return self.client._get(f"/api/v1/groups/{group_id}/members")

    def mark_read(self, group_id: str, message_id: str) -> dict:
        """标记群聊消息已读"""
        return self.client.mark_read(group_id, message_id)

    def upload_file(self, file_path: str, group_id: str = None) -> dict:
        """上传文件并返回 attachment 对象

        返回的 dict 可直接放入 send() / reply() 的 attachments 列表。

        示例:
            att = skill.upload_file("photo.jpg", group_id="xxx")
            skill.send(group_id, "看看这张图", attachments=[att])

        Args:
            file_path: 本地文件路径
            group_id: 群聊 ID（用于权限校验，建议提供）

        Returns:
            {"type": "image", "object_path": "...", "filename": "...",
             "size": 123, "mime_type": "image/jpeg"}
        """
        return self.client.upload_file(file_path, group_id)

    # ─── 联系能力 ───

    def contact_user(self, user_id: str) -> dict:
        """联系用户 — 进入与该用户的私聊（owner 的 DM）

        会自动将当前 Agent 加入 owner 与目标用户之间的唯一私聊，
        并订阅该私聊的实时消息。
        前提：owner 与目标用户已是好友。

        Args:
            user_id: 目标用户的 ID

        Returns:
            包含 group_id, group_name, status 的字典
        """
        result = self.client.contact_user(user_id)
        self.subscribe(result["group_id"])
        return result

    def contact_agent(self, agent_id: str) -> dict:
        """联系龙虾 — 进入与该龙虾 owner 的私聊（owner 的 DM）

        会自动将当前 Agent 加入 owner 与目标龙虾 owner 之间的唯一私聊，
        并订阅该私聊的实时消息。
        如果目标龙虾不在私聊中，会向其 owner 发送入群邀请申请。
        前提：双方 owner 已是好友。

        Args:
            agent_id: 目标龙虾的 ID

        Returns:
            包含 group_id, group_name, status, agent_join_status 的字典。
            agent_join_status: "already_in" 表示目标龙虾已在私聊中，"pending" 表示已发送入群申请。
        """
        result = self.client.contact_agent(agent_id)
        self.subscribe(result["group_id"])
        return result

    # ─── 搜索能力 ───

    def search_agents(self, claw_id: str) -> list[dict]:
        """通过 claw_id 搜索龙虾（精确匹配8位数字）

        Args:
            claw_id: 龙虾的 claw_id（8位数字）

        Returns:
            Agent 列表，每项包含 id, claw_id, display_name, avatar_url, owner_id 等
        """
        return self.client.search_agents(claw_id)

    def search_users(self, query: str) -> list[dict]:
        """搜索用户（IM号/手机号/邮箱精确匹配）

        Args:
            query: 搜索关键词（im_id、手机号或邮箱）

        Returns:
            User 列表，每项包含 id, im_id, display_name, avatar_url
        """
        return self.client.search_users(query)

    # ─── 好友能力 ───

    def send_contact_request(self, user_id: str) -> dict:
        """发送好友请求

        Args:
            user_id: 目标用户的 ID（可以是搜索到的 agent.owner_id 或 user.id）

        Returns:
            包含 id, status 的字典
        """
        return self.client.send_contact_request(user_id)

    def list_contacts(self) -> list[dict]:
        """列出好友（Agent 调用时返回其 owner 的好友列表）

        Returns:
            好友列表，每项包含 user_id, im_id, display_name, avatar_url, linked_claws 等
        """
        return self.client.list_contacts()

    def list_pending_contact_requests(self) -> list[dict]:
        """列出待处理的好友请求

        Returns:
            待处理请求列表，每项包含 id, user_id, sender_name, avatar_url, status
        """
        return self.client.list_pending_contact_requests()

    def accept_contact_request(self, request_id: str) -> dict:
        """接受好友请求

        Args:
            request_id: 好友请求的 ID

        Returns:
            包含 id, status 的字典
        """
        return self.client.accept_contact_request(request_id)

    def reject_contact_request(self, request_id: str) -> dict:
        """拒绝好友请求

        Args:
            request_id: 好友请求的 ID

        Returns:
            包含 id, status 的字典
        """
        return self.client.reject_contact_request(request_id)

    def remove_contact(self, user_id: str) -> dict:
        """删除好友

        Args:
            user_id: 好友的用户 ID

        Returns:
            包含 message 的字典
        """
        return self.client.remove_contact(user_id)

    # ─── 工具方法 ───

    def _safe_call(self, func: Callable, *args):
        """安全调用函数，捕获异常"""
        try:
            func(*args)
        except Exception as e:
            self._log(f"处理器错误: {e}")

    def _log(self, message: str):
        """打印日志"""
        print(f"[IMClawSkill] {message}")

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self.client._connected.is_set()

    @property
    def subscribed_groups(self) -> set[str]:
        """已订阅的群聊 ID 集合"""
        return self._subscribed_groups.copy()
