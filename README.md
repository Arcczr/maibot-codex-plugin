# 麦麦掌握 Codex CLI 

> [!NOTE]
>
> **免责声明**
> 本插件代码由 **GPT-5.5** 进行编写。作者不对因使用本插件或其生成内容所导致的任何直接或间接的问题、损失或纠纷承担任何责任。**请用户自行评估风险并谨慎使用**。

这个插件用于让 MaiBot 在 QQ 聊天中接收 `/codex` 指令，并在 MaiBot 所在设备上启动 Codex CLI 执行任务。插件会把任务进度、最终摘要和生成的文件产物回传到当前聊天流，并可对接 NapCat 或 SnowLuma 适配器的公开 API 做增强发送。

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

### 1. 从插件商店安装

在 MaiBot 插件商店中安装本插件后，确认插件目录存在：

```text
MaiBot/plugins/remote_codex_agent/
```

如果你是手动部署，目录第一层应直接包含 `plugin.py`、`config.toml`、`_manifest.json` 和 `README.md`。

### 2. 确认依赖

插件依赖 `httpx`。插件商店通常会按 manifest 安装依赖；如果启动日志提示找不到 `httpx`，在 MaiBot 根目录执行：

```bash
uv add httpx
```

或用你实际启动 MaiBot 的 Python 环境安装 `httpx`。

### 3. 准备 Codex CLI

插件只负责调用本机 `codex` 命令，不负责安装、登录或配置 Codex CLI。请在运行 MaiBot 的同一个系统用户下确认：

Linux / macOS：

```bash
command -v codex
codex --version
codex -a never exec --json --color never -s workspace-write --skip-git-repo-check -C /tmp "用中文回复：测试成功"
```

Windows PowerShell：

```powershell
where codex
codex --version
codex -a never exec --json --color never -s workspace-write --skip-git-repo-check -C "$env:TEMP" "用中文回复：测试成功"
```

如果这些命令失败，先解决 Codex CLI 的安装、登录、网络、模型权限或本机配置问题。

### 4. 修改插件配置

可以在 MaiBot Web UI 的插件配置页修改，也可以编辑：

```text
MaiBot/plugins/remote_codex_agent/config.toml
```

最少需要检查这些项：

```toml
[plugin]
enabled = true

[local_codex]
codex_binary = "codex"
work_root = "data/tasks"
sandbox = "workspace-write"
approval_policy = "never"
pass_env_vars = []

[permission]
allow_all_users = false
user_list_mode = "whitelist"
trigger_users = ["qq:你的QQ号"]
admin_users = ["qq:你的QQ号"]

[task]
execution_mode = "local"
```

如果使用 SnowLuma 适配器，并希望 `/codex --dm` 私聊进度和回复消息追溯走 SnowLuma 的 NapCat 兼容 API，可额外开启：

```toml
[snowluma]
enabled = true
send_artifacts_as_file_segments = true
```

`[napcat].enabled` 和 `[snowluma].enabled` 必须二选一。插件不会在 SnowLuma 模式下调用 NapCat 作为兜底。

如果只想先跑通测试，可以临时设置：

```toml
[permission]
allow_all_users = true
```

生产环境不建议长期允许所有用户触发。

如果 MaiBot 启动环境找不到 `codex`，把 `codex_binary` 改成绝对路径：

```toml
[local_codex]
# Ubuntu/Linux 示例
codex_binary = "/root/.local/bin/codex"
```

```toml
[local_codex]
# Windows 示例
codex_binary = "C:\\Users\\你的用户名\\AppData\\Roaming\\npm\\codex.cmd"
```

```toml
[local_codex]
# Windows 也可以用正斜杠
codex_binary = "C:/Users/你的用户名/AppData/Roaming/npm/codex.cmd"
```

### 5. 重启 MaiBot

改完配置后需要重启 MaiBot。无论使用 `uv run bot.py`、宝塔、systemd、Docker 还是 nohup，都要确保重启后的 MaiBot 使用的是你刚才测试过 `codex` 的同一个用户环境。

### 6. 在 QQ 中测试

发送：

