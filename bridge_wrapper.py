#!/usr/bin/env python3
"""
Bridge 守护进程 — 崩溃自动重启

通过 subprocess 运行 bridge_simple.py，当进程异常退出时自动重启（5s 延迟）。
正常退出（exit code 0）或 bridge.state 为 disabled 时停止守护。

用法:
    macOS/Linux:
        nohup venv/bin/python3 bridge_wrapper.py > bridge.log 2>&1 &
    Windows (PowerShell):
        Start-Process -NoNewWindow -FilePath venv\Scripts\python.exe -ArgumentList "bridge_wrapper.py" -RedirectStandardOutput bridge.log

上下线控制:
    上线: echo enabled > bridge.state  (然后启动 wrapper)
    下线: echo disabled > bridge.state  (wrapper 会在下次检查时自动退出)
"""

import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).parent.resolve()
STATE_FILE = SKILL_DIR / "bridge.state"
RESTART_DELAY = 5


def is_enabled():
    if not STATE_FILE.exists():
        return True
    return STATE_FILE.read_text().strip() == "enabled"


def main():
    while True:
        if not is_enabled():
            print(f"[wrapper] Bridge 已禁用 (bridge.state=disabled)，退出守护")
            break

        print(f"[wrapper] 启动 bridge_simple.py ...")
        proc = subprocess.run(
            [sys.executable, "bridge_simple.py"] + sys.argv[1:],
            cwd=str(SKILL_DIR)
        )

        if proc.returncode == 0:
            print(f"[wrapper] bridge_simple.py 正常退出")
            break

        if not is_enabled():
            print(f"[wrapper] Bridge 已禁用，不再重启")
            break

        print(f"[wrapper] bridge_simple.py 异常退出 (code={proc.returncode})，{RESTART_DELAY}秒后重启...")
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
