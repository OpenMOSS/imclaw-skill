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
    default_dir = Path.home() / ".openclaw" / "workspace" / "skills" / "imclaw"
    return default_dir

SKILL_DIR = get_skill_dir()
ASSETS_DIR = SKILL_DIR / "assets"
QUEUE_DIR = SKILL_DIR / "imclaw_queue"
PROCESSED_DIR = SKILL_DIR / "imclaw_processed"
SESSIONS_DIR = SKILL_DIR / "sessions"
GROUP_SETTINGS_FILE = ASSETS_DIR / "group_settings.yaml"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

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

# 加载配置
try:
    skill = IMClawSkill.from_env()
    logger.info(f"✅ 配置加载成功")
    logger.info(f"   Hub: {skill.config.hub_url}")
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


def format_history_for_prompt(history: list[dict], limit: int = 30) -> str:
    """格式化历史消息供 prompt 使用（渐进式截断：旧消息短，新消息长）"""
    if not history:
        return "无历史记录"
    
    recent = history[-limit:]
    lines = []
    threshold = max(len(recent) - 5, 0)
    
    for i, msg in enumerate(recent):
        sender = msg.get("sender_name") or msg.get("sender_id", "")[:6]
        content = msg.get("content", "")
        
        if msg.get("type") == "system":
            content = content[:80]
        elif i < threshold:
            content = content[:150]
        else:
            content = content[:500]
        
        lines.append(f"  {sender}: {content}")
    return "\n".join(lines)

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


_warm_sessions = {}  # {session_key: last_wake_time}
_WARM_THRESHOLD = 3600  # 1小时内视为热 Session

if sys.platform == "win32":
    _SKILL_DIR_STR = "%USERPROFILE%\\.openclaw\\workspace\\skills\\imclaw"
    _VENV_PY = "venv\\Scripts\\python.exe"
    _CMD_SEP = " && "
else:
    _SKILL_DIR_STR = "~/.openclaw/workspace/skills/imclaw"
    _VENV_PY = "venv/bin/python3"
    _CMD_SEP = " && "
_RULES_PATH = f"{_SKILL_DIR_STR}/references/session_rules.md"


