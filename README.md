# 麦麦掌握 Codex CLI 

> [!NOTE]
>
> **免责声明**
> 本插件代码由 **Codex** 进行编写。作者不对因使用本插件或其生成内容所导致的任何直接或间接的问题、损失或纠纷承担任何责任。**请用户自行评估风险并谨慎使用**。

这个插件用于让 MaiBot 在 QQ 聊天中接收 `/codex` 指令，并在 MaiBot 所在设备上启动 Codex CLI 执行任务。插件会把任务进度、最终摘要和生成的文件产物回传到当前聊天流。

默认推荐部署方式是 `local` 模式：

```text
QQ 用户发送 /codex
 -> MaiBot 插件创建任务目录
 -> 插件启动本机 Codex CLI
 -> Codex 在 workspace 中处理任务并生成产物
 -> 插件把摘要和产物回传到 QQ
```

插件也保留 `remote` 模式，适合后续把 Codex 执行器拆成独立 HTTP Agent 服务。

## 快速开始

### 1. 放置插件

进入 MaiBot 插件目录：

```bash
cd /root/functional_project/maimai/MaiBot/plugins
```

把本仓库放到 `remote_codex_agent` 目录：

```bash
git clone https://github.com/Arcczr/maibot-codex-plugin.git remote_codex_agent
```

最终目录应类似：

```text
MaiBot/
  plugins/
    remote_codex_agent/
      plugin.py
      config.toml
      _manifest.json
      README.md
```

### 2. 安装插件依赖

插件依赖 `httpx`。如果 MaiBot 使用 `uv` 启动，建议在 MaiBot 根目录执行：

```bash
cd /root/functional_project/maimai/MaiBot
uv add httpx
```

如果你的 MaiBot 项目不希望修改依赖文件，也可以按你当前环境的方式安装 `httpx`，只要启动 MaiBot 的 Python 环境能 import `httpx` 即可。

### 3. 准备 Codex CLI(若已有则忽略)

在运行 MaiBot 的同一个系统用户下安装并登录 Codex CLI。

先确认 MaiBot 启动用户能执行：

```bash
codex --version
```

再做一次最小执行测试：

```bash
codex -a never exec --json --color never -s workspace-write --skip-git-repo-check -C /tmp "用中文回复：测试成功"
```

如果这一步失败，先解决 Codex CLI 的安装、登录、网络或权限问题。插件只是调用本机 `codex` 命令，不负责登录 Codex 账号。

### 4. 修改配置

编辑：

```bash
/root/functional_project/maimai/MaiBot/plugins/remote_codex_agent/config.toml
```

最小可用配置：

```toml
[plugin]
enabled = true

[permission]
allow_all_users = false(一定要检查一遍这一项)
allowed_users = ["qq:你的QQ号"]
allowed_groups = []

[task]
execution_mode = "local"

[local_codex]
codex_binary = "codex"
work_root = "data/remote_codex_agent/tasks"
sandbox = "workspace-write"
approval_policy = "never"
```

如果只是自己测试，也可以临时：

```toml
[permission]
allow_all_users = true
```

生产环境不建议长期开放给所有人。

### 5. 启动或重启 MaiBot

在 MaiBot 根目录启动：

```bash
cd /root/functional_project/maimai/MaiBot
uv run bot.py
```

如果你使用宝塔、systemd、Docker 或其他方式启动 MaiBot，需要确保启动用户和你测试 `codex` 的用户一致，或者至少能访问同一个 Codex 登录状态和配置目录。

### 6. 在 QQ 中测试

发送：

```text
/codex 用中文回复“任务创建成功”，并生成一个 txt 文件放到 artifacts 目录
```

正常情况下，麦麦会先返回任务 ID，随后返回进度、最终摘要和产物信息。

## 常用命令

插件固定支持两个命令前缀：

```text
/codex
/agent
```

`config.toml` 中的 `task.command_prefix` 只影响帮助文本展示，不改变实际可触发的前缀。

### 基础任务

```text
/codex <任务描述>
```

创建一次性 task。适合单次处理、生成文件、总结材料等任务。

示例：

```text
/codex 生成一份关于本周工作安排的 Word 文档
```

### 查看帮助

```text
/codex help
```

### 查看运行中任务

```text
/codex status
/codex status <task_id>
```

### 取消任务

```text
/codex cancel <task_id>
```

### 查看历史记录

```text
/codex list
```

会显示当前用户在当前聊天流里最近的 task 和 session 记录。

## Task 和 Session

### Task

默认 `/codex <任务描述>` 创建的是一次性 task。

特点：

- 适合单次任务。
- 会保存一段时间，可通过 `resume` 短时间继续。
- 超过 `task.resumable_task_ttl_hours` 后，插件重启时会清理 task 记录。
- 清理的是插件记录，不会自动删除已经生成的产物目录。

相关配置：

```toml
[task]
resumable_task_ttl_hours = 24.0
```

### Session

