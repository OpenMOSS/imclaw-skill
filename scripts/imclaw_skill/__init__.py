"""
IMClaw Skill — 让 AI Agent 具备跨网通信能力

这是一个独立的、开箱即用的 Python 包，让你的 Agent 能够：
- 与其他 Agent 实时聊天
- 创建和加入群聊
- 接收和发送消息

快速开始:
    from imclaw_skill import IMClawSkill

    skill = IMClawSkill.from_env()

    @skill.on_message
    def handle(msg):
        print(f"收到: {msg['content']}")

    skill.run()
"""

import os
import platform

from .skill import IMClawSkill, SkillConfig
from .client import IMClawClient

__version__ = "0.1.0"
__all__ = ["IMClawSkill", "SkillConfig", "IMClawClient", "resolve_env", "get_venv_python", "IS_WINDOWS"]

IS_WINDOWS = platform.system() == "Windows"


def get_venv_python(skill_dir: str = "") -> str:
    """获取 venv 中 Python 可执行文件的相对路径（跨平台）

    Args:
        skill_dir: skill 目录路径前缀，为空则只返回 venv 内的相对路径
    """
    if IS_WINDOWS:
        venv_py = "venv\\Scripts\\python.exe"
        sep = "\\"
    else:
        venv_py = "venv/bin/python3"
        sep = "/"
    if skill_dir:
        return f"{skill_dir}{sep}{venv_py}"
    return venv_py


def resolve_env(key: str, fallback: str = "") -> str:
    return os.environ.get(key, "") or fallback
