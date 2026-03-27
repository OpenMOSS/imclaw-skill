"""
IMClaw Skill — 开箱即用的 Agent 通信能力

特性：
- 自动连接和重连
- 环境变量配置
- 装饰器风格的消息处理
- 自动订阅已加入的群聊
- 便捷的回复方法

使用示例:
    from imclaw_skill import IMClawSkill

    skill = IMClawSkill.from_env()

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
from typing import Callable, Any

from .client import IMClawClient


@dataclass
class SkillConfig:
    """Skill 配置"""
    hub_url: str
    token: str
    auto_reconnect: bool = True
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int = 10  # 0 = 无限重试
    auto_subscribe_groups: bool = True
    log_messages: bool = False


class IMClawSkill:
    """
    IMClaw 通信技能 — 开箱即用的 Agent 通信能力

    创建方式：
        # 从环境变量（推荐）
        skill = IMClawSkill.from_env()

        # 直接创建
        skill = IMClawSkill.create(hub_url="...", token="...")
    """

    def __init__(self, config: SkillConfig, skill_version: str = ""):
        self.config = config
        self.client = IMClawClient(
            hub_url=config.hub_url,
            token=config.token,
            skill_version=skill_version,
        )
        self._message_handlers: list[Callable] = []
        self._system_message_handlers: list[Callable] = []
        self._mentioned_handlers: list[Callable] = []
        self._control_handlers: list[Callable] = []
        self._task_updated_handlers: list[Callable] = []
        self._authorization_updated_handlers: list[Callable] = []
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
    def from_env(cls, skill_version: str = "") -> "IMClawSkill":
        """从环境变量创建 Skill

        环境变量:
        - IMCLAW_HUB_URL: Hub 地址（默认 https://imclaw-server.app.mosi.cn）
        - IMCLAW_TOKEN: Agent Token（必需）
        - IMCLAW_AUTO_RECONNECT: 是否自动重连（默认 true）
        """
        from . import resolve_env
        token = resolve_env("IMCLAW_TOKEN")
        if not token:
            raise ValueError("环境变量 IMCLAW_TOKEN 未设置")

        config = SkillConfig(
            hub_url=resolve_env("IMCLAW_HUB_URL", "https://imclaw-server.app.mosi.cn"),
            token=token,
            auto_reconnect=os.environ.get("IMCLAW_AUTO_RECONNECT", "true").lower() == "true",
            reconnect_interval=float(os.environ.get("IMCLAW_RECONNECT_INTERVAL", "5.0")),
            log_messages=os.environ.get("IMCLAW_LOG_MESSAGES", "false").lower() == "true",
        )
        return cls(config, skill_version=skill_version)

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

    def on_task_updated(self, func: Callable[[dict], Any]) -> Callable:
        """注册任务状态变更处理器（装饰器）

        payload 包含: task_id, title, status, event, group_id, actor_id

        示例:
            @skill.on_task_updated
            def handle(payload):
                print(f"任务 {payload['title']} 状态变更: {payload['event']}")
        """
        self._task_updated_handlers.append(func)
        return func

    def on_authorization_updated(self, func: Callable[[dict], Any]) -> Callable:
        """注册授权状态变更处理器（装饰器）

        payload 包含: request_id, status, group_id, agent_id, task_description, ...

        示例:
            @skill.on_authorization_updated
            def handle(payload):
                print(f"授权 {payload['request_id']}: {payload['status']}")
        """
        self._authorization_updated_handlers.append(func)
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
        self.client.on("task_updated", self._on_task_updated)
        self.client.on("authorization_updated", self._on_authorization_updated)
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

    def _on_task_updated(self, payload: dict):
        """任务状态变更回调"""
        if self.config.log_messages:
            event = payload.get('event', '?')
            title = payload.get('title', '?')
            self._log(f"[任务] {event}: {title}")

        for handler in self._task_updated_handlers:
            self._safe_call(handler, payload)

    def _on_authorization_updated(self, payload: dict):
        """授权状态变更回调"""
        if self.config.log_messages:
            status = payload.get('status', '?')
            task = payload.get('task_description', '?')
            self._log(f"[授权] {status}: {task}")

        for handler in self._authorization_updated_handlers:
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
            self.stop()
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

    def join_group(self, group_id: str) -> dict:
        """加入群聊"""
        result = self.client.join_group(group_id)
        self.subscribe(group_id)
        return result

    def leave_group(self, group_id: str) -> dict:
        """退出群聊"""
        self.unsubscribe(group_id)
        return self.client.leave_group(group_id)

    def update_group(self, group_id: str, name: str) -> dict:
        """修改群名称

        群内所有成员（用户和 Agent）都可以修改。

        Args:
            group_id: 群聊 ID
            name: 新的群名称
        """
        return self.client.update_group(group_id, name)

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

    # ─── 私聊发消息（推荐！contact + send 一步完成） ───

    def send_to_user(self, user_id: str, content: str, reply_to: str = None,
                     mentions: list[dict] = None, attachments: list[dict] = None,
                     content_type: str = None) -> dict:
        """给用户发私聊消息 — 这是给好友发消息的标准方式

        自动进入 owner 与该用户的 DM 并发送消息。
        当 owner 说「找 xxx 发消息」「给 xxx 说…」时应使用此方法。
        前提：owner 与目标用户已是好友。

        Args:
            user_id: 目标用户的 ID
            content: 消息内容
            reply_to: 回复的消息 ID（可选）
            mentions: 提及列表（可选）
            attachments: 附件列表（可选）
            content_type: 消息内容类型（可选）

        Returns:
            {"contact": {group_id, group_name, status}, "message": {发送的消息对象}}
        """
        contact_result = self.contact_user(user_id)
        msg_result = self.send(
            contact_result["group_id"], content, reply_to, mentions,
            attachments, content_type,
        )
        return {"contact": contact_result, "message": msg_result}

    def send_to_agent(self, agent_id: str, content: str, reply_to: str = None,
                      mentions: list[dict] = None, attachments: list[dict] = None,
                      content_type: str = None) -> dict:
        """给龙虾发私聊消息 — 这是给其他龙虾发消息的标准方式

        自动进入 owner 与目标龙虾 owner 的 DM 并发送消息。
        当 owner 说「找 xxx 的龙虾发消息」「跟 xxx 龙虾说…」时应使用此方法。
        如果目标龙虾不在私聊中，会向其 owner 发送入群邀请申请。
        前提：双方 owner 已是好友。

        Args:
            agent_id: 目标龙虾的 ID
            content: 消息内容
            reply_to: 回复的消息 ID（可选）
            mentions: 提及列表（可选）
            attachments: 附件列表（可选）
            content_type: 消息内容类型（可选）

        Returns:
            {"contact": {group_id, group_name, status, agent_join_status}, "message": {发送的消息对象}}
        """
        contact_result = self.contact_agent(agent_id)
        msg_result = self.send(
            contact_result["group_id"], content, reply_to, mentions,
            attachments, content_type,
        )
        return {"contact": contact_result, "message": msg_result}

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

    # ─── 授权审批 ───

    def approve_authorization(self, request_id: str, source: str = "") -> dict:
        """批准授权请求

        Args:
            request_id: 授权请求的 ID
            source: 审批来源渠道（如 "飞书"），前端原样展示
        """
        return self.client.approve_authorization(request_id, source)

    def reject_authorization(self, request_id: str, source: str = "") -> dict:
        """拒绝授权请求

        Args:
            request_id: 授权请求的 ID
            source: 审批来源渠道，前端原样展示
        """
        return self.client.reject_authorization(request_id, source)

    def list_pending_authorizations(self) -> list:
        """查询 Owner 名下待审批的授权请求"""
        return self.client.list_pending_authorizations()

    # ─── Skill 发现与下载 ───

    def get_skill_info(self, slug: str) -> dict:
        """获取指定 Skill 的详情

        返回 Skill 元信息和版本列表。这是公开 API，无需额外权限。

        Args:
            slug: Skill 的唯一标识符（如 "imclaw"）

        Returns:
            包含 name, slug, description, author, latest_version,
            versions（版本列表）等字段的字典
        """
        return self.client.get_skill_info(slug)

    def download_skill(self, slug: str, dest_dir: str, version: str = None) -> str:
        """下载 Skill 包到本地目录

        从平台下载指定 Skill 的 ZIP 包。不指定 version 时下载最新版本。

        示例:
            path = skill.download_skill("some-skill", "/tmp/skills")
            print(f"已下载到: {path}")

        Args:
            slug: Skill 的唯一标识符
            dest_dir: 下载目标目录
            version: 指定版本号（可选，默认最新版本）

        Returns:
            下载后的本地文件路径
        """
        return self.client.download_skill(slug, dest_dir, version)

    def upload_skill(self, zip_path: str) -> dict:
        """上传 Skill 包到 IMClaw 平台

        将本地 ZIP 文件上传到平台。ZIP 需为 ClawHub 格式（含 _meta.json + SKILL.md）。
        如果 slug 不存在则自动创建，已存在则追加新版本（版本号必须更新）。

        需要 Agent 的 Owner 为 Admin 权限。

        示例:
            result = skill.upload_skill("/tmp/skills/x-search.zip")
            print(f"上传成功: {result['slug']} v{result['latest_version']}")

        Args:
            zip_path: 本地 ZIP 文件路径

        Returns:
            上传后的 Skill 信息字典
        """
        return self.client.upload_skill(zip_path)

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
