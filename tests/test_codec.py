import copy
import json

import pytest
from analyse_message.codec import (
    SCHEMA,
    Compiler,
    InputError,
    Limits,
    json_text,
    parse_json,
    templates,
)


def compile_document(value, **kwargs):
    return Compiler(Limits(**kwargs), "123456", "测试用户").compile(value)


@pytest.mark.parametrize("name", list(templates()))
def test_every_template_compiles(name):
    spec = compile_document(templates()[name])
    assert spec.segments
    assert spec.kind == ("forward" if name in ("forward", "nested") else "message")


def test_json_keeps_whitespace_unicode_and_escaped_newlines():
    data = {"message": [{"type": "text", "data": {"text": "中  文\nline\t2"}}]}
    encoded = "\ufeff" + chr(96) * 3 + "json\n" + json_text(data) + "\n" + chr(96) * 3
    assert parse_json(encoded, Limits()) == data


@pytest.mark.parametrize(
    "value", ['{"a":1,"a":2}', '{"x":NaN}', "[Infinity]", "{", chr(96) * 3 + "json\n{}"]
)
def test_bad_json_has_actionable_error(value):
    with pytest.raises(InputError):
        parse_json(value, Limits())


def test_json_byte_limit_counts_utf8_bytes():
    with pytest.raises(InputError, match="字节"):
        parse_json('"中文"', Limits(max_json_bytes=6))


def test_media_uses_download_url_without_losing_extensions():
    data = {
        "message": [
            {
                "type": "image",
                "data": {
                    "file": "ABC.jpg",
                    "url": "https://host/original",
                    "summary": "[别的文字]",
                    "sub_type": 1,
                    "vendor_extension": {"a": 1},
                },
            }
        ]
    }
    original = copy.deepcopy(data)
    spec = compile_document(data)
    assert spec.segments[0]["data"] == dict(
        data["message"][0]["data"], file="https://host/original"
    )
    assert data == original


def test_image_summary_is_not_a_forward_option():
    spec = compile_document(
        {"kind": "image", "file": "base64://YWJj", "summary": "自定义"}
    )
    assert spec.options == {}
    assert spec.segments[0]["data"]["summary"] == "自定义"


def test_thumbnail_is_honest_link_card_with_distinct_urls():
    value = templates()["image_card"]
    spec = compile_document(value)
    card = json.loads(spec.segments[0]["data"]["data"])
    assert card["meta"]["news"]["preview"] == value["preview"]
    assert card["meta"]["news"]["jumpUrl"] == value["url"]
    assert card["meta"]["news"]["preview"] != card["meta"]["news"]["jumpUrl"]
    with pytest.raises(InputError, match="原生图片"):
        compile_document({"kind": "image", "file": "a", "thumb": "b"})


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "javascript:alert(1)", "https://", 1, "https://[broken"],
)
def test_card_requires_http_urls(url):
    with pytest.raises(InputError):
        compile_document(
            {"kind": "image_card", "preview": url, "url": "https://host/image"}
        )


def test_forward_accepts_get_forward_msg_records_and_sender():
    value = {
        "messages": [
            {
                "sender": {"user_id": 10001, "nickname": "张三"},
                "time": 1700000000,
                "content": [],
                "message": [{"type": "text", "data": {"text": "ok"}}],
            }
        ]
    }
    node = compile_document(value).segments[0]["data"]
    assert node["user_id"] == "10001"
    assert node["nickname"] == "张三"
    assert node["time"] == "1700000000"
    assert node["content"][0]["data"]["text"] == "ok"


def test_full_export_build_round_trip_and_reference_node():
    build = {"kind": "forward", "messages": [{"type": "node", "data": {"id": "123"}}]}
    spec = compile_document(
        {"schema": SCHEMA, "build": build, "raw": {"secret": "unused"}}
    )
    assert spec.kind == "forward"
    assert spec.segments == build["messages"]
    with pytest.raises(InputError, match="没有可重建"):
        compile_document({"schema": SCHEMA, "build": None})


def test_force_forward_wraps_and_defaults_sender():
    spec = Compiler(Limits(), "123456", "测试用户").compile(
        "中  文", force_forward=True
    )
    assert spec.kind == "forward"
    assert spec.segments == [
        {
            "type": "node",
            "data": {
                "user_id": "123456",
                "nickname": "测试用户",
                "content": [{"type": "text", "data": {"text": "中  文"}}],
            },
        }
    ]


def test_forward_limits_count_nested_nodes_globally():
    with pytest.raises(InputError, match="节点总数"):
        compile_document(templates()["nested"], max_nodes=1)
    with pytest.raises(InputError, match="嵌套"):
        compile_document(templates()["nested"], max_depth=1)


def test_does_not_mix_nodes_and_normal_segments():
    with pytest.raises(InputError, match="不能混合"):
        compile_document(
            [
                {"type": "node", "data": {"id": 1}},
                {"type": "text", "data": {"text": "oops"}},
            ]
        )


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"type": "text", "data": {"text": 3}},
        {"type": "image", "data": {}},
        {"type": "reply", "data": {"id": True}},
        {"type": "reply", "data": {"id": ""}},
        {"kind": "forward", "messages": []},
        {"kind": "oops"},
        {"type": "text", "data": "oops"},
        {"kind": "forward", "messages": [{"user_id": "x", "content": "x"}]},
        {
            "kind": "forward",
            "messages": [{"type": "node", "data": {"id": 1, "content": "x"}}],
        },
    ],
)
def test_rejects_invalid_shapes_before_send(document):
    with pytest.raises(InputError):
        compile_document(document)


def test_opaque_extensions_and_json_cards_survive():
    value = [
        {"type": "vendor_custom", "data": {"opaque": [1, 2]}},
        {"type": "json", "data": {"data": {"app": "example"}}},
    ]
    spec = compile_document(value)
    assert spec.segments[0] == value[0]
    assert json.loads(spec.segments[1]["data"]["data"]) == {"app": "example"}


@pytest.mark.parametrize("value", [True, "5", 0, 1001])
def test_invalid_config_fails_early(value):
    with pytest.raises(InputError):
        Limits.from_config({"max_nodes": value})
