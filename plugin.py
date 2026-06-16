"""Codex CLI QQ 调度插件。

默认在 MaiBot 所在服务器上直接运行本机 Codex CLI。
同时保留 remote 模式，用于后续对接独立 HTTP Agent 服务。
"""

from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
from typing import Any, ClassVar, Dict, List, Optional
from uuid import uuid4
from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

import asyncio
import httpx
import json
import time


PLUGIN_ID = "local.remote-codex-agent"
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

    config_version: str = Field(default="0.3.0", description="配置版本号")
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

    allow_all_users: bool = Field(default=False, description="是否允许所有用户触发")
    allowed_users: List[str] = Field(default_factory=list, description="允许触发的用户，推荐格式 qq:用户ID")
    allowed_groups: List[str] = Field(default_factory=list, description="允许触发的群号、qq:群号 或 stream_id")


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


class LocalCodexConfig(PluginConfigBase):
    """本机 Codex CLI 配置。"""

    __ui_label__: ClassVar[str] = "本地 Codex"
    __ui_icon__: ClassVar[str] = "terminal"
    __ui_order__: ClassVar[int] = 4

    codex_binary: str = Field(default="codex", description="Codex CLI 可执行文件名或绝对路径")
    work_root: str = Field(default="data/remote_codex_agent/tasks", description="本地任务根目录")
    sandbox: str = Field(default="workspace-write", description="Codex CLI 沙箱模式")
    approval_policy: str = Field(default="never", description="Codex CLI 审批策略")
    model: str = Field(default="", description="可选模型名")
    enable_search: bool = Field(default=False, description="是否启用 Codex CLI --search")
    extra_args: List[str] = Field(default_factory=list, description="额外传给 codex exec 的参数")
    env_file: str = Field(default="plugins/remote_codex_agent/.env.local", description="可选环境变量文件，供本机 Codex 子进程使用")
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
    allowed_local_roots: List[str] = Field(default_factory=list, description="允许复制的本地文件根目录，空列表表示不限制")


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
    watch_task: Optional[asyncio.Task[None]] = None
    process: Optional[asyncio.subprocess.Process] = None
    workspace_dir: str = ""
    final_message_path: str = ""
    input_files: List[InputFile] = field(default_factory=list)


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

    async def upload_artifact(self, task_state: RemoteTaskState, artifact: Dict[str, Any]) -> Dict[str, Any]:
        """上传一个产物到当前 QQ 群聊或私聊。"""

        file_value = self._extract_file_value(artifact)
        name = self._extract_name(artifact, file_value)
        self._check_file_size(artifact, file_value)

        group_id = str(task_state.group_id or "").strip()
        user_id = str(task_state.user_id or "").strip()
        if group_id:
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

    async def on_load(self) -> None:
        """插件加载时初始化运行态。"""

        self._client.update_config(self.config)
        self._napcat_client.update_config(self.config)

    async def on_unload(self) -> None:
        """插件卸载时停止本地轮询任务。"""

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
        await self._client.close()
        await self._napcat_client.close()

    async def on_config_update(self, scope: str, config_data: Dict[str, object], version: str) -> None:
        """处理配置热重载。"""

        del scope, config_data, version
        await self._client.close()
        await self._napcat_client.close()
        self._client.update_config(self.config)
        self._napcat_client.update_config(self.config)

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

        if execution_mode == "local":
            return await self._create_local_task(
                prompt=command_body,
                raw_command=raw_command,
                stream_id=stream_id,
                platform=platform,
                user_id=user_id,
                group_id=group_id,
                command_message=kwargs.get("message"),
            )

        return await self._create_remote_task(
            prompt=command_body,
            raw_command=raw_command,
            stream_id=stream_id,
            platform=platform,
            user_id=user_id,
            group_id=group_id,
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

    def _check_permission(self, platform: str, user_id: str, group_id: str, stream_id: str) -> str:
        """检查当前用户和聊天流是否允许触发。"""

        permission = self.config.permission
        normalized_platform = str(platform or "").strip().lower()
        normalized_user_id = str(user_id or "").strip().lower()
        normalized_group_id = str(group_id or "").strip().lower()
        normalized_stream_id = str(stream_id or "").strip().lower()

        if not permission.allow_all_users:
            allowed_users = _normalize_set(permission.allowed_users)
            user_candidates = {normalized_user_id}
            if normalized_platform and normalized_user_id:
                user_candidates.add(f"{normalized_platform}:{normalized_user_id}")
            if not allowed_users or allowed_users.isdisjoint(user_candidates):
                return "你没有权限触发远程 Codex Agent。"

        allowed_groups = _normalize_set(permission.allowed_groups)
        if allowed_groups:
            group_candidates = {normalized_stream_id, normalized_group_id}
            if normalized_platform and normalized_group_id:
                group_candidates.add(f"{normalized_platform}:{normalized_group_id}")
            if allowed_groups.isdisjoint({candidate for candidate in group_candidates if candidate}):
                return "当前聊天流不允许触发远程 Codex Agent。"

        return ""

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
        )
        self._tasks[task_id] = task_state
        task_state.watch_task = asyncio.create_task(self._watch_remote_task(task_state), name=f"remote_codex:{task_id}")

        remote_message = str(response_data.get("message") or "").strip()
        reply = f"远程 Codex 任务已创建：{task_id}"
        if remote_message:
            reply = f"{reply}\n{remote_message}"
        await self.ctx.send.text(reply, stream_id)
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
        )
        self._tasks[task_id] = task_state
        task_state.watch_task = asyncio.create_task(
            self._run_local_codex_task(task_state, task_dir, workspace_dir, prompt_path, final_message_path),
            name=f"local_codex:{task_id}",
        )
        created_message = f"本机 Codex 任务已创建：{task_id}"
        if input_files:
            names = "、".join(item.name for item in input_files[:3])
            if len(input_files) > 3:
                names = f"{names} 等 {len(input_files)} 个文件"
            created_message = f"{created_message}\n已导入参考文件：{names}"
        await self.ctx.send.text(created_message, stream_id)
        return True, f"本机任务已创建: {task_id}", True

    def _prepare_local_task_files(self, task_id: str, prompt: str) -> tuple[Path, Path, Path, Path]:
        """创建本地任务目录和 prompt 文件。"""

        work_root = Path(self.config.local_codex.work_root).expanduser()
        task_dir = work_root / task_id
        workspace_dir = task_dir / "workspace"
        task_dir.mkdir(parents=True, exist_ok=False)
        workspace_dir.mkdir(parents=True, exist_ok=False)
        prompt_path = task_dir / "prompt.md"
        final_message_path = task_dir / "final.md"
        prompt_path.write_text(self._build_local_codex_prompt(prompt, []), encoding="utf-8")
        return task_dir, workspace_dir, prompt_path, final_message_path

    def _build_local_codex_prompt(self, prompt: str, input_files: List[InputFile]) -> str:
        """构造发给本机 Codex CLI 的 prompt。"""

        input_text = ""
        if input_files:
            input_dir_name = self._safe_dir_name(self.config.input_file.input_dir_name or "input")
            lines = [
                f"用户上传的参考文件已经放在当前 workspace/{input_dir_name}/ 目录下。",
                f"请优先读取这些文件并按用户任务处理；不要把 {input_dir_name}/ 中的原始材料当作最终产物回传。",
                "输入文件：",
            ]
            for item in input_files:
                rel_path = Path(item.path).name
                size_text = f"，大小 {self._format_size(item.size)}" if item.size > 0 else ""
                lines.append(f"- {input_dir_name}/{rel_path}{size_text}")
            input_text = "\n".join(lines) + "\n\n"

        return (
            "你正在由 QQ 群中的 MaiBot 插件触发执行任务。\n"
            "请在当前 workspace 内完成用户请求；如需生成文件，请放在 workspace/artifacts/ 下。\n"
            "如果用户要求 Word/word/docx 文档，必须生成 .docx 文件，不能只生成 Markdown 或纯文本替代品。\n"
            "所有面向用户的进度更新和最终回答都必须使用简体中文，语气简洁，不要输出 Markdown 格式。\n"
            "最终回答请用简体中文，简要说明完成内容和产物路径。\n\n"
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
            return

        resolved_source = source_path.resolve()
        for root in roots:
            resolved_root = Path(root).expanduser().resolve()
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
    ) -> None:
        """运行本机 Codex CLI 并转发输出。"""

        stdout_log = task_dir / "stdout.jsonl"
        stderr_log = task_dir / "stderr.log"
        task_state.last_status = "running"

        command = self._build_local_codex_command(workspace_dir, final_message_path)
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
            final_data = {
                "status": task_state.last_status,
                "summary": self._read_local_final_message(
                    final_message_path,
                    stderr_log,
                    stdout_log,
                    process.returncode,
                ),
                "artifacts": self._collect_local_artifacts(workspace_dir),
            }
            await self._send_final_result(task_state, final_data)
        except asyncio.CancelledError:
            if task_state.process is not None and task_state.process.returncode is None:
                task_state.process.terminate()
            raise
        except Exception as exc:
            task_state.last_status = "failed"
            await self.ctx.send.text(f"本机 Codex 任务 {task_state.task_id} 执行失败：{exc}", task_state.stream_id)

    def _build_local_codex_command(self, workspace_dir: Path, final_message_path: Path) -> List[str]:
        """构造本机 Codex CLI 命令。"""

        local_config = self.config.local_codex
        command = [
            local_config.codex_binary.strip() or "codex",
            "-a",
            local_config.approval_policy.strip() or "never",
            "exec",
            "--json",
            "--color",
            "never",
            "-s",
            local_config.sandbox.strip() or "workspace-write",
            "-C",
            str(workspace_dir),
            "--skip-git-repo-check",
            "--output-last-message",
            str(final_message_path),
        ]
        if local_config.model.strip():
            command.extend(["-m", local_config.model.strip()])
        if local_config.enable_search:
            command.append("--search")
        command.extend(str(arg) for arg in local_config.extra_args if str(arg).strip())
        command.append("-")
        return command

    def _build_local_codex_env(self) -> Dict[str, str]:
        """构造本机 Codex 子进程环境变量。"""

        env = dict(os.environ)
        env_file_value = str(self.config.local_codex.env_file or "").strip()
        if not env_file_value:
            return env

        env_path = Path(env_file_value).expanduser()
        if not env_path.is_absolute():
            env_path = Path.cwd() / env_path
        if not env_path.exists():
            return env

        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if not key:
                continue
            env[key] = value.strip().strip('"').strip("'")
        return env

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
                progress_text = self._extract_progress_from_codex_line(line)
                if progress_text:
                    await self._send_local_progress(task_state, progress_text)

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
        message = _format_progress_message(task_state.task_id, [progress_text], max_chars)
        await self.ctx.send.text(message, task_state.stream_id)

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
        await self.ctx.send.text(message, task_state.stream_id)
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
            lines.append(_truncate_text(_plain_qq_text(summary), max(int(self.config.progress.max_summary_chars), 100)))
        if error:
            lines.append(f"错误：{_truncate_text(_plain_qq_text(error), 800)}")

        artifacts = _coerce_artifacts(data.get("artifacts") or data.get("files") or data.get("artifact"))
        if self.config.artifact.send_artifact_links and artifacts:
            lines.append("")
            lines.append("产物：")
            for artifact in artifacts:
                lines.append(self._format_artifact_line(artifact))

        await self.ctx.send.text("\n".join(lines).strip(), task_state.stream_id)

        if self.config.napcat.enabled and artifacts:
            await self._upload_artifacts_via_napcat(task_state, artifacts)

        if self.config.artifact.try_custom_file_message and artifacts:
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

    async def _upload_artifacts_via_napcat(self, task_state: RemoteTaskState, artifacts: List[Dict[str, Any]]) -> None:
        """通过 NapCat HTTP API 直传产物文件。"""

        failures: List[str] = []
        uncertain_failures: List[str] = []
        for artifact in artifacts:
            name = str(artifact.get("name") or artifact.get("filename") or "未命名产物").strip()
            try:
                await self._napcat_client.upload_artifact(task_state, artifact)
            except Exception as exc:
                self.ctx.logger.warning("NapCat 文件直传失败: %s", exc)
                failure_line = f"- {name}: {exc}"
                if self._is_uncertain_napcat_upload_failure(exc):
                    uncertain_failures.append(failure_line)
                else:
                    failures.append(failure_line)

        if failures:
            await self.ctx.send.text(
                "NapCat 文件直传失败：\n" + "\n".join(failures[:5]),
                task_state.stream_id,
            )
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
            await self.ctx.send.text(f"本地未跟踪任务：{normalized_task_id}", stream_id)
            return

        if task_state.process is not None and task_state.process.returncode is None:
            task_state.process.terminate()
            task_state.last_status = "cancelled"
            if task_state.watch_task is not None and not task_state.watch_task.done():
                task_state.watch_task.cancel()
            await self.ctx.send.text(f"任务 {normalized_task_id}: 已请求终止本机 Codex 进程", stream_id)
            return

        try:
            data = await self._client.cancel_task(normalized_task_id)
        except Exception as exc:
            await self.ctx.send.text(f"取消远程 Codex 任务失败：{exc}", stream_id)
            return

        task_state.last_status = self._normalize_status(data.get("status"), default="cancelled")
        if task_state.watch_task is not None and not task_state.watch_task.done():
            task_state.watch_task.cancel()
        message = str(data.get("message") or "已请求取消远程任务").strip()
        await self.ctx.send.text(f"任务 {normalized_task_id}: {message}", stream_id)

    def _get_execution_mode(self) -> str:
        """返回规范化执行模式。"""

        return str(self.config.task.execution_mode or "local").strip().lower()

    @staticmethod
    def _build_help_text(prefix: str) -> str:
        """构造帮助文本。"""

        escaped_prefix = prefix or "/codex"
        return (
            "远程 Codex Agent 命令：\n"
            f"{escaped_prefix} <任务描述> 创建远程 Codex 任务\n"
            f"{escaped_prefix} status 查看当前聊天流任务\n"
            f"{escaped_prefix} status <task_id> 查看指定任务\n"
            f"{escaped_prefix} cancel <task_id> 取消指定任务\n"
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
