# 麦麦掌握 Codex CLI

> [!NOTE]
>
> **声明**
> 本插件代码由 **GPT-5.5** 进行编写。经测试其功能完整，已确定其能力边界。其在使用过程中不会产生安全问题，如有介意，请避免使用喵~

本插件用于让 MaiBot 在 QQ 聊天中接收 `/codex` 指令，并在 MaiBot 所在设备上启动 Codex CLI 执行任务。插件会把任务进度、最终摘要和生成的文件产物回传到当前聊天流，并可对接 NapCat 或 SnowLuma 适配器的公开 API 做增强发送。

默认推荐部署方式是 `local` 模式：

```text
QQ 用户发送 /codex
 -> MaiBot 插件创建任务目录
 -> 插件启动本机 Codex CLI
 -> Codex 在 workspace 中处理任务并生成产物
 -> 插件把摘要和产物回传到 QQ
```

插件也保留 `remote` 模式，可以调用非本机 Codex CLI（通过 HTTP Agent 服务）。

---

## 快速开始

### 1. 从插件商店安装

在 MaiBot 插件商店中安装本插件后，确认插件目录存在：

```text
MaiBot/plugins/remote_codex_agent/
```

如果你是手动部署，目录第一层应直接包含 `plugin.py`、`config.toml`、`_manifest.json` 和 `README.md`。

### 2. 第一次安装必须配置

在启动插件之前，你需要完成以下**最小必要配置**，否则插件无法正常工作：

- **配置环境变量**（供 Codex CLI 使用）：  
  插件默认不会把任何环境变量传给 Codex 子进程。如果你依赖 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 或类似的认证变量，**必须**在 `config.toml` 的 `[local_codex]` 中显式加入：

  ```toml
  pass_env_vars = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]   # 按需添加
  ```

  这样 Codex、Skills 和 MCP 才能读取到这些变量。  
  > ⚠️ 注意：这些变量会被传递给子进程，存在泄露风险，请仅在可信环境中使用。

- **确保 `codex` 命令可用**：  
  在运行 MaiBot 的同一个系统用户下，终端执行 `codex --version` 应正常返回版本信息。若找不到命令，请在配置中指定 `codex_binary` 的绝对路径（见下文“启动与常见问题”）。

- **设置触发权限**：  
  首次测试建议临时开放所有用户：

  ```toml
  [permission]
  allow_all_users = true
  ```

  生产环境务必限制为你的 QQ 号：

  ```toml
  allow_all_users = false
  user_list_mode = "whitelist"
  trigger_users = ["qq:你的QQ号"]
  admin_users = ["qq:你的QQ号"]
  ```

- **（可选）选择高级适配器**：  
  若要支持文件上传或私聊进度，请参考后续“NapCat 直传文件”或“SnowLuma 适配”章节配置相应 `[napcat]` 或 `[snowluma]` 块。

### 3. 配置项概述

插件的主要配置集中在 `MaiBot/plugins/remote_codex_agent/config.toml`。核心配置组包括：

- `[plugin]` – 插件开关（`enabled`）
- `[local_codex]` – 本机 Codex CLI 路径、沙箱、模型、环境变量透传等
- `[permission]` – 用户/群聊白名单与管理员
- `[task]` – 任务过期时间、清理策略、执行模式（local/remote）
- `[input_file]` – 回复文件作为输入材料的限制和清理
- `[napcat]` – 启用 NapCat 适配器文件上传
- `[snowluma]` – 启用 SnowLuma 适配器兼容发送

详细说明见下文 **配置项详解** 章节。

---

## 启动与常见问题

### 准备 Codex CLI

插件只负责调用本机 `codex` 命令，不负责安装、登录或配置 Codex CLI。请在运行 MaiBot 的同一个系统用户下确认：

**Linux / macOS：**

```bash
command -v codex
codex --version
codex -a never exec --json --color never -s workspace-write --skip-git-repo-check -C /tmp "用中文回复：测试成功"
```

