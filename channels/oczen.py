"""channels/oczen.py — OpenCodeZen 匿名免费通道。

渠道特性（对齐 wild-work/internal/oczen，2026-09 实测）：
  - 匿名凭证是字面量 "public"，无需登录、无 token 轮换、无签到、无积分；
  - 上游免费档三道校验：会话头须为 ses_<12hex><14Base62>、请求体须为
    「智能体形态」（stream=true 且 tools 内同时含 bash 与 read）、伪装头齐套；
  - 无请求数配额，唯一约束是并发升高时的延迟背压。

模型规格：上游 /v1/models 不返回上下文/最大输出，因此：
  ① 已知免费模型用社区目录（models.dev）的值补齐；
  ② 未知模型不猜——上下文与最大输出留 0，前端显示「未知」。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Optional

import httpx

from .common import (
    Account, Channel, ModelSpec, aggregate_chunks, log, register, sse_chunk, sse_done,
)

BASE = "https://opencode.ai/zen/v1"
ANONYMOUS_KEY = "public"
ANONYMOUS_UID = "oczen-anonymous"
ANONYMOUS_NAME = "匿名"
USER_AGENT = "opencode/1.18.31 (windows amd64; node22)"
NO_EXPIRY = 4102444800  # 2100-01-01
STUB_TOOLS = ("bash", "read")

_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _is_base62(c: str) -> bool:
    return c.isdigit() or c.isalpha()


def canonical_session_id(seed: str) -> str:
    """把任意会话种子映射成 ses_<12位小写hex><14位Base62>。

    已是规范形状则原样返回（保住 prompt cache 亲和）。
    上游自 2026-09-16 起对非该形状的会话头一律 403 FreeTierError。
    """
    if len(seed) == 4 + 12 + 14 and seed.startswith("ses_"):
        hex_part, b62_part = seed[4:16], seed[16:]
        if all(c in "0123456789abcdef" for c in hex_part) and all(_is_base62(c) for c in b62_part):
            return seed
    digest = __import__("hashlib").sha256(("ses\x00" + seed).encode()).digest()
    time_part = digest[:6].hex()          # 12 位小写 hex
    n = int.from_bytes(digest[6:16], "big")
    chars = []
    for _ in range(14):
        n, rem = divmod(n, 62)
        chars.append(_B62[rem])
    return "ses_" + time_part + "".join(reversed(chars))


def _random_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _conversation_seed(body: dict) -> str:
    """取首个 user 消息作为会话种子：同一对话映射同一会话（prompt cache 亲和）。"""
    for m in body.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        raw = json.dumps(m.get("content"), ensure_ascii=False)
        if raw and raw != "null":
            return raw
    return ""


def _stub_tool(name: str) -> dict:
    if name == "bash":
        return {"type": "function", "function": {
            "name": "bash", "description": "(internal placeholder — do not call)",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}
    return {"type": "function", "function": {
        "name": "read", "description": "(internal placeholder — do not call)",
        "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}}}}}


def ensure_agent_shape(body: dict) -> None:
    """就地改写请求体以通过免费档校验（强制流式 + 补齐 bash/read 桩工具）。"""
    body["stream"] = True
    tools = body.get("tools")
    if not isinstance(tools, list):
        tools = []
    have = set()
    for t in tools:
        if isinstance(t, dict) and t.get("type") == "function":
            fn = t.get("function") or {}
            if isinstance(fn, dict) and fn.get("name"):
                have.add(fn["name"])
    had_tools = len(tools) > 0
    for name in STUB_TOOLS:
        if name not in have:
            tools.append(_stub_tool(name))
    body["tools"] = tools
    if not had_tools:
        # 纯聊天：桩工具仅供上游过检，明确禁止调用
        body["tool_choice"] = "none"


# 已知免费模型的规格（models.dev 目录 + 2026-09 实测）。
_KNOWN: dict[str, ModelSpec] = {}


def _known(mid: str, name: str, ctx: int, max_out: int, reasoning: bool = True) -> None:
    _KNOWN[mid] = ModelSpec(id=mid, name=name, context_window=ctx, max_output_tokens=max_out,
                            supports_reasoning=reasoning, supports_images=False,
                            supports_tools=True, context_from_api=False,
                            rate=0.0, rate_note="免费")


_known("big-pickle", "Big Pickle", 200000, 32000)
_known("ling-3.0-flash-fin-free", "Ling 3.0 Flash Fin Free", 262144, 32768)
_known("mimo-v2.5-free", "MiMo V2.5 Free", 200000, 32000)
_known("mimo-v2.6-flash-free", "MiMo-V2.6-Flash Free", 200000, 32000)
_known("muse-spark-1.2-contributor-free", "Muse Spark 1.2 Free", 1048576, 131072)
_known("muse-spark-1.3-contributor-free", "Muse Spark 1.3 Free", 1048576, 131072)
_known("nemotron-3-ultra-free", "Nemotron 3 Ultra Free", 1000000, 128000)
_known("nemotron-3.5-lightning-free", "Nemotron 3.5 Lightning Free", 262144, 262144)


def is_free_model(mid: str) -> bool:
    low = (mid or "").strip().lower()
    return bool(low) and ("free" in low or low == "big-pickle")


class OczenChannel(Channel):
    KIND = "oczen"
    DISPLAY_NAME = "OpenCodeZen"
    MODEL_PREFIX = "oczen/"
    NEEDS_LOGIN = False

    def __init__(self):
        super().__init__()
        # 匿名渠道没有账号文件：内置单条虚拟账号
        self._accounts = [Account(
            kind=self.KIND, uid=ANONYMOUS_UID, nickname=ANONYMOUS_NAME,
            access_token=ANONYMOUS_KEY, expires_at=NO_EXPIRY, domain="opencode.ai",
        )]
        self._client = httpx.Client(
            timeout=httpx.Timeout(600.0, connect=15.0),
            headers={}, follow_redirects=True,
        )

    # 匿名渠道：账号固定，禁止增删
    def load_accounts(self) -> list[Account]:
        self._accounts = [Account(
            kind=self.KIND, uid=ANONYMOUS_UID, nickname=ANONYMOUS_NAME,
            access_token=ANONYMOUS_KEY, expires_at=NO_EXPIRY, domain="opencode.ai",
        )]
        return list(self._accounts)

    def save_account(self, acc: Account):  # noqa: D102
        raise RuntimeError("OpenCodeZen 为匿名渠道，无需添加账号")

    def delete_account(self, uid: str) -> bool:  # noqa: D102
        raise RuntimeError("OpenCodeZen 为匿名渠道，账号不可删除")

    def login_available(self) -> bool:
        return False

    def _headers(self, session: str, api_key: str = "") -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
            "Authorization": "Bearer " + (api_key or ANONYMOUS_KEY),
            "x-opencode-client": "cli",
            "x-opencode-session": session,
            "x-session-affinity": session,
            "X-Session-Id": session,
            "x-opencode-request": _random_id("req"),
            "x-opencode-project": _random_id("prj"),
        }

    # -- 对话 ---------------------------------------------------------------

    def chat_stream(self, acc: Account, body: dict, model: str, rid: str = "") -> tuple[int, bytes]:
        payload = dict(body)
        payload["model"] = model
        ensure_agent_shape(payload)
        seed = _conversation_seed(payload) or _random_id("fallback")
        session = canonical_session_id(seed)
        want_stream = bool(body.get("stream"))

        try:
            with self._client.stream("POST", f"{BASE}/chat/completions",
                                     headers=self._headers(session, acc.access_token),
                                     json=payload) as r:
                if r.status_code >= 400:
                    return r.status_code, r.read()
                chunks: list[dict] = []
                out: list[bytes] = []
                for line in r.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    obj["model"] = model  # 回填客户端模型名
                    if want_stream:
                        out.append(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
                    else:
                        chunks.append(obj)
        except httpx.HTTPError as e:
            return 502, json.dumps({"error": {"message": f"opencodezen 网络错误: {e}",
                                              "type": "upstream_error"}}).encode()

        if want_stream:
            out.append(sse_done())
            return 200, b"".join(out)
        agg = aggregate_chunks(chunks, model)
        return 200, json.dumps(agg, ensure_ascii=False).encode()

    # -- 模型 ---------------------------------------------------------------

    def fetch_models(self, acc: Account) -> list[ModelSpec]:
        req_id = _random_id("catalog")
        try:
            r = self._client.get(f"{BASE}/models",
                                 headers=self._headers(canonical_session_id(req_id)))
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            data = r.json()
        except Exception as e:
            log(f"oczen 模型列表拉取失败: {e}")
            return self.static_models()
        out: list[ModelSpec] = []
        for m in data.get("data") or []:
            mid = m.get("id") if isinstance(m, dict) else None
            if not mid or not is_free_model(mid):
                continue
            known = _KNOWN.get(mid)
            if known:
                out.append(known)
            else:
                # 未知免费模型：不猜规格（留 0，前端显示未知）；倍率恒为 0（匿名免费通道）
                out.append(ModelSpec(id=mid, name=mid, supports_tools=True,
                                     rate=0.0, rate_note="免费"))
        return out or self.static_models()

    def static_models(self) -> list[ModelSpec]:
        return list(_KNOWN.values())


register(OczenChannel())
