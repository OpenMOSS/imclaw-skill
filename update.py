#!/usr/bin/env python3
"""
IMClaw Skill 更新检查 & 下载脚本

职责仅限于：
  1. 查询 Hub 获取最新版本信息
  2. 与本地 _meta.json 比较版本号
  3. 如需更新，下载 tarball 到临时目录

输出 JSON 供 Agent 消费，Agent 按 UPDATE.md 执行后续步骤。

用法:
  venv/bin/python3 update.py              # 检查 + 下载
  venv/bin/python3 update.py --check      # 仅检查，不下载
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

SKILL_DIR = Path(__file__).resolve().parent
META_FILE = SKILL_DIR / "_meta.json"


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
    req = Request(url, headers={"User-Agent": "imclaw-skill-updater"})
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def download_file(url, dest):
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
                    print(
                        f"\r  进度: {pct}% ({downloaded // 1024}KB / {total // 1024}KB)",
                        end="", flush=True, file=sys.stderr,
                    )
        if total > 0:
            print(file=sys.stderr)
    return True


def output(result):
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("needs_update") is not None else 1)


def output_error(msg):
    print(json.dumps({"error": msg}, ensure_ascii=False))
    sys.exit(1)


def main():
    load_env()
    hub_url = get_hub_url()
    check_only = "--check" in sys.argv

    local_ver = get_local_version()

    try:
        latest = fetch_latest(hub_url)
    except URLError as e:
        output_error(f"无法连接 Hub: {e}")
    except Exception as e:
        output_error(f"获取版本信息失败: {e}")

    remote_ver = latest.get("version", "0.0.0")
    download_url = latest.get("download_url", "")
    changelog = latest.get("changelog", [])

    needs_update = parse_version(remote_ver) > parse_version(local_ver)

    if not needs_update or check_only:
        output({
            "needs_update": needs_update,
            "current_version": local_ver,
            "latest_version": remote_ver,
            "changelog": changelog,
        })

    if not download_url:
        output_error("Hub 返回的版本信息中缺少 download_url")

    tmpdir = tempfile.mkdtemp(prefix="imclaw-update-")
    tarball_path = os.path.join(tmpdir, "update.tar.gz")

    print(f"  下载 v{remote_ver} ...", file=sys.stderr)
    try:
        download_file(download_url, tarball_path)
    except Exception as e:
        output_error(f"下载失败: {e}")

    output({
        "needs_update": True,
        "current_version": local_ver,
        "latest_version": remote_ver,
        "changelog": changelog,
        "tarball_path": tarball_path,
    })


if __name__ == "__main__":
    main()
