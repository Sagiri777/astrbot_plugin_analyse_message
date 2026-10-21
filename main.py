"""AstrBot 4.29+ entry point. Preserve command JSON and raw OneBot fields."""

import asyncio
import base64
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.star import Context, Star

from .codec import (
    BuildSpec,
    Compiler,
    InputError,
    Limits,
    json_text,
    message_segments,
    parse_json,
    templates,
)
from .service import (
    Inspector,
    OneBot,
    ProtocolError,
    current_segments,
    quoted_id,
    quoted_text,
)

HELP = """消息元数据与构造
（指令前缀以 AstrBot 配置为准，以下用 / 举例）

/元数据：回复一条消息，导出其原始元数据及可编辑的 build。
/元数据 消息ID：查询指定消息。
/元数据 forward:资源ID：展开聊天记录。
不引用时，解析当前消息，可与转发卡片一起发送。

/构造消息 JSON：从消息段、get_msg 结果或完整元数据重建消息。
/构造记录 JSON：构造聊天记录；普通消息自动包装为一个节点。
JSON 可直接跟在指令后、放在 JSON 代码块中、作为 .json 附件，或通过回复 JSON 消息/文件提供。
构造指令默认仅 AstrBot 管理员可用；消息发到当前会话。

/消息模板 message|image|image_card|forward|nested
/消息帮助

image 的 summary 是消息摘要，不是图片内文字。
image_card 的 preview 是预览图，url 是点击跳转的大图地址；不是原生图片查看器的双图功能。
QQ 高级消息需要 OneBot v11（推荐 NapCat）。
别名：msgmeta / msgbuild / forwardbuild / msgtemplate / msghelp"""


def command_body(event: AstrMessageEvent) -> str:
    """Bypass command argument normalization: keep spaces and newlines in JSON."""
    parts = event.get_message_str().lstrip().split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


