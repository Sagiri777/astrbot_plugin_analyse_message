"""Pure JSON/OneBot transformations; no AstrBot or network dependency."""

import copy
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

SCHEMA = "astrbot.message-metadata/v1"
FORWARD_OPTIONS = ("source", "summary", "prompt", "news")


class InputError(ValueError):
    """A user-correctable metadata error."""


@dataclass(frozen=True)
class Limits:
    max_json_bytes: int = 1_048_576
    max_nodes: int = 100
    max_depth: int = 5
    max_forward_fetches: int = 20
    api_timeout: int = 20
    inline_chars: int = 3000

    @classmethod
    def from_config(cls, config: dict) -> "Limits":
        bounds = {
            "max_json_bytes": (1024, 16_777_216),
            "max_nodes": (1, 1000),
            "max_depth": (1, 10),
            "max_forward_fetches": (1, 100),
            "api_timeout": (1, 120),
            "inline_chars": (256, 10_000),
        }
        defaults = cls()
        values = {}
        for key, (low, high) in bounds.items():
            value = config.get(key, getattr(defaults, key))
            if isinstance(value, bool) or not isinstance(value, int):
                raise InputError(f"配置 {key} 必须是整数。")
            if not low <= value <= high:
                raise InputError(f"配置 {key} 必须在 {low}–{high} 之间。")
            values[key] = value
        return cls(**values)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def parse_json(text: str, limits: Limits) -> Any:
    if len(text.encode("utf-8")) > limits.max_json_bytes:
        raise InputError(f"JSON 超过 {limits.max_json_bytes} 字节限制。")
    text = text.lstrip("\ufeff").strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        lines = text.splitlines()
        if len(lines) < 3 or lines[-1].strip() != fence:
            raise InputError("JSON 代码块未闭合。")
        if lines[0].strip().lower() not in (fence, fence + "json"):
            raise InputError("只接受 JSON 代码块。")
        text = "\n".join(lines[1:-1])

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InputError(f"JSON 存在重复字段：{key}。")
            result[key] = value
        return result

    def invalid_constant(value):
        raise InputError(f"JSON 不允许 {value}。")

    try:
        result = json.loads(
            text, object_pairs_hook=pairs, parse_constant=invalid_constant
        )
    except json.JSONDecodeError as exc:
        raise InputError(
            f"JSON 格式错误：第 {exc.lineno} 行、第 {exc.colno} 列，{exc.msg}。"
        ) from exc
    except (RecursionError, ValueError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("JSON 嵌套过深或数字无效。") from exc
    pending = [(result, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise InputError("JSON 结构嵌套不能超过 64 层。")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    return result


def message_segments(value: Any) -> list[dict]:
    """Accept raw message objects, chains, a single segment, or plain text."""
    if isinstance(value, str):
        if "[CQ:" in value:
            raise InputError("不解析 CQ 码字符串；请让协议端返回 message 数组。")
        return [{"type": "text", "data": {"text": value}}]
    if isinstance(value, dict):
        if "type" in value and "data" in value:
            return [value]
        for key in ("message", "content"):
            if key in value:
                return message_segments(value[key])
    if isinstance(value, list) and all(isinstance(v, dict) for v in value):
        return value
    raise InputError("消息必须是消息段数组、单个消息段或包含 message 的对象。")


def _http_url(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise InputError(f"{name} 必须是 HTTP(S) URL。")
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise InputError(f"{name} 必须是 HTTP(S) URL。")
    return value


def image_card(document: dict) -> dict:
    """A real QQ link card: preview image opens the specified original URL."""
    preview = _http_url(document.get("preview"), "preview")
    url = _http_url(document.get("url"), "url")
    title = document.get("title", "点击查看大图")
    description = document.get("description", "")
    prompt = document.get("prompt", f"[分享] {title}")
    if not all(isinstance(v, str) for v in (title, description, prompt)):
        raise InputError("title、description、prompt 必须是字符串。")
    card = {
        "app": "com.tencent.structmsg",
        "config": {"autosize": True, "forward": True, "type": "normal"},
        "desc": "新闻",
        "meta": {
            "news": {
                "title": title,
                "desc": description,
                "preview": preview,
                "jumpUrl": url,
                "tag": "图片",
            }
        },
        "prompt": prompt,
        "ver": "0.0.0.1",
        "view": "news",
    }
    return {"type": "json", "data": {"data": json.dumps(card, ensure_ascii=False)}}


@dataclass
class BuildSpec:
    kind: str
    segments: list[dict]
    options: dict


class Compiler:
    def __init__(self, limits: Limits, user_id: str, nickname: str):
        self.limits = limits
        self.user_id = str(user_id)
        self.nickname = nickname or "QQ用户"
        self.node_count = 0

    def compile(self, document: Any, force_forward: bool = False) -> BuildSpec:
        document = copy.deepcopy(document)
        if isinstance(document, dict) and document.get("schema") == SCHEMA:
            document = document.get("build")
            if document is None:
                raise InputError("这份元数据没有可重建的 OneBot 消息；请先编辑 build。")
        options = {}
        kind = document.get("kind") if isinstance(document, dict) else None
        if kind not in (None, "message", "forward", "image", "image_card"):
            raise InputError("kind 只能是 message、forward、image 或 image_card。")
        if kind == "image":
            data = {k: v for k, v in document.items() if k != "kind"}
            if "thumb" in data:
                raise InputError(
                    "原生图片不保证支持独立 thumb；请使用 image_card 的 preview/url。"
                )
            segments = self.chain([{"type": "image", "data": data}], 0)
        elif kind == "image_card":
            segments = [image_card(document)]
        elif kind == "forward" or (
            isinstance(document, dict)
            and "messages" in document
            and "message" not in document
        ):
            nodes = document.get("messages")
            if not isinstance(nodes, list) or not nodes:
                raise InputError("messages 必须是非空聊天记录节点数组。")
            segments = [self.node(node, 0) for node in nodes]
        else:
            segments = self.chain(message_segments(document), 0)
        if isinstance(document, dict) and kind not in ("image", "image_card"):
            options = self.options(document)
        all_nodes = all(seg["type"] == "node" for seg in segments)
        if force_forward and not all_nodes:
            segments = [self.node({"content": segments}, 0)]
            all_nodes = True
        if options and not all_nodes:
            raise InputError("source、summary、prompt、news 仅适用于聊天记录。")
        return BuildSpec("forward" if all_nodes else "message", segments, options)

    @staticmethod
    def options(document: dict) -> dict:
        result = {k: document[k] for k in FORWARD_OPTIONS if k in document}
        for key in ("source", "summary", "prompt"):
            if key in result and not isinstance(result[key], str):
                raise InputError(f"{key} 必须是字符串。")
        if "news" in result and (
            not isinstance(result["news"], list)
            or not all(
                isinstance(v, dict) and isinstance(v.get("text"), str)
                for v in result["news"]
            )
        ):
            raise InputError('news 必须是 [{"text":"预览文本"}] 数组。')
        return result

    def node(self, value: Any, depth: int) -> dict:
        if depth >= self.limits.max_depth:
            raise InputError(f"聊天记录嵌套超过 {self.limits.max_depth} 层。")
        self.node_count += 1
        if self.node_count > self.limits.max_nodes:
            raise InputError(f"聊天记录节点总数超过 {self.limits.max_nodes}。")
        if not isinstance(value, dict):
            raise InputError("聊天记录的每个节点必须是对象。")
        raw = value.get("type") == "node"
        data = value.get("data") if raw else value
        if not isinstance(data, dict):
            raise InputError("node.data 必须是对象。")
        if raw and data.get("id") is not None:
            if any(k in data for k in ("content", "message", "messages")):
                raise InputError("引用节点 id 与自定义 content 不能同时提供。")
            if (
                isinstance(data["id"], bool)
                or not isinstance(data["id"], str | int)
                or str(data["id"]) == ""
            ):
                raise InputError("节点 id 必须是字符串或整数。")
            return {"type": "node", "data": copy.deepcopy(data)}
        sender = data.get("sender", {})
        if not isinstance(sender, dict):
            raise InputError("sender 必须是对象。")
        user_id = data.get(
            "user_id", data.get("uin", sender.get("user_id", self.user_id))
        )
        nickname = data.get(
            "nickname", data.get("name", sender.get("nickname", self.nickname))
        )
        if isinstance(user_id, bool) or not isinstance(user_id, str | int):
            raise InputError("节点 user_id / uin 必须是 QQ 号字符串或整数。")
        if not str(user_id).isascii() or not str(user_id).isdigit():
            raise InputError("节点 user_id / uin 必须是数字 QQ 号。")
        if not isinstance(nickname, str) or not nickname:
            raise InputError("节点 nickname / name 必须是非空字符串。")
        # Some protocol backends return content=[] together with message=[...].
        content = data.get("content") or data.get("message")
        if content is None:
            raise InputError("自定义节点需要 content 或 message。")
        result = copy.deepcopy(data) if raw else {}
        result.pop("message", None)
        result.pop("uin", None)
        result.pop("name", None)
        result.update(
            user_id=str(user_id),
            nickname=nickname,
            content=self.chain(message_segments(content), depth + 1),
        )
        if "time" in data:
            if isinstance(data["time"], bool) or not isinstance(
                data["time"], str | int
            ):
                raise InputError("节点 time 必须是时间戳字符串或整数。")
            result["time"] = str(data["time"])
        result.update(self.options(data))
        return {"type": "node", "data": result}

    def chain(self, segments: list[dict], depth: int) -> list[dict]:
        if not segments:
            raise InputError("消息段数组不能为空。")
        result = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise InputError("每个消息段都必须是对象。")
            kind, data = segment.get("type"), segment.get("data")
            if not isinstance(kind, str) or not kind:
                raise InputError("每个消息段都需要字符串 type。")
            if not isinstance(data, dict):
                raise InputError(f"{kind}.data 必须是对象。")
            if kind == "node":
                result.append(self.node(segment, depth))
                continue
            segment = copy.deepcopy(segment)
            data = segment["data"]
            if kind == "text" and not isinstance(data.get("text"), str):
                raise InputError("text.data.text 必须是字符串。")
            if kind in ("image", "record", "video", "file"):
                # Received image.file is often a cache name; url is reusable.
                if data.get("url"):
                    data["file"] = data["url"]
                if not isinstance(data.get("file"), str) or not data["file"]:
                    raise InputError(f"{kind} 需要非空的 file 或 url。")
                if "summary" in data and not isinstance(data["summary"], str):
                    raise InputError("图片 summary 必须是字符串。")
            if kind in ("json", "xml"):
                payload = data.get("data")
                if kind == "json" and isinstance(payload, dict):
                    data["data"] = json.dumps(payload, ensure_ascii=False)
                elif not isinstance(payload, str) or not payload:
                    raise InputError(f"{kind}.data.data 必须是非空字符串或 JSON 对象。")
            if kind in ("reply", "forward") and (
                isinstance(data.get("id"), bool)
                or not isinstance(data.get("id"), str | int)
                or str(data["id"]) == ""
            ):
                raise InputError(f"{kind}.data.id 必须是字符串或整数。")
            result.append(segment)
        if any(s["type"] == "node" for s in result) and not all(
            s["type"] == "node" for s in result
        ):
            raise InputError("同一层不能混合 node 与普通消息段；请用节点包装普通消息。")
        return result


def templates() -> dict[str, dict]:
    return {
        "message": {
            "kind": "message",
            "message": [{"type": "text", "data": {"text": "你好，世界"}}],
        },
        "image": {
            "kind": "image",
            "file": "https://example.com/image.jpg",
            "summary": "[图片] 这里是自定义摘要，不是图片内的像素文字",
            "sub_type": 0,
        },
        "image_card": {
            "kind": "image_card",
            "preview": "https://example.com/preview.jpg",
            "url": "https://example.com/original.jpg",
            "title": "点击打开另一张大图",
            "description": "聊天中显示 preview，点击卡片跳转 url",
        },
        "forward": {
            "kind": "forward",
            "source": "聊天记录",
            "summary": "查看 2 条消息",
            "prompt": "[聊天记录] 示例",
            "news": [{"text": "张三：第一条消息"}],
            "messages": [
                {"user_id": "10001", "nickname": "张三", "content": "第一条消息"},
                {"user_id": "10002", "nickname": "李四", "content": "第二条消息"},
            ],
        },
        "nested": {
            "kind": "forward",
            "messages": [
                {
                    "user_id": "10001",
                    "nickname": "张三",
                    "content": [
                        {
                            "type": "node",
                            "data": {
                                "user_id": "10002",
                                "nickname": "李四",
                                "content": [{"type": "text", "data": {"text": "内层"}}],
                            },
                        }
                    ],
                }
            ],
        },
    }
