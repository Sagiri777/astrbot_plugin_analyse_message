"""Exercise handlers end to end with a fake AstrBot transport and real aiohttp."""

import base64
import importlib
import json
import logging
import sys
from enum import StrEnum
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from analyse_message.codec import InputError, Limits, json_text


class Kind(StrEnum):
    Plain = "Plain"
    Reply = "Reply"


class Plain:
    type = Kind.Plain

    def __init__(self, text):
        self.text = text


class File:
    def __init__(self, name, file="", url=""):
        self.name, self.file_, self.url = name, file, url

    async def get_file(self, allow_return_url=False):
        return self.url if allow_return_url and self.url else self.file_


class MessageChain:
    def __init__(self, chain):
        self.chain = chain

    def use_t2i(self, value):
        self.use_t2i_ = value
        return self


@pytest.fixture
def entrypoint(monkeypatch):
    names = [
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.star",
    ]
    modules = {name: ModuleType(name) for name in names}
    commands = {}

    def command(name, alias=None):
        def decorate(handler):
            commands[name] = (handler, alias)
            return handler

        return decorate

    class Star:
        def __init__(self, context):
            self.context = context
            self.logger = logging.getLogger("entrypoint-test")

    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.event"].filter = SimpleNamespace(command=command)
    modules["astrbot.api.message_components"].Plain = Plain
    modules["astrbot.api.message_components"].File = File
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules.pop("analyse_message.main", None)
    module = importlib.import_module("analyse_message.main")
    yield module, commands
    sys.modules.pop("analyse_message.main", None)


class Event:
    def __init__(self, text, components=None, raw=None, admin=True):
        self.text = text
        self.components = components or []
        self.message_obj = SimpleNamespace(raw_message=raw or {}, message_id="1")
        self.bot = SimpleNamespace(
            call_action=AsyncMock(return_value={"message_id": 10})
        )
        self.admin = admin
        self.sent = []
        self.stopped = False
        self.temporary = []

    def get_message_str(self):
        return self.text

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return "9988"

    def get_sender_id(self):
        return "1234"

    def get_sender_name(self):
        return "测试用户"

    def get_self_id(self):
        return "5678"

    def get_messages(self):
        return self.components

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True

    def track_temporary_local_file(self, path):
        self.temporary.append(path)

    async def send(self, chain):
        self.sent.append(chain)


async def test_registered_commands_and_literal_json_whitespace(entrypoint):
    module, commands = entrypoint
    assert set(commands) == {"消息帮助", "消息模板", "元数据", "构造消息", "构造记录"}
    plugin = module.AnalyseMessagePlugin(None, {})
    document = {"message": [{"type": "text", "data": {"text": "a  b\n中文"}}]}
    event = Event("构造消息 " + json_text(document))
    await plugin.build_message_command(event)
    assert event.stopped
    payload = event.bot.call_action.await_args.kwargs
    assert payload["message"][0]["data"]["text"] == "a  b\n中文"


async def test_permissions_are_checked_before_query_or_send(entrypoint):
    module, _ = entrypoint
    plugin = module.AnalyseMessagePlugin(None, {})
    event = Event("构造消息 {}", admin=False)
    await plugin.build_message_command(event)
    event.bot.call_action.assert_not_awaited()
    assert "管理员" in event.sent[0].chain[0].text


async def test_config_can_allow_members(entrypoint):
    module, _ = entrypoint
    plugin = module.AnalyseMessagePlugin(None, {"build_admin_only": False})
    event = Event('构造消息 "hello"', admin=False)
    await plugin.build_message_command(event)
    assert event.bot.call_action.await_count == 1


async def test_quoted_json_is_refetched_and_rebuilt(entrypoint):
    module, _ = entrypoint
    plugin = module.AnalyseMessagePlugin(None, {})
    event = Event("构造消息", raw={"message": [{"type": "reply", "data": {"id": 42}}]})
    document = {"kind": "image", "file": "https://host/img", "summary": "a  b"}
    event.bot.call_action.side_effect = [
        {"message": [{"type": "text", "data": {"text": json.dumps(document)}}]},
        {"message_id": 12},
    ]
    await plugin.build_message_command(event)
    assert event.bot.call_action.await_args_list[0].args[0] == "get_msg"
    assert (
        event.bot.call_action.await_args.kwargs["message"][0]["data"]["summary"]
        == "a  b"
    )


