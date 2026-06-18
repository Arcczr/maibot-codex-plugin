# AI Deployment Guide

This document is for AI agents deploying or verifying the
`remote_codex_agent` MaiBot plugin on a new machine.

## Goal

The plugin lets MaiBot receive QQ commands such as `/codex ...`, run Codex CLI
on the MaiBot host, forward progress to QQ, import replied QQ files as task
materials, and return generated artifacts.

Recommended production mode:

```toml
[task]
execution_mode = "local"
```

Remote HTTP Agent mode still exists, but local mode is the primary deployment
path.

## Required Plugin Files

The plugin directory must contain:

- `_manifest.json`
- `plugin.py`
- `config.toml`
- `README.md`
- `LICENSE`

Recommended supporting docs:

- `DEPLOYMENT_AI.md`
- `DEVELOPMENT_AI.md`

Do not deploy or commit runtime data:

- `__pycache__/`
- `data/`
- `tasks/`
- `artifacts/`
- `workspace/`
- `_records/`
- copied external docs
- local runbooks

## Host Requirements

Assume the target machine already has:

- MaiBot installed and runnable
- the same Python environment that starts MaiBot
- `uv` if MaiBot is normally started with `uv run bot.py`
- network access required by Codex CLI
- NapCat Adapter loaded if QQ file upload/download integration is needed

The plugin requires the Python package:

```text
httpx >= 0.28.0
```

Install `httpx` into the exact Python environment that starts MaiBot.

If MaiBot uses `uv` and owns a project environment, run from the MaiBot root:

```bash
uv add httpx
```

If you do not want to modify project dependency files, install into the active
environment used by MaiBot. The exact command depends on the host, for example:

```bash
uv pip install httpx
```

Verify from the MaiBot root:

```bash
uv run python - <<'PY'
import httpx
print(httpx.__version__)
PY
```

If MaiBot is not started with `uv`, use that deployment's Python command
instead.

## Codex CLI Requirement

The plugin does not install Codex CLI and does not log in. The same OS user that
starts MaiBot must be able to run Codex.

As that user, verify:

```bash
which codex
codex --version
codex -a never exec --json --color never -s workspace-write --skip-git-repo-check -C /tmp "用中文回复：测试成功"
```

If this fails, fix Codex installation, authentication, network access, model
access, `PATH`, `HOME`, or `CODEX_HOME` before debugging the plugin.

BaoTa, systemd, Docker, SSH shells, and VS Code terminals may run as different
users or with different environment variables. Always test Codex as the actual
MaiBot process user.

The plugin does not load an env file for Codex subprocesses. Configure the
MaiBot process environment itself, or set `local_codex.codex_binary` to an
absolute executable path.

Absolute path examples:

```toml
[local_codex]
# Ubuntu/Linux
codex_binary = "/root/.local/bin/codex"

# Windows
codex_binary = "C:\\Users\\YourName\\AppData\\Roaming\\npm\\codex.cmd"
```

## Install Location

Place the plugin at:

```text
MaiBot/plugins/remote_codex_agent/
```

Example:

```bash
cd /path/to/MaiBot/plugins
git clone https://github.com/Arcczr/maibot-codex-plugin.git remote_codex_agent
```

Then confirm:

```text
MaiBot/plugins/remote_codex_agent/plugin.py
MaiBot/plugins/remote_codex_agent/config.toml
MaiBot/plugins/remote_codex_agent/_manifest.json
```

The plugin supports `/codex` and `/agent`. The config key
`task.command_prefix` only affects help text.

## Minimum Configuration

Edit `plugins/remote_codex_agent/config.toml` or the MaiBot plugin WebUI.

Safe baseline:

```toml
[plugin]
enabled = true

[permission]
allow_all_users = false
allowed_users = ["qq:USER_ID"]
admin_users = ["qq:USER_ID"]
allowed_groups = []

[task]
execution_mode = "local"
enable_cancel = true
resumable_task_ttl_hours = 24.0
auto_cleanup_task_records = true
auto_cleanup_task_workspaces = false
enable_periodic_cleanup = false
periodic_cleanup_interval_minutes = 60.0

[local_codex]
# Absolute examples:
# Ubuntu/Linux: "/root/.local/bin/codex" or "/usr/local/bin/codex"
# Windows: "C:\\Users\\YourName\\AppData\\Roaming\\npm\\codex.cmd"
codex_binary = "codex"
work_root = "data/tasks"
sandbox = "workspace-write"
approval_policy = "never"
model = ""
enable_search = false

[input_file]
enable_reply_file = true
input_dir_name = "input"
max_files_per_task = 5
max_file_size_mb = 100.0
auto_cleanup_input_files = true
input_file_ttl_hours = 24.0
allow_url_download = true
allowed_local_roots = []
```

Important choices:

- Do not leave `allow_all_users = true` for public use unless explicitly
  accepted by the operator.
- Configure `admin_users`; admin-only commands include `/codex list all` and
  `/codex clean`.
- Use `workspace-write` by default.
- Use `danger-full-access` only on isolated hosts and only for admin users.
- `work_root` is relative to the plugin directory when not
  absolute. Do not change it away from the default directory unless the
  operator accepts the safety and accidental deletion risks.

## Cleanup Configuration

Startup cleanup:

- `auto_cleanup_task_records = true` deletes expired normal task records.
- `auto_cleanup_task_workspaces = false` keeps task files and artifacts by
  default.
- `auto_cleanup_input_files = true` deletes expired input materials under
  `workspace/input/`.

Periodic cleanup:

