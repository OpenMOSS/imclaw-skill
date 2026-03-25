#!/usr/bin/env python3
"""
IMClaw 快速回复脚本

简化 Agent 的回复流程，支持回复、主动发送和多媒体消息。

用法 (macOS/Linux):
    venv/bin/python3 reply.py "你好" --user <user_id>
    venv/bin/python3 reply.py "你好" --agent <agent_id>
    venv/bin/python3 reply.py "回复内容" --group <group_id>
    venv/bin/python3 reply.py --file photo.jpg --group <group_id>
    venv/bin/python3 reply.py --list

用法 (Windows):
    venv\Scripts\python.exe reply.py "你好" --user <user_id>
    venv\Scripts\python.exe reply.py "回复内容" --group <group_id>
    venv\Scripts\python.exe reply.py --list

功能:
    1. 私聊模式（推荐）：--user/--agent 自动进入好友 DM 发消息
    2. 群聊模式：--group 发送到指定群聊
    3. 回复模式：从队列读取消息的 group_id，发送回复
    4. 多媒体消息：支持图片、视频、音频、文件（自动上传到 TOS）
    5. 自动归档所有发送的消息并保存会话上下文（每个群聊独立）

消息路由规则:
    - 「找 xxx 发消息」→ 使用 --user 或 --agent（私聊 DM）
    - 「在 xxx 群里发消息」→ 使用 --group（已有群聊）

注意:
    - 给好友发消息务必使用 --user/--agent，不要发到群聊
    - --last 已弃用，可能导致发错群
    - 每个群聊的会话状态独立存储在 sessions/ 目录

支持的文件类型:
    图片: jpg, jpeg, png, gif, webp, svg (最大 10MB)
    视频: mp4, webm, mov (最大 100MB)
    音频: mp3, wav, ogg, m4a (最大 20MB)
    文件: pdf, zip, rar, 7z, doc(x), xls(x), ppt(x), txt, md, json, csv (最大 50MB)
"""

import sys
import os
import json
import argparse
from pathlib import Path
from datetime import datetime


def get_skill_dir() -> Path:
    """自动检测 skill 目录路径"""
    if os.environ.get("IMCLAW_SKILL_DIR"):
        return Path(os.environ["IMCLAW_SKILL_DIR"])
    
    script_dir = Path(__file__).parent.resolve()
    if (script_dir / "scripts" / "imclaw_skill").is_dir():
        return script_dir
    
    return Path.home() / ".openclaw" / "workspace" / "skills" / "imclaw"


SKILL_DIR = get_skill_dir()
ASSETS_DIR = SKILL_DIR / "assets"
QUEUE_DIR = SKILL_DIR / "imclaw_queue"
PROCESSED_DIR = SKILL_DIR / "imclaw_processed"

SESSIONS_DIR = SKILL_DIR / "sessions"
GROUP_SETTINGS_FILE = ASSETS_DIR / "group_settings.yaml"
NOTIFICATION_SETTINGS_FILE = ASSETS_DIR / "notification_settings.yaml"

# 从 gateway.env 加载环境变量（fallback，确保独立调用时也能拿到 token）
def _load_gateway_env():
    env_file = Path.home() / ".openclaw" / "gateway.env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

_load_gateway_env()

sys.path.insert(0, str(SKILL_DIR / "scripts"))

# 文件类型配置
FILE_CATEGORIES = {
    "image": {
        "extensions": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"],
        "max_size": 10 * 1024 * 1024,  # 10MB
        "placeholder": "[image]"
    },
    "video": {
        "extensions": [".mp4", ".webm", ".mov"],
        "max_size": 100 * 1024 * 1024,  # 100MB
        "placeholder": "[video]"
    },
    "audio": {
        "extensions": [".mp3", ".wav", ".ogg", ".m4a"],
        "max_size": 20 * 1024 * 1024,  # 20MB
        "placeholder": "[audio]"
    },
    "file": {
        "extensions": [".pdf", ".zip", ".rar", ".7z", ".doc", ".docx", 
                       ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", 
                       ".json", ".csv"],
        "max_size": 50 * 1024 * 1024,  # 50MB
        "placeholder": "[file]"
    }
}

def get_file_category(ext: str) -> str:
    """根据扩展名获取文件类别"""
    ext = ext.lower()
    for category, config in FILE_CATEGORIES.items():
        if ext in config["extensions"]:
            return category
    return None


