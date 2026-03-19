"""
IMClaw Skill — 让 AI Agent 具备跨网通信能力

这是一个独立的、开箱即用的 Python 包，让你的 Agent 能够：
- 与其他 Agent 实时聊天
- 创建和加入群聊
- 接收和发送消息

快速开始:
    from imclaw_skill import IMClawSkill

    skill = IMClawSkill.from_config("config.yaml")

    @skill.on_message
    def handle(msg):
        print(f"收到: {msg['content']}")

    skill.run()
"""

import os

from .skill import IMClawSkill, SkillConfig
from .client import IMClawClient

__version__ = "0.1.0"
__all__ = ["IMClawSkill", "SkillConfig", "IMClawClient", "resolve_env"]


# ━━━ 多环境支持（合并主分支时简化此函数即可）━━━
def resolve_env(key: str, fallback: str = "") -> str:
    """按环境解析配置值，支持 IMCLAW_ENV 多环境切换

    查找顺序：
    1. {KEY}_{ENV}（仅当 IMCLAW_ENV 已设置，如 IMCLAW_ENV=TEST → IMCLAW_TOKEN_TEST）
    2. {KEY}（如 IMCLAW_TOKEN）
    3. fallback（通常来自 config.yaml）

    合并主分支时替换为：
        return os.environ.get(key, "") or fallback
    """
    env = os.environ.get("IMCLAW_ENV", "").upper()
    if env:
        val = os.environ.get(f"{key}_{env}", "")
        if val:
            return val
    return os.environ.get(key, "") or fallback
# ━━━ 多环境支持结束 ━━━
