"""channels/trae.py — TraeWork（SOLO）渠道。

移植自 wild-work/internal/traework（协议逆向成果来自社区 trae2api 等项目）。

要点：
  - 登录：Web OAuth 授权（PKCE S256 + 本地一次性回调）。为不触碰用户桌面，
    这里只生成授权链接并**由用户自行在浏览器完成**；回调由本服务本地端口接收，
    或用户把回调链接整条粘贴回来解析（两种方式都支持）。
  - 对话：SOLO llm_utils_chat（OpenAI 请求体改写为 SOLO 形状 + 自定义 SSE 事件）。
  - 模型：/api/ide/v1/get_detail_param 拉取 config_info_list（含 display_name），
    模型规格（上下文/最大输出）上游不下发——不猜，留 0 由前端显示「未知」。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import uuid as _uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .common import Account, Channel, ModelSpec, aggregate_chunks, log, register, sse_done

AGENT_HOST = "https://trae-api-cn.mchost.guru"
UG_HOST = "https://api.trae.cn"
OAUTH_HOST = "https://api.trae.com.cn"
CONSOLE_HOST = "https://www.trae.cn"
WORK_HOST = "https://work.trae.cn"
CLIENT_ID = "en1oxy7wnw8j9n"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IDE_VERSION = "0.1.52"
IDE_VERSION_CODE = "20260811"
DEVICE_BRAND = "20Y5A002XX"
OS_VERSION = "Windows 10 Pro"
PLUGIN_VERSION = "2.3.73734"
FUNCTION_WORK = "solo_work_lite"

# 定价分组：Trae 同一上游有两个 function，各渠道记录自己所用的口径。
# 必须含**无** _remote 后缀的 solo_agent，否则拿不到新版 flash 模型的费率
# （实测 deepseek-v4.1-flash、glm-5.3-flash 仅在 solo_agent 分组下发）。
PRICING_FUNCTIONS = "solo_agent,solo_agent_remote,solo_work_remote,solo_design_remote"
# 主 function：同一模型可能在多个分组下倍率不同，对话按主 function 计费。
PRICING_PRIMARY = "solo_work_remote"

EP_CHAT = "/api/agent/v3/llm_utils_chat"
EP_MODELS = "/api/ide/v1/get_detail_param"
EP_PRICING = "/api/remote/v1/models"
EP_EXCHANGE = "/cloudide/api/v3/trae/oauth/ExchangeToken"
EP_AUTHCODE_EXCHANGE = "/trae/api/v3/oauth/ExchangeToken"
EP_USERINFO = "/cloudide/api/v3/trae/GetUserInfo"
# 网页版积分接口（POST {"require_usage":true}）：返回每个权益包的 credits_limit 与
# usage.credits_amount(实际用量)。对齐 wild-work/internal/traework 的 UserEntUsage。
EP_ENT_USAGE = "/trae/api/v2/pay/web_user_ent_usage"
# 每日签到（UG 接口，对齐 wild-work/internal/traework）：
#   POST status 查今日是否已签 + 活动是否开放；POST claim 领取（空 body）。
#   需设备指纹头，否则上游以 9074（频率限制）拒绝。
EP_CHECKIN_STATUS = "/trae/api/v2/ug/checkin_credits/status"
EP_CHECKIN_CLAIM = "/trae/api/v2/ug/checkin_credits/claim"
# 不可消耗池的商品 ID：209 = 200 档每日签到（官方客户端专用，本工具扣不到）。
# 上游自 2026-09-23 起不再用 available_endpoint 区分，只能靠 product_id。
UNUSABLE_PRODUCT_ID = 209

_PENDING = {"status": "pending"}

# 授权成功后的回调基址：Trae 授权页会 window.location.href 到
# `{此基址}/v1/channels/trae/login/callback?...`（令牌在 query 里）。
# Windows 浏览器经 WSL localhost 转发可直连本服务（已实测），故默认 127.0.0.1。


def _parse_features_rate(features) -> Optional[float]:
    """解析 features（JSON 字符串）里的积分倍率。

    优先取折扣价（discount.consumption_rate），否则取原价（consumption_rate.rate）。
    features 可能是字符串或已是 dict；无法解析时返回 None（倍率显示「未知」）。
    """
    if not features:
        return None
    if isinstance(features, str):
        try:
            f = json.loads(features)
        except Exception:
            return None
    elif isinstance(features, dict):
        f = features
    else:
        return None
    if not isinstance(f, dict):
        return None
    disc = f.get("discount") or {}
    if isinstance(disc, dict) and disc.get("enable"):
        d = disc.get("data") or {}
        if isinstance(d, dict) and d.get("consumption_rate"):
            return float(d["consumption_rate"])
    cr = f.get("consumption_rate") or {}
    if isinstance(cr, dict) and cr.get("enable"):
        d = cr.get("data") or {}
        if isinstance(d, dict) and d.get("rate"):
            return float(d["rate"])
    return None


def _rand_hex(n: int) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def _rand_numeric_id() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(15))


def _gen_pkce() -> tuple[str, str]:
    import base64
    import hashlib
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


# ---------------------------------------------------------------------------
# 请求体改写（OpenAI → SOLO）
# ---------------------------------------------------------------------------

def prepare_body(body: dict, function: str = FUNCTION_WORK) -> dict:
    """把 OpenAI chat 请求改写为 Trae SOLO llm_utils_chat 请求。"""
    obj = dict(body)
    obj["stream"] = True
    obj["function"] = function

    msgs = obj.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "developer":
                m["role"] = "system"
                role = "system"
            if role == "assistant":
                tcs = m.get("tool_calls")
                if isinstance(tcs, list):
                    kept = []
                    for tc in tcs:
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function")
                        if isinstance(fn, dict):
                            tc["function_call"] = fn
                            tc.pop("function", None)
                        fc = tc.get("function_call")
                        if isinstance(fc, dict) and not str(fc.get("name") or "").strip():
                            continue
                        kept.append(tc)
                    if kept:
                        m["tool_calls"] = kept
                    else:
                        m.pop("tool_calls", None)
            content = m.get("content")
            if isinstance(content, str):
                m["content"] = [{"type": "text", "text": content}]

    model = str(obj.get("model") or "").strip() or "glm-5.2"
    obj["config_name"] = model
    obj["model"] = model
    _normalize_tool_choice(obj)
    _normalize_tools(obj)
    return obj


def _normalize_tool_choice(obj: dict) -> None:
    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            obj.pop("tool_choice", None)
            obj.pop("tools", None)
            obj.pop("functions", None)
        return
    if isinstance(tc, dict):
        typ = str(tc.get("type") or "").strip().lower()
        if typ == "none":
            obj.pop("tool_choice", None)
            obj.pop("tools", None)
            obj.pop("functions", None)
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name") or tc.get("name") or "").strip()
            obj["tool_choice"] = name or "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)


def _normalize_tools(obj: dict) -> None:
    tools = obj.get("tools")
    if not isinstance(tools, list) or not tools:
        return
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            continue
        params = fn.get("parameters")
        if isinstance(params, (dict, list)):
            fn["parameters"] = json.dumps(params, ensure_ascii=False)
        out.append(t)
    if out:
        obj["tools"] = out
    else:
        obj.pop("tools", None)


# ---------------------------------------------------------------------------
# SOLO SSE 解析
# ---------------------------------------------------------------------------

class SoloStreamError(RuntimeError):
    def __init__(self, code: int, msg: str):
        super().__init__(f"solo error code={code} msg={msg}")
        self.code = code
        self.msg = msg


def parse_solo_sse(text: str):
    """解析 SOLO 事件流，产出归一化事件 dict 列表。"""
    events = []
    ev_name, data_lines = "", []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if line == "":
            if ev_name or data_lines:
                events.append((ev_name, "\n".join(data_lines)))
                ev_name, data_lines = "", []
            continue
        if line.startswith("event:"):
            ev_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        # 其余（含注释行）忽略
    if ev_name or data_lines:
        events.append((ev_name, "\n".join(data_lines)))
    return events


def solo_to_openai_chunks(text: str, model: str) -> tuple[list[dict], Optional[SoloStreamError]]:
    """把 SOLO 事件流转换为 OpenAI 形状的 chunk 列表（同时合并 tool_calls）。"""
    cid = f"chatcmpl-{int(time.time()*1000)}"
    chunks: list[dict] = []
    err: Optional[SoloStreamError] = None
    tool_calls: dict[int, dict] = {}
    order: list[int] = []

    def _emit(delta: dict, finish=None) -> None:
        chunks.append({
            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        })

    for ev, data in parse_solo_sse(text):
        if not data:
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if ev == "token_usage":
            # SOLO 的 usage 独立成事件（不挂在 delta 上）；必须转成末帧 usage，
            # 否则下游聚合/记账拿不到 token 数（实测丢失）。
            # 上游口径（实测两次对照确认）：prompt_tokens **已包含**缓存，
            #   prompt=530 + completion=4002 = total=4532，而 cache_read=512 是其中命中部分。
            # 即 prompt_tokens 是 OpenAI 语义（输入含缓存），cache_read 只是子集，
            # 因此**不能**再把它加进 prompt（否则重复计数、数值虚高）。
            # 统一转成 OpenAI 规范：prompt_tokens 原样 + prompt_tokens_details.cached_tokens。
            u = obj if isinstance(obj, dict) else {}
            prompt = int(u.get("prompt_tokens") or 0)
            completion = int(u.get("completion_tokens") or 0)
            total = int(u.get("total_tokens") or (prompt + completion))
            cache_read = int(u.get("cache_read_input_tokens") or 0)
            cache_creation = int(u.get("cache_creation_input_tokens") or 0)
            usage = {"prompt_tokens": prompt, "completion_tokens": completion,
                     "total_tokens": total}
            if cache_read or cache_creation:
                usage["prompt_tokens_details"] = {
                    "cached_tokens": cache_read,
                    "cache_creation_tokens": cache_creation,
                }
            if cache_creation:
                # usage_stats 从该键读 cache_write（写入缓存的溢价部分，单独计费）
                usage["cache_creation_input_tokens"] = cache_creation
            if u.get("reasoning_tokens"):
                usage["completion_tokens_details"] = {
                    "reasoning_tokens": int(u["reasoning_tokens"])}
            chunks.append({
                "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                "usage": usage,
            })
        elif ev == "output":
            delta: dict = {}
            if obj.get("response"):
                delta["content"] = obj["response"]
            if obj.get("reasoning_content"):
                delta["reasoning_content"] = obj["reasoning_content"]
            tc = obj.get("tool_calls")
            if tc:
                arr = tc if isinstance(tc, list) else [tc]
                deltas = []
                for call in arr:
                    if not isinstance(call, dict):
                        continue
                    idx = int(call.get("index") or 0)
                    fn = call.get("function") if isinstance(call.get("function"), dict) else None
                    if fn is None and isinstance(call.get("function_call"), dict):
                        fn = call["function_call"]
                    fn = fn or {}
                    fn = {k: v for k, v in fn.items() if k in ("name", "arguments")}
                    deltas.append({"index": idx, "id": call.get("id") or "",
                                   "type": call.get("type") or "function", "function": fn})
                    cur = tool_calls.setdefault(idx, {"index": idx, "id": "", "type": "function",
                                                      "function": {"name": "", "arguments": ""}})
                    if call.get("id"):
                        cur["id"] = call["id"]
                    if fn.get("name"):
                        cur["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        cur["function"]["arguments"] += fn["arguments"]
                    if idx not in order:
                        order.append(idx)
                if deltas:
                    delta["tool_calls"] = deltas
            if delta:
                _emit(delta)
        elif ev == "done":
            _emit({}, finish=obj.get("finish_reason") or "stop")
        elif ev == "error":
            err = SoloStreamError(int(obj.get("code") or 0), str(obj.get("message") or ""))
    return chunks, err


class TraeChannel(Channel):
    KIND = "trae"
    DISPLAY_NAME = "TraeWork"
    MODEL_PREFIX = "trae/"
    # 自动回调式：授权页会 window.location.href 回本服务的 /login/callback，
    # 由服务端直接完成登录（面板轮询账号是否出现，并提供粘贴链接兜底）。
    LOGIN_MODE = "auto_callback"

    def __init__(self):
        super().__init__()
        self._client = httpx.Client(timeout=httpx.Timeout(600.0, connect=15.0))
        self._logins: dict[str, dict] = {}

    # -- 请求头 -------------------------------------------------------------

    def _solo_headers(self, acc: Account, stream: bool) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": f"Trae/{IDE_VERSION}",
            "Authorization": f"Cloud-IDE-JWT {acc.access_token}",
            "X-Cloudide-Token": acc.access_token,
            "X-Ide-Token": acc.access_token,
            "X-App-Id": APP_ID,
            "X-App-Version": "default",
            "X-Ide-Version": IDE_VERSION,
            "X-Ide-Version-Code": IDE_VERSION_CODE,
            "X-App-Version-Code": IDE_VERSION_CODE,
            "X-Ide-Version-Type": "stable",
            "X-Device-Type": "windows",
            "X-OS-Version": OS_VERSION,
            "X-Device-Brand": DEVICE_BRAND,
            "Request-Traffic-Type": "prod",
        }
        if acc.uid:
            h["X-Uid"] = acc.uid
        if acc.extra.get("machineId"):
            h["X-Machine-Id"] = acc.extra["machineId"]
        if acc.extra.get("deviceId"):
            h["x-device-id"] = acc.extra["deviceId"]
        return h

    # -- token 刷新 ---------------------------------------------------------

    def refresh_token(self, acc: Account) -> None:
        if not acc.refresh_token:
            raise RuntimeError("no refreshToken")
        host = acc.extra.get("apiHost") or OAUTH_HOST
        r = self._client.post(
            f"{host}{EP_EXCHANGE}",
            json={"ClientID": CLIENT_ID, "RefreshToken": acc.refresh_token,
                  "ClientSecret": "-", "UserID": ""},
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": f"Trae/{IDE_VERSION}"})
        if r.status_code >= 400:
            raise RuntimeError(f"exchange HTTP {r.status_code}: {r.text[:200]}")
        res = (r.json() or {}).get("Result") or {}
        token = res.get("Token") or ""
        if not token:
            raise RuntimeError("refresh_failed: no token in response — re-login required")
        acc.access_token = token
        if res.get("RefreshToken"):
            acc.refresh_token = res["RefreshToken"]
        exp = int(res.get("TokenExpireAt") or 0)
        if exp:
            acc.expires_at = float(exp / 1000 if exp > 1e12 else exp)
        elif res.get("TokenExpireDuration"):
            dur = int(res["TokenExpireDuration"])
            acc.expires_at = time.time() + (dur / 1000 if dur > 1e9 else dur)
        # uid 为空说明还在登录流程中（尚未 _user_info），此时落盘会产生
        # uid 为空的凭据文件（表现为面板出现一张 UID 空白、无法删除的卡）
        if acc.uid:
            self.store.save(acc)
        log(f"trae token 刷新成功 uid={acc.uid}")

    def ensure_token(self, acc: Account) -> None:
        if acc.expired(skew=300):
            try:
                self.refresh_token(acc)
            except Exception as e:
                log(f"trae token 刷新失败（沿用旧 token）: {e}")

    # -- 模型 ---------------------------------------------------------------

    def fetch_models(self, acc: Account) -> list[ModelSpec]:
        self.ensure_token(acc)
        body = {"function": FUNCTION_WORK, "config_names": None, "need_prompt": False,
                "current_config_info": None, "poly_prompt": True, "mode_type": None,
                "agent_type": None}
        r = self._client.post(f"{AGENT_HOST}{EP_MODELS}", json=body,
                              headers=self._solo_headers(acc, stream=False))
        if r.status_code >= 400:
            raise RuntimeError(f"models HTTP {r.status_code}: {r.text[:200]}")
        data = r.json() or {}
        # 定价（含积分倍率）：独立接口，失败不影响模型列表
        rates = self.fetch_pricing(acc)
        out: list[ModelSpec] = []
        seen: set[str] = set()   # 上游可能为同一模型返回多条配置（流式/非流式），须去重
        skipped: list[str] = []
        for cfg in data.get("config_info_list") or []:
            if not isinstance(cfg, dict):
                continue
            name = str(cfg.get("config_name") or "").strip()
            if not name or name in seen:
                continue
            dc = cfg.get("display_config") or {}
            if dc.get("is_custom_model") or name.startswith("custom_model_"):
                continue
            # 无积分倍率 = 落后/内部模型（实测含 summary、file_search_agent、
            # computer_use_subagent 等工具型，以及 glm-5、DeepSeek-V4-Pro 等旧版），
            # 官方计费接口已不再为其定价，不对外提供。
            # 但定价接口整体失败（rates 为空）时不能据此过滤，否则会清空整个列表。
            if rates and name not in rates:
                skipped.append(name)
                continue
            seen.add(name)
            rate = rates.get(name)
            # 上游不下发上下文/最大输出：不猜，留 0（前端显示「未知」）
            out.append(ModelSpec(id=name, name=dc.get("display_name") or name,
                                 supports_tools=True, context_from_api=False,
                                 rate=rate,
                                 rate_note="折扣" if rate else "",
                                 extra={"source": "upstream"}))
        if skipped:
            log(f"trae 跳过 {len(skipped)} 个无倍率模型（落后/内部）: "
                f"{', '.join(skipped[:8])}{' …' if len(skipped) > 8 else ''}")
        if not out:
            raise RuntimeError("models api returned empty list")
        return out

    def fetch_pricing(self, acc: Account) -> dict[str, float]:
        """拉取模型积分倍率（/api/remote/v1/models 的 features.consumption_rate）。

        返回 {config_name: rate}；接口不可用时返回空 dict（倍率显示「未知」）。
        同一模型可能出现在多个 function 分组下且倍率不同（实测豆包 Seed-2.1-Pro
        为 solo_agent=0.08 / _remote=0.8），对话按主 function 计费，故主 function 优先。
        """
        url = (f"{WORK_HOST}{EP_PRICING}?functions={PRICING_FUNCTIONS}"
               f"&show_custom_model=true")
        try:
            r = self._client.get(url, headers={
                "Authorization": f"Cloud-IDE-JWT {acc.access_token}",
                "X-Trae-Client-Type": "web",
                "X-Trae-User-Timezone": "Asia/Shanghai",
                "X-Preferenced-Language": "zh-cn",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
                "Referer": f"{WORK_HOST}/",
            })
            if r.status_code >= 400:
                log(f"trae 定价接口 HTTP {r.status_code}，倍率留空")
                return {}
            data = r.json() or {}
            if data.get("code") not in (0, None):
                log(f"trae 定价接口 code={data.get('code')}，倍率留空")
                return {}
        except Exception as e:
            log(f"trae 定价拉取失败（倍率留空）: {e}")
            return {}
        by_name: dict[str, float] = {}
        for fn in (data.get("data") or {}).get("list") or []:
            if not isinstance(fn, dict):
                continue
            is_primary = fn.get("function") == PRICING_PRIMARY
            for m in fn.get("models") or []:
                if not isinstance(m, dict):
                    continue
                name = str(m.get("name") or "").strip()
                if not name:
                    continue
                rate = _parse_features_rate(m.get("features"))
                if rate is None:
                    continue
                if name not in by_name or is_primary:
                    by_name[name] = rate
        return by_name

    # -- 对话 ---------------------------------------------------------------

    def chat_stream(self, acc: Account, body: dict, model: str, rid: str = "") -> tuple[int, bytes]:
        self.ensure_token(acc)
        payload = prepare_body(body)
        payload["config_name"] = model
        payload["model"] = model
        want_stream = bool(body.get("stream"))
        try:
            r = self._client.post(f"{AGENT_HOST}{EP_CHAT}", json=payload,
                                  headers=self._solo_headers(acc, stream=True))
            if r.status_code >= 400:
                return r.status_code, r.content
            text = r.text
        except httpx.HTTPError as e:
            return 502, json.dumps({"error": {"message": f"trae 网络错误: {e}",
                                              "type": "upstream_error"}}).encode()

        chunks, err = solo_to_openai_chunks(text, model)
        if err is not None:
            code = 402 if err.code == 1005 else 502
            return code, json.dumps({"error": {"message": str(err),
                                               "type": "upstream_error"}}).encode()
        if want_stream:
            out = [f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode() for c in chunks]
            out.append(sse_done())
            return 200, b"".join(out)
        return 200, json.dumps(aggregate_chunks(chunks, model),
                               ensure_ascii=False).encode()

    # -- 积分 ---------------------------------------------------------------

    def _ug_headers(self, acc: Account) -> dict:
        """UG 接口（积分/签到）请求头：含设备指纹，缺任一环节上游会以 9074 拒绝。"""
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"Trae/{IDE_VERSION}",
            "Authorization": f"Cloud-IDE-JWT {acc.access_token}",
            "X-User-Region": "CN",
            "x-device-brand": DEVICE_BRAND,
            "x-device-type": "windows",
            "x-os-version": OS_VERSION,
            "x-app-version": IDE_VERSION,
        }
        if acc.extra.get("deviceId"):
            h["x-device-id"] = acc.extra["deviceId"]
        return h

    def checkin(self, acc: Account) -> dict:
        """Trae 每日签到（对齐 wild-work/internal/traework 的 DailyCheckin）。

        流程：查 status → 已签/未开放则直接返回；否则 claim 领取 → 复查确认。
        返回 {"ok", "msg"}（与其它渠道同构，供面板统一展示）。
        """
        try:
            self.ensure_token(acc)
        except Exception as e:
            return {"ok": False, "msg": f"token 不可用: {e}"}
        h = self._ug_headers(acc)

        def _status() -> tuple[bool, bool, str]:
            """返回 (已签到, 活动开放, 错误信息)。"""
            try:
                r = self._client.post(f"{UG_HOST}{EP_CHECKIN_STATUS}", json={}, headers=h)
            except Exception as e:
                return False, False, f"状态查询异常: {e}"
            if r.status_code >= 400:
                return False, False, f"状态查询失败 HTTP {r.status_code}"
            try:
                d = r.json() or {}
            except Exception:
                return False, False, "状态响应解析失败"
            code = int(d.get("code") or 0)
            msg = str(d.get("message") or d.get("msg") or "")
            if code != 0:
                return False, False, f"{msg or '状态查询失败'} (code={code})"
            return bool(d.get("checked_in")), bool(d.get("enable", True)), ""

        checked, enable, err = _status()
        if err:
            return {"ok": False, "msg": err}
        if checked:
            return {"ok": True, "msg": "今日已签到"}
        if not enable:
            return {"ok": False, "msg": "该账号未开放签到活动"}
        # 领取（空 body；9074 = 频率限制，稍等重试一次）
        for attempt in range(2):
            try:
                r2 = self._client.post(f"{UG_HOST}{EP_CHECKIN_CLAIM}", json={}, headers=h)
                d2 = r2.json() if r2.status_code < 400 else {}
            except Exception as e:
                return {"ok": False, "msg": f"领取异常: {e}"}
            code = int((d2 or {}).get("code") or 0)
            if code == 9074 and attempt == 0:
                time.sleep(3)
                continue
            break
        if code != 0:
            msg = str((d2 or {}).get("message") or (d2 or {}).get("msg") or "")
            return {"ok": False, "msg": f"{msg or '领取失败'} (code={code})"}
        # 复查确认（claim 的响应可能是无害业务码，以最终状态为准）
        checked2, _, err2 = _status()
        if err2:
            return {"ok": True, "msg": "已提交签到（状态复查失败，稍后可在积分里确认）"}
        return ({"ok": True, "msg": "签到成功"} if checked2
                else {"ok": False, "msg": "领取已提交但未生效，请稍后重试"})

    def credits(self, acc: Account) -> dict:
        """查询 Trae 积分（网页版 web_user_ent_usage）。

        返回 {"total_remain", "total_size", "packages":[...]}，与其它渠道同构，
        便于面板统一展示。不可消耗池（product_id=209，官方客户端专用）不计入
        total_remain，但会在 packages 里带 usable=False 标记。
        """
        self.ensure_token(acc)
        h = self._ug_headers(acc)
        r = self._client.post(f"{UG_HOST}{EP_ENT_USAGE}", json={"require_usage": True},
                              headers=h)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")
        data = r.json() or {}
        packs = data.get("user_entitlement_pack_list") or []
        packages, total_remain, total_size, unusable = [], 0, 0, 0
        for p in packs:
            if not isinstance(p, dict):
                continue
            base = p.get("entitlement_base_info") or {}
            quota = base.get("quota") or {}
            limit = int(float(quota.get("credits_limit") or 0))
            used = int(float((p.get("usage") or {}).get("credits_amount") or 0))
            remain = max(limit - used, 0)
            # 可用性：product_id=209 为官方客户端专用池（历史兜底看 available_endpoint==1）
            usable = not (base.get("product_id") == UNUSABLE_PRODUCT_ID
                          or base.get("available_endpoint") == 1)
            exp = int(p.get("expire_time") or 0)
            packages.append({
                "name": base.get("package_name") or p.get("display_desc") or "权益包",
                "remain": remain, "used": used, "size": limit,
                "unit": "credits",
                "cycle_end": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else "",
                "usable": usable,
            })
            # 分母口径必须与分子一致：不可用池既不计入 remain，也不能计入 size，
            # 否则前端「已用 = size - remain」会凭空多出不可用池的额度（假异常）。
            if usable:
                total_size += limit
                total_remain += remain
            else:
                unusable += remain
        packages.sort(key=lambda x: (not x.get("usable"), x["cycle_end"] or "9999-12-31"))
        # 到期时间升序（同可用性内），即将过期的排前面
        return {"total_remain": total_remain, "total_size": total_size,
                "unusable": unusable, "packages": packages, "error": None}

    # -- 登录（生成链接由用户自行授权；支持粘贴回调链接） --------------------

    def login_start(self, callback_port: int = 0) -> dict:
        machine_id = _rand_hex(32)
        device_id = _rand_numeric_id()
        verifier, challenge = _gen_pkce()
        sid = secrets.token_urlsafe(12)
        self._logins[sid] = {
            "machineId": machine_id, "deviceId": device_id,
            "codeVerifier": verifier, "at": time.time(),
            "callback": "", "host": OAUTH_HOST,
        }
        # 回调地址：**必须与 wild-work 完全同形** —— http://127.0.0.1:<port>/authorize，
        # 不能带任何 query。上游 GetRefreshToken 接口对 auth_callback_url 做严格校验，
        # 带 ?session=… 的形态实测直接失败（授权页显示「登录失败 网络错误」）。
        # 会话关联用 wild-work 同款方案：每次登录临时监听一个随机端口，端口即会话；
        # 回调处理完自动关闭（5 分钟无人授权也自动回收）。
        srv = self._start_one_shot_callback_server(sid)
        port = srv.server_address[1]
        cb_url = f"http://127.0.0.1:{port}/authorize"
        # 参数与 wild-work/internal/login_trae 完全对齐（其实现已验证可用）：
        #   redirect=0  「已登录则静默授权」；授权页把凭证 POST 到 auth_callback_url
        #   auth_from=solo + login_channel=native_ide  标识 SOLO 办公版渠道
        params = {
            "login_version": "1", "auth_from": "solo", "login_channel": "native_ide",
            "plugin_version": PLUGIN_VERSION, "auth_type": "local", "client_id": CLIENT_ID,
            "redirect": "0", "login_trace_id": str(_uuid.uuid4()),
            "auth_callback_url": cb_url,
            "machine_id": machine_id, "device_id": device_id, "x_device_id": device_id,
            "x_machine_id": machine_id, "x_device_brand": DEVICE_BRAND,
            "x_device_type": "windows", "x_os_version": OS_VERSION, "x_env": "",
            "x_app_version": IDE_VERSION, "x_app_type": "stable",
            "code_challenge": challenge, "code_challenge_method": "S256",
            "hide_saas_login": "true", "channel_name": "common",
            "click_id": f"TRAE SOLOSetup-stable-{PLUGIN_VERSION}",
        }
        url = f"{CONSOLE_HOST}/authorization?{urlencode(params)}"
        return {
            "url": url,
            "session": sid,
            # 关键前置条件：该授权页会先调 CheckLogin 判断浏览器登录态，
            # 未登录 Trae 账号时直接显示「登录失败 网络错误」（实测）。
            # 所以必须先在同一浏览器登录 trae.cn，再打开授权链接。
            "prerequisite_url": f"{CONSOLE_HOST}/login",
            "prerequisite": "若浏览器未登录 TRAE，请先登录（手机号验证码 / 抖音 / 苹果均可）",
            "hint": ("【第 1 步】确保浏览器已登录 TRAE（未登录先访问 "
                     "https://www.trae.cn/login 登录）；"
                     "【第 2 步】点「在新标签打开 ↗」打开授权链接，页面会自动完成授权；"
                     "【第 3 步】浏览器自动跳回本服务完成登录，无需手工复制。"),
        }

    # -- 登录回调服务器（wild-work 同款：随机端口 + /authorize，处理完即关） ----

    def _start_one_shot_callback_server(self, sid: str):
        """起一个一次性 HTTP 回调服务器：127.0.0.1:<随机端口>/authorize。

        - 授权页授权成功后跳转/POST 到该地址投递凭证（refreshToken/userJwt/authCodeInfo）；
        - 收到回调 → 调 login_submit_callback 完成落盘 → 返回结果页 → 服务器自动关闭；
        - 5 分钟无人授权自动关闭（与 wild-work 一致）。
        端口本身即会话标识，因此 auth_callback_url 无需携带 query（上游校验要求）。
        """
        ch = self
        srv_ref = {}

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):   # 静默
                pass

            def _merge_body(self) -> str:
                """POST body（JSON/form）合并进 query，返回最终 callback URL。"""
                parsed = urlparse(self.path)
                q = parse_qs(parsed.query)
                if self.command == "POST":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        raw = self.rfile.read(min(n, 65536)).decode("utf-8", "replace")
                    except Exception:
                        raw = ""
                    if raw:
                        import json as _json
                        try:
                            obj = _json.loads(raw)
                            if isinstance(obj, dict):
                                for k, v in obj.items():
                                    if isinstance(v, str):
                                        q.setdefault(k, [v])
                        except Exception:
                            from urllib.parse import parse_qsl
                            for k, v in parse_qsl(raw):
                                q.setdefault(k, [v])
                flat = {k: v[0] for k, v in q.items() if v}
                from urllib.parse import urlencode as _urlencode
                return f"http://127.0.0.1/callback?{_urlencode(flat)}"

            def _respond(self, ok: bool, msg: str):
                color = "#32f08c" if ok else "#ff6b6b"
                icon = "✓" if ok else "✕"
                title = "登录成功" if ok else "登录失败"
                html = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                        f'<title>{title}</title><style>body{{background:#0a0b0d;color:#fff;'
                        f'font-family:system-ui,sans-serif;display:flex;align-items:center;'
                        f'justify-content:center;height:100vh;margin:0;text-align:center}}'
                        f'.ic{{font-size:56px;color:{color}}}</style></head><body>'
                        f'<div><div class="ic">{icon}</div><h1>{title}</h1>'
                        f'<p style="color:#9ca3af">{msg}</p>'
                        f'<p style="color:#6b7280">此窗口可以关闭，请返回控制台查看</p></div></body></html>')
                data = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                callback_url = self._merge_body()
                try:
                    res = ch.login_submit_callback(sid, callback_url)
                except Exception as e:
                    res = {"status": "error", "message": str(e)}
                ok = res.get("status") == "ok"
                msg = res.get("message") or res.get("status") or ""
                if ok:
                    # 会话标记完成，供面板轮询读取（login_submit_callback 内部已 pop）
                    ch._logins[sid] = {"doneUid": res.get("uid") or "", "at": time.time()}
                else:
                    ch._logins[sid] = {"failed": msg, "at": time.time()}
                self._respond(ok, msg)
                srv_ref["srv"].shutdown()

            def do_GET(self):
                self._handle()

            do_POST = do_GET

        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        srv_ref["srv"] = srv
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        threading.Timer(300.0, srv.shutdown).start()   # 5 分钟超时回收（wild-work 同款）
        return srv

    def login_poll(self, session: str) -> dict:
        """轮询登录结果。

        Trae 为自动回调式：授权页会跳回本服务写入账号，因此这里只需检查
        该会话对应的账号是否已落盘（回调成功后 _logins 里会记录 uid）。
        """
        st = self._logins.get(session)
        if not st:
            # 会话已被回调消费 → 说明登录已成功（回调成功时会 pop 掉）
            accs = self.accounts()
            if accs:
                a = accs[-1]
                return {"status": "ok", "message": f"登录成功：{a.nickname or a.uid}",
                        "uid": a.uid}
            return {"status": "error",
                    "message": "登录会话不存在或已过期，请重新发起"}
        if st.get("failed"):
            self._logins.pop(session, None)
            return {"status": "error", "message": f"登录失败：{st['failed']}"}
        done_uid = st.get("doneUid")
        if done_uid:
            a = next((x for x in self.accounts() if x.uid == done_uid), None)
            self._logins.pop(session, None)
            return {"status": "ok",
                    "message": f"登录成功：{(a.nickname if a else '') or done_uid}",
                    "uid": done_uid}
        return {"status": "pending",
                "message": "等待浏览器完成授权…（授权成功后会自动跳回）"}

    def login_submit_callback(self, session: str, callback_url: str) -> dict:
        st = self._logins.get(session)
        if not st:
            return {"status": "error", "message": "登录会话不存在或已过期，请重新发起"}
        q = parse_qs(urlparse(callback_url).query)
        host = (q.get("host") or [OAUTH_HOST])[0] or OAUTH_HOST
        refresh = (q.get("refreshToken") or [""])[0]
        access = ""
        auth_code = ""
        if not refresh:
            import urllib.parse as _up
            uj = _up.unquote((q.get("userJwt") or [""])[0])
            if uj:
                try:
                    m = json.loads(uj)
                    refresh = str(m.get("RefreshToken") or "")
                    access = str(m.get("Token") or "")
                except Exception:
                    pass
        if not refresh and not access:
            raw = (q.get("authCodeInfo") or [""])[0]
            if raw:
                try:
                    m = json.loads(raw)
                    auth_code = str(m.get("AuthCode") or "")
                    for cont in (m, m.get("Result"), m.get("result")):
                        if isinstance(cont, dict) and cont.get("AuthCode"):
                            auth_code = str(cont["AuthCode"])
                            break
                except Exception:
                    auth_code = raw
        if not refresh and not access and not auth_code:
            return {"status": "error",
                    "message": "回调链接里找不到 refreshToken / authCodeInfo，请确认复制的是登录后的完整地址"}

        acc = Account(kind=self.KIND, uid="", refresh_token=refresh, access_token=access,
                      domain="trae.cn",
                      extra={"machineId": st["machineId"], "deviceId": st["deviceId"],
                             "apiHost": host})
        if auth_code:
            res = self._exchange_auth_code(acc, auth_code, st["codeVerifier"])
            acc.access_token = res["accessToken"]
            acc.refresh_token = res.get("refreshToken") or acc.refresh_token
            acc.expires_at = res.get("expiresAt") or 0
            if res.get("host"):
                acc.extra["apiHost"] = res["host"]
        elif acc.refresh_token:
            self.refresh_token(acc)
        uid, nick, ent = self._user_info(acc)
        if not uid:
            # token 换到了但拿不到账号信息 → 凭证无效，明确失败；
            # 绝不落盘空账号（否则面板会出现 UID 空白且无法删除的卡片）
            raise RuntimeError("无法获取账号信息（凭证可能无效），登录未完成")
        acc.uid = uid
        acc.nickname = nick or acc.uid
        acc.enterprise_id = ent
        self.save_account(acc)
        self._logins.pop(session, None)
        return {"status": "ok", "message": f"登录成功：{acc.nickname}", "uid": acc.uid}

    def _exchange_auth_code(self, acc: Account, auth_code: str, verifier: str) -> dict:
        """AuthCode + PKCE verifier + 设备公钥换 token。"""
        pub_pem = _device_public_key_pem()
        device_info = {
            "DeviceID": acc.extra.get("deviceId", ""),
            "MachineID": acc.extra.get("machineId", ""),
            "PlatformCode": "SOLO_PC", "DeviceType": "PC",
            "DeviceName": "PC", "DeviceModel": DEVICE_BRAND,
            "ClientVersion": IDE_VERSION, "DevicePublicKey": pub_pem,
            "DeviceBrand": DEVICE_BRAND, "DeviceCPU": "", "OSInfo": "windows",
            "OSVersion": OS_VERSION,
        }
        body = {"ClientID": CLIENT_ID, "AuthCode": auth_code, "CodeVerifier": verifier,
                "DeviceInfo": device_info, "IDEVersion": IDE_VERSION}
        origins = [UG_HOST, (acc.extra.get("apiHost") or "").rstrip("/"), OAUTH_HOST]
        last = ""
        for origin in [o for o in origins if o]:
            try:
                r = self._client.post(f"{origin}{EP_AUTHCODE_EXCHANGE}", json=body,
                                      headers={"Content-Type": "application/json",
                                               "Accept": "application/json",
                                               "User-Agent": f"Trae/{IDE_VERSION}"})
            except Exception as e:
                last = f"{origin} => {e}"
                continue
            if r.status_code >= 400:
                last = f"{origin} => HTTP {r.status_code} {r.text[:160]}"
                continue
            data = r.json() or {}
            token = refresh = ""
            exp = 0
            for cont in (data.get("Result"), data.get("result"), data.get("data"), data):
                if not isinstance(cont, dict):
                    continue
                token = (cont.get("AccessToken") or cont.get("accessToken")
                         or cont.get("access_token") or cont.get("Token") or cont.get("token") or "")
                if token:
                    refresh = (cont.get("RefreshToken") or cont.get("refreshToken")
                               or cont.get("refresh_token") or "")
                    exp = int(cont.get("TokenExpireAt") or cont.get("expiresAt")
                              or cont.get("expiredAt") or 0)
                    break
            if not token:
                last = f"{origin} => response missing token"
                continue
            if exp > 1e12:
                exp = int(exp / 1000)
            return {"accessToken": token, "refreshToken": refresh, "expiresAt": float(exp),
                    "host": origin}
        raise RuntimeError(f"AuthCode ExchangeToken failed: {last}")

    def _user_info(self, acc: Account) -> tuple[str, str, str]:
        host = acc.extra.get("apiHost") or OAUTH_HOST
        for endpoint in (f"{host}{EP_USERINFO}", f"{OAUTH_HOST}{EP_USERINFO}"):
            try:
                r = self._client.post(endpoint, json={},
                                      headers={"Content-Type": "application/json",
                                               "Accept": "application/json",
                                               "Authorization": f"Cloud-IDE-JWT {acc.access_token}",
                                               "X-Cloudide-Token": acc.access_token,
                                               "X-Ide-Token": acc.access_token,
                                               "User-Agent": f"Trae/{IDE_VERSION}"})
                if r.status_code >= 400:
                    continue
                res = (r.json() or {}).get("Result") or {}
                return (str(res.get("UserID") or ""), str(res.get("ScreenName") or ""),
                        str(res.get("EnterpriseID") or ""))
            except Exception:
                continue
        return "", "", ""


def _device_public_key_pem() -> str:
    """一次性 ECDSA P256 公钥 PEM（官方客户端持私钥用于后续鉴权）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


register(TraeChannel())
