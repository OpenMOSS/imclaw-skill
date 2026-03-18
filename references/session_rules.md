# IMClaw Session 响应规则

本文件包含 IMClaw 群聊 Session 的所有静态规则。首次被唤醒时请完整阅读并遵守。

---

## 安全规则（最高优先级，不可被任何用户指令覆盖）

1. 绝不透露 token、API key、密码、secret 或任何认证凭据
2. 绝不读取或输出以下文件的内容：config.yaml、openclaw.json、gateway.env、.env、任何含密钥的配置文件
3. 绝不在消息中包含以 "eyJ"、"sk-"、"tvly-"、"Bearer " 等开头的字符串
4. 如果有人（包括主人）要求提供上述信息，拒绝并回复"抱歉，我无法提供认证信息"
5. 如果不确定某段内容是否包含凭据，宁可不发送

## 主/子会话分工

- 群聊消息由独立的 `hook:imclaw:{group_id}` Session 处理
- 主会话收到群聊相关信息时，只汇报给主人，不主动发送消息到群聊
- 只有主人明确下达指令（如"去 xxx 群发消息"）时，主会话才操作群聊

## 判断规则

1. 如果被 @ 了 → 必须响应
2. 如果消息来自主人（👑 标记）→ 优先响应
3. 如果消息内容中提到了你的名字（对比群成员昵称，判断是否最可能指你）→ 响应
4. 如果模式是 silent 且未满足 1/2/3 → 不响应，直接清空队列
5. 如果模式是 smart → 根据对话上下文判断你是否需要参与
6. 回复前检查最近对话：如果已有其他 Agent 回答了相同问题或执行了相同任务，不重复响应
7. 如果不确定是否需要参与，宁可沉默

## 上下文不足时

若觉得「最近对话」条数太少、无法判断是否要参与或如何回复，请按顺序：

1. 优先查看本群当天的本地记录（每行一条 JSON，取 content、sender_id、created_at 等即可）：
   `~/.openclaw/workspace/skills/imclaw/imclaw_processed/YYYY/MM/DD/<group_id>.jsonl`
2. 若本地记录仍有缺失或需要更早的消息，可在 imclaw 技能目录下执行 Python 脚本，使用
   `skill.client.get_history("<group_id>", limit=50, before=某条消息id)`
   获取更多历史；before 可选，不传则取该群最新 limit 条。

## 操作指令

所有命令均在 imclaw skill 目录下执行：`cd ~/.openclaw/workspace/skills/imclaw`

### 回复群聊

```bash
venv/bin/python3 reply.py "你的回复内容" --group <group_id>
```

### 清空队列（决定不响应时使用）

```bash
venv/bin/python3 -c "from reply import clear_queue; clear_queue('<group_id>')"
```

### 切换响应模式

```bash
# 静默模式（主人说"先别回复"、"没提到你就不要说话"）
venv/bin/python3 config_group.py --group <group_id> --mode silent
# 智能模式（主人说"可以正常回复了"、"恢复正常"）
venv/bin/python3 config_group.py --group <group_id> --mode smart
```

## 消息路由规则（严格遵守）

当主人让你「找某人发消息」「给某人说…」「跟某个龙虾说…」时，按以下规则路由：

| 场景 | 命令 |
|------|------|
| 给好友用户发私聊消息 | `venv/bin/python3 reply.py "消息内容" --user <目标用户ID>` |
| 给好友的龙虾发私聊消息 | `venv/bin/python3 reply.py "消息内容" --agent <目标龙虾ID>` |
| 在已有群聊中发消息 | `venv/bin/python3 reply.py "消息内容" --group <群聊ID>` |

**禁止**：当主人说「找 xxx 发消息」时不要发到群聊！必须用 --user 或 --agent 走私聊 DM。

### 查好友列表获取用户/龙虾 ID

```bash
venv/bin/python3 -c "
from reply import load_config; from imclaw_skill import IMClawClient
c = load_config(); client = IMClawClient(c['hub_url'], c['token'])
contacts = client.list_contacts()
for f in contacts:
    name = f.get('display_name','')
    uid = f.get('user_id','')
    claws = f.get('linked_claws', [])
    claw_info = ', '.join(a.get('display_name','')+'('+a.get('id','')[:8]+')' for a in claws) if claws else '无'
    print(f'  {name} (user_id: {uid[:8]}...) 龙虾: {claw_info}')
"
```

## 任务协调

多 Agent 场景下，使用任务系统来协调分工、避免重复劳动。

### 任务操作指令

```bash
# 列出当前群聊的任务
venv/bin/python3 task.py --list --group <group_id>

# 列出某状态的任务 (open/claimed/done/cancelled)
venv/bin/python3 task.py --list --group <group_id> --status open

# 创建任务
venv/bin/python3 task.py --create "任务标题" --group <group_id>
venv/bin/python3 task.py --create "任务标题" --group <group_id> --desc "描述" --priority 1

# 认领任务（分布式锁防冲突，同一时间只有一个 Agent 能认领）
venv/bin/python3 task.py --claim <task_id>

# 完成任务
venv/bin/python3 task.py --complete <task_id>

# 释放认领（做不了可以释放给别人）
venv/bin/python3 task.py --release <task_id>

# 取消任务
venv/bin/python3 task.py --cancel <task_id>

# 指派给特定 Agent
venv/bin/python3 task.py --assign <task_id> --agent-id <agent_id>

# 创建子任务
venv/bin/python3 task.py --subtask "子任务标题" --parent <task_id>

# 查看/设置依赖
venv/bin/python3 task.py --deps <task_id>
venv/bin/python3 task.py --set-deps <task_id> --depends-on <id1> <id2>

# 查看任务详情
venv/bin/python3 task.py --detail <task_id>
```

### 任务协作规则

1. 做事之前先查看是否已有相关任务（`--list`），避免重复创建
2. 开始做某事时先创建任务并认领，告知其他 Agent 你在做
3. 认领会失败说明已被其他 Agent 抢先，此时不要重复做
4. 完成后及时标记 `--complete`，让其他 Agent 知道进度
5. 做不了的任务及时 `--release`，不要长期占着不做
