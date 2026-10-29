from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .prompts import COMMON, PROMPTS

Search = Callable[[str], Awaitable[dict[str, Any]]]


def _content_tool_request(content: Any) -> dict[str, str] | None:
    """Accept one explicitly shaped compatibility envelope, never arbitrary JSON."""
    if not isinstance(content, str):
        return None
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or "tool_name" not in value:
        return None
    if set(value) - {"thoughts", "tool_name", "parameters"}:
        raise ValueError("MODEL_TOOL_ENVELOPE_INVALID")
    if value.get("tool_name") != "semantic_search_rules":
        raise ValueError("MODEL_TOOL_UNAUTHORIZED")
    parameters = value.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != {"query"}:
        raise ValueError("MODEL_TOOL_ARGUMENTS_INVALID")
    query = parameters["query"]
    if not isinstance(query, str) or not 1 <= len(query) <= 250:
        raise ValueError("MODEL_TOOL_ARGUMENTS_INVALID")
    return {"query": query}


class OpenAIModel:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        model: str,
        *,
        concurrency: int = 4,
        max_rounds: int = 17,
        retries: int = 3,
    ):
        if concurrency < 1 or max_rounds < 1 or retries < 0:
            raise ValueError("INVALID_MODEL_LIMITS")
        self.http, self.base_url, self.api_key, self.model = (
            http,
            base_url.rstrip("/"),
            api_key,
            model,
        )
        self.semaphore = asyncio.Semaphore(concurrency)
        # 限流冷却：某个请求被 429 后，全体等这个时刻再发，避免并发重试又一起撞上限。
        self._cooldown_until = 0.0
        self.max_rounds, self.retries = max_rounds, retries

    async def _request(self, body: dict) -> dict:
        if not self.base_url or not self.model:
            raise RuntimeError("MODEL_NOT_CONFIGURED")
        for attempt in range(self.retries + 1):
            retry_after = 0.0
            throttled = False
            # 别的请求刚被限流过就一起等，别让它自己先撞上去。
            wait = self._cooldown_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self.semaphore:
                    response = await self.http.post(
                        self.base_url + "/chat/completions",
                        json=body,
                        headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                    )
            except httpx.RequestError as exc:
                if attempt == self.retries:
                    raise RuntimeError("MODEL_TRANSPORT_ERROR") from exc
            else:
                if response.is_success:
                    return response.json()
                if response.status_code not in {429, 500, 502, 503, 504} or attempt == self.retries:
                    raise RuntimeError(f"MODEL_HTTP_{response.status_code}")
                throttled = response.status_code == 429
                # 服务端给了 Retry-After 就按它等，别用固定退避硬撞限流。
                header = response.headers.get("retry-after", "").strip()
                if header.isdigit():
                    retry_after = min(float(header), 30.0)
            # 指数退避并加抖动：并发下多个请求同时重试会再次撞上限流。
            delay = retry_after or min(2 ** attempt, 8)
            delay += random.uniform(0, 0.5)
            if throttled:
                self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)
            await asyncio.sleep(delay)
        raise RuntimeError("MODEL_RETRY_EXHAUSTED")

    async def ask(self, task: str, payload: dict, *, search: Search | None = None) -> dict:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": COMMON + "\n" + PROMPTS[task]},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        tool_count = 0
        compatibility_tool_retry = 0
        json_repairs = 0
        semantic_search_required = (
            task == "chapter" and payload.get("route") == "SEMANTIC" and search is not None
        )
        for _ in range(self.max_rounds):
            body: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
            }
            # Some compatible providers turn function calls into ordinary JSON when
            # JSON mode is combined with tools. Validate final JSON locally instead.
            if search is None:
                body["response_format"] = {"type": "json_object"}
            if search is not None:
                body["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": "semantic_search_rules",
                            "description": "按当前章节正文主动搜索补充规则候选，可多次换词查询。",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "minLength": 1, "maxLength": 250}
                                },
                                "required": ["query"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ]
                body["tool_choice"] = "auto"
                if semantic_search_required and tool_count == 0:
                    # Only semantic_search_rules is exposed, so `required` forces
                    # the same action while supporting more compatible providers.
                    body["tool_choice"] = "required"
            raw = await self._request(body)
            choices = raw.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("MODEL_RESPONSE_INVALID")
            choice = choices[0]
            finish = choice.get("finish_reason")
            if finish not in {"stop", "tool_calls"}:
                raise RuntimeError(f"MODEL_INCOMPLETE_{finish}")
            message = choice["message"]
            calls = message.get("tool_calls") or []
            if not calls and search is not None:
                compat_args = _content_tool_request(message.get("content"))
                if compat_args is not None:
                    calls = [
                        {
                            "id": f"compat-{tool_count + 1}",
                            "type": "function",
                            "function": {
                                "name": "semantic_search_rules",
                                "arguments": json.dumps(compat_args, ensure_ascii=False),
                            },
                        }
                    ]
            if calls:
                tool_count += len(calls)
                if search is None or tool_count > 16 or len(calls) > 4:
                    raise ValueError("MODEL_TOOL_LIMIT_OR_UNAUTHORIZED")
                # Only relay protocol fields; provider internals never become authority.
                messages.append({"role": "assistant", "content": None, "tool_calls": calls})
                for call in calls:
                    if (
                        call.get("type") != "function"
                        or call["function"]["name"] != "semantic_search_rules"
                    ):
                        raise ValueError("MODEL_TOOL_UNAUTHORIZED")
                    try:
                        args = json.loads(call["function"]["arguments"])
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise ValueError("MODEL_TOOL_ARGUMENTS_INVALID") from exc
                    if (
                        set(args) != {"query"}
                        or not isinstance(args["query"], str)
                        or not 1 <= len(args["query"]) <= 250
                    ):
                        raise ValueError("MODEL_TOOL_ARGUMENTS_INVALID")
                    result = await search(args["query"])
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue
            if finish != "stop":
                raise ValueError("MODEL_MISSING_TOOL_CALLS")
            content = message.get("content")
            if semantic_search_required and tool_count == 0 and compatibility_tool_retry == 0:
                compatibility_tool_retry += 1
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "你尚未执行必需的语义规则检索。不要继续输出审查结论或候选规则；"
                            "请立即调用 semantic_search_rules。如果接口只能把工具请求放在普通消息中，"
                            '请仅输出 {"tool_name":"semantic_search_rules",'
                            '"parameters":{"query":"从当前章节正文提炼的查询词"}}。'
                            "执行器会校验并真实执行该请求。"
                        ),
                    }
                )
                continue
            try:
                result = json.loads(content or "")
            except (TypeError, json.JSONDecodeError) as exc:
                if json_repairs >= 1:
                    raise ValueError("MODEL_JSON_INVALID") from exc
                json_repairs += 1
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一个回答不是合法 JSON。请严格按照任务要求重新输出一个完整 JSON 对象；"
                            "不要输出解释、Markdown、thoughts 或工具调用模拟文本。"
                        ),
                    }
                )
                continue
            if not isinstance(result, dict):
                raise ValueError("MODEL_JSON_OBJECT_REQUIRED")
            return result
        raise RuntimeError("MODEL_TOOL_ROUNDS_EXHAUSTED")
