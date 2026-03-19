#!/usr/bin/env python3
"""
IMClaw 消息处理脚本

当 OpenClaw 主会话被 cron wake 唤醒时，读取队列中的消息并处理。

特性：
- 按 年/月/日/group_id.jsonl 层级存储
- 每个群组独立 JSONL 文件，便于查看历史
- 聊天记录永久保存（不自动清理）
"""

import sys
import os
import json
from pathlib import Path
from datetime import datetime, timedelta


# 默认清理天数（仅用于手动 cleanup 命令，自动清理已禁用）
DEFAULT_RETENTION_DAYS = 7


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
QUEUE_DIR = SKILL_DIR / "imclaw_queue"
PROCESSED_DIR = SKILL_DIR / "imclaw_processed"
CONFIG_FILE = ASSETS_DIR / "config.yaml"

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

def load_config():
    """加载配置，token 通过 resolve_token() 解析（支持多环境）"""
    import yaml
    from imclaw_skill import resolve_token
    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f)
    config["token"] = resolve_token(config.get("token", ""))
    return config

def get_pending_messages(group_id: str = None):
    """获取待处理消息（支持按 group_id 过滤）
    
    Args:
        group_id: 指定群聊 ID，为 None 时返回所有群聊的消息
    """
    messages = []
    if not QUEUE_DIR.exists():
        return messages
    
    if group_id:
        # 获取指定群聊的消息
        group_dir = QUEUE_DIR / group_id
        if group_dir.exists():
            for msg_file in sorted(group_dir.glob("*.json")):
                try:
                    with open(msg_file) as f:
                        msg = json.load(f)
                        msg['_queue_file'] = str(msg_file)
                        messages.append(msg)
                except:
                    pass
    else:
        # 获取所有群聊的消息（按 group_id 分组目录）
        for group_dir in sorted(QUEUE_DIR.iterdir()):
            if group_dir.is_dir():
                for msg_file in sorted(group_dir.glob("*.json")):
                    try:
                        with open(msg_file) as f:
                            msg = json.load(f)
                            msg['_queue_file'] = str(msg_file)
                            messages.append(msg)
                    except:
                        pass
        # 兼容旧版：也检查根目录的 json 文件
        for msg_file in sorted(QUEUE_DIR.glob("*.json")):
            try:
                with open(msg_file) as f:
                    msg = json.load(f)
                    msg['_queue_file'] = str(msg_file)
                    messages.append(msg)
            except:
                pass
    return messages

def mark_processed(msg_file, msg: dict = None):
    """
    标记消息已处理 - 按 年/月/日/group_id.jsonl 层级存储
    
    结构示例：
    imclaw_processed/
    └── 2026/
        └── 03/
            └── 13/
                └── 543410f3-6bac-4103-80a1-6ca4671501ad.jsonl
    """
    # 读取消息内容（如果未提供）
    if msg is None:
        try:
            with open(msg_file, 'r', encoding='utf-8') as f:
                msg = json.load(f)
        except Exception:
            msg = {}
    
    # 添加处理时间戳
    msg['_processed_at'] = datetime.now().isoformat()
    msg['_source_file'] = msg_file.name
    
    # 构建层级目录：年/月/日
    now = datetime.now()
    day_dir = PROCESSED_DIR / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    
    # 按 group_id 归档
    group_id = msg.get('group_id', 'unknown')
    archive_file = day_dir / f"{group_id}.jsonl"
    
    with open(archive_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(msg, ensure_ascii=False) + '\n')
    
    # 删除原始队列文件
    msg_file.unlink()