```text
/codex 用中文回复“任务创建成功”，并生成一个 txt 文件放到 artifacts 目录
```

正常情况下，麦麦会先返回任务 ID，随后返回进度、最终摘要和产物信息。

### 常见启动报错

`[WinError 2] 系统找不到指定的文件。`

Windows 找不到 `codex` 可执行文件。用 PowerShell 执行 `where codex`，然后把 `codex.cmd` 的完整路径写入 `local_codex.codex_binary`。

`[Errno 2] No such file or directory: 'codex'`

Linux 找不到 `codex`。用 `command -v codex` 查询完整路径，然后写入 `local_codex.codex_binary`。如果终端能找到但插件找不到，通常是 MaiBot 的启动环境和当前终端 PATH 不一样。

`ModuleNotFoundError: No module named 'httpx'`

插件依赖没装进 MaiBot 当前 Python 环境。在 MaiBot 根目录执行 `uv add httpx`，或按你的启动环境安装 `httpx`。

`Codex 可以在终端运行，但插件任务失败`

检查 MaiBot 启动用户是否和你测试 Codex 的用户一致。宝塔、systemd、Docker、nohup、VS Code 终端可能使用不同的 PATH、HOME、CODEX_HOME 和登录状态。

`Codex CLI 读取不到 API key`

插件默认只给 Codex 子进程传递最小环境变量。确实需要传递 API key 时，在 `[local_codex] pass_env_vars` 中显式加入变量名，例如 `["OPENAI_API_KEY"]`。这些变量会被 Codex、skill 和 MCP 读取，存在泄露风险，只给可信环境使用。

`麦麦说没有 /codex 技能包`