def _build_dynamic_section(msg: dict) -> str:
    """构建 wake_text 的动态部分（每条消息都不同的内容）"""
    content = msg.get('content', '')[:2000]
    sender = msg.get('sender_name', msg.get('sender_id', '未知')[:8])
    group_name = msg.get('group_name', '群聊')
    group_id = msg.get('group_id', '')
    from_owner = msg.get('_from_owner', False)

    ctx = msg.get('_context', {})
    response_mode = ctx.get('response_mode', 'smart')
    is_mentioned = ctx.get('is_mentioned', False)
    group_members = ctx.get('group_members', [])
    recent_history = ctx.get('recent_history', [])

    my_name = MY_PROFILE.get('display_name', '未知')
    my_desc = MY_PROFILE.get('description', '')

    members_str = format_members_for_prompt(group_members)
    history_str = format_history_for_prompt(recent_history)
    history_count = len(recent_history)
    date_ymd = datetime.now().strftime("%Y/%m/%d")
    return f"""===== 群聊任务开始 [group:{group_id}] =====
⚠️ 以下内容来自群「{group_name}」，请仅处理本群消息，处理完毕后等待下一条群聊任务。

== 身份 ==
你是 **{my_name}**{"（" + my_desc + "）" if my_desc else ""}
群成员: {members_str}

== 状态 ==
响应模式: {response_mode} | 被@: {"是" if is_mentioned else "否"} | 来自主人: {"是 👑" if from_owner else "否"}

== 最近对话（{history_count} 条） ==
{history_str}

== 本地记录路径 ==
{_SKILL_DIR_STR}/imclaw_processed/{date_ymd}/{group_id}.jsonl

== 消息 ==
群聊: {group_name}
发送者: {sender}{"（你的主人）" if from_owner else ""}
内容: {content}

== 操作 ==
回复: cd {_SKILL_DIR_STR}{_CMD_SEP}{_VENV_PY} reply.py "回复内容" --group {group_id}
静默: cd {_SKILL_DIR_STR}{_CMD_SEP}{_VENV_PY} -c "from reply import clear_queue; clear_queue('{group_id}')"
切模式: cd {_SKILL_DIR_STR}{_CMD_SEP}{_VENV_PY} config_group.py --group {group_id} --mode silent|smart
===== 群聊任务结束 [group:{group_id}] ====="""


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
        wake_key = f"imclaw:{group_id}"

        now = time.time()
        last_wake = _warm_sessions.get(wake_key, 0)
        is_cold = (now - last_wake) > _WARM_THRESHOLD
        _warm_sessions[wake_key] = now

        owner_hint = " 👑 [来自主人]" if from_owner else ""
        mentioned_hint = " 📢 [被@提及]" if is_mentioned else ""
        dynamic = _build_dynamic_section(msg)

        if is_cold:
            wake_text = f"""[IMClaw] 收到新消息（群聊激活）{owner_hint}{mentioned_hint}

规则文件: {_RULES_PATH}
请先阅读规则文件，然后处理以下消息。所有命令在 cd {_SKILL_DIR_STR} 下执行。

{dynamic}"""
        else:
            wake_text = f"""[IMClaw] 新消息{owner_hint}{mentioned_hint}

{dynamic}"""

        resp = requests.post(
            f"{gateway_url}/hooks/wake",
            json={"message": wake_text},
            headers={
                "Authorization": f"Bearer {HOOKS_TOKEN}",
                "Content-Type": "application/json"
            },
            timeout=5
        )
        cold_tag = " [冷启动]" if is_cold else ""
        logger.info(f"   🔔 主 Session 唤醒成功 [群:{group_name}]{cold_tag}: HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"   ❌ 主 Session 唤醒失败: {e}")

@skill.on_connect
def on_connect():
    logger.info("\n" + "=" * 50)
    logger.info("✅ 已连接到 IMClaw Hub")
    logger.info(f"📋 订阅的群聊: {skill.subscribed_groups}")
    logger.info("=" * 50 + "\n")

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

@skill.on_task_updated
def on_task_updated(payload):
    """任务事件仅记录日志，不唤醒 Session"""
    event = payload.get('event', '?')
    title = payload.get('title', '?')
    group_id = payload.get('group_id', '')
    logger.info(f"📋 任务事件: {event} - {title} (群:{group_id[:8]})")

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
    
    # 获取响应模式和 @提及 状态（轻量操作，不需要 API 调用）
    response_mode = get_response_mode(group_id)
    is_mentioned = check_if_mentioned(msg, MY_AGENT_ID) if MY_AGENT_ID else False
    
    logger.info(f"   📋 响应模式: {response_mode}, 被@: {is_mentioned}")
    
    # silent 模式短路：未被 @ 且不是主人消息时，仅归档不唤醒
    if response_mode == 'silent' and not is_mentioned and not from_owner:
        logger.info("   🔇 静默模式，未被提及，仅归档")
        archive_message(msg)
        return

    # 获取群成员和历史消息（带缓存，减少 API 调用）
    group_members = []
    recent_history = []
    if group_id:
        group_members = _members_cache.get(group_id)
        if group_members is None:
            group_members = get_group_members(group_id)
            if group_members:
                _members_cache.set(group_id, group_members)
        
        # 冷/热 Session 差异化拉取：冷 Session 拉更多历史建立上下文
        session_key = f"hook:imclaw:{group_id}"
        is_cold = (time.time() - _warm_sessions.get(session_key, 0)) > _WARM_THRESHOLD
        history_limit = 30 if is_cold else 15
        
        recent_history = _history_cache.get(group_id)
        if recent_history is None:
            recent_history = get_recent_history(group_id, limit=history_limit)
            if recent_history:
                _history_cache.set(group_id, recent_history)
    
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
    }
    
    # 处理消息
    logger.info("   📝 开始处理...")
    archive_message(msg)
    write_to_queue(msg)
    wake_session_for_group(msg)

# PID 管理
pid_manager = PIDManager(SKILL_DIR / "bridge.pid")

# 检查是否已有实例运行
force_start = "--force" in sys.argv
if not pid_manager.acquire(force=force_start):
    sys.exit(1)

logger.info(f"📝 PID 文件已写入: {pid_manager.pid_file} (PID: {pid_manager.pid})")

logger.info("\n🚀 启动 WebSocket 连接...")
logger.info("按 Ctrl+C 退出\n")

try:
    skill.run()
finally:
    stop_group_refresh_timer()
    pid_manager.release()
