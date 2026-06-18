"""Codex CLI QQ 调度插件。

这个文件是插件的主体实现，核心目标是把 QQ 里的 /codex 指令转换成一次
Codex CLI 任务，并把进度、最终结果、产物文件回传到 QQ。

默认走 local 模式：在 MaiBot 所在服务器上直接调用本机 Codex CLI。
remote 模式保留给独立 HTTP Agent 服务，适合以后把执行机拆出去。

几个容易踩坑的设计点：
- QQ 不渲染 Markdown，所以所有发回 QQ 的文本都尽量压成纯文本。
- QQ 群临时私聊在当前 adapter 路由里不稳定，可能把回复发回群里，因此默认拒绝作为命令入口。
- /codex --dm 只表示“阶段性进度尝试私聊”，是否把创建提示、最终结果和产物也私聊由配置单独控制。
- Codex 子进程只应该在任务 workspace 内工作，避免读取服务器上任意本地路径。
"""

from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from typing import Any, ClassVar, Dict, List, Optional
from uuid import uuid4
from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

import asyncio
import httpx
import json
import time


PLUGIN_ID = "arcczr.remote-codex-agent"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "running"}
SUPPORTED_COMMAND_PREFIXES = ("/codex", "/agent")
STATUS_ALIASES = {
    "complete": "succeeded",
    "completed": "succeeded",
    "done": "succeeded",
    "ok": "succeeded",
    "success": "succeeded",
    "error": "failed",
    "failure": "failed",
    "canceled": "cancelled",
}


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__: ClassVar[str] = "基础设置"
    __ui_icon__: ClassVar[str] = "bot"
    __ui_order__: ClassVar[int] = 0

    config_version: str = Field(default="0.5.0", description="配置版本号")
    enabled: bool = Field(default=True, description="是否启用插件")


class ServerConfig(PluginConfigBase):
    """远程服务配置。"""

    __ui_label__: ClassVar[str] = "远程服务"
    __ui_icon__: ClassVar[str] = "server"
    __ui_order__: ClassVar[int] = 1

    base_url: str = Field(default="", description="远程 Ubuntu Agent 服务地址")
    api_token: str = Field(default="", description="远程服务鉴权 token")
    require_api_token: bool = Field(default=True, description="是否要求配置远程服务 token")
    create_path: str = Field(default="/v1/tasks", description="创建任务接口路径")
    status_path_template: str = Field(default="/v1/tasks/{task_id}", description="查询任务接口路径模板")
    cancel_path_template: str = Field(default="/v1/tasks/{task_id}/cancel", description="取消任务接口路径模板")
    request_timeout_seconds: float = Field(default=20.0, description="HTTP 请求超时秒数")
    verify_tls: bool = Field(default=True, description="是否校验 HTTPS 证书")


class PermissionConfig(PluginConfigBase):
    """触发权限配置。"""

    __ui_label__: ClassVar[str] = "权限"
    __ui_icon__: ClassVar[str] = "shield"
    __ui_order__: ClassVar[int] = 2

    # allow_all_users 是用户维度的总开关。开启后默认所有用户能用；
    # 但 user_list_mode=blacklist 时，trigger_users 里的用户仍会被拦截。
    # 关闭后必须进入旧 allowed_users 或新 trigger_users 白名单。
    allow_all_users: bool = Field(default=False, description="是否允许所有用户触发")
    allowed_users: List[str] = Field(default_factory=list, description="允许触发的用户，推荐格式 qq:用户ID")
    user_list_mode: str = Field(default="whitelist", description="用户列表模式：whitelist 或 blacklist")
    trigger_users: List[str] = Field(default_factory=list, description="用户黑白名单，推荐格式 qq:用户ID")
    admin_users: List[str] = Field(default_factory=list, description="管理员用户，允许使用高危权限")
    allowed_groups: List[str] = Field(default_factory=list, description="允许触发的群号、qq:群号 或 stream_id")
    # “聊天流”是 MaiBot 对会话来源的抽象，可能是群、私聊或 adapter 生成的 stream_id。
    # 用户权限和聊天流权限同时生效，两边都通过才允许触发。
    chat_list_mode: str = Field(default="whitelist", description="聊天流列表模式：whitelist 或 blacklist")
    trigger_chats: List[str] = Field(default_factory=list, description="聊天流黑白名单，可写群号、qq:群号 或 stream_id")
    reject_temporary_private_chat: bool = Field(default=True, description="是否拒绝 QQ 群临时私聊触发")


class TaskConfig(PluginConfigBase):
    """任务配置。"""

    __ui_label__: ClassVar[str] = "任务"
    __ui_icon__: ClassVar[str] = "terminal"
    __ui_order__: ClassVar[int] = 3

    command_prefix: str = Field(default="/codex", description="帮助文本默认展示的主命令前缀")
    execution_mode: str = Field(default="local", description="执行模式：local 或 remote")
    enable_cancel: bool = Field(default=True, description="是否允许取消远程任务")
    task_type: str = Field(default="codex_cli", description="提交给远程服务的任务类型")
    max_running_tasks_per_stream: int = Field(default=1, description="单个聊天流同时运行的最大任务数")
    max_running_tasks_per_user: int = Field(default=1, description="单个用户同时运行的最大任务数")
    poll_interval_seconds: float = Field(default=5.0, description="轮询远程任务状态间隔")
    max_watch_seconds: float = Field(default=3600.0, description="单个任务最长跟踪时间")
    resumable_task_ttl_hours: float = Field(default=24.0, description="普通 task 可继续对话的保留小时数")
    require_session_confirm: bool = Field(default=True, description="把 task 转为 session 时是否要求二次确认")
    auto_cleanup_task_records: bool = Field(default=True, description="启动时是否自动清理过期普通 task 记录")
    auto_cleanup_task_workspaces: bool = Field(default=False, description="自动清理过期 task 记录时是否同时删除 task 目录和文件")
    enable_periodic_cleanup: bool = Field(default=False, description="是否启用后台定时清理")
    periodic_cleanup_interval_minutes: float = Field(default=60.0, description="后台定时清理间隔分钟数")


class LocalCodexConfig(PluginConfigBase):
    """本机 Codex CLI 配置。"""

    __ui_label__: ClassVar[str] = "本地 Codex"
    __ui_icon__: ClassVar[str] = "terminal"
    __ui_order__: ClassVar[int] = 4

    codex_binary: str = Field(default="codex", description="Codex CLI 可执行文件名或绝对路径")
    work_root: str = Field(default="data/tasks", description="本地任务根目录；相对路径按插件目录解析")
    sandbox: str = Field(default="workspace-write", description="Codex CLI 沙箱模式")
    approval_policy: str = Field(default="never", description="Codex CLI 审批策略")
    model: str = Field(default="", description="可选模型名")
    enable_search: bool = Field(default=False, description="是否启用 Codex CLI --search")
    extra_args: List[str] = Field(default_factory=list, description="额外传给 codex exec 的参数")
    process_timeout_seconds: float = Field(default=3600.0, description="本地 Codex 任务运行超时")
    artifact_globs: List[str] = Field(
        default_factory=lambda: ["artifacts/*", "*.docx", "*.pdf", "*.md", "*.zip", "*.xlsx", "*.pptx"],
        description="任务 workspace 内产物匹配规则",
    )


class ProgressConfig(PluginConfigBase):
    """进度转发配置。"""

    __ui_label__: ClassVar[str] = "进度"
    __ui_icon__: ClassVar[str] = "activity"
    __ui_order__: ClassVar[int] = 5

    forward_progress: bool = Field(default=True, description="是否把运行进度转发到 QQ")
    min_send_interval_seconds: float = Field(default=5.0, description="进度消息最小发送间隔")
    max_progress_items_per_message: int = Field(default=5, description="每次最多合并多少条进度")
    max_progress_item_chars: int = Field(default=300, description="单条进度最大字符数")
    max_summary_chars: int = Field(default=1800, description="最终摘要最大字符数")
    # 私聊进度依赖 NapCat HTTP API 的 send_private_msg。
    # QQ 侧通常要求用户先主动私聊过机器人，否则可能发送失败。
    enable_private_progress: bool = Field(default=False, description="是否允许用户用参数请求私聊接收阶段性进度")
    private_progress_trigger_args: List[str] = Field(
        default_factory=lambda: ["--dm", "--private-progress"],
        description="触发私聊进度的命令参数",
    )
    private_progress_fallback_to_origin: bool = Field(default=True, description="私聊进度发送失败时是否回退到原聊天流")
    private_progress_send_task_created: bool = Field(default=False, description="使用私聊进度时是否把任务创建提示也私聊发送")
    private_progress_send_artifacts: bool = Field(default=False, description="使用私聊进度时是否把最终结果和产物也私聊发送")


class ArtifactConfig(PluginConfigBase):
    """产物回传配置。"""

    __ui_label__: ClassVar[str] = "产物"
    __ui_icon__: ClassVar[str] = "file-text"
    __ui_order__: ClassVar[int] = 6

    send_artifact_links: bool = Field(default=True, description="完成时是否发送产物列表或下载链接")
    try_custom_file_message: bool = Field(default=False, description="是否尝试用 send.custom 发送文件消息")
    custom_file_message_type: str = Field(default="file", description="自定义文件消息类型")


class NapCatConfig(PluginConfigBase):
    """NapCat 直连配置。"""

    __ui_label__: ClassVar[str] = "NapCat 直传"
    __ui_icon__: ClassVar[str] = "upload"
    __ui_order__: ClassVar[int] = 7

    enabled: bool = Field(default=False, description="是否启用 NapCat HTTP API 直接上传产物文件")
    scheme: str = Field(default="http", description="NapCat HTTP Server 协议：http 或 https")
    host: str = Field(default="127.0.0.1", description="NapCat HTTP Server 地址")
    port: int = Field(default=9998, description="NapCat HTTP Server 端口")
    token: str = Field(default="", description="NapCat HTTP Server token，留空则不发送鉴权头")
    request_timeout_seconds: float = Field(default=120.0, description="NapCat 上传请求超时秒数")
    upload_file: bool = Field(default=True, description="调用 upload_*_file 时是否执行真实上传")
    max_file_size_mb: float = Field(default=100.0, description="单个产物最大上传大小，0 表示不限制")


class InputFileConfig(PluginConfigBase):
    """用户输入文件配置。"""

    __ui_label__: ClassVar[str] = "输入文件"
    __ui_icon__: ClassVar[str] = "paperclip"
    __ui_order__: ClassVar[int] = 8

    enable_reply_file: bool = Field(default=True, description="是否允许回复 QQ 文件消息创建带材料的 Codex 任务")
    input_dir_name: str = Field(default="input", description="输入材料放入 workspace 下的目录名")
    max_files_per_task: int = Field(default=5, description="单个任务最多导入多少个文件")
    max_file_size_mb: float = Field(default=100.0, description="单个输入文件最大大小，0 表示不限制")
    allow_url_download: bool = Field(default=True, description="是否允许从 QQ 文件消息中的 HTTP URL 下载输入文件")
    allowed_local_roots: List[str] = Field(default_factory=list, description="允许复制的本地文件根目录，空列表表示禁止复制本地路径")
    auto_cleanup_input_files: bool = Field(default=True, description="启动时是否自动清理过期输入材料")
    input_file_ttl_hours: float = Field(default=24.0, description="输入材料保留小时数，0 表示不自动清理")


