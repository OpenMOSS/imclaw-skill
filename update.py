#!/usr/bin/env python3
"""
IMClaw Skill 自更新脚本

用法: venv/bin/python3 update.py
流程:
  1. 查询 Hub 获取最新版本信息
  2. 与本地 _meta.json 比较版本号
  3. 下载新版本 tarball
  4. 备份用户配置和数据
  5. 解压覆盖代码文件
  6. 恢复用户配置和数据
  7. 重装依赖
  8. 重启 bridge 进程
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

SKILL_DIR = Path(__file__).resolve().parent
META_FILE = SKILL_DIR / "_meta.json"
PID_FILE = SKILL_DIR / "bridge.pid"

USER_DATA_DIRS = ["imclaw_queue", "imclaw_processed", "sessions"]
USER_CONFIG_FILES = [
    "config.yaml",
    "group_settings.yaml",
    "assets/group_settings.yaml",
    "bridge.log",
    "bridge.pid",
    "bridge_status.json",
]
# tarball 中自带新版本的 _meta.json，恢复备份时绝不能用旧的覆盖回去
NEVER_RESTORE = {"_meta.json"}

def load_env():
    """Load IMCLAW_HUB_URL from gateway.env or environment."""
    env_file = Path.home() / ".openclaw" / "gateway.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

def get_hub_url():
    env_suffix = os.environ.get("IMCLAW_ENV", "")
    if env_suffix:
        url = os.environ.get(f"IMCLAW_HUB_URL_{env_suffix}")
        if url:
            return url.rstrip("/")
    return os.environ.get("IMCLAW_HUB_URL", "https://imclaw-server.app.mosi.cn").rstrip("/")

def get_local_version():
    if not META_FILE.exists():
        return "0.0.0"
    try:
        return json.loads(META_FILE.read_text())["version"]
    except Exception:
        return "0.0.0"

def parse_version(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0, 0, 0)

def fetch_latest(hub_url):
    url = f"{hub_url}/api/v1/skill/latest"
    try:
        req = Request(url, headers={"User-Agent": "imclaw-skill-updater"})
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except URLError as e:
        print(f"❌ 无法连接 Hub: {e}")
        return None
    except Exception as e:
        print(f"❌ 获取版本信息失败: {e}")
        return None

def download_file(url, dest):
    print(f"  下载中: {url}")
    try:
        req = Request(url, headers={"User-Agent": "imclaw-skill-updater"})
        with urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded * 100 // total
                        print(f"\r  进度: {pct}% ({downloaded // 1024}KB / {total // 1024}KB)", end="", flush=True)
            if total > 0:
                print()
        return True
    except Exception as e:
        print(f"\n❌ 下载失败: {e}")
        return False

def stop_bridge():
    if not PID_FILE.exists():
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"  已停止 bridge 进程 (PID: {pid})")
        time.sleep(2)
    except (ProcessLookupError, ValueError):
        pass

def start_bridge():
    venv_python = SKILL_DIR / "venv" / "bin" / "python3"
    if not venv_python.exists():
        venv_python = SKILL_DIR / "venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        print("  ⚠️ 未找到 venv，跳过启动 bridge")
        return

    bridge_script = SKILL_DIR / "bridge_simple.py"
    bridge_log = SKILL_DIR / "bridge.log"
    subprocess.Popen(
        [str(venv_python), str(bridge_script)],
        stdout=open(bridge_log, "w"),
        stderr=subprocess.STDOUT,
        cwd=str(SKILL_DIR),
        start_new_session=True,
    )
    time.sleep(2)
    if PID_FILE.exists():
        print(f"  ✅ bridge 已重启 (PID: {PID_FILE.read_text().strip()})")
    else:
        print("  ⚠️ bridge 可能未成功启动，请检查 bridge.log")

def install_deps():
    venv_pip = SKILL_DIR / "venv" / "bin" / "pip"
    if not venv_pip.exists():
        venv_pip = SKILL_DIR / "venv" / "Scripts" / "pip.exe"
    if not venv_pip.exists():
        print("  ⚠️ 未找到 venv/pip，跳过依赖安装")
        return

    req_file = SKILL_DIR / "scripts" / "requirements.txt"
    if req_file.exists():
        print("  安装依赖...")
        subprocess.run(
            [str(venv_pip), "install", "-q", "-r", str(req_file)],
            cwd=str(SKILL_DIR),
        )

def main():
    load_env()
    hub_url = get_hub_url()

    print("=== IMClaw Skill 更新 ===")
    print()

    local_ver = get_local_version()
    print(f"当前版本: v{local_ver}")

    print(f"查询最新版本...")
    latest = fetch_latest(hub_url)
    if not latest:
        sys.exit(1)

    remote_ver = latest.get("version", "0.0.0")
    download_url = latest.get("download_url", "")

    print(f"最新版本: v{remote_ver}")
    print()

    if parse_version(remote_ver) <= parse_version(local_ver):
        print("✅ 已是最新版本，无需更新")
        return

    if not download_url:
        print("❌ 未找到下载地址")
        sys.exit(1)

    if latest.get("changelog"):
        print("更新内容:")
        for item in latest["changelog"]:
            print(f"  · {item}")
        print()

    print(f"📦 开始更新 v{local_ver} → v{remote_ver}")
    print()

    print("1/6 停止 bridge 进程...")
    stop_bridge()

    with tempfile.TemporaryDirectory() as tmpdir:
        tarball_path = os.path.join(tmpdir, "update.tar.gz")

        print("2/6 下载新版本...")
        if not download_file(download_url, tarball_path):
            print("  尝试重启 bridge...")
            start_bridge()
            sys.exit(1)

        print("3/6 备份用户数据...")
        backup_dir = os.path.join(tmpdir, "backup")
        os.makedirs(backup_dir, exist_ok=True)

        for dirname in USER_DATA_DIRS:
            src = SKILL_DIR / dirname
            if src.exists():
                shutil.copytree(str(src), os.path.join(backup_dir, dirname))
                print(f"  备份: {dirname}/")

        for filename in USER_CONFIG_FILES:
            src = SKILL_DIR / filename
            if src.exists():
                shutil.copy2(str(src), os.path.join(backup_dir, filename))
                print(f"  备份: {filename}")

        venv_dir = SKILL_DIR / "venv"
        venv_existed = venv_dir.exists()

        print("4/6 解压新版本...")
        try:
            with tarfile.open(tarball_path, "r:gz") as tar:
                members = tar.getmembers()
                prefix = ""
                if members and "/" in members[0].name:
                    prefix = members[0].name.split("/")[0] + "/"

                for member in members:
                    if prefix:
                        member.name = member.name[len(prefix):]
                    if not member.name or member.name.startswith(".."):
                        continue
                    base = member.name.split("/")[0]
                    if base in USER_DATA_DIRS or member.name.rstrip("/") in USER_CONFIG_FILES:
                        continue
                    if base == "venv":
                        continue
                    tar.extract(member, str(SKILL_DIR))
            new_meta = None
            if META_FILE.exists():
                new_meta = META_FILE.read_text(encoding="utf-8")
            print("  ✅ 解压完成")
        except Exception as e:
            print(f"  ❌ 解压失败: {e}")
            print("5/6 恢复备份...")
            for dirname in USER_DATA_DIRS:
                bak = os.path.join(backup_dir, dirname)
                if os.path.exists(bak):
                    shutil.copytree(bak, str(SKILL_DIR / dirname), dirs_exist_ok=True)
            for filename in USER_CONFIG_FILES:
                bak = os.path.join(backup_dir, filename)
                if os.path.exists(bak):
                    shutil.copy2(bak, str(SKILL_DIR / filename))
            start_bridge()
            sys.exit(1)

        print("5/6 恢复用户数据...")
        for dirname in USER_DATA_DIRS:
            bak = os.path.join(backup_dir, dirname)
            dst = SKILL_DIR / dirname
            if os.path.exists(bak):
                if dst.exists():
                    shutil.rmtree(str(dst))
                shutil.copytree(bak, str(dst))
                print(f"  恢复: {dirname}/")

        for filename in USER_CONFIG_FILES:
            bak = os.path.join(backup_dir, filename)
            if os.path.exists(bak):
                shutil.copy2(bak, str(SKILL_DIR / filename))
                print(f"  恢复: {filename}")

        if new_meta:
            META_FILE.write_text(new_meta, encoding="utf-8")

    print("6/6 安装依赖并重启...")
    if venv_existed:
        install_deps()
    start_bridge()

    new_ver = get_local_version()
    print()
    print(f"=== 更新完成: v{local_ver} → v{new_ver} ===")


if __name__ == "__main__":
    main()
