# AI Development Guide

This document is for AI agents maintaining or extending the
`remote_codex_agent` MaiBot plugin.

## Scope

Work inside this plugin directory unless the user explicitly asks to modify
MaiBot core or an adapter.

Tracked source files normally are:

- `.gitignore`
- `LICENSE`
- `README.md`
- `_manifest.json`
- `config.toml`
- `plugin.py`
- `DEPLOYMENT_AI.md`
- `DEVELOPMENT_AI.md`

Do not track runtime or local-only files:

- `__pycache__/`
- `data/`
- `tasks/`
- `artifacts/`
- `workspace/`
- `_records/`
- copied external docs
- local runbooks

## Current Shape

The plugin is currently a large single-file implementation in `plugin.py`.
Avoid opportunistic splitting while fixing behavior. If the user asks for a
structure-only refactor, split in a separate commit with no behavior changes.

Major groups inside `plugin.py`:

- Config models:
  `PluginSectionConfig`, `ServerConfig`, `PermissionConfig`, `TaskConfig`,
  `LocalCodexConfig`, `ProgressConfig`, `ArtifactConfig`, `NapCatConfig`,
  `InputFileConfig`, `RemoteCodexAgentConfig`
- Runtime models: `InputFile`, `RemoteTaskState`
- HTTP clients: `RemoteAgentClient`; NapCat operations go through MaiBot SDK `ctx.api.call`
- Main plugin lifecycle and command entry: `RemoteCodexAgentPlugin`,
  `handle_codex_command`
- Local Codex execution: `_create_local_task`, `_run_local_codex_task`,
  `_build_local_codex_command`, `_consume_local_stdout`,
  `_read_local_final_message`
- Records and sessions: `_record_task_state`, `_load_task_record`,
  `_load_session_record`, `_update_session_from_task`,
  `_hydrate_session_history`
- Cleanup: `_cleanup_expired_task_records`, `_cleanup_expired_input_files`,
  `_run_periodic_cleanup`, `_delete_task_record_by_id`,
  `_delete_session_record_by_name`
- Reply-file input: `_prepare_reply_input_files`,
  `_extract_reply_message_id`, `_extract_file_segments`,
  `_import_input_file_segment`
- Artifacts and upload: `_collect_local_artifacts`,
  `_upload_artifacts_via_napcat`, `_try_send_custom_artifacts`
- User commands: `_handle_status`, `_handle_cancel`, `_handle_list`,
  `_handle_clean`, `_handle_skills`, `_handle_mcp`, `_handle_config`,
  `_handle_session_command`, `_handle_continue_command`,
  `_handle_resume_command`

Possible future split:

```text
plugin.py          # plugin entry, lifecycle, Command decorator
config.py          # config models
models.py          # dataclasses and status constants
clients.py         # RemoteAgentClient
records.py         # task/session records and cleanup
input_files.py     # QQ reply-file import
local_codex.py     # Codex CLI process and stdout/final parsing
artifacts.py       # artifact scan and upload helpers
commands.py        # _handle_* command handlers
```

Do not do that split together with feature work.

## Command Surface

Supported prefixes:

```text
/codex
/agent
```

Current commands:

```text
/codex <task>
/codex help
/codex status
/codex status <task_id>
/codex cancel <task_id>
/codex list
/codex list all
/codex clean
/codex clean input
/codex clean task <task_id>
/codex clean session <session_name>
/codex clean session <session_name> confirm
/codex skills
/codex mcp
/codex config
/codex session <name> <task>
/codex session <task_id> confirm [name]
/codex continue <task>
/codex resume <task_id|session_name|thread_id> <task>
```

Admin-only commands:

- `/codex list all`
- `/codex clean`
- `/codex clean input`
- `/codex clean task <task_id>`
- `/codex clean session <session_name>`
- `/codex clean session <session_name> confirm`

When command behavior changes, update:

- `_build_help_text`
- `README.md`
- `DEPLOYMENT_AI.md`
- this file

QQ does not render Markdown reliably. User-facing plugin messages should be
plain text, concise, and Chinese-first.

## Local Codex Execution

Local mode is the recommended production mode. The command is built roughly as:

```bash
codex -a never -s workspace-write -C <workspace> exec --json --color never \
  --skip-git-repo-check --output-last-message <final.md> -
```

