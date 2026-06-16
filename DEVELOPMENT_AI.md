# AI Development Guide

This document is written for AI agents that need to maintain or extend the
`remote_codex_agent` plugin.

## Repository Scope

The plugin is currently implemented as a single `plugin.py` plus configuration
and documentation files. Keep changes scoped to this plugin directory unless the
user explicitly asks to modify MaiBot core or adapter code.

Tracked source files should normally be:

- `.gitignore`
- `LICENSE`
- `README.md`
- `_manifest.json`
- `config.toml`
- `plugin.py`
- AI-facing docs such as this file

Do not track:

- `.env.local`
- `__pycache__/`
- copied external docs
- local runbooks
- task workspaces
- generated artifacts
- `_records/`

## Current Architecture

Main components in `plugin.py`:

- Config models: `PluginSectionConfig`, `ServerConfig`,
  `PermissionConfig`, `TaskConfig`, `LocalCodexConfig`, `ProgressConfig`,
  `ArtifactConfig`, `NapCatConfig`, `InputFileConfig`,
  `RemoteCodexAgentConfig`
- Runtime models: `InputFile`, `RemoteTaskState`
- HTTP clients: `RemoteAgentClient`, `NapCatUploadClient`
- Main plugin: `RemoteCodexAgentPlugin`
- Command entry: `handle_codex_command`
- Local execution: `_create_local_task`, `_run_local_codex_task`,
  `_build_local_codex_command`, `_consume_local_stdout`,
  `_collect_local_artifacts`
- Records/session logic: `_record_task_state`, `_load_task_record`,
  `_load_session_record`, `_update_session_from_task`,
  `_hydrate_session_history`
- Reply-file import: `_prepare_reply_input_files`,
  `_extract_reply_message_id`, `_extract_file_segments`,
  `_import_input_file_segment`
- User commands: `_handle_status`, `_handle_cancel`, `_handle_skills`,
  `_handle_mcp`, `_handle_config`, `_handle_list`,
  `_handle_session_command`, `_handle_continue_command`,
  `_handle_resume_command`

`plugin.py` is large. Avoid unrelated refactors during feature work. If a future
task explicitly asks for splitting, a reasonable split is:

- `config.py`
- `models.py`
- `clients.py`
- `records.py`
- `input_files.py`
- `local_codex.py`
- `commands.py`

Do not split opportunistically while fixing a bug.

## Public Command Surface

Supported prefixes:

```text
/codex
/agent
```

Important commands:

```text
/codex <task>
/codex help
/codex status [task_id]
/codex cancel <task_id>
/codex list
/codex skills
/codex mcp
/codex config
/codex session <name> <task>
/codex session <task_id> confirm [name]
/codex continue <task>
/codex resume <task_id|session|thread_id> <task>
```

When changing command behavior, update:

- `_build_help_text`
- README
- deployment/development docs if operational behavior changes

## Local Codex Invocation

Local mode builds commands similar to:

```bash
codex -a never -s workspace-write -C <workspace> exec --json --color never \
  --skip-git-repo-check --output-last-message <final.md> -
```

Important details:

- `--search` is appended before `exec` when
  `local_codex.enable_search = true`.
- `resume_thread_id` uses `codex exec resume <thread_id>`.
- Prompt is written to stdin and then stdin is closed.
- Current `codex exec` mode is non-interactive; it cannot accept user steering
  during an active run.
- True in-flight steering would require a separate app-server mode using
  `codex app-server` and `turn/steer`.

## Progress and Final Replies

Local stdout is JSONL. The plugin extracts `item.completed` events whose item
type is `agent_message`.

Duplicate-answer mitigation:

- Agent messages are queued one item behind with `_queue_local_progress`.
- On process completion, `_discard_pending_local_progress` drops the final
  queued message because it is usually the final answer.
- Final output is read from `final.md` through `_read_local_final_message`.

When modifying progress handling, test both:

- file-producing tasks
- pure Q&A tasks with no artifacts

The QQ side does not render Markdown reliably. Keep user-facing plugin messages
plain, concise, and Chinese-first.

## Records and Sessions

Task records live under:

```text
<work_root>/_records/tasks/
```

Session records live under:

```text
<work_root>/_records/sessions/
```

Session records should preserve:

- latest task fields for `continue`
- `latest_task_id`
- `codex_thread_id`
- `task_ids`
- `history`

`_hydrate_session_history` exists for migration from older session records that
only kept the latest task. Do not remove it unless a formal migration replaces
it.

Normal task cleanup must not delete session records. It currently cleans records
only, not task workspace directories.

## File Input Behavior

Stable user flow:

```text
Reply to a QQ file message with /codex <task>
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

Do not implement "guess the latest uploaded file" behavior without explicit user
confirmation. It is unsafe in group chats.

## Artifact Behavior

Artifacts are discovered by `local_codex.artifact_globs`, defaulting to:

```toml
["artifacts/*", "*.docx", "*.pdf", "*.md", "*.zip", "*.xlsx", "*.pptx"]
```

Recommended output directory for Codex tasks:

```text
workspace/artifacts/
```

When changing artifact scan/upload logic, avoid re-uploading stale artifacts
from previous task directories. Each local task has its own workspace.

## Skills and MCP Listing

`/codex skills` scans:

```text
CODEX_HOME/skills/**/SKILL.md
```

Descriptions are Chinese-first:

- Known skills use built-in Chinese descriptions.
- Existing Chinese metadata is preserved.
- Unknown English-only descriptions are shortened and marked as lacking a
  Chinese description.

Do not call an LLM on every `/codex skills` request. If AI translation is added,
prefer an explicit refresh command and a persistent cache.

`/codex mcp` calls:

```bash
codex mcp list --json
```

MCP descriptions are also Chinese-first using known-name mappings and generic
transport descriptions.

## Configuration Rules

If adding a config field:

1. Add it to the appropriate `PluginConfigBase` model.
2. Add it to `config.toml` with a clear Chinese comment.
3. If user-visible, include it in `/codex config` or docs.
4. Consider whether `on_config_update` needs to refresh related clients.

Hot update:

- WebUI/plugin config updates call `on_config_update`.
- Direct file edits may require restart depending on MaiBot's config loader.

## Manifest Rules

Keep `_manifest.json` consistent with official plugin repository expectations:

- `manifest_version = 2`
- `id = "arcczr.remote-codex-agent"`
- author and URLs point to the public plugin repository
- `license = "MIT"` and root `LICENSE` exists
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
- Running MaiBot from BaoTa/systemd may use a different environment from an SSH
  or VS Code shell. Check `HOME`, `CODEX_HOME`, `PATH`, and `which codex`.
- NapCat local file upload requires path visibility from the NapCat process.
- QQ does not render Markdown well; plugin-generated QQ messages should be plain
  text.
