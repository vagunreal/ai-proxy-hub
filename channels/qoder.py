"""channels/qoder.py — Qoder（qoder.com.cn）渠道。

移植自 wild-work/internal/qodercn（协议逆向成果来自社区 qoder2api 项目）。

要点：
  - 登录：OAuth 设备授权流（PKCE S256，轮询 deviceToken/poll）；只输出授权链接，
    由用户自行在浏览器完成，服务端不触碰任何桌面环境。
  - 对话：COSY 签名 + QoderEncoding + 嵌套 SSE（外层 data:{body:"<json>"}）。
  - 模型：动态拉取 /algo/api/v2/model/list?Encode=1，上下文窗口取
    context_config 的默认档（无标记不猜），最大输出按上游 max_input_tokens 与
    档位推导；is_vl → 图像输入，is_reasoning → 思考模式。
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Optional

import httpx

from .common import (
    Account, Channel, ModelSpec, aggregate_chunks, log, register, sse_chunk, sse_done,
)
from .qoder_cosy import CosySession, ensure_fingerprint, new_uuid4, qoder_encode

OPENAPI_BASE = "https://openapi.qoder.com.cn"
GATEWAY_BASE = "https://gateway.qoder.com.cn"
OAUTH_WEBSITE = "https://qoder.com.cn"
OAUTH_CLIENT_ID = "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb"

EP_DT_REFRESH = "/api/v1/deviceToken/refresh"
EP_USERINFO = "/api/v1/userinfo"
EP_MODELS = "/algo/api/v2/model/list?Encode=1"
EP_CHAT = ("/algo/api/v2/service/pro/sse/agent_chat_generation"
           "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1")
EP_QUOTA = "/api/v2/quota/usage"
# 每日签到（campaigns 活动路径）：legacy daily-check-in 已全局 DISABLED，
# campaigns 是唯一真实领取路径（对齐 wild-work/internal/qodercn/checkin.go）。
EP_CAMPAIGNS = "/sash/api/v1/me/campaigns"

CLIENT_UA = "Go-http-client/2.0"
DEFAULT_MAX_TOKENS = 32768

# 静态兜底：仅在上游模型接口不可达时使用（客户端名 → 上游 key）
STATIC_MODEL_KEYS = {
    "auto": "auto",
    "qwen3.8-max": "qmodel_38max",
    "qwen3.7-max": "qmodel_latest",
    "qwen3.7-plus": "qmodel",
    "qwen3.7-flash": "q37fmodel",
    "qwen3.6-flash": "q36fmodel",
    "deepseek-v4-pro": "dmodel",
    "deepseek-v4-flash": "dfmodel",
    "glm-5.3": "gmodel",
    "glm-5.2": "gm51model",
    "kimi-k2.7-code": "kmodel",
    "minimax-m2.7": "mmodel",
}

_PENDING = {"status": "pending"}


def normalize_model_name(s: str) -> str:
    """display_name → OpenAI 风格客户端名（小写、空格/下划线转连字符、保留点号）。"""
    s = (s or "").strip().lower()
    out: list[str] = []
    prev_dash = False
    for ch in s:
        if ch.isalnum() or ch == ".":
            out.append(ch)
            prev_dash = False
        elif ch in " _-":
            if not prev_dash and out:
                out.append("-")
                prev_dash = True
        else:
            out.append(ch)
            prev_dash = False
    return "".join(out).strip("-")


def _pkce() -> tuple[str, str]:
    import hashlib
    import secrets as _s
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    verifier = "".join(alphabet[b % len(alphabet)] for b in _s.token_bytes(64))
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


class QoderChannel(Channel):
    KIND = "qoder"
    DISPLAY_NAME = "Qoder"
    MODEL_PREFIX = "qoder/"

    def __init__(self):
        super().__init__()
        self._client = httpx.Client(
            timeout=httpx.Timeout(180.0, connect=15.0),
            # Qoder 网关对 HTTP/2 不友好（流式 INTERNAL_ERROR），强制 HTTP/1.1
            http2=False,
        )
        self._entries: dict[str, dict] = {}   # 上游 key → 模型条目（含档位）
        self._model_map: dict[str, str] = {}  # 客户端名 → 上游 key
        self._user_types: dict[str, str] = {}
        # 登录会话暂存（内存态；进程重启即失效，符合一次性登录语义）
        self._logins: dict[str, dict] = {}

    # -- 账号 ---------------------------------------------------------------

    def load_accounts(self) -> list[Account]:
        accounts = super().load_accounts()
        dirty = False
        for a in accounts:
            before = dict(a.extra)
            ensure_fingerprint(a)
            if a.extra != before:
                dirty = True
                try:
                    self.store.save(a)
                except Exception as e:
                    log(f"qoder 指纹落盘失败 {a.uid}: {e}")
        with self._lock:
            self._accounts = accounts
        return accounts

    # -- token 刷新 ---------------------------------------------------------

    def refresh_token(self, acc: Account) -> None:
        if not acc.refresh_token:
            raise RuntimeError("no drt- available")
        r = self._client.post(f"{OPENAPI_BASE}{EP_DT_REFRESH}",
                              json={"refresh_token": acc.refresh_token},
                              headers={"Content-Type": "application/json",
                                       "Accept": "application/json"})
        if r.status_code in (401, 403):
            raise RuntimeError(f"session dead (HTTP {r.status_code}) — 需重新登录")
        if r.status_code >= 400:
            raise RuntimeError(f"deviceToken refresh HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        dt = data.get("token") or data.get("device_token") or ""
        drt = data.get("refresh_token") or ""
        if not dt or not drt:
            raise RuntimeError("deviceToken refresh: incomplete token pair")
        acc.access_token = dt
        acc.refresh_token = drt
        expires_in = data.get("expires_in") or 0
        if expires_in:
            acc.expires_at = time.time() + float(expires_in) / 1000.0
        else:
            acc.expires_at = time.time() + 30 * 86400
        self.store.save(acc)
        log(f"qoder token 刷新成功 uid={acc.uid}")

    def ensure_token(self, acc: Account) -> None:
        if not acc.access_token:
            self.refresh_token(acc)
            return
        if acc.expired(skew=300):
            try:
                self.refresh_token(acc)
            except Exception as e:
                log(f"qoder token 刷新失败（沿用旧 token）: {e}")

    def _user_type(self, acc: Account) -> str:
        ut = self._user_types.get(acc.uid)
        if ut:
            return ut
        try:
            r = self._client.get(f"{OPENAPI_BASE}{EP_USERINFO}",
                                 headers=self._billing_headers(acc))
            if r.status_code < 400:
                info = r.json() or {}
                ut = str(info.get("userType") or "")
                name = info.get("name") or info.get("nickName") or ""
                if ut:
                    self._user_types[acc.uid] = ut
                if name and not acc.nickname:
                    acc.nickname = name
                    self.store.save(acc)
        except Exception as e:
            log(f"qoder userinfo 失败: {e}")
        return self._user_types.get(acc.uid, "personal_standard")

    # -- 模型 ---------------------------------------------------------------

    def fetch_models(self, acc: Account) -> list[ModelSpec]:
        url = f"{GATEWAY_BASE}{EP_MODELS}"
        sess = self._session(acc, url)
        headers = sess.apply_headers({}, "", url, accept="application/json")
        r = self._client.get(url, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"models api status {r.status_code}: {r.text[:200]}")
        data = r.json()
        entries = self._parse_scene_models(data)
        if not entries:
            raise RuntimeError("no enabled models in assistant/developer/chat scenes")
        # 更新映射表（客户端名 → key）与条目表（key → entry）
        self._model_map = {}
        self._entries = {}
        for e in entries:
            name = normalize_model_name(e.get("display_name", "")) or e.get("key", "")
            if name and e.get("key"):
                self._model_map[name] = e["key"]
                self._entries[e["key"]] = e
        specs = [self._to_spec(e) for e in entries]
        any_from_api = any(s.context_from_api for s in specs)
        if not any_from_api:
            log("qoder 模型接口未下发上下文档位，规格回退 max_input_tokens")
        return specs

    def _parse_scene_models(self, api_resp: dict) -> list[dict]:
        """只取上游**下发**的规格；assistant → developer → chat 三级回退。"""
        for scene in ("assistant", "developer", "chat"):
            raw = api_resp.get(scene)
            if not isinstance(raw, list):
                continue
            enabled = []
            for m in raw:
                if not isinstance(m, dict) or not m.get("enable") or not m.get("key"):
                    continue
                e = dict(m)
                e["_ctx_default"], e["_ctx_windows"] = _parse_context_config(
                    m.get("context_config"))
                enabled.append(e)
            if enabled:
                return enabled
        return []

    def _to_spec(self, e: dict) -> ModelSpec:
        name = normalize_model_name(e.get("display_name", "")) or e.get("key", "")
        max_in = int(e.get("max_input_tokens") or 0)
        ctx = 0
        windows = e.get("_ctx_windows") or []
        if windows:
            ctx = max(windows)          # 广告口径：取最大档（与 wild-work 一致）
        elif e.get("_ctx_default"):
            ctx = e["_ctx_default"]
        elif max_in > 0:
            ctx = max_in
        # 倍率：Qoder 上游下发 price_factor（实测 0.0=免费、0.1~1.4 递增）
        pf = e.get("price_factor")
        try:
            rate = float(pf) if pf is not None else None
        except (TypeError, ValueError):
            rate = None
        # 最大输出：上游未单列字段且无法推导，按 ModelSpec 原则留 0（未知），
        # 不用 32768 冒充真值；请求时的默认值仍由 buildAgentBody 模板负责。
        return ModelSpec(
            id=name,
            name=e.get("display_name") or name,
            context_window=ctx,
            max_output_tokens=0,
            supports_images=bool(e.get("is_vl")),
            supports_reasoning=bool(e.get("is_reasoning")),
            supports_tools=True,
            context_from_api=bool(windows or e.get("_ctx_default") or max_in),
            rate=rate,
            rate_note="免费" if rate == 0 else "",
            extra={"model_key": e.get("key"), "context_windows": windows,
                   "price_factor": pf},
        )

    def static_models(self) -> list[ModelSpec]:
        return [ModelSpec(id=k, name=k, context_window=180000,
                          max_output_tokens=0,
                          supports_tools=True, context_from_api=False)
                for k in sorted(STATIC_MODEL_KEYS)]

    # -- 对话 ---------------------------------------------------------------

    def _session(self, acc: Account, url: str) -> CosySession:
        ensure_fingerprint(acc)
        return CosySession(
            acc.extra.get("machineId", ""), acc.extra.get("machineToken", ""),
            acc.extra.get("machineType", ""), acc.nickname, acc.uid,
            acc.access_token, acc.refresh_token, self._user_type(acc),
        )

    def _billing_headers(self, acc: Account) -> dict:
        return {"Authorization": f"Bearer {acc.access_token}",
                "Accept": "application/json", "Content-Type": "application/json"}

    def chat_stream(self, acc: Account, body: dict, model: str, rid: str = "") -> tuple[int, bytes]:
        self.ensure_token(acc)
        # 模型映射表在 fetch_models 时才填充；若本进程还没拉过模型（如刚启动就来请求），
        # 先惰性拉一次，否则会退化成把客户端名当上游 key 直传，导致上游拒绝。
        if not self._model_map:
            try:
                self.fetch_models(acc)
            except Exception as e:
                log(f"qoder 模型表惰性加载失败（改用静态兜底）: {e}")
        model_key = self._model_map.get(model) or STATIC_MODEL_KEYS.get(model) or model
        entry = self._entries.get(model_key)

        messages = body.get("messages") or []
        tools = body.get("tools")
        enable_reasoning = bool(body.get("reasoning_effort")) or (
            isinstance(body.get("thinking"), dict) and body["thinking"].get("type") == "enabled")
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 0)
        ctx_hint = int(body.get("context_length") or body.get("context_window") or 0)
        context_window = _resolve_context_window(ctx_hint, entry)
        user_type = self._user_type(acc)

        raw_body = build_agent_body(messages, model_key, entry, tools, enable_reasoning,
                                    max_tokens, user_type, context_window)
        encoded = qoder_encode(raw_body)
        url = f"{GATEWAY_BASE}{EP_CHAT}"
        sess = self._session(acc, url)
        headers = sess.apply_headers({}, encoded, url, accept="text/event-stream",
                                     model_key=model_key)

        want_stream = bool(body.get("stream"))
        chunks: list[dict] = []
        out: list[bytes] = []
        try:
            with self._client.stream("POST", url, headers=headers,
                                     content=encoded.encode()) as r:
                if r.status_code >= 400:
                    return r.status_code, r.read()
                for line in r.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    try:
                        env = json.loads(payload)
                    except Exception:
                        continue
                    inner = env.get("body") if isinstance(env, dict) else None
                    if not inner:
                        continue
                    if inner == "[DONE]":
                        break
                    try:
                        chunk = json.loads(inner)
                    except Exception:
                        continue
                    # 上游偶发下发 null / 非对象帧（心跳），跳过而非崩溃
                    if not isinstance(chunk, dict):
                        continue
                    chunk["model"] = model
                    if want_stream:
                        out.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    else:
                        chunks.append(chunk)
        except httpx.HTTPError as e:
            return 502, json.dumps({"error": {"message": f"qoder 网络错误: {e}",
                                              "type": "upstream_error"}}).encode()

        if want_stream:
            out.append(sse_done())
            return 200, b"".join(out)
        return 200, json.dumps(aggregate_chunks(chunks, model), ensure_ascii=False).encode()

    # -- 积分 ---------------------------------------------------------------

    def credits(self, acc: Account) -> dict:
        try:
            self.ensure_token(acc)
            r = self._client.get(f"{OPENAPI_BASE}{EP_QUOTA}", headers=self._billing_headers(acc))
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}"}
            q = r.json() or {}
            uq = q.get("userQuota") or {}
            aq = q.get("addOnQuota") or {}
            return {
                "total_remain": int(float(uq.get("remaining") or 0) + float(aq.get("remaining") or 0)),
                "total_size": int(float(uq.get("total") or 0) + float(aq.get("total") or 0)),
                "packages": [
                    {"name": "用户套餐", "remain": int(float(uq.get("remaining") or 0)),
                     "used": int(float(uq.get("used") or 0)), "size": int(float(uq.get("total") or 0)),
                     "unit": "credits", "cycle_end": ""},
                    *([{"name": "赠送额度", "remain": int(float(aq.get("remaining") or 0)),
                        "used": int(float(aq.get("used") or 0)), "size": int(float(aq.get("total") or 0)),
                        "unit": "credits", "cycle_end": ""}] if float(aq.get("total") or 0) > 0 else []),
                ],
            }
        except Exception as e:
            return {"error": str(e)}

    # -- 每日签到（campaigns 活动路径） ---------------------------------------

    def _checkin_headers(self, acc: Account) -> dict:
        """签到请求头：桌面端标识 cosy-clienttype=10（推理链路用 5，不可混用）。"""
        return {"Authorization": f"Bearer {acc.access_token}",
                "Accept": "application/json", "Accept-Language": "zh-CN",
                "User-Agent": "Qoder", "cosy-clienttype": "10"}

    def checkin(self, acc: Account) -> dict:
        """执行 Qoder 每日签到。返回 {ok, msg, amount?}。

        对齐 wild-work/internal/qodercn/checkin.go：
          GET  /sash/api/v1/me/campaigns                  找 CLAIMABLE 的 CLAIM_BENEFIT
          POST /sash/api/v1/me/campaigns/{id}/claim       领取（空 body）
        幂等：409 / CLAIMED / replayed 视为「今日已领取」（成功语义）。
        legacy daily-check-in 已全局 DISABLED，不再走该路径（否则恒 409 假成功）。
        """
        try:
            self.ensure_token(acc)
        except Exception as e:
            return {"ok": False, "msg": f"token 不可用: {e}"}
        h = self._checkin_headers(acc)
        try:
            r = self._client.get(f"{OPENAPI_BASE}{EP_CAMPAIGNS}", headers=h)
            if r.status_code == 401:
                return {"ok": False, "msg": "登录态失效（401），需重新登录"}
            if r.status_code >= 400:
                return {"ok": False, "msg": f"活动查询失败 HTTP {r.status_code}"}
            camps = ((r.json() or {}).get("campaigns") or [])
        except Exception as e:
            return {"ok": False, "msg": f"活动查询异常: {e}"}
        target_id, already = "", False
        for cp in camps:
            if not isinstance(cp, dict) or cp.get("actionType") != "CLAIM_BENEFIT":
                continue
            st = str(cp.get("claimStatus") or "")
            if st == "CLAIMABLE":
                target_id = str(cp.get("campaignId") or "")
            elif st == "CLAIMED":
                already = True
        if not target_id:
            if already:
                return {"ok": True, "msg": "今日已领取"}
            return {"ok": False, "msg": "无可用签到活动（活动未开始或已结束）"}
        try:
            r2 = self._client.post(f"{OPENAPI_BASE}{EP_CAMPAIGNS}/{target_id}/claim",
                                   headers=h, json={})
            if r2.status_code == 409:
                return {"ok": True, "msg": "今日已领取"}
            if r2.status_code == 401:
                return {"ok": False, "msg": "登录态失效（401），需重新登录"}
            if r2.status_code >= 400:
                return {"ok": False, "msg": f"领取失败 HTTP {r2.status_code}"}
            res = r2.json() or {}
        except Exception as e:
            return {"ok": False, "msg": f"领取异常: {e}"}
        if str(res.get("status") or "") != "CLAIMED":
            return {"ok": False, "msg": f"未知状态 {res.get('status')}"}
        if res.get("replayed"):
            return {"ok": True, "msg": "今日已领取"}
        amount = int(((res.get("benefit") or {}).get("amount")) or 0)
        return {"ok": True, "msg": f"签到成功 +{amount}", "amount": amount}

    # -- 登录（OAuth 设备授权流：只出链接，由用户自行授权） -------------------

    def login_start(self) -> dict:
        verifier, challenge = _pkce()
        nonce = f"{int(time.time()*1000)}{new_uuid4().replace('-', '')[:8]}"
        sid = new_uuid4()
        url = (f"{OAUTH_WEBSITE}/device/selectAccounts?nonce={nonce}"
               f"&challenge={challenge}&challenge_method=S256&client_id={OAUTH_CLIENT_ID}")
        self._logins[sid] = {"verifier": verifier, "nonce": nonce, "at": time.time()}
        return {"url": url, "session": sid,
                "hint": "在浏览器打开该链接并完成登录授权，然后回到本页面点「我已授权」"}

    def login_poll(self, session: str) -> dict:
        st = self._logins.get(session)
        if not st:
            return {"status": "error", "message": "登录会话不存在或已过期，请重新发起"}
        url = (f"{OPENAPI_BASE}/api/v1/deviceToken/poll?nonce={st['nonce']}"
               f"&verifier={st['verifier']}&challenge_method=S256")
        try:
            r = self._client.get(url, headers={"User-Agent": CLIENT_UA, "Accept": "application/json"})
        except Exception as e:
            return {"status": "pending", "message": f"网络异常，继续等待：{e}"}
        if r.status_code in (404, 202):
            return {"status": "pending", "message": "尚未完成授权…"}
        if r.status_code >= 400:
            return {"status": "error", "message": f"HTTP {r.status_code}: {r.text[:200]}"}
        tok = r.json() or {}
        dt = tok.get("token") or tok.get("device_token") or ""
        if not dt:
            return {"status": "pending", "message": "尚未完成授权…"}
        uid = str(tok.get("user_id") or tok.get("uid") or "")
        drt = tok.get("refresh_token") or ""
        expires_in = float(tok.get("expires_in") or 0)
        acc = Account(kind=self.KIND, uid=uid or new_uuid4(),
                      access_token=dt, refresh_token=drt,
                      expires_at=(time.time() + expires_in / 1000.0) if expires_in
                      else (time.time() + 30 * 86400),
                      domain="qoder.com.cn")
        ensure_fingerprint(acc)
        self.save_account(acc)
        self._logins.pop(session, None)
        # 拉一次 userinfo 补昵称与 userType
        try:
            name = self._user_type(acc)
            r2 = self._client.get(f"{OPENAPI_BASE}{EP_USERINFO}", headers=self._billing_headers(acc))
            if r2.status_code < 400:
                info = r2.json() or {}
                acc.nickname = info.get("name") or info.get("nickName") or acc.nickname
                self.save_account(acc)
        except Exception:
            pass
        return {"status": "ok", "message": f"登录成功：{acc.nickname or acc.uid}", "uid": acc.uid}


# ---------------------------------------------------------------------------
# 请求体构造（对齐 qoder2api 的 baseprompt 模板）
# ---------------------------------------------------------------------------

def _parse_context_config(cc) -> tuple[int, list[int]]:
    """解析 context_config → (默认档, 全部档位升序)。形状不符时返回 (0, [])。"""
    if not isinstance(cc, dict):
        return 0, []
    default = 0
    windows: set[int] = set()
    for cfg in cc.values():
        if not isinstance(cfg, dict):
            continue
        tc = int(cfg.get("token_count") or 0)
        if tc <= 0:
            continue
        windows.add(tc)
        if cfg.get("is_default"):
            default = min(default, tc) if default else tc
    return default, sorted(windows)


def _resolve_context_window(requested: int, entry: Optional[dict]) -> int:
    """上下文档位：客户端显式值（校验档位）→ 最大档 → 默认档 → max_input_tokens。"""
    if requested > 0:
        if not entry:
            return requested
        windows = entry.get("_ctx_windows") or []
        if windows:
            if requested in windows:
                return requested
        else:
            max_in = int(entry.get("max_input_tokens") or 0)
            if max_in <= 0 or requested <= max_in:
                return requested
    if not entry:
        return 0
    windows = entry.get("_ctx_windows") or []
    if windows:
        return windows[-1]
    if entry.get("_ctx_default"):
        return int(entry["_ctx_default"])
    return int(entry.get("max_input_tokens") or 0)


def _last_user_prompt(messages: list) -> str:
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str) and c:
            return c
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                    return str(blk["text"])
    return ""


def _model_config(entry: Optional[dict], key: str, enable_reasoning: bool) -> dict:
    if not entry or not entry.get("key"):
        return {"key": key or "auto", "display_name": "Auto", "model": "", "format": "openai",
                "is_vl": False, "is_reasoning": enable_reasoning, "api_key": "", "url": "",
                "source": "system", "max_input_tokens": 180000}
    return {
        "key": entry["key"],
        "display_name": entry.get("display_name") or entry["key"],
        "model": "",
        "format": entry.get("format") or "openai",
        "is_vl": bool(entry.get("is_vl")),
        "is_reasoning": enable_reasoning,
        "api_key": "",
        "url": "",
        "source": entry.get("source") or "system",
        "max_input_tokens": int(entry.get("max_input_tokens") or 180000),
    }


def _normalize_qoder_messages(msgs: list) -> list:
    """qoder 网关对 messages 的校验比 OpenAI 标准严（2026-09-29 重放实测），两处不合规即整单被拒：
    1. 一条 assistant 带多个 tool_calls（并行工具调用），报
       "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'"；
    2. assistant 的 content 为 null（纯工具调用无文本）。
    规范化：content=None → ""；多 tool_calls 拆成 [assistant(单call), tool, assistant(单call), tool, ...]。
    """
    out: list = []
    pending: list = []  # 尚未与其 tool 结果重新配对的 tool_calls
    for m in msgs:
        if not isinstance(m, dict):
            out.append(m)
            continue
        cp = dict(m)
        if cp.get("role") == "assistant" and cp.get("content") is None:
            cp["content"] = ""
        tcs = cp.get("tool_calls") if cp.get("role") == "assistant" else None
        if isinstance(tcs, list) and len(tcs) > 1:
            head = dict(cp)
            head["tool_calls"] = [tcs[0]]
            out.append(head)
            pending = list(tcs[1:])
            continue
        if cp.get("role") == "tool" and pending:
            for i, tc in enumerate(pending):
                if isinstance(tc, dict) and tc.get("id") == cp.get("tool_call_id"):
                    out.append({"role": "assistant", "content": "", "tool_calls": [tc]})
                    pending.pop(i)
                    break
        out.append(cp)
    return out


def build_agent_body(messages: list, model_key: str, entry: Optional[dict], tools,
                     enable_reasoning: bool, max_tokens: int, user_type: str,
                     context_window: int) -> bytes:
    """构造 agent_chat_generation 请求体。developer 角色必须改写为 system。"""
    msgs = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        cp = dict(m)
        if cp.get("role") == "developer":
            cp["role"] = "system"
        msgs.append(cp)
    msgs = _normalize_qoder_messages(msgs)

    prompt = _last_user_prompt(msgs)
    if max_tokens <= 0:
        max_tokens = DEFAULT_MAX_TOKENS
    if not user_type:
        user_type = "personal_standard"
    model_cfg = _model_config(entry, model_key, enable_reasoning)

    params: dict = {"max_tokens": max_tokens}
    if context_window > 0:
        params["context_length"] = context_window
        model_cfg["max_input_tokens"] = context_window

    now_ms = int(time.time() * 1000)
    rid = new_uuid4()
    base = {
        "request_id": rid,
        "chat_record_id": rid,
        "request_set_id": new_uuid4(),
        "session_id": new_uuid4(),
        "stream": True,
        "aliyun_user_type": user_type,
        "agent_id": "agent_common",
        "chat_task": "FREE_INPUT",
        "is_reply": True,
        "is_retry": False,
        "code_language": "",
        "source": 1,
        "version": "3",
        "chat_prompt": "",
        "task_id": "common",
        "parameters": params,
        "session_type": "qoder",
        "model_config": model_cfg,
        "chat_context": {
            "chatPrompt": "",
            "text": {"type": "text", "text": prompt},
            "extra": {
                "context": [],
                "modelConfig": {"key": model_cfg["key"], "is_reasoning": model_cfg["is_reasoning"]},
                "originalContent": {"type": "text", "text": prompt},
            },
            "features": [],
            "imageUrls": None,
        },
        "messages": msgs,
        "business": {"id": new_uuid4(), "begin_at": now_ms, "name": prompt[:30]},
    }
    if tools:
        base["tools"] = tools
    return json.dumps(base, ensure_ascii=False).encode()


register(QoderChannel())