Details:

- `local_codex.codex_binary` selects the executable.
- `local_codex.model` appends `-m <model>` when non-empty.
- `local_codex.enable_search = true` appends `--search`.
- `local_codex.extra_args` is appended before the prompt is sent.
- `_build_local_codex_env` intentionally passes only a minimal environment plus
  explicit `local_codex.pass_env_vars`. Do not restore full `os.environ`
  inheritance; secrets exposed here may be read by Codex, skills, or MCP.
- If the process environment cannot find `codex`, set `codex_binary` to an
  absolute path, for example `/root/.local/bin/codex` on Ubuntu/Linux or
  `C:\Users\YourName\AppData\Roaming\npm\codex.cmd` on Windows.
- `resume_thread_id` uses `codex exec resume <thread_id>`.
- Prompt is sent through stdin and stdin is closed.
- `codex exec` is non-interactive in this plugin. It cannot accept in-flight
  steering during an active run.

True in-flight steering would need a different runtime design, likely
`codex app-server` with turn/steer semantics.

Dangerous local permission:

- `sandbox = "danger-full-access"` or
  `--dangerously-bypass-approvals-and-sandbox` is only allowed for admin users.
- Admin users are configured with `permission.admin_users`.

## Progress And Final Replies

Local stdout is JSONL. The plugin extracts completed `agent_message` items.

Duplicate-answer mitigation:

- `_queue_local_progress` queues one progress item behind.
- `_schedule_pending_local_progress_flush` sends a pending item after the
  progress interval if the task keeps running and no newer agent message
  arrives. This prevents long web/search tasks from showing their first
  progress only at completion time.
- `_discard_pending_local_progress` drops the final queued item on completion,
  because it is usually the final answer.
- `_read_local_final_message` reads the final answer from `final.md`.

When touching progress handling, test:

- a pure Q&A task with no artifact
- a task that creates one artifact
- a task that creates multiple artifacts
- a task that produces English progress from Codex

## Records, Tasks, And Sessions

Task records:

```text
<work_root>/_records/tasks/<task_id>.json
```

Session records:

```text
<work_root>/_records/sessions/<session_name>.json
```

Session records should preserve:

- latest task fields for `/codex continue`
- `latest_task_id`
- `codex_thread_id`
- `task_ids`
- `history`

`_hydrate_session_history` migrates older session records that only kept the
latest task. Do not remove it without a replacement migration.

Normal task cleanup must not delete session records or session task records.
Session cleanup is explicit and admin-only.

## Cancel Semantics

`/codex cancel <task_id>` only cancels a task id, not a session name.

Current behavior:

- Running local task: call `process.terminate()`, set `last_status` to
  `cancelled`, persist the task record, update the session record if needed,
  and cancel the watcher task.
- Queued/local tracked task without a process yet: mark as `cancelled`,
  persist it, update the session if needed, and cancel the watcher.
- Completed tracked task: persist current terminal state and tell the user it
  has already ended.
- Untracked task with a terminal record: tell the user it has already ended.
- Untracked non-terminal record: tell the user the current process is not
  tracking it, so the plugin cannot terminate the process.
- Session name: tell the user it is a session record and provide the latest
  task id if available.

If cancel behavior changes, keep `/codex list` and session history consistent
by updating persisted records immediately.

## Cleanup Semantics

Configuration in `[task]`:

```toml
resumable_task_ttl_hours = 24.0
auto_cleanup_task_records = true
auto_cleanup_task_workspaces = false
enable_periodic_cleanup = false
periodic_cleanup_interval_minutes = 60.0
```

Configuration in `[input_file]`:

```toml
auto_cleanup_input_files = true
input_file_ttl_hours = 24.0
```

Startup cleanup:

- Runs in `on_load`.
- Uses `auto_cleanup_task_records`.
- Uses `auto_cleanup_task_workspaces` if task records are expired.
- Uses `auto_cleanup_input_files`.

Periodic cleanup:

- Controlled by `task.enable_periodic_cleanup`.
- Default is off.
- Runs `_run_periodic_cleanup` as an asyncio task.
- Config hot update restarts/stops the cleanup task.
- `on_unload` cancels and awaits the cleanup task.

Manual cleanup:

- `/codex clean`: admin-only; cleans expired normal task records and expired
  input materials.