class AnalyseMessagePlugin(Star):
    """解析消息元数据，按 JSON 构造消息、图片及聊天记录。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.limits = Limits.from_config(config)

    async def text(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(MessageChain([Plain(text)]).use_t2i(False))

    async def document(self, event: AstrMessageEvent, value, filename: str) -> None:
        text = json_text(value)
        encoded = text.encode("utf-8")
        if len(encoded) > self.limits.max_json_bytes:
            raise InputError(
                "元数据超出 JSON 大小限制，请降低 max_nodes / max_depth，"
                "或提高 max_json_bytes 后重试。"
            )
        if len(text) <= self.limits.inline_chars:
            await self.text(event, text)
        elif event.get_platform_name() == "aiocqhttp":
            # Base64 travels over OneBot, so AstrBot/NapCat need no shared disk.
            file = "base64://" + base64.b64encode(encoded).decode("ascii")
            segment = {"type": "file", "data": {"name": filename, "file": file}}
            await OneBot(event, self.limits).send(BuildSpec("message", [segment], {}))
        else:
            with tempfile.NamedTemporaryFile(
                prefix="message-metadata-", suffix=".json", delete=False
            ) as handle:
                handle.write(encoded)
                path = handle.name
            event.track_temporary_local_file(path)
            await event.send(MessageChain([File(name=filename, file=path)]))

    @filter.command("消息帮助", alias={"msghelp"})
    async def help_command(self, event: AstrMessageEvent):
        """查看消息元数据、构造消息和聊天记录的用法。"""
        event.stop_event()
        await self.text(event, HELP)

    @filter.command("消息模板", alias={"msgtemplate"})
    async def template_command(self, event: AstrMessageEvent):
        """获取 message / image / image_card / forward / nested 的 JSON 模板。"""
        event.stop_event()
        name = command_body(event)
        aliases = {
            "消息": "message",
            "图片": "image",
            "卡片": "image_card",
            "记录": "forward",
            "嵌套": "nested",
        }
        name = aliases.get(name, name)
        values = templates()
        if name not in values:
            await self.text(
                event, "用法：/消息模板 message|image|image_card|forward|nested"
            )
            return
        try:
            await self.document(event, values[name], f"template-{name}.json")
        except (InputError, ProtocolError) as exc:
            await self.text(event, str(exc))

    @filter.command("元数据", alias={"msgmeta", "消息元数据"})
    async def metadata_command(self, event: AstrMessageEvent):
        """解析引用消息、消息 ID 或聊天记录，导出 JSON 元数据。"""
        event.stop_event()
        try:
            result = await Inspector(event, self.limits).inspect(command_body(event))
            await self.document(event, result, "message-metadata.json")
        except (InputError, ProtocolError) as exc:
            await self.text(event, str(exc))
        except Exception as exc:
            self.logger.error("元数据解析异常：%s", type(exc).__name__)
            await self.text(event, "元数据解析发生异常，请检查 AstrBot 日志。")

    @filter.command("构造消息", alias={"msgbuild"})
    async def build_message_command(self, event: AstrMessageEvent):
        """根据 JSON 或引用的元数据构造消息。默认仅管理员可用。"""
        await self.build(event, force_forward=False)

    @filter.command("构造记录", alias={"forwardbuild"})
    async def build_forward_command(self, event: AstrMessageEvent):
        """根据 JSON 构造聊天记录。默认仅管理员可用。"""
        await self.build(event, force_forward=True)

    async def build(self, event: AstrMessageEvent, force_forward: bool) -> None:
        event.stop_event()
        if self.config.get("build_admin_only", True) and not event.is_admin():
            await self.text(
                event, "构造功能仅 AstrBot 管理员可用；可在插件配置中调整。"
            )
            return
        try:
            api = OneBot(event, self.limits)
            document = await self.read_input(event, api)
            compiler = Compiler(
                self.limits, event.get_sender_id(), event.get_sender_name()
            )
            spec = compiler.compile(document, force_forward=force_forward)
            await api.send(spec)
        except (InputError, ProtocolError) as exc:
            await self.text(event, str(exc))
        except Exception as exc:
            self.logger.error("消息构造异常：%s", type(exc).__name__)
            await self.text(event, "消息构造发生异常，请检查 AstrBot 日志。")

    async def read_input(self, event: AstrMessageEvent, api: OneBot):
        body = command_body(event)
        if body:
            return parse_json(body, self.limits)
        files = [c for c in event.get_messages() if isinstance(c, File)]
        if files:
            if len(files) != 1:
                raise InputError("每次请只提供一个 JSON 文件。")
            return await self.read_component_file(files[0])
        reply = quoted_id(event)
        if reply:
            # Refetch raw text, avoiding AstrBot's whitespace-normalized outline.
            original = await api.message(reply)
            segments = message_segments(original)
            texts = [
                s["data"]["text"]
                for s in segments
                if s.get("type") == "text"
                and isinstance(s.get("data", {}).get("text"), str)
            ]
            if texts:
                return parse_json("".join(texts), self.limits)
            files = [s.get("data", {}) for s in segments if s.get("type") == "file"]
            if files:
                if len(files) != 1:
                    raise InputError("每次请只提供一个 JSON 文件。")
                return await self.read_raw_file(event, api, files[0])
            snapshot = quoted_text(event)
            if snapshot:
                return parse_json(snapshot, self.limits)
        # On some adapters a file is only exposed in the raw chain.
        files = [
            s.get("data", {})
            for s in current_segments(event)
            if s.get("type") == "file"
        ]
        if len(files) == 1:
            return await self.read_raw_file(event, api, files[0])
        raise InputError(
            "请在指令后提供 JSON，附加 .json 文件，或回复 JSON 文本/文件。"
        )

    async def read_component_file(self, component: File):
        name = component.name or ""
        if not name.lower().endswith(".json"):
            raise InputError("只接受 .json 文件。")
        location = await component.get_file(allow_return_url=True)
        if not location:
            raise InputError("无法获取附件地址，请检查协议端文件下载接口。")
        return await self.read_location(location)

    async def read_raw_file(self, event, api: OneBot, data: dict):
        name = data.get("name") or data.get("file_name") or data.get("file", "")
        if not isinstance(name, str) or not name.lower().endswith(".json"):
            raise InputError("只接受 .json 文件。")
        location = data.get("url")
        if not location and data.get("file_id"):
            params = {"file_id": data["file_id"]}
            group_id = event.get_group_id()
            action = "get_private_file_url"
            if group_id:
                params["group_id"] = int(group_id)
                action = "get_group_file_url"
            response = await api.call(action, **params)
            location = response.get("url")
        if not location:
            raise InputError("附件没有下载 URL 或 file_id，请直接粘贴 JSON。")
        return await self.read_location(location)

    async def read_location(self, location: str):
        if not isinstance(location, str) or not location:
            raise InputError("JSON 文件地址无效。")
        limit = self.limits.max_json_bytes
        if location.startswith(("http://", "https://")):
            timeout = aiohttp.ClientTimeout(total=self.limits.api_timeout)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(location) as response:
                        response.raise_for_status()
                        if response.content_length and response.content_length > limit:
                            raise InputError("JSON 文件超过大小限制。")
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(65_536):
                            data.extend(chunk)
                            if len(data) > limit:
                                raise InputError("JSON 文件超过大小限制。")
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise InputError(
                    "JSON 文件下载失败或超时，请检查文件地址是否可访问。"
                ) from exc
        else:
            path = Path(
                unquote(urlsplit(location).path)
                if location.startswith("file://")
                else location
            )

            def read():
                with path.open("rb") as handle:
                    return handle.read(limit + 1)

            try:
                data = await asyncio.to_thread(read)
            except OSError as exc:
                raise InputError("JSON 文件无法读取。") from exc
            if len(data) > limit:
                raise InputError("JSON 文件超过大小限制。")
        try:
            text = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InputError("JSON 文件必须使用 UTF-8 编码。") from exc
        return parse_json(text, self.limits)
