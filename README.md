# Codex CLI QQ 调度器

这个插件让 QQ 群里的命令在 MaiBot 所在服务器上触发 `codex exec --json`。默认执行模式是 `local`：QQ 机器人和 Codex CLI 在同一台 Ubuntu 服务器上，插件直接启动本机 Codex CLI 子进程，并把运行进度、最终摘要和产物路径回传到当前聊天流。

插件也保留 `remote` 模式，后续如果你想把 Codex 执行器拆成独立 HTTP Agent 服务，可以继续使用。

## 使用方式

1. 确认服务器上能直接运行：

```bash
codex -a never exec --json --color never -s workspace-write --skip-git-repo-check -C /tmp "你好"
```

2. 配置 `config.toml`：
   - `plugin.enabled = true`
   - `task.execution_mode = "local"`
   - `permission.allowed_users = ["qq:你的QQ号"]`
   - 按需配置 `permission.allowed_groups`
   - 确认 `local_codex.work_root` 指向服务器上允许写入的目录

3. 重载或启动插件。

4. 在 QQ 中发送：

```text
/codex 帮我搜索某个主题，整理成一份 Word 文档，并把产物放到 artifacts 目录
```

插件命令组件固定支持 `/codex` 和 `/agent` 两个前缀；`config.toml` 中的 `task.command_prefix` 只影响帮助文本默认展示。

辅助命令：

```text
/codex help
/codex status
/codex status <task_id>
/codex cancel <task_id>
```

## 本机执行流程

```text
QQ 群触发
 -> 插件回复任务已创建
 -> 插件创建 data/remote_codex_agent/tasks/<task_id>/
 -> 插件写入 prompt.md
 -> 插件启动 codex exec --json
 -> 插件读取 stdout JSONL 并节流转发进度
 -> Codex 最终回答写入 final.md
 -> 插件扫描 workspace 下的产物
 -> 插件把摘要和产物路径发回 QQ
 -> 如果启用 NapCat 直传，插件调用 NapCat HTTP API 把产物作为 QQ 文件发回当前聊天
```

本机命令大致为：

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

插件会把 prompt 从 stdin 传给 Codex CLI。

## 产物规则

插件会扫描 `local_codex.artifact_globs` 匹配到的文件，默认包括：

```toml
artifact_globs = ["artifacts/*", "*.docx", "*.pdf", "*.md", "*.zip", "*.xlsx", "*.pptx"]
```

建议让 Codex 把最终文件放在：

```text
workspace/artifacts/
```

当前插件默认回传服务器本地路径。若要让 QQ 群成员直接收到文件，可以启用 NapCat 直传。

### NapCat 直传文件

在 NapCat WebUI 为这个 bot 新增一个 **HTTP 服务器** 配置：

- host：同机部署建议 `127.0.0.1`
- port：例如 `9998`
- token：可选；如果设置了 token，插件配置里也要填写

然后配置插件：

```toml
[napcat]
enabled = true
scheme = "http"
host = "127.0.0.1"
port = 9998
token = ""
```

启用后，任务结束时插件会根据当前聊天类型调用 NapCat：

- 群聊：`upload_group_file`
- 私聊：`upload_private_file`

因为 Codex CLI、MaiBot 和 NapCat 在同一台服务器上，插件会把产物的服务器绝对路径直接交给 NapCat，不需要公网下载链接。

## 远程模式接口契约

如果把 `task.execution_mode` 改为 `remote`，插件会调用 `server.base_url`。

### 创建任务

`POST /v1/tasks`

请求头：

```http
Authorization: Bearer <api_token>
Content-Type: application/json
```

响应体至少包含：

```json
{
  "task_id": "task_123",
  "status": "queued",
  "message": "任务已创建"
}
```

### 查询任务

`GET /v1/tasks/{task_id}`

响应体建议：

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

任务结束时：

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

`status` 支持：`queued`、`running`、`succeeded`、`failed`、`cancelled`。插件也兼容 `completed`、`done`、`success`、`error` 等常见别名。

## 安全建议

- 只给白名单 QQ 用户和群开放。
- Codex CLI 使用 `workspace-write` 或更严格的沙箱；不要默认裸跑全盘权限。
- `work_root` 应放在专用目录，不要指向 MaiBot 主仓库根目录。
- 限制运行时长、输出长度、文件大小。
- 不要把完整群聊历史默认发给 Codex。
- 如果启用 `remote` 模式，远程服务必须有鉴权，并保持 `server.require_api_token = true`。