插件没有加载成功。检查插件是否启用、目录结构是否正确、MaiBot 日志里是否有 manifest 或依赖错误。

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
/codex list all
```

会显示当前用户在当前聊天流里最近的 task 和 session 记录。
`/codex list all` 仅管理员可用，会查看所有聊天流的记录。

### 清理记录和文件

```text
/codex clean
/codex clean input
/codex clean task <task_id>
/codex clean session <session名>
/codex clean session <session名> confirm
```

这些命令仅管理员可用。

- `/codex clean` 清理过期普通 task 记录，并顺带清理过期输入材料。
- `/codex clean input` 只清理过期输入材料。
- `/codex clean task <task_id>` 删除指定 task 的记录和本地文件目录。
- `/codex clean session <session名>` 先显示删除影响，不会立即删除。
- `/codex clean session <session名> confirm` 确认删除 session 记录和关联 task 文件。

## Task 和 Session

### Task

默认 `/codex <任务描述>` 创建的是一次性 task。

特点：

- 适合单次任务。
- 会保存一段时间，可通过 `resume` 短时间继续。
- 超过 `task.resumable_task_ttl_hours` 后，插件可在启动时或定时清理时清理 task 记录。
- 默认只清理插件记录，不删除产物目录；开启 `auto_cleanup_task_workspaces` 后才会一起删除 task workspace。

相关配置：

```toml
[task]
resumable_task_ttl_hours = 24.0
auto_cleanup_task_records = true
auto_cleanup_task_workspaces = false
enable_periodic_cleanup = false
periodic_cleanup_interval_minutes = 60.0
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
- session 不会自动过期；需要管理员用 `/codex clean session <session名>` 并二次确认删除。

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
auto_cleanup_input_files = true
input_file_ttl_hours = 24.0
allow_url_download = true
download_timeout_seconds = 120.0
allowed_local_roots = []
```

注意：

- 插件不会自动猜“最近上传的文件”，必须回复具体文件消息。
- Word、PPT、Excel、PDF、Markdown、TXT、ZIP 等文件都可以作为材料传入，最终能否正确读取取决于 Codex 环境中可用的解析工具。
- NapCat 模式下，如果 QQ 文件消息只有 `file_id`，插件会调用 NapCat Adapter 的 `get_file` / `get_group_file_url` / `get_private_file_url` 补全路径或下载地址。
- SnowLuma 模式下，入站 file 段若已经带 `url` 或 `path`，插件可以直接导入；若只有 `file_id`，当前 SnowLuma Adapter 没有公开等价文件补全 API，插件不会调用 NapCat 兜底。要实现这一点，需要 SnowLuma 的 `get_file`、`get_group_file_url`、`get_private_file_url` 或等价动作文档。
- 如果 MaiBot 和当前 QQ 适配器不在同一个文件系统里，需要用共享卷或 URL 方式让 MaiBot 读到文件。
- 输入材料会放在 `workspace/input/`，可按 `input_file_ttl_hours` 自动清理；清理输入材料不会删除产物、日志或 task 记录。

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

只要文件被 `artifact_globs` 匹配到，插件就会在任务完成时把它列入产物。启用 NapCat 时，插件走 NapCat 文件上传 API；启用 SnowLuma 时，插件走 SnowLuma 兼容发送 API 的 OneBot `file` 段。两条路径互相独立，不会跨适配器兜底。

## NapCat 直传文件

如果只使用默认产物列表，群里只会看到文件名和大小。要让 QQ 群直接收到文件，可启用 NapCat Adapter API 直传。

### 前置条件

MaiBot 需要加载 NapCat Adapter。

如果 MaiBot 和 NapCat 不在同一个文件系统里，产物路径必须是 NapCat 进程也能读取的路径，容器部署时通常需要共享卷。

### 插件侧配置

```toml
[napcat]
enabled = true
upload_file = true
max_file_size_mb = 100.0
```

插件会按聊天类型调用：

- 群聊：`upload_group_file`
- 私聊：`upload_private_file`

## SnowLuma 适配

SnowLuma 适配器 0.7.x 提供了一部分 `adapter.napcat.*` 兼容 API。本插件会在 `[snowluma] enabled = true` 时只使用 SnowLuma 暴露的这些兼容 API：

- `/codex --dm` 阶段性进度私聊：调用 `adapter.napcat.message.send_private_msg`
- 回复消息追溯：调用 `adapter.napcat.message.get_msg`
- 回复文件作为输入：如果 SnowLuma 入站 file 段带有 `url`，插件会按 URL 下载
- 产物文件回传：调用 `adapter.napcat.message.send_group_msg` / `send_private_msg` 发送 OneBot `file` 段

配置示例：

```toml
[snowluma]
enabled = true
send_artifacts_as_file_segments = true
max_file_size_mb = 100.0
```

如果 SnowLuma 或 QQ 端不支持 OneBot `file` 段，插件会报告 SnowLuma 文件段发送失败，并保留产物列表；不会改用 NapCat。

SnowLuma 目前无法独立补全“只有 `file_id`、没有 `url/path`”的文件消息。本插件配置中有 `unsupported_file_id_note` 提醒这一限制。要补齐这项能力，需要 SnowLuma 提供 `file_id + group_id/user_id -> url/path` 的公开 API 文档。

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

会显示插件当前使用的本机 Codex 配置，包括模型、沙箱、审批策略、联网搜索、进度转发和当前启用的 QQ 高级适配器状态。

相关配置：

```toml
[local_codex]
model = ""
enable_search = false
sandbox = "workspace-write"
approval_policy = "never"
extra_args = []
pass_env_vars = []
```

说明：

- `model = ""` 表示使用本机 Codex CLI 默认模型配置。
- `enable_search = true` 时，插件会给 Codex CLI 传递 `--search`。
- `sandbox = "danger-full-access"` 属于高危配置，只建议管理员明确需要时使用。
- `pass_env_vars` 默认空列表。插件只给 Codex 子进程传递最小运行环境；如果把 `OPENAI_API_KEY`、`*_TOKEN`、`*_SECRET` 等变量加入这里，Codex、skill 和 MCP 都可能读取这些值。

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
  -C data/tasks/<task_id>/workspace \
  --skip-git-repo-check \
  --output-last-message data/tasks/<task_id>/final.md \
  -
```

插件会把构造好的 prompt 从 stdin 传给 Codex CLI。

每个任务目录类似：

```text
data/tasks/<task_id>/
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
data/tasks/_records/
```


## 运行时清理策略

普通 task、输入材料和 session 的清理策略不同：