session 是持久会话，适合连续多轮处理同一个主题。

创建 session：

```text
/codex session <会话名> <任务描述>
```

继续当前用户最近的 session：

```text
/codex continue <继续处理的要求>
```

恢复指定 task、session 或 Codex thread：

```text
/codex resume <task_id|session名|thread_id> <继续处理的要求>
```

把已有 task 转为 session：

```text
/codex session <task_id> confirm [会话名]
```

说明：

- `continue` 会按用户区分，只继续当前用户最近的 session。
- `resume` 可以指定具体 task、session 名或 Codex thread_id。
- session 会保存历史 task 列表，`/codex list` 可以看到这个 session 下最近做过的任务。

## 回复文件作为输入材料

插件支持“回复 QQ 文件消息 + `/codex` 指令”的稳定交互。

推荐用法：

```text
用户先上传 report.docx
用户回复这条文件消息：/codex 阅读这个文档，提取重点并生成一份总结
```

插件会尝试：

```text
读取被回复消息
 -> 找到文件段
 -> 复制或下载文件
 -> 放入本次任务 workspace/input/
 -> 在 prompt 中告诉 Codex 优先读取 input 目录
```

相关配置：

```toml
[input_file]
enable_reply_file = true
input_dir_name = "input"
max_files_per_task = 5
max_file_size_mb = 100.0
allow_url_download = true
allowed_local_roots = []
```

注意：

- 插件不会自动猜“最近上传的文件”，必须回复具体文件消息。
- Word、PPT、Excel、PDF、Markdown、TXT、ZIP 等文件都可以作为材料传入，最终能否正确读取取决于 Codex 环境中可用的解析工具。
- 如果 QQ 文件消息只有 `file_id`，通常需要配置 NapCat HTTP API，让插件调用 `get_file` 获取文件路径或下载地址。
- 如果 MaiBot 和 NapCat 不在同一个文件系统里，需要用共享卷或 URL 方式让 MaiBot 读到文件。

## 产物生成和回传

插件会扫描本次任务 workspace 中匹配 `local_codex.artifact_globs` 的文件。

默认配置：

```toml
[local_codex]
artifact_globs = ["artifacts/*", "*.docx", "*.pdf", "*.md", "*.zip", "*.xlsx", "*.pptx"]
```

建议让 Codex 把最终产物放到：

```text
workspace/artifacts/
```

例如：

```text
workspace/artifacts/report.docx
workspace/artifacts/summary.md
workspace/artifacts/slides.pptx
```

只要文件被 `artifact_globs` 匹配到，插件就会在任务完成时把它列入产物。启用 NapCat 直传后，插件还会尝试把这些文件直接发回 QQ。

## NapCat 直传文件

如果只使用默认产物链接，群里看到的可能是服务器本地路径。要让 QQ 群直接收到文件，建议启用 NapCat HTTP API 直传。

### NapCat 侧配置

在 NapCat WebUI 中为当前 bot 新增 HTTP Server：

```text
host: 127.0.0.1
port: 9998
token: 可留空，也可以自行设置
```

如果 MaiBot 和 NapCat 不在同一台机器，`host` 要填 MaiBot 能访问到的 NapCat 地址，并确保端口、防火墙和容器网络可通。

### 插件侧配置

```toml
[napcat]
enabled = true
scheme = "http"
host = "127.0.0.1"
port = 9998
token = ""
request_timeout_seconds = 120.0
upload_file = true
max_file_size_mb = 100.0
```

插件会按聊天类型调用：

- 群聊：`upload_group_file`
- 私聊：`upload_private_file`

## Skills、MCP 和 Codex 配置

### 查看 Skills

```text
/codex skills
```

插件会扫描本机 `CODEX_HOME/skills` 下的 `SKILL.md`，返回可用 skill 和简短描述。

如果用户的任务描述自然触发了某个 skill，通常不需要额外写“指定 skill”的命令。比如已经安装了生成 PPT 的 skill，用户直接说“请用生成 PPT 的方式做一个演示文稿”，Codex 会按本机 Codex 的 skill 规则处理。

### 查看 MCP

```text
/codex mcp
```

插件会调用：

```bash
codex mcp list --json
```

并把当前 Codex 可见的 MCP server 列出来。

### 查看模型和运行配置

```text
/codex config
```

会显示插件当前使用的本机 Codex 配置，包括模型、沙箱、审批策略、联网搜索、进度转发和 NapCat 直传状态。

相关配置：

```toml
[local_codex]
model = ""
enable_search = false
sandbox = "workspace-write"
approval_policy = "never"
extra_args = []
```

说明：

- `model = ""` 表示使用本机 Codex CLI 默认模型配置。
- `enable_search = true` 时，插件会给 Codex CLI 传递 `--search`。
- `sandbox = "danger-full-access"` 属于高危配置，只建议管理员明确需要时使用。

## 权限配置

推荐至少限制用户，必要时再限制群。

