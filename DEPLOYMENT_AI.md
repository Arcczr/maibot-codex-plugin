# AI Deployment Guide

This document is written for AI agents that need to deploy or verify the
`remote_codex_agent` MaiBot plugin on a new machine.

## Purpose

The plugin lets MaiBot receive QQ commands such as `/codex ...`, run Codex CLI
on the MaiBot host, forward progress to QQ, and return generated artifacts. The
recommended production mode is `task.execution_mode = "local"`.

## Required Files

The plugin repository root must contain:

- `_manifest.json`
- `plugin.py`
- `config.toml`
- `README.md`
- `LICENSE`

Do not deploy runtime files, local credentials, caches, or task workspaces.
These are intentionally ignored:

- `.env.local`
- `__pycache__/`
- `data/`
- `tasks/`
- `artifacts/`
- `workspace/`
- `_records/`
- local runbooks and copied documentation folders

## Host Requirements

Assume the target machine already has:

- MaiBot installed and runnable
- Python available through the MaiBot runtime
- `uv` if MaiBot is normally started with `uv run bot.py`
- network access required by Codex CLI and optional NapCat HTTP API

The plugin adds one Python dependency:

```bash
uv add httpx
```

If the host does not use `uv`, install `httpx` into the same Python environment
that starts MaiBot.

## Codex CLI Requirement

The plugin does not install or log in Codex CLI. The same OS user that starts
MaiBot must be able to run Codex.

Verify:

```bash
codex --version
codex -a never exec --json --color never -s workspace-write --skip-git-repo-check -C /tmp "用中文回复：测试成功"
```

If this fails, fix Codex installation, authentication, network access, model
access, or `CODEX_HOME` before debugging the plugin.

## Install Location

Deploy the repository as:

```text
MaiBot/plugins/remote_codex_agent/
```

The command component supports `/codex` and `/agent` regardless of
`task.command_prefix`; that config key only affects help text.

## Minimum Local Configuration

Edit `config.toml` or the MaiBot plugin WebUI configuration.

Recommended production baseline:

```toml
[plugin]
enabled = true

[permission]
allow_all_users = false
allowed_users = ["qq:USER_ID"]
allowed_groups = []

[task]
execution_mode = "local"

[local_codex]
codex_binary = "codex"
work_root = "data/remote_codex_agent/tasks"
sandbox = "workspace-write"
approval_policy = "never"
enable_search = false
```

Important checks:

- Review `permission.allow_all_users`; do not leave it enabled for public use
  unless the operator explicitly accepts that risk.
- Use `workspace-write` by default.
- Do not use `danger-full-access` unless an administrator explicitly enables it
  and the host is appropriately isolated.
- `work_root` is resolved relative to the MaiBot process working directory when
  it is not absolute.

## Optional Search

To enable Codex live web search for local tasks:

```toml
[local_codex]
enable_search = true
```

The installed Codex CLI must support `--search`. Confirm after config update:

```text
/codex config
```

Expected QQ output includes:

```text
联网搜索：启用
```

When changed through MaiBot WebUI, this plugin handles config hot update through
`on_config_update`. Directly editing `config.toml` may require a MaiBot restart
depending on the host configuration loader.

## NapCat Artifact Upload

NapCat direct upload is optional but recommended when QQ users need to receive
files instead of server-local paths.

NapCat WebUI should expose an HTTP server reachable from the MaiBot host.
Common same-host configuration:

```toml
[napcat]
enabled = true
scheme = "http"
host = "127.0.0.1"
port = 9998
token = ""
upload_file = true
max_file_size_mb = 100.0
```

The plugin calls:

- `upload_group_file` for group chats
- `upload_private_file` for private chats

If MaiBot and NapCat run in separate containers, make sure artifact paths are
visible to the NapCat side or use a deployment topology that supports file
transfer.

## Reply File Input

The stable interaction is:

```text
User uploads a QQ file.
User replies to that file message with: /codex <task>
```

The plugin imports the replied file into:

```text
workspace/input/
```

Relevant config:

```toml
[input_file]
enable_reply_file = true
input_dir_name = "input"
max_files_per_task = 5
max_file_size_mb = 100.0
allow_url_download = true
allowed_local_roots = []
```

The plugin does not guess recently uploaded files. It requires an explicit reply
to a file message.

## Runtime Data

Default task root:

```text
MaiBot/data/remote_codex_agent/tasks/
```

Per-task layout:

```text
<task_id>/
  prompt.md
  stdout.jsonl
  stderr.log
  final.md
  workspace/
    input/
    artifacts/
```

Persistent task/session records:

```text
data/remote_codex_agent/tasks/_records/
```

Normal task records are cleaned on plugin load when older than
`task.resumable_task_ttl_hours`. Session records are persistent.

## Smoke Test

After deployment or restart, test in QQ:

```text
/codex config
```

Then test a file-generating task:

```text
/codex 用中文生成一个 txt 文件，内容是“任务创建成功”，放到 artifacts 目录
```

Expected sequence:

1. QQ receives a task-created message with a task id.
2. Progress may be forwarded.
3. QQ receives a final summary.
4. Artifact list or direct uploaded file is returned.

## Troubleshooting Checklist

If `/codex` is not triggered:

- Send `/codex help` with `/codex` at the start of the message.
- Avoid manually typed `@bot /codex`; real QQ mentions may be represented
  differently from plain text.
- Check MaiBot logs for `命令执行成功: remote_codex_agent`.
- Confirm plugin loaded: `插件 arcczr.remote-codex-agent` or current manifest id.
- Confirm `plugin.enabled = true`.
- Confirm user/group permission.

If Codex task creation fails:

- Run the Codex CLI smoke command as the MaiBot process user.
- Check `local_codex.work_root` write permissions.
- Check `local_codex.env_file` if Codex needs extra environment variables.
- Check `stderr.log` and `stdout.jsonl` under the task directory.

If file upload fails:

- Check NapCat HTTP server, host, port, and token.
- Check file path visibility between MaiBot and NapCat.
- Check `napcat.max_file_size_mb`.

## Pre-release Validation

Run from the plugin root:

```bash
uv run python -m py_compile plugin.py
uv run python -m json.tool _manifest.json >/dev/null
git diff --check
```

For official plugin repository submission, also verify:

- `LICENSE` exists and matches `_manifest.json` license.
- `_manifest.json` author and URLs point to the public plugin repository.
- Runtime data and local credentials are not tracked.