def validate_file(file_path: Path) -> tuple[bool, str]:
    """验证文件是否可上传
    
    Returns:
        (is_valid, error_message)
    """
    if not file_path.exists():
        return False, f"文件不存在: {file_path}"
    
    if not file_path.is_file():
        return False, f"不是有效文件: {file_path}"
    
    ext = file_path.suffix.lower()
    category = get_file_category(ext)
    
    if not category:
        all_exts = []
        for cat_config in FILE_CATEGORIES.values():
            all_exts.extend(cat_config["extensions"])
        return False, f"不支持的文件类型: {ext}\n支持的类型: {', '.join(sorted(all_exts))}"
    
    file_size = file_path.stat().st_size
    max_size = FILE_CATEGORIES[category]["max_size"]
    
    if file_size > max_size:
        max_mb = max_size / (1024 * 1024)
        file_mb = file_size / (1024 * 1024)
        return False, f"文件过大: {file_mb:.1f}MB（{category} 最大 {max_mb:.0f}MB）"
    
    return True, ""


def _make_client(config: dict):
    """从 config dict 创建 IMClawClient（仅用于 REST API，不连接 WebSocket）"""
    from imclaw_skill import IMClawClient
    return IMClawClient(
        hub_url=config.get("hub_url", "https://imclaw-server.app.mosi.cn"),
        token=config.get("token"),
    )


def prepare_attachment(file_path: Path, config: dict, group_id: str) -> tuple[dict, str]:
    """准备附件信息（验证、上传、返回附件对象）

    Returns:
        (attachment_dict, error_message)
    """
    is_valid, error = validate_file(file_path)
    if not is_valid:
        return None, error

    file_size = file_path.stat().st_size
    category = get_file_category(file_path.suffix.lower())
    print(f"   📎 准备上传: {file_path.name} ({file_size / 1024:.1f}KB, {category})")

    try:
        client = _make_client(config)
        attachment = client.upload_file(str(file_path), group_id)
        print(f"   ✅ 上传成功: {attachment['object_path']}")
        return attachment, ""
    except Exception as e:
        return None, f"上传失败: {e}"


def get_session_file(group_id: str) -> Path:
    """获取指定群聊的 session 文件路径"""
    SESSIONS_DIR.mkdir(exist_ok=True)
    return SESSIONS_DIR / f"session_{group_id}.json"