def cleanup_old_archives(retention_days: int = None):
    """
    清理过期的归档目录（按日期文件夹清理）
    
    Args:
        retention_days: 保留天数，默认使用 DEFAULT_RETENTION_DAYS
    """
    if retention_days is None:
        retention_days = DEFAULT_RETENTION_DAYS
    
    if not PROCESSED_DIR.exists():
        return 0
    
    cutoff_date = datetime.now() - timedelta(days=retention_days)
    removed_count = 0
    
    # 遍历 年/月/日 目录结构
    for year_dir in PROCESSED_DIR.glob("[0-9][0-9][0-9][0-9]"):
        if not year_dir.is_dir():
            continue
        for month_dir in year_dir.glob("[0-9][0-9]"):
            if not month_dir.is_dir():
                continue
            for day_dir in month_dir.glob("[0-9][0-9]"):
                if not day_dir.is_dir():
                    continue
                try:
                    # 从目录结构解析日期
                    date_str = f"{year_dir.name}-{month_dir.name}-{day_dir.name}"
                    dir_date = datetime.strptime(date_str, "%Y-%m-%d")
                    
                    if dir_date < cutoff_date:
                        # 删除该日期目录下的所有文件
                        for f in day_dir.glob("*.jsonl"):
                            f.unlink()
                            removed_count += 1
                        # 删除空目录
                        day_dir.rmdir()
                        print(f"  🗑️ 已删除过期归档: {date_str}/")
                        
                        # 清理空的月份目录
                        if not any(month_dir.iterdir()):
                            month_dir.rmdir()
                        # 清理空的年份目录
                        if not any(year_dir.iterdir()):
                            year_dir.rmdir()
                except (ValueError, OSError):
                    continue
    
    # 兼容：清理旧版 processed_*.jsonl 文件
    for archive_file in PROCESSED_DIR.glob("processed_*.jsonl"):
        try:
            date_str = archive_file.stem.replace("processed_", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff_date:
                archive_file.unlink()
                removed_count += 1
                print(f"  🗑️ 已删除旧版归档: {archive_file.name}")
        except (ValueError, OSError):
            continue
    
    return removed_count


def migrate_legacy_files():
    """
    迁移旧版文件到新的 年/月/日/group_id.jsonl 格式
    
    支持迁移：
    1. 散落的 JSON 文件（20260313_154208_373588.json）
    2. 旧版 JSONL 文件（processed_2026-03-13.jsonl）
    
    Returns:
        int: 迁移的消息数量
    """
    if not PROCESSED_DIR.exists():
        return 0
    
    migrated = 0
    # (year, month, day, group_id) -> [messages]
    archives = {}
    
    # 1. 迁移散落的 JSON 文件
    legacy_json_files = list(PROCESSED_DIR.glob("2*.json"))
    if legacy_json_files:
        print(f"📦 发现 {len(legacy_json_files)} 个旧版 JSON 文件...")
        
        for legacy_file in sorted(legacy_json_files):
            try:
                with open(legacy_file, 'r', encoding='utf-8') as f:
                    msg = json.load(f)
                
                # 从文件名解析日期：20260313_154208_373588.json
                filename = legacy_file.stem
                year, month, day = filename[:4], filename[4:6], filename[6:8]
                group_id = msg.get('group_id', 'unknown')
                
                msg['_processed_at'] = msg.get('created_at', datetime.now().isoformat())
                msg['_source_file'] = legacy_file.name
                msg['_migrated'] = True
                
                key = (year, month, day, group_id)
                if key not in archives:
                    archives[key] = []
                archives[key].append(msg)
                
                legacy_file.unlink()
                migrated += 1
                
            except Exception as e:
                print(f"  ⚠️ 迁移失败 {legacy_file.name}: {e}")
    
    # 2. 迁移旧版 JSONL 文件（processed_2026-03-13.jsonl）
    legacy_jsonl_files = list(PROCESSED_DIR.glob("processed_*.jsonl"))
    if legacy_jsonl_files:
        print(f"📦 发现 {len(legacy_jsonl_files)} 个旧版 JSONL 文件...")
        
        for jsonl_file in legacy_jsonl_files:
            try:
                # 从文件名解析日期：processed_2026-03-13.jsonl
                date_str = jsonl_file.stem.replace("processed_", "")
                parts = date_str.split("-")
                if len(parts) != 3:
                    continue
                year, month, day = parts
                
                with open(jsonl_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            group_id = msg.get('group_id', 'unknown')
                            msg['_migrated'] = True
                            
                            key = (year, month, day, group_id)
                            if key not in archives:
                                archives[key] = []
                            archives[key].append(msg)
                            migrated += 1
                        except json.JSONDecodeError:
                            continue
                
                jsonl_file.unlink()
                
            except Exception as e:
                print(f"  ⚠️ 迁移失败 {jsonl_file.name}: {e}")
    
    if not archives:
        return 0
    
    # 写入新的层级结构
    for (year, month, day, group_id), messages in archives.items():
        day_dir = PROCESSED_DIR / year / month / day
        day_dir.mkdir(parents=True, exist_ok=True)
        
        archive_file = day_dir / f"{group_id}.jsonl"
        with open(archive_file, 'a', encoding='utf-8') as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + '\n')
        print(f"  📁 已归档 {len(messages)} 条消息到 {year}/{month}/{day}/{group_id}.jsonl")
    
    return migrated

def main():
    print("=" * 50)
    print("🦞 IMClaw 消息队列管理")
    print("=" * 50)
    
    # 检查命令行参数
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "migrate":
            migrated = migrate_legacy_files()
            print(f"✅ 迁移完成: {migrated} 个文件")
            return
        elif cmd == "cleanup":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_RETENTION_DAYS
            removed = cleanup_old_archives(days)
            print(f"✅ 清理完成: 删除 {removed} 个过期归档")
            return
        elif cmd == "archive":
            # 归档所有待处理消息（不回复，仅归档）
            archived = archive_pending_messages()
            print(f"✅ 归档完成: {archived} 条消息")
            return
        elif cmd == "help":
            print("""
用法: process_messages.py [命令]

命令:
  (无)      显示待处理消息（不处理）
  archive   归档所有待处理消息（不回复）
  migrate   迁移旧版文件到 年/月/日/group_id.jsonl 格式
  cleanup [天数]   清理过期归档（默认保留 7 天）
  help      显示此帮助

注意: 消息回复由 Agent 智能处理，此脚本仅负责队列管理。

归档结构:
  imclaw_processed/
  └── 2026/
      └── 03/
          └── 13/
              └── <group_id>.jsonl
""")
            return
    
    # 加载配置
    config = load_config()
    print("✅ 配置加载成功")
    
    # 自动迁移旧版文件（如有）
    migrated = migrate_legacy_files()
    if migrated > 0:
        print(f"📦 已自动迁移 {migrated} 个旧版文件")
    
    # 自动清理已禁用 - 聊天记录永久保存
    # 如需手动清理，运行: python process_messages.py cleanup [天数]
    # retention_days = config.get("processed_retention_days", DEFAULT_RETENTION_DAYS)
    # removed = cleanup_old_archives(retention_days)
    # if removed > 0:
    #     print(f"🗑️ 已清理 {removed} 个过期归档")
    
    # 显示待处理消息（不自动处理）
    messages = get_pending_messages()
    
    if not messages:
        print("📭 没有待处理消息")
        return
    
    print(f"\n📬 发现 {len(messages)} 条待处理消息:\n")
    
    for i, msg in enumerate(messages, 1):
        content = msg.get("content", "")[:60]
        sender = msg.get("sender_name", msg.get("sender_id", "未知")[:8])
        group = msg.get("group_name", msg.get("group_id", "未知")[:8])
        msg_id = msg.get("id", "")[:8]
        print(f"  {i}. [{group}] {sender}: {content}")
        print(f"     消息ID: {msg_id}  群聊ID: {msg.get('group_id', 'N/A')}")
        print()
    
    print("=" * 50)
    print("提示: 运行 'process_messages.py archive' 归档这些消息")
    print("      消息回复请由 Agent 智能处理")
    print("=" * 50)


def archive_pending_messages(group_id: str = None):
    """归档待处理消息（不回复）
    
    Args:
        group_id: 指定群聊 ID，为 None 时归档所有群聊
    """
    archived = 0
    
    if group_id:
        # 归档指定群聊
        group_dir = QUEUE_DIR / group_id
        if group_dir.exists():
            for msg_file in sorted(group_dir.glob("*.json")):
                try:
                    with open(msg_file, encoding='utf-8') as f:
                        msg = json.load(f)
                    mark_processed(msg_file, msg)
                    archived += 1
                except Exception as e:
                    print(f"  ⚠️ 归档失败 {msg_file.name}: {e}")
            # 清理空目录
            if not any(group_dir.iterdir()):
                group_dir.rmdir()
    else:
        # 归档所有群聊
        for group_dir in list(QUEUE_DIR.iterdir()):
            if group_dir.is_dir():
                for msg_file in sorted(group_dir.glob("*.json")):
                    try:
                        with open(msg_file, encoding='utf-8') as f:
                            msg = json.load(f)
                        mark_processed(msg_file, msg)
                        archived += 1
                    except Exception as e:
                        print(f"  ⚠️ 归档失败 {msg_file.name}: {e}")
                # 清理空目录
                if not any(group_dir.iterdir()):
                    group_dir.rmdir()
        # 兼容旧版：也处理根目录的 json 文件
        for msg_file in sorted(QUEUE_DIR.glob("*.json")):
            try:
                with open(msg_file, encoding='utf-8') as f:
                    msg = json.load(f)
                mark_processed(msg_file, msg)
                archived += 1
            except Exception as e:
                print(f"  ⚠️ 归档失败 {msg_file.name}: {e}")
    
    return archived

if __name__ == "__main__":
    main()