**Windows PowerShell：**

```powershell
where codex
codex --version
codex -a never exec --json --color never -s workspace-write --skip-git-repo-check -C "$env:TEMP" "用中文回复：测试成功"
```

如果这些命令失败，先解决 Codex CLI 的安装、登录、网络、模型权限或本机配置问题。

### 修改插件配置

可以在 MaiBot Web UI 的插件配置页修改，也可以直接编辑 `config.toml`。最少需要检查这些项：

```toml
[plugin]
enabled = true

[local_codex]
codex_binary = "codex"          # 若终端找不到，改为绝对路径
work_root = "data/tasks"
sandbox = "workspace-write"
approval_policy = "never"
pass_env_vars = []              # 按需填入 ["OPENAI_API_KEY"] 等

[permission]
allow_all_users = false
user_list_mode = "whitelist"
trigger_users = ["qq:你的QQ号"]
admin_users = ["qq:你的QQ号"]

[task]
execution_mode = "local"
```

如果使用 SnowLuma 适配器并希望私聊进度，可额外开启：

```toml
[snowluma]
enabled = true
send_artifacts_as_file_segments = true
```

`[napcat].enabled` 和 `[snowluma].enabled` 必须二选一。

### 重启 MaiBot

改完配置后需要重启 MaiBot。无论使用 `uv run bot.py`、宝塔、systemd、Docker 还是 nohup，都要确保重启后的 MaiBot 使用的是你刚才测试过 `codex` 的同一个用户环境。

### 在 QQ 中测试

发送：

```text
/codex 用中文回复“任务创建成功”，并生成一个 txt 文件放到 artifacts 目录
```

正常情况下，麦麦会先返回任务 ID，随后返回进度、最终摘要和产物信息。

---

### 常见启动报错及解决

| 报错信息 | 可能原因与解决办法 |
| :--- | :--- |
| `[WinError 2] 系统找不到指定的文件。` | Windows 找不到 `codex`。用 PowerShell 执行 `where codex`，把 `codex.cmd` 的完整路径写入 `local_codex.codex_binary`。 |
| `[Errno 2] No such file or directory: 'codex'` | Linux 找不到 `codex`。用 `command -v codex` 查询完整路径并写入 `codex_binary`。若终端能找到但插件找不到，通常是 MaiBot 的启动环境和当前终端 PATH 不同。 |
| `ModuleNotFoundError: No module named 'httpx'` | 插件依赖没装进 MaiBot 当前 Python 环境。在 MaiBot 根目录执行 `uv add httpx`，或按你的启动环境安装 `httpx`。 |
| `Codex 可以在终端运行，但插件任务失败` | 检查 MaiBot 启动用户是否和你测试 Codex 的用户一致。宝塔、systemd、Docker、nohup、VS Code 终端可能使用不同的 PATH、HOME、CODEX_HOME 和登录状态。 |
| `Codex CLI 读取不到 API key` | 插件默认不传递任何环境变量。在 `[local_codex] pass_env_vars` 中显式加入所需变量名，例如 `["OPENAI_API_KEY"]`。这些变量会被 Codex、skill 和 MCP 读取，存在泄露风险，只给可信环境使用。 |
| `麦麦说没有 /codex 技能包` | 插件没有加载成功。检查插件是否启用、目录结构是否正确、MaiBot 日志里是否有 manifest 或依赖错误。 |

---

## 配置项详解

> 以下为完整的配置项说明，按功能分组，与实际 `config.toml` 保持同步。

### `[plugin]`

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `config_version` | `"1.0.0"` | 配置文件版本，用于兼容性检查。 |
| `enabled` | `true` | 是否启用插件。触发权限由 `[permission]` 控制。 |

### `[napcat]`

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `enabled` | `false` | 是否使用 NapCat Adapter API 高级能力。与 `[snowluma].enabled` 二选一。 |
| `upload_file` | `true` | 调用 `upload_group_file`/`upload_private_file` 时是否执行真实上传。 |
| `max_file_size_mb` | `100.0` | 单个产物最大上传大小（MB），`0` 表示不限制。 |

