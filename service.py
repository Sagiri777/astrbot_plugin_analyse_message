"""OneBot routing, lossless metadata inspection, and forward expansion."""

import asyncio
import copy
import dataclasses
import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any

from .codec import SCHEMA, BuildSpec, InputError, Limits, message_segments


class ProtocolError(RuntimeError):
    """The connected protocol implementation rejected or timed out a request."""


def serializable(value: Any, depth: int = 0) -> Any:
    """Serialize components without downloading media or invoking File.file."""
    if depth > 64:
        return "<maximum serialization depth>"
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(k): serializable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [serializable(v, depth + 1) for v in value]
    if dataclasses.is_dataclass(value):
        return {
            field.name: serializable(getattr(value, field.name), depth + 1)
            for field in dataclasses.fields(value)
        }
    if hasattr(value, "model_dump"):
        # mode=python avoids custom JSON serializers with side effects.
        return serializable(value.model_dump(mode="python"), depth + 1)
    # AstrBot uses pydantic.v1 components on Python < 3.14.
    if hasattr(value, "__fields__") and callable(getattr(value, "dict", None)):
        return serializable(value.dict(), depth + 1)
    if hasattr(value, "__dict__"):
        return {
            k: serializable(v, depth + 1)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    return str(value)


def component_segments(components: list) -> list[dict]:
    result = []
    for component in components:
        data = serializable(component)
        if not isinstance(data, dict):
            continue
        kind = str(data.pop("type", "unknown")).lower()
        kind = {"plain": "text", "atall": "at"}.get(kind, kind)
        if kind == "text":
            data = {"text": data.get("text", "")}
        elif kind == "reply":
            data = {"id": str(data.get("id", ""))}
        elif kind == "node":
            data = {
                "user_id": data.get("uin", "0"),
                "nickname": data.get("name", "QQ用户"),
                "content": component_segments(getattr(component, "content", [])),
            }
        result.append({"type": kind, "data": data})
    return result


def raw_event(event) -> dict:
    raw = serializable(getattr(event.message_obj, "raw_message", None))
    return raw if isinstance(raw, dict) else {}


def current_segments(event) -> list[dict]:
    raw = raw_event(event)
    if isinstance(raw.get("message"), list):
        return raw["message"]
    return component_segments(event.get_messages())


def quoted_id(event) -> str | None:
    for segment in current_segments(event):
        if segment.get("type") == "reply" and isinstance(segment.get("data"), dict):
            value = segment["data"].get("id")
            if value is not None:
                return str(value)
    return None


def quoted_text(event) -> str:
    for component in event.get_messages():
        kind = getattr(component, "type", "")
        if str(getattr(kind, "value", kind)).lower() == "reply":
            chain = getattr(component, "chain", None) or []
            texts = [
                getattr(c, "text", "")
                for c in chain
                if str(
                    getattr(getattr(c, "type", ""), "value", getattr(c, "type", ""))
                ).lower()
                in ("plain", "text")
            ]
            if texts:
                return "".join(texts)
            return getattr(component, "message_str", "") or ""
    return ""


class OneBot:
    def __init__(self, event, limits: Limits):
        if event.get_platform_name() != "aiocqhttp" or not hasattr(event, "bot"):
            raise InputError(
                "消息构造和原始协议查询需要 QQ OneBot v11（aiocqhttp）适配器。"
            )
        self.event = event
        self.bot = event.bot
        self.timeout = limits.api_timeout

    async def call(self, action: str, **params) -> dict:
        self_id = raw_event(self.event).get("self_id") or self.event.get_self_id()
        if self_id:
            params["self_id"] = self_id
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.bot.call_action(action, **params)
        except TimeoutError as exc:
            hint = (
                "发送操作可能已生效，请先检查聊天。"
                if action.startswith("send_")
                else "请检查协议端连接或稍后重试。"
            )
            raise ProtocolError(f"{action} 超时（{self.timeout} 秒）；{hint}") from exc
        except Exception as exc:
            # aiocqhttp's exception repr can contain private URLs or payloads.
            detail = (
                f"retcode={exc.retcode}"
                if hasattr(exc, "retcode")
                else type(exc).__name__
            )
            raise ProtocolError(
                f"协议端 {action} 失败（{detail}），请检查协议端日志与接口支持情况。"
            ) from exc
        if not isinstance(response, Mapping):
            raise ProtocolError(f"{action} 返回了非对象结果。")
        if "status" in response and "retcode" in response:
            if response["status"] != "ok" or response["retcode"] not in (0, "0"):
                raise ProtocolError(f"{action} 失败，retcode={response['retcode']}。")
            response = response.get("data")
            if not isinstance(response, Mapping):
                raise ProtocolError(f"{action} 返回了无效 data。")
        return dict(response)

    async def message(self, message_id: str) -> dict:
        if not re.fullmatch(r"-?[0-9]+", str(message_id)):
            raise InputError("消息 ID 必须是整数；转发资源请使用 forward:资源ID。")
        result = await self.call("get_msg", message_id=int(message_id))
        if "message" not in result:
            raise ProtocolError("get_msg 没有返回 message。")
        return result

    async def forward(self, resource_id: str) -> dict:
        result = await self.call("get_forward_msg", id=resource_id)
        if not isinstance(result.get("messages"), list):
            raise ProtocolError("get_forward_msg 没有返回 messages 数组。")
        return result

    async def send(self, spec: BuildSpec) -> dict:
        group_id = self.event.get_group_id()
        target = str(group_id or self.event.get_sender_id())
        if not target.isascii() or not target.isdigit():
            raise InputError("当前会话缺少有效群号或用户 QQ 号。")
        params = {"group_id" if group_id else "user_id": int(target)}
        if spec.kind == "forward":
            action = (
                "send_group_forward_msg" if group_id else "send_private_forward_msg"
            )
            params.update(messages=spec.segments, **spec.options)
        else:
            action = "send_group_msg" if group_id else "send_private_msg"
            params["message"] = spec.segments
        return await self.call(action, **params)


def forward_id(segment: dict) -> str | None:
    data = segment.get("data")
    if not isinstance(data, dict):
        return None
    if segment.get("type") == "forward" and data.get("id"):
        return str(data["id"])
    if segment.get("type") == "json":
        card = data.get("data")
        if isinstance(card, str):
            try:
                card = json.loads(card)
            except (ValueError, RecursionError):
                return None
        if isinstance(card, dict) and card.get("app") == "com.tencent.multimsg":
            meta = card.get("meta")
            detail = meta.get("detail") if isinstance(meta, dict) else None
            if isinstance(detail, dict) and detail.get("resid"):
                return str(detail["resid"])
    return None


class Inspector:
    def __init__(self, event, limits: Limits):
        self.event = event
        self.limits = limits
        self.api = (
            OneBot(event, limits) if event.get_platform_name() == "aiocqhttp" else None
        )
        self.warnings: list[str] = []
        self.forwards: dict[str, dict] = {}
        self.fetches = 0
        self.expanded_nodes = 0

    async def inspect(self, query: str = "") -> dict:
        if query.startswith("forward:"):
            if not self.api:
                raise InputError("转发资源查询只支持 OneBot v11。")
            resource_id = query.removeprefix("forward:").strip()
            if not resource_id:
                raise InputError("请提供 forward:资源ID。")
            source = {"type": "forward_resource", "id": resource_id}
            original = {"message": [{"type": "forward", "data": {"id": resource_id}}]}
        else:
            message_id = query or quoted_id(self.event)
            source = {"type": "current"}
            original = raw_event(self.event)
            if message_id:
                source = {
                    "type": "quoted" if not query else "message_id",
                    "id": message_id,
                }
                if self.api:
                    original = await self.api.message(message_id)
                else:
                    original = self.quote_snapshot(message_id)
                    self.warnings.append(
                        "当前平台仅导出 AstrBot 引用快照，无法查询 OneBot 原始字段。"
                    )
            elif not original:
                original = {
                    "message_id": getattr(self.event.message_obj, "message_id", None),
                    "message": component_segments(self.event.get_messages()),
                }
                self.warnings.append(
                    "当前平台未提供原始对象，导出内容来自 AstrBot 组件快照。"
                )
        build = None
        if self.api:
            try:
                segments = copy.deepcopy(message_segments(original))
                segments = await self.expand_chain(segments, 0, frozenset())
                if segments and all(s.get("type") == "node" for s in segments):
                    build = {"kind": "forward", "messages": segments}
                else:
                    build = {"kind": "message", "message": segments}
            except InputError as exc:
                self.warnings.append(f"原始内容已保留，但无法生成 build：{exc}")
        return {
            "schema": SCHEMA,
            "platform": self.event.get_platform_name(),
            "source": source,
            "raw": serializable(original),
            "trigger_components": serializable(self.event.get_messages()),
            "forwards": self.forwards,
            "build": build,
            "warnings": self.warnings,
        }

    def quote_snapshot(self, message_id: str) -> dict:
        for component in self.event.get_messages():
            kind = getattr(component, "type", "")
            if (
                str(getattr(kind, "value", kind)).lower() == "reply"
                and str(getattr(component, "id", "")) == message_id
            ):
                return serializable(component)
        raise InputError("当前平台未提供这条引用消息的内容。")

    async def expand_chain(
        self, segments: list[dict], depth: int, path: frozenset
    ) -> list[dict]:
        output = []
        for segment in segments:
            resource = forward_id(segment)
            if resource and self.api:
                if depth >= self.limits.max_depth or resource in path:
                    self.warnings.append(
                        f"转发 {resource} 达到嵌套限制或检测到循环，保留引用。"
                    )
                else:
                    if resource not in self.forwards:
                        if self.fetches >= self.limits.max_forward_fetches:
                            self.warnings.append(
                                "达到转发资源获取次数上限，保留其余引用。"
                            )
                            output.append(segment)
                            continue
                        self.fetches += 1
                        try:
                            raw = await self.api.forward(resource)
                            self.forwards[resource] = {"raw": raw}
                        except ProtocolError as exc:
                            self.forwards[resource] = {"error": str(exc)}
                            self.warnings.append(f"转发 {resource} 未展开：{exc}")
                    raw = self.forwards.get(resource, {}).get("raw")
                    if raw and raw.get("messages"):
                        try:
                            nodes = await self.expand_nodes(
                                raw["messages"], depth, path | {resource}
                            )
                        except InputError as exc:
                            self.warnings.append(f"转发 {resource} 未展开：{exc}")
                        else:
                            if len(segments) == 1:
                                output.extend(nodes)
                            else:
                                # node and ordinary segments cannot share one chain.
                                self.warnings.append(
                                    "混合消息中的转发保留资源引用；完整节点见 forwards。"
                                )
                                output.append(segment)
                            continue
            if segment.get("type") == "node":
                nodes = await self.expand_nodes([segment], depth, path)
                output.extend(nodes)
            else:
                output.append(segment)
        return output

    async def expand_nodes(
        self, values: list, depth: int, path: frozenset
    ) -> list[dict]:
        if depth >= self.limits.max_depth:
            raise InputError("达到聊天记录嵌套上限。")
        nodes = []
        for value in values:
            self.expanded_nodes += 1
            if self.expanded_nodes > self.limits.max_nodes:
                raise InputError("达到聊天记录节点数量上限。")
            if not isinstance(value, dict):
                raise InputError("协议端返回了非对象转发节点。")
            data = value.get("data", {}) if value.get("type") == "node" else value
            if not isinstance(data, dict):
                raise InputError("协议端返回了无效 node.data。")
            content = data.get("content") or data.get("message")
            if content is None and data.get("id"):
                nodes.append(copy.deepcopy(value))
                continue
            sender = data.get("sender") or {}
            if not isinstance(sender, dict):
                raise InputError("协议端返回了无效 sender。")
            node = copy.deepcopy(data) if value.get("type") == "node" else {}
            for key in ("message", "uin", "name"):
                node.pop(key, None)
            node.update(
                {
                    "user_id": str(
                        data.get("user_id", data.get("uin", sender.get("user_id", "0")))
                    ),
                    "nickname": data.get(
                        "nickname", data.get("name", sender.get("nickname", "QQ用户"))
                    ),
                    "content": await self.expand_chain(
                        message_segments(content), depth + 1, path
                    ),
                }
            )
            if "time" in data:
                node["time"] = data["time"]
            nodes.append({"type": "node", "data": node})
        return nodes