```toml
[task]
enable_periodic_cleanup = true
periodic_cleanup_interval_minutes = 60.0
```

Enable this for long-running MaiBot processes that are rarely restarted.

Session records are persistent and are not automatically deleted. To delete a
session, an admin must run:

```text
/codex clean session <session_name>
/codex clean session <session_name> confirm
```

The confirm step deletes the session record and associated task records and
workspaces.

## Optional Search

To enable Codex live web search:

```toml
[local_codex]
enable_search = true
```

The installed Codex CLI must support `--search`. Confirm in QQ:

```text
/codex config
```

Expected output includes:

```text
联网搜索：启用
```

WebUI/plugin config updates call `on_config_update`. Direct file edits may
require a MaiBot restart depending on the host configuration loader.

## NapCat Artifact Upload

NapCat direct upload is optional but recommended when QQ users should receive
files directly instead of only seeing artifact names. The plugin uses MaiBot
SDK `api.call` to call NapCat Adapter public APIs; do not configure NapCat HTTP
host, port, or token in this plugin.

Plugin config:

```toml
[napcat]
enabled = true
upload_file = true
max_file_size_mb = 100.0
```

The plugin calls:

- `adapter.napcat.file.upload_group_file` for group chats
- `adapter.napcat.file.upload_private_file` for private chats

If MaiBot and NapCat run in separate containers or machines:

- The returned artifact path must be readable by the NapCat process, or the
  deployment must use an upload path/API that supports the file location.
- Use shared volumes when both services are containerized.

## Reply File Input

Stable interaction:

```text
User uploads a QQ file.
User replies to that file message with: /codex <task>
```

The plugin imports the replied file into:

```text
<task_workspace>/input/
```

It can use MaiBot message lookup, NapCat `get_msg`, direct local file paths,
file URLs, or NapCat `get_file`, depending on what the incoming message exposes.

The plugin does not guess recently uploaded files. It requires an explicit reply
to the file message.

## Runtime Data

Default task root:

```text
MaiBot/plugins/remote_codex_agent/data/tasks/
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

Persistent records:

```text
data/tasks/_records/tasks/
data/tasks/_records/sessions/
```

Normal task records can expire. Session records persist until admin cleanup.

## Start Or Restart MaiBot

Start MaiBot using the deployment's normal method, for example:

```bash
cd /path/to/MaiBot
uv run bot.py
```

If using BaoTa, systemd, Docker, or another process manager:

- confirm the process user
- confirm `PATH` includes `codex`
- confirm `HOME` and `CODEX_HOME`
- confirm the process can write `local_codex.work_root`

Check logs for plugin load errors and for the manifest id:

```text
arcczr.remote-codex-agent
```

## Smoke Tests

Run these in QQ after deployment.

1. Command trigger:

```text
/codex help
```

2. Runtime config:

```text
/codex config
```

3. Simple text task:

```text
/codex 用中文回复：插件测试成功
```

4. Artifact task:

```text
/codex 生成一个 txt 文件，内容是“任务创建成功”，放到 artifacts 目录
```

Expected:

- task-created message with a task id
- optional progress
- final summary
- artifact list or direct QQ file upload if NapCat is enabled

5. Reply-file task:

```text
Upload a small txt/docx/pdf file.
Reply to that file message with: /codex 总结这个文件
```

Expected:

- task-created message says imported reference files
- Codex uses `workspace/input/`

6. Admin record commands:

```text
/codex list
/codex list all
/codex clean input
```

`list all` and `clean input` require `permission.admin_users`.

## Troubleshooting

If `/codex` is not triggered:

- Send `/codex help` with `/codex` at the start of the message.
- Avoid manually typed `@bot /codex`; real QQ mentions may be represented
  differently from plain text.
- Confirm plugin loaded.
- Confirm `plugin.enabled = true`.
- Confirm user/group permission.
- Check MaiBot logs for command match or plugin load errors.

If Codex task creation fails:

- Run the Codex CLI smoke command as the MaiBot process user.
- Check `local_codex.work_root` write permissions.
- Check task `stderr.log` and `stdout.jsonl`.
- Compare `HOME`, `CODEX_HOME`, and `PATH` between shell and process manager.
- If `codex` is not on `PATH`, set `local_codex.codex_binary` to an absolute
  path such as `/root/.local/bin/codex` on Ubuntu/Linux or
  `C:\Users\YourName\AppData\Roaming\npm\codex.cmd` on Windows.

If NapCat artifact upload fails:

- Check that MaiBot loaded NapCat Adapter and the plugin manifest has `api.call`.
- Check `napcat.upload_file = true`.
- Check file path visibility between MaiBot and NapCat.
- Check `napcat.max_file_size_mb`.
- Check whether NapCat returned timeout after a file may already have been sent.

If reply-file input fails:

- Confirm the command replied to the file message.
- Confirm `input_file.enable_reply_file = true`.
- Confirm `napcat.enabled = true` and NapCat Adapter public APIs are available if only `file_id` is present.
- Confirm MaiBot can read the returned local file path or download URL.
- For containers, confirm shared volumes or URL access.

## Pre-release Validation

Run from plugin root:

```bash
uv run python -m py_compile plugin.py
uv run python -m json.tool _manifest.json >/dev/null
uv run python - <<'PY'
import tomllib
with open('config.toml', 'rb') as f:
    tomllib.load(f)
PY
git diff --check
```

For official plugin repository submission:

- `LICENSE` exists and matches `_manifest.json`.
- `_manifest.json` author and URLs point to the public repository.
- `_manifest.json` capabilities match actual SDK use.
- Runtime data and local credentials are not tracked.
