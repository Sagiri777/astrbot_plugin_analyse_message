import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from analyse_message.codec import SCHEMA, BuildSpec, Compiler, InputError, Limits
from analyse_message.service import (
    Inspector,
    OneBot,
    ProtocolError,
    forward_id,
    serializable,
)


class FakeEvent:
    def __init__(self, raw=None, platform="aiocqhttp", group="9988"):
        self.message_obj = SimpleNamespace(raw_message=raw or {}, message_id="1")
        self.bot = SimpleNamespace(
            call_action=AsyncMock(return_value={"message_id": 10})
        )
        self.platform = platform
        self.group = group

    def get_platform_name(self):
        return self.platform

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return "1234"

    def get_self_id(self):
        return "5678"

    def get_messages(self):
        return []


async def test_group_raw_send_preserves_image_fields_and_uses_current_target():
    event = FakeEvent({"self_id": 777})
    spec = BuildSpec(
        "message",
        [
            {
                "type": "image",
                "data": {"file": "https://img", "summary": "hello", "sub_type": 1},
            }
        ],
        {},
    )
    await OneBot(event, Limits()).send(spec)
    event.bot.call_action.assert_awaited_once_with(
        "send_group_msg", group_id=9988, message=spec.segments, self_id=777
    )


async def test_private_forward_send_does_not_trust_export_destination():
    event = FakeEvent(group="")
    spec = BuildSpec(
        "forward",
        [{"type": "node", "data": {"id": "1"}}],
        {"summary": "记录", "source": "测试"},
    )
    await OneBot(event, Limits()).send(spec)
    event.bot.call_action.assert_awaited_once_with(
        "send_private_forward_msg",
        user_id=1234,
        messages=spec.segments,
        summary="记录",
        source="测试",
        self_id="5678",
    )


async def test_quoted_metadata_keeps_raw_and_provides_rebuild():
    event = FakeEvent({"message": [{"type": "reply", "data": {"id": "42"}}]})
    original = {
        "message_id": 42,
        "sender": {"user_id": 123},
        "message": [
            {
                "type": "image",
                "data": {
                    "file": "ABC.jpg",
                    "url": "https://host/img",
                    "summary": "[图]",
                    "unknown": 3,
                },
            }
        ],
    }
    event.bot.call_action.return_value = original
    export = await Inspector(event, Limits()).inspect()
    assert export["schema"] == SCHEMA
    assert export["raw"] == original
    assert export["source"] == {"type": "quoted", "id": "42"}
    spec = Compiler(Limits(), "1234", "me").compile(export)
    assert spec.segments[0]["data"]["unknown"] == 3
    assert spec.segments[0]["data"]["file"] == "https://host/img"
    event.bot.call_action.assert_awaited_once_with(
        "get_msg", message_id=42, self_id="5678"
    )


async def test_nested_forward_expansion_round_trip():
    event = FakeEvent()

    async def respond(action, **kwargs):
        assert action == "get_forward_msg"
        if kwargs["id"] == "outer":
            return {
                "messages": [
                    {
                        "sender": {"user_id": 10, "nickname": "A"},
                        "message": [{"type": "forward", "data": {"id": "inner"}}],
                    }
                ]
            }
        return {
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "user_id": "11",
                        "nickname": "B",
                        "content": [],
                        "message": [{"type": "text", "data": {"text": "nested"}}],
                    },
                }
            ]
        }

    event.bot.call_action.side_effect = respond
    export = await Inspector(event, Limits()).inspect("forward:outer")
    assert set(export["forwards"]) == {"outer", "inner"}
    spec = Compiler(Limits(), "1234", "me").compile(export)
    inner = spec.segments[0]["data"]["content"][0]["data"]
    assert inner["nickname"] == "B"
    assert inner["content"][0]["data"]["text"] == "nested"


async def test_forward_cycle_terminates_without_losing_reference():
    event = FakeEvent()
    event.bot.call_action.return_value = {
        "messages": [
            {
                "sender": {"user_id": 10},
                "message": [{"type": "forward", "data": {"id": "loop"}}],
            }
        ]
    }
    export = await Inspector(event, Limits()).inspect("forward:loop")
    assert export["warnings"]
    assert event.bot.call_action.await_count == 1
    assert export["build"]["messages"][0]["data"]["content"][0]["data"]["id"] == "loop"