```toml
[permission]
allow_all_users = false
allowed_users = ["qq:123456789"]
admin_users = ["qq:123456789"]
allowed_groups = ["qq:987654321"]
```

字段说明：

- `allow_all_users`: 是否允许所有用户触发。
- `allowed_users`: 允许触发的用户，推荐格式 `qq:用户ID`。
- `admin_users`: 管理员用户，可使用高危权限相关能力。
- `allowed_groups`: 允许触发的群号、`qq:群号` 或 MaiBot stream_id；空列表表示不限制聊天流。

## 本机执行细节

本机 local 模式下，插件大致执行：

```bash
codex -a never exec \
  --json \
  --color never \
  -s workspace-write \
  -C data/remote_codex_agent/tasks/<task_id>/workspace \
  --skip-git-repo-check \
  --output-last-message data/remote_codex_agent/tasks/<task_id>/final.md \
  -
```

插件会把构造好的 prompt 从 stdin 传给 Codex CLI。

每个任务目录类似：

```text
data/remote_codex_agent/tasks/<task_id>/
  prompt.md
  stdout.jsonl
  stderr.log
  final.md
  workspace/
    input/
    artifacts/
```

持久记录在：

```text
data/remote_codex_agent/tasks/_records/
```

这些运行时目录不应该提交到 Git。

## 远程模式

如果你把配置改成：

```toml
[task]
execution_mode = "remote"
```

插件会调用 `[server]` 中配置的 HTTP Agent，而不是直接启动本机 Codex CLI。

### 创建任务

```http
POST /v1/tasks
Authorization: Bearer <api_token>
Content-Type: application/json
```

响应至少包含：

```json
{
  "task_id": "task_123",
  "status": "queued",
  "message": "任务已创建"
}
```

### 查询任务

```http
GET /v1/tasks/{task_id}
Authorization: Bearer <api_token>
```

运行中响应示例：

```json
{
  "task_id": "task_123",
  "status": "running",
  "progress": [
    {"id": "1", "text": "正在分析需求"}
  ],
  "summary": "",
  "artifacts": []
}
```

完成响应示例：

```json
{
  "task_id": "task_123",
  "status": "succeeded",
  "summary": "任务完成。",
  "artifacts": [
    {
      "name": "report.docx",
      "url": "https://agent.example.com/artifacts/task_123/report.docx",
      "size": 123456
    }
  ]
}
```

支持状态：

```text
queued
running
succeeded
failed
cancelled
```

插件也兼容 `completed`、`done`、`success`、`error` 等常见别名。

## 常见问题

### 麦麦说没有 `/codex` 技能包

通常是插件没有加载成功。检查：

- 插件目录是否叫 `remote_codex_agent`。
- `plugin.py`、`config.toml`、`_manifest.json` 是否在插件目录第一层。
- `config.toml` 中 `plugin.enabled = true`。
- MaiBot 启动日志里是否有插件加载错误。

### 创建任务失败

先在 MaiBot 启动用户下手动执行：

```bash
codex -a never exec --json --color never -s workspace-write --skip-git-repo-check -C /tmp "你好"
```

如果手动命令失败，优先处理 Codex CLI 安装、登录、网络、模型权限或本机配置问题。

### 宝塔启动能跑 MaiBot，但 Codex 不工作

宝塔、systemd、SSH、VS Code 终端可能使用不同的环境变量和用户上下文。重点检查：

- `which codex`
- `codex --version`
- `echo $HOME`
- `echo $CODEX_HOME`
- MaiBot 启动用户是否能读取 Codex 登录状态
- `local_codex.env_file` 是否需要补充环境变量

### NapCat 上传失败

检查：

- NapCat HTTP Server 是否开启。
- `host`、`port`、`token` 是否和插件配置一致。
- MaiBot 机器是否能访问 NapCat 端口。
- `napcat.upload_file = true`。
- 产物文件路径是否是 NapCat 能读取的本机路径。
- 文件大小是否超过 `napcat.max_file_size_mb`。

### 回复文件没有被读取

检查：

- 是否是“回复文件消息”发送 `/codex`，不是单独发送 `/codex`。
- `input_file.enable_reply_file = true`。
- 如果文件消息只有 `file_id`，NapCat HTTP API 是否可用。
- MaiBot 是否有权限读取 NapCat 返回的本地文件路径。
- 容器部署时是否配置了共享卷。

## 安全建议

- 不要长期对所有用户开放，优先使用 `allowed_users` 和 `allowed_groups`。
- 默认使用 `workspace-write` 沙箱，不要随意开放 `danger-full-access`。
- `work_root` 使用专用目录，不要指向 MaiBot 主仓库或系统关键目录。
- 控制任务超时、文件大小、进度长度和摘要长度。
- 用户上传的文件可能包含隐私或敏感信息，只允许可信用户触发。
- 不要把完整群聊历史默认交给 Codex。
- 如果使用 `remote` 模式，务必启用鉴权，并保持 `server.require_api_token = true`。