class RemoteCodexAgentConfig(PluginConfigBase):
    """远程 Codex Agent 插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig, description="插件基础配置")
    server: ServerConfig = Field(default_factory=ServerConfig, description="远程服务配置")
    permission: PermissionConfig = Field(default_factory=PermissionConfig, description="触发权限配置")
    task: TaskConfig = Field(default_factory=TaskConfig, description="任务配置")
    local_codex: LocalCodexConfig = Field(default_factory=LocalCodexConfig, description="本机 Codex CLI 配置")
    progress: ProgressConfig = Field(default_factory=ProgressConfig, description="进度转发配置")
    artifact: ArtifactConfig = Field(default_factory=ArtifactConfig, description="产物回传配置")
    napcat: NapCatConfig = Field(default_factory=NapCatConfig, description="NapCat 直连配置")
    input_file: InputFileConfig = Field(default_factory=InputFileConfig, description="输入文件配置")


@dataclass
class InputFile:
    """导入到 Codex workspace 的用户材料文件。"""

    name: str
    path: str
    size: int = 0
    source: str = ""


@dataclass
class RemoteTaskState:
    """插件侧跟踪的远程任务状态。"""

    task_id: str
    stream_id: str
    platform: str
    user_id: str
    group_id: str
    prompt: str
    created_at: float = field(default_factory=time.monotonic)
    last_status: str = "queued"
    last_progress_cursor: str = ""
    sent_progress_ids: set[str] = field(default_factory=set)
    last_progress_sent_at: float = 0.0
    pending_local_progress_text: str = ""
    watch_task: Optional[asyncio.Task[None]] = None
    process: Optional[asyncio.subprocess.Process] = None
    workspace_dir: str = ""
    final_message_path: str = ""
    input_files: List[InputFile] = field(default_factory=list)
    codex_thread_id: str = ""
    record_type: str = "task"
    session_name: str = ""
    parent_task_id: str = ""
    private_progress: bool = False
    private_progress_user_id: str = ""
    private_progress_failure_notified: bool = False


class RemoteAgentClient:
    """远程 Ubuntu Agent 服务客户端。"""

    def __init__(self, config: RemoteCodexAgentConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def update_config(self, config: RemoteCodexAgentConfig) -> None:
        """更新配置；下次请求会使用新配置。"""

        self._config = config

    def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端。"""

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=max(float(self._config.server.request_timeout_seconds), 1.0),
                verify=bool(self._config.server.verify_tls),
            )
        return self._client

    def _build_url(self, path: str) -> str:
        """拼接远程接口 URL。"""

        base_url = self._config.server.base_url.strip().rstrip("/")
        normalized_path = path.strip()
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        if not base_url:
            raise ValueError("未配置远程服务地址 server.base_url")
        return f"{base_url}{normalized_path}"

    def _headers(self) -> Dict[str, str]:
        """构造请求头。"""

        headers = {"Content-Type": "application/json"}
        token = self._config.server.api_token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def create_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """创建远程任务。"""

        response = await self._get_client().post(
            self._build_url(self._config.server.create_path),
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("远程服务创建任务响应不是 JSON 对象")
        return data

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        """查询远程任务状态。"""

        path = self._config.server.status_path_template.format(task_id=task_id)
        response = await self._get_client().get(self._build_url(path), headers=self._headers())
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("远程服务任务状态响应不是 JSON 对象")
        return data

    async def cancel_task(self, task_id: str) -> Dict[str, Any]:
        """请求取消远程任务。"""

        path = self._config.server.cancel_path_template.format(task_id=task_id)
        response = await self._get_client().post(self._build_url(path), headers=self._headers(), json={})
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("远程服务取消任务响应不是 JSON 对象")
        return data


class NapCatUploadClient:
    """NapCat HTTP API 文件上传客户端。"""

    def __init__(self, config: RemoteCodexAgentConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def update_config(self, config: RemoteCodexAgentConfig) -> None:
        """更新配置；下次请求使用新配置。"""

        self._config = config

    def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端。"""

        if self._client is None:
            timeout = max(float(self._config.napcat.request_timeout_seconds), 1.0)
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    def _build_url(self, action: str) -> str:
        """构造 NapCat HTTP action URL。"""

        scheme = str(self._config.napcat.scheme or "http").strip().lower()
        if scheme not in {"http", "https"}:
            raise ValueError("napcat.scheme 只能是 http 或 https")
        host = str(self._config.napcat.host or "").strip()
        port = int(self._config.napcat.port)
        if not host:
            raise ValueError("未配置 napcat.host")
        return f"{scheme}://{host}:{port}/{action.strip('/')}"

    def _headers(self) -> Dict[str, str]:
        """构造请求头。"""

        headers = {"Content-Type": "application/json"}
        token = str(self._config.napcat.token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def upload_artifact(
        self,
        task_state: RemoteTaskState,
        artifact: Dict[str, Any],
        force_private: bool = False,
    ) -> Dict[str, Any]:
        """上传一个产物到当前 QQ 群聊或私聊。"""

        file_value = self._extract_file_value(artifact)
        name = self._extract_name(artifact, file_value)
        self._check_file_size(artifact, file_value)

        # 默认产物回到任务来源：群里触发就发群文件，私聊触发就发私聊文件。
        # force_private 用于 /codex --dm 且配置要求“产物也私聊”的场景。
        group_id = str(task_state.group_id or "").strip()
        user_id = str(task_state.user_id or "").strip()
        if group_id and not force_private:
            action = "upload_group_file"
            payload: Dict[str, Any] = {
                "group_id": group_id,
                "file": file_value,
                "name": name,
                "upload_file": bool(self._config.napcat.upload_file),
            }
        elif user_id:
            action = "upload_private_file"
            payload = {
                "user_id": user_id,
                "file": file_value,
                "name": name,
                "upload_file": bool(self._config.napcat.upload_file),
            }
        else:
            raise ValueError("无法获取当前聊天的 group_id 或 user_id，不能直传文件")

        response = await self._get_client().post(self._build_url(action), headers=self._headers(), json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("NapCat 上传响应不是 JSON 对象")
        if data.get("status") != "ok":
            message = data.get("message") or data.get("wording") or data
            raise RuntimeError(f"NapCat 上传失败：{message}")
        return data

    @staticmethod
    def _extract_file_value(artifact: Dict[str, Any]) -> str:
        """从产物信息中提取可交给 NapCat 的文件路径或 URL。"""

        for key in ("path", "file", "url", "download_url"):
            value = str(artifact.get(key) or "").strip()
            if value:
                return value
        raise ValueError("产物缺少 path/file/url/download_url，无法直传")

    @staticmethod
    def _extract_name(artifact: Dict[str, Any], file_value: str) -> str:
        """提取 QQ 文件显示名。"""

        name = str(artifact.get("name") or artifact.get("filename") or "").strip()
        if name:
            return name
        return Path(file_value).name or "codex_artifact"

    def _check_file_size(self, artifact: Dict[str, Any], file_value: str) -> None:
        """检查上传大小限制。"""

        max_file_size_mb = float(self._config.napcat.max_file_size_mb)
        if max_file_size_mb <= 0:
            return

        size = artifact.get("size") or artifact.get("size_bytes")
        if not isinstance(size, (int, float)):
            path = Path(file_value)
            if path.exists() and path.is_file():
                size = path.stat().st_size
        if isinstance(size, (int, float)) and size > max_file_size_mb * 1024 * 1024:
            raise ValueError(f"产物超过 napcat.max_file_size_mb 限制：{size:.0f} bytes")

    async def call_action(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """调用一个 NapCat HTTP action。"""

        response = await self._get_client().post(self._build_url(action), headers=self._headers(), json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"NapCat {action} 响应不是 JSON 对象")
        status = data.get("status")
        retcode = data.get("retcode")
        if (status is not None and status != "ok") or (retcode is not None and retcode != 0):
            message = data.get("message") or data.get("wording") or data
            raise RuntimeError(f"NapCat {action} 失败：{message}")
        return data


def _normalize_set(values: List[str]) -> set[str]:
    """规范化字符串列表为集合。"""

    return {str(value or "").strip().lower() for value in values if str(value or "").strip()}


def _truncate_text(text: str, max_chars: int) -> str:
    """按字符数截断文本。"""

    normalized_text = str(text or "").strip()
    if max_chars <= 0 or len(normalized_text) <= max_chars:
        return normalized_text
    return f"{normalized_text[: max_chars - 1]}…"


def _plain_qq_text(text: str) -> str:
    """清理 Markdown 风格标记，保留适合 QQ 展示的纯文本。"""

    cleaned = str(text or "").replace("\r", "\n").strip()
    replacements = {
        "```": "",
        "`": "",
        "**": "",
        "__": "",
        "### ": "",
        "## ": "",
        "# ": "",
        "> ": "",
    }
    for source, target in replacements.items():
        cleaned = cleaned.replace(source, target)
    lines = [" ".join(line.split()) for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _sanitize_path_text(text: str, replacements: Dict[str, str]) -> str:
    """把面向用户的文本中的本机绝对路径替换为相对描述。"""

    cleaned = str(text or "")
    ordered_items = sorted(
        ((str(source or ""), str(target or "")) for source, target in replacements.items() if str(source or "")),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for source, target in ordered_items:
        cleaned = cleaned.replace(source, target)
    return re.sub(r"/[^\s，。；：、]+/workspace/artifacts/([^\s，。；：、]+)", r"workspace/artifacts/\1", cleaned)


def _format_progress_message(task_id: str, progress_lines: List[str], max_chars: int) -> str:
    """格式化 QQ 进度消息。"""

    display_lines = []
    for line in progress_lines:
        cleaned = _plain_qq_text(line)
        if cleaned:
            display_lines.append(_truncate_text(cleaned, max_chars))

    if not display_lines:
        display_lines = ["正在处理任务。"]

    now_text = datetime.now().strftime("%H:%M:%S")
    body = "\n\n".join(f"{index}. {line}" for index, line in enumerate(display_lines, start=1))
    return (
        "任务进度\n"
        f"时间：{now_text}\n"
        f"任务ID：{task_id}\n"
        "\n"
        f"{body}"
    )


def _looks_like_final_progress(text: str) -> bool:
    """判断 Codex 流式输出是否已经是最终总结。"""

    cleaned = _plain_qq_text(text)
    if not cleaned:
        return False
    markers = ("产物路径", "产物：", "产物:", "artifact", "artifacts/")
    if not any(marker.lower() in cleaned.lower() for marker in markers):
        return False
    final_markers = ("已完成", "已生成", "已读取", "已整理", "生成结果", "完成")
    return any(marker in cleaned for marker in final_markers)


def _contains_cjk(text: str) -> bool:
    """判断文本是否包含中文或其他 CJK 字符。"""

    return bool(re.search(r"[\u3400-\u9fff]", str(text or "")))


def _display_task_kind(task_id: str) -> str:
    """返回用户可见的任务类型。"""

    if str(task_id or "").startswith("local_"):
        return "本机 Codex 任务"
    return "远程 Codex 任务"


def _coerce_progress_items(raw_progress: Any) -> List[Dict[str, str]]:
    """将远程服务返回的进度字段归一化为列表。"""

    if raw_progress is None:
        return []
    if isinstance(raw_progress, str):
        raw_items: List[Any] = [raw_progress]
    elif isinstance(raw_progress, list):
        raw_items = raw_progress
    else:
        return []

    progress_items: List[Dict[str, str]] = []
    for index, item in enumerate(raw_items):
        if isinstance(item, str):
            text = item.strip()
            item_id = text
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("message") or item.get("content") or "").strip()
            item_id = str(item.get("id") or item.get("seq") or item.get("sequence") or item.get("cursor") or "").strip()
            if not item_id:
                item_id = f"{index}:{text}"
        else:
            continue
        if text:
            progress_items.append({"id": item_id, "text": text})
    return progress_items


def _coerce_artifacts(raw_artifacts: Any) -> List[Dict[str, Any]]:
    """将远程服务返回的产物字段归一化为列表。"""

    if isinstance(raw_artifacts, dict):
        return [dict(raw_artifacts)]
    if not isinstance(raw_artifacts, list):
        return []
    return [dict(item) for item in raw_artifacts if isinstance(item, dict)]


class RemoteCodexAgentPlugin(MaiBotPlugin):
    """通过远程服务运行 Codex CLI 的调度插件。"""

    config_model = RemoteCodexAgentConfig

    def __init__(self) -> None:
        super().__init__()
        self._client = RemoteAgentClient(RemoteCodexAgentConfig())
        self._napcat_client = NapCatUploadClient(RemoteCodexAgentConfig())
        self._tasks: Dict[str, RemoteTaskState] = {}
        self._cleanup_task: Optional[asyncio.Task[None]] = None

    async def on_load(self) -> None:
        """插件加载时初始化运行态。"""

        self._client.update_config(self.config)
        self._napcat_client.update_config(self.config)
        self._ensure_records_dirs()
        self._cleanup_expired_task_records()
        self._cleanup_expired_input_files()
        self._restart_periodic_cleanup_task()

    async def on_unload(self) -> None:
        """插件卸载时停止本地轮询任务。"""

        cleanup_task = self._stop_periodic_cleanup_task()
        for task_state in list(self._tasks.values()):
            if task_state.watch_task is not None and not task_state.watch_task.done():
                task_state.watch_task.cancel()
        await asyncio.gather(
            *[
                task_state.watch_task
                for task_state in self._tasks.values()
                if task_state.watch_task is not None
            ],
            return_exceptions=True,
        )
        if cleanup_task is not None:
            await asyncio.gather(cleanup_task, return_exceptions=True)
        await self._client.close()
        await self._napcat_client.close()

    async def on_config_update(self, scope: str, config_data: Dict[str, object], version: str) -> None:
        """处理配置热重载。"""

        del scope, config_data, version
        await self._client.close()
        await self._napcat_client.close()
        self._client.update_config(self.config)
        self._napcat_client.update_config(self.config)
        self._restart_periodic_cleanup_task()

    def _restart_periodic_cleanup_task(self) -> None:
        """按配置重启后台定时清理任务。"""

        self._stop_periodic_cleanup_task()
        if not self.config.task.enable_periodic_cleanup:
            return
        self._cleanup_task = asyncio.create_task(self._run_periodic_cleanup(), name="remote_codex_agent:cleanup")

    def _stop_periodic_cleanup_task(self) -> Optional[asyncio.Task[None]]:
        """停止后台定时清理任务。"""

        cleanup_task = self._cleanup_task
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        self._cleanup_task = None
        return cleanup_task

    async def _run_periodic_cleanup(self) -> None:
        """定时清理过期 task 记录、workspace 和输入材料。"""

        try:
            while True:
                interval_seconds = max(float(self.config.task.periodic_cleanup_interval_minutes), 1.0) * 60.0
                await asyncio.sleep(interval_seconds)
                try:
                    task_result = self._cleanup_expired_task_records()
                    input_result = self._cleanup_expired_input_files()
                    if any(task_result.values()) or any(input_result.values()):
                        self.ctx.logger.info(
                            "Codex 定时清理完成: task_records=%s, task_workspaces=%s, input_files=%s, input_dirs=%s",
                            task_result.get("records", 0),
                            task_result.get("workspaces", 0),
                            input_result.get("files", 0),
                            input_result.get("dirs", 0),
                        )
                except Exception as exc:
                    self.ctx.logger.warning("Codex 定时清理失败: %s", exc)
        except asyncio.CancelledError:
            return

    @Command(
        "remote_codex_agent",
        description="触发远程 Ubuntu Codex CLI 任务",
        pattern=r"^\s*(?:\[回复<[^>]+>：[\s\S]*?\]，说：\s*)?(?:@<[^>]+>\s*)*(?P<agent_command>/(?:codex|agent)(?:\s+[\s\S]*)?)$",
    )
    async def handle_codex_command(
        self,
        stream_id: str = "",
        platform: str = "",
        user_id: str = "",
        group_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /codex 命令。"""

        raw_command = self._extract_raw_command(matched_groups, kwargs)
        prefix = self._extract_command_prefix(raw_command)
        if prefix not in SUPPORTED_COMMAND_PREFIXES:
            return False, "命令前缀不匹配", False

        if not stream_id:
            return False, "无法获取当前聊天流 ID", True

        command_message = kwargs.get("message")
        # 群临时私聊在当前 NapCat adapter 里会带 private 类型，但同时携带 group_id。
        # 如果继续使用 ctx.send.text(stream_id)，回复可能被送回群聊。这里直接拒绝作为命令入口。
        # 提示消息也尽量走 NapCat 私聊，失败就静默，只在日志里留痕，避免再次污染群聊。
        if self.config.permission.reject_temporary_private_chat and self._is_temporary_private_chat(command_message):
            message = "当前是 QQ 群临时会话，消息路由可能不稳定。请在群聊中使用 /codex，或先主动私聊机器人后再使用。"
            await self._try_send_private_notice(user_id, message)
            return False, message, True

        permission_error = self._check_permission(platform=platform, user_id=user_id, group_id=group_id, stream_id=stream_id)
        if permission_error:
            await self.ctx.send.text(permission_error, stream_id)
            return False, permission_error, True

        command_body = raw_command[len(prefix) :].strip()
        if not command_body or command_body.lower() in {"help", "帮助"}:
            await self.ctx.send.text(self._build_help_text(prefix), stream_id)
            return True, "已发送帮助", True

        parts = command_body.split(maxsplit=1)
        sub_command = parts[0].lower()
        sub_arg = parts[1].strip() if len(parts) > 1 else ""

        if sub_command in {"status", "状态"}:
            await self._handle_status(stream_id=stream_id, task_id=sub_arg)
            return True, "已查询任务状态", True

        if sub_command in {"cancel", "取消"}:
            await self._handle_cancel(stream_id=stream_id, task_id=sub_arg)
            return True, "已处理取消命令", True

        if sub_command in {"skills", "skill", "技能"}:
            await self._handle_skills(stream_id=stream_id)
            return True, "已查询 Codex skills", True

        if sub_command == "mcp":
            await self._handle_mcp(stream_id=stream_id)
            return True, "已查询 Codex MCP", True

        if sub_command in {"config", "配置"}:
            await self._handle_config(stream_id=stream_id)
            return True, "已查询 Codex 配置", True

        if sub_command in {"list", "列表"}:
            await self._handle_list(stream_id=stream_id, platform=platform, user_id=user_id, arg=sub_arg)
            return True, "已查询 Codex 记录", True

        if sub_command in {"clean", "清理"}:
            await self._handle_clean(stream_id=stream_id, platform=platform, user_id=user_id, arg=sub_arg)
            return True, "已处理 Codex 清理命令", True

        if sub_command == "session":
            handled, message = await self._handle_session_command(
                arg=sub_arg,
                stream_id=stream_id,
                platform=platform,
                user_id=user_id,
                group_id=group_id,
                command_message=command_message,
            )
            return handled, message, True

        if sub_command in {"continue", "继续"}:
            handled, message = await self._handle_continue_command(
                prompt=sub_arg,
                stream_id=stream_id,
                platform=platform,
                user_id=user_id,
                group_id=group_id,
            )
            return handled, message, True

        if sub_command == "resume":
            handled, message = await self._handle_resume_command(
                arg=sub_arg,
                stream_id=stream_id,
                platform=platform,
                user_id=user_id,
                group_id=group_id,
                command_message=command_message,
            )
            return handled, message, True

        # 普通任务创建才在这里解析 --dm。session/continue/resume 在各自处理器里解析，
        # 这样可以把 --dm 从真正发给 Codex 的 prompt 里剥离掉，避免 Codex 把它当任务文本。
        parsed_prompt, private_progress = self._parse_private_progress_args(command_body)
        if not parsed_prompt:
            await self.ctx.send.text("用法：/codex --dm <任务描述>", stream_id)
            return False, "任务描述为空", True

        execution_mode = self._get_execution_mode()
        if execution_mode == "remote":
            if not self.config.server.base_url.strip():
                await self.ctx.send.text("远程 Codex Agent 服务地址未配置，请先设置 server.base_url。", stream_id)
                return False, "远程服务地址未配置", True

            if self.config.server.require_api_token and not self.config.server.api_token.strip():
                await self.ctx.send.text("远程 Codex Agent token 未配置，请先设置 server.api_token。", stream_id)
                return False, "远程服务 token 未配置", True
        elif execution_mode != "local":
            await self.ctx.send.text("执行模式配置不合法，请设置 task.execution_mode 为 local 或 remote。", stream_id)
            return False, "执行模式配置不合法", True

        limit_error = self._check_running_limit(stream_id=stream_id, platform=platform, user_id=user_id)
        if limit_error:
            await self.ctx.send.text(limit_error, stream_id)
            return False, limit_error, True

        path_error = self._check_prompt_local_path_access(parsed_prompt)
        if path_error:
            await self.ctx.send.text(path_error, stream_id)
            return False, path_error, True

        if execution_mode == "local":
            dangerous_error = self._check_dangerous_local_permission(platform=platform, user_id=user_id)
            if dangerous_error:
                await self.ctx.send.text(dangerous_error, stream_id)
                return False, dangerous_error, True
            return await self._create_local_task(
                prompt=parsed_prompt,
                raw_command=raw_command,
                stream_id=stream_id,
                platform=platform,
                user_id=user_id,
                group_id=group_id,
                command_message=command_message,
                private_progress=private_progress,
            )

        return await self._create_remote_task(
            prompt=parsed_prompt,
            raw_command=raw_command,
            stream_id=stream_id,
            platform=platform,
            user_id=user_id,
            group_id=group_id,
            private_progress=private_progress,
        )

    def _extract_raw_command(self, matched_groups: Optional[Dict[str, Any]], kwargs: Dict[str, Any]) -> str:
        """从命令参数中提取原始命令文本。"""

        if isinstance(matched_groups, dict):
            command = str(matched_groups.get("agent_command") or "").strip()
            if command:
                return command
        text = str(kwargs.get("text") or kwargs.get("raw_message") or "").strip()
        match = re.search(r"(?<!\S)/(?:codex|agent)(?:\s+[\s\S]*)?$", text, flags=re.IGNORECASE)
        if match:
            return match.group(0).strip()
        return text

    @staticmethod
    def _extract_command_prefix(raw_command: str) -> str:
        """提取命令前缀。"""

        return raw_command.split(maxsplit=1)[0].strip().lower()

    @staticmethod
    def _is_temporary_private_chat(message: Any) -> bool:
        """判断是否是 QQ 群临时私聊。"""

        # 命令桥传给插件的 message 可能是 SessionMessage 对象，也可能已经序列化成 dict。
        # 两种结构都要支持，否则临时私聊拦截会漏掉。
        if isinstance(message, dict):
            message_info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
            additional_config = message_info.get("additional_config") if isinstance(message_info, dict) else {}
            group_info = message_info.get("group_info") if isinstance(message_info, dict) else {}
        else:
            message_info = getattr(message, "message_info", None)
            additional_config = getattr(message_info, "additional_config", None)
            group_info = getattr(message_info, "group_info", None)
        if not isinstance(additional_config, dict):
            return False
        message_type = str(additional_config.get("napcat_message_type") or "").strip().lower()
        target_group_id = str(additional_config.get("platform_io_target_group_id") or "").strip()
        target_user_id = str(additional_config.get("platform_io_target_user_id") or "").strip()
        if isinstance(group_info, dict):
            group_info_id = str(group_info.get("group_id") or "").strip()
        else:
            group_info_id = str(getattr(group_info, "group_id", "") or "").strip()
        has_group_info = bool(target_group_id or group_info_id)
        return message_type == "private" and has_group_info and not target_user_id

    async def _try_send_private_notice(self, user_id: str, message: str) -> None:
        """尝试私聊发送提示，失败时静默拦截。"""

        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id or not self.config.napcat.enabled:
            self.ctx.logger.info("已拒绝 QQ 群临时私聊 Codex 指令，未发送提示：缺少 user_id 或 NapCat 未启用")
            return
        try:
            await self._napcat_client.call_action(
                "send_private_msg",
                {
                    "user_id": normalized_user_id,
                    "message": [{"type": "text", "data": {"text": message}}],
                },
            )
        except Exception as exc:
            self.ctx.logger.info("已拒绝 QQ 群临时私聊 Codex 指令，私聊提示发送失败: %s", exc)

    def _parse_private_progress_args(self, prompt: str) -> tuple[str, bool]:
        """识别并移除请求私聊进度的命令参数。"""

        if not self.config.progress.enable_private_progress:
            return prompt, False

        trigger_args = _normalize_set(self.config.progress.private_progress_trigger_args)
        if not trigger_args:
            return prompt, False

        kept_parts: List[str] = []
        enabled = False
        for part in str(prompt or "").split():
            if part.strip().lower() in trigger_args:
                enabled = True
                continue
            kept_parts.append(part)
        return " ".join(kept_parts).strip(), enabled

    def _check_permission(self, platform: str, user_id: str, group_id: str, stream_id: str) -> str:
        """检查当前用户和聊天流是否允许触发。"""

        permission = self.config.permission
        normalized_platform = str(platform or "").strip().lower()
        normalized_user_id = str(user_id or "").strip().lower()
        normalized_group_id = str(group_id or "").strip().lower()
        normalized_stream_id = str(stream_id or "").strip().lower()

        user_candidates = {normalized_user_id}
        if normalized_platform and normalized_user_id:
            user_candidates.add(f"{normalized_platform}:{normalized_user_id}")

        # 用户名单和聊天流名单是两道门：谁能用、在哪里能用。
        # allow_all_users=false 时，用户必须在白名单里；allow_all_users=true 时，
        # 仍然可以用 trigger_users 的 blacklist 模式单独封禁某些用户。
        legacy_allowed_users = _normalize_set(permission.allowed_users)
        trigger_users = _normalize_set(permission.trigger_users)
        user_list_mode = self._normalize_list_mode(permission.user_list_mode)
        if not permission.allow_all_users:
            allowed_users = legacy_allowed_users | (trigger_users if user_list_mode == "whitelist" else set())
            if not allowed_users or allowed_users.isdisjoint(user_candidates):
                return "你没有权限触发远程 Codex Agent。"
        elif user_list_mode == "whitelist" and trigger_users and trigger_users.isdisjoint(user_candidates):
            return "你没有权限触发远程 Codex Agent。"
        elif user_list_mode == "blacklist" and trigger_users and not trigger_users.isdisjoint(user_candidates):
            return "你没有权限触发远程 Codex Agent。"

        # 旧 allowed_groups 继续当白名单用，避免老配置升级后失效。
        # 新 trigger_chats 支持 whitelist/blacklist，两者都会参与判断。
        chat_candidates = self._build_chat_candidates(normalized_platform, normalized_group_id, normalized_stream_id)
        legacy_allowed_groups = _normalize_set(permission.allowed_groups)
        if legacy_allowed_groups:
            if legacy_allowed_groups.isdisjoint(chat_candidates):
                return "当前聊天流不允许触发远程 Codex Agent。"

        trigger_chats = _normalize_set(permission.trigger_chats)
        chat_list_mode = self._normalize_list_mode(permission.chat_list_mode)
        if chat_list_mode == "whitelist" and trigger_chats and trigger_chats.isdisjoint(chat_candidates):
            return "当前聊天流不允许触发远程 Codex Agent。"
        if chat_list_mode == "blacklist" and trigger_chats and not trigger_chats.isdisjoint(chat_candidates):
            return "当前聊天流不允许触发远程 Codex Agent。"

        return ""

    @staticmethod
    def _normalize_list_mode(value: str) -> str:
        """规范化黑白名单模式。"""

        normalized = str(value or "").strip().lower()
        if normalized in {"black", "blacklist", "block", "blocklist", "deny", "denylist"}:
            return "blacklist"
        return "whitelist"

    @staticmethod
    def _build_chat_candidates(platform: str, group_id: str, stream_id: str) -> set[str]:
        """构造当前聊天流匹配候选。"""

        candidates = {stream_id, group_id}
        if platform and group_id:
            candidates.add(f"{platform}:{group_id}")
        return {candidate for candidate in candidates if candidate}

    def _check_dangerous_local_permission(self, platform: str, user_id: str) -> str:
        """限制 danger-full-access 仅管理员可用。"""

        sandbox = str(self.config.local_codex.sandbox or "").strip().lower()
        extra_args = " ".join(str(arg or "").strip().lower() for arg in self.config.local_codex.extra_args)
        dangerous = sandbox == "danger-full-access" or "--dangerously-bypass-approvals-and-sandbox" in extra_args
        if dangerous and not self._is_admin_user(platform, user_id):
            return "当前 Codex 配置使用高危权限，仅管理员可以触发任务。"
        return ""

    def _check_prompt_local_path_access(self, prompt: str) -> str:
        """限制通过自然语言任务直接读取服务器本地绝对路径。"""

        paths = self._extract_local_path_mentions(prompt)
        if not paths:
            return ""
        if all(self._is_allowed_local_path_text(path) for path in paths):
            return ""
        return "为避免读取服务器敏感文件，当前不接受任务描述中的本地路径。请上传 QQ 文件并回复它使用 /codex，或由管理员配置允许读取的本地根目录。"

    @staticmethod
    def _extract_local_path_mentions(text: str) -> List[str]:
        """从文本中提取看起来像本机路径的片段。"""

        pattern = r"(?<![\w:])(?:file://)?(?:~|\.\.|/(?:root|home|etc|var|usr|opt|tmp|mnt|srv|data|www|run|proc|sys|dev))(?:/[^\s，。；：、]+)?"
        return re.findall(pattern, str(text or ""))

    def _is_allowed_local_path_text(self, path_text: str) -> bool:
        """判断任务描述中的本地路径是否落在允许目录内。"""

        roots = [str(root or "").strip() for root in self.config.input_file.allowed_local_roots if str(root or "").strip()]
        if not roots:
            return False
        try:
            candidate = self._normalize_local_file_path(path_text).resolve()
        except OSError:
            return False
        for root in roots:
            root_path = Path(root).expanduser()
            if not root_path.is_absolute():
                root_path = self._plugin_dir() / root_path
            try:
                candidate.relative_to(root_path.resolve())
                return True
            except (OSError, ValueError):
                continue
        return False

    def _is_admin_user(self, platform: str, user_id: str) -> bool:
        """判断用户是否是插件管理员。"""

        admins = _normalize_set(self.config.permission.admin_users)
        if not admins:
            return False
        normalized_platform = str(platform or "").strip().lower()
        normalized_user_id = str(user_id or "").strip().lower()
        candidates = {normalized_user_id}
        if normalized_platform and normalized_user_id:
            candidates.add(f"{normalized_platform}:{normalized_user_id}")
        return not admins.isdisjoint(candidates)

    def _check_running_limit(self, stream_id: str, platform: str, user_id: str) -> str:
        """检查并发任务数量限制。"""

        active_tasks = [
            task_state
            for task_state in self._tasks.values()
            if task_state.last_status in ACTIVE_STATUSES
        ]
        stream_count = sum(1 for task_state in active_tasks if task_state.stream_id == stream_id)
        if stream_count >= max(int(self.config.task.max_running_tasks_per_stream), 1):
            return "当前聊天流已有远程 Codex 任务在运行，请等待完成或先取消。"

        scoped_user = self._build_scoped_user(platform, user_id)
        user_count = sum(1 for task_state in active_tasks if self._build_scoped_user(task_state.platform, task_state.user_id) == scoped_user)
        if scoped_user and user_count >= max(int(self.config.task.max_running_tasks_per_user), 1):
            return "你已有远程 Codex 任务在运行，请等待完成或先取消。"

        return ""

    @staticmethod
    def _build_scoped_user(platform: str, user_id: str) -> str:
        """构造跨平台用户 ID。"""

        normalized_platform = str(platform or "").strip().lower()
        normalized_user_id = str(user_id or "").strip().lower()
        if normalized_platform and normalized_user_id:
            return f"{normalized_platform}:{normalized_user_id}"
        return normalized_user_id

    async def _create_remote_task(
        self,
        prompt: str,
        raw_command: str,
        stream_id: str,
        platform: str,
        user_id: str,
        group_id: str,
        private_progress: bool = False,
    ) -> tuple[bool, str, bool]:
        """向远程服务创建任务并启动本地轮询。"""

        payload = {
            "prompt": prompt,
            "task_type": self.config.task.task_type,
            "stream_id": stream_id,
            "platform": platform,
            "user_id": user_id,
            "group_id": group_id,
            "source": "maibot_remote_codex_agent",
            "options": {
                "forward_progress": bool(self.config.progress.forward_progress),
            },
            "metadata": {
                "raw_command": raw_command,
                "plugin_id": PLUGIN_ID,
            },
        }

        try:
            response_data = await self._client.create_task(payload)
        except Exception as exc:
            message = f"远程 Codex 任务创建失败：{exc}"
            await self.ctx.send.text(message, stream_id)
            return False, message, True

        task_id = str(response_data.get("task_id") or response_data.get("id") or "").strip()
        if not task_id:
            message = "远程 Codex 服务未返回 task_id，无法跟踪任务。"
            await self.ctx.send.text(message, stream_id)
            return False, message, True

        task_state = RemoteTaskState(
            task_id=task_id,
            stream_id=stream_id,
            platform=platform,
            user_id=user_id,
            group_id=group_id,
            prompt=prompt,
            last_status=self._normalize_status(response_data.get("status"), default="queued"),
            private_progress=private_progress,
            private_progress_user_id=user_id if private_progress else "",
        )
        self._tasks[task_id] = task_state
        task_state.watch_task = asyncio.create_task(self._watch_remote_task(task_state), name=f"remote_codex:{task_id}")

        remote_message = str(response_data.get("message") or "").strip()
        reply = f"远程 Codex 任务已创建：{task_id}"
        if private_progress:
            reply = f"{reply}\n阶段性进度将尝试私聊发送给你。"
        if remote_message:
            reply = f"{reply}\n{remote_message}"
        await self._send_task_created_message(task_state, reply)
        return True, f"远程任务已创建: {task_id}", True

    async def _create_local_task(
        self,
        prompt: str,
        raw_command: str,
        stream_id: str,
        platform: str,
        user_id: str,
        group_id: str,
        command_message: Any = None,
        record_type: str = "task",
        session_name: str = "",
        resume_thread_id: str = "",
        parent_task_id: str = "",
        private_progress: bool = False,
    ) -> tuple[bool, str, bool]:
        """创建本机 Codex CLI 任务。"""

        del raw_command
        task_id = f"local_{time.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        try:
            task_dir, workspace_dir, prompt_path, final_message_path = self._prepare_local_task_files(task_id, prompt)
            input_files = await self._prepare_reply_input_files(command_message, stream_id, workspace_dir)
            prompt_path.write_text(self._build_local_codex_prompt(prompt, input_files), encoding="utf-8")
        except Exception as exc:
            message = f"创建本地 Codex 任务目录失败：{exc}"
            await self.ctx.send.text(message, stream_id)
            return False, message, True

        task_state = RemoteTaskState(
            task_id=task_id,
            stream_id=stream_id,
            platform=platform,
            user_id=user_id,
            group_id=group_id,
            prompt=prompt,
            last_status="queued",
            workspace_dir=str(workspace_dir),
            final_message_path=str(final_message_path),
            input_files=input_files,
            record_type=record_type,
            session_name=session_name,
            codex_thread_id=resume_thread_id if resume_thread_id else "",
            parent_task_id=parent_task_id,
            private_progress=private_progress,
            private_progress_user_id=user_id if private_progress else "",
        )
        self._tasks[task_id] = task_state
        self._record_task_state(task_state)
        if record_type == "session" and session_name:
            self._update_session_from_task(task_state)
        task_state.watch_task = asyncio.create_task(
            self._run_local_codex_task(
                task_state,
                task_dir,
                workspace_dir,
                prompt_path,
                final_message_path,
                resume_thread_id=resume_thread_id,
            ),
            name=f"local_codex:{task_id}",
        )
        if record_type == "session":
            created_message = f"Codex session 任务已创建：{task_id}\n会话：{session_name}"
        elif resume_thread_id:
            created_message = f"Codex 续聊任务已创建：{task_id}"
        else:
            created_message = f"本机 Codex 任务已创建：{task_id}"
        if input_files:
            names = "、".join(item.name for item in input_files[:3])
            if len(input_files) > 3:
                names = f"{names} 等 {len(input_files)} 个文件"
            created_message = f"{created_message}\n已导入参考文件：{names}"
        if private_progress:
            created_message = f"{created_message}\n阶段性进度将尝试私聊发送给你。"
        await self._send_task_created_message(task_state, created_message)
        return True, f"本机任务已创建: {task_id}", True

    def _prepare_local_task_files(self, task_id: str, prompt: str) -> tuple[Path, Path, Path, Path]:
        """创建本地任务目录和 prompt 文件。"""

        work_root = self._local_work_root()
        task_dir = work_root / task_id
        workspace_dir = task_dir / "workspace"
        task_dir.mkdir(parents=True, exist_ok=False)
        workspace_dir.mkdir(parents=True, exist_ok=False)
        prompt_path = task_dir / "prompt.md"
        final_message_path = task_dir / "final.md"
        prompt_path.write_text(self._build_local_codex_prompt(prompt, []), encoding="utf-8")
        return task_dir, workspace_dir, prompt_path, final_message_path

    def _local_work_root(self) -> Path:
        """返回本地 Codex 工作根目录。"""

        work_root = Path(self.config.local_codex.work_root).expanduser()
        if not work_root.is_absolute():
            work_root = self._plugin_dir() / work_root
        return work_root

    def _records_root(self) -> Path:
        """返回插件持久记录目录。"""

        return self._local_work_root() / "_records"

    @staticmethod
    def _plugin_dir() -> Path:
        """返回插件目录。"""

        return Path(__file__).resolve().parent

    def _task_records_dir(self) -> Path:
        return self._records_root() / "tasks"

    def _session_records_dir(self) -> Path:
        return self._records_root() / "sessions"

    def _pending_session_dir(self) -> Path:
        return self._records_root() / "pending_sessions"

    def _task_dir_for_id(self, task_id: str) -> Path:
        """返回本地 task 目录。"""

        return self._local_work_root() / str(task_id or "").strip()

    def _ensure_records_dirs(self) -> None:
        """确保记录目录存在。"""

        self._task_records_dir().mkdir(parents=True, exist_ok=True)
        self._session_records_dir().mkdir(parents=True, exist_ok=True)
        self._pending_session_dir().mkdir(parents=True, exist_ok=True)

    def _record_task_state(self, task_state: RemoteTaskState, artifacts: Optional[List[Dict[str, Any]]] = None) -> None:
        """落盘保存 task 状态。"""

        if not task_state.task_id:
            return
        self._ensure_records_dirs()
        data = self._task_state_to_record(task_state, artifacts=artifacts)
        self._write_json_file(self._task_records_dir() / f"{task_state.task_id}.json", data)

    def _task_state_to_record(
        self,
        task_state: RemoteTaskState,
        artifacts: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """把任务状态转成可落盘 JSON。"""

        return {
            "task_id": task_state.task_id,
            "record_type": task_state.record_type or "task",
            "session_name": task_state.session_name,
            "stream_id": task_state.stream_id,
            "platform": task_state.platform,
            "user_id": task_state.user_id,
            "group_id": task_state.group_id,
            "prompt": task_state.prompt,
            "created_at": task_state.created_at,
            "updated_at": time.time(),
            "last_status": task_state.last_status,
            "workspace_dir": task_state.workspace_dir,
            "final_message_path": task_state.final_message_path,
            "codex_thread_id": task_state.codex_thread_id,
            "parent_task_id": task_state.parent_task_id,
            "input_files": [item.__dict__ for item in task_state.input_files],
            "artifacts": artifacts or [],
        }

    def _load_task_record(self, task_id: str) -> Optional[Dict[str, Any]]:
        """读取 task 记录。"""

        normalized = str(task_id or "").strip()
        if not normalized:
            return None
        memory_task = self._tasks.get(normalized)
        if memory_task is not None:
            return self._task_state_to_record(memory_task)
        return self._read_json_file(self._task_records_dir() / f"{normalized}.json")

    def _load_session_record(self, session_name: str) -> Optional[Dict[str, Any]]:
        """读取 session 记录。"""

        normalized = self._safe_record_name(session_name)
        if not normalized:
            return None
        record = self._read_json_file(self._session_records_dir() / f"{normalized}.json")
        return self._hydrate_session_history(record) if record else None

    def _write_session_record(self, record: Dict[str, Any]) -> None:
        """写入 session 记录。"""

        name = self._safe_record_name(str(record.get("session_name") or ""))
        if not name:
            raise RuntimeError("session 名称不能为空")
        self._ensure_records_dirs()
        record["session_name"] = name
        record["record_type"] = "session"
        record["updated_at"] = time.time()
        self._write_json_file(self._session_records_dir() / f"{name}.json", record)

    def _task_record_to_history_item(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """把 task 记录压缩成 session history 条目。"""

        return {
            "task_id": str(record.get("task_id") or ""),
            "prompt": str(record.get("prompt") or ""),
            "last_status": str(record.get("last_status") or "unknown"),
            "created_at": record.get("created_at") or 0,
            "updated_at": record.get("updated_at") or record.get("created_at") or 0,
            "codex_thread_id": str(record.get("codex_thread_id") or ""),
            "parent_task_id": str(record.get("parent_task_id") or ""),
            "artifacts": _coerce_artifacts(record.get("artifacts") or []),
        }

    @staticmethod
    def _merge_session_history(history: List[Dict[str, Any]], item: Dict[str, Any]) -> List[Dict[str, Any]]:
        """按 task_id 合并 session history。"""

        task_id = str(item.get("task_id") or "").strip()
        if not task_id:
            return history
        merged = []
        replaced = False
        for old_item in history:
            if str(old_item.get("task_id") or "") == task_id:
                merged.append(item)
                replaced = True
            else:
                merged.append(old_item)
        if not replaced:
            merged.append(item)
        merged.sort(key=lambda value: float(value.get("updated_at") or value.get("created_at") or 0))
        return merged

    def _hydrate_session_history(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """为旧 session 记录回填 task_ids/history。"""

        session_name = str(record.get("session_name") or "").strip()
        stream_id = str(record.get("stream_id") or "")
        platform = str(record.get("platform") or "")
        user_id = str(record.get("user_id") or "")
        if not session_name:
            return record

        history = record.get("history")
        normalized_history = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        seen_ids = {str(item.get("task_id") or "") for item in normalized_history if str(item.get("task_id") or "")}

        for path in self._task_records_dir().glob("*.json"):
            task_record = self._read_json_file(path)
            if not task_record:
                continue
            if str(task_record.get("record_type") or "task") != "session":
                continue
            if str(task_record.get("session_name") or "") != session_name:
                continue
            if stream_id and str(task_record.get("stream_id") or "") != stream_id:
                continue
            if platform and str(task_record.get("platform") or "") != platform:
                continue
            if user_id and str(task_record.get("user_id") or "") != user_id:
                continue
            task_id = str(task_record.get("task_id") or "")
            if task_id and task_id not in seen_ids:
                normalized_history.append(self._task_record_to_history_item(task_record))
                seen_ids.add(task_id)

        current_task_id = str(record.get("task_id") or "").strip()
        if current_task_id and current_task_id not in seen_ids:
            normalized_history.append(self._task_record_to_history_item(record))

        normalized_history.sort(key=lambda value: float(value.get("updated_at") or value.get("created_at") or 0))
        record["history"] = normalized_history
        record["task_ids"] = [str(item.get("task_id") or "") for item in normalized_history if str(item.get("task_id") or "")]
        if normalized_history and not str(record.get("latest_task_id") or "").strip():
            record["latest_task_id"] = str(normalized_history[-1].get("task_id") or "")
        return record

    def _update_session_from_task(
        self,
        task_state: RemoteTaskState,
        artifacts: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """用一次任务执行结果更新 session 记录。"""

        if not task_state.session_name:
            return
        record = self._load_session_record(task_state.session_name) or {}
        record = self._hydrate_session_history(record)
        task_record = self._task_state_to_record(task_state, artifacts=artifacts)
        history = [item for item in record.get("history", []) if isinstance(item, dict)]
        history = self._merge_session_history(history, self._task_record_to_history_item(task_record))
        record.update(task_record)
        record["session_name"] = task_state.session_name
        record["latest_task_id"] = task_state.task_id
        record["history"] = history
        record["task_ids"] = [str(item.get("task_id") or "") for item in history if str(item.get("task_id") or "")]
        self._write_session_record(record)

    def _cleanup_expired_task_records(self, force: bool = False) -> Dict[str, int]:
        """清理超过保留时间的普通 task 记录。"""

        result = {"records": 0, "workspaces": 0}
        if not force and not self.config.task.auto_cleanup_task_records:
            return result
        ttl_hours = float(self.config.task.resumable_task_ttl_hours)
        if ttl_hours <= 0:
            return result
        cutoff = time.time() - ttl_hours * 3600
        for path in self._task_records_dir().glob("*.json"):
            record = self._read_json_file(path)
            if not record:
                continue
            if str(record.get("record_type") or "task") == "session":
                continue
            updated_at = float(record.get("updated_at") or record.get("created_at") or 0)
            if updated_at and updated_at < cutoff:
                workspace_deleted = self._delete_task_workspace_from_record(record)
                try:
                    path.unlink()
                    result["records"] += 1
                    if workspace_deleted:
                        result["workspaces"] += 1
                except OSError:
                    continue
        return result

    def _cleanup_expired_input_files(self, force: bool = False) -> Dict[str, int]:
        """清理超过保留时间的输入材料文件。"""

        result = {"records": 0, "files": 0, "dirs": 0}
        if not force and not self.config.input_file.auto_cleanup_input_files:
            return result
        ttl_hours = float(self.config.input_file.input_file_ttl_hours)
        if ttl_hours <= 0:
            return result
        cutoff = time.time() - ttl_hours * 3600

        for path in list(self._task_records_dir().glob("*.json")) + list(self._session_records_dir().glob("*.json")):
            record = self._read_json_file(path)
            if not record:
                continue
            updated_at = float(record.get("updated_at") or record.get("created_at") or 0)
            if not updated_at or updated_at >= cutoff:
                continue
            files_deleted, dir_deleted = self._delete_input_files_from_record(record)
            if files_deleted or dir_deleted:
                record["input_files"] = []
                record["input_files_cleaned_at"] = time.time()
                self._write_json_file(path, record)
                result["records"] += 1
                result["files"] += files_deleted
                result["dirs"] += 1 if dir_deleted else 0
        return result

    def _delete_input_files_from_record(self, record: Dict[str, Any]) -> tuple[int, bool]:
        """按记录删除 workspace/input 下的输入材料。"""

        input_dir_name = self._safe_dir_name(self.config.input_file.input_dir_name or "input")
        work_root = self._local_work_root().resolve()
        candidate_files: List[Path] = []
        for item in record.get("input_files") or []:
            if not isinstance(item, dict):
                continue
            raw_path = str(item.get("path") or "").strip()
            if raw_path:
                candidate_files.append(self._resolve_stored_path(raw_path))

        workspace_dir = str(record.get("workspace_dir") or "").strip()
        input_dir: Optional[Path] = None
        if workspace_dir:
            input_dir = self._resolve_stored_path(workspace_dir) / input_dir_name
            if not candidate_files and input_dir.exists():
                candidate_files.extend(path for path in input_dir.iterdir() if path.is_file())

        deleted_files = 0
        for candidate in candidate_files:
            try:
                target = candidate.resolve()
            except OSError:
                continue
            if target == work_root or work_root not in target.parents:
                continue
            if input_dir is not None:
                try:
                    resolved_input_dir = input_dir.resolve()
                except OSError:
                    resolved_input_dir = None
                if resolved_input_dir is not None and resolved_input_dir not in target.parents:
                    continue
            if not target.exists() or not target.is_file():
                continue
            try:
                target.unlink()
                deleted_files += 1
            except OSError:
                continue

        dir_deleted = False
        if input_dir is not None:
            try:
                resolved_input_dir = input_dir.resolve()
            except OSError:
                resolved_input_dir = None
            if resolved_input_dir is not None and work_root in resolved_input_dir.parents and resolved_input_dir.exists():
                try:
                    resolved_input_dir.rmdir()
                    dir_deleted = True
                except OSError:
                    dir_deleted = False
        return deleted_files, dir_deleted

    def _delete_task_workspace_from_record(self, record: Dict[str, Any], force: bool = False) -> bool:
        """按记录删除 task 工作目录。"""

        if not force and not self.config.task.auto_cleanup_task_workspaces:
            return False
        task_id = str(record.get("task_id") or "").strip()
        workspace_dir = str(record.get("workspace_dir") or "").strip()
        candidates = []
        if workspace_dir:
            workspace_path = self._resolve_stored_path(workspace_dir)
            candidates.append(workspace_path.parent if workspace_path.name == "workspace" else workspace_path)
        if task_id:
            candidates.append(self._task_dir_for_id(task_id))

        work_root = self._local_work_root().resolve()
        for candidate in candidates:
            try:
                target = candidate.expanduser().resolve()
            except OSError:
                continue
            if target == work_root or work_root not in target.parents:
                continue
            if not target.exists() or not target.is_dir():
                continue
            try:
                shutil.rmtree(target)
                return True
            except OSError:
                continue
        return False

    def _resolve_stored_path(self, path_value: str) -> Path:
        """解析记录中的路径，兼容绝对路径和相对插件目录路径。"""

        path = Path(str(path_value or "")).expanduser()
        if path.is_absolute():
            return path
        return self._plugin_dir() / path

    def _delete_task_record_by_id(self, task_id: str, delete_workspace: bool = True) -> tuple[bool, bool]:
        """删除 task 记录和可选 workspace。"""

        normalized = str(task_id or "").strip()
        if not normalized:
            return False, False
        record_path = self._task_records_dir() / f"{normalized}.json"
        record = self._read_json_file(record_path) or {}
        if "task_id" not in record:
            record["task_id"] = normalized
        workspace_deleted = self._delete_task_workspace_from_record(record, force=delete_workspace)
        record_deleted = False
        try:
            if record_path.exists():
                record_path.unlink()
                record_deleted = True
        except OSError:
            record_deleted = False
        self._tasks.pop(normalized, None)
        return record_deleted, workspace_deleted

    def _delete_session_record_by_name(self, session_name: str) -> tuple[bool, int]:
        """删除 session 记录和 history 中的 task workspace。"""

        name = self._safe_record_name(session_name)
        if not name:
            return False, 0
        record_path = self._session_records_dir() / f"{name}.json"
        record = self._read_json_file(record_path)
        workspace_count = 0
        if record:
            record = self._hydrate_session_history(record)
            task_ids = [str(item.get("task_id") or "") for item in record.get("history", []) if isinstance(item, dict)]
            for task_id in task_ids:
                _, workspace_deleted = self._delete_task_record_by_id(task_id, delete_workspace=True)
                if workspace_deleted:
                    workspace_count += 1
        record_deleted = False
        try:
            if record_path.exists():
                record_path.unlink()
                record_deleted = True
        except OSError:
            record_deleted = False
        return record_deleted, workspace_count

    @staticmethod
    def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
        """读取 JSON 对象。"""

        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write_json_file(path: Path, data: Dict[str, Any]) -> None:
        """写入 JSON 对象。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _safe_record_name(name: str) -> str:
        """生成安全记录名。"""

        cleaned = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", str(name or "").strip()).strip("._-")
        return cleaned[:80]

    def _build_local_codex_prompt(self, prompt: str, input_files: List[InputFile]) -> str:
        """构造发给本机 Codex CLI 的 prompt。"""

        input_dir_name = self._safe_dir_name(self.config.input_file.input_dir_name or "input")
        input_text = ""
        if input_files:
            lines = [
                f"用户上传的参考文件已经放在当前工作目录的 {input_dir_name}/ 目录下。",
                f"请优先读取这些文件并按用户任务处理；不要把 {input_dir_name}/ 中的原始材料当作最终产物回传。",
                "输入文件：",
            ]
            for item in input_files:
                rel_path = Path(item.path).name
                size_text = f"，大小 {self._format_size(item.size)}" if item.size > 0 else ""
                lines.append(f"- {input_dir_name}/{rel_path}{size_text}")
            input_text = "\n".join(lines) + "\n\n"

        return (
            # 这段提示词是插件给 Codex CLI 的“运行边界”。
            # 重点是限制它只在 workspace 内工作，并要求最终产物放到 artifacts/，
            # 这样插件后续才能稳定扫描和回传文件。
            "你正在由 QQ 群中的 MaiBot 插件触发执行任务。\n"
            "请只在当前工作目录内完成用户请求；如需生成文件，请放在 artifacts/ 下。\n"
            "不要读取当前工作目录之外的服务器文件；除非输入文件列表明确提供了材料，否则不要尝试访问外部本地路径。\n"
            "如果用户要求 Word/word/docx 文档，必须生成 .docx 文件，不能只生成 Markdown 或纯文本替代品。\n"
            "所有面向用户的进度更新和最终回答都必须使用简体中文，语气简洁，不要输出 Markdown 格式。\n"
            "最终回答请用简体中文，简要说明完成内容和产物文件名；不要输出服务器绝对路径。\n\n"
            f"{input_text}"
            f"用户任务：\n{prompt.strip()}\n"
        )

    async def _prepare_reply_input_files(
        self,
        command_message: Any,
        stream_id: str,
        workspace_dir: Path,
        group_id: str = "",
        user_id: str = "",
    ) -> List[InputFile]:
        """从被回复的 QQ 文件消息中导入输入材料。"""

        if not self.config.input_file.enable_reply_file:
            return []

        reply_message_id = self._extract_reply_message_id(command_message)
        if not reply_message_id:
            return []

        try:
            reply_message = await self.ctx.message.get_by_id(reply_message_id, stream_id=stream_id)
        except Exception as exc:
            raise RuntimeError(f"无法读取被回复的消息：{exc}") from exc

        # 优先用 MaiBot 自己的 message.get_by_id 读取被回复消息。
        # 如果 SDK 返回的消息里没有文件段，再退回 NapCat get_msg 读取原始 QQ 消息。
        file_segments = self._extract_file_segments(reply_message)
        if not file_segments:
            napcat_message = await self._get_napcat_message(reply_message_id)
            file_segments = self._extract_file_segments(napcat_message)
        if not file_segments:
            raise RuntimeError("被回复的消息里没有可用文件。请回复 QQ 文件消息后再发送 /codex。")

        input_dir_name = self._safe_dir_name(self.config.input_file.input_dir_name or "input")
        input_dir = workspace_dir / input_dir_name
        input_dir.mkdir(parents=True, exist_ok=True)

        imported_files: List[InputFile] = []
        max_files = max(int(self.config.input_file.max_files_per_task), 1)
        for segment in file_segments[:max_files]:
            imported_files.append(await self._import_input_file_segment(segment, input_dir, group_id=group_id, user_id=user_id))
        return imported_files

    async def _get_napcat_message(self, message_id: str) -> Any:
        """通过 NapCat get_msg 获取原始 QQ 消息。"""

        if not str(message_id or "").strip():
            return None
        try:
            response = await self._napcat_client.call_action("get_msg", {"message_id": message_id})
        except Exception as exc:
            self.ctx.logger.warning("NapCat get_msg 获取被回复消息失败: %s", exc)
            return None
        return response.get("data") if isinstance(response, dict) else response

    def _extract_reply_message_id(self, message: Any) -> str:
        """从命令消息中提取被回复消息 ID。"""

        for value in self._walk_message_values(message):
            if isinstance(value, dict):
                message_type = str(value.get("type") or value.get("msg_type") or "").strip().lower()
                data = value.get("data")
                if message_type == "reply" and isinstance(data, dict):
                    reply_id = self._first_text_value(data, ["id", "message_id", "msg_id", "reply_id"])
                    if reply_id:
                        return reply_id
                reply_id = self._first_text_value(
                    value,
                    [
                        "reply_message_id",
                        "reply_to_message_id",
                        "source_message_id",
                        "quoted_message_id",
                        "quote_message_id",
                        "reply_id",
                    ],
                )
                if reply_id:
                    return reply_id
            else:
                reply_id = self._first_attr_value(
                    value,
                    [
                        "reply_message_id",
                        "reply_to_message_id",
                        "source_message_id",
                        "quoted_message_id",
                        "quote_message_id",
                        "reply_id",
                    ],
                )
                if reply_id:
                    return reply_id
        return ""

    def _extract_file_segments(self, message: Any) -> List[Dict[str, Any]]:
        """从消息对象中提取 file 段。"""

        segments: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        for value in self._walk_message_values(message):
            if not isinstance(value, dict):
                continue
            segment_type = str(value.get("type") or value.get("msg_type") or value.get("message_type") or "").strip().lower()
            data = value.get("data")
            if segment_type == "file":
                if isinstance(data, dict):
                    segment = dict(data)
                else:
                    segment = {key: val for key, val in value.items() if key not in {"type", "msg_type", "message_type"}}
                key = self._file_segment_key(segment)
                if key not in seen_keys:
                    seen_keys.add(key)
                    segments.append(segment)
                continue
            if any(key in value for key in ("file", "file_id", "fileId", "url", "path")):
                name = str(value.get("name") or value.get("file_name") or value.get("filename") or "").strip().lower()
                file_value = str(value.get("file") or value.get("path") or value.get("url") or value.get("file_id") or value.get("fileId") or "").strip()
                has_file_identity = any(str(value.get(key) or "").strip() for key in ("file", "path", "file_id", "fileId"))
                if file_value and (segment_type == "file" or name or has_file_identity):
                    segment = dict(value)
                    key = self._file_segment_key(segment)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        segments.append(segment)
        return segments

    @staticmethod
    def _file_segment_key(segment: Dict[str, Any]) -> str:
        """生成文件段去重 key。"""

        return "|".join(
            str(segment.get(key) or "").strip()
            for key in ("file_id", "fileId", "file", "path", "url", "name", "filename", "file_name")
        )

    async def _import_input_file_segment(
        self,
        segment: Dict[str, Any],
        input_dir: Path,
        group_id: str = "",
        user_id: str = "",
    ) -> InputFile:
        """把单个 QQ 文件段导入 input 目录。"""

        resolved = await self._resolve_input_file_segment(segment, group_id=group_id, user_id=user_id)
        file_ref = self._select_input_file_ref(resolved)
        name = self._guess_input_filename(resolved, file_ref)
        if not file_ref:
            raise RuntimeError(f"文件 {name} 缺少可下载地址或本地路径")

        target_path = self._dedupe_path(input_dir / self._safe_filename(name))
        if file_ref.lower().startswith(("http://", "https://")):
            if not self.config.input_file.allow_url_download:
                raise RuntimeError(f"输入文件 {name} 是 URL，但 input_file.allow_url_download 未启用")
            await self._download_input_file(file_ref, target_path)
        else:
            source_path = self._normalize_local_file_path(file_ref)
            self._check_allowed_input_source(source_path)
            if not source_path.exists() or not source_path.is_file():
                raise RuntimeError(f"输入文件不存在或不可读：{source_path}")
            self._check_input_file_size(source_path.stat().st_size, source_path.name)
            shutil.copy2(source_path, target_path)

        size = target_path.stat().st_size
        self._check_input_file_size(size, target_path.name)
        return InputFile(name=target_path.name, path=str(target_path.resolve()), size=size, source=file_ref)

    async def _resolve_input_file_segment(
        self,
        segment: Dict[str, Any],
        group_id: str = "",
        user_id: str = "",
    ) -> Dict[str, Any]:
        """通过 NapCat 补全 file_id 对应的文件信息。"""

        data = dict(segment)
        if self._select_input_file_ref(data):
            return data

        file_id = str(data.get("file_id") or data.get("fileId") or data.get("id") or "").strip()
        if not file_id:
            return data

        try:
            response = await self._napcat_client.call_action("get_file", {"file_id": file_id})
        except Exception as exc:
            self.ctx.logger.warning("NapCat get_file 获取输入文件失败: %s", exc)
        else:
            response_data = response.get("data")
            if isinstance(response_data, dict):
                merged = dict(data)
                merged.update(response_data)
                if self._select_input_file_ref(merged):
                    return merged
                data = merged

        url_data = await self._resolve_input_file_url(data, file_id=file_id, group_id=group_id, user_id=user_id)
        if url_data:
            merged = dict(data)
            merged.update(url_data)
            return merged
        return data

    async def _resolve_input_file_url(
        self,
        data: Dict[str, Any],
        file_id: str,
        group_id: str = "",
        user_id: str = "",
    ) -> Dict[str, Any]:
        """通过 NapCat 文件 URL action 补全下载地址。"""

        busid = str(data.get("busid") or data.get("bus_id") or "").strip()
        candidates: List[tuple[str, Dict[str, Any]]] = []
        if group_id:
            payload: Dict[str, Any] = {"group_id": group_id, "file_id": file_id}
            if busid:
                payload["busid"] = busid
            candidates.append(("get_group_file_url", payload))
        if user_id:
            candidates.append(("get_private_file_url", {"user_id": user_id, "file_id": file_id}))

        for action, payload in candidates:
            try:
                response = await self._napcat_client.call_action(action, payload)
            except Exception as exc:
                self.ctx.logger.warning("NapCat %s 获取输入文件 URL 失败: %s", action, exc)
                continue
            response_data = response.get("data")
            if isinstance(response_data, dict):
                return response_data
        return {}

    @staticmethod
    def _select_input_file_ref(data: Dict[str, Any]) -> str:
        """从文件段中选择真实可读取的路径或 URL。"""

        for key in ("path", "url", "download_url"):
            value = str(data.get(key) or "").strip()
            if value:
                return value

        file_value = str(data.get("file") or "").strip()
        if not file_value:
            return ""
        lowered = file_value.lower()
        if lowered.startswith(("http://", "https://", "file://", "base64:", "data:")):
            return file_value
        path = Path(file_value).expanduser()
        if path.is_absolute() or "/" in file_value or "\\" in file_value or path.exists():
            return file_value
        return ""

    async def _download_input_file(self, url: str, target_path: Path) -> None:
        """下载输入文件到目标路径。"""

        timeout = max(float(self.config.napcat.request_timeout_seconds), 1.0)
        headers = {}
        token = str(self.config.napcat.token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                total = 0
                with target_path.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        self._check_input_file_size(total, target_path.name)
                        file.write(chunk)

    def _check_allowed_input_source(self, source_path: Path) -> None:
        """检查本地输入文件是否在允许根目录内。"""

        roots = [str(root or "").strip() for root in self.config.input_file.allowed_local_roots if str(root or "").strip()]
        if not roots:
            raise RuntimeError("当前未允许读取服务器本地路径。请使用 QQ 文件上传，或由管理员配置 input_file.allowed_local_roots。")

        resolved_source = source_path.resolve()
        for root in roots:
            root_path = Path(root).expanduser()
            if not root_path.is_absolute():
                root_path = self._plugin_dir() / root_path
            resolved_root = root_path.resolve()
            try:
                resolved_source.relative_to(resolved_root)
                return
            except ValueError:
                continue
        raise RuntimeError(f"输入文件路径不在允许目录内：{source_path}")

    def _check_input_file_size(self, size_bytes: int, name: str) -> None:
        """检查输入文件大小。"""

        max_file_size_mb = float(self.config.input_file.max_file_size_mb)
        if max_file_size_mb <= 0:
            return
        if size_bytes > max_file_size_mb * 1024 * 1024:
            raise RuntimeError(f"输入文件 {name} 超过 {max_file_size_mb:g}MB 限制")

    @staticmethod
    def _normalize_local_file_path(file_ref: str) -> Path:
        """把 NapCat 文件引用规范化为本地路径。"""

        value = str(file_ref or "").strip()
        if value.lower().startswith("file://"):
            value = value[7:]
        return Path(value).expanduser()

    @staticmethod
    def _guess_input_filename(data: Dict[str, Any], file_ref: str) -> str:
        """推断输入文件名。"""

        for key in ("name", "filename", "file_name"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
        ref = str(file_ref or "").strip()
        if ref.lower().startswith(("http://", "https://")):
            return Path(httpx.URL(ref).path).name or "input_file"
        return Path(ref).name or "input_file"

    @staticmethod
    def _safe_filename(name: str) -> str:
        """生成适合落盘的文件名。"""

        cleaned = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", str(name or "").strip()).strip(" .")
        return cleaned[:160] or "input_file"

    @staticmethod
    def _safe_dir_name(name: str) -> str:
        """生成安全目录名。"""

        cleaned = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", str(name or "").strip()).strip(" .")
        return cleaned[:80] or "input"

    @staticmethod
    def _dedupe_path(path: Path) -> Path:
        """避免输入文件重名覆盖。"""

        if not path.exists():
            return path
        stem = path.stem or "input_file"
        suffix = path.suffix
        for index in range(2, 1000):
            candidate = path.with_name(f"{stem}_{index}{suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"无法为输入文件生成唯一文件名：{path.name}")

    @staticmethod
    def _first_text_value(data: Dict[str, Any], keys: List[str]) -> str:
        """按候选 key 取第一个非空字符串。"""

        for key in keys:
            value = data.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _first_attr_value(value: Any, keys: List[str]) -> str:
        """按候选属性名取第一个非空字符串。"""

        for key in keys:
            if not hasattr(value, key):
                continue
            attr_value = getattr(value, key)
            if attr_value is None:
                continue
            text = str(attr_value).strip()
            if text:
                return text
        return ""

    def _walk_message_values(self, value: Any, depth: int = 0) -> List[Any]:
        """遍历消息对象中的 dict、list 和常见对象字段。"""

        if depth > 8 or value is None:
            return []

        values = [value]
        if isinstance(value, dict):
            for child in value.values():
                values.extend(self._walk_message_values(child, depth + 1))
            return values

        if isinstance(value, (list, tuple)):
            for child in value:
                values.extend(self._walk_message_values(child, depth + 1))
            return values

        for attr in ("raw_message", "message", "message_chain", "segments", "data"):
            if hasattr(value, attr):
                try:
                    values.extend(self._walk_message_values(getattr(value, attr), depth + 1))
                except Exception:
                    continue
        return values

    async def _watch_remote_task(self, task_state: RemoteTaskState) -> None:
        """轮询远程任务状态并转发进度。"""

        while True:
            if time.monotonic() - task_state.created_at > max(float(self.config.task.max_watch_seconds), 30.0):
                task_state.last_status = "watch_timeout"
                await self.ctx.send.text(
                    f"远程 Codex 任务 {task_state.task_id} 已超过本地跟踪时长，停止转发进度。", task_state.stream_id
                )
                return

            try:
                data = await self._client.get_task(task_state.task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.ctx.send.text(f"远程 Codex 任务 {task_state.task_id} 查询失败：{exc}", task_state.stream_id)
                await asyncio.sleep(max(float(self.config.task.poll_interval_seconds), 1.0))
                continue

            status = self._normalize_status(data.get("status"), default=task_state.last_status or "running")
            task_state.last_status = status

            if self.config.progress.forward_progress:
                await self._send_progress_updates(task_state, data)

            if status in TERMINAL_STATUSES:
                await self._send_final_result(task_state, data)
                return

            await asyncio.sleep(max(float(self.config.task.poll_interval_seconds), 1.0))

    async def _run_local_codex_task(
        self,
        task_state: RemoteTaskState,
        task_dir: Path,
        workspace_dir: Path,
        prompt_path: Path,
        final_message_path: Path,
        resume_thread_id: str = "",
    ) -> None:
        """运行本机 Codex CLI 并转发输出。"""

        stdout_log = task_dir / "stdout.jsonl"
        stderr_log = task_dir / "stderr.log"
        task_state.last_status = "running"

        command = self._build_local_codex_command(workspace_dir, final_message_path, resume_thread_id=resume_thread_id)
        self.ctx.logger.info("启动本机 Codex 任务 %s: %s", task_state.task_id, " ".join(command))

        try:
            prompt_text = prompt_path.read_text(encoding="utf-8")
            env = self._build_local_codex_env()
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            task_state.process = process
            if process.stdin is not None:
                process.stdin.write(prompt_text.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()

            stdout_task = asyncio.create_task(
                self._consume_local_stdout(task_state, process, stdout_log),
                name=f"local_codex_stdout:{task_state.task_id}",
            )
            stderr_task = asyncio.create_task(
                self._consume_local_stderr(process, stderr_log),
                name=f"local_codex_stderr:{task_state.task_id}",
            )
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=max(float(self.config.local_codex.process_timeout_seconds), 30.0),
                )
            except asyncio.TimeoutError:
                process.kill()
                task_state.last_status = "failed"
                await self.ctx.send.text(f"本机 Codex 任务 {task_state.task_id} 超时，已终止。", task_state.stream_id)
                return
            finally:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

            task_state.last_status = "succeeded" if process.returncode == 0 else "failed"
            self._discard_pending_local_progress(task_state)
            artifacts = self._collect_local_artifacts(workspace_dir)
            final_data = {
                "status": task_state.last_status,
                "summary": self._read_local_final_message(
                    final_message_path,
                    stderr_log,
                    stdout_log,
                    process.returncode,
                ),
                "artifacts": artifacts,
            }
            self._record_task_state(task_state, artifacts=artifacts)
            if task_state.record_type == "session" and task_state.session_name:
                self._update_session_from_task(task_state, artifacts=artifacts)
            await self._send_final_result(task_state, final_data)
        except asyncio.CancelledError:
            if task_state.process is not None and task_state.process.returncode is None:
                task_state.process.terminate()
            raise
        except Exception as exc:
            task_state.last_status = "failed"
            await self.ctx.send.text(f"本机 Codex 任务 {task_state.task_id} 执行失败：{exc}", task_state.stream_id)

    def _build_local_codex_command(
        self,
        workspace_dir: Path,
        final_message_path: Path,
        resume_thread_id: str = "",
    ) -> List[str]:
        """构造本机 Codex CLI 命令。"""

        local_config = self.config.local_codex
        command = [
            local_config.codex_binary.strip() or "codex",
            "-a",
            local_config.approval_policy.strip() or "never",
            "-s",
            local_config.sandbox.strip() or "workspace-write",
            "-C",
            str(workspace_dir),
        ]
        if local_config.enable_search:
            command.append("--search")
        command.append("exec")
        if resume_thread_id:
            command.extend(["resume", resume_thread_id])
        command.append("--json")
        if not resume_thread_id:
            command.extend(["--color", "never"])
        command.extend(["--skip-git-repo-check", "--output-last-message", str(final_message_path)])
        if local_config.model.strip():
            command.extend(["-m", local_config.model.strip()])
        command.extend(str(arg) for arg in local_config.extra_args if str(arg).strip())
        command.append("-")
        return command

    def _build_local_codex_env(self) -> Dict[str, str]:
        """构造本机 Codex 子进程环境变量。"""

        # 不再从插件配置读取 .env 文件，避免把额外密钥注入 Codex 子进程。
        # Codex CLI 需要的 PATH/HOME/CODEX_HOME 应由 MaiBot 的启动环境提供。
        return dict(os.environ)

    async def _consume_local_stdout(
        self,
        task_state: RemoteTaskState,
        process: asyncio.subprocess.Process,
        stdout_log: Path,
    ) -> None:
        """读取 Codex stdout JSONL 并转成进度消息。"""

        if process.stdout is None:
            return

        with stdout_log.open("w", encoding="utf-8") as log_file:
            while True:
                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    return
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                log_file.write(f"{line}\n")
                log_file.flush()
                self._capture_codex_thread_id(task_state, line)
                progress_text = self._extract_progress_from_codex_line(line)
                if progress_text:
                    await self._queue_local_progress(task_state, progress_text)

    async def _consume_local_stderr(self, process: asyncio.subprocess.Process, stderr_log: Path) -> None:
        """记录 Codex stderr。"""

        if process.stderr is None:
            return

        with stderr_log.open("w", encoding="utf-8") as log_file:
            while True:
                line_bytes = await process.stderr.readline()
                if not line_bytes:
                    return
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                log_file.write(f"{line}\n")
                log_file.flush()

    def _capture_codex_thread_id(self, task_state: RemoteTaskState, line: str) -> None:
        """从 Codex JSONL 里捕获 thread_id。"""

        if task_state.codex_thread_id:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        thread_id = str(event.get("thread_id") or event.get("conversation_id") or "").strip()
        if not thread_id and event.get("type") == "thread.started":
            thread_id = str(event.get("thread_id") or "").strip()
        if thread_id:
            task_state.codex_thread_id = thread_id
            self._record_task_state(task_state)

    def _extract_progress_from_codex_line(self, line: str) -> str:
        """从 Codex JSONL 或普通文本输出中抽取适合转发的进度。"""

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return self._clean_codex_progress_text(line)

        if not isinstance(event, dict):
            return ""

        event_type = str(event.get("type") or "").strip()
        item = event.get("item")
        item_type = str(item.get("type") or "").strip() if isinstance(item, dict) else ""
        if event_type != "item.completed" or item_type != "agent_message":
            return ""

        candidates: List[Any] = [
            event.get("message"),
            event.get("text"),
            event.get("content"),
            event.get("delta"),
        ]
        if isinstance(item, dict):
            candidates.extend([item.get("message"), item.get("text"), item.get("content")])
        for candidate in candidates:
            text = self._stringify_codex_event_value(candidate)
            if text:
                return self._clean_codex_progress_text(text)
        return ""

    @staticmethod
    def _stringify_codex_event_value(value: Any) -> str:
        """将 Codex event 字段转成文本。"""

        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
            return " ".join(part.strip() for part in parts if part.strip())
        if isinstance(value, dict):
            return str(value.get("text") or value.get("content") or value.get("message") or "").strip()
        return ""

    @staticmethod
    def _clean_codex_progress_text(text: str) -> str:
        """清理进度文本。"""

        cleaned = " ".join(str(text or "").replace("\r", "\n").split())
        if not cleaned:
            return ""
        noisy_prefixes = ("{\"", "[debug]", "debug:", "trace:")
        if cleaned.lower().startswith(noisy_prefixes):
            return ""
        if _looks_like_final_progress(cleaned):
            return ""
        return cleaned

    def _path_redactions_for_task(self, task_state: RemoteTaskState) -> Dict[str, str]:
        """构造面向用户输出的路径替换表。"""

        replacements: Dict[str, str] = {}
        try:
            plugin_dir = self._plugin_dir().resolve()
            replacements[str(plugin_dir)] = "remote_codex_agent"
        except OSError:
            pass
        try:
            work_root = self._local_work_root().resolve()
            replacements[str(work_root)] = "data/tasks"
        except OSError:
            pass
        workspace_dir = str(task_state.workspace_dir or "").strip()
        if workspace_dir:
            try:
                workspace_path = self._resolve_stored_path(workspace_dir).resolve()
                replacements[str(workspace_path)] = "workspace"
                replacements[str(workspace_path / "artifacts")] = "workspace/artifacts"
                replacements[str(workspace_path / self._safe_dir_name(self.config.input_file.input_dir_name or "input"))] = "workspace/input"
            except OSError:
                pass
        if task_state.task_id:
            replacements[f"data/tasks/{task_state.task_id}/workspace"] = "workspace"
        return replacements

    def _sanitize_task_output_text(self, task_state: RemoteTaskState, text: str) -> str:
        """清理任务输出中不应暴露给 QQ 用户的本机路径。"""

        return _sanitize_path_text(text, self._path_redactions_for_task(task_state))

    async def _send_local_progress(self, task_state: RemoteTaskState, progress_text: str) -> None:
        """按节流规则发送本机任务进度。"""

        if not self.config.progress.forward_progress:
            return
        progress_id = f"local:{hash(progress_text)}"
        if progress_id in task_state.sent_progress_ids:
            return

        now = time.monotonic()
        if now - task_state.last_progress_sent_at < max(float(self.config.progress.min_send_interval_seconds), 0.0):
            return
        task_state.sent_progress_ids.add(progress_id)
        task_state.last_progress_sent_at = now
        max_chars = max(int(self.config.progress.max_progress_item_chars), 20)
        progress_text = self._sanitize_task_output_text(task_state, progress_text)
        message = _format_progress_message(task_state.task_id, [progress_text], max_chars)
        await self._send_progress_text(task_state, message)

    async def _send_progress_text(self, task_state: RemoteTaskState, message: str) -> None:
        """按任务设置发送进度消息。"""

        if task_state.private_progress:
            # --dm 的核心行为只影响阶段性进度：先尝试私聊触发用户。
            # 失败后只提醒一次，避免每条进度都刷屏报告失败。
            if await self._try_send_private_task_text(task_state, message, log_prefix="Codex 私聊进度发送失败"):
                return
            if not task_state.private_progress_failure_notified:
                task_state.private_progress_failure_notified = True
                notice = f"任务 {task_state.task_id} 私聊进度发送失败，后续进度将发回当前聊天。"
                if self.config.progress.private_progress_fallback_to_origin:
                    await self.ctx.send.text(notice, task_state.stream_id)

        if self.config.progress.private_progress_fallback_to_origin or not task_state.private_progress:
            await self.ctx.send.text(message, task_state.stream_id)

    async def _send_task_created_message(self, task_state: RemoteTaskState, message: str) -> None:
        """发送任务创建提示。"""

        # 任务创建提示默认留在原聊天流，群里其他人能看到 task_id，方便后续 status/cancel。
        # 只有显式开启 private_progress_send_task_created 才私聊。
        if task_state.private_progress and self.config.progress.private_progress_send_task_created:
            if await self._try_send_private_task_text(task_state, message, log_prefix="Codex 私聊任务创建提示发送失败"):
                return
        await self.ctx.send.text(message, task_state.stream_id)

    async def _try_send_private_task_text(self, task_state: RemoteTaskState, message: str, log_prefix: str) -> bool:
        """尝试向触发用户私聊发送任务文本。"""

        # 不通过 MaiBot stream_id 发私聊，因为临时私聊路由可能不稳定。
        # 这里直接调用 NapCat HTTP API 的 send_private_msg。
        user_id = str(task_state.private_progress_user_id or task_state.user_id or "").strip()
        if not task_state.private_progress or not user_id or not self.config.napcat.enabled:
            return False
        try:
            await self._napcat_client.call_action(
                "send_private_msg",
                {
                    "user_id": user_id,
                    "message": [{"type": "text", "data": {"text": message}}],
                },
            )
            return True
        except Exception as exc:
            self.ctx.logger.warning("%s: %s", log_prefix, exc)
            return False

    async def _queue_local_progress(self, task_state: RemoteTaskState, progress_text: str) -> None:
        """延后一条本机进度，避免把最终回答提前当进度发出。"""

        pending_text = task_state.pending_local_progress_text
        if pending_text:
            await self._send_local_progress(task_state, pending_text)
        task_state.pending_local_progress_text = progress_text

    def _discard_pending_local_progress(self, task_state: RemoteTaskState) -> None:
        """丢弃最后一条本机进度；它通常就是 final 摘要。"""

        task_state.pending_local_progress_text = ""

    def _read_local_final_message(
        self,
        final_message_path: Path,
        stderr_log: Path,
        stdout_log: Path,
        returncode: Optional[int],
    ) -> str:
        """读取本地 Codex 最终回答。"""

        if final_message_path.exists():
            content = final_message_path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                return content
        stdout_error = self._read_local_stdout_error(stdout_log)
        if stdout_error:
            return f"Codex CLI 退出码 {returncode}。\n{stdout_error}"
        if returncode not in (None, 0) and stderr_log.exists():
            stderr_tail = stderr_log.read_text(encoding="utf-8", errors="replace")[-1200:].strip()
            if stderr_tail:
                return f"Codex CLI 退出码 {returncode}。\n{stderr_tail}"
        return "Codex CLI 已结束，但没有生成最终回答。"

    @staticmethod
    def _read_local_stdout_error(stdout_log: Path) -> str:
        """从 Codex JSONL stdout 中提取错误信息。"""

        if not stdout_log.exists():
            return ""

        errors: List[str] = []
        for line in stdout_log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "error":
                message = str(event.get("message") or "").strip()
                if message:
                    errors.append(message)
                continue
            error = event.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("error") or "").strip()
                if message:
                    errors.append(message)
            elif isinstance(error, str) and error.strip():
                errors.append(error.strip())

        return "\n".join(dict.fromkeys(errors))[-1200:].strip()

    def _collect_local_artifacts(self, workspace_dir: Path) -> List[Dict[str, Any]]:
        """扫描本地任务 workspace 中的产物。"""

        artifacts: List[Dict[str, Any]] = []
        seen_paths: set[Path] = set()
        for pattern in self.config.local_codex.artifact_globs:
            for path in workspace_dir.glob(pattern):
                if not path.is_file():
                    continue
                resolved_path = path.resolve()
                if resolved_path in seen_paths:
                    continue
                seen_paths.add(resolved_path)
                artifacts.append(
                    {
                        "name": path.name,
                        "path": str(resolved_path),
                        "url": str(resolved_path),
                        "size": path.stat().st_size,
                    }
                )
        return artifacts

    async def _send_progress_updates(self, task_state: RemoteTaskState, data: Dict[str, Any]) -> None:
        """发送远程任务新增进度。"""

        progress_items = _coerce_progress_items(data.get("progress") or data.get("events"))
        new_items = [item for item in progress_items if item["id"] not in task_state.sent_progress_ids]
        if not new_items:
            return

        now = time.monotonic()
        if now - task_state.last_progress_sent_at < max(float(self.config.progress.min_send_interval_seconds), 0.0):
            return

        max_items = max(int(self.config.progress.max_progress_items_per_message), 1)
        max_chars = max(int(self.config.progress.max_progress_item_chars), 20)
        selected_items = new_items[:max_items]
        for item in selected_items:
            task_state.sent_progress_ids.add(item["id"])

        message = _format_progress_message(
            task_state.task_id,
            [item["text"] for item in selected_items],
            max_chars,
        )
        await self._send_progress_text(task_state, message)
        task_state.last_progress_sent_at = now

    async def _send_final_result(self, task_state: RemoteTaskState, data: Dict[str, Any]) -> None:
        """发送任务结束消息和产物信息。"""

        status = self._normalize_status(data.get("status"), default=task_state.last_status)
        summary = str(
            data.get("summary")
            or data.get("final_message")
            or data.get("result")
            or data.get("message")
            or ""
        ).strip()
        error = str(data.get("error") or "").strip()
        status_text = {
            "succeeded": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
        }.get(status, status or "结束")

        lines = [f"{_display_task_kind(task_state.task_id)} {task_state.task_id} {status_text}。"]
        if summary:
            sanitized_summary = self._sanitize_task_output_text(task_state, summary)
            lines.append(_truncate_text(_plain_qq_text(sanitized_summary), max(int(self.config.progress.max_summary_chars), 100)))
        if error:
            sanitized_error = self._sanitize_task_output_text(task_state, error)
            lines.append(f"错误：{_truncate_text(_plain_qq_text(sanitized_error), 800)}")

        artifacts = _coerce_artifacts(data.get("artifacts") or data.get("files") or data.get("artifact"))
        if self.config.artifact.send_artifact_links and artifacts:
            lines.append("")
            lines.append("产物：")
            for artifact in artifacts:
                lines.append(self._format_artifact_line(artifact))

        # 最终结果默认回原聊天流。只有在 --dm 且配置允许时，才把最终摘要和产物一起私聊。
        # 这样默认行为仍适合群协作，想要更私密时再显式开启。
        final_message = "\n".join(lines).strip()
        private_final = bool(task_state.private_progress and self.config.progress.private_progress_send_artifacts)
        if private_final:
            if not await self._try_send_private_task_text(
                task_state,
                final_message,
                log_prefix="Codex 私聊最终结果发送失败",
            ):
                await self.ctx.send.text(final_message, task_state.stream_id)
        else:
            await self.ctx.send.text(final_message, task_state.stream_id)

        if self.config.napcat.enabled and artifacts:
            await self._upload_artifacts_via_napcat(task_state, artifacts, force_private=private_final)

        # send.custom 是早期兼容路径，主要给非 NapCat 适配器预留。
        # 如果最终结果配置成私聊，就不再额外走 send.custom，避免群里又出现一份文件提示。
        if self.config.artifact.try_custom_file_message and artifacts and not private_final:
            await self._try_send_custom_artifacts(task_state.stream_id, artifacts)

    def _format_artifact_line(self, artifact: Dict[str, Any]) -> str:
        """格式化产物信息。"""

        name = str(artifact.get("name") or artifact.get("filename") or "未命名产物").strip()
        url = str(artifact.get("download_url") or artifact.get("url") or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            url = ""
        size = artifact.get("size") or artifact.get("size_bytes")
        size_text = ""
        if isinstance(size, (int, float)) and size > 0:
            size_text = f" ({self._format_size(float(size))})"
        if url:
            return f"- {name}{size_text}: {url}"
        return f"- {name}{size_text}"

    @staticmethod
    def _format_size(size_bytes: float) -> str:
        """格式化文件大小。"""

        units = ["B", "KB", "MB", "GB"]
        value = size_bytes
        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(value)}{unit}"
                return f"{value:.1f}{unit}"
            value /= 1024
        return f"{size_bytes:.0f}B"

    async def _try_send_custom_artifacts(self, stream_id: str, artifacts: List[Dict[str, Any]]) -> None:
        """尝试用适配器自定义消息发送文件。"""

        for artifact in artifacts:
            custom_payload = artifact.get("custom_payload")
            if not isinstance(custom_payload, dict):
                url = str(artifact.get("url") or artifact.get("download_url") or "").strip()
                name = str(artifact.get("name") or artifact.get("filename") or "").strip()
                if not url:
                    continue
                custom_payload = {"name": name, "url": url}
            try:
                await self.ctx.send.custom(
                    self.config.artifact.custom_file_message_type,
                    custom_payload,
                    stream_id,
                    processed_plain_text=str(custom_payload.get("name") or "远程 Codex 产物"),
                )
            except Exception as exc:
                self.ctx.logger.warning("自定义文件消息发送失败: %s", exc)

    async def _upload_artifacts_via_napcat(
        self,
        task_state: RemoteTaskState,
        artifacts: List[Dict[str, Any]],
        force_private: bool = False,
    ) -> None:
        """通过 NapCat HTTP API 直传产物文件。"""

        failures: List[str] = []
        uncertain_failures: List[str] = []
        for artifact in artifacts:
            name = str(artifact.get("name") or artifact.get("filename") or "未命名产物").strip()
            try:
                await self._napcat_client.upload_artifact(task_state, artifact, force_private=force_private)
            except Exception as exc:
                self.ctx.logger.warning("NapCat 文件直传失败: %s", exc)
                failure_line = f"- {name}: {exc}"
                if self._is_uncertain_napcat_upload_failure(exc):
                    uncertain_failures.append(failure_line)
                else:
                    failures.append(failure_line)

        if failures:
            failure_message = "NapCat 文件直传失败：\n" + "\n".join(failures[:5])
            if force_private:
                # 产物私聊失败时，用户最重要的是拿到文件。
                # 因此回退到原聊天流再发一次，同时清掉 url，避免本地绝对路径被当链接展示。
                fallback_artifacts = []
                for artifact in artifacts:
                    fallback_artifact = dict(artifact)
                    fallback_artifact["url"] = ""
                    fallback_artifact["download_url"] = ""
                    fallback_artifacts.append(fallback_artifact)
                await self.ctx.send.text(
                    f"{failure_message}\n已回退到当前聊天发送产物。",
                    task_state.stream_id,
                )
                await self._upload_artifacts_via_napcat(task_state, fallback_artifacts, force_private=False)
            else:
                await self.ctx.send.text(failure_message, task_state.stream_id)
        elif uncertain_failures:
            self.ctx.logger.warning("NapCat 文件直传返回超时，可能已发送成功: %s", "; ".join(uncertain_failures[:5]))

    @staticmethod
    def _is_uncertain_napcat_upload_failure(exc: Exception) -> bool:
        """NapCat sendMsg 超时可能已经把文件发出，避免在群里误报失败。"""

        message = str(exc).lower()
        return "timeout" in message and ("sendmsg" in message or "ntevent" in message)

    async def _handle_status(self, stream_id: str, task_id: str) -> None:
        """处理任务状态查询命令。"""

        normalized_task_id = task_id.strip()
        if normalized_task_id:
            task_state = self._tasks.get(normalized_task_id)
            if task_state is None:
                await self.ctx.send.text(f"本地未跟踪任务：{normalized_task_id}", stream_id)
                return
            await self.ctx.send.text(
                f"任务 {task_state.task_id}: {task_state.last_status}\n"
                f"提示词：{_truncate_text(task_state.prompt, 300)}",
                stream_id,
            )
            return

        if not self._tasks:
            await self.ctx.send.text("当前没有本地跟踪的远程 Codex 任务。", stream_id)
            return

        lines = ["远程 Codex 任务："]
        for task_state in self._tasks.values():
            if task_state.stream_id != stream_id:
                continue
            lines.append(f"- {task_state.task_id}: {task_state.last_status} / {_truncate_text(task_state.prompt, 80)}")

        await self.ctx.send.text("\n".join(lines) if len(lines) > 1 else "当前聊天流没有远程 Codex 任务。", stream_id)

    async def _handle_cancel(self, stream_id: str, task_id: str) -> None:
        """处理任务取消命令。"""

        if not self.config.task.enable_cancel:
            await self.ctx.send.text("远程 Codex 任务取消功能未启用。", stream_id)
            return

        normalized_task_id = task_id.strip()
        if not normalized_task_id:
            await self.ctx.send.text("用法：/codex cancel <task_id>", stream_id)
            return

        task_state = self._tasks.get(normalized_task_id)
        if task_state is None:
            session_record = self._load_session_record(normalized_task_id)
            if session_record:
                latest_task_id = str(session_record.get("latest_task_id") or session_record.get("task_id") or "").strip()
                suffix = f"\n如需取消正在运行的 session 任务，请使用 task_id：/codex cancel {latest_task_id}" if latest_task_id else ""
                await self.ctx.send.text(
                    f"{normalized_task_id} 是 session 记录，不是正在运行的 task。\n"
                    f"cancel 不会删除 session，也不会清理历史文件。{suffix}",
                    stream_id,
                )
                return
            record = self._load_task_record(normalized_task_id)
            if record:
                status = self._normalize_status(record.get("last_status"), default="unknown")
                if status in TERMINAL_STATUSES or status == "watch_timeout":
                    await self.ctx.send.text(f"任务 {normalized_task_id} 已结束，当前状态：{status}，无需取消。", stream_id)
                    return
                await self.ctx.send.text(
                    f"任务 {normalized_task_id} 有本地记录，但当前进程未跟踪，不能终止正在运行的 Codex 进程。",
                    stream_id,
                )
                return
            await self.ctx.send.text(f"本地未跟踪任务：{normalized_task_id}", stream_id)
            return

        if task_state.last_status in TERMINAL_STATUSES or task_state.last_status == "watch_timeout":
            old_record = self._read_json_file(self._task_records_dir() / f"{task_state.task_id}.json") or {}
            old_artifacts = _coerce_artifacts(old_record.get("artifacts") or [])
            self._record_task_state(task_state, artifacts=old_artifacts)
            if task_state.record_type == "session" and task_state.session_name:
                self._update_session_from_task(task_state, artifacts=old_artifacts)
            await self.ctx.send.text(f"任务 {normalized_task_id} 已结束，当前状态：{task_state.last_status}，无需取消。", stream_id)
            return

        if task_state.process is not None and task_state.process.returncode is None:
            task_state.process.terminate()
            task_state.last_status = "cancelled"
            old_record = self._read_json_file(self._task_records_dir() / f"{task_state.task_id}.json") or {}
            old_artifacts = _coerce_artifacts(old_record.get("artifacts") or [])
            self._record_task_state(task_state, artifacts=old_artifacts)
            if task_state.record_type == "session" and task_state.session_name:
                self._update_session_from_task(task_state, artifacts=old_artifacts)
            if task_state.watch_task is not None and not task_state.watch_task.done():
                task_state.watch_task.cancel()
            await self.ctx.send.text(f"任务 {normalized_task_id}: 已请求终止本机 Codex 进程", stream_id)
            return

        if task_state.process is not None and task_state.process.returncode is not None:
            task_state.last_status = self._normalize_status(task_state.last_status, default="failed")
            old_record = self._read_json_file(self._task_records_dir() / f"{task_state.task_id}.json") or {}
            old_artifacts = _coerce_artifacts(old_record.get("artifacts") or [])
            self._record_task_state(task_state, artifacts=old_artifacts)
            if task_state.record_type == "session" and task_state.session_name:
                self._update_session_from_task(task_state, artifacts=old_artifacts)
            await self.ctx.send.text(f"任务 {normalized_task_id} 已结束，当前状态：{task_state.last_status}，无需取消。", stream_id)
            return

        if task_state.task_id.startswith("local_") or task_state.workspace_dir:
            task_state.last_status = "cancelled"
            old_record = self._read_json_file(self._task_records_dir() / f"{task_state.task_id}.json") or {}
            old_artifacts = _coerce_artifacts(old_record.get("artifacts") or [])
            self._record_task_state(task_state, artifacts=old_artifacts)
            if task_state.record_type == "session" and task_state.session_name:
                self._update_session_from_task(task_state, artifacts=old_artifacts)
            if task_state.watch_task is not None and not task_state.watch_task.done():
                task_state.watch_task.cancel()
            await self.ctx.send.text(f"任务 {normalized_task_id}: 已标记为取消。", stream_id)
            return

        try:
            data = await self._client.cancel_task(normalized_task_id)
        except Exception as exc:
            await self.ctx.send.text(f"取消远程 Codex 任务失败：{exc}", stream_id)
            return

        task_state.last_status = self._normalize_status(data.get("status"), default="cancelled")
        old_record = self._read_json_file(self._task_records_dir() / f"{task_state.task_id}.json") or {}
        old_artifacts = _coerce_artifacts(old_record.get("artifacts") or [])
        self._record_task_state(task_state, artifacts=old_artifacts)
        if task_state.record_type == "session" and task_state.session_name:
            self._update_session_from_task(task_state, artifacts=old_artifacts)
        if task_state.watch_task is not None and not task_state.watch_task.done():
            task_state.watch_task.cancel()
        message = str(data.get("message") or "已请求取消远程任务").strip()
        await self.ctx.send.text(f"任务 {normalized_task_id}: {message}", stream_id)

    async def _handle_skills(self, stream_id: str) -> None:
        """列出可用 Codex skills。"""

        skills = self._list_codex_skills()
        if not skills:
            await self.ctx.send.text("当前未发现可用 Codex skill。", stream_id)
            return

        lines = ["可用 Codex skills："]
        for index, skill in enumerate(skills[:30], start=1):
            name = skill.get("name") or "未命名"
            description = self._localize_skill_description(name, skill.get("description") or "")
            lines.append(f"{index}. {name}：{_truncate_text(description, 80)}")
        if len(skills) > 30:
            lines.append(f"还有 {len(skills) - 30} 个未显示。")
        await self.ctx.send.text("\n".join(lines), stream_id)

    async def _handle_mcp(self, stream_id: str) -> None:
        """列出当前 Codex MCP 服务器。"""

        try:
            servers = await asyncio.to_thread(self._list_codex_mcp_servers)
        except Exception as exc:
            await self.ctx.send.text(f"读取 Codex MCP 配置失败：{exc}", stream_id)
            return

        if not servers:
            await self.ctx.send.text("当前未配置 Codex MCP 服务器。", stream_id)
            return

        lines = ["Codex MCP 服务器："]
        for index, server in enumerate(servers[:30], start=1):
            name = str(server.get("name") or "未命名").strip()
            enabled = server.get("enabled")
            status = "启用" if enabled is not False else "停用"
            transport = str(server.get("transport") or server.get("type") or "").strip()
            detail = str(server.get("command") or server.get("url") or "").strip()
            parts = [status]
            if transport:
                parts.append(transport)
            if detail:
                parts.append(_truncate_text(detail, 60))
            description = self._localize_mcp_description(server)
            lines.append(f"{index}. {name}：{description}（{' / '.join(parts)}）")
        if len(servers) > 30:
            lines.append(f"还有 {len(servers) - 30} 个未显示。")
        lines.append("")
        lines.append("默认策略：任务会使用本机 Codex 当前启用的 MCP。")
        await self.ctx.send.text("\n".join(lines), stream_id)

    async def _handle_config(self, stream_id: str) -> None:
        """列出当前插件和 Codex 关键配置。"""

        user_config = self._read_codex_user_config()
        local_model = str(self.config.local_codex.model or "").strip()
        default_model = str(user_config.get("model") or "").strip()
        model = local_model or default_model or "Codex 默认模型"
        reasoning = str(user_config.get("model_reasoning_effort") or "未设置").strip()
        provider = str(user_config.get("model_provider") or "默认").strip()
        lines = [
            "Codex 当前配置：",
            f"模型：{model}",
            f"推理强度：{reasoning}",
            f"模型提供方：{provider}",
            f"执行模式：{self._get_execution_mode()}",
            f"沙箱：{self.config.local_codex.sandbox}",
            f"审批策略：{self.config.local_codex.approval_policy}",
            f"联网搜索：{'启用' if self.config.local_codex.enable_search else '停用'}",
            f"进度转发：{'启用' if self.config.progress.forward_progress else '停用'}",
            f"NapCat 直传：{'启用' if self.config.napcat.enabled else '停用'}",
            f"启动清理 task 记录：{'启用' if self.config.task.auto_cleanup_task_records else '停用'}",
            f"自动清理 task 文件：{'启用' if self.config.task.auto_cleanup_task_workspaces else '停用'}",
            f"定时清理：{'启用' if self.config.task.enable_periodic_cleanup else '停用'}",
            f"定时清理间隔：{self.config.task.periodic_cleanup_interval_minutes} 分钟",
            f"输入材料自动清理：{'启用' if self.config.input_file.auto_cleanup_input_files else '停用'}",
            f"输入材料 TTL：{self.config.input_file.input_file_ttl_hours} 小时",
        ]
        if local_model:
            lines.append("说明：模型来自插件 local_codex.model。")
        elif default_model:
            lines.append("说明：模型来自本机 Codex 用户配置。")
        else:
            lines.append("说明：模型未显式配置，由 Codex CLI 自行选择默认值。")
        await self.ctx.send.text("\n".join(lines), stream_id)

    async def _handle_list(self, stream_id: str, platform: str, user_id: str, arg: str = "") -> None:
        """列出当前用户可见的最近 task/session。"""

        if arg.strip().lower() in {"all", "全部", "global", "全局"}:
            await self._handle_list_all(stream_id=stream_id, platform=platform, user_id=user_id)
            return

        scoped_user = self._build_scoped_user(platform, user_id)
        task_records = self._list_task_records(stream_id=stream_id, scoped_user=scoped_user, limit=8)
        session_records = self._list_session_records(stream_id=stream_id, scoped_user=scoped_user, limit=8)
        lines = ["Codex 记录："]
        if session_records:
            lines.append("session：")
            for record in session_records:
                history = [item for item in record.get("history", []) if isinstance(item, dict)]
                lines.append(
                    f"- {record.get('session_name')}: {record.get('last_status', 'unknown')} / "
                    f"{len(history) or len(record.get('task_ids') or [])} 条记录"
                )
                recent_history = history[-5:]
                for item in recent_history:
                    artifact_count = len(_coerce_artifacts(item.get("artifacts") or []))
                    artifact_text = f"，产物 {artifact_count} 个" if artifact_count else ""
                    lines.append(
                        f"  - {item.get('task_id')}: {item.get('last_status', 'unknown')}{artifact_text} / "
                        f"{_truncate_text(str(item.get('prompt') or ''), 44)}"
                    )
        if task_records:
            lines.append("task：")
            for record in task_records:
                lines.append(
                    f"- {record.get('task_id')}: {record.get('last_status', 'unknown')} / "
                    f"{_truncate_text(str(record.get('prompt') or ''), 50)}"
                )
        if len(lines) == 1:
            lines.append("当前没有可显示的记录。")
        await self.ctx.send.text("\n".join(lines), stream_id)

    async def _handle_list_all(self, stream_id: str, platform: str, user_id: str) -> None:
        """管理员列出所有聊天流的最近 task/session。"""

        if not self._is_admin_user(platform, user_id):
            await self.ctx.send.text("只有管理员可以查看所有聊天流的 Codex 记录。", stream_id)
            return

        session_records = self._list_all_session_records(limit=12)
        task_records = self._list_all_task_records(limit=20)
        lines = ["Codex 全局记录："]
        if session_records:
            lines.append("session：")
            for record in session_records:
                history = [item for item in record.get("history", []) if isinstance(item, dict)]
                lines.append(
                    f"- {record.get('session_name')}: {record.get('last_status', 'unknown')} / "
                    f"{record.get('stream_id', '')} / {self._build_scoped_user(str(record.get('platform') or ''), str(record.get('user_id') or ''))} / "
                    f"{len(history) or len(record.get('task_ids') or [])} 条记录"
                )
        if task_records:
            lines.append("task：")
            for record in task_records:
                lines.append(
                    f"- {record.get('task_id')}: {record.get('last_status', 'unknown')} / "
                    f"{record.get('stream_id', '')} / {self._build_scoped_user(str(record.get('platform') or ''), str(record.get('user_id') or ''))} / "
                    f"{_truncate_text(str(record.get('prompt') or ''), 36)}"
                )
        if len(lines) == 1:
            lines.append("当前没有全局记录。")
        lines.append("")
        lines.append("删除：/codex clean task <task_id> 或 /codex clean session <session名>")
        lines.append("删除 session 需要按提示二次确认。")
        await self.ctx.send.text("\n".join(lines), stream_id)

    async def _handle_clean(self, stream_id: str, platform: str, user_id: str, arg: str) -> None:
        """管理员清理 task/session 记录和文件。"""

        if not self._is_admin_user(platform, user_id):
            await self.ctx.send.text("只有管理员可以使用 /codex clean。", stream_id)
            return

        stripped = arg.strip()
        if not stripped or stripped.lower() in {"expired", "过期"}:
            result = self._cleanup_expired_task_records(force=True)
            input_result = self._cleanup_expired_input_files(force=True)
            await self.ctx.send.text(
                f"Codex 清理完成。\n"
                f"过期 task 记录：{result['records']} 个\n"
                f"删除 task 文件目录：{result['workspaces']} 个\n"
                f"过期输入材料：{input_result['files']} 个",
                stream_id,
            )
            return

        parts = stripped.split(maxsplit=2)
        action = parts[0].lower()
        if action in {"task", "任务"} and len(parts) >= 2:
            task_id = parts[1].strip()
            task_state = self._tasks.get(task_id)
            if task_state and task_state.last_status in ACTIVE_STATUSES:
                await self.ctx.send.text(f"任务 {task_id} 仍在运行，请先 /codex cancel {task_id}。", stream_id)
                return
            record_deleted, workspace_deleted = self._delete_task_record_by_id(task_id, delete_workspace=True)
            if not record_deleted and not workspace_deleted:
                await self.ctx.send.text(f"未找到可清理的 task：{task_id}", stream_id)
                return
            await self.ctx.send.text(
                f"已清理 task：{task_id}\n记录：{'已删除' if record_deleted else '未找到'}\n文件目录：{'已删除' if workspace_deleted else '未找到'}",
                stream_id,
            )
            return

        if action in {"session", "会话"} and len(parts) >= 2:
            session_name = parts[1].strip()
            normalized_session = self._safe_record_name(session_name)
            confirm = len(parts) >= 3 and parts[2].lower() in {"confirm", "确认"}
            active_session_tasks = [
                task_state.task_id
                for task_state in self._tasks.values()
                if task_state.session_name == normalized_session and task_state.last_status in ACTIVE_STATUSES
            ]
            if active_session_tasks:
                await self.ctx.send.text(
                    f"session {normalized_session} 仍有运行中的任务：{active_session_tasks[0]}\n请先 /codex cancel {active_session_tasks[0]}。",
                    stream_id,
                )
                return
            session_record = self._load_session_record(normalized_session)
            if not session_record:
                await self.ctx.send.text(f"未找到可清理的 session：{session_name}", stream_id)
                return
            history = [item for item in session_record.get("history", []) if isinstance(item, dict)]
            task_count = len(history) or len(session_record.get("task_ids") or [])
            if not confirm:
                await self.ctx.send.text(
                    f"将删除 session：{normalized_session}\n"
                    f"关联 task：{task_count} 个\n"
                    "会同时删除这些 task 的本地记录、workspace、输入材料和产物文件。\n"
                    f"确认执行：/codex clean session {normalized_session} confirm",
                    stream_id,
                )
                return
            record_deleted, workspace_count = self._delete_session_record_by_name(normalized_session)
            if not record_deleted and workspace_count == 0:
                await self.ctx.send.text(f"未找到可清理的 session：{session_name}", stream_id)
                return
            await self.ctx.send.text(
                f"已清理 session：{normalized_session}\n"
                f"记录：{'已删除' if record_deleted else '未找到'}\n"
                f"关联 task 文件目录：已删除 {workspace_count} 个",
                stream_id,
            )
            return

        if action in {"input", "inputs", "material", "materials", "材料", "输入材料"}:
            input_result = self._cleanup_expired_input_files(force=True)
            await self.ctx.send.text(
                f"输入材料清理完成。\n"
                f"清理记录：{input_result['records']} 个\n"
                f"删除文件：{input_result['files']} 个\n"
                f"删除空目录：{input_result['dirs']} 个",
                stream_id,
            )
            return

        await self.ctx.send.text(
            "用法：\n"
            "/codex clean 清理过期普通 task\n"
            "/codex clean input 清理过期输入材料\n"
            "/codex clean task <task_id> 删除指定 task 记录和文件\n"
            "/codex clean session <session名> 查看删除影响并要求确认\n"
            "/codex clean session <session名> confirm 确认删除 session 和关联 task 文件",
            stream_id,
        )

    async def _handle_session_command(
        self,
        arg: str,
        stream_id: str,
        platform: str,
        user_id: str,
        group_id: str,
        command_message: Any = None,
    ) -> tuple[bool, str]:
        """处理 /codex session。"""

        if self._get_execution_mode() != "local":
            await self.ctx.send.text("session 仅支持本机 local 模式。", stream_id)
            return False, "session 不支持远程模式"

        stripped = arg.strip()
        if not stripped:
            await self._handle_list(stream_id=stream_id, platform=platform, user_id=user_id)
            return True, "已查询 session"

        parts = stripped.split(maxsplit=2)
        target = parts[0]
        if self._looks_like_task_id(target):
            confirm = len(parts) >= 2 and parts[1].lower() in {"confirm", "确认"}
            session_name = parts[2].strip() if len(parts) >= 3 else target
            if not confirm and self.config.task.require_session_confirm:
                await self.ctx.send.text(
                    f"将 task 转为 session 需要确认。\n用法：/codex session {target} confirm {session_name}",
                    stream_id,
                )
                return True, "已请求 session 转换确认"
            await self._convert_task_to_session(target, session_name, stream_id, platform, user_id)
            return True, "已处理 session 转换"

        if len(parts) < 2:
            await self.ctx.send.text("用法：/codex session <会话名> <任务描述>", stream_id)
            return False, "session 参数不足"

        session_name = self._safe_record_name(parts[0])
        prompt = stripped[len(parts[0]) :].strip()
        prompt, private_progress = self._parse_private_progress_args(prompt)
        if not session_name or not prompt:
            await self.ctx.send.text("用法：/codex session <会话名> [--dm] <任务描述>", stream_id)
            return False, "session 参数不足"
        path_error = self._check_prompt_local_path_access(prompt)
        if path_error:
            await self.ctx.send.text(path_error, stream_id)
            return False, path_error
        if self._load_session_record(session_name):
            await self.ctx.send.text(f"session 已存在：{session_name}\n继续它：/codex continue {prompt}", stream_id)
            return False, "session 已存在"
        dangerous_error = self._check_dangerous_local_permission(platform=platform, user_id=user_id)
        if dangerous_error:
            await self.ctx.send.text(dangerous_error, stream_id)
            return False, dangerous_error

        result = await self._create_local_task(
            prompt=prompt,
            raw_command=f"/codex session {stripped}",
            stream_id=stream_id,
            platform=platform,
            user_id=user_id,
            group_id=group_id,
            command_message=command_message,
            record_type="session",
            session_name=session_name,
            private_progress=private_progress,
        )
        return result[0], result[1]

    async def _handle_continue_command(
        self,
        prompt: str,
        stream_id: str,
        platform: str,
        user_id: str,
        group_id: str,
    ) -> tuple[bool, str]:
        """继续当前用户最近 session。"""

        if self._get_execution_mode() != "local":
            await self.ctx.send.text("continue 仅支持本机 local 模式。", stream_id)
            return False, "continue 不支持远程模式"
        dangerous_error = self._check_dangerous_local_permission(platform=platform, user_id=user_id)
        if dangerous_error:
            await self.ctx.send.text(dangerous_error, stream_id)
            return False, dangerous_error
        prompt, private_progress = self._parse_private_progress_args(prompt)
        if not prompt.strip():
            await self.ctx.send.text("用法：/codex continue [--dm] <继续处理的要求>", stream_id)
            return False, "continue 缺少要求"
        path_error = self._check_prompt_local_path_access(prompt)
        if path_error:
            await self.ctx.send.text(path_error, stream_id)
            return False, path_error

        scoped_user = self._build_scoped_user(platform, user_id)
        sessions = self._list_session_records(stream_id=stream_id, scoped_user=scoped_user, limit=1)
        if not sessions:
            await self.ctx.send.text("当前用户在这个聊天流里没有可继续的 session。", stream_id)
            return False, "没有可继续 session"
        session_record = sessions[0]
        thread_id = str(session_record.get("codex_thread_id") or "").strip()
        if not thread_id:
            await self.ctx.send.text("最近 session 缺少 Codex thread_id，不能继续。", stream_id)
            return False, "session 缺少 thread_id"
        result = await self._create_local_task(
            prompt=prompt,
            raw_command=f"/codex continue {prompt}",
            stream_id=stream_id,
            platform=platform,
            user_id=user_id,
            group_id=group_id,
            record_type="session",
            session_name=str(session_record.get("session_name") or ""),
            resume_thread_id=thread_id,
            parent_task_id=str(session_record.get("latest_task_id") or session_record.get("task_id") or ""),
            private_progress=private_progress,
        )
        return result[0], result[1]

    async def _handle_resume_command(
        self,
        arg: str,
        stream_id: str,
        platform: str,
        user_id: str,
        group_id: str,
        command_message: Any = None,
    ) -> tuple[bool, str]:
        """恢复指定 task/session/thread。"""

        del command_message
        if self._get_execution_mode() != "local":
            await self.ctx.send.text("resume 仅支持本机 local 模式。", stream_id)
            return False, "resume 不支持远程模式"
        dangerous_error = self._check_dangerous_local_permission(platform=platform, user_id=user_id)
        if dangerous_error:
            await self.ctx.send.text(dangerous_error, stream_id)
            return False, dangerous_error
        parts = arg.strip().split(maxsplit=1)
        if len(parts) < 2:
            await self.ctx.send.text("用法：/codex resume <task_id|session名|thread_id> <继续处理的要求>", stream_id)
            return False, "resume 参数不足"

        target, prompt = parts[0], parts[1].strip()
        prompt, private_progress = self._parse_private_progress_args(prompt)
        if not prompt:
            await self.ctx.send.text("用法：/codex resume <task_id|session名|thread_id> [--dm] <继续处理的要求>", stream_id)
            return False, "resume 缺少要求"
        path_error = self._check_prompt_local_path_access(prompt)
        if path_error:
            await self.ctx.send.text(path_error, stream_id)
            return False, path_error
        record = self._resolve_resume_record(target)
        thread_id = str((record or {}).get("codex_thread_id") or "").strip()
        if not thread_id and self._looks_like_thread_id(target):
            thread_id = target
        if not thread_id:
            await self.ctx.send.text(f"未找到可恢复的 Codex thread：{target}", stream_id)
            return False, "未找到 thread"

        session_name = str((record or {}).get("session_name") or "")
        record_type = "session" if session_name else "task"
        result = await self._create_local_task(
            prompt=prompt,
            raw_command=f"/codex resume {arg}",
            stream_id=stream_id,
            platform=platform,
            user_id=user_id,
            group_id=group_id,
            record_type=record_type,
            session_name=session_name,
            resume_thread_id=thread_id,
            parent_task_id=str((record or {}).get("latest_task_id") or (record or {}).get("task_id") or ""),
            private_progress=private_progress,
        )
        return result[0], result[1]

    def _get_execution_mode(self) -> str:
        """返回规范化执行模式。"""

        return str(self.config.task.execution_mode or "local").strip().lower()

    def _list_codex_skills(self) -> List[Dict[str, str]]:
        """扫描 CODEX_HOME 下的 skills。"""

        skills_root = self._codex_home() / "skills"
        if not skills_root.exists() or not skills_root.is_dir():
            return []

        skills: List[Dict[str, str]] = []
        for skill_file in sorted(skills_root.glob("**/SKILL.md")):
            parsed = self._parse_skill_file(skill_file)
            if parsed:
                skills.append(parsed)
        skills.sort(key=lambda item: item.get("name", "").lower())
        return skills

    @staticmethod
    def _parse_skill_file(skill_file: Path) -> Dict[str, str]:
        """读取一个 SKILL.md 的名称和描述。"""

        try:
            text = skill_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}

        metadata: Dict[str, str] = {}
        if text.startswith("---"):
            end_index = text.find("\n---", 3)
            if end_index != -1:
                for line in text[3:end_index].splitlines():
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    metadata[key.strip().lower()] = value.strip().strip('"').strip("'")

        name = metadata.get("name") or skill_file.parent.name
        description = metadata.get("description") or ""
        return {"name": name, "description": description}

    @staticmethod
    def _localize_skill_description(name: str, description: str) -> str:
        """优先返回 skill 的中文短描述。"""

        normalized_name = str(name or "").strip().lower()
        known_descriptions = {
            "imagegen": "生成或编辑位图图片，适合照片、插画、贴图、素材和 UI 视觉稿。",
            "openai-docs": "查询 OpenAI 和 Codex 官方文档，适合确认最新能力、配置和用法。",
            "plugin-creator": "创建或维护 Codex 插件结构，生成插件清单和基础目录。",
            "skill-creator": "创建或更新 Codex skill，整理可复用的专业工作流和说明。",
            "skill-installer": "安装 Codex skills，支持官方精选 skill 或 GitHub 仓库路径。",
        }
        if normalized_name in known_descriptions:
            return known_descriptions[normalized_name]

        cleaned = " ".join(str(description or "").split())
        if not cleaned:
            return "暂无描述。"
        if _contains_cjk(cleaned):
            return cleaned
        return f"暂无中文描述。原文：{_truncate_text(cleaned, 48)}"

    def _list_codex_mcp_servers(self) -> List[Dict[str, Any]]:
        """调用 codex mcp list --json 并规整输出。"""

        command = [self.config.local_codex.codex_binary.strip() or "codex", "mcp", "list", "--json"]
        result = subprocess.run(
            command,
            cwd=str(Path.cwd()),
            env=self._build_local_codex_env(),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip() or f"退出码 {result.returncode}"
            raise RuntimeError(_truncate_text(stderr, 500))

        output = result.stdout.strip()
        if not output:
            return []
        data = json.loads(output)
        if isinstance(data, list):
            return [self._normalize_mcp_server(item) for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            if isinstance(data.get("servers"), list):
                return [self._normalize_mcp_server(item) for item in data["servers"] if isinstance(item, dict)]
            return [self._normalize_mcp_server({"name": name, **value}) for name, value in data.items() if isinstance(value, dict)]
        return []

    @staticmethod
    def _normalize_mcp_server(server: Dict[str, Any]) -> Dict[str, Any]:
        """规整不同版本 codex mcp list 的字段。"""

        normalized = dict(server)
        name = normalized.get("name") or normalized.get("id") or normalized.get("server")
        normalized["name"] = str(name or "").strip() or "未命名"
        if "enabled" not in normalized:
            normalized["enabled"] = True
        if "transport" not in normalized:
            if normalized.get("url"):
                normalized["transport"] = "http"
            elif normalized.get("command"):
                normalized["transport"] = "stdio"
        return normalized

    @staticmethod
    def _localize_mcp_description(server: Dict[str, Any]) -> str:
        """优先返回 MCP 服务器的中文短描述。"""

        name = str(server.get("name") or "").strip()
        normalized_name = name.lower()
        known_descriptions = {
            "openaiDeveloperDocs": "OpenAI 官方开发者文档查询服务。",
            "openaideveloperdocs": "OpenAI 官方开发者文档查询服务。",
            "github": "GitHub 仓库、议题和代码相关操作服务。",
            "filesystem": "本地文件系统读写服务。",
            "playwright": "浏览器自动化和页面测试服务。",
            "postgres": "PostgreSQL 数据库访问服务。",
            "sqlite": "SQLite 数据库访问服务。",
            "fetch": "网页或 HTTP 内容读取服务。",
            "memory": "长期记忆或知识存储服务。",
        }
        if normalized_name in known_descriptions:
            return known_descriptions[normalized_name]

        raw_description = str(
            server.get("description")
            or server.get("summary")
            or server.get("title")
            or ""
        ).strip()
        if raw_description:
            cleaned = " ".join(raw_description.split())
            if _contains_cjk(cleaned):
                return _truncate_text(cleaned, 60)
            return f"暂无中文描述，原文：{_truncate_text(cleaned, 36)}"

        transport = str(server.get("transport") or server.get("type") or "").strip().lower()
        if transport == "stdio":
            return "通过本地命令启动的 MCP 服务。"
        if transport in {"http", "sse", "streamable-http"}:
            return "通过网络地址连接的 MCP 服务。"
        return "暂无中文描述。"

    def _read_codex_user_config(self) -> Dict[str, Any]:
        """读取本机 Codex 用户配置。"""

        config_path = self._codex_home() / "config.toml"
        if not config_path.exists():
            return {}
        try:
            with config_path.open("rb") as file:
                data = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _list_task_records(self, stream_id: str, scoped_user: str, limit: int = 10) -> List[Dict[str, Any]]:
        """列出当前用户 task 记录。"""

        records = []
        for path in self._task_records_dir().glob("*.json"):
            record = self._read_json_file(path)
            if not record:
                continue
            if str(record.get("stream_id") or "") != stream_id:
                continue
            record_user = self._build_scoped_user(str(record.get("platform") or ""), str(record.get("user_id") or ""))
            if scoped_user and record_user != scoped_user:
                continue
            if str(record.get("record_type") or "task") == "session":
                continue
            records.append(record)
        records.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0), reverse=True)
        return records[:limit]

    def _list_session_records(self, stream_id: str, scoped_user: str, limit: int = 10) -> List[Dict[str, Any]]:
        """列出当前用户 session 记录。"""

        records = []
        for path in self._session_records_dir().glob("*.json"):
            record = self._read_json_file(path)
            if not record:
                continue
            if str(record.get("stream_id") or "") != stream_id:
                continue
            record_user = self._build_scoped_user(str(record.get("platform") or ""), str(record.get("user_id") or ""))
            if scoped_user and record_user != scoped_user:
                continue
            record = self._hydrate_session_history(record)
            records.append(record)
        records.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0), reverse=True)
        return records[:limit]

    def _list_all_task_records(self, limit: int = 30) -> List[Dict[str, Any]]:
        """列出所有聊天流的 task 记录。"""

        records = []
        for path in self._task_records_dir().glob("*.json"):
            record = self._read_json_file(path)
            if not record:
                continue
            records.append(record)
        records.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0), reverse=True)
        return records[:limit]

    def _list_all_session_records(self, limit: int = 30) -> List[Dict[str, Any]]:
        """列出所有聊天流的 session 记录。"""

        records = []
        for path in self._session_records_dir().glob("*.json"):
            record = self._read_json_file(path)
            if not record:
                continue
            records.append(self._hydrate_session_history(record))
        records.sort(key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0), reverse=True)
        return records[:limit]

    def _resolve_resume_record(self, target: str) -> Optional[Dict[str, Any]]:
        """按 task_id 或 session 名找到可恢复记录。"""

        if self._looks_like_task_id(target):
            return self._load_task_record(target)
        session_record = self._load_session_record(target)
        if session_record:
            return session_record
        return None

    async def _convert_task_to_session(
        self,
        task_id: str,
        session_name: str,
        stream_id: str,
        platform: str,
        user_id: str,
    ) -> None:
        """把已有 task 转为 session。"""

        record = self._load_task_record(task_id)
        if not record:
            await self.ctx.send.text(f"未找到 task：{task_id}", stream_id)
            return
        if str(record.get("stream_id") or "") != stream_id:
            await self.ctx.send.text("不能把其他聊天流的 task 转为 session。", stream_id)
            return
        scoped_user = self._build_scoped_user(platform, user_id)
        record_user = self._build_scoped_user(str(record.get("platform") or ""), str(record.get("user_id") or ""))
        if scoped_user and record_user != scoped_user:
            await self.ctx.send.text("不能把其他用户的 task 转为 session。", stream_id)
            return
        if not str(record.get("codex_thread_id") or "").strip():
            await self.ctx.send.text("这个 task 缺少 Codex thread_id，不能转为 session。", stream_id)
            return
        name = self._safe_record_name(session_name or task_id)
        if self._load_session_record(name):
            await self.ctx.send.text(f"session 已存在：{name}", stream_id)
            return
        record["record_type"] = "session"
        record["session_name"] = name
        record["latest_task_id"] = task_id
        record["history"] = [self._task_record_to_history_item(record)]
        record["task_ids"] = [task_id]
        self._write_session_record(record)
        await self.ctx.send.text(f"已将 task 转为 session：{name}\n继续：/codex continue <要求>", stream_id)

    @staticmethod
    def _looks_like_task_id(value: str) -> bool:
        """判断是否像插件 task_id。"""

        return bool(re.match(r"^(?:local|remote)_\d{8}_\d{6}_[0-9a-fA-F]+$", str(value or "").strip()))

    @staticmethod
    def _looks_like_thread_id(value: str) -> bool:
        """判断是否像 Codex thread/session id。"""

        text = str(value or "").strip()
        return bool(re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{13,}$", text) or text.startswith("019"))

    @staticmethod
    def _codex_home() -> Path:
        """返回 Codex 本地状态目录。"""

        return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()

    @staticmethod
    def _build_help_text(prefix: str) -> str:
        """构造帮助文本。"""

        escaped_prefix = prefix or "/codex"
        return (
            "远程 Codex Agent 命令：\n"
            f"{escaped_prefix} <任务描述> 创建一次性 task\n"
            f"{escaped_prefix} --dm <任务描述> 阶段性进度尝试私聊发送给你\n"
            f"{escaped_prefix} session <会话名> [--dm] <任务描述> 创建持久 session\n"
            f"{escaped_prefix} session <task_id> confirm [会话名] 将 task 转为 session\n"
            f"{escaped_prefix} continue [--dm] <要求> 继续当前用户最近 session\n"
            f"{escaped_prefix} resume <task_id|session名|thread_id> [--dm] <要求> 恢复指定对话\n"
            f"{escaped_prefix} list 查看当前用户记录\n"
            f"{escaped_prefix} list all 管理员查看所有聊天流记录\n"
            f"{escaped_prefix} status 查看当前聊天流任务\n"
            f"{escaped_prefix} status <task_id> 查看指定任务\n"
            f"{escaped_prefix} cancel <task_id> 取消指定任务\n"
            f"{escaped_prefix} clean 管理员清理过期 task\n"
            f"{escaped_prefix} clean input 管理员清理过期输入材料\n"
            f"{escaped_prefix} clean task <task_id> 管理员删除指定 task 记录和文件\n"
            f"{escaped_prefix} clean session <会话名> 管理员查看 session 删除影响\n"
            f"{escaped_prefix} clean session <会话名> confirm 管理员确认删除 session 和关联文件\n"
            f"{escaped_prefix} skills 查看可用 Codex skills\n"
            f"{escaped_prefix} mcp 查看当前 Codex MCP 服务器\n"
            f"{escaped_prefix} config 查看当前模型和运行配置\n"
            f"{escaped_prefix} help 查看帮助\n"
            "示例：\n"
            f"{escaped_prefix} 搜索某主题并生成一份 Word 文档，完成后返回下载链接"
        )

    @staticmethod
    def _normalize_status(raw_status: Any, default: str = "running") -> str:
        """规范化远程任务状态。"""

        status = str(raw_status or default or "running").strip().lower()
        return STATUS_ALIASES.get(status, status)


def create_plugin() -> RemoteCodexAgentPlugin:
    """创建插件实例。"""

    return RemoteCodexAgentPlugin()