### `[snowluma]`

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `enabled` | `false` | 是否使用 SnowLuma Adapter 高级能力。与 `[napcat].enabled` 二选一。 |
| `send_artifacts_as_file_segments` | `true` | 通过 SnowLuma 兼容发送 API 发送 OneBot `file` 段回传产物。 |
| `max_file_size_mb` | `100.0` | SnowLuma `file` 段单个产物最大大小（MB），`0` 表示不限制。 |

### `[local_codex]`

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `codex_binary` | `"codex"` | Codex CLI 可执行文件名或绝对路径。 |
| `work_root` | `"data/tasks"` | 本地任务根目录，相对路径按插件目录解析。不建议使用默认目录以外的路径。 |
| `sandbox` | `"workspace-write"` | Codex 沙箱模式，推荐 `workspace-write`。`danger-full-access` 属高危。 |
| `approval_policy` | `"never"` | 审批策略，服务器无人值守时推荐 `never`。 |
| `model` | `""` | 可选模型名，留空使用 Codex CLI 默认配置。 |
| `enable_search` | `true` | 是否传递 `--search`（取决于 Codex CLI 版本是否支持）。 |
| `extra_args` | `[]` | 额外传给 `codex exec` 的参数，例如 `["--profile", "server-agent"]`。 |
| `pass_env_vars` | `[]` | 额外传给 Codex 子进程的环境变量名。默认只传最小运行环境。 |
| `process_timeout_seconds` | `3600.0` | 本地 Codex 任务运行超时（秒）。 |
| `artifact_globs` | `["artifacts/*", "*.docx", ...]` | 产物匹配规则，在任务 workspace 下扫描。 |

### `[permission]`

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `allow_all_users` | `false` | 是否允许所有用户触发。 |
| `allowed_users` | `[]` | 旧版用户白名单，保留兼容。推荐使用 `user_list_mode` + `trigger_users`。 |
| `user_list_mode` | `"blacklist"` | 用户名单模式：`whitelist` 或 `blacklist`。 |
| `trigger_users` | `[]` | 用户黑白名单，推荐格式 `qq:用户ID`。 |
| `admin_users` | `[]` | 管理员用户，允许使用高危配置。 |
| `allowed_groups` | `[]` | 旧版聊天流白名单，保留兼容。推荐使用 `chat_list_mode` + `trigger_chats`。 |
| `chat_list_mode` | `"blacklist"` | 聊天流名单模式：`whitelist` 或 `blacklist`。 |
| `trigger_chats` | `[]` | 聊天流黑白名单，可写群号、`qq:群号` 或 `stream_id`。 |
| `reject_temporary_private_chat` | `true` | 是否拒绝 QQ 群临时私聊触发。 |

### `[progress]`

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `forward_progress` | `true` | 是否把运行进度转发到 QQ。 |
| `min_send_interval_seconds` | `5.0` | 进度消息最小发送间隔（秒）。 |
| `max_progress_items_per_message` | `5` | 每次最多合并多少条进度。 |
| `max_progress_item_chars` | `300` | 单条进度最大字符数。 |
| `max_summary_chars` | `1800` | 最终摘要最大字符数。 |
| `enable_private_progress` | `true` | 是否允许用户使用 `--dm` 参数将进度私聊发送。 |
| `private_progress_trigger_args` | `["--dm", "--private-progress"]` | 触发私聊进度的参数。 |
| `private_progress_fallback_to_origin` | `true` | 私聊进度发送失败时，是否回退到原聊天流。 |
| `private_progress_send_task_created` | `false` | 使用 `--dm` 时是否把“任务已创建”也私聊发送。 |
| `private_progress_send_artifacts` | `false` | 使用 `--dm` 时是否把最终结果和产物也私聊发送。 |

