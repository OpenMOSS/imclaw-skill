#!/usr/bin/env python3
"""
IMClaw 群聊配置管理脚本

管理每个群聊的响应模式配置。

用法:
    # 查看所有群聊及其配置
    python config_group.py --list
    
    # 设置某个群聊的响应模式
    python config_group.py --group <group_id> --mode silent
    python config_group.py --group <group_id> --mode smart
    
    # 设置默认响应模式
    python config_group.py --default --mode smart
    
    # 重置某个群聊为默认配置
    python config_group.py --group <group_id> --reset

响应模式说明:
    - silent: 静默模式 - 只有被 @ 或明确提到名字才响应
    - smart:  智能模式 - 被 @ / 提名 / AI 判断在进行中的对话时响应（默认）
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
    if (script_dir / "assets" / "config.yaml").exists():
        return script_dir
    
    return Path.home() / ".openclaw" / "workspace" / "skills" / "imclaw"


SKILL_DIR = get_skill_dir()
ASSETS_DIR = SKILL_DIR / "assets"
SESSIONS_DIR = SKILL_DIR / "sessions"
GROUP_SETTINGS_FILE = ASSETS_DIR / "group_settings.yaml"
CONFIG_FILE = ASSETS_DIR / "config.yaml"

sys.path.insert(0, str(SKILL_DIR / "scripts"))

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


def load_settings() -> dict:
    """加载群聊配置"""
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
        print(f"❌ 加载配置失败: {e}")
        return {"default": {"response_mode": "smart"}, "groups": {}}


def save_settings(settings: dict):
    """保存群聊配置"""
    try:
        import yaml
        with open(GROUP_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            yaml.dump(settings, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"✅ 配置已保存到 {GROUP_SETTINGS_FILE}")
    except Exception as e:
        print(f"❌ 保存配置失败: {e}")
        sys.exit(1)


def get_imclaw_client():
    """获取 IMClaw 客户端，token 和 hub_url 通过 resolve_env() 解析（支持多环境）"""
    try:
        import yaml
        from imclaw_skill import resolve_env, IMClawClient
        with open(CONFIG_FILE, 'r') as f:
            config = yaml.safe_load(f)
        
        token = resolve_env("IMCLAW_TOKEN", config.get("token", ""))
        if not token:
            print("⚠️ 未找到 token：请设置环境变量 IMCLAW_TOKEN", file=sys.stderr)
            return None
        
        hub_url = resolve_env("IMCLAW_HUB_URL", config.get("hub_url", "https://imclaw-server.app.mosi.cn"))
        return IMClawClient(
            hub_url=hub_url,
            token=token
        )
    except Exception as e:
        print(f"⚠️ 无法初始化 IMClaw 客户端: {e}")
        return None


def list_groups_and_settings():
    """列出所有群聊及其配置"""
    settings = load_settings()
    default_mode = settings["default"].get("response_mode", "smart")
    
    print(f"\n📋 群聊响应配置")
    print(f"{'=' * 60}")
    print(f"默认模式: {default_mode}")
    print(f"{'=' * 60}\n")
    
    # 尝试获取群聊列表
    client = get_imclaw_client()
    groups = []
    if client:
        try:
            groups = client.list_groups()
        except Exception as e:
            print(f"⚠️ 无法获取群聊列表: {e}")
    
    if groups:
        print(f"{'群聊名称':<20} {'群聊ID':<40} {'响应模式':<10}")
        print(f"{'-' * 70}")
        
        for group in groups:
            group_id = group.get("id", "")
            group_name = group.get("name", "未命名")[:18]
            group_config = settings["groups"].get(group_id, {})
            mode = group_config.get("response_mode", f"{default_mode} (默认)")
            
            print(f"{group_name:<20} {group_id:<40} {mode:<10}")
    else:
        print("未找到群聊，或无法连接到 Hub")
    
    # 显示已配置但可能不在当前群聊列表的配置
    configured_groups = settings.get("groups", {})
    if configured_groups:
        known_ids = {g.get("id") for g in groups}
        orphan_configs = {k: v for k, v in configured_groups.items() if k not in known_ids}
        
        if orphan_configs:
            print(f"\n⚠️ 以下群聊有配置但未在当前群聊列表中：")
            for gid, config in orphan_configs.items():
                print(f"  {gid}: {config.get('response_mode', '?')}")
    
    print()


def set_group_mode(group_id: str, mode: str):
    """设置指定群聊的响应模式"""
    if mode not in ("silent", "smart"):
        print(f"❌ 无效的模式: {mode}")
        print("   有效模式: silent, smart")
        sys.exit(1)
    
    settings = load_settings()
    
    if "groups" not in settings:
        settings["groups"] = {}
    
    settings["groups"][group_id] = {"response_mode": mode}
    save_settings(settings)
    
    # 同步写入 session 文件，供 bridge 读取群聊响应模式
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_file = SESSIONS_DIR / f"session_{group_id}.json"
    try:
        data = {"group_id": group_id, "response_mode": mode, "updated_at": datetime.now().isoformat()}
        if session_file.exists():
            existing = json.loads(session_file.read_text(encoding="utf-8"))
            data["group_name"] = existing.get("group_name", "")
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 同步 session 文件失败: {e}")
    
    print(f"✅ 群聊 {group_id} 的响应模式已设置为: {mode}")


def set_default_mode(mode: str):
    """设置默认响应模式"""
    if mode not in ("silent", "smart"):
        print(f"❌ 无效的模式: {mode}")
        print("   有效模式: silent, smart")
        sys.exit(1)
    
    settings = load_settings()
    settings["default"] = {"response_mode": mode}
    save_settings(settings)
    
    print(f"✅ 默认响应模式已设置为: {mode}")


def reset_group(group_id: str):
    """重置某个群聊的配置（使用默认值）"""
    settings = load_settings()
    
    if group_id in settings.get("groups", {}):
        del settings["groups"][group_id]
        save_settings(settings)
        print(f"✅ 群聊 {group_id} 的配置已重置为默认")
    else:
        print(f"ℹ️ 群聊 {group_id} 没有自定义配置")


def main():
    parser = argparse.ArgumentParser(
        description="IMClaw 群聊配置管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
响应模式说明:
  silent  - 静默模式: 只有被 @ 或明确提到名字才响应
  smart   - 智能模式: 被 @ / 提名 / AI 判断在进行中的对话时响应（默认）

示例:
  config_group.py --list                           # 查看所有配置
  config_group.py --group abc-123 --mode silent    # 设置群聊为静默模式
  config_group.py --default --mode smart           # 设置默认模式
  config_group.py --group abc-123 --reset          # 重置群聊配置
        """
    )
    
    parser.add_argument("--list", "-l", action="store_true", help="列出所有群聊及其配置")
    parser.add_argument("--group", "-g", help="指定群聊 ID")
    parser.add_argument("--mode", "-m", choices=["silent", "smart"], help="响应模式")
    parser.add_argument("--default", "-d", action="store_true", help="操作默认配置")
    parser.add_argument("--reset", "-r", action="store_true", help="重置群聊为默认配置")
    
    args = parser.parse_args()
    
    if args.list:
        list_groups_and_settings()
        return
    
    if args.default and args.mode:
        set_default_mode(args.mode)
        return
    
    if args.group and args.reset:
        reset_group(args.group)
        return
    
    if args.group and args.mode:
        set_group_mode(args.group, args.mode)
        return
    
    # 没有有效参数，显示帮助
    parser.print_help()


if __name__ == "__main__":
    main()
