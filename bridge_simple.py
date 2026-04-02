#!/usr/bin/env python3
"""
IMClaw 连接 Agent

职责：
1. 保持 WebSocket 连接到 IMClaw Hub
2. 收到消息 → 写入队列 → hooks/wake 唤醒主会话
3. 不处理任何逻辑，只做转发

环境变量：
- OPENCLAW_GATEWAY_URL: OpenClaw Gateway 地址（默认 http://127.0.0.1:18789）
- OPENCLAW_HOOKS_TOKEN: OpenClaw hooks token（必需，需与 openclaw.json 中配置一致）
"""

import sys
import os
import json
import time
import base64
import signal
import atexit
import logging
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

try:
    import yaml
    from typing import Optional
except ImportError:
    yaml = None  # type: ignore

# 配置 logging（线程安全，替代 print）
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


class PIDManager:
    """PID 文件管理器 - 确保 PID 文件的准确性，防止重复启动"""
    
    def __init__(self, pid_file: Path, process_name: str = "bridge_simple.py"):
        self.pid_file = pid_file
        self.process_name = process_name
        self.pid = os.getpid()
        self._registered = False
        self._shutdown_requested = False
    
    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """检测进程是否存活（跨平台）"""
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, 0, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            try:
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False

    def _find_other_instances(self) -> list[int]:
        """查找其他同名进程（排除自己）"""
        other_pids = []
        try:
            import subprocess
            if sys.platform == "win32":
                result = subprocess.run(
                    ["wmic", "process", "where",
                     f"CommandLine like '%{self.process_name}%'",
                     "get", "ProcessId"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n'):
                        line = line.strip()
                        if line.isdigit():
                            pid = int(line)
                            if pid != self.pid:
                                other_pids.append(pid)
            else:
                result = subprocess.run(
                    ["pgrep", "-f", self.process_name],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split('\n'):
                        if line:
                            pid = int(line)
                            if pid != self.pid:
                                other_pids.append(pid)
        except Exception:
            pass
        return other_pids
    
    def is_running(self) -> tuple[bool, list[int]]:
        """检查是否有其他实例正在运行"""
        running_pids = []
        
        if self.pid_file.exists():
            try:
                old_pid = int(self.pid_file.read_text().strip())
                if old_pid != self.pid and self._is_pid_alive(old_pid):
                    running_pids.append(old_pid)
            except (ValueError, OSError):
                pass
        
        other_pids = self._find_other_instances()
        for pid in other_pids:
            if pid not in running_pids:
                running_pids.append(pid)
        
        return len(running_pids) > 0, running_pids
    
    def acquire(self, force: bool = False) -> bool:
        """获取 PID 锁，写入当前进程的 PID"""
        running, running_pids = self.is_running()
        
        if running and not force:
            logger.warning(f"⚠️ 已有 {len(running_pids)} 个实例运行中: {running_pids}")
            if sys.platform == "win32":
                logger.info(f"   请先停止旧进程: taskkill /F /PID {running_pids[0]}")
            else:
                logger.info(f"   请先停止旧进程: pkill -f {self.process_name}")
            logger.info(f"   或使用 --force 参数强制启动")
            return False
        
        if running and force:
            logger.warning(f"⚠️ 强制启动，已有实例: {running_pids}")
        
        self.pid_file.write_text(str(self.pid))
        
        if not self._registered:
            atexit.register(self.release)
            signal.signal(signal.SIGINT, self._signal_handler)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, self._signal_handler)
            self._registered = True
        
        return True
    
    def release(self):
        """释放 PID 锁，删除 PID 文件"""
        try:
            if self.pid_file.exists():
                current_pid = int(self.pid_file.read_text().strip())
                if current_pid == self.pid:
                    self.pid_file.unlink()
        except Exception:
            pass
    
    def _signal_handler(self, signum, frame):
        """信号处理器 - 避免在此处做复杂 I/O 操作"""
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        
        # 先停止后台线程，避免 I/O 死锁
        stop_group_refresh_timer()
        time.sleep(0.3)
        
        write_status("stopped")
        self.release()
        os._exit(0)


class MessageDedup:
    """滑动窗口消息去重，防止 WebSocket 重连/网络抖动导致的重复处理"""

    def __init__(self, max_size=1000):
        self._seen = OrderedDict()
        self._max_size = max_size

    def is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = time.time()
        while len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return False


class TTLCache:
    """带过期时间的内存缓存，减少高频 API 调用"""

    def __init__(self, ttl_seconds=60):
        self._cache = {}
        self._ttl = ttl_seconds

    def get(self, key):
        if key in self._cache:
            value, expire_at = self._cache[key]
            if time.time() < expire_at:
                return value
            del self._cache[key]
        return None

    def set(self, key, value):
        self._cache[key] = (value, time.time() + self._ttl)

    def invalidate(self, key):
        self._cache.pop(key, None)


_msg_dedup = MessageDedup()
_members_cache = TTLCache(ttl_seconds=60)
_history_cache = TTLCache(ttl_seconds=10)
_tasks_cache = TTLCache(ttl_seconds=30)

# 路径设置 - 自动检测，支持多种部署方式
def get_skill_dir() -> Path:
    """自动检测 skill 目录路径"""
    # 优先使用环境变量
    if os.environ.get("IMCLAW_SKILL_DIR"):
        return Path(os.environ["IMCLAW_SKILL_DIR"])
    
    # 其次使用脚本所在目录（通过 scripts/ 子目录判断是否为 skill 目录）
    script_dir = Path(__file__).parent.resolve()
    if (script_dir / "scripts" / "imclaw_skill").is_dir():
        return script_dir
    
    # 最后使用默认路径
    default_dir = Path.home() / ".openclaw" / "workspace" / "skills" / "im-skill"
    return default_dir

SKILL_DIR = get_skill_dir()
ASSETS_DIR = SKILL_DIR / "assets"
QUEUE_DIR = SKILL_DIR / "imclaw_queue"
PROCESSED_DIR = SKILL_DIR / "imclaw_processed"
SESSIONS_DIR = SKILL_DIR / "sessions"
GROUP_SETTINGS_FILE = ASSETS_DIR / "group_settings.yaml"
STATUS_FILE = SKILL_DIR / "bridge_status.json"
sys.path.insert(0, str(SKILL_DIR / "scripts"))


def write_status(status: str, **extra):
    """写入 bridge_status.json，供外部脚本轮询连接状态"""
    data = {"status": status, "updated_at": datetime.now().isoformat(), "pid": os.getpid()}
    data.update(extra)
    try:
        STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


# 从 gateway.env 加载环境变量（bridge 作为独立进程需要自行加载）
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

# 确保队列目录存在
QUEUE_DIR.mkdir(exist_ok=True)

# ─── 群聊响应配置管理 ───

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
        logger.warning(f"⚠️ 加载群聊配置失败: {e}")
        return {"default": {"response_mode": "smart"}, "groups": {}}


def get_response_mode(group_id: str) -> str:
    """获取指定群聊的响应模式（silent/smart），优先读 sessions 下该群 session 文件"""
    session_file = SESSIONS_DIR / f"session_{group_id}.json"
    if session_file.exists():
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
            mode = data.get("response_mode")
            if mode in ("silent", "smart"):
                return mode
        except Exception as e:
            logger.debug(f"读取 session 响应模式失败: {e}")
    settings = load_group_settings()
    group_config = settings.get("groups", {}).get(group_id, {})
    return group_config.get("response_mode", settings["default"].get("response_mode", "smart"))


def get_response_language() -> str:
    """获取 Agent 的回复语言（从环境变量读取）"""
    from imclaw_skill import resolve_env
    return resolve_env("IMCLAW_DEFAULT_LANGUAGE", "zh-CN")


def check_if_mentioned(msg: dict, my_agent_id: str) -> bool:
    """检查消息是否 @ 了当前 Agent"""
    metadata = msg.get("metadata")
    if not metadata:
        return False
    try:
        if isinstance(metadata, str):
            parsed = json.loads(metadata)
        else:
            parsed = metadata
        if not isinstance(parsed, dict):
            return False
        mentions = parsed.get("mentions", [])
        return any(m.get("id") == my_agent_id for m in mentions)
    except (json.JSONDecodeError, TypeError):
        return False

def get_identity_from_token() -> tuple[str, str]:
    """从环境变量中的 token 解析 Agent ID 和 Owner ID
    
    Returns:
        tuple: (agent_id, owner_id) - 如果解析失败返回 (None, None)
    """
    try:
        from imclaw_skill import resolve_env
        token = resolve_env("IMCLAW_TOKEN")
        
        if not token or token == 'your-agent-token-here':
            return None, None
        
        parts = token.split('.')
        if len(parts) != 3:
            return None, None
        
        payload = parts[1]
        payload += '=' * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        
        agent_id = data.get('sub') or data.get('agent_id')
        owner_id = data.get('user_id')
        return agent_id, owner_id
    except Exception as e:
        logger.warning(f"⚠️ 无法从 token 解析身份信息: {e}")
        return None, None

# 从配置中动态获取 Agent ID 和 Owner ID
MY_AGENT_ID, MY_OWNER_ID = get_identity_from_token()

logger.info("=" * 50)
logger.info("🦞 IMClaw 连接 Agent")
logger.info("=" * 50)
logger.info(f"📁 Skill 目录: {SKILL_DIR}")
logger.info(f"📁 队列目录: {QUEUE_DIR}")
if MY_AGENT_ID:
    logger.info(f"🆔 我的 Agent ID: {MY_AGENT_ID}")
else:
    logger.warning("⚠️ 无法获取 Agent ID，将无法过滤自己的消息")

if MY_OWNER_ID:
    logger.info(f"👤 我的 Owner ID: {MY_OWNER_ID}")
else:
    logger.warning("⚠️ 无法获取 Owner ID，将无法识别主人消息")

def get_hooks_token() -> str:
    """获取 OpenClaw hooks token（优先环境变量，其次配置文件）"""
    # 优先使用环境变量
    token = os.environ.get("OPENCLAW_HOOKS_TOKEN", "")
    if token:
        return token
    
    # 从 openclaw.json 读取
    try:
        openclaw_config = Path.home() / ".openclaw" / "openclaw.json"
        if openclaw_config.exists():
            config = json.loads(openclaw_config.read_text())
            token = config.get("hooks", {}).get("token", "")
            if token:
                return token
    except Exception as e:
        logger.warning(f"⚠️ 读取 openclaw.json 失败: {e}")
    
    return ""

# 获取 hooks token
HOOKS_TOKEN = get_hooks_token()
if not HOOKS_TOKEN:
    logger.warning("⚠️ 警告: OPENCLAW_HOOKS_TOKEN 未设置")
    logger.info("   请设置环境变量或在 ~/.openclaw/openclaw.json 中配置 hooks")
else:
    logger.info(f"✅ OPENCLAW_HOOKS_TOKEN: {'*' * 8}{HOOKS_TOKEN[-4:]}")

# 导入模块
try:
    from imclaw_skill import IMClawSkill
    from reply import archive_history_messages
    logger.info("✅ 模块导入成功")
except Exception as e:
    logger.error(f"❌ 模块导入失败: {e}")
    sys.exit(1)

# 读取 skill 版本
def _read_skill_version() -> str:
    meta_file = SKILL_DIR / "_meta.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text()).get("version", "")
        except Exception:
            pass
    return ""

SKILL_VERSION = _read_skill_version()

_notification_file_lock = threading.Lock()

_NOTIFICATION_DEFAULT_EVENTS = [
    "task_claimed",
    "task_completed",
    "task_blocked",
    "exception",
    "progress_report",
    "authorization_request",
    "status_change",
    "mentioned",
]


def _notification_yaml_path() -> Path:
    return ASSETS_DIR / "notification_settings.yaml"


def _load_openclaw_channel_names() -> list[str]:
    """扫描 ~/.openclaw/openclaw.json 的 channels 键名作为可选通知渠道。"""
    try:
        p = Path.home() / ".openclaw" / "openclaw.json"
        if not p.exists():
            return []
        cfg = json.loads(p.read_text(encoding="utf-8"))
        ch = cfg.get("channels")
        if isinstance(ch, dict):
            return sorted(ch.keys())
        return []
    except Exception as e:
        logger.warning(f"⚠️ 读取 openclaw.json channels 失败: {e}")
        return []


def _read_notification_settings() -> tuple[bool, list[str]]:
    path = _notification_yaml_path()
    enabled = False
    events = list(_NOTIFICATION_DEFAULT_EVENTS)
    if not path.exists():
        return enabled, events
    if yaml is None:
        logger.warning("⚠️ PyYAML 未安装，无法读取 notification_settings.yaml")
        return enabled, events
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return enabled, events
        enabled = bool(raw.get("enabled", False))
        ev = raw.get("events")
        if isinstance(ev, list):
            events = [str(x) for x in ev if x is not None]
        else:
            events = list(_NOTIFICATION_DEFAULT_EVENTS)
        return enabled, events
    except Exception as e:
        logger.warning(f"⚠️ 读取通知配置失败: {e}")
        return False, list(_NOTIFICATION_DEFAULT_EVENTS)


def _write_notification_settings(enabled: bool, events: list[str]) -> None:
    if yaml is None:
        raise RuntimeError("PyYAML is required to write notification_settings.yaml")
    path = _notification_yaml_path()
    data: dict = {}
    if path.exists():
        prev = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(prev, dict):
            data = dict(prev)
    data["enabled"] = bool(enabled)
    data["events"] = list(events)
    text = yaml.safe_dump(
        data, allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    path.write_text(text, encoding="utf-8")


def _read_channel_binding() -> Optional[dict]:
    path = _notification_yaml_path()
    if not path.exists() or yaml is None:
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        binding = raw.get("channel_binding")
        if isinstance(binding, dict) and binding.get("channel") and binding.get("target"):
            return binding
        return None
    except Exception:
        return None


def _on_hub_config_query(data: dict):
    req_id = data.get("request_id") or ""
    key = data.get("key", "")
    channels = _load_openclaw_channel_names()
    if key != "notification":
        payload = {"enabled": False, "events": [], "available_channels": channels, "channel_binding": None}
    else:
        with _notification_file_lock:
            en, ev = _read_notification_settings()
            binding = _read_channel_binding()
        payload = {"enabled": en, "events": ev, "available_channels": channels, "channel_binding": binding}
    try:
        skill.client.send_ws_json(
            {"type": "config_response", "request_id": req_id, "payload": payload}
        )
    except Exception as e:
        logger.warning(f"⚠️ 发送 config_response 失败: {e}")


def _on_hub_config_update(data: dict):
    req_id = data.get("request_id") or ""
    key = data.get("key", "")
    ack: dict = {"success": False, "error": ""}
    try:
        if key != "notification":
            ack["error"] = "unsupported key"
        else:
            inner = data.get("payload")
            if isinstance(inner, str):
                inner = json.loads(inner)
            if not isinstance(inner, dict):
                ack["error"] = "invalid payload"
            else:
                en = bool(inner.get("enabled", False))
                ev = inner.get("events")
                if not isinstance(ev, list):
                    ack["error"] = "invalid events"
                else:
                    ev_clean = [str(x) for x in ev]
                    with _notification_file_lock:
                        _write_notification_settings(en, ev_clean)
                    ack["success"] = True
                    ack.pop("error", None)
    except Exception as e:
        logger.warning(f"⚠️ config_update 处理失败: {e}")
        ack["success"] = False
        ack["error"] = "write failed"
    try:
        skill.client.send_ws_json(
            {"type": "config_update_ack", "request_id": req_id, "payload": ack}
        )
    except Exception as e:
        logger.warning(f"⚠️ 发送 config_update_ack 失败: {e}")


# 加载配置
try:
    skill = IMClawSkill.from_env(skill_version=SKILL_VERSION)
    logger.info(f"✅ 配置加载成功")
    logger.info(f"   Hub: {skill.config.hub_url}")
    if SKILL_VERSION:
        logger.info(f"   Skill 版本: v{SKILL_VERSION}")
    skill.client.on("config_query", _on_hub_config_query)
    skill.client.on("config_update", _on_hub_config_update)
except Exception as e:
    logger.error(f"❌ 配置加载失败: {e}")
    sys.exit(1)

# ─── 上下文获取函数（需要 skill 对象）───

MY_PROFILE = {}  # 稀后在连接成功时从 API 获取完整信息
GROUP_NAME_CACHE = {}  # 群名缓存 {group_id: group_name}

def fetch_my_profile():
    """获取当前 Agent 的完整信息（从 IMClaw Hub API）"""
    global MY_PROFILE
    try:
        MY_PROFILE = skill.client.get_profile()
        name = MY_PROFILE.get("display_name", "")
        desc = MY_PROFILE.get("description", "")
        logger.info(f"📛 我是: {name}")
        if desc:
            logger.info(f"   描述: {desc[:50]}")
    except Exception as e:
        logger.warning(f"⚠️ 获取个人信息失败: {e}")
        MY_PROFILE = {}


def get_group_members(group_id: str) -> list[dict]:
    """获取群聊成员列表，403 时主动取消订阅"""
    try:
        members = skill.client._get(f"/api/v1/groups/{group_id}/members")
        return members if isinstance(members, list) else []
    except Exception as e:
        _handle_api_error(e, group_id, "获取群成员")
        return []


def get_recent_history(group_id: str, limit: int = 10) -> list[dict]:
    """获取最近的历史消息，403 时主动取消订阅"""
    try:
        result = skill.client.get_history(group_id, limit=limit)
        messages = result.get("messages", []) if isinstance(result, dict) else result
        return messages if isinstance(messages, list) else []
    except Exception as e:
        _handle_api_error(e, group_id, "获取历史消息")
        return []


def _handle_api_error(e: Exception, group_id: str, action: str):
    """统一处理 API 错误，403 时主动取消订阅（已被移除的兜底防护）"""
    import requests as _requests
    if isinstance(e, _requests.exceptions.HTTPError) and e.response is not None:
        if e.response.status_code == 403:
            logger.warning(f"⚠️ {action}失败: 403 无权访问群 {group_id[:8]}（可能已被移除），取消订阅")
            skill.unsubscribe(group_id)
            return
    logger.warning(f"⚠️ {action}失败: {e}")


def format_members_for_prompt(members: list[dict]) -> str:
    """格式化成员列表供 prompt 使用，显示 名字(类型/id)"""
    names = []
    for m in members:
        name = m.get("display_name") or m.get("agent_name") or m.get("username") or m.get("id", "")[:8]
        mtype = m.get("member_type") or m.get("type", "unknown")
        mid = (m.get("member_id") or m.get("id") or "")
        mid_short = mid[:8] if mid else "unknown"
        names.append(f"{name}({mtype}/{mid_short})")
    return ", ".join(names) if names else "无法获取"


def _extract_mentions(msg: dict) -> list[dict]:
    """从消息 metadata 中提取 @mention 列表，每项含 type/id/display_name"""
    metadata = msg.get("metadata")
    if not metadata:
        return []
    try:
        parsed = json.loads(metadata) if isinstance(metadata, str) else metadata
        if not isinstance(parsed, dict):
            return []
        return parsed.get("mentions", [])
    except (json.JSONDecodeError, TypeError):
        return []


def _format_mentions_for_prompt(mentions: list[dict]) -> str:
    """格式化 @mention 列表供 prompt 使用，用 display_name(type/id前缀) 区分重名"""
    if not mentions:
        return ""
    parts = []
    for m in mentions:
        name = m.get("display_name", "?")
        mtype = m.get("type", "?")
        mid = m.get("id", "")[:8]
        parts.append(f"@{name}({mtype}/{mid})")
    return ", ".join(parts)


def _extract_attachments(msg: dict) -> list[dict]:
    """从消息 metadata 中提取附件列表"""
    metadata = msg.get("metadata")
    if not metadata:
        return []
    try:
        parsed = json.loads(metadata) if isinstance(metadata, str) else metadata
        if not isinstance(parsed, dict):
            return []
        return parsed.get("attachments", [])
    except (json.JSONDecodeError, TypeError):
        return []


def _format_attachments(attachments: list[dict]) -> str:
    """将附件列表格式化为可读字符串"""
    if not attachments:
        return ""
    parts = []
    for att in attachments:
        filename = att.get("filename") or att.get("object_path", "").split("/")[-1] or "未知文件"
        url = att.get("url") or att.get("access_url") or ""
        att_type = att.get("type", "file")
        size = att.get("size")
        size_str = f" ({size // 1024}KB)" if size and size >= 1024 else (f" ({size}B)" if size else "")
        if url:
            parts.append(f"[{att_type}]{size_str} {filename} → {url}")
        else:
            parts.append(f"[{att_type}]{size_str} {filename}")
    return " | ".join(parts)


def format_history_for_prompt(history: list[dict], limit: int = 30) -> str:
    """格式化历史消息供 prompt 使用（渐进式截断：旧消息短，新消息长）

    发送者格式: name(id前缀) — 用 ID 区分重名成员
    """
    if not history:
        return "无历史记录"
    
    recent = history[-limit:]
    lines = []
    threshold = max(len(recent) - 5, 0)
    
    for i, msg in enumerate(recent):
        sender_name = msg.get("sender_name") or ""
        sender_id = msg.get("sender_id") or ""
        if sender_name and sender_id:
            sender = f"{sender_name}({sender_id[:8]})"
        else:
            sender = sender_name or sender_id[:8] or "未知"

        content = msg.get("content", "")
        
        if msg.get("type") == "system":
            content = content[:80]
        elif i < threshold:
            content = content[:150]
        else:
            content = content[:500]

        attachments = _extract_attachments(msg)
        if attachments:
            att_str = _format_attachments(attachments)
            content = f"{content} 📎 {att_str}" if content and content not in ("[file]", "[image]", "[video]", "[audio]") else f"📎 {att_str}"
        
        lines.append(f"  {sender}: {content}")
    return "\n".join(lines)

def get_group_tasks(group_id: str) -> list[dict]:
    """获取群聊的任务列表，403 时主动取消订阅"""
    try:
        tasks = skill.client.list_tasks(group_id)
        return tasks if isinstance(tasks, list) else []
    except Exception as e:
        _handle_api_error(e, group_id, "获取任务列表")
        return []


def format_tasks_for_prompt(tasks: list[dict], members: list[dict]) -> str:
    """格式化任务列表为树形结构供 prompt 使用

    展示层级：主任务 → 子任务，含状态/认领者/指派者/依赖。
    只展示活跃任务（open/claimed/in_progress），done/cancelled 只统计数量。
    """
    if not tasks:
        return "📋 暂无任务"

    member_names = {}
    for m in members:
        mid = m.get("member_id") or m.get("id", "")
        name = m.get("display_name") or m.get("agent_name") or mid[:8]
        if mid:
            member_names[mid] = name

    def _agent_name(agent_id):
        if not agent_id:
            return ""
        return member_names.get(agent_id, agent_id[:8])

    active_statuses = {"open", "claimed", "in_progress"}
    top_tasks = [t for t in tasks if not t.get("parent_task_id")]
    child_map = {}
    for t in tasks:
        parent = t.get("parent_task_id")
        if parent:
            child_map.setdefault(parent, []).append(t)

    status_icons = {
        "open": "⬜", "claimed": "🔒", "in_progress": "🔄",
        "done": "✅", "cancelled": "❌",
    }
    priority_tags = {1: " 🔥", 2: " 🚨"}

    lines = []
    done_count = sum(1 for t in tasks if t.get("status") == "done")
    cancelled_count = sum(1 for t in tasks if t.get("status") == "cancelled")
    active_count = sum(1 for t in tasks if t.get("status") in active_statuses)

    def _format_task(t, indent=""):
        s = t.get("status", "open")
        icon = status_icons.get(s, "❓")
        tid = t.get("id", "")[:8]
        title = t.get("title", "?")
        prio = priority_tags.get(t.get("priority", 0), "")

        parts = [f"{indent}{icon} [{tid}] {title}{prio}"]

        claimer = t.get("claimed_by_id")
        assignee = t.get("assigned_to_id")
        if claimer:
            parts.append(f"认领: {_agent_name(claimer)}")
        elif assignee:
            parts.append(f"指派: {_agent_name(assignee)}")
        elif s == "open":
            parts.append("无人认领")

        return " (".join(parts[:1]) if len(parts) == 1 else f"{parts[0]} ({', '.join(parts[1:])})"

    for t in top_tasks:
        s = t.get("status", "open")
        if s not in active_statuses:
            continue
        lines.append(_format_task(t))
        children = child_map.get(t.get("id"), [])
        for child in children:
            if child.get("status") in active_statuses:
                lines.append(_format_task(child, indent="  └─ "))

    summary_parts = [f"📋 {active_count} 个活跃"]
    if done_count:
        summary_parts.append(f"{done_count} 个已完成")
    if cancelled_count:
        summary_parts.append(f"{cancelled_count} 个已取消")
    header = " | ".join(summary_parts)

    if not lines:
        return f"{header}\n（所有任务已完成或取消）"

    return f"{header}\n" + "\n".join(lines)


def archive_message(msg: dict):
    """立即归档消息到 年/月/日/group_id.jsonl（所有消息都记录）"""
    now = datetime.now()
    day_dir = PROCESSED_DIR / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    
    group_id = msg.get('group_id', 'unknown')
    archive_file = day_dir / f"{group_id}.jsonl"
    
    archive_record = msg.copy()
    archive_record['_archived_at'] = now.isoformat()
    
    with open(archive_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(archive_record, ensure_ascii=False) + '\n')
    logger.info(f"   📦 已归档: {archive_file.name}")


def write_to_queue(msg: dict):
    """写入消息队列（按 group_id 分目录存储）"""
    group_id = msg.get('group_id', 'unknown')
    group_queue_dir = QUEUE_DIR / group_id
    group_queue_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    queue_file = group_queue_dir / f"{timestamp}.json"
    with open(queue_file, 'w', encoding='utf-8') as f:
        json.dump(msg, f, ensure_ascii=False, indent=2)
    logger.info(f"   📝 已写入: {group_id[:8]}/{queue_file.name}")

def get_queue_count(group_id: str = None) -> int:
    """获取队列中待处理消息数量
    
    Args:
        group_id: 指定群聊 ID，为 None 时统计所有群聊
    """
    try:
        if group_id:
            group_dir = QUEUE_DIR / group_id
            return len(list(group_dir.glob("*.json"))) if group_dir.exists() else 0
        else:
            return len(list(QUEUE_DIR.glob("*/*.json")))
    except:
        return 0


_warm_sessions = {}  # {session_key: {"last_wake": float, "count": int}}
_WARM_THRESHOLD = 3600  # 1小时内视为热 Session
_WARM_REFRESH_INTERVAL = 20  # 每 N 条消息发一次冷模板刷新上下文

if sys.platform == "win32":
    _SKILL_DIR_STR = "%USERPROFILE%\\.openclaw\\workspace\\skills\\imclaw"
    _VENV_PY = "venv\\Scripts\\python.exe"
    _CMD_SEP = " && "
else:
    _SKILL_DIR_STR = "~/.openclaw/workspace/skills/imclaw"
    _VENV_PY = "venv/bin/python3"
    _CMD_SEP = " && "
_PY_CMD = f"cd {_SKILL_DIR_STR}{_CMD_SEP}{_VENV_PY}"
_RULES_PATH = f"{_SKILL_DIR_STR}/references/session_rules.md"
_RULES_REF_PATH = f"{_SKILL_DIR_STR}/references/session_rules_ref.md"


def _build_auth_block(trust_level: str, msg: dict) -> str:
    """根据信任等级构建授权提示块"""
    if trust_level == "T0":
        return ""
    
    group_id = msg.get('group_id', '')
    sender_id = msg.get('sender_id', '')
    sender_type = msg.get('sender_type', 'user')
    
    if trust_level == "T2":
        return f"""
⚠️ 信任: T2(其他人) | L0-L2 ✅ | L3/L4 🔒需授权 | L5 ❌拒绝
授权: PY reply.py --auth-request "任务描述" --risk-level L3 --requester-type {sender_type} --requester-id {sender_id} --group {group_id}
🔒 流程: 先回复"需等主人确认" → 发授权请求 → 等批准再执行
"""
    
    if trust_level == "T1":
        return "\n信任: T1(主人的龙虾) | 仅 L5 敏感操作需授权，其余 ✅\n"
    
    return ""


def _extract_common_context(msg: dict) -> dict:
    """提取消息的通用上下文字段，供冷/热模板共用"""
    ctx = msg.get('_context', {})
    group_members = ctx.get('group_members', [])

    mentions = _extract_mentions(msg)
    mentions_str = _format_mentions_for_prompt(mentions)

    other_members = [m for m in group_members
                     if (m.get('member_id') or m.get('id', '')) != MY_AGENT_ID]

    trust_level = get_trust_level(msg)
    trust_labels = {"T0": "T0(主人)", "T1": "T1(主人的龙虾)", "T2": "T2(其他人)"}

    attachments = _extract_attachments(msg)
    attachment_block = ""
    if attachments:
        att_lines = []
        for att in attachments:
            filename = att.get("filename") or att.get("object_path", "").split("/")[-1] or "未知文件"
            url = att.get("url") or att.get("access_url") or ""
            att_type = att.get("type", "file")
            size = att.get("size")
            size_str = f" ({size // 1024}KB)" if size and size >= 1024 else (f" ({size}B)" if size else "")
            if url:
                att_lines.append(f"  - [{att_type}]{size_str} {filename}\n    URL: {url}")
            else:
                att_lines.append(f"  - [{att_type}]{size_str} {filename}")
        attachment_block = "\n附件:\n" + "\n".join(att_lines)

    notify_enabled = False
    notify_events: list[str] = []
    try:
        with _notification_file_lock:
            notify_enabled, notify_events = _read_notification_settings()
    except Exception:
        pass

    return {
        "content": msg.get('content', '')[:2000],
        "sender": msg.get('sender_name', msg.get('sender_id', '未知')[:8]),
        "group_name": msg.get('group_name', '群聊'),
        "group_id": msg.get('group_id', ''),
        "from_owner": msg.get('_from_owner', False),
        "response_mode": ctx.get('response_mode', 'smart'),
        "is_mentioned": ctx.get('is_mentioned', False),
        "group_members": group_members,
        "recent_history": ctx.get('recent_history', []),
        "group_tasks": ctx.get('group_tasks', []),
        "trust_level": trust_level,
        "trust_display": trust_labels.get(trust_level, trust_level),
        "mentions_str": mentions_str,
        "mention_detail": f" → {mentions_str}" if mentions_str else "",
        "is_one_on_one": len(other_members) == 1,
        "attachment_block": attachment_block,
        "auth_block": _build_auth_block(trust_level, msg),
        "language": get_response_language(),
        "notify_enabled": notify_enabled,
        "notify_events": notify_events,
    }


def _build_dynamic_section(msg: dict, is_cold: bool = True) -> str:
    """构建 wake_text 的动态部分

    Args:
        is_cold: True=冷启动完整模板, False=热Session精简模板
    """
    c = _extract_common_context(msg)
    if is_cold:
        return _build_cold_section(c)
    return _build_hot_section(c)


def _build_cold_section(c: dict) -> str:
    """冷 Session 完整模板：包含身份、成员、完整操作命令"""
    my_name = MY_PROFILE.get('display_name', '未知')
    my_desc = MY_PROFILE.get('description', '')
    members_str = format_members_for_prompt(c["group_members"])
    history_str = format_history_for_prompt(c["recent_history"])
    tasks_str = format_tasks_for_prompt(c["group_tasks"], c["group_members"])
    date_ymd = datetime.now().strftime("%Y/%m/%d")
    group_id = c["group_id"]
    chat_type_hint = " | **一对一**" if c["is_one_on_one"] else ""

    notify_block = ""
    if c["notify_enabled"] and c["notify_events"]:
        ev_str = "/".join(c["notify_events"][:6])
        notify_block = f"""
== 通知主人 ==
已开启 | 事件: {ev_str}
通知: PY reply.py --notify-owner "内容" --event <事件>
⚠️ 认领/完成/阻塞任务等关键节点必须通知（详见规则文件）
"""

    return f"""===== 群聊任务开始 [group:{group_id}] =====
⚠️ 来自群「{c["group_name"]}」，仅处理本群消息，处理完等待下一条。

PY = {_PY_CMD}

== 身份 ==
你是 **{my_name}**{"（" + my_desc + "）" if my_desc else ""}
群成员: {members_str}

== 状态 ==
{c["response_mode"]} | @:{"是" if c["is_mentioned"] else "否"}{c["mention_detail"]} | 主人:{"是👑" if c["from_owner"] else "否"} | {c["trust_display"]} | lang:{c["language"]}{chat_type_hint}
{c["auth_block"]}
== 任务看板 ==
{tasks_str}

== 最近对话（{len(c["recent_history"])} 条） ==
{history_str}

== 消息 ==
{c["sender"]}{"👑" if c["from_owner"] else ""}: {c["content"]}{c["attachment_block"]}

== 操作 ==
回复: PY reply.py "内容" --group {group_id}
静默: PY -c "from reply import clear_queue; clear_queue('{group_id}')"
任务: PY task.py --list --group {group_id}
本地记录: imclaw_processed/{date_ymd}/{group_id}.jsonl
📖 操作参考: {_RULES_REF_PATH}{notify_block}===== 群聊任务结束 [group:{group_id}] ====="""


def _build_hot_section(c: dict) -> str:
    """热 Session 精简模板：省略已知的身份/成员/完整命令"""
    history_str = format_history_for_prompt(c["recent_history"])
    tasks_str = format_tasks_for_prompt(c["group_tasks"], c["group_members"])
    group_id = c["group_id"]
    chat_type_hint = " | **一对一**" if c["is_one_on_one"] else ""

    # 任务看板：只在有活跃任务时显示
    active_count = sum(1 for t in c["group_tasks"]
                       if t.get("status") in ("open", "claimed", "in_progress"))
    tasks_block = f"\n任务: {tasks_str}\n" if active_count > 0 else ""

    notify_hint = ""
    if c["notify_enabled"]:
        notify_hint = "\n通知主人: PY reply.py --notify-owner \"内容\" --event <事件>"

    return f"""===== [group:{group_id}] {c["group_name"]} =====
{c["response_mode"]} | @:{"是" if c["is_mentioned"] else "否"}{c["mention_detail"]} | 主人:{"是👑" if c["from_owner"] else "否"} | {c["trust_display"]} | lang:{c["language"]}{chat_type_hint}
{c["auth_block"]}
对话（最近 {len(c["recent_history"])} 条）:
{history_str}
{tasks_block}
{c["sender"]}{"👑" if c["from_owner"] else ""}: {c["content"]}{c["attachment_block"]}

回复: PY reply.py "内容" --group {group_id}{notify_hint}
===== [end:{group_id}] ====="""


def _check_cold_status(group_id: str) -> bool:
    """判断是否为冷 Session（含周期性刷新逻辑）

    冷 Session 条件（满足任一即为冷）：
    1. 超过 _WARM_THRESHOLD 未活动
    2. 累计 wake 次数达到 _WARM_REFRESH_INTERVAL 的倍数（周期性刷新上下文）
    """
    wake_key = f"imclaw:{group_id}"
    now = time.time()
    session = _warm_sessions.get(wake_key)

    if session is None:
        _warm_sessions[wake_key] = {"last_wake": now, "count": 1}
        return True

    elapsed = now - session["last_wake"]
    session["last_wake"] = now
    session["count"] += 1

    if elapsed > _WARM_THRESHOLD:
        session["count"] = 1
        return True

    if session["count"] % _WARM_REFRESH_INTERVAL == 0:
        return True

    return False


def wake_session_for_group(msg: dict):
    """通过 hooks/wake 唤醒主 Session 处理群聊消息"""
    try:
        import requests
        group_name = msg.get('group_name', '群聊')
        group_id = msg.get('group_id', '')
        from_owner = msg.get('_from_owner', False)
        is_mentioned = msg.get('_context', {}).get('is_mentioned', False)

        if not HOOKS_TOKEN:
            logger.error("   ❌ 唤醒失败: OPENCLAW_HOOKS_TOKEN 未配置")
            return

        gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
        is_cold = _check_cold_status(group_id)

        owner_hint = " 👑 [来自主人]" if from_owner else ""
        mentioned_hint = " 📢 [被@提及]" if is_mentioned else ""
        dynamic = _build_dynamic_section(msg, is_cold=is_cold)

        if is_cold:
            wake_text = f"""[IMClaw] 收到新消息（群聊激活）{owner_hint}{mentioned_hint}

规则文件: {_RULES_PATH}
请先阅读规则文件，然后处理以下消息。

{dynamic}"""
        else:
            wake_text = f"""[IMClaw] 新消息{owner_hint}{mentioned_hint}

{dynamic}"""

        resp = requests.post(
            f"{gateway_url}/hooks/wake",
            json={"text": wake_text},
            headers={
                "Authorization": f"Bearer {HOOKS_TOKEN}",
                "Content-Type": "application/json"
            },
            timeout=5
        )
        cold_tag = " [冷启动]" if is_cold else ""
        if resp.status_code < 300:
            logger.info(f"   🔔 主 Session 唤醒成功 [群:{group_name}]{cold_tag}: HTTP {resp.status_code}")
        elif resp.status_code == 404:
            logger.error(f"   ❌ 唤醒失败 [群:{group_name}]: HTTP 404 — Gateway hooks 未启用，请检查 openclaw.json 中 hooks.enabled")
        elif resp.status_code == 401:
            logger.error(f"   ❌ 唤醒失败 [群:{group_name}]: HTTP 401 — Token 不匹配，请检查 OPENCLAW_HOOKS_TOKEN 与 openclaw.json 中 hooks.token 是否一致")
        else:
            logger.error(f"   ❌ 唤醒失败 [群:{group_name}]{cold_tag}: HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"   ❌ 主 Session 唤醒失败: {e}")

@skill.on_connect
def on_connect():
    logger.info("\n" + "=" * 50)
    logger.info("✅ 已连接到 IMClaw Hub")
    logger.info(f"📋 订阅的群聊: {skill.subscribed_groups}")
    logger.info("=" * 50 + "\n")
    write_status("connected", subscribed_groups=len(skill.subscribed_groups))

    def _post_connect_init():
        fetch_my_profile()
        refresh_group_name_cache()
        logger.info(f"📛 已缓存 {len(GROUP_NAME_CACHE)} 个群名")
        start_group_refresh_timer()

    threading.Thread(target=_post_connect_init, daemon=True).start()


def refresh_group_name_cache(groups: list[dict] = None):
    """刷新群名缓存
    
    Args:
        groups: 群聊列表，如果为 None 则从 API 获取
    """
    global GROUP_NAME_CACHE
    try:
        if groups is None:
            groups = skill.list_groups()
        
        for g in groups:
            gid = g.get('id')
            name = g.get('name')
            if gid and name:
                GROUP_NAME_CACHE[gid] = name
    except Exception as e:
        logger.warning(f"⚠️ 刷新群名缓存失败: {e}")


def refresh_groups():
    """定期检查并订阅新群聊，清理已移除的群聊"""
    if not skill.is_connected:
        return
    
    try:
        all_groups = skill.list_groups()
        current_group_ids = {g.get('id') for g in all_groups if g.get('id')}
        
        # 同时更新群名缓存
        refresh_group_name_cache(all_groups)
        subscribed = skill.subscribed_groups
        
        # 清理已不再属于的群聊（被移除的）
        removed_groups = []
        for gid in subscribed:
            if gid not in current_group_ids:
                skill.unsubscribe(gid)
                removed_groups.append(gid[:8])
        
        if removed_groups:
            logger.info(f"🚫 已清理不再属于的群聊: {removed_groups}")
        
        # 订阅新群聊
        new_groups = []
        for g in all_groups:
            gid = g.get('id')
            if gid and gid not in subscribed:
                skill.subscribe(gid)
                new_groups.append(g.get('name', gid[:8]))
        
        if new_groups:
            logger.info(f"🆕 自动订阅新群聊: {new_groups}")
    except Exception as e:
        logger.warning(f"⚠️ 检查新群聊失败: {e}")


_refresh_stop_event = None
_refresh_thread = None

def start_group_refresh_timer():
    """启动定期检查新群聊的定时器（每 5 秒）"""
    global _refresh_stop_event, _refresh_thread
    if _refresh_stop_event and not _refresh_stop_event.is_set():
        logger.info("🔄 群聊自动发现已在运行")
        return
    
    _refresh_stop_event = threading.Event()
    
    def timer_loop(stop_event):
        while not stop_event.is_set():
            if stop_event.wait(timeout=5):
                break
            if skill.is_connected:
                try:
                    refresh_groups()
                except Exception:
                    pass
    
    _refresh_thread = threading.Thread(target=timer_loop, args=(_refresh_stop_event,), daemon=True)
    _refresh_thread.start()
    logger.info("🔄 已启动新群聊自动发现（每 5 秒检查）")


def stop_group_refresh_timer():
    """停止群聊检查定时器"""
    global _refresh_stop_event, _refresh_thread
    if _refresh_stop_event:
        _refresh_stop_event.set()
        if _refresh_thread and _refresh_thread.is_alive():
            _refresh_thread.join(timeout=1)
        _refresh_stop_event = None
        _refresh_thread = None

@skill.on_disconnect
def on_disconnect():
    logger.warning("⚠️ WebSocket 连接已断开")
    write_status("disconnected")
    stop_group_refresh_timer()


@skill.on_system_message
def on_system_message(msg, parsed):
    """处理系统消息 - 成员变动检测（含 target 单数和 targets 复数）"""
    if not parsed:
        return

    action = parsed.get('action')
    group_id = msg.get('group_id', '')

    # 成员变动时清除缓存，确保下次获取最新数据
    if action in ('invite', 'join', 'remove', 'leave') and group_id:
        _members_cache.invalidate(group_id)

    if not MY_AGENT_ID:
        return
    
    if action not in ('remove', 'leave'):
        return

    # 检查 target（单数）和 targets（复数，级联移除时使用）
    target = parsed.get('target', {})
    targets = parsed.get('targets', [])
    is_self = (
        target.get('id') == MY_AGENT_ID or
        any(t.get('id') == MY_AGENT_ID for t in targets)
    )
    
    if not is_self:
        return

    group_name = msg.get('group_name', group_id[:8])
    if action == 'remove':
        logger.info(f"🚫 被移除出群聊: {group_name}")
    else:
        logger.info(f"👋 已离开群聊: {group_name}")
    skill.unsubscribe(group_id)
    logger.info(f"   已取消订阅")


def _wake_for_task_assignment(payload: dict):
    """唤醒 Session 通知 Agent 被指派了任务（延迟发送，等当前 turn 结束）"""
    task_id = payload.get('task_id', '')
    title = payload.get('title', '?')
    group_id = payload.get('group_id', '')
    actor_id = payload.get('actor_id', '')
    group_name = GROUP_NAME_CACHE.get(group_id, group_id[:8]) if group_id else '未知'

    wake_text = f"""[IMClaw] 任务指派通知

你被指派了一个新任务，请尽快处理。

== 任务详情 ==
任务: [{task_id[:8]}] {title}
群聊: {group_name} ({group_id})
指派者: {actor_id[:8]}

== 下一步 ==
1. 查看任务详情: cd {_SKILL_DIR_STR}{_CMD_SEP}{_VENV_PY} task.py --detail {task_id}
2. 认领任务: cd {_SKILL_DIR_STR}{_CMD_SEP}{_VENV_PY} task.py --claim {task_id}
3. 执行工作并完成: cd {_SKILL_DIR_STR}{_CMD_SEP}{_VENV_PY} task.py --complete {task_id}

📖 完整任务规则见: {_RULES_PATH}"""

    def _delayed_wake():
        import requests
        gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
        headers = {"Authorization": f"Bearer {HOOKS_TOKEN}", "Content-Type": "application/json"}

        for attempt, delay in enumerate((3, 10, 30), 1):
            time.sleep(delay)
            try:
                resp = requests.post(
                    f"{gateway_url}/hooks/wake", json={"text": wake_text},
                    headers=headers, timeout=5
                )
                logger.info(f"   🔔 任务指派 wake #{attempt} (延迟{delay}s): HTTP {resp.status_code}")
                if resp.status_code < 300:
                    return
            except Exception as e:
                logger.warning(f"   ⚠️ 任务指派 wake #{attempt} 失败: {e}")

    threading.Thread(target=_delayed_wake, daemon=True).start()
    logger.info(f"   🔔 任务指派 wake 已排程（3s/10s/30s 延迟重试）")


@skill.on_task_updated
def on_task_updated(payload):
    """任务事件处理：记录日志 + 清除任务缓存 + 指派给自己时唤醒 Session"""
    event = payload.get('event', '?')
    title = payload.get('title', '?')
    group_id = payload.get('group_id', '')
    actor_id = payload.get('actor_id', '')
    logger.info(f"📋 任务事件: {event} - {title} (群:{group_id[:8]})")

    if group_id:
        _tasks_cache.invalidate(group_id)

    if event == 'task_assigned' and actor_id != MY_AGENT_ID and MY_AGENT_ID:
        assigned_to = payload.get('assigned_to_id', '')
        if assigned_to == MY_AGENT_ID:
            task_id = payload.get('task_id', '')
            logger.info(f"   📌 任务被指派给我: [{task_id[:8]}] {title}")
            _wake_for_task_assignment(payload)

@skill.on_authorization_updated
def on_authorization_updated(payload):
    """授权状态变更 → 写入队列 + 延迟唤醒主 Session
    
    为什么不能立即 wake？
    ─────────────────────
    典型时序：agent 调用 reply.py --auth-request → IMClaw Hub 生成授权卡片 → 主人秒批
    → bridge 收到 authorization_updated 事件 → 发 wake
    
    问题：此时 agent 的 session 很可能还没结束当前 turn（还在说"等待主人审批"），
    Gateway 无法往一个正在执行中的 session 注入新消息，wake 会被静默丢弃。
    
    解决：
    1. 写入消息队列（与普通消息一致），确保授权结果不丢失
    2. 后台线程延迟发 wake（5s/15s/45s），等 session turn 结束后再唤醒
    3. 每次 wake 前检查队列：如果授权结果已被 heartbeat 或其他消息消费，则跳过
    """
    status = payload.get('status', '?')
    task = payload.get('task_description', '?')
    group_id = payload.get('group_id', '')
    requester_name = payload.get('requester_name', '?')
    requester_type = payload.get('requester_type', '?')
    risk_level = payload.get('risk_level', '?')
    logger.info(f"🔐 授权事件: {status} - {task} (群:{group_id[:8]})")

    if status not in ('approved', 'rejected'):
        return

    status_label = "已授权 ✅" if status == "approved" else "已拒绝 ❌"
    if status == "approved":
        action_hint = f"已授权，请立即执行任务「{task}」，并将结果回复到群聊。"
    else:
        action_hint = f"已拒绝，请回复群聊告知 {requester_name} 该请求已被主人拒绝。"

    auth_content = (
        f"[授权结果] {status_label}\n"
        f"任务: {task}\n"
        f"请求者: {requester_name} ({requester_type})\n"
        f"风险等级: {risk_level}\n"
        f"下一步: {action_hint}"
    )

    # ── 步骤 1：写入队列（持久化，heartbeat 兜底） ──
    # 与普通消息的 write_to_queue 一致，保证即使所有 wake 都失败，
    # 下一次 heartbeat 扫描队列时也能发现并处理。
    if group_id:
        queue_msg = {
            "type": "authorization_result",
            "content": auth_content,
            "content_type": "authorization_result",
            "group_id": group_id,
            "sender_id": "system",
            "sender_type": "system",
            "sender_name": "授权系统",
            "created_at": datetime.now().isoformat(),
            "metadata": {"authorization": payload},
            "_from_owner": True,
            "_context": {
                "my_agent_id": MY_AGENT_ID,
                "my_profile": MY_PROFILE,
                "response_mode": get_response_mode(group_id),
                "is_mentioned": True,
                "group_members": _members_cache.get(group_id) or [],
                "recent_history": [],
            },
        }
        write_to_queue(queue_msg)
        logger.info(f"   📝 授权结果已写入队列: {group_id[:8]}")

    # ── 步骤 2：后台延迟 wake（等 session turn 结束再唤醒） ──
    wake_text = f"""[IMClaw] 授权结果通知

主人对以下授权请求做出了决定：{status_label}

== 授权详情 ==
任务描述: {task}
请求者: {requester_name} ({requester_type})
风险等级: {risk_level}
群聊 ID: {group_id}

== 下一步 ==
{action_hint}

== 操作 ==
回复: cd {_SKILL_DIR_STR}{_CMD_SEP}{_VENV_PY} reply.py "回复内容" --group {group_id}

规则文件: {_RULES_PATH}
所有命令在 cd {_SKILL_DIR_STR} 下执行。"""

    def _delayed_wake():
        """后台线程：延迟后尝试 wake，递增间隔重试。
        
        为什么是 5/15/45 秒？
        - 5s：大多数 turn 在几秒内结束，5s 后大概率 session 已空闲
        - 15s：如果 5s 时 session 仍忙（复杂任务），15s 再试
        - 45s：最后兜底，再不行就等 heartbeat（约 10 分钟）自动消费队列
        """
        import requests
        gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
        headers = {"Authorization": f"Bearer {HOOKS_TOKEN}", "Content-Type": "application/json"}

        for attempt, delay in enumerate((5, 15, 45), 1):
            time.sleep(delay)
            # 检查队列：如果 authorization_result 文件已不在，说明已被消费，无需再 wake
            if group_id:
                gq = QUEUE_DIR / group_id
                if gq.exists() and not any(
                    "authorization_result" in f.read_text(encoding="utf-8")[:200]
                    for f in gq.glob("*.json")
                ):
                    logger.info(f"   ✅ 授权队列已被消费，跳过 wake #{attempt}")
                    return
            try:
                resp = requests.post(
                    f"{gateway_url}/hooks/wake", json={"text": wake_text},
                    headers=headers, timeout=5
                )
                logger.info(f"   🔔 授权 wake #{attempt} (延迟{delay}s): HTTP {resp.status_code}")
                if resp.status_code < 300:
                    return
            except Exception as e:
                logger.warning(f"   ⚠️ 授权 wake #{attempt} 失败: {e}")

    threading.Thread(target=_delayed_wake, daemon=True).start()
    logger.info(f"   🔔 授权 wake 已排程（5s/15s/45s 延迟重试）")

@skill.on_error
def on_error(e):
    logger.error(f"❌ 错误: {e}")

def is_from_owner(msg: dict) -> bool:
    """判断消息是否来自 owner"""
    if not MY_OWNER_ID:
        return False
    sender_id = msg.get('sender_id', '')
    sender_type = msg.get('sender_type', '')
    return sender_type == 'user' and sender_id == MY_OWNER_ID


def get_trust_level(msg: dict) -> str:
    """根据消息发送者计算信任等级
    
    Returns:
        "T0" - 主人本人
        "T1" - 主人的其他龙虾（同 owner 的 agent）
        "T2" - 其他人/不确定来源
    """
    if msg.get('_from_owner'):
        return "T0"
    
    sender_type = msg.get('sender_type', '')
    sender_id = msg.get('sender_id', '')
    
    if sender_type == 'agent' and MY_OWNER_ID:
        group_members = msg.get('_context', {}).get('group_members', [])
        for member in group_members:
            member_id = member.get('member_id') or member.get('id', '')
            if member_id == sender_id and member.get('owner_id') == MY_OWNER_ID:
                return "T1"
    
    return "T2"


def _is_self_removal(msg: dict) -> bool:
    """检查系统消息是否表示当前 Agent 被移除或离开（含 target 单数和 targets 复数）"""
    if not MY_AGENT_ID or msg.get('type') != 'system':
        return False
    from imclaw_skill.client import IMClawClient
    parsed = IMClawClient.parse_system_message(msg)
    if not parsed:
        return False
    action = parsed.get('action')
    if action not in ('remove', 'leave'):
        return False
    target = parsed.get('target', {})
    if target.get('id') == MY_AGENT_ID:
        return True
    targets = parsed.get('targets', [])
    return any(t.get('id') == MY_AGENT_ID for t in targets)

def _is_skill_update_request(text: str) -> bool:
    """
    判断是否为“更新 IMClaw skill 到最新版本”的自然语言请求。
    只在 from_owner=true 时才会触发本地 update.py。
    """
    if not text:
        return False
    t_lower = text.strip().lower()
    if not t_lower:
        return False

    # 期望的中文/英文组合：IMClaw + (skill/技能) + 更新/Latest + 最新版本
    has_imclaw = "imclaw" in t_lower
    has_skill = ("skill" in t_lower) or ("技能" in text)
    has_update = ("更新" in text) or ("update" in t_lower)
    has_latest = ("最新版本" in text) or ("最新" in text) or ("latest" in t_lower)

    return has_imclaw and has_skill and has_update and has_latest


@skill.on_message
def handle(msg):
    """处理收到的消息"""
    msg_id = msg.get('id', '')
    sender_id = msg.get('sender_id', '')
    sender_type = msg.get('sender_type', '')
    group_id = msg.get('group_id', '')
    content = msg.get('content', '')[:200]

    # 消息去重（防止重连/网络抖动导致重复处理）
    if _msg_dedup.is_duplicate(msg_id):
        logger.debug(f"   ⏭️ 重复消息，跳过: {msg_id[:8]}")
        return

    # 从缓存补充群名（API 消息不带群名）
    if group_id and 'group_name' not in msg:
        cached_name = GROUP_NAME_CACHE.get(group_id)
        if cached_name:
            msg['group_name'] = cached_name
    
    group_name = msg.get('group_name', group_id[:8] if group_id else '未知')

    # 提前检测自身被移除/离开 — 只归档，不调 API、不入队列、不唤醒 Session
    if _is_self_removal(msg):
        logger.info(f"\n🚫 收到移除通知: {group_name}")
        skill.unsubscribe(group_id)
        archive_message(msg)
        logger.info(f"   已取消订阅并归档，跳过后续处理")
        return

    # 系统消息（成员变动、改名等）只归档，不唤醒 AI Session
    if msg.get('type') == 'system':
        logger.info(f"\n📢 系统消息: {content}")
        logger.info(f"   群聊: {group_name}")
        archive_message(msg)
        return

    # 标记是否来自 owner
    from_owner = is_from_owner(msg)
    owner_tag = " 👑" if from_owner else ""
    
    logger.info(f"\n📨 收到消息: {content}")
    logger.info(f"   群聊: {group_name}")
    logger.info(f"   发送者: {sender_type}:{sender_id[:8] if sender_id else '未知'}{owner_tag}")
    
    # 跳过自己发送的消息
    if MY_AGENT_ID and sender_id == MY_AGENT_ID:
        logger.info("   ⏭️ 跳过自己的消息")
        return

    # Owner 主动请求“更新 skill”（自然语言触发，避免展示 shell 命令）
    if from_owner and _is_skill_update_request(content):
        logger.info("🛠️ 检测到 Skill 更新请求：启动 update.py ...")
        try:
            # 先归档本条消息（保持行为可追溯）
            archive_message(msg)
        except Exception:
            pass

        # 先回复一句“已开始更新”（尽量在 bridge 被停止前把反馈发出去）
        try:
            import subprocess as _subprocess
            ack_text = "收到，我正在检查并更新 IMClaw Skill（本地自更新）... 请稍等。"
            if group_id:
                _subprocess.run(
                    [sys.executable, str(SKILL_DIR / "reply.py"), ack_text, "--group", group_id],
                    cwd=str(SKILL_DIR),
                    check=False,
                    stdout=_subprocess.DEVNULL,
                    stderr=_subprocess.STDOUT,
                )
            else:
                if sender_type == "user" and sender_id:
                    _subprocess.run(
                        [sys.executable, str(SKILL_DIR / "reply.py"), ack_text, "--user", sender_id],
                        cwd=str(SKILL_DIR),
                        check=False,
                        stdout=_subprocess.DEVNULL,
                        stderr=_subprocess.STDOUT,
                    )
                elif sender_type == "agent" and sender_id:
                    _subprocess.run(
                        [sys.executable, str(SKILL_DIR / "reply.py"), ack_text, "--agent", sender_id],
                        cwd=str(SKILL_DIR),
                        check=False,
                        stdout=_subprocess.DEVNULL,
                        stderr=_subprocess.STDOUT,
                    )
        except Exception as e:
            logger.warning(f"⚠️ 更新请求回复失败: {e}")

        # 然后启动更新脚本（detach，避免 bridge 终止导致中断）
        try:
            import subprocess as _subprocess
            log_fd = open(SKILL_DIR / "update.log", "a", encoding="utf-8")
            _subprocess.Popen(
                [sys.executable, str(SKILL_DIR / "update.py")],
                cwd=str(SKILL_DIR),
                stdout=log_fd,
                stderr=_subprocess.STDOUT,
                start_new_session=True,
            )
            logger.info("✅ update.py 已启动（后台）")
        except Exception as e:
            logger.error(f"❌ 启动 update.py 失败: {e}")
        return
    
    # 获取响应模式和 @提及 状态（轻量操作，不需要 API 调用）
    response_mode = get_response_mode(group_id)
    is_mentioned = check_if_mentioned(msg, MY_AGENT_ID) if MY_AGENT_ID else False
    
    logger.info(f"   📋 响应模式: {response_mode}, 被@: {is_mentioned}")
    
    # silent 模式短路：未被 @ 且不是主人消息时，仅归档不唤醒
    # 但一对一对话例外 — 你是唯一能回复的人，消息必须到达 Session
    if response_mode == 'silent' and not is_mentioned and not from_owner:
        members = _members_cache.get(group_id) if group_id else None
        if members is None and group_id:
            members = get_group_members(group_id)
            if members:
                _members_cache.set(group_id, members)
        other_members = [m for m in (members or [])
                         if (m.get('member_id') or m.get('id', '')) != MY_AGENT_ID]
        if len(other_members) != 1:
            logger.info("   🔇 静默模式，未被提及，仅归档")
            archive_message(msg)
            return
        logger.info("   💬 静默模式但一对一对话，穿透到 Session")

    # 获取群成员、历史消息和任务列表（带缓存，减少 API 调用）
    group_members = []
    recent_history = []
    group_tasks = []
    if group_id:
        group_members = _members_cache.get(group_id)
        if group_members is None:
            group_members = get_group_members(group_id)
            if group_members:
                _members_cache.set(group_id, group_members)
        
        # 冷/热 Session 差异化拉取：冷 Session 拉更多历史建立上下文
        session_key = f"imclaw:{group_id}"
        session = _warm_sessions.get(session_key)
        is_cold_preview = (
            session is None
            or (time.time() - session["last_wake"]) > _WARM_THRESHOLD
            or session["count"] % _WARM_REFRESH_INTERVAL == _WARM_REFRESH_INTERVAL - 1
        )
        history_limit = 30 if is_cold_preview else 10
        
        recent_history = _history_cache.get(group_id)
        if recent_history is None:
            recent_history = get_recent_history(group_id, limit=history_limit)
            if recent_history:
                _history_cache.set(group_id, recent_history)
        
        group_tasks = _tasks_cache.get(group_id)
        if group_tasks is None:
            group_tasks = get_group_tasks(group_id)
            _tasks_cache.set(group_id, group_tasks if group_tasks else [])
    
    # 将 API 拉到的历史消息归档到本地（自动去重）
    if recent_history and group_id:
        try:
            archived = archive_history_messages(recent_history, group_id)
            if archived > 0:
                logger.info(f"   📦 已归档 {archived} 条历史消息")
        except Exception as e:
            logger.warning(f"   ⚠️ 归档历史消息失败: {e}")
    
    # 在消息中附加上下文信息
    msg['_from_owner'] = from_owner
    msg['_context'] = {
        "my_agent_id": MY_AGENT_ID,
        "my_profile": MY_PROFILE,
        "response_mode": response_mode,
        "is_mentioned": is_mentioned,
        "group_members": group_members,
        "recent_history": recent_history,
        "group_tasks": group_tasks,
    }
    
    # 处理消息
    logger.info("   📝 开始处理...")
    archive_message(msg)
    write_to_queue(msg)
    wake_session_for_group(msg)


def _detach_from_parent_group():
    """非交互式启动时脱离父进程组，防止被 exec 工具的进程清理杀死。

    当 stdin 不是 tty（通过 nohup/cron/exec 启动）时调用 os.setsid()
    创建新会话，使 bridge 不在 exec 的进程组中。
    交互式启动（stdin 是 tty）时跳过，保留 Ctrl+C 能力。
    """
    if sys.platform == "win32":
        return
    try:
        if not os.isatty(sys.stdin.fileno()):
            os.setsid()
    except (OSError, ValueError, AttributeError):
        pass


_detach_from_parent_group()

# PID 管理
pid_manager = PIDManager(SKILL_DIR / "bridge.pid")

# 检查是否已有实例运行
force_start = "--force" in sys.argv
if not pid_manager.acquire(force=force_start):
    sys.exit(1)

logger.info(f"📝 PID 文件已写入: {pid_manager.pid_file} (PID: {pid_manager.pid})")

logger.info("\n🚀 启动 WebSocket 连接...")
logger.info("按 Ctrl+C 退出\n")
write_status("starting")

try:
    skill.run()
finally:
    write_status("stopped")
    stop_group_refresh_timer()
    pid_manager.release()