async def test_json_file_input_is_bounded_and_sends_forward(entrypoint, tmp_path):
    module, _ = entrypoint
    plugin = module.AnalyseMessagePlugin(None, {})
    path = tmp_path / "payload.json"
    path.write_text('{"message":"文件里的消息"}', encoding="utf-8")
    event = Event("构造记录", [File("payload.json", file=str(path))])
    await plugin.build_forward_command(event)
    call = event.bot.call_action.await_args
    assert call.args[0] == "send_group_forward_msg"
    assert (
        call.kwargs["messages"][0]["data"]["content"][0]["data"]["text"]
        == "文件里的消息"
    )


async def test_oversized_file_and_invalid_encoding_never_send(entrypoint, tmp_path):
    module, _ = entrypoint
    plugin = module.AnalyseMessagePlugin(None, {})
    plugin.limits = Limits(max_json_bytes=10)
    path = tmp_path / "payload.json"
    path.write_bytes(b"x" * 11)
    event = Event("构造消息", [File("payload.json", file=str(path))])
    await plugin.build_message_command(event)
    event.bot.call_action.assert_not_awaited()
    assert "大小限制" in event.sent[0].chain[0].text
    path.write_bytes(b"\xff")
    event.sent.clear()
    await plugin.build_message_command(event)
    assert "UTF-8" in event.sent[0].chain[0].text


async def test_large_metadata_sends_base64_json_file_without_shared_disk(entrypoint):
    module, _ = entrypoint
    plugin = module.AnalyseMessagePlugin(None, {})
    plugin.limits = Limits(inline_chars=1)
    event = Event("元数据")
    value = {"message": "中  文"}
    await plugin.document(event, value, "metadata.json")
    data = event.bot.call_action.await_args.kwargs["message"][0]["data"]
    assert data["name"] == "metadata.json"
    assert json.loads(base64.b64decode(data["file"].removeprefix("base64://"))) == value


async def test_metadata_inline_is_plain_json_with_text_to_image_disabled(entrypoint):
    module, _ = entrypoint
    plugin = module.AnalyseMessagePlugin(None, {})
    event = Event(
        "元数据", raw={"message": [{"type": "text", "data": {"text": "source"}}]}
    )
    await plugin.metadata_command(event)
    export = json.loads(event.sent[0].chain[0].text)
    assert export["build"]["message"][0]["data"]["text"] == "source"
    assert event.sent[0].use_t2i_ is False


async def test_invalid_json_reports_line_and_column(entrypoint):
    module, _ = entrypoint
    plugin = module.AnalyseMessagePlugin(None, {})
    event = Event("构造消息 {broken}")
    await plugin.build_message_command(event)
    event.bot.call_action.assert_not_awaited()
    assert "第 1 行" in event.sent[0].chain[0].text


async def test_http_file_download_enforces_content_length_and_stream_limit(entrypoint):
    module, _ = entrypoint
    plugin = module.AnalyseMessagePlugin(None, {})
    plugin.limits = Limits(max_json_bytes=64)

    async def valid(request):
        return web.Response(body=b'{"message":"ok"}')

    async def oversized(request):
        return web.Response(body=b"x" * 65)

    async def chunked(request):
        response = web.StreamResponse()
        response.enable_chunked_encoding()
        await response.prepare(request)
        await response.write(b"x" * 65)
        await response.write_eof()
        return response

    async def missing(request):
        raise web.HTTPNotFound()

    app = web.Application()
    app.router.add_get("/valid", valid)
    app.router.add_get("/oversized", oversized)
    app.router.add_get("/chunked", chunked)
    app.router.add_get("/missing", missing)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        assert await plugin.read_location(base + "/valid") == {"message": "ok"}
        for route in ("oversized", "chunked"):
            with pytest.raises(InputError, match="大小限制"):
                await plugin.read_location(base + "/" + route)
        with pytest.raises(InputError, match="下载失败"):
            await plugin.read_location(base + "/missing")
    finally:
        await runner.cleanup()


async def test_quoted_file_uses_correct_group_download_api(entrypoint, tmp_path):
    module, _ = entrypoint
    plugin = module.AnalyseMessagePlugin(None, {})
    path = tmp_path / "payload.json"
    path.write_text('{"message":"quoted file"}', encoding="utf-8")
    event = Event("构造消息", raw={"message": [{"type": "reply", "data": {"id": 42}}]})
    event.bot.call_action.side_effect = [
        {
            "message": [
                {"type": "file", "data": {"file": "payload.json", "file_id": "file123"}}
            ]
        },
        {"url": path.as_uri()},
        {"message_id": 99},
    ]
    await plugin.build_message_command(event)
    assert event.bot.call_action.await_args_list[1].args[0] == "get_group_file_url"
    assert event.bot.call_action.await_args_list[1].kwargs["group_id"] == 9988
    assert (
        event.bot.call_action.await_args.kwargs["message"][0]["data"]["text"]
        == "quoted file"
    )