async def test_forward_failure_keeps_raw_and_warns():
    event = FakeEvent()
    event.bot.call_action.side_effect = RuntimeError("https://secret/private")
    export = await Inspector(event, Limits()).inspect("forward:gone")
    assert export["build"]["message"][0]["data"]["id"] == "gone"
    assert "secret" not in str(export["warnings"])
    assert export["forwards"]["gone"]["error"]


async def test_expansion_respects_fetch_budget():
    event = FakeEvent(
        {
            "message": [
                {"type": "forward", "data": {"id": "a"}},
                {"type": "forward", "data": {"id": "b"}},
            ]
        }
    )
    event.bot.call_action.return_value = {
        "messages": [{"sender": {"user_id": 10}, "message": "text"}]
    }
    export = await Inspector(event, Limits(max_forward_fetches=1)).inspect()
    assert event.bot.call_action.await_count == 1
    assert export["warnings"]


async def test_non_onebot_metadata_is_available_but_build_is_not():
    event = FakeEvent({"message": "raw", "extra": {"a": 1}}, platform="telegram")
    export = await Inspector(event, Limits()).inspect()
    assert export["raw"]["extra"] == {"a": 1}
    assert export["build"] is None
    with pytest.raises(InputError, match="OneBot"):
        OneBot(event, Limits())


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"status": "failed", "retcode": 100},
        {"status": "ok", "retcode": 0, "data": None},
    ],
)
async def test_rejects_invalid_api_responses(response):
    event = FakeEvent()
    event.bot.call_action.return_value = response
    with pytest.raises(ProtocolError):
        await OneBot(event, Limits()).call("get_msg")


async def test_unwraps_full_response_envelope():
    event = FakeEvent()
    event.bot.call_action.return_value = {
        "status": "ok",
        "retcode": 0,
        "data": {"message": []},
    }
    assert await OneBot(event, Limits()).message("-42") == {"message": []}


def test_json_forward_cards_support_resid_and_bad_json():
    assert (
        forward_id(
            {
                "type": "json",
                "data": {
                    "data": '{"app":"com.tencent.multimsg","meta":{"detail":{"resid":"abc"}}}'
                },
            }
        )
        == "abc"
    )
    assert forward_id({"type": "json", "data": {"data": "{"}}) is None


def test_snapshot_never_accesses_file_property():
    class File:
        @property
        def file(self):
            raise AssertionError("Would download a file")

        def model_dump(self, mode):
            return {"file_": "", "url": "https://host/file"}

    assert serializable(File()) == {"file_": "", "url": "https://host/file"}


@pytest.mark.parametrize("action", ["send_group_msg", "get_msg"])
async def test_api_timeout_does_not_retry_sends(action):
    event = FakeEvent()

    async def delayed(*args, **kwargs):
        await asyncio.sleep(1)

    event.bot.call_action.side_effect = delayed
    api = OneBot(event, Limits())
    api.timeout = 0.005
    with pytest.raises(ProtocolError) as exc:
        await api.call(action)
    assert "超时" in str(exc.value)
    assert ("可能已生效" in str(exc.value)) == action.startswith("send_")
    assert event.bot.call_action.await_count == 1


async def test_raw_node_vendor_fields_survive_expansion():
    event = FakeEvent()
    event.bot.call_action.return_value = {
        "messages": [
            {
                "type": "node",
                "data": {
                    "uin": "10",
                    "name": "A",
                    "source": "nested-source",
                    "vendor_extension": 123,
                    "content": [{"type": "text", "data": {"text": "hello"}}],
                },
            }
        ]
    }
    export = await Inspector(event, Limits()).inspect("forward:resource")
    node = export["build"]["messages"][0]["data"]
    assert node["vendor_extension"] == 123
    assert node["source"] == "nested-source"
    assert "uin" not in node and "name" not in node