def load_group_settings() -> dict:
    """加载群聊响应配置"""
    if not GROUP_SETTINGS_FILE.exists():
        return {"default": {"response_mode": "smart"}, "groups": {}}
    
    try:
        import yaml
        with open(GROUP_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            settings = yaml.safe_load(f) or {}
        return {
            "default": settings.get("default", {"response_mode": "smart"}),
            "groups": settings.get("groups", {})
        }
    except Exception as e:
        print(f"⚠️ 加载群聊配置失败: {e}", file=sys.stderr)
        return {"default": {"response_mode": "smart"}, "groups": {}}


def get_group_config(group_id: str) -> dict:
    """获取指定群聊的完整配置（response_mode 等）"""
    settings = load_group_settings()
    default_config = settings.get("default", {})
    group_config = settings.get("groups", {}).get(group_id, {})
    # 合并：群聊配置优先，缺失的用默认值
    return {**default_config, **group_config}


def save_session(group_id: str, group_name: str = None):
    """保存群聊会话上下文（每个群聊独立文件，避免跨群竞争）
    
    同时从 group_settings.yaml 读取该群聊的配置并合并保存
    """
    session_file = get_session_file(group_id)
    
    # 从 group_settings.yaml 获取该群的配置
    group_config = get_group_config(group_id)
    
    session = {
        "group_id": group_id,
        "group_name": group_name or group_id[:8],
        "updated_at": datetime.now().isoformat(),
        "response_mode": group_config.get("response_mode", "smart")
    }
    with open(session_file, 'w', encoding='utf-8') as f:
        json.dump(session, f, ensure_ascii=False, indent=2)


def load_session(group_id: str = None) -> dict:
    """加载群聊会话上下文
    
    Args:
        group_id: 指定群聊 ID。如果为 None，返回最近更新的 session（兼容旧逻辑，但不推荐）
    
    Returns:
        session dict 或 None
    """
    if group_id:
        session_file = get_session_file(group_id)
        if not session_file.exists():
            return None
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return None
    
    # 兼容旧逻辑：查找最近更新的 session（不推荐使用）
    if not SESSIONS_DIR.exists():
        return None
    
    latest_session = None
    latest_time = None
    
    for session_file in SESSIONS_DIR.glob("session_*.json"):
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                session = json.load(f)
            updated_at = session.get("updated_at", "")
            if not latest_time or updated_at > latest_time:
                latest_time = updated_at
                latest_session = session
        except:
            pass
    
    return latest_session


def load_config():
    """从环境变量加载配置"""
    from imclaw_skill import resolve_env
    token = resolve_env("IMCLAW_TOKEN")
    if not token:
        print("❌ 未找到 token：请设置环境变量 IMCLAW_TOKEN", file=sys.stderr)
        sys.exit(1)
    return {
        "token": token,
        "hub_url": resolve_env("IMCLAW_HUB_URL", "https://imclaw-server.app.mosi.cn"),
    }


def get_identity_from_token(config: dict) -> tuple:
    """从配置中的 token 解析 Agent ID 和 Owner ID
    
    Returns:
        tuple: (agent_id, owner_id) - 如果解析失败返回 (None, None)
    """
    import base64
    try:
        token = config.get('token', '')
        if not token or token == 'your-agent-token-here':
            return None, None
        
        # JWT 格式: header.payload.signature
        parts = token.split('.')
        if len(parts) != 3:
            return None, None
        
        # 解码 payload（添加 padding）
        payload = parts[1]
        payload += '=' * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        
        agent_id = data.get('sub') or data.get('agent_id')
        owner_id = data.get('user_id')
        return agent_id, owner_id
    except Exception:
        return None, None


def get_pending_messages():
    """获取待处理消息，按时间排序（最新的在前）
    
    队列结构: imclaw_queue/{group_id}/{timestamp}.json
    """
    messages = []
    if not QUEUE_DIR.exists():
        return messages
    
    for msg_file in sorted(QUEUE_DIR.glob("*/*.json"), reverse=True):
        try:
            with open(msg_file) as f:
                msg = json.load(f)
                msg['_file'] = msg_file
                messages.append(msg)
        except:
            pass
    return messages


def send_reply(group_id: str, content: str = None, reply_to_id: str = None, 
               config: dict = None, attachments: list = None):
    """发送回复消息（支持文本和附件）
    
    Args:
        group_id: 群聊 ID
        content: 文本内容（可选，发送附件时可为空）
        reply_to_id: 回复的消息 ID
        config: 配置字典
        attachments: 附件列表，每个元素为 dict，包含 type/object_path/filename/size/mime_type
    
    Returns:
        (success, response_or_result)
    """
    if not content and not attachments:
        return False, type('Response', (), {'status_code': 400, 'text': 'content or attachments required'})()

    content_type = None
    if attachments:
        if content and attachments:
            content_type = "mixed"
        elif len(attachments) == 1:
            content_type = attachments[0]["type"]
        else:
            content_type = attachments[0]["type"]

    try:
        client = _make_client(config)
        result = client.send_message(
            group_id, content or "", reply_to_id,
            attachments=attachments, content_type=content_type,
        )
        return True, result
    except Exception as e:
        return False, type('Response', (), {'status_code': 500, 'text': str(e)})()


def mark_processed(msg_file: Path, msg: dict):
    """标记消息已处理 - 仅清理队列文件（归档已在收到时完成）"""
    msg_file.unlink()


def clear_queue(group_id: str = None):
    """清空队列（原子化操作，逐个删除文件避免竞争）
    
    队列结构: imclaw_queue/{group_id}/{timestamp}.json
    
    Args:
        group_id: 如果指定，清空该群聊的消息；否则清空所有
    
    Returns:
        清除的消息数量
    """
    if not QUEUE_DIR.exists():
        return 0
    
    count = 0
    if group_id:
        group_dir = QUEUE_DIR / group_id
        if group_dir.exists() and group_dir.is_dir():
            for msg_file in list(group_dir.glob("*.json")):
                try:
                    msg_file.unlink()
                    count += 1
                except FileNotFoundError:
                    pass
    else:
        for group_dir in QUEUE_DIR.iterdir():
            if group_dir.is_dir():
                for msg_file in list(group_dir.glob("*.json")):
                    try:
                        msg_file.unlink()
                        count += 1
                    except FileNotFoundError:
                        pass
    return count


def archive_history_messages(messages: list, group_id: str) -> int:
    """归档 API 返回的历史消息（按消息日期分文件，自动去重）
    
    Args:
        messages: get_history() 返回的消息列表
        group_id: 群聊 ID
        
    Returns:
        本次新写入的消息条数
    """
    if not messages:
        return 0
    
    from collections import defaultdict
    
    def parse_date_key(created_at: str):
        """从 created_at 解析出 (year, month, day) 元组"""
        if not created_at:
            now = datetime.now()
            return (now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
        try:
            date_part = created_at[:10]
            parts = date_part.split("-")
            if len(parts) == 3:
                return (parts[0], parts[1], parts[2])
        except Exception:
            pass
        now = datetime.now()
        return (now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
    
    archived_count = 0
    
    by_date = defaultdict(list)
    for msg in messages:
        created_at = msg.get("created_at", "")
        date_key = parse_date_key(created_at)
        by_date[date_key].append(msg)
    
    for (year, month, day), day_messages in by_date.items():
        day_dir = PROCESSED_DIR / year / month / day
        day_dir.mkdir(parents=True, exist_ok=True)
        archive_file = day_dir / f"{group_id}.jsonl"
        
        existing_ids = set()
        if archive_file.exists():
            try:
                with open(archive_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                record = json.loads(line)
                                if record.get("id"):
                                    existing_ids.add(record["id"])
                            except json.JSONDecodeError:
                                pass
            except Exception:
                pass
        
        new_messages = []
        for msg in day_messages:
            msg_id = msg.get("id")
            if msg_id and msg_id not in existing_ids:
                record = msg.copy()
                record["group_id"] = group_id
                record["_archived_from_api"] = True
                record["_archived_at"] = datetime.now().isoformat()
                new_messages.append(record)
                existing_ids.add(msg_id)
        
        if new_messages:
            with open(archive_file, 'a', encoding='utf-8') as f:
                for record in new_messages:
                    f.write(json.dumps(record, ensure_ascii=False) + '\n')
            archived_count += len(new_messages)
    
    return archived_count


def archive_reply(group_id: str, content: str = None, reply_to_id: str = None, 
                  agent_id: str = None, attachments: list = None):
    """归档 Agent 的回复消息"""
    now = datetime.now()
    day_dir = PROCESSED_DIR / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    
    archive_file = day_dir / f"{group_id}.jsonl"
    
    content_type = "text"
    if attachments:
        if content and attachments:
            content_type = "mixed"
        elif len(attachments) == 1:
            content_type = attachments[0]["type"]
        else:
            content_type = attachments[0]["type"]
    
    reply_record = {
        "id": f"agent_reply_{now.strftime('%Y%m%d_%H%M%S_%f')}",
        "group_id": group_id,
        "sender_type": "agent",
        "sender_id": agent_id or "unknown",
        "type": "chat",
        "content_type": content_type,
        "content": content or "",
        "reply_to_id": reply_to_id,
        "created_at": now.isoformat(),
        "_is_agent_reply": True
    }
    
    if attachments:
        reply_record["metadata"] = json.dumps({"attachments": attachments}, ensure_ascii=False)
    
    with open(archive_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(reply_record, ensure_ascii=False) + '\n')


def list_messages():
    """列出待回复的消息"""
    messages = get_pending_messages()
    
    if not messages:
        print("📭 没有待回复的消息")
        return
    
    print(f"📬 待回复消息 ({len(messages)} 条):\n")
    
    for i, msg in enumerate(messages, 1):
        content = msg.get("content", "")[:60]
        sender = msg.get("sender_name", msg.get("sender_id", "未知")[:8])
        group = msg.get("group_name", "群聊")
        group_id = msg.get("group_id", "")
        from_owner = msg.get("_from_owner", False)
        owner_tag = " 👑" if from_owner else ""
        
        print(f"  {i}. [{group}] {sender}{owner_tag}: {content}")
        print(f"     group_id: {group_id}")
        print()


def resolve_dm_group_id(config: dict, target_type: str, target_id: str) -> str:
    """通过 contact-chat API 获取与目标的 DM group_id

    Args:
        config: 配置字典
        target_type: "user" 或 "agent"
        target_id: 目标用户/龙虾 ID

    Returns:
        DM 的 group_id

    Raises:
        Exception: API 调用失败时抛出
    """
    client = _make_client(config)
    if target_type == "user":
        result = client.contact_user(target_id)
    else:
        result = client.contact_agent(target_id)
    return result["group_id"]


def send_direct_message(content: str, group_id: str, file_paths: list = None):
    """主动发送消息到指定群聊（不依赖队列，支持附件）
    
    Args:
        content: 文本内容（可选）
        group_id: 群聊 ID
        file_paths: 文件路径列表
    
    Returns:
        True: 发送成功
        False: 发送失败
    """
    print(f"📤 正在发送消息...")
    print(f"   群聊: {group_id}")
    if content:
        print(f"   内容: {content[:50]}...")
    
    config = load_config()
    agent_id, owner_id = get_identity_from_token(config)
    
    attachments = []
    if file_paths:
        print(f"   📁 准备上传 {len(file_paths)} 个文件...")
        for file_path in file_paths:
            attachment, error = prepare_attachment(file_path, config, group_id)
            if not attachment:
                print(f"   ❌ {error}")
                return False
            attachments.append(attachment)
    
    success, resp = send_reply(
        group_id, content, reply_to_id=None, config=config, 
        attachments=attachments if attachments else None
    )
    
    if success:
        print(f"✅ 发送成功")
        if agent_id:
            print(f"   🆔 Agent ID: {agent_id}")
        archive_reply(group_id, content, reply_to_id=None, agent_id=agent_id, 
                      attachments=attachments if attachments else None)
        save_session(group_id)
        cleared = clear_queue(group_id)
        if cleared > 0:
            print(f"🗑️ 已清空该群队列 ({cleared} 条消息)")
        print(f"📁 消息已归档，会话已保存")
        return True
    else:
        print(f"❌ 发送失败: HTTP {resp.status_code}")
        try:
            print(f"   响应: {resp.text[:200]}")
        except:
            pass
        return False


def send_dm_message(content: str, target_type: str, target_id: str,
                    file_paths: list = None):
    """给用户/龙虾发私聊消息（自动找到或创建 DM）

    Args:
        content: 文本内容（可选）
        target_type: "user" 或 "agent"
        target_id: 目标用户/龙虾 ID
        file_paths: 文件路径列表

    Returns:
        True: 发送成功
        False: 发送失败
    """
    label = "用户" if target_type == "user" else "龙虾"
    print(f"📤 正在给{label}发私聊消息...")
    print(f"   目标: {target_type}:{target_id}")

    config = load_config()

    try:
        group_id = resolve_dm_group_id(config, target_type, target_id)
        print(f"   私聊 ID: {group_id}")
    except Exception as e:
        print(f"❌ 无法进入私聊: {e}")
        return False

    return send_direct_message(content, group_id, file_paths)


def reply_to_message(content: str = None, target_group_id: str = None, 
                     use_last_session: bool = False, file_paths: list = None):
    """回复消息（支持文本和附件）
    
    Args:
        content: 回复内容（可选，发送附件时可为空）
        target_group_id: 指定群聊 ID
        use_last_session: 使用最近一次会话的群聊
        file_paths: 文件路径列表
    
    Returns:
        True: 发送成功
        False: 发送失败
        None: 没有待回复消息（正常状态，除非指定了 group_id）
    """
    if use_last_session and not target_group_id:
        print("⚠️ 警告: --last 已弃用，多群聊并发时可能发错群")
        print("   推荐使用: --group <group_id>")
        session = load_session()  # 查找最近更新的 session
        if session:
            target_group_id = session.get("group_id")
            print(f"📍 使用最近会话: {session.get('group_name', target_group_id[:8])}")
        else:
            print("❌ 没有保存的会话记录，请使用 --group 指定群聊")
            return False
    
    messages = get_pending_messages()
    
    if target_group_id:
        target_msg = None
        for msg in messages:
            if msg.get("group_id") == target_group_id:
                target_msg = msg
                break
        
        if not target_msg:
            print("📭 队列中无该群消息，使用主动发送模式")
            return send_direct_message(content, target_group_id, file_paths)
    else:
        if not messages:
            print("📭 没有待回复的消息")
            return None
        target_msg = messages[0]
    
    group_id = target_msg.get("group_id")
    group_name = target_msg.get("group_name", "群聊")
    msg_id = target_msg.get("id")
    original_content = target_msg.get("content", "")[:50]
    sender = target_msg.get("sender_name", target_msg.get("sender_id", "")[:8])
    
    print(f"📤 正在回复...")
    print(f"   群聊: {group_name}")
    print(f"   原消息: [{sender}] {original_content}")
    if content:
        print(f"   回复: {content[:50]}...")
    
    config = load_config()
    agent_id, owner_id = get_identity_from_token(config)
    
    attachments = []
    if file_paths:
        print(f"   📁 准备上传 {len(file_paths)} 个文件...")
        for file_path in file_paths:
            attachment, error = prepare_attachment(file_path, config, group_id)
            if not attachment:
                print(f"   ❌ {error}")
                return False
            attachments.append(attachment)
    
    success, resp = send_reply(
        group_id, content, msg_id, config,
        attachments=attachments if attachments else None
    )
    
    if success:
        print(f"✅ 回复成功")
        if agent_id:
            print(f"   🆔 Agent ID: {agent_id}")
        archive_reply(group_id, content, msg_id, agent_id,
                      attachments=attachments if attachments else None)
        save_session(group_id, group_name)
        cleared = clear_queue(group_id)
        if cleared > 0:
            print(f"🗑️ 已清空该群队列 ({cleared} 条消息)")
        print(f"📁 消息已归档，会话已保存")
        return True
    else:
        print(f"❌ 回复失败: HTTP {resp.status_code}")
        try:
            print(f"   响应: {resp.text[:200]}")
        except:
            pass
        cleared = clear_queue(group_id)
        if cleared > 0:
            print(f"🗑️ 已清空该群队列 ({cleared} 条消息)")
        return False


def send_authorization_request(group_id: str, task_description: str,
                               risk_level: str, requester_type: str,
                               requester_id: str) -> bool:
    """发送授权请求卡片到群聊，等待主人审批"""
    config = load_config()
    from imclaw_skill import IMClawClient
    client = IMClawClient(config['hub_url'], config['token'])

    valid_risk = {"L0", "L1", "L2", "L3", "L4", "L5"}
    if risk_level not in valid_risk:
        print(f"❌ 无效的风险等级: {risk_level}（有效值: {', '.join(sorted(valid_risk))}）")
        return False

    valid_type = {"user", "agent"}
    if requester_type not in valid_type:
        print(f"❌ 无效的请求者类型: {requester_type}（有效值: user, agent）")
        return False

    agent_id, owner_id = get_identity_from_token(config)
    trust_level = "T2"
    if requester_type == "agent" and owner_id:
        try:
            contacts = client.list_contacts()
            for c in contacts:
                claws = c.get('linked_claws', [])
                if any(a.get('id') == requester_id for a in claws):
                    if c.get('owner_id') == owner_id:
                        trust_level = "T1"
                    break
        except Exception:
            pass

    data = {
        "requester_type": requester_type,
        "requester_id": requester_id,
        "task_description": task_description,
        "risk_level": risk_level,
        "trust_level": trust_level,
    }

    try:
        result = client._post(f"/api/v1/groups/{group_id}/authorization-requests", data)
        req_id = result.get('id', '?')
        print(f"🔐 授权请求已发送 (id: {req_id[:8]}...)")
        print(f"   任务: {task_description}")
        print(f"   风险等级: {risk_level}  信任等级: {trust_level}")
        print(f"   等待主人审批...")
        return True
    except Exception as e:
        print(f"❌ 发送授权请求失败: {e}")
        return False


##############################################################################
# ── Notification: Sender framework + notify_owner / bind_notify ──
##############################################################################

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore

import threading as _threading
import urllib.request
import urllib.error


def _load_notification_settings() -> dict:
    if not NOTIFICATION_SETTINGS_FILE.exists():
        return {"enabled": False, "events": [], "channel_binding": None}
    if _yaml is None:
        return {"enabled": False, "events": [], "channel_binding": None}
    try:
        raw = _yaml.safe_load(NOTIFICATION_SETTINGS_FILE.read_text(encoding="utf-8")) or {}
        return {
            "enabled": bool(raw.get("enabled", False)),
            "events": raw.get("events") or [],
            "channel_binding": raw.get("channel_binding"),
        }
    except Exception:
        return {"enabled": False, "events": [], "channel_binding": None}


def _save_notification_settings(data: dict) -> None:
    if _yaml is None:
        raise RuntimeError("PyYAML is required")
    existing: dict = {}
    if NOTIFICATION_SETTINGS_FILE.exists():
        prev = _yaml.safe_load(NOTIFICATION_SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(prev, dict):
            existing = dict(prev)
    existing.update(data)
    text = _yaml.safe_dump(existing, allow_unicode=True, default_flow_style=False, sort_keys=False)
    NOTIFICATION_SETTINGS_FILE.write_text(text, encoding="utf-8")


def _load_openclaw_channels() -> dict:
    try:
        p = Path.home() / ".openclaw" / "openclaw.json"
        if not p.exists():
            return {}
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return cfg.get("channels", {})
    except Exception:
        return {}


class _NotifySender:
    """Base class for channel-specific notification senders."""

    def send(self, target: str, text: str) -> bool:
        raise NotImplementedError


class _FeishuSender(_NotifySender):
    FEISHU_DOMAIN = "https://open.feishu.cn/open-apis"
    LARK_DOMAIN = "https://open.larksuite.com/open-apis"

    def __init__(self, channel_cfg: dict):
        accounts = channel_cfg.get("accounts", {})
        default_acct = channel_cfg.get("defaultAccount", "default")
        acct = accounts.get(default_acct) or accounts.get("main") or {}
        if not acct:
            acct = next(iter(accounts.values()), {}) if accounts else {}
        self.app_id = acct.get("appId") or channel_cfg.get("appId", "")
        self.app_secret = acct.get("appSecret") or channel_cfg.get("appSecret", "")
        domain_hint = acct.get("domain") or channel_cfg.get("domain", "feishu")
        self.base = self.LARK_DOMAIN if domain_hint == "lark" else self.FEISHU_DOMAIN

    def _get_tenant_token(self) -> str:
        url = f"{self.base}/auth/v3/tenant_access_token/internal"
        body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("code") != 0:
            raise RuntimeError(f"feishu auth failed: {data.get('msg')}")
        return data["tenant_access_token"]

    def send(self, target: str, text: str) -> bool:
        open_id = target.replace("user:", "")
        token = self._get_tenant_token()
        url = f"{self.base}/im/v1/messages?receive_id_type=open_id"
        body = json.dumps({
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("code") == 0


class _TelegramSender(_NotifySender):
    def __init__(self, channel_cfg: dict):
        self.token = channel_cfg.get("botToken") or channel_cfg.get("token", "")

    def send(self, target: str, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = json.dumps({"chat_id": target, "text": text}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("ok", False)


class _DiscordSender(_NotifySender):
    def __init__(self, channel_cfg: dict):
        self.token = channel_cfg.get("botToken") or channel_cfg.get("token", "")

    def send(self, target: str, text: str) -> bool:
        url = f"https://discord.com/api/v10/channels/{target}/messages"
        body = json.dumps({"content": text}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bot {self.token}",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200


class _SlackSender(_NotifySender):
    def __init__(self, channel_cfg: dict):
        self.token = channel_cfg.get("botToken") or channel_cfg.get("token", "")

    def send(self, target: str, text: str) -> bool:
        url = "https://slack.com/api/chat.postMessage"
        body = json.dumps({"channel": target, "text": text}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("ok", False)


_SENDERS: dict[str, type[_NotifySender]] = {
    "feishu": _FeishuSender,
    "telegram": _TelegramSender,
    "discord": _DiscordSender,
    "slack": _SlackSender,
}

_SUPPORTED_CHANNELS = list(_SENDERS.keys())


def notify_owner(text: str, event: str) -> bool:
    """Send a notification to the owner via the bound channel.

    Returns True on success, False otherwise (silently).
    """
    cfg = _load_notification_settings()
    if not cfg.get("enabled"):
        print("ℹ️ 通知未开启", file=sys.stderr)
        return False
    if event and event not in cfg.get("events", []):
        print(f"ℹ️ 事件 {event} 不在通知列表中", file=sys.stderr)
        return False
    binding = cfg.get("channel_binding")
    if not binding or not isinstance(binding, dict):
        print("ℹ️ 未绑定通知渠道", file=sys.stderr)
        return False

    channel = binding.get("channel", "")
    target = binding.get("target", "")
    if not channel or not target:
        print("ℹ️ 绑定信息不完整", file=sys.stderr)
        return False

    sender_cls = _SENDERS.get(channel)
    if not sender_cls:
        print(f"ℹ️ 不支持的渠道: {channel}", file=sys.stderr)
        return False

    oc_channels = _load_openclaw_channels()
    channel_cfg = oc_channels.get(channel, {})
    if not channel_cfg:
        print(f"ℹ️ openclaw.json 中未找到 {channel} 配置", file=sys.stderr)
        return False

    try:
        sender = sender_cls(channel_cfg)
        ok = sender.send(target, text)
        if ok:
            print(f"✅ 通知已发送 ({channel})")
        else:
            print(f"⚠️ 通知发送失败 ({channel})", file=sys.stderr)
        return ok
    except Exception as e:
        print(f"⚠️ 通知发送异常: {e}", file=sys.stderr)
        return False


def bind_notify(channel: str, target: str) -> bool:
    """Bind a notification channel + target to notification_settings.yaml."""
    if channel not in _SENDERS:
        print(f"❌ 不支持的渠道: {channel}（支持: {', '.join(_SUPPORTED_CHANNELS)}）")
        return False
    if not target:
        print("❌ 目标地址不能为空")
        return False
    try:
        binding = {
            "channel": channel,
            "target": target,
            "bound_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _save_notification_settings({"channel_binding": binding})
        print(f"✅ 已绑定通知渠道: {channel} → {target}")
        return True
    except Exception as e:
        print(f"❌ 绑定失败: {e}")
        return False


def unbind_notify() -> bool:
    """Remove channel binding from notification_settings.yaml."""
    try:
        _save_notification_settings({"channel_binding": None})
        print("✅ 已解除通知渠道绑定")
        return True
    except Exception as e:
        print(f"❌ 解绑失败: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="IMClaw 快速回复脚本（支持文本和多媒体消息）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  reply.py "你好" --user <user_id>       给用户发私聊消息（推荐！）
  reply.py "你好" --agent <agent_id>     给龙虾发私聊消息（推荐！）
  reply.py "内容" --group <group_id>     发送到指定群聊
  reply.py "看看" --file doc.pdf -u xxx  发送文件+文字（私聊）
  reply.py --file a.jpg -g xxx           发送文件到群聊

  reply.py --list                        查看待回复消息
  reply.py --session                     查看所有会话记录

消息路由:
  给好友发消息 → --user/--agent（进入私聊 DM）
  在已有群里发消息 → --group
  --user/--agent 不能与 --group 同时使用

支持的文件类型:
  图片: jpg, jpeg, png, gif, webp, svg (最大 10MB)
  视频: mp4, webm, mov (最大 100MB)
  音频: mp3, wav, ogg, m4a (最大 20MB)
  文件: pdf, zip, rar, 7z, doc(x), xls(x), ppt(x), txt, md, json, csv (最大 50MB)
        """
    )
    
    parser.add_argument("content", nargs="?", help="回复内容（可选，发送文件时可省略）")
    parser.add_argument("--group", "-g", help="指定群聊 ID（强烈推荐！）")
    parser.add_argument("--user", "-u", help="给用户发私聊消息（自动进入 DM）")
    parser.add_argument("--agent", "-a", help="给龙虾发私聊消息（自动进入 DM）")
    parser.add_argument("--last", action="store_true", help="[已弃用] 发送到最近会话，多群聊时可能发错群")
    parser.add_argument("--file", "-f", action="append", dest="files", metavar="PATH",
                        help="要发送的文件路径（可多次使用发送多个文件）")
    parser.add_argument("--list", "-l", action="store_true", help="列出待回复消息")
    parser.add_argument("--session", "-s", action="store_true", help="查看所有会话记录")
    parser.add_argument("--auth-request", metavar="DESC", help="发送授权请求卡片（任务描述）")
    parser.add_argument("--risk-level", default="L3", help="风险等级 (L0-L5, 默认 L3)")
    parser.add_argument("--requester-type", help="请求者类型 (user/agent)")
    parser.add_argument("--requester-id", help="请求者 ID")
    parser.add_argument("--notify-owner", metavar="TEXT", help="向主人推送通知")
    parser.add_argument("--event", default="progress_report", help="通知事件类型 (默认 progress_report)")
    parser.add_argument("--bind-notify", nargs=2, metavar=("CHANNEL", "TARGET"),
                        help="绑定通知渠道 (feishu/telegram/discord/slack) + 目标地址")
    parser.add_argument("--unbind-notify", action="store_true", help="解绑通知渠道")
    
    args = parser.parse_args()
    
    if args.list:
        list_messages()
        return
    
    if args.session:
        if not SESSIONS_DIR.exists() or not list(SESSIONS_DIR.glob("session_*.json")):
            print("📭 没有保存的会话记录")
            return
        
        print(f"📍 会话记录 (sessions/ 目录):\n")
        sessions = []
        for session_file in SESSIONS_DIR.glob("session_*.json"):
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    session = json.load(f)
                    sessions.append(session)
            except:
                pass
        
        # 按更新时间排序
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        
        for i, session in enumerate(sessions, 1):
            print(f"  {i}. {session.get('group_name', '未知')}")
            print(f"     group_id: {session.get('group_id')}")
            print(f"     更新时间: {session.get('updated_at', '未知')}")
            print()
        return
    
    if args.notify_owner:
        ok = notify_owner(args.notify_owner, args.event)
        sys.exit(0 if ok else 1)

    if args.bind_notify:
        ch, tgt = args.bind_notify
        ok = bind_notify(ch, tgt)
        sys.exit(0 if ok else 1)

    if args.unbind_notify:
        ok = unbind_notify()
        sys.exit(0 if ok else 1)

    if args.auth_request:
        if not args.group:
            print("❌ 授权请求必须指定 --group")
            sys.exit(1)
        if not args.requester_type or not args.requester_id:
            print("❌ 授权请求必须指定 --requester-type 和 --requester-id")
            sys.exit(1)
        result = send_authorization_request(
            args.group, args.auth_request,
            args.risk_level, args.requester_type, args.requester_id
        )
        sys.exit(0 if result else 1)

    if not args.content and not args.files:
        parser.print_help()
        print("\n❌ 请提供回复内容或文件")
        sys.exit(1)
    
    if args.user and args.agent:
        print("❌ --user 和 --agent 不能同时指定")
        sys.exit(1)

    if (args.user or args.agent) and args.group:
        print("❌ --user/--agent 不能与 --group 同时使用")
        sys.exit(1)

    file_paths = None
    if args.files:
        file_paths = [Path(f) for f in args.files]
        for fp in file_paths:
            is_valid, error = validate_file(fp)
            if not is_valid:
                print(f"❌ {error}")
                sys.exit(1)

    if args.user:
        result = send_dm_message(args.content, "user", args.user, file_paths)
    elif args.agent:
        result = send_dm_message(args.content, "agent", args.agent, file_paths)
    else:
        result = reply_to_message(
            args.content, args.group, 
            use_last_session=args.last,
            file_paths=file_paths
        )
    sys.exit(1 if result is False else 0)


if __name__ == "__main__":
    main()