### `[artifact]`

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `send_artifact_links` | `true` | 是否在完成时发送产物列表或下载链接。 |
| `try_custom_file_message` | `false` | 若远程服务返回 `artifact.custom_payload`，是否尝试用 `send.custom` 发送。 |
| `custom_file_message_type` | `"file"` | 自定义文件消息类型。 |

### `[input_file]`

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `enable_reply_file` | `true` | 是否允许回复文件消息作为输入材料。 |
| `input_dir_name` | `"input"` | 输入材料放入 Codex workspace 下的目录名。 |
| `max_files_per_task` | `5` | 单个任务最多导入多少个被回复文件。 |
| `max_file_size_mb` | `100.0` | 单个输入文件最大大小（MB），`0` 表示不限制。 |
| `auto_cleanup_input_files` | `true` | 启动时是否自动清理过期输入材料。 |
| `input_file_ttl_hours` | `24.0` | 输入材料保留时间（小时），`0` 表示不自动清理。 |
| `allow_url_download` | `true` | 是否允许从文件消息中的 HTTP URL 下载材料。 |
| `download_timeout_seconds` | `120.0` | 从 URL 下载材料的超时时间（秒）。 |
| `allowed_local_roots` | `[]` | 允许复制的本地文件根目录白名单。空列表表示禁止复制本地路径。 |

### `[task]`

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `command_prefix` | `"/codex"` | 帮助文本默认展示的主命令前缀（实际固定支持 `/codex` 和 `/agent`）。 |
| `execution_mode` | `"local"` | 执行模式：`local` 或 `remote`。 |
| `enable_cancel` | `true` | 是否允许取消远程任务。 |
| `task_type` | `"codex_cli"` | 提交给远程服务的任务类型。 |
| `max_running_tasks_per_stream` | `1` | 单个聊天流同时运行的最大任务数。 |
| `max_running_tasks_per_user` | `1` | 单个用户同时运行的最大任务数。 |
| `poll_interval_seconds` | `5.0` | 轮询远程任务状态间隔（秒）。 |
| `max_watch_seconds` | `3600.0` | 单个任务最长跟踪时间（秒），超时停止转发。 |
| `resumable_task_ttl_hours` | `24.0` | 普通 task 可通过 `resume` 继续的保留时间（小时）。 |
| `require_session_confirm` | `true` | 把普通 task 转为 session 时是否要求二次确认。 |
| `auto_cleanup_task_records` | `true` | 启动时是否自动清理过期普通 task 记录。 |
| `auto_cleanup_task_workspaces` | `true` | 清理过期 task 记录时是否同时删除 workspace 和产物。 |
| `enable_periodic_cleanup` | `false` | 是否启用后台定时清理。 |
| `periodic_cleanup_interval_minutes` | `60.0` | 后台定时清理间隔（分钟）。 |

### `[server]`（远程模式）

| 字段 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `base_url` | `""` | 远程 Agent 服务地址，如 `"https://agent.example.com"`。 |
| `api_token` | `""` | 鉴权 token，以 `Authorization: Bearer <token>` 发送。 |
| `require_api_token` | `true` | 是否要求配置 `api_token`，生产环境建议保持 `true`。 |
| `create_path` | `"/v1/tasks"` | 创建任务接口路径。 |
| `status_path_template` | `"/v1/tasks/{task_id}"` | 查询任务状态接口路径模板。 |
| `cancel_path_template` | `"/v1/tasks/{task_id}/cancel"` | 取消任务接口路径模板。 |
| `request_timeout_seconds` | `20.0` | HTTP 请求超时时间（秒）。 |
| `verify_tls` | `true` | 是否校验 HTTPS 证书，内网自签证书可临时关闭。 |

---

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

---

## Task 和 Session

### Task

默认 `/codex <任务描述>` 创建的是一次性 task。

特点：