- 普通 task 记录可按 `task.resumable_task_ttl_hours` 过期清理。
- `task.auto_cleanup_task_records = true` 时，插件启动会自动清理过期普通 task 记录。
- `task.enable_periodic_cleanup = true` 时，长时间不重启 MaiBot 也会按间隔定时清理。
- `task.auto_cleanup_task_workspaces = false` 是默认值，表示自动清理 task 记录时不删除 workspace 和产物。
- `input_file.auto_cleanup_input_files = true` 时，过期输入材料会按 `input_file.input_file_ttl_hours` 清理。
- 输入材料清理只删除 `workspace/input/`，不会删除 `workspace/artifacts/`、日志或 task 记录。
- session 不自动清理，需要管理员手动 `/codex clean session <session名>` 并按提示确认。

## 远程模式

#### 注意：以下内容未进行调试和验证，不确保其可用性

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

如果报错时提示：Missing environment variable:

检查配置项`pass_env_vars`,填入该报错提示的环境变量名称。

### 宝塔启动能跑 MaiBot，但 Codex 不工作

宝塔、systemd、SSH、VS Code 终端可能使用不同的环境变量和用户上下文。重点检查：

- `which codex`
- `codex --version`
- `echo $HOME`
- `echo $CODEX_HOME`
- MaiBot 启动用户是否能读取 Codex 登录状态

如启动环境找不到 codex（windows:[WinError 2] 系统找不到指定的文件。linux:[Errno 2] No such file or directory: codex）,在 `local_codex.codex_binary` 填绝对路径：
  Ubuntu/Linux 可用 `/root/.local/bin/codex` 或 `/usr/local/bin/codex`；
  Windows 可用 `C:\Users\你的用户名\AppData\Roaming\npm\codex.cmd`。

### NapCat 上传失败

检查：

- MaiBot 是否加载了 NapCat Adapter。
- manifest 是否声明了 `api.call` 能力。
- `napcat.upload_file = true`。
- 产物文件路径是否是 NapCat 进程能读取的本机路径或共享卷路径。
- 文件大小是否超过 `napcat.max_file_size_mb`。

### SnowLuma 私聊进度或文件段失败

检查：

- MaiBot 是否加载并启用了 SnowLuma Adapter。
- 本插件 `[snowluma] enabled` 是否为 `true`。
- SnowLuma 是否已经连接成功，且能正常发送普通消息。
- 私聊进度通常要求触发用户先主动私聊机器人一次。
- 文件段回传失败时，可关闭 `snowluma.send_artifacts_as_file_segments`，使用默认产物列表；插件不会改用 NapCat 兜底。

### 回复文件没有被读取

检查：

- 是否是“回复文件消息”发送 `/codex`，不是单独发送 `/codex`。
- `input_file.enable_reply_file = true`。
- NapCat 模式下，如果文件消息只有 `file_id`，检查 `napcat.enabled` 是否开启、NapCat Adapter 文件 API 是否可用。
- SnowLuma 模式下，如果文件消息只有 `file_id`，当前缺少 SnowLuma 文件补全 API；需要补充 `get_file` / `get_group_file_url` / `get_private_file_url` 或等价动作文档。
- MaiBot 是否有权限读取当前适配器返回的本地文件路径。
- 容器部署时是否配置了共享卷。

## 安全建议

- 不要长期对所有用户开放，优先使用 `allowed_users` 和 `allowed_groups`。
- 默认使用 `workspace-write` 沙箱，不要随意开放 `danger-full-access`。
- `work_root` 默认使用插件目录的 `data/tasks`；不建议改到默认目录以外，可能存在安全和误删风险，除非你知道自己在做什么。
- 控制任务超时、文件大小、进度长度和摘要长度。
- 用户上传的文件可能包含隐私或敏感信息，只允许可信用户触发。
- 不要把完整群聊历史默认交给 Codex。
- 不要随意把 API key、token、secret 加入 `local_codex.pass_env_vars`；这些变量会暴露给 Codex、skill 和 MCP。
- 如果使用 `remote` 模式，务必启用鉴权，并保持 `server.require_api_token = true`。
