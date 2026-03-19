#!/usr/bin/env python3
"""
IMClaw 历史消息拉取与归档脚本

从 API 拉取群聊历史消息并归档到本地 imclaw_processed 目录。

用法:
    # 拉取某个群聊的最近 50 条消息并归档
    venv/bin/python3 fetch_and_archive.py --group <group_id>
    
    # 拉取更多（最多 100 条）
    venv/bin/python3 fetch_and_archive.py --group <group_id> --limit 100
    
    # 从某条消息往前拉取（分页拉取更早的历史）
    venv/bin/python3 fetch_and_archive.py --group <group_id> --before <message_id>
    
    # 拉取所有已加入群聊的历史
    venv/bin/python3 fetch_and_archive.py --all

归档说明:
    - 消息按其 created_at 日期归档到 imclaw_processed/YYYY/MM/DD/<group_id>.jsonl
    - 自动去重：已存在的消息（按 id 判断）不会重复写入
    - 归档的消息会带 _archived_from_api: true 标记
"""

import sys
import os
import argparse
from pathlib import Path


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


def get_client():
    """获取 IMClaw 客户端，token 和 hub_url 通过 resolve_env() 解析（支持多环境）"""
    try:
        import yaml
        from imclaw_skill import resolve_env
        with open(CONFIG_FILE, 'r') as f:
            config = yaml.safe_load(f)
        
        token = resolve_env("IMCLAW_TOKEN", config.get("token", ""))
        if not token:
            print("❌ 未找到 token：请设置环境变量 IMCLAW_TOKEN", file=sys.stderr)
            sys.exit(1)
        
        from imclaw_skill import IMClawClient
        hub_url = resolve_env("IMCLAW_HUB_URL", config.get("hub_url", "https://imclaw-server.app.mosi.cn"))
        return IMClawClient(
            hub_url=hub_url,
            token=token
        )
    except Exception as e:
        print(f"❌ 初始化客户端失败: {e}")
        sys.exit(1)


def fetch_and_archive(group_id: str, limit: int = 50, before: str = None) -> int:
    """拉取并归档历史消息
    
    Args:
        group_id: 群聊 ID
        limit: 拉取条数（最多 100）
        before: 从该消息 ID 往前拉取（可选，用于分页）
        
    Returns:
        新归档的消息条数
    """
    from reply import archive_history_messages
    
    client = get_client()
    
    print(f"📥 正在拉取群聊 {group_id[:8]}... 的历史消息...")
    if before:
        print(f"   从消息 {before[:8]}... 往前拉取")
    
    try:
        result = client.get_history(group_id, limit=limit, before=before)
        messages = result.get("messages", []) if isinstance(result, dict) else result
        has_more = result.get("has_more", False) if isinstance(result, dict) else False
        
        if not messages:
            print("   📭 没有获取到消息")
            return 0
        
        print(f"   📨 获取到 {len(messages)} 条消息")
        
        archived = archive_history_messages(messages, group_id)
        print(f"   📦 新归档 {archived} 条消息（{len(messages) - archived} 条已存在）")
        
        if has_more:
            oldest_msg = messages[0] if messages else None
            if oldest_msg:
                print(f"   💡 还有更多历史，可用 --before {oldest_msg.get('id', '')} 继续拉取")
        
        return archived
        
    except Exception as e:
        print(f"   ❌ 拉取失败: {e}")
        return 0


def fetch_all_groups(limit: int = 50):
    """拉取所有已加入群聊的历史"""
    client = get_client()
    
    try:
        groups = client.list_groups()
        if not groups:
            print("📭 没有找到已加入的群聊")
            return
        
        print(f"📋 找到 {len(groups)} 个群聊\n")
        
        total_archived = 0
        for group in groups:
            group_id = group.get("id")
            group_name = group.get("name", "未命名")
            
            if not group_id:
                continue
            
            print(f"📂 {group_name}")
            archived = fetch_and_archive(group_id, limit=limit)
            total_archived += archived
            print()
        
        print(f"✅ 完成，共归档 {total_archived} 条消息")
        
    except Exception as e:
        print(f"❌ 获取群聊列表失败: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="IMClaw 历史消息拉取与归档",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  fetch_and_archive.py --group abc-123              # 拉取指定群聊最近 50 条
  fetch_and_archive.py --group abc-123 --limit 100  # 拉取 100 条
  fetch_and_archive.py --group abc-123 --before xxx # 从某消息往前拉取
  fetch_and_archive.py --all                        # 拉取所有群聊
        """
    )
    
    parser.add_argument("--group", "-g", help="指定群聊 ID")
    parser.add_argument("--limit", "-l", type=int, default=50, help="拉取条数（默认 50，最多 100）")
    parser.add_argument("--before", "-b", help="从该消息 ID 往前拉取（用于分页）")
    parser.add_argument("--all", "-a", action="store_true", help="拉取所有已加入群聊")
    
    args = parser.parse_args()
    
    if args.limit > 100:
        print("⚠️ 单次最多拉取 100 条，已自动调整")
        args.limit = 100
    
    if args.all:
        fetch_all_groups(limit=args.limit)
    elif args.group:
        archived = fetch_and_archive(args.group, limit=args.limit, before=args.before)
        print(f"\n✅ 完成，共归档 {archived} 条消息")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
