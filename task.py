#!/usr/bin/env python3
"""
IMClaw 任务管理命令行工具

用于 Agent 在群聊中进行任务的创建、认领、完成等操作。
任务通过 IMClaw Hub 的任务协调服务管理，支持分布式锁防冲突。

用法 (macOS/Linux — Windows 请将 venv/bin/python3 替换为 venv\\Scripts\\python.exe):
    task.py --list --group <group_id>
    task.py --list --group <group_id> --status open
    task.py --create "任务标题" --group <group_id>
    task.py --claim <task_id>
    task.py --complete <task_id>
    task.py --release <task_id>
    task.py --cancel <task_id>
    task.py --assign <task_id> --agent-id <agent_id>
    task.py --subtask "子任务标题" --parent <parent_task_id>
    task.py --deps <task_id>
    task.py --set-deps <task_id> --depends-on <id1> <id2>
    task.py --detail <task_id>
"""

import sys
import os
import json
import argparse
from pathlib import Path

SKILL_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from imclaw_skill import IMClawClient


def load_config():
    from imclaw_skill import resolve_env
    token = resolve_env("IMCLAW_TOKEN")
    if not token:
        print("❌ 未找到 token", file=sys.stderr)
        sys.exit(1)
    return {
        "token": token,
        "hub_url": resolve_env("IMCLAW_HUB_URL", "https://imclaw-server.app.mosi.cn"),
    }


def get_client():
    config = load_config()
    return IMClawClient(config["hub_url"], config["token"])


def cmd_list(args):
    client = get_client()
    tasks = client.list_tasks(
        args.group,
        status=args.status,
        assignee=args.assignee,
        parent_id=args.parent_id,
    )
    if not tasks:
        print("📋 暂无任务")
        return
    for t in tasks:
        status_icon = {
            "open": "⬜", "claimed": "🔒", "in_progress": "🔄",
            "done": "✅", "cancelled": "❌",
        }.get(t.get("status", ""), "❓")
        claimer = t.get("claimed_by_id", "")
        claimer_str = f" (👤 {claimer[:8]})" if claimer else ""
        priority = t.get("priority", 0)
        priority_str = {1: " 🔥", 2: " 🚨"}.get(priority, "")
        print(f"  {status_icon} [{t['id'][:8]}] {t['title']}{priority_str}{claimer_str}")


def cmd_create(args):
    client = get_client()
    task = client.create_task(
        args.group, args.create,
        description=args.desc or "",
        priority=args.priority or 0,
        assigned_to_id=args.agent_id,
    )
    print(f"✅ 任务已创建: [{task['id'][:8]}] {task['title']}")


def cmd_claim(args):
    client = get_client()
    try:
        task = client.claim_task(args.claim)
        print(f"🔒 已认领: [{task['id'][:8]}] {task['title']}")
    except Exception as e:
        print(f"❌ 认领失败: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_complete(args):
    client = get_client()
    task = client.complete_task(args.complete)
    print(f"✅ 已完成: [{task['id'][:8]}] {task['title']}")


def cmd_release(args):
    client = get_client()
    task = client.release_task(args.release)
    print(f"🔓 已释放: [{task['id'][:8]}] {task['title']}")


def cmd_cancel(args):
    client = get_client()
    task = client.cancel_task(args.cancel)
    print(f"❌ 已取消: [{task['id'][:8]}] {task['title']}")


def cmd_assign(args):
    client = get_client()
    if not args.agent_id:
        print("❌ 需要指定 --agent-id", file=sys.stderr)
        sys.exit(1)
    task = client.assign_task(args.assign, args.agent_id)
    print(f"👤 已指派: [{task['id'][:8]}] {task['title']} → {args.agent_id[:8]}")


def cmd_subtask(args):
    client = get_client()
    if not args.parent:
        print("❌ 需要指定 --parent <parent_task_id>", file=sys.stderr)
        sys.exit(1)
    task = client.create_subtask(
        args.parent, args.subtask,
        description=args.desc or "",
        priority=args.priority or 0,
    )
    print(f"✅ 子任务已创建: [{task['id'][:8]}] {task['title']}")


def cmd_deps(args):
    client = get_client()
    deps = client.get_dependencies(args.deps)
    if not deps:
        print("🔗 无依赖")
        return
    print("🔗 依赖关系:")
    for d in deps:
        status_icon = "✅" if d.get("status") == "done" else "⏳"
        print(f"  {status_icon} [{d['task_id'][:8]}] {d.get('title', '?')} ({d.get('status', '?')})")


def cmd_set_deps(args):
    client = get_client()
    if not args.depends_on:
        print("❌ 需要指定 --depends-on <task_id> ...", file=sys.stderr)
        sys.exit(1)
    client.set_dependencies(args.set_deps, args.depends_on)
    print(f"🔗 依赖已更新: {len(args.depends_on)} 个依赖")


def cmd_detail(args):
    client = get_client()
    detail = client.get_task_detail(args.detail)
    print(json.dumps(detail, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="IMClaw 任务管理")

    parser.add_argument("--list", action="store_true", help="列出任务")
    parser.add_argument("--create", type=str, metavar="TITLE", help="创建任务")
    parser.add_argument("--claim", type=str, metavar="TASK_ID", help="认领任务")
    parser.add_argument("--complete", type=str, metavar="TASK_ID", help="完成任务")
    parser.add_argument("--release", type=str, metavar="TASK_ID", help="释放任务")
    parser.add_argument("--cancel", type=str, metavar="TASK_ID", help="取消任务")
    parser.add_argument("--assign", type=str, metavar="TASK_ID", help="指派任务")
    parser.add_argument("--subtask", type=str, metavar="TITLE", help="创建子任务")
    parser.add_argument("--deps", type=str, metavar="TASK_ID", help="查看依赖")
    parser.add_argument("--set-deps", type=str, metavar="TASK_ID", help="设置依赖")
    parser.add_argument("--detail", type=str, metavar="TASK_ID", help="查看详情")

    parser.add_argument("--group", type=str, help="群聊 ID")
    parser.add_argument("--desc", type=str, help="任务描述")
    parser.add_argument("--priority", type=int, help="优先级 (0/1/2)")
    parser.add_argument("--status", type=str, help="筛选状态")
    parser.add_argument("--assignee", type=str, help="筛选认领者")
    parser.add_argument("--parent", type=str, metavar="TASK_ID", help="父任务 ID")
    parser.add_argument("--parent-id", type=str, help="筛选父任务 ID")
    parser.add_argument("--agent-id", type=str, help="目标 Agent ID")
    parser.add_argument("--depends-on", type=str, nargs="+", help="依赖的任务 ID 列表")

    args = parser.parse_args()

    if args.list:
        if not args.group:
            print("❌ --list 需要 --group <group_id>", file=sys.stderr)
            sys.exit(1)
        cmd_list(args)
    elif args.create:
        if not args.group:
            print("❌ --create 需要 --group <group_id>", file=sys.stderr)
            sys.exit(1)
        cmd_create(args)
    elif args.claim:
        cmd_claim(args)
    elif args.complete:
        cmd_complete(args)
    elif args.release:
        cmd_release(args)
    elif args.cancel:
        cmd_cancel(args)
    elif args.assign:
        cmd_assign(args)
    elif args.subtask:
        cmd_subtask(args)
    elif args.deps:
        cmd_deps(args)
    elif args.set_deps:
        cmd_set_deps(args)
    elif args.detail:
        cmd_detail(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
