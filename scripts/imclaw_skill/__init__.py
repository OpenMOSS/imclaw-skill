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
__all__ = ["IMClawSkill", "SkillConfig", "IMClawClient", "resolve_token"]


# 合并主分支时
# 只需把 __init__.py 中的 resolve_token() 函数体简化为一行：
# def resolve_token(fallback: str = "") -> str:
#     return os.environ.get("IMCLAW_TOKEN", "") or fallback

# ━━━ 多环境支持（合并主分支时简化此函数，保留 fallback 逻辑即可）━━━
def resolve_token(fallback: str = "") -> str:
    """解析 Token，支持多环境

    查找顺序：
    1. IMCLAW_TOKEN_{ENV}（仅当 IMCLAW_ENV 已设置，如 IMCLAW_ENV=TEST → IMCLAW_TOKEN_TEST）
    2. IMCLAW_TOKEN
    3. fallback（通常来自 config.yaml）

    合并主分支时替换为：
        return os.environ.get("IMCLAW_TOKEN", "") or fallback
    """
    env = os.environ.get("IMCLAW_ENV", "").upper()
    if env:
        env_token = os.environ.get(f"IMCLAW_TOKEN_{env}", "")
        if env_token:
            return env_token
    return os.environ.get("IMCLAW_TOKEN", "") or fallback
# ━━━ 多环境支持结束 ━━━