- `/codex clean input`: admin-only; cleans expired input materials only.
- `/codex clean task <task_id>`: admin-only; deletes the task record and task
  directory. It refuses active tasks.
- `/codex clean session <session_name>`: admin-only; first step only shows
  impact and prints the confirm command.
- `/codex clean session <session_name> confirm`: admin-only; deletes the
  session record and associated task records/workspaces.

Input-file cleanup only deletes files under the task `workspace/input/`
directory. It must not delete artifacts, logs, or arbitrary paths outside
`local_codex.work_root`.

## Reply File Input

Stable user flow:

```text
User uploads a QQ file.
User replies to that file message with: /codex <task>
```

The plugin tries these sources:

- MaiBot `message.get_by_id`
- NapCat `get_msg`
- file segment local path
- file segment URL
- NapCat `get_file`

Imported files go to:

```text
workspace/input/
```

Do not implement "guess the latest uploaded file" behavior without explicit
user confirmation. It is unsafe in group chats.

## Artifact Behavior

Artifacts are discovered by `local_codex.artifact_globs`, defaulting to:

```toml
["artifacts/*", "*.docx", "*.pdf", "*.md", "*.zip", "*.xlsx", "*.pptx"]
```

Recommended output directory for Codex tasks:

```text
workspace/artifacts/
```

Each task gets its own workspace, so stale artifacts from an older task should
not be uploaded unless a future change reuses workspaces. Avoid workspace reuse.

## Skills And MCP

`/codex skills` scans:

```text
CODEX_HOME/skills/**/SKILL.md
```

Descriptions are Chinese-first:

- known skills use built-in Chinese descriptions
- existing Chinese metadata is preserved
- unknown English-only descriptions are shortened and marked as lacking a
  Chinese description

Do not call an LLM on every `/codex skills` request. If AI translation is added,
prefer an explicit refresh command and a persistent cache.

`/codex mcp` calls:

```bash
codex mcp list --json
```

MCP descriptions are Chinese-first using known-name mappings and generic
transport descriptions.

## Configuration Rules

When adding a config field:

1. Add it to the correct `PluginConfigBase` model.
2. Add it to `config.toml` with a Chinese comment.
3. Update `/codex config` if it is user-visible runtime state.
4. Update README and AI docs.
5. Decide whether `on_config_update` must refresh clients or background tasks.

Hot update:

- MaiBot WebUI/plugin config updates call `on_config_update`.
- Direct file edits may require a MaiBot restart depending on the host loader.
- Current hot-update-sensitive parts:
  - remote client
  - NapCat client
  - periodic cleanup task

## Manifest Rules

Keep `_manifest.json` aligned with official plugin repository expectations:

- `manifest_version = 2`
- `id = "arcczr.remote-codex-agent"`
- `license = "MIT"` and root `LICENSE` exists
- author and URLs point to the public plugin repository
- dependencies include `httpx`
- capabilities include currently used SDK capabilities:
  - `send.text`
  - `send.custom`
  - `message.get_by_id`

If `PLUGIN_ID` changes, update both `_manifest.json` and `plugin.py`.

## Validation Commands

Run before committing:

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

Useful local checks:

```bash
git status --short --branch
git diff --stat
git log --oneline --decorate -n 8
```

## Git Workflow

The user may keep local and remote changes moving independently. Before commits
or pushes:

- Check `git status --short --branch`.
- If local is behind remote, inspect the remote commit before rebasing or
  merging.
- Prefer non-interactive commands.
- Do not force-push unless explicitly instructed.
- Do not revert user changes unless explicitly requested.

If asked to leave pushing to the user, stop after local commits and provide the
exact `git push` command.

## Known Operational Pitfalls

- A manually typed `@bot /codex` may not match the command regex. Use `/codex`
  at the start of the message for reliable triggering.
- `codex exec` cannot accept in-flight steering after stdin closes.
- BaoTa/systemd/Docker/VS Code shells may use different `HOME`, `PATH`, and
  `CODEX_HOME`. Test Codex as the same OS user that starts MaiBot.
- NapCat local file upload requires path visibility from the NapCat process.
- QQ does not render Markdown well; plugin-generated QQ messages should be
  plain text.
- Session names are parsed as one token. Avoid spaces in session names.
