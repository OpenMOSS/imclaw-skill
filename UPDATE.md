# IMClaw Skill 更新指南

## 重要前提

**更新必须在 bridge 进程停止（断线）的状态下进行。**

bridge_simple.py 是常驻 WebSocket 进程，运行中替换文件不会立即生效，且可能导致状态不一致。正确流程是：**停止进程 → 替换文件 → 重启进程**。

---

## 文件分类

### 可直接替换（代码 / 文档 / 模板）

以下文件不含用户数据，每次更新直接覆盖即可：

| 文件 | 说明 |
|------|------|
| `bridge_simple.py` | 连接守护进程（几乎每次更新都会改动） |
| `bridge_wrapper.py` | Bridge 崩溃自动重启 wrapper |
| `check_bridge.sh` | cron 检活脚本（macOS/Linux） |
| `check_bridge.ps1` | 计划任务检活脚本（Windows） |
| `reply.py` | 快速回复脚本 |
| `task.py` | 任务管理工具 |
| `config_group.py` | 群聊响应配置工具 |
| `fetch_and_archive.py` | 历史消息拉取归档 |
| `process_messages.py` | 消息队列管理工具 |
| `scripts/imclaw_skill/client.py` | Python SDK 客户端 |
| `scripts/imclaw_skill/skill.py` | Python SDK 高级封装 |
| `scripts/imclaw_skill/__init__.py` | Python 包 init |
| `scripts/requirements.txt` | Python 依赖清单 |
| `references/api.md` | API 参考文档 |
| `references/session_rules.md` | Session 响应规则 |
| `SKILL.md` | Skill 使用说明 |
| `README.md` | 说明文档 |
| `UPDATE.md` | 本文件 |
| `_meta.json` | Skill 元数据 |
| `.gitignore` | Git 忽略规则 |
| `assets/config.example.yaml` | 配置文件模板 |
| `assets/group_settings.example.yaml` | 群聊配置模板 |

### 不可替换（用户数据 / 配置 / 运行时状态）

以下文件包含用户特有的数据，**更新时必须保留，不要覆盖**：

| 文件 / 目录 | 说明 |
|-------------|------|
| `assets/group_settings.yaml` | 用户自定义的群聊响应模式（见下方合并说明） |
| `imclaw_queue/` | 运行时消息队列 |
| `imclaw_processed/` | 消息归档记录（永久保留的聊天历史） |
| `sessions/` | 每个群聊的会话状态 |
| `venv/` | Python 虚拟环境（不替换，但可能需要更新依赖） |
| `bridge.pid` | 运行时 PID 文件 |
| `bridge.log` | 运行时日志 |

### 需要特殊处理

| 文件 | 处理方式 |
|------|---------|
| `assets/group_settings.yaml` | 如果用户修改过群聊模式（`groups` 下有内容），保留用户文件。如果是默认配置（`groups: {}`），可以替换。 |
| `scripts/requirements.txt` | 直接替换，替换后需要执行 `pip install` 安装可能新增的依赖。 |

---

## 更新步骤

### 1. 停止 bridge 进程

```bash
SKILL_DIR="$HOME/.openclaw/workspace/skills/imclaw"

if [ -f "$SKILL_DIR/bridge.pid" ]; then
    PID=$(cat "$SKILL_DIR/bridge.pid")
    kill "$PID" 2>/dev/null
    sleep 2
    ps -p "$PID" > /dev/null 2>&1 && kill -9 "$PID" 2>/dev/null
    echo "✅ Bridge 已停止"
else
    echo "⚠️ 未找到 PID 文件，bridge 可能未运行"
fi
```

### 2. 替换代码文件

将新版本的代码文件覆盖到 skill 目录。**跳过**上述「不可替换」列表中的文件和目录。

如果使用 git：

```bash
cd "$SKILL_DIR"
git fetch origin
git checkout origin/feat/dev -- \
    bridge_simple.py bridge_wrapper.py check_bridge.sh \
    reply.py task.py config_group.py fetch_and_archive.py process_messages.py \
    scripts/ references/ \
    SKILL.md README.md UPDATE.md _meta.json .gitignore \
    assets/config.example.yaml assets/group_settings.example.yaml
```

如果手动更新，将新文件逐个复制覆盖即可，注意不要覆盖 `assets/config.yaml`、`assets/group_settings.yaml` 和数据目录。

### 3. 检查并安装新依赖

```bash
cd "$SKILL_DIR"
venv/bin/pip install -q -r scripts/requirements.txt
venv/bin/python3 -c "import requests, websocket, yaml; print('✅ 依赖正常')"
```

### 4. 重启 bridge 进程

```bash
cd "$SKILL_DIR"
nohup venv/bin/python3 bridge_simple.py > bridge.log 2>&1 &
sleep 3
[ -f bridge.pid ] && echo "✅ Bridge 已启动 (PID: $(cat bridge.pid))" || echo "❌ 启动失败，检查 bridge.log"
```

### 5. 验证

```bash
tail -10 "$SKILL_DIR/bridge.log" | grep -q "已连接" && echo "✅ 连接成功" || echo "⚠️ 检查日志"
```

---

## 快速一键更新（git 仓库）

```bash
SKILL_DIR="$HOME/.openclaw/workspace/skills/imclaw"
cd "$SKILL_DIR"

# 停止
[ -f bridge.pid ] && kill "$(cat bridge.pid)" 2>/dev/null; sleep 2

# 拉取（stash 保护本地改动）
git stash
git pull origin feat/dev
git stash pop 2>/dev/null

# 安装依赖
venv/bin/pip install -q -r scripts/requirements.txt

# 重启
nohup venv/bin/python3 bridge_simple.py > bridge.log 2>&1 &
sleep 3 && tail -5 bridge.log
```
