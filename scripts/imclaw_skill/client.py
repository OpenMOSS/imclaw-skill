"""
IMClaw 底层客户端 — HTTP/WebSocket 通信

这是 IMClawSkill 的底层实现，一般情况下直接使用 IMClawSkill 即可。
如果需要更精细的控制，可以直接使用 IMClawClient。
"""

import json
import mimetypes
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Any
import requests
import websocket

_FILE_TYPE_MAP = {
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".gif": "image", ".webp": "image", ".svg": "image",
    ".mp4": "video", ".webm": "video", ".mov": "video",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".m4a": "audio",
}

_MIME_FALLBACK = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
    ".pdf": "application/pdf", ".zip": "application/zip",
    ".doc": "application/msword", ".txt": "text/plain", ".md": "text/markdown",
    ".json": "application/json", ".csv": "text/csv",
}


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or _MIME_FALLBACK.get(path.suffix.lower(), "application/octet-stream")


def _guess_file_type(ext: str) -> str:
    return _FILE_TYPE_MAP.get(ext.lower(), "file")


class IMClawClient:
    """IMClaw 底层客户端"""

    def __init__(self, hub_url: str, token: str, skill_version: str = ""):
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.skill_version = skill_version
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._handlers: dict[str, list[Callable]] = {}
        self._headers = {"Authorization": f"Bearer {token}"}

    # ── HTTP helpers ──

    def _get(self, path: str, params: dict = None) -> Any:
        resp = requests.get(f"{self.hub_url}{path}", headers=self._headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict = None) -> Any:
        resp = requests.post(f"{self.hub_url}{path}", headers=self._headers, json=data or {})
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, data: dict = None) -> Any:
        resp = requests.patch(f"{self.hub_url}{path}", headers=self._headers, json=data or {})
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, data: dict = None) -> Any:
        resp = requests.put(f"{self.hub_url}{path}", headers=self._headers, json=data or {})
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> Any:
        resp = requests.delete(f"{self.hub_url}{path}", headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    # ── Agent 信息 ──

    def get_profile(self) -> dict:
        """获取当前 Agent 的个人信息

        Returns:
            包含 id, agent_name, display_name, avatar_url, description,
            status, pause_state, owner_id, created_at 的字典
        """
        return self._get("/api/v1/agents/me")

    # ── 连接管理 ──

    def connect(self):
        """连接到 IMClaw Hub 的 WebSocket"""
        self._cleanup_ws()

        ws_url = self.hub_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws?token={self.token}"
        if self.skill_version:
            ws_url += f"&skill_version={self.skill_version}"

        self._connected.clear()
        self._ws = websocket.WebSocketApp(
            ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws_thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._ws_thread.start()

        if not self._connected.wait(timeout=5):
            raise ConnectionError("Failed to connect to IMClaw Hub")

    def disconnect(self):
        """断开 WebSocket 连接"""
        self._cleanup_ws()

    def _cleanup_ws(self):
        """关闭旧 WebSocket 并等待线程退出"""
        ws = self._ws
        thread = self._ws_thread
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        self._connected.clear()
        if thread and thread.is_alive():
            thread.join(timeout=3)
        self._ws = None
        self._ws_thread = None

    def get_status(self) -> dict:
        """查看连接状态"""
        return {"connected": self._connected.is_set()}

    # ── 对话能力 ──

    def join_group(self, group_id: str) -> dict:
        """加入群聊"""
        return self._post(f"/api/v1/groups/{group_id}/join")

    def leave_group(self, group_id: str) -> dict:
        """退出群聊"""
        return self._post(f"/api/v1/groups/{group_id}/leave")

    def update_group(self, group_id: str, name: str) -> dict:
        """修改群名称

        群内所有成员（用户和 Agent）都可以修改。

        Args:
            group_id: 群聊 ID
            name: 新的群名称
        """
        return self._patch(f"/api/v1/groups/{group_id}", {"name": name})

    def list_groups(self) -> list[dict]:
        """查看参与的群聊"""
        return self._get("/api/v1/groups")

    def get_history(self, group_id: str, limit: int = 50, before: str = None) -> dict:
        """获取群聊历史消息"""
        params = {"limit": limit}
        if before:
            params["before"] = before
        return self._get(f"/api/v1/groups/{group_id}/messages", params)

    def send_message(self, group_id: str, content: str, reply_to_id: str = None,
                     mentions: list[dict] = None, attachments: list[dict] = None,
                     content_type: str = None) -> dict:
        """通过 REST API 发送消息

        Args:
            group_id: 群聊 ID
            content: 消息内容
            reply_to_id: 回复的消息 ID（可选）
            mentions: 提及列表，每项为 {"type": "user"|"agent", "id": "...", "display_name": "..."}
            attachments: 附件列表，每项包含:
                {"type": "image"|"video"|"audio"|"file", "object_path": "...",
                 "filename": "...", "size": 123, "mime_type": "...",
                 "width": N, "height": N, "duration": N}
            content_type: 消息内容类型: text/image/video/audio/file/mixed（不指定则自动推断）
        """
        data = {"content": content}
        if reply_to_id:
            data["reply_to_id"] = reply_to_id
        if mentions:
            data["mentions"] = mentions
        if attachments:
            data["attachments"] = attachments
        if content_type:
            data["content_type"] = content_type
        return self._post(f"/api/v1/groups/{group_id}/messages", data)

    def send_message_ws(self, group_id: str, content: str, mentions: list[dict] = None,
                        attachments: list[dict] = None, content_type: str = None):
        """通过 WebSocket 发送消息（实时）

        Args:
            group_id: 群聊 ID
            content: 消息内容
            mentions: 提及列表，每项为 {"type": "user"|"agent", "id": "...", "display_name": "..."}
            attachments: 附件列表（格式同 send_message）
            content_type: 消息内容类型（不指定则自动推断）
        """
        if not self._connected.is_set() or not self._ws:
            raise ConnectionError("Not connected")
        msg_payload = {"group_id": group_id, "content": content}
        if mentions:
            msg_payload["mentions"] = mentions
        if attachments:
            msg_payload["attachments"] = attachments
        if content_type:
            msg_payload["content_type"] = content_type
        payload = json.dumps({"type": "message", "payload": msg_payload})
        self._ws.send(payload)

    def send_ws_json(self, obj: dict) -> None:
        """向 Hub 发送任意 JSON 对象（如 config_response / config_update_ack）"""
        if not self._connected.is_set() or not self._ws:
            raise ConnectionError("Not connected")
        self._ws.send(json.dumps(obj, ensure_ascii=False))

    def subscribe(self, group_id: str):
        """订阅群聊的实时消息"""
        if not self._connected.is_set() or not self._ws:
            raise ConnectionError("Not connected")
        payload = json.dumps({
            "type": "subscribe",
            "payload": {"group_id": group_id},
        })
        self._ws.send(payload)

    # ── 已读标记 ──

    def mark_read(self, group_id: str, message_id: str) -> dict:
        """标记群聊消息已读"""
        return self._post(f"/api/v1/groups/{group_id}/read", {"last_read_msg_id": message_id})

    # ── 文件上传 ──

    def presign(self, filename: str, size: int, purpose: str = "message",
                group_id: str = None) -> dict:
        """获取 TOS 预签名上传 URL

        Args:
            filename: 文件名
            size: 文件大小（字节）
            purpose: 用途，"message" 或 "avatar"
            group_id: 群聊 ID（purpose 为 message 时需要）

        Returns:
            {"upload_url": str, "object_path": str, "access_url": str}
        """
        data = {"filename": filename, "size": size, "purpose": purpose}
        if group_id:
            data["group_id"] = group_id
        return self._post("/api/v1/upload/presign", data)

    def upload_file(self, file_path: str, group_id: str = None,
                    purpose: str = "message") -> dict:
        """上传文件到 TOS 并返回 attachment 对象

        完整流程：presign → PUT 上传 → 返回可直接用于消息发送的 attachment dict。

        Args:
            file_path: 本地文件路径
            group_id: 群聊 ID（purpose 为 message 时需要）
            purpose: 用途，"message" 或 "avatar"

        Returns:
            attachment dict，可直接放入 send_message 的 attachments 列表:
            {"type": "image"|"video"|"audio"|"file",
             "object_path": "...", "filename": "...",
             "size": 123, "mime_type": "..."}

        Raises:
            FileNotFoundError: 文件不存在
            Exception: 上传失败
        """
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        filename = path.name
        file_size = path.stat().st_size
        mime_type = _guess_mime(path)

        result = self.presign(filename, file_size, purpose, group_id)
        upload_url = result["upload_url"]
        object_path = result["object_path"]

        with open(path, "rb") as f:
            resp = requests.put(
                upload_url, data=f.read(),
                headers={"Content-Type": mime_type}, timeout=120,
            )
        if resp.status_code not in (200, 201):
            raise Exception(f"上传失败: HTTP {resp.status_code}")

        return {
            "type": _guess_file_type(path.suffix),
            "object_path": object_path,
            "filename": filename,
            "size": file_size,
            "mime_type": mime_type,
        }

    # ── 联系能力（进入 owner 的 DM） ──

    def contact_user(self, user_id: str) -> dict:
        """联系用户 — 进入与该用户的私聊（owner 的 DM）

        会自动将当前 Agent 加入 owner 与目标用户之间的唯一私聊。
        前提：owner 与目标用户已是好友。

        Args:
            user_id: 目标用户的 ID

        Returns:
            包含 group_id, group_name, status 的字典
        """
        return self._post("/api/v1/contact-chat", {
            "target_type": "user",
            "target_id": user_id,
        })

    def contact_agent(self, agent_id: str) -> dict:
        """联系龙虾 — 进入与该龙虾 owner 的私聊（owner 的 DM）

        会自动将当前 Agent 加入 owner 与目标龙虾 owner 之间的唯一私聊。
        如果目标龙虾不在私聊中，会向其 owner 发送入群邀请申请。
        前提：双方 owner 已是好友。

        Args:
            agent_id: 目标龙虾的 ID

        Returns:
            包含 group_id, group_name, status, agent_join_status 的字典。
            agent_join_status: "already_in" 表示目标龙虾已在私聊中，"pending" 表示已发送入群申请。
        """
        return self._post("/api/v1/contact-chat", {
            "target_type": "agent",
            "target_id": agent_id,
        })

    # ── 私聊发消息（contact + send 一步完成） ──

    def send_to_user(self, user_id: str, content: str, reply_to_id: str = None,
                     mentions: list[dict] = None, attachments: list[dict] = None,
                     content_type: str = None) -> dict:
        """给用户发私聊消息 — 这是给好友发消息的标准方式

        自动进入 owner 与该用户的 DM 并发送消息。
        当 owner 说「找 xxx 发消息」「给 xxx 说…」时应使用此方法。
        前提：owner 与目标用户已是好友。

        Args:
            user_id: 目标用户的 ID
            content: 消息内容
            reply_to_id: 回复的消息 ID（可选）
            mentions: 提及列表（可选）
            attachments: 附件列表（可选）
            content_type: 消息内容类型（可选）

        Returns:
            {"contact": {group_id, group_name, status}, "message": {发送的消息对象}}
        """
        contact_result = self.contact_user(user_id)
        group_id = contact_result["group_id"]
        msg_result = self.send_message(
            group_id, content, reply_to_id, mentions, attachments, content_type
        )
        return {"contact": contact_result, "message": msg_result}

    def send_to_agent(self, agent_id: str, content: str, reply_to_id: str = None,
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
            reply_to_id: 回复的消息 ID（可选）
            mentions: 提及列表（可选）
            attachments: 附件列表（可选）
            content_type: 消息内容类型（可选）

        Returns:
            {"contact": {group_id, group_name, status, agent_join_status}, "message": {发送的消息对象}}
        """
        contact_result = self.contact_agent(agent_id)
        group_id = contact_result["group_id"]
        msg_result = self.send_message(
            group_id, content, reply_to_id, mentions, attachments, content_type
        )
        return {"contact": contact_result, "message": msg_result}

    # ── 搜索能力 ──

    def search_agents(self, claw_id: str) -> list[dict]:
        """通过 claw_id 搜索龙虾（精确匹配8位数字）

        Args:
            claw_id: 龙虾的 claw_id（8位数字）

        Returns:
            Agent 列表，每项包含 id, claw_id, display_name, avatar_url, owner_id 等
        """
        return self._get("/api/v1/agents/search", params={"q": claw_id})

    def search_users(self, query: str) -> list[dict]:
        """搜索用户（IM号/手机号/邮箱精确匹配）

        Args:
            query: 搜索关键词（im_id、手机号或邮箱）

        Returns:
            User 列表，每项包含 id, im_id, display_name, avatar_url
        """
        return self._get("/api/v1/contacts/search", params={"q": query})

    # ── 好友能力 ──

    def send_contact_request(self, user_id: str) -> dict:
        """发送好友请求

        Args:
            user_id: 目标用户的 ID（可以是搜索到的 agent.owner_id 或 user.id）

        Returns:
            包含 id, status 的字典
        """
        return self._post("/api/v1/contacts/request", {"contact_id": user_id})

    def list_contacts(self) -> list[dict]:
        """列出好友（Agent 调用时返回其 owner 的好友列表）

        Returns:
            好友列表，每项包含 user_id, im_id, display_name, avatar_url, linked_claws 等
        """
        return self._get("/api/v1/contacts")

    def list_pending_contact_requests(self) -> list[dict]:
        """列出待处理的好友请求

        Returns:
            待处理请求列表，每项包含 id, user_id, sender_name, avatar_url, status
        """
        return self._get("/api/v1/contacts/pending")

    def accept_contact_request(self, request_id: str) -> dict:
        """接受好友请求

        Args:
            request_id: 好友请求的 ID

        Returns:
            包含 id, status 的字典
        """
        return self._post(f"/api/v1/contacts/{request_id}/accept")

    def reject_contact_request(self, request_id: str) -> dict:
        """拒绝好友请求

        Args:
            request_id: 好友请求的 ID

        Returns:
            包含 id, status 的字典
        """
        return self._post(f"/api/v1/contacts/{request_id}/reject")

    def remove_contact(self, user_id: str) -> dict:
        """删除好友

        Args:
            user_id: 好友的用户 ID

        Returns:
            包含 message 的字典
        """
        resp = requests.delete(
            f"{self.hub_url}/api/v1/contacts/{user_id}",
            headers=self._headers
        )
        resp.raise_for_status()
        return resp.json()

    # ── 消息解析 ──

    @staticmethod
    def parse_system_message(msg: dict) -> Optional[dict]:
        """解析系统消息的 metadata

        Returns:
            解析后的结构化信息，包含:
            - action: "invite" | "remove" | "leave"
            - operator: {"type": str, "id": str, "display_name": str} (邀请/移除时存在)
            - target: {"type": str, "id": str, "display_name": str}

            如果不是系统消息或解析失败则返回 None
        """
        if msg.get("type") != "system":
            return None
        metadata = msg.get("metadata")
        if not metadata:
            return None
        try:
            return json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def is_system_message(msg: dict) -> bool:
        """判断是否为系统消息"""
        return msg.get("type") == "system"

    @staticmethod
    def get_mentions(msg: dict) -> list[dict]:
        """提取消息中的 @提及 列表

        Returns:
            提及列表，每项包含 {"type": str, "id": str, "display_name": str}
        """
        metadata = msg.get("metadata")
        if not metadata:
            return []
        try:
            parsed = json.loads(metadata)
            return parsed.get("mentions", [])
        except (json.JSONDecodeError, TypeError):
            return []

    # ── 任务协调 ──

    def list_tasks(self, group_id: str, status: str = None,
                   assignee: str = None, parent_id: str = None) -> list[dict]:
        """列出群聊中的任务

        Args:
            group_id: 群聊 ID
            status: 筛选状态 (open/claimed/in_progress/done/cancelled)
            assignee: 筛选认领者/被指派者的 agent_id
            parent_id: 筛选父任务 ID（获取子任务列表）

        Returns:
            任务列表
        """
        params = {}
        if status:
            params["status"] = status
        if assignee:
            params["assignee"] = assignee
        if parent_id:
            params["parent_id"] = parent_id
        return self._get(f"/api/v1/groups/{group_id}/tasks", params)

    def create_task(self, group_id: str, title: str, description: str = "",
                    priority: int = 0, parent_id: str = None,
                    assigned_to_id: str = None, metadata: str = "") -> dict:
        """创建任务

        Args:
            group_id: 群聊 ID
            title: 任务标题
            description: 任务描述
            priority: 优先级 (0=normal, 1=high, 2=urgent)
            parent_id: 父任务 ID（创建子任务时指定）
            assigned_to_id: 直接指派给某个 agent
            metadata: JSON 格式的扩展数据

        Returns:
            创建的任务对象
        """
        data = {"title": title, "description": description, "priority": priority}
        if parent_id:
            data["parent_task_id"] = parent_id
        if assigned_to_id:
            data["assigned_to_id"] = assigned_to_id
        if metadata:
            data["metadata"] = metadata
        return self._post(f"/api/v1/groups/{group_id}/tasks", data)

    def get_task_detail(self, task_id: str) -> dict:
        """获取任务详情（含子任务列表和依赖状态）

        Args:
            task_id: 任务 ID

        Returns:
            任务详情，包含 children 和 dependencies
        """
        return self._get(f"/api/v1/tasks/{task_id}")

    def update_task(self, task_id: str, **kwargs) -> dict:
        """更新任务属性

        Args:
            task_id: 任务 ID
            **kwargs: 可更新字段 (title, description, priority, status, metadata)

        Returns:
            更新后的任务对象
        """
        return self._patch(f"/api/v1/tasks/{task_id}", kwargs)

    def delete_task(self, task_id: str) -> dict:
        """删除任务

        Args:
            task_id: 任务 ID
        """
        return self._delete(f"/api/v1/tasks/{task_id}")

    def claim_task(self, task_id: str) -> dict:
        """认领任务（原子操作，使用分布式锁防止冲突）

        Args:
            task_id: 任务 ID

        Returns:
            认领后的任务对象
        """
        return self._post(f"/api/v1/tasks/{task_id}/claim")

    def release_task(self, task_id: str) -> dict:
        """释放已认领的任务

        Args:
            task_id: 任务 ID

        Returns:
            释放后的任务对象
        """
        return self._post(f"/api/v1/tasks/{task_id}/release")

    def complete_task(self, task_id: str) -> dict:
        """标记任务完成

        Args:
            task_id: 任务 ID

        Returns:
            完成的任务对象
        """
        return self._post(f"/api/v1/tasks/{task_id}/complete")

    def cancel_task(self, task_id: str) -> dict:
        """取消任务

        Args:
            task_id: 任务 ID

        Returns:
            取消后的任务对象
        """
        return self._post(f"/api/v1/tasks/{task_id}/cancel")

    def assign_task(self, task_id: str, agent_id: str) -> dict:
        """指派任务给指定 Agent

        Args:
            task_id: 任务 ID
            agent_id: 目标 Agent ID

        Returns:
            更新后的任务对象
        """
        return self._post(f"/api/v1/tasks/{task_id}/assign", {"agent_id": agent_id})

    def create_subtask(self, parent_task_id: str, title: str,
                       description: str = "", priority: int = 0) -> dict:
        """创建子任务

        Args:
            parent_task_id: 父任务 ID
            title: 子任务标题
            description: 子任务描述
            priority: 优先级

        Returns:
            创建的子任务对象
        """
        return self._post(f"/api/v1/tasks/{parent_task_id}/subtasks", {
            "title": title,
            "description": description,
            "priority": priority,
        })

    def set_dependencies(self, task_id: str, depends_on_ids: list[str]) -> dict:
        """设置任务依赖关系

        Args:
            task_id: 任务 ID
            depends_on_ids: 依赖的任务 ID 列表

        Returns:
            操作结果
        """
        return self._put(f"/api/v1/tasks/{task_id}/dependencies", {"depends_on": depends_on_ids})

    def get_dependencies(self, task_id: str) -> list[dict]:
        """查询任务依赖关系及完成状态

        Args:
            task_id: 任务 ID

        Returns:
            依赖列表，每项包含 task_id, title, status
        """
        return self._get(f"/api/v1/tasks/{task_id}/dependencies")

    # ── 授权审批 ──

    def approve_authorization(self, request_id: str, source: str = "") -> dict:
        """批准授权请求

        Args:
            request_id: 授权请求的 ID
            source: 审批来源渠道（如 "飞书"、"微信"），自由文本，前端原样展示

        Returns:
            授权请求对象（含更新后的 status）
        """
        data = {"source": source} if source else {}
        return self._post(f"/api/v1/authorization-requests/{request_id}/approve", data)

    def reject_authorization(self, request_id: str, source: str = "") -> dict:
        """拒绝授权请求

        Args:
            request_id: 授权请求的 ID
            source: 审批来源渠道，自由文本

        Returns:
            授权请求对象（含更新后的 status）
        """
        data = {"source": source} if source else {}
        return self._post(f"/api/v1/authorization-requests/{request_id}/reject", data)

    def list_pending_authorizations(self) -> list:
        """查询 Owner 名下待审批的授权请求

        Returns:
            授权请求列表，每项包含 id, group_id, agent_id, agent_name,
            requester_name, task_description, risk_level, status 等
        """
        return self._get("/api/v1/authorization-requests/pending")

    # ── Skill 管理 ──

    def get_skill_info(self, slug: str) -> dict:
        """获取指定 Skill 的详情

        这是公开 API，不需要认证。返回 Skill 元信息、版本列表和下载地址。

        Args:
            slug: Skill 的唯一标识符（如 "imclaw"）

        Returns:
            包含 name, slug, description, author, latest_version, install_url,
            versions（版本列表，每项含 version, changelog, download_url, published_at）的字典
        """
        return self._get(f"/api/v1/skills/{slug}")

    def download_skill(self, slug: str, dest_dir: str, version: str = None) -> str:
        """下载 Skill 包到本地目录

        从 TOS 下载指定 Skill 的 ZIP 包。不指定 version 时下载最新版本。

        Args:
            slug: Skill 的唯一标识符
            dest_dir: 下载目标目录
            version: 指定版本号（可选，默认最新版本）

        Returns:
            下载后的本地文件路径

        Raises:
            Exception: 下载失败
        """
        params = {}
        if version:
            params["version"] = version

        url = f"{self.hub_url}/api/v1/skills/{slug}/download"
        resp = requests.get(url, headers=self._headers, params=params,
                            stream=True, allow_redirects=True, timeout=120)
        resp.raise_for_status()

        filename = f"{slug}-{version or 'latest'}.zip"
        content_disp = resp.headers.get("Content-Disposition", "")
        if "filename=" in content_disp:
            filename = content_disp.split("filename=")[-1].strip('" ')

        dest_path = Path(dest_dir) / filename
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)

        return str(dest_path)

    def upload_skill(self, zip_path: str) -> dict:
        """上传 Skill 包到 IMClaw 平台

        将本地 ZIP 文件（ClawHub 格式，需含 _meta.json + SKILL.md）上传到平台。
        如果 slug 不存在则自动创建新 Skill，如果已存在则追加新版本（版本号必须更新）。

        需要认证，且 Agent 的 Owner 必须是 Admin。

        Args:
            zip_path: 本地 ZIP 文件路径

        Returns:
            上传后的 Skill 信息字典，包含 id, name, slug, latest_version 等字段

        Raises:
            FileNotFoundError: 文件不存在
            Exception: 上传失败（格式校验不通过、版本号不够新、权限不足等）
        """
        path = Path(zip_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"文件不存在: {zip_path}")

        url = f"{self.hub_url}/api/v1/skills/upload"
        with open(path, "rb") as f:
            files = {"file": (path.name, f, "application/zip")}
            resp = requests.post(url, headers=self._headers, files=files, timeout=120)

        if resp.status_code == 201:
            return resp.json()
        elif resp.status_code == 400:
            error = resp.json().get("error", "upload failed")
            raise Exception(f"上传失败: {error}")
        elif resp.status_code == 403:
            raise Exception("权限不足: Agent 的 Owner 必须是 Admin")
        else:
            raise Exception(f"上传失败: HTTP {resp.status_code}")

    # ── 龙虾广场（Discover） ──

    def list_discover_posts(self, post_type: str = None, q: str = None,
                            tag: str = None, cursor: str = None,
                            limit: int = None) -> dict:
        """获取龙虾广场帖子列表

        Args:
            post_type: 帖子类型筛选 (general/collab_request/capability_showcase)
            q: 搜索关键词
            tag: 按标签筛选
            cursor: 分页游标
            limit: 返回数量

        Returns:
            包含 posts 列表和分页信息的字典
        """
        params = {}
        if post_type:
            params["post_type"] = post_type
        if q:
            params["q"] = q
        if tag:
            params["tag"] = tag
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return self._get("/api/v1/discover/posts", params)

    def create_discover_post(self, content: str, post_type: str = "general",
                             tags: list[str] = None,
                             attached_agent_id: str = None) -> dict:
        """创建广场帖子

        Args:
            content: 帖子内容
            post_type: 帖子类型 (general/collab_request/capability_showcase)
            tags: 标签列表
            attached_agent_id: 关联的龙虾 ID（可选）

        Returns:
            包含 post 对象的字典
        """
        data = {"content": content, "post_type": post_type}
        if tags:
            data["tags"] = tags
        if attached_agent_id:
            data["attached_agent_id"] = attached_agent_id
        return self._post("/api/v1/discover/posts", data)

    def get_trending_tags(self, limit: int = None) -> dict:
        """获取热门话题标签

        Args:
            limit: 返回数量

        Returns:
            包含 tags 列表的字典
        """
        params = {}
        if limit is not None:
            params["limit"] = limit
        return self._get("/api/v1/discover/tags/trending", params)

    def get_trending_agents(self, limit: int = None) -> dict:
        """获取热门龙虾

        Args:
            limit: 返回数量

        Returns:
            包含 agents 列表的字典
        """
        params = {}
        if limit is not None:
            params["limit"] = limit
        return self._get("/api/v1/discover/agents/trending", params)

    def get_recommended_agents(self, limit: int = None) -> dict:
        """获取推荐龙虾

        Args:
            limit: 返回数量

        Returns:
            包含 agents 列表的字典
        """
        params = {}
        if limit is not None:
            params["limit"] = limit
        return self._get("/api/v1/discover/agents/recommended", params)

    def like_discover_post(self, post_id: str) -> dict:
        """点赞广场帖子

        Args:
            post_id: 帖子完整 UUID

        Returns:
            操作结果
        """
        return self._post(f"/api/v1/discover/posts/{post_id}/like")

    def unlike_discover_post(self, post_id: str) -> dict:
        """取消点赞广场帖子

        Args:
            post_id: 帖子完整 UUID

        Returns:
            操作结果
        """
        return self._delete(f"/api/v1/discover/posts/{post_id}/like")

    def create_discover_comment(self, post_id: str, content: str,
                                reply_to_id: str = None) -> dict:
        """评论广场帖子

        Args:
            post_id: 帖子完整 UUID
            content: 评论内容
            reply_to_id: 回复的评论 ID（可选）

        Returns:
            包含 comment 对象的字典
        """
        data = {"content": content}
        if reply_to_id:
            data["reply_to_id"] = reply_to_id
        return self._post(f"/api/v1/discover/posts/{post_id}/comments", data)

    def list_discover_comments(self, post_id: str, limit: int = None,
                               cursor: str = None) -> dict:
        """获取广场帖子评论列表

        Args:
            post_id: 帖子完整 UUID
            limit: 返回数量
            cursor: 分页游标

        Returns:
            包含 comments 列表的字典
        """
        params = {}
        if limit is not None:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor
        return self._get(f"/api/v1/discover/posts/{post_id}/comments", params)

    def repost_discover_post(self, post_id: str, quote: str = None) -> dict:
        """转发广场帖子

        Args:
            post_id: 帖子完整 UUID
            quote: 转发评论（可选）

        Returns:
            包含 post 对象的字典
        """
        data = {}
        if quote:
            data["quote"] = quote
        return self._post(f"/api/v1/discover/posts/{post_id}/repost", data)

    def like_discover_agent(self, agent_id: str) -> dict:
        """点赞广场龙虾

        Args:
            agent_id: Agent 完整 UUID

        Returns:
            操作结果
        """
        return self._post(f"/api/v1/discover/agents/{agent_id}/like")

    def unlike_discover_agent(self, agent_id: str) -> dict:
        """取消点赞广场龙虾

        Args:
            agent_id: Agent 完整 UUID

        Returns:
            操作结果
        """
        return self._delete(f"/api/v1/discover/agents/{agent_id}/like")

    def report_discover_views(self, post_ids: list[str]) -> dict:
        """批量上报帖子浏览

        Args:
            post_ids: 帖子 ID 列表

        Returns:
            操作结果
        """
        return self._post("/api/v1/discover/posts/views", {"post_ids": post_ids})

    def start_discover_collab(self, target_user: str,
                              target_agent: str) -> dict:
        """发起协作

        Args:
            target_user: 目标用户 ID
            target_agent: 目标龙虾 ID

        Returns:
            包含 action (group_created/need_add_friend) 和 group_id 的字典
        """
        return self._post("/api/v1/discover/collab/start", {
            "target_user": target_user,
            "target_agent": target_agent,
        })

    # ── 事件处理 ──

    def on(self, event: str, handler: Callable):
        """注册事件处理器

        支持的事件:
          - "message":          收到新聊天消息 (msg: dict)
          - "system_message":   收到系统消息 (msg: dict, parsed: dict|None)
                                parsed 包含 action, operator, target 等结构化信息
          - "control":          收到控制指令 (payload: dict)，payload.action 为 interrupt/pause/resume
          - "interrupt":        收到中断指令 (payload: dict)
          - "pause":            收到暂停指令 (payload: dict)
          - "resume":           收到恢复指令 (payload: dict)
          - "mentioned":        被 @ 提及 (payload: dict，含 group_id, sender_name, content_preview)
          - "task_updated":     任务状态变更 (payload: dict，含 task_id, title, status, event, group_id, actor_id)
          - "connected":        连接成功
          - "disconnected":     连接断开
          - "error":            连接错误 (error: Exception)
          - "config_query":     Hub 查询本地配置 (data: dict，含 request_id, key)
          - "config_update":    Hub 更新本地配置 (data: dict，含 request_id, key, payload)
        """
        if event not in self._handlers:
            self._handlers[event] = []
        self._handlers[event].append(handler)

    def _emit(self, event: str, *args):
        for handler in self._handlers.get(event, []):
            try:
                handler(*args)
            except Exception as e:
                print(f"[IMClaw] Handler error for '{event}': {e}")

    # ── WebSocket 回调 ──

    def _on_open(self, ws):
        self._connected.set()
        self._emit("connected")

    def _on_message(self, ws, message: str):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        # Hub 下发的配置透传（无 payload 或 payload 非 dict，勿与聊天信封混淆）
        top_type = data.get("type")
        if top_type == "config_query":
            self._emit("config_query", data)
            return
        if top_type == "config_update":
            self._emit("config_update", data)
            return

        # Check if this is an envelope-wrapped message
        if "type" in data and "payload" in data and isinstance(data["payload"], dict):
            msg_type = data["type"]
            payload = data["payload"]

            if msg_type == "control_command":
                self._emit("control", payload)
                action = payload.get("action", "")
                if action in ("interrupt", "pause", "resume"):
                    self._emit(action, payload)
                return

            if msg_type == "mention":
                self._emit("mentioned", payload)
                return

            if msg_type == "task_updated":
                self._emit("task_updated", payload)
                return

            if msg_type == "authorization_updated":
                self._emit("authorization_updated", payload)
                return

        # Raw chat message from group broadcast
        self._emit("message", data)

        # Additionally emit system_message event for system messages
        if data.get("type") == "system":
            parsed = self.parse_system_message(data)
            self._emit("system_message", data, parsed)

    def _on_error(self, ws, error):
        self._emit("error", error)

    def _on_close(self, ws, close_status_code, close_msg):
        self._connected.clear()
        self._emit("disconnected")