- 适合单次任务。
- 会保存一段时间，可通过 `resume` 短时间继续。
- 超过 `task.resumable_task_ttl_hours` 后，插件可在启动时或定时清理时清理 task 记录。
- 清理行为由 `auto_cleanup_task_records` 和 `auto_cleanup_task_workspaces` 控制。

相关配置：

```toml
[task]
resumable_task_ttl_hours = 24.0
auto_cleanup_task_records = true
auto_cleanup_task_workspaces = true
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

---

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
- SnowLuma 模式下，入站 file 段若已经带 `url` 或 `path`，插件可以直接导入；若只有 `file_id`，当前 SnowLuma Adapter 没有公开等价文件补全 API，插件不会调用 NapCat 兜底。
- 如果 MaiBot 和当前 QQ 适配器不在同一个文件系统里，需要用共享卷或 URL 方式让 MaiBot 读到文件。
- 输入材料会放在 `workspace/input/`，可按 `input_file_ttl_hours` 自动清理；清理输入材料不会删除产物、日志或 task 记录。

---

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

---

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

---

## SnowLuma 适配

SnowLuma 适配是独立路径。开启 `[snowluma]` 后，本插件只通过 MaiBot SDK 调用 SnowLuma 适配器公开的能力；不会把 NapCat 当作兜底，也不要求同时启用 `[napcat]`。

SnowLuma 适配器 0.7.x 公开了一部分 `adapter.napcat.*` 兼容 API。这里的 API 名称带有 `napcat`，但调用对象仍然是 SnowLuma 适配器暴露给 MaiBot 插件系统的公开 API，不是直接连接 NapCat。

### 配置

`[napcat].enabled` 和 `[snowluma].enabled` 必须二选一。SnowLuma 模式推荐配置：

```toml
[napcat]
enabled = false
```

```toml
[snowluma]
enabled = true
send_artifacts_as_file_segments = true
max_file_size_mb = 100.0
```

如果只需要普通文本进度和结果，`send_artifacts_as_file_segments` 可以关闭；插件仍会发送产物列表。开启后，插件会尝试通过 SnowLuma 发送 OneBot `file` 段。

### 已适配能力

| 能力 | SnowLuma 实现方式 |
| --- | --- |
| `/codex` 创建任务 | 走 MaiBot 正常命令入口，依赖 SnowLuma 把 QQ 消息注入 MaiBot |
| `/codex --dm` 私聊进度 | 调用 `adapter.napcat.message.send_private_msg` |
| 任务创建、进度、最终文本回传 | 走 MaiBot `send.text`，由 SnowLuma 网关发回 QQ |
| 回复消息追溯 | 调用 `adapter.napcat.message.get_msg` 获取被回复消息 |
| 回复文件作为输入 | SnowLuma 入站 file 段或渲染文本里带 `url/path` 时，插件导入到 `workspace/input` |
| 产物 file 段回传 | 调用 `adapter.napcat.message.send_group_msg` / `send_private_msg` 发送 OneBot `file` 段 |
| 无 NapCat 兜底 | SnowLuma 模式下不会调用 NapCat 文件 API 或 NapCat 进程 |

### 当前限制

SnowLuma 目前无法独立补全“只有 `file_id`、没有 `url/path`”的普通文件消息。也就是说：

- 如果入站文件消息已经带 `url`，插件可以下载并导入。
- 如果入站文件消息只带 `file_id`，插件会拒绝导入并说明缺少 SnowLuma 文件补全能力。
- 要补齐这项能力，需要 SnowLuma 提供 `file_id + group_id/user_id -> url/path` 的公开 API 文档，例如 `get_file`、`get_group_file_url`、`get_private_file_url` 或等价动作。

如果 SnowLuma 或 QQ 端不支持 OneBot `file` 段，插件会报告 SnowLuma 文件段发送失败，并保留产物列表；不会改用 NapCat。

### 排查建议

如果 SnowLuma 能看到消息但插件没有响应，优先检查：

- SnowLuma 适配器是否已启用并连接成功，MaiBot 日志中应出现 `SnowLuma WebSocket 已连接`。
- SnowLuma 适配器的群聊/私聊名单过滤是否拦截了当前会话。
- 本插件 `[snowluma].enabled` 是否为 `true`，且 `[napcat].enabled` 是否为 `false`。
- `/codex` 命令是否被权限配置拦截，例如 `allow_all_users`、`trigger_users`、`trigger_chats`。
- 文件输入是否真的带有 `url/path`，只有 `file_id` 时当前 SnowLuma 路径无法独立下载。

---

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
enable_search = true
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

---

## 权限配置

推荐至少限制用户，必要时再限制群。

```toml
[permission]
allow_all_users = false
user_list_mode = "whitelist"
trigger_users = ["qq:123456789"]
admin_users = ["qq:123456789"]
chat_list_mode = "whitelist"
trigger_chats = ["qq:987654321"]
```

字段说明：

- `allow_all_users`: 是否允许所有用户触发。
- `user_list_mode`: `whitelist` 或 `blacklist`，配合 `trigger_users` 使用。
- `trigger_users`: 允许或禁止触发的用户，推荐格式 `qq:用户ID`。
- `admin_users`: 管理员用户，可使用高危权限相关能力。
- `chat_list_mode`: `whitelist` 或 `blacklist`，配合 `trigger_chats` 使用。
- `trigger_chats`: 允许或禁止触发的群号、`qq:群号` 或 MaiBot `stream_id`。

---

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

---

## 运行时清理策略

普通 task、输入材料和 session 的清理策略不同：

- 普通 task 记录可按 `task.resumable_task_ttl_hours` 过期清理。
- `task.auto_cleanup_task_records = true` 时，插件启动会自动清理过期普通 task 记录。
- `task.enable_periodic_cleanup = true` 时，长时间不重启 MaiBot 也会按间隔定时清理。
- `task.auto_cleanup_task_workspaces = true` 时，清理 task 记录时会同时删除 workspace 和产物。
- `input_file.auto_cleanup_input_files = true` 时，过期输入材料会按 `input_file.input_file_ttl_hours` 清理。
- 输入材料清理只删除 `workspace/input/`，不会删除 `workspace/artifacts/`、日志或 task 记录。
- session 不自动清理，需要管理员手动 `/codex clean session <session名>` 并按提示确认。

---

## 远程模式

> [!NOTE]
> 以下内容未进行调试和验证，不确保其可用性。

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

---

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

如果报错时提示 `Missing environment variable:`，检查 `pass_env_vars`，填入该报错提示的环境变量名称。

### 宝塔启动能跑 MaiBot，但 Codex 不工作

宝塔、systemd、SSH、VS Code 终端可能使用不同的环境变量和用户上下文。重点检查：

- `which codex` / `where codex`
- `codex --version`
- `echo $HOME`
- `echo $CODEX_HOME`
- MaiBot 启动用户是否能读取 Codex 登录状态

如启动环境找不到 codex，在 `local_codex.codex_binary` 填绝对路径：
- Ubuntu/Linux：`/root/.local/bin/codex` 或 `/usr/local/bin/codex`
- Windows：`C:\Users\你的用户名\AppData\Roaming\npm\codex.cmd`

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

---

## 安全建议

- 不要长期对所有用户开放，优先使用 `trigger_users` 和 `trigger_chats` 白名单。
- 默认使用 `workspace-write` 沙箱，不要随意开放 `danger-full-access`。
- `work_root` 默认使用插件目录的 `data/tasks`；不建议改到默认目录以外。
- 控制任务超时、文件大小、进度长度和摘要长度。
- 用户上传的文件可能包含隐私或敏感信息，只允许可信用户触发。
- 不要把完整群聊历史默认交给 Codex。
- 不要随意把 API key、token、secret 加入 `local_codex.pass_env_vars`；这些变量会暴露给 Codex、skill 和 MCP。
- 如果使用 `remote` 模式，务必启用鉴权，并保持 `server.require_api_token = true`。
