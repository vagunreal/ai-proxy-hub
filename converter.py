#!/usr/bin/env python3
"""
codebuddy2openai — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传（含 Anthropic / Chat / Responses 三种协议）。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
import uvicorn

try:
    from core.desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
    def desensitize_body(body, roles=("system",), desensitize_harness_user=False,
                         desensitize_tools=False, compact_harness=False,
                         strip_tool_metadata=False):
        return body

from core.responses_adapter import (
    responses_request_to_chat,
    ResponsesStreamConverter,
)
from core.responses_projection import project_responses_chat_body
from core.anthropic_adapter import (
    anthropic_request_to_chat,
    AnthropicStreamConverter,
)

try:
    from core.usage_stats import UsageStats
except Exception:  # 统计为旁路能力，缺失时降级为不统计
    UsageStats = None  # type: ignore

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
USER_AGENT = "codebuddy2openai/2.0"

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------

def auth_dirs() -> list[Path]:
    """收集所有可能的凭据目录（多账号池支持：全部扫描，按 uid 去重）。

    优先级：CODEBUDDY_AUTH_DIR > 平台默认目录 > WSL2 下挂载的 Windows 宿主目录。
    """
    dirs: list[Path] = []
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    if env_dir:
        dirs.append(Path(env_dir))
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        dirs.append(home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    elif plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        dirs.append(local / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    else:
        xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        dirs.append(xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth")
        # WSL2：探测 Windows 宿主机所有用户的凭据目录
        for p in Path("/mnt/c/Users").glob("*/AppData/Local/CodeBuddyExtension/Data/Public/auth"):
            dirs.append(p)
    # 去重目录路径本身，保持顺序
    seen: set[str] = set()
    uniq: list[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def _read_uid(path: Path) -> str | None:
    """读取凭据文件的 account.uid；文件不可读/格式坏返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data.get("account") or {}).get("uid") or ""
    except Exception:
        return None


def find_auth_files() -> list[Path]:
    """收集所有账号凭据文件，按 uid 去重（同一账号在多个目录只保留第一份）。

    无法解析的文件跳过（启动预检会给出警告）。
    """
    seen_uids: set[str] = set()
    result: list[Path] = []
    for d in auth_dirs():
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.info")):
            uid = _read_uid(f)
            if uid is None:
                sys.stderr.write(f"[warn] 跳过无法解析的凭据文件: {f}\n")
                continue
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            result.append(f)
    return result


def find_auth_file() -> Path | None:
    files = find_auth_files()
    return files[0] if files else None


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------

class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": USER_AGENT,
        }
        return h

    def get_headers(self) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }


# ---------------------------------------------------------------------------
# 账号池：多账号粘性使用 + 额度/认证失败自动切换
# ---------------------------------------------------------------------------

# 触发切换的后端 HTTP 状态码：限流/额度用尽、token 失效、账号不可用
FAILOVER_STATUS_CODES = {401, 402, 403, 429}
# 账号失败后的冷却时间（秒）：期间排到候选队尾，仅当无健康账号时才硬试
FAIL_COOLDOWN_SECS = 1800
# 当前选定账号的持久化文件：重启后按 uid 恢复（否则会回落到第一个账号）
CURRENT_ACCOUNT_FILE = Path(__file__).parent / "current_account.json"


def _is_failover_error(status: int, raw: bytes | str = "") -> bool:
    """判断后端响应是否应当切换账号重试：状态码命中，或错误文本含额度/账号类关键词。"""
    if status in FAILOVER_STATUS_CODES:
        return True
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    text = (text or "").lower()
    quota_keywords = ("额度", "余额", "积分不足", "配额", "配額",
                      "quota", "insufficient", "exceeded", "限流", "频率")
    return any(k in text for k in quota_keywords)


class CredentialPool:
    """多账号凭据池。

    调度策略（粘性主账号 + 故障切换）：
      - 正常时一直使用当前账号（candidates() 把它排在最前）；
      - 某账号请求遇到额度/认证类错误（_is_failover_error）后进入冷却
        （FAIL_COOLDOWN_SECS），后续请求自动落到下一个健康账号；
      - 成功响应会"粘住"该账号，直到它再次失败；
      - 所有账号都在冷却时，仍会按顺序硬试（额度可能已恢复），失败则重新冷却。
    """

    def __init__(self, paths: list[Path]):
        self.creds: list[CredentialManager] = []
        for p in paths:
            try:
                self.creds.append(CredentialManager(p))
            except Exception as e:
                sys.stderr.write(f"[warn] 加载凭据失败 {p}: {e}\n")
        self._lock = threading.Lock()
        self._current = 0
        self._failed_until: dict[int, float] = {}   # index -> 失败冷却截止时间戳
        # 恢复上次手动选定的账号（按 uid 持久化，避免重启后回落到第一个账号）
        saved = self._load_saved_uid()
        if saved:
            for i, c in enumerate(self.creds):
                try:
                    if c.summary().get("uid") == saved:
                        self._current = i
                        break
                except Exception:
                    continue

    # -- 当前账号持久化 -----------------------------------------------------

    def _load_saved_uid(self) -> str:
        try:
            return json.loads(CURRENT_ACCOUNT_FILE.read_text(encoding="utf-8")).get("uid") or ""
        except Exception:
            return ""

    def _save_uid(self, uid: str) -> None:
        try:
            tmp = CURRENT_ACCOUNT_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"uid": uid}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, CURRENT_ACCOUNT_FILE)
        except Exception as e:
            sys.stderr.write(f"[warn] 当前账号落盘失败: {e}\n")

    def _uid_of(self, idx: int) -> str:
        try:
            return str(self.creds[idx].summary().get("uid") or "")
        except Exception:
            return ""

    def __len__(self) -> int:
        return len(self.creds)

    def _in_cooldown(self, idx: int) -> bool:
        return time.time() < self._failed_until.get(idx, 0.0)

    def candidates(self) -> list[tuple[int, CredentialManager]]:
        """按切换顺序返回候选账号：当前(健康) → 其他健康 → 冷却中兜底。"""
        with self._lock:
            healthy = [i for i in range(len(self.creds)) if not self._in_cooldown(i)]
            cooling = [i for i in range(len(self.creds)) if self._in_cooldown(i)]
            if self._current in healthy:
                healthy.remove(self._current)
            order = [self._current] + healthy + cooling
            # 去重保序（current 已冷却时会同时出现在队首和 cooling 里）
            seen: set[int] = set()
            order = [i for i in order if not (i in seen or seen.add(i))]
            return [(i, self.creds[i]) for i in order]

    def get_current(self) -> CredentialManager | None:
        with self._lock:
            return self.creds[self._current] if self.creds else None

    def set_current(self, uid: str) -> bool:
        """手动切换当前账号（按 uid 或凭据文件名匹配）；清除其冷却并粘住。"""
        with self._lock:
            for i, c in enumerate(self.creds):
                try:
                    u = c.summary().get("uid")
                except Exception:
                    u = None
                if u == uid or c.path.name == uid:
                    self._current = i
                    self._failed_until.pop(i, None)
                    self._save_uid(str(u or uid))
                    return True
            return False

    def report_success(self, cred: CredentialManager):
        """粘住成功账号；清除其冷却状态。"""
        with self._lock:
            for i, c in enumerate(self.creds):
                if c is cred:
                    self._current = i
                    self._failed_until.pop(i, None)
                    break

    def report_failure(self, cred: CredentialManager, status: int, raw: bytes | str = ""):
        """账号失败：进入冷却，并把当前账号移到下一个健康账号（若无则保持）。"""
        with self._lock:
            idx = next((i for i, c in enumerate(self.creds) if c is cred), None)
            if idx is None:
                return
            self._failed_until[idx] = time.time() + FAIL_COOLDOWN_SECS
            healthy = [i for i in range(len(self.creds)) if not self._in_cooldown(i)]
            if healthy and healthy[0] != self._current:
                self._current = healthy[0]
                self._save_uid(self._uid_of(self._current))

    def snapshot(self) -> list[dict]:
        """所有账号状态（health 端点用）。"""
        out = []
        with self._lock:
            for i, c in enumerate(self.creds):
                try:
                    info = c.summary()
                except Exception as e:
                    info = {"error": str(e), "path": str(c.path)}
                info["current"] = (i == self._current)
                info["cooldown_remaining"] = max(0, int(self._failed_until.get(i, 0) - time.time()))
                out.append(info)
        return out


# ---------------------------------------------------------------------------
# 模型注册表：上游发现 + 内置 + 自定义 + 探测
# ---------------------------------------------------------------------------

UPSTREAM_MODELS_URL = "https://copilot.tencent.com/console/enterprises/personal/models"
MODELS_FILE = Path(__file__).parent / "models_registry.json"
_models_lock = threading.RLock()
_upstream_models_cache: dict = {"t": 0.0, "details": {}, "agent_only": []}  # 60s TTL
_probe_state: dict = {"running": False, "done": 0, "total": 0, "current": ""}


def _load_registry() -> dict:
    try:
        return json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"custom": [], "disabled": [], "probe": {}}


def _save_registry(reg: dict):
    """写盘;不加锁——调用方负责持有 _models_lock(RLock,可重入)。"""
    tmp = MODELS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, MODELS_FILE)


def _fetch_upstream_models(cred: CredentialManager) -> tuple[dict, list[str]]:
    """从上游拉取模型详情（60s TTL）。

    返回 (details, agent_only)：
      details    — {模型id: 官方详情}，来自 data.models（含 maxInputTokens/maxOutputTokens/
                   credits 倍率/中文描述/多模态/免费标签等）
      agent_only — 仅出现在 agents 配置里、无详情的模型 id（如内部辅助模型 lite）
    """
    with _models_lock:
        if time.time() - _upstream_models_cache["t"] < 60:
            return _upstream_models_cache["details"], _upstream_models_cache["agent_only"]
    details: dict = {}
    agent_models: list[str] = []
    try:
        headers = cred.get_headers()
        with httpx.Client(timeout=15) as c:
            r = c.get(UPSTREAM_MODELS_URL, headers=headers)
        data = r.json()
        if data.get("code") == 0:
            dd = data.get("data") or {}
            for m in dd.get("models") or []:
                mid = m.get("id")
                if mid:
                    details[mid] = m
            for agent in dd.get("agents") or []:
                for m in agent.get("models") or []:
                    if m not in agent_models:
                        agent_models.append(m)
    except Exception:
        pass
    agent_only = [m for m in agent_models if m not in details]
    with _models_lock:
        _upstream_models_cache.update({"t": time.time(), "details": details, "agent_only": agent_only})
    return details, agent_only


def _specs_from_upstream(info: dict) -> dict:
    """把上游模型详情转换成面板规格(用户编辑前的基础值)。"""
    inp = ["text"]
    if info.get("supportsImages") and not info.get("disabledMultimodal"):
        inp.append("image")
    return {
        "context_length": int(info.get("maxInputTokens") or info.get("maxAllowedSize") or 0) or 131072,
        "max_output_tokens": int(info.get("maxOutputTokens") or 0) or 8192,
        "input": inp,
        "output": ["text"],
    }


def _default_specs() -> dict:
    """客户端接入所需的模型规格默认值(面板可逐模型编辑)。"""
    return {"context_length": 131072, "max_output_tokens": 8192,
            "input": ["text"], "output": ["text"]}


def _model_specs(reg: dict, name: str) -> dict:
    merged = _default_specs()
    merged.update((reg.get("specs") or {}).get(name) or {})
    return merged


def _all_models(cred: CredentialManager | None) -> list[dict]:
    """合并上游/内置/自定义模型，标注来源与规格，过滤禁用项。顺序：上游 → 内置 → 自定义。"""
    reg = _load_registry()
    disabled = set(reg.get("disabled") or [])
    out: list[dict] = []
    seen: set[str] = set()

    def _merged(name: str, upstream_info: dict | None) -> dict:
        # 优先级:上游官方规格 < 用户面板编辑(registry.specs)
        base = _specs_from_upstream(upstream_info) if upstream_info else _default_specs()
        merged = {**base, **((reg.get("specs") or {}).get(name) or {})}
        meta = {}
        if upstream_info:
            meta = {
                "display_name": upstream_info.get("name"),
                "description": upstream_info.get("descriptionZh") or upstream_info.get("descriptionEn"),
                "credits": upstream_info.get("credits"),
                "tags": [t for t in (upstream_info.get("tags") or []) if str(t).startswith("badge:")]
                        or None,
                "supports_reasoning": upstream_info.get("supportsReasoning"),
                "supports_tool_call": upstream_info.get("supportsToolCall"),
            }
        return merged, meta

    details, agent_only = _fetch_upstream_models(cred) if cred is not None else ({}, [])
    for mid in details:
        if mid in disabled or mid in seen:
            continue
        seen.add(mid)
        specs, meta = _merged(mid, details[mid])
        out.append({"name": mid, "source": "上游", "specs": specs, "meta": meta})
    for m in agent_only:
        if m in disabled or m in seen:
            continue
        seen.add(m)
        specs, meta = _merged(m, None)
        out.append({"name": m, "source": "上游", "specs": specs, "meta": meta})
    for m in DEFAULT_MODELS:
        if m in disabled or m in seen:
            continue
        seen.add(m)
        specs, meta = _merged(m, details.get(m))
        out.append({"name": m, "source": "内置", "specs": specs, "meta": meta})
    for c in reg.get("custom") or []:
        if c["name"] in disabled or c["name"] in seen:
            continue
        seen.add(c["name"])
        specs, meta = _merged(c["name"], details.get(c["name"]))
        out.append({"name": c["name"], "source": "自定义", "specs": specs, "meta": meta})
    return out


def _resolve_model(raw: str) -> str:
    """客户端模型名 → 上游真实模型名：内置别名 → 自定义别名 → 原样透传。"""
    reg = _load_registry()
    for c in reg.get("custom") or []:
        if c.get("alias") and c["alias"] == raw:
            return c["name"]
    return MODEL_ALIASES.get(raw, raw)


def probe_model(cred: CredentialManager, model: str) -> dict:
    """对单个模型发一次最小真实请求，测可用性/首字延迟/总延迟。"""
    reg = _load_registry()
    headers = cred.get_headers()
    body = {"model": model, "messages": [{"role": "user", "content": "只回复两个字:正常"}],
            "stream": True, "max_tokens": 16}
    result: dict = {"ok": False, "error": None, "ttfb_ms": None, "total_ms": None,
                    "resp_model": None, "finish_reason": None, "reply": "", "tested_at": int(time.time())}
    t0 = time.time()
    try:
        with httpx.Client(timeout=90) as c:
            with c.stream("POST", f"{BACKEND}/v2/chat/completions", headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = r.read()
                    result["error"] = f"HTTP {r.status_code}: {_truncate(raw.decode('utf-8','replace'), 200)}"
                else:
                    got_first = False
                    finish = None
                    parts: list[str] = []
                    resp_model = None
                    for line in r.iter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            obj = json.loads(payload)
                        except Exception:
                            continue
                        resp_model = obj.get("model") or resp_model
                        for ch in obj.get("choices") or []:
                            if ch.get("finish_reason"):
                                finish = ch["finish_reason"]
                            content = (ch.get("delta") or {}).get("content")
                            if content:
                                parts.append(content)
                                if not got_first:
                                    got_first = True
                                    result["ttfb_ms"] = int((time.time() - t0) * 1000)
                    result.update({"ok": True, "total_ms": int((time.time() - t0) * 1000),
                                   "resp_model": resp_model, "finish_reason": finish,
                                   "reply": "".join(parts)[:40]})
    except Exception as e:
        result["error"] = str(e)
    result["elapsed_hint"] = f"{result['ttfb_ms'] or '-'}ms 首字 / {result['total_ms'] or '-'}ms 总计" if result["ok"] else None
    with _models_lock:
        reg.setdefault("probe", {})[model] = result
        _save_registry(reg)
    return result


def _probe_all_worker():
    cred = (CONFIG["pool"] or CredentialPool([])).get_current()
    if cred is None:
        _probe_state.update({"running": False})
        return
    models = [m["name"] for m in _all_models(cred)]
    _probe_state.update({"running": True, "done": 0, "total": len(models), "current": ""})
    for m in models:
        if not _probe_state.get("running"):
            break
        _probe_state["current"] = m
        try:
            probe_model(cred, m)
        except Exception:
            pass
        _probe_state["done"] += 1
    _probe_state.update({"running": False, "current": ""})


def _api_info() -> dict:
    host = CONFIG.get("host") or "127.0.0.1"
    keys = [k for k in _load_keys() if not k.get("disabled")]
    master = CONFIG.get("api_key") or ""
    return {
        "base_url": f"http://{host}:{CONFIG.get('port', 8787)}/v1",
        "api_key": master or (keys[0]["key"] if keys else ""),
        "auth_enabled": bool(master) or bool(keys),
        "keys_count": len(keys),
    }


PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>聚合控制台</title>
<style>
  :root { --green:#22c55e; --orange:#f59e0b; --red:#ef4444; --blue:#3b82f6;
          --bg:#f5f6f8; --card:#ffffff; --text:#1f2937; --muted:#6b7280; --line:#e5e7eb; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text);
         font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; padding:32px 24px; }
  .wrap { max-width:960px; margin:0 auto; }
  h1 { font-size:26px; font-weight:700; margin-bottom:4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .sub b { color:var(--green); }
  .stats { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:12px 18px; min-width:150px; }
  .stat .v { font-size:22px; font-weight:700; }
  .stat .k { font-size:12px; color:var(--muted); margin-top:2px; }
  .stat.hl { border-color:#ddd6fe; background:#faf5ff; }
  .stat.hl .v { color:#7c3aed; }
  /* 按渠道的积分卡片：渠道色点 + 稍紧凑 */
  .stat.cred-ch { min-width:132px; padding:12px 16px; }
  .stat.cred-ch .v { color:#7c3aed; }
  .chdot { display:inline-block; width:8px; height:8px; border-radius:50%;
           margin-right:5px; vertical-align:middle; }
  /* 渠道分组（渠道为一级，模型可展开为二级） */
  .ch-group { border:1px solid var(--line); border-radius:12px; margin-bottom:10px;
              overflow:hidden; background:#fff; }
  .ch-group:last-child { margin-bottom:0; }
  .ch-head { display:flex; align-items:center; gap:9px; padding:10px 12px;
             background:#f8fafc; cursor:pointer; user-select:none; }
  .ch-head:hover { background:#f1f5f9; }
  .ch-head .ch-name { font-size:14px; font-weight:700; display:flex; align-items:center; }
  .ch-head .ch-cnt { font-size:11px; color:var(--muted); background:#e5e7eb;
                     border-radius:99px; padding:2px 8px; }
  .ch-head .ch-metrics { margin-left:auto; display:flex; gap:14px; font-size:12px;
                         font-variant-numeric:tabular-nums; }
  .ch-head .ch-metrics .m { color:var(--muted); }
  .ch-head .ch-metrics .m b { color:var(--text); font-weight:600; }
  .ch-head .ch-metrics .m.cr b { color:#7c3aed; }
  .ch-zoom { width:24px; height:24px; border:1px solid var(--line); border-radius:6px;
             background:#fff; color:#6b7280; cursor:pointer; font-size:13px;
             display:inline-flex; align-items:center; justify-content:center;
             flex-shrink:0; line-height:1; }
  .ch-zoom:hover { color:#4338ca; border-color:#4338ca; background:#eef2ff; }
  /* 渠道拖动手柄与拖动状态 */
  .ch-grip { color:#cbd5e1; cursor:grab; font-size:13px; line-height:1; user-select:none;
             padding:2px 3px; border-radius:4px; flex-shrink:0; }
  .ch-grip:hover { color:#64748b; background:#e2e8f0; }
  .ch-grip:active { cursor:grabbing; }
  .ch-group.dragging { opacity:.45; }
  .ch-group.drag-over { border-color:#4338ca; box-shadow:0 0 0 2px #c7d2fe; }
  .ch-body { padding:2px 0 6px; }
  .ch-body .trow.sub { padding-left:26px; }
  .ch-body .trow.sub .tn { font-weight:500; }
  .ch-empty { font-size:12px; color:var(--muted); padding:8px 12px; }
  .pg-info { font-size:12px; color:var(--muted); min-width:96px; text-align:center; }
  /* 日志卡片工具栏：不换行、按需收缩 */
  .chead.nowrap { flex-wrap:nowrap; }
  .chead.nowrap .note { min-width:0; overflow:hidden; text-overflow:ellipsis;
                        white-space:nowrap; }
  .log-tools { display:flex; align-items:center; gap:8px; flex-wrap:nowrap;
               margin-left:auto; flex-shrink:0; }
  .log-tools select, .log-tools input { font-size:12px; padding:5px 8px;
               border:1px solid var(--line); border-radius:8px; background:#fff; }
  .log-tools select { width:120px; }
  .log-tools input { width:150px; }
  @media (max-width: 900px) { .log-tools { flex-wrap:wrap; margin-left:0; } }
  .tth.cr { color:#7c3aed; font-weight:700; }
  .crbig { color:#7c3aed; font-weight:700; }
  /* 折线图 */
  svg.chart { width:100%; height:auto; display:block; }
  .cax { font-size:10px; fill:#9ca3af; font-variant-numeric:tabular-nums; }
  .legend { display:flex; gap:16px; flex-wrap:wrap; margin-top:10px; padding-top:10px;
            border-top:1px solid #f3f4f6; }
  .lg { display:inline-flex; align-items:center; gap:6px; font-size:12.5px; }
  .lg i { width:11px; height:11px; border-radius:3px; display:inline-block; }
  .lg b { font-variant-numeric:tabular-nums; }
  /* 每日明细的分组行 */
  .trow.dayhead { background:#f8fafc; border-top:1px solid #e5e7eb; font-weight:600; }
  .trow.sub { padding-left:22px; font-size:12px; color:#4b5563; }
  .trow.sub .tn { font-weight:500; }
  /* ---- 账号管理（对齐 wild-work 卡片式设计）---- */
  .acct-head { display:flex; align-items:flex-start; justify-content:space-between;
               gap:12px; margin-bottom:12px; flex-wrap:wrap; }
  .pa-left { display:flex; gap:8px; }
  .pa-right { display:grid; grid-template-columns:repeat(3, auto); gap:8px; }
  .btn { display:inline-flex; align-items:center; justify-content:center;
         border:1px solid var(--line); background:#fff; color:var(--text);
         border-radius:8px; padding:8px 14px; font-size:13px; font-weight:600;
         cursor:pointer; transition:all .15s; white-space:nowrap; }
  .btn:hover { border-color:#94a3b8; background:#f8fafc; }
  .btn.primary { background:#4338ca; border-color:#4338ca; color:#fff; }
  .btn.primary:hover { background:#3730a3; border-color:#3730a3; }
  .acct-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  @media (max-width: 900px) { .acct-grid { grid-template-columns:1fr; } }
  /* ---- 账号长条卡（主页面与账号管理合并视图，可拖动排序）---- */
  .strip-list { display:flex; flex-direction:column; gap:10px; }
  .strip-card { display:grid; grid-template-columns:minmax(240px,280px) 1fr; gap:8px 18px;
                border:1px solid var(--line); border-radius:12px; padding:10px 14px 12px;
                background:#fff; }
  .strip-card.disabled { opacity:.55; background:#f9fafb; }
  .strip-card.dragging { opacity:.4; }
  .strip-card.drag-over { border-color:#4338ca; box-shadow:0 0 0 2px #c7d2fe; }
  .strip-card .sc-grip { grid-column:1 / -1; height:12px; margin:-2px 0 2px;
              display:flex; align-items:center; justify-content:center;
              color:#cbd5e1; cursor:grab; font-size:14px; letter-spacing:4px;
              user-select:none; border-radius:6px; }
  .strip-card .sc-grip:hover { color:#94a3b8; background:#f8fafc; }
  .strip-card .sc-grip:active { cursor:grabbing; }
  .strip-card .sc-side { display:flex; flex-direction:column; gap:8px; min-width:0; }
  .strip-card .sc-main { min-width:0; display:flex; flex-direction:column; gap:8px;
              justify-content:center; }
  .strip-card .acct-top { display:flex; align-items:flex-start; justify-content:space-between;
                         gap:6px; }
  .strip-card .acct-name { font-weight:600; font-size:14px; margin-left:7px; }
  .strip-card .acct-uid { font-size:11px; color:var(--muted);
                         font-family:ui-monospace,monospace; word-break:break-all; }
  .strip-card .acct-mid { display:flex; align-items:center; justify-content:space-between;
                         gap:8px; flex-wrap:wrap; }
  .strip-card .acct-credits { font-size:18px; font-weight:700; white-space:nowrap; }
  .strip-card .acct-credits .credit-num { font-size:19px; font-weight:700; color:#0f766e; }
  .strip-card .acct-credits span { font-size:11px; color:var(--muted); font-weight:400;
                                  margin-left:2px; }
  .strip-card .acct-credits .credit-expiring { font-size:12px; font-weight:600; color:#b91c1c; }
  .strip-card .acct-credits .credit-na { font-size:13px; color:var(--muted); font-weight:500; }
  .strip-card .acct-ops { display:flex; gap:4px; justify-content:flex-end;
                         align-items:center; flex-wrap:wrap; }
  .strip-card .acct-checkin { font-size:12px; }
  /* 长条卡里的资源包合计行 */
  .pkg-sum { font-size:12.5px; color:var(--muted); }
  .pkg-sum b { font-size:16px; color:#0f766e; font-variant-numeric:tabular-nums; }
  .pkg-note { font-size:12.5px; color:var(--muted); }
  /* 「＋ 渠道」添加按钮：按渠道配色（对齐 wild-work） */
  .btn.ch-workbuddy { background:#4338ca; border-color:#4338ca; color:#fff; }
  .btn.ch-workbuddy:hover { background:#3730a3; border-color:#3730a3; }
  .btn.ch-qoder { background:#7c3aed; border-color:#7c3aed; color:#fff; }
  .btn.ch-qoder:hover { background:#6d28d9; border-color:#6d28d9; }
  .btn.ch-trae { background:#0891b2; border-color:#0891b2; color:#fff; }
  .btn.ch-trae:hover { background:#0e7490; border-color:#0e7490; }
  @media (max-width: 900px) { .strip-card { grid-template-columns:1fr; } }
  .acct-card { border:1px solid var(--line); border-radius:12px; padding:12px 14px;
               display:flex; flex-direction:column; gap:8px; background:#fff; }
  .acct-card.disabled { opacity:.5; background:#f9fafb; }
  .acct-card .acct-top { display:flex; align-items:flex-start; justify-content:space-between;
                         gap:6px; }
  .acct-card .acct-name { font-weight:600; font-size:14px; margin-left:7px; }
  .acct-card .acct-name.editable { cursor:pointer; border-bottom:1px dashed transparent; }
  .acct-card .acct-name.editable:hover { border-bottom-color:#94a3b8; }
  .acct-card .acct-uid { font-size:11px; color:var(--muted);
                         font-family:ui-monospace,monospace; }
  .acct-card .acct-mid { display:flex; align-items:center; justify-content:space-between;
                         gap:8px; }
  .acct-card .acct-credits { font-size:18px; font-weight:700; cursor:pointer; white-space:nowrap; }
  .acct-card .acct-credits .credit-num { font-size:18px; font-weight:700; }
  .acct-card .acct-credits span { font-size:11px; color:var(--muted); font-weight:400;
                                  margin-left:2px; }
  .acct-card .acct-credits .credit-expiring { font-size:12px; font-weight:600; color:#b91c1c; }
  .acct-card .acct-credits .credit-na { font-size:13px; color:var(--muted); font-weight:500; }
  .acct-card .acct-ops { display:flex; gap:4px; justify-content:flex-end; }
  .icon-op { display:inline-flex; align-items:center; justify-content:center;
             width:28px; height:28px; border:1px solid var(--line); border-radius:6px;
             background:#fff; color:var(--muted); cursor:pointer; font-size:14px;
             transition:all .15s; margin:0 1px; user-select:none; }
  .icon-op:hover { color:#4338ca; border-color:#4338ca; background:#eef2ff; }
  .icon-op.danger:hover { color:#dc2626; border-color:#dc2626; background:#fef2f2; }
  .icon-op.warn:hover { color:#b45309; border-color:#b45309; background:#fffbeb; }
  .icon-op.off { cursor:not-allowed; color:#cbd5e1; opacity:.55; }
  .icon-op.off:hover { color:#cbd5e1; border-color:var(--line); background:#fff; }
  .acct-card .acct-checkin { font-size:11px; }
  .tag { display:inline-block; border-radius:5px; padding:2px 7px; font-size:11px;
         font-weight:600; }
  .tag.ok { background:#f0fdf4; color:#15803d; }
  .tag.bad { background:#fef2f2; color:#b91c1c; }
  .tag.neutral { background:#f3f4f6; color:#6b7280; }
  .tag.cur { background:#eef2ff; color:#4338ca; }
  /* 渠道角标（卡片左上） */
  .badge-ch { display:inline-block; color:#fff; border-radius:6px; padding:2px 8px;
              font-size:11px; font-weight:700; letter-spacing:.3px; }
  .badge-ch.workbuddy { background:#4338ca; }
  .badge-ch.oczen { background:#475569; }
  .badge-ch.qoder { background:#7c3aed; }
  .badge-ch.trae { background:#0891b2; }
  /* 登录卡片：前置条件提示 */
  .prereq { background:#fffbeb; border:1px solid #fde68a; border-radius:9px;
            padding:9px 12px; font-size:12.5px; color:#92400e; margin-bottom:12px;
            line-height:1.6; }
  .prereq .plink { color:#b45309; font-weight:700; text-decoration:underline; }
  /* 登录等待浮条：fixed 悬浮于页面底部中央，不占文档流 */
  .login-toast { position:fixed; bottom:26px; left:50%; transform:translateX(-50%);
                 background:#0f172a; color:#e5e7eb; border-radius:12px; padding:12px 18px;
                 display:flex; align-items:center; gap:12px; z-index:180;
                 box-shadow:0 8px 28px rgba(0,0,0,.28); max-width:92vw; font-size:13px; }
  .login-toast .lt-spin { width:14px; height:14px; flex-shrink:0; border:2px solid #6366f1;
                 border-top-color:transparent; border-radius:50%; animation:lt-rot 1s linear infinite; }
  @keyframes lt-rot { to { transform:rotate(360deg); } }
  .login-toast .lt-text { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .page-toast { position:fixed; bottom:26px; left:50%; transform:translateX(-50%);
                background:rgba(31,35,41,.94); color:#fff; padding:11px 20px; border-radius:10px;
                font-size:13.5px; z-index:190; box-shadow:0 4px 16px rgba(0,0,0,.22); max-width:80vw; }
  .hidden { display:none !important; }
  /* 小公告弹窗 */
  .notice-overlay { position:fixed; inset:0; background:rgba(15,23,42,.42); z-index:210;
                    display:flex; align-items:center; justify-content:center; padding:20px; }
  .notice-box { background:#fff; border-radius:14px; width:420px; max-width:94vw;
                box-shadow:0 12px 40px rgba(0,0,0,.25); padding:18px 20px 16px; }
  .notice-head { display:flex; align-items:center; gap:9px; margin-bottom:10px; }
  .notice-ic { width:24px; height:24px; border-radius:50%; background:#fef3c7; color:#b45309;
               font-weight:800; font-size:14px; display:flex; align-items:center;
               justify-content:center; flex-shrink:0; }
  .notice-ic.err { background:#fee2e2; color:#b91c1c; }
  .notice-ic.ok { background:#dcfce7; color:#15803d; }
  .notice-title { font-size:15px; font-weight:700; }
  .notice-body { font-size:13px; color:#374151; line-height:1.65; word-break:break-word;
                 max-height:52vh; overflow-y:auto; white-space:pre-wrap; }
  .notice-actions { display:flex; justify-content:flex-end; margin-top:14px; }
  /* 签到结果分组：一级标题=渠道，二级标题=账号，右侧状态；失败账号下方附原因注释 */
  .notice-box.wide { width:520px; }
  .ck-group { border:1px solid var(--line); border-radius:10px; overflow:hidden;
              margin-bottom:10px; }
  .ck-group:last-child { margin-bottom:0; }
  .ck-ch { display:flex; align-items:center; gap:8px; padding:8px 12px;
           background:#f8fafc; border-bottom:1px solid var(--line); }
  .ck-ch .ck-ch-name { font-size:13.5px; font-weight:700; }
  .ck-ch .ck-ch-sum { margin-left:auto; font-size:11.5px; color:var(--muted);
                      font-variant-numeric:tabular-nums; }
  .ck-acct { display:flex; align-items:center; gap:8px; padding:8px 12px; font-size:12.5px; }
  .ck-acct + .ck-acct { border-top:1px solid #f1f5f9; }
  .ck-acct .ck-name { font-weight:600; min-width:0; overflow:hidden;
                      text-overflow:ellipsis; white-space:nowrap; }
  .ck-acct .ck-uid { font-size:11px; color:var(--muted); font-family:ui-monospace,monospace; }
  .ck-acct .ck-status { margin-left:auto; flex-shrink:0; font-size:12px; font-weight:600; }
  .ck-acct .ck-status.ok { color:#15803d; }
  .ck-acct .ck-status.bad { color:#b91c1c; }
  .ck-note { margin:0 12px 9px 12px; padding:7px 10px; border-radius:8px;
             background:#fffbeb; border:1px solid #fde68a; color:#92400e;
             font-size:12px; line-height:1.6; }
  .ck-note b { color:#b45309; }
  .ck-note.info { background:#f8fafc; border-color:var(--line); color:#4b5563; }
  /* 渠道空态卡片 */
  .empty-card { background:#fafbfc; border-style:dashed; padding:26px 20px; }
  .empty-inner { display:flex; flex-direction:column; align-items:center; gap:7px; }
  .empty-inner .etitle { font-size:13.5px; font-weight:600; color:#4b5563; }
  .empty-inner .edesc { font-size:12px; color:var(--muted); }
  /* 优先级需高于 .toolbar input（后者 width:220px，会压过单类选择器） */
  #page-traffic .toolbar input.datein,
  input.datein { border:1px solid var(--line); border-radius:8px; padding:5px 8px;
                 font-size:12px; background:#fff; width:132px; box-sizing:border-box; }
  /* 流量页工具栏：允许换行，避免与右侧按钮互挤 */
  #page-traffic .toolbar { flex-wrap:wrap; row-gap:8px; }
  #page-traffic .toolbar .chips { flex-wrap:nowrap; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px;
          padding:20px 22px; margin-bottom:22px; box-shadow:0 1px 3px rgba(0,0,0,.04); }
  .head { display:flex; align-items:center; gap:10px; margin-bottom:6px; flex-wrap:wrap; }
  .badge { font-size:11px; font-weight:700; letter-spacing:.5px; padding:3px 8px;
           border-radius:6px; background:#eff6ff; color:var(--blue); }
  .badge.cur { background:#dcfce7; color:#16a34a; }
  .badge.cool { background:#fef3c7; color:#d97706; }
  .nick { font-size:18px; font-weight:600; }
  .meta { color:var(--muted); font-size:12px; margin-bottom:12px; word-break:break-all; }
  .meta code { background:#f3f4f6; padding:1px 6px; border-radius:4px; }
  .checkin { font-size:13px; margin-bottom:14px; }
  .checkin .ok { color:#16a34a; } .checkin .no { color:var(--red); }
  /* 资源包列表：整体一个卡片，固定露出 4 行，内部滚动，先结束的排前面 */
  .pkg-list { border:1px solid var(--line); border-radius:12px; background:#fafbfc;
              max-height:236px; overflow-y:auto; padding:5px 6px; }
  .pkg-list::-webkit-scrollbar { width:6px; }
  .pkg-list::-webkit-scrollbar-track { background:transparent; }
  .pkg-list::-webkit-scrollbar-thumb { background:#d1d5db; border-radius:3px; }
  .pkg-list::-webkit-scrollbar-thumb:hover { background:#9ca3af; }
  .pkg { display:grid; grid-template-columns:minmax(200px,1.1fr) minmax(140px,1.6fr) auto;
         gap:14px; align-items:center; height:56px; padding:6px 12px; border-radius:9px; }
  .pkg + .pkg { border-top:1px solid #eef0f2; }
  .pkg:hover { background:#f1f3f5; }
  .pkg .name { font-size:13px; font-weight:500; min-width:0; }
  .pkg .name .t { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .pkg .name .d { font-size:11px; color:var(--muted); font-weight:400; margin-top:2px; }
  .pkg .name .d.soon { color:var(--orange); font-weight:600; }
  .pkg .mid { display:flex; align-items:center; gap:10px; min-width:0; }
  .pkg .bar { flex:1; height:7px; background:#e5e7eb; border-radius:99px; overflow:hidden; }
  .pkg .bar i { display:block; height:100%; border-radius:99px; background:var(--green); transition:width .4s; }
  .pkg .bar i.mid { background:var(--orange); } .pkg .bar i.low { background:var(--red); }
  .pkg .pct { font-size:11px; color:var(--muted); width:34px; text-align:right;
              font-variant-numeric:tabular-nums; }
  .pkg .nums { font-size:13px; text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .pkg .nums b { font-size:15px; }
  .pkg .nums .u { color:var(--muted); font-size:11px; }
  .pkg .nums.low b { color:var(--red); }
  .err { color:var(--red); font-size:13px; }
  .toolbar { display:flex; justify-content:flex-end; margin-bottom:16px; align-items:center; gap:12px; }
  button { background:#111827; color:#fff; border:0; border-radius:10px; padding:10px 18px;
           font-size:14px; cursor:pointer; }
  button:active { transform:scale(.98); }
  .auto { font-size:12px; color:var(--muted); }
  .empty { text-align:center; color:var(--muted); padding:40px; }
  .sw { background:#f3f4f6; color:#374151; border:1px solid var(--line); border-radius:8px;
        padding:5px 12px; font-size:12px; cursor:pointer; margin-left:auto; }
  .sw:hover { background:#e5e7eb; }
  /* ---- 模型管理板块 ---- */
  .sec-h { font-size:19px; font-weight:700; margin:30px 0 4px; }
  .sec-sub { font-size:12px; color:var(--muted); margin-bottom:14px; }
  .kv { display:flex; align-items:center; gap:10px; margin-bottom:9px; font-size:13px; flex-wrap:wrap; }
  .kv > span:not(.note) { color:var(--muted); width:64px; flex-shrink:0; }
  .kv .note { color:var(--muted); font-size:12px; flex:1 1 260px; }
  .kv code { background:#f3f4f6; padding:5px 11px; border-radius:6px; word-break:break-all; }
  .kv .note { color:var(--muted); font-size:12px; }
  .mini { background:#f3f4f6; color:#374151; border:1px solid var(--line); border-radius:6px;
          padding:3px 10px; font-size:12px; cursor:pointer; }
  .mini:hover { background:#e5e7eb; }
  .mini.warn { color:#b91c1c; }
  .mini2 { background:#111827; color:#fff; border:0; border-radius:8px; padding:8px 13px;
           font-size:12px; cursor:pointer; }
  .toolbar input { border:1px solid var(--line); border-radius:8px; padding:8px 11px;
                   font-size:13px; width:220px; }
  .toolbar input.short { width:110px; }
  .model-list { }
  /* ---- 模型页:厂商卡片 ---- */
  .vcard { background:var(--card); border:1px solid var(--line); border-radius:14px;
           box-shadow:0 1px 3px rgba(0,0,0,.04); margin-bottom:16px; overflow:hidden; }
  .vhead { display:flex; align-items:center; gap:10px; padding:14px 18px 10px; }
  .vd { width:30px; height:30px; border-radius:9px; color:#fff; font-size:14px; font-weight:800;
        display:flex; align-items:center; justify-content:center; }
  .vname { font-size:15px; font-weight:700; }
  .vg-n { background:#f3f4f6; color:var(--muted); border-radius:99px; font-size:11px;
          padding:2px 9px; font-weight:600; }
  .vbody { padding:0 12px 8px; }
  .accrow { display:flex; align-items:center; gap:10px; padding:10px 6px; }
  .accrow + .accrow { border-top:1px solid #f3f4f6; }
  .amain { display:flex; align-items:center; gap:7px; min-width:0; }
  .amain .nick { font-size:13.5px; font-weight:600; }
  .afile { flex:1; font-size:11px; color:var(--muted); font-family:ui-monospace,monospace;
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .badge.ok { background:#f0fdf4; color:#15803d; border:1px solid #bbf7d0; }
  .badge.warn { background:#fffbeb; color:#b45309; border:1px solid #fde68a; }
  .mini.danger { color:#b91c1c; border-color:#fecaca; }
  .mrow { display:flex; align-items:center; gap:14px; padding:10px 6px; }
  .mrow + .mrow { border-top:1px solid #f3f4f6; }
  .mrow.off { opacity:.55; }
  .mmain { flex:1; min-width:0; }
  .mtitle { display:flex; align-items:center; gap:7px; flex-wrap:wrap; }
  .mtitle .t { font-size:13.5px; font-weight:600; }
  .mspec { font-size:11.5px; color:var(--muted); margin-top:3px; }
  .mrate { min-width:66px; text-align:center; border-radius:9px; padding:5px 10px;
           font-size:14px; font-weight:800; white-space:nowrap; }
  .mrate small { display:block; font-size:9.5px; font-weight:500; opacity:.75; }
  .rate-lo { background:#f0fdf4; color:#15803d; border:1px solid #bbf7d0; }
  .rate-mid { background:#fffbeb; color:#b45309; border:1px solid #fde68a; }
  .rate-hi { background:#fef2f2; color:#b91c1c; border:1px solid #fecaca; }
  .badge.src { background:#f0fdf4; color:#15803d; font-size:10px; padding:2px 7px; }
  .mtag { border:1px solid; border-radius:5px; padding:1px 6px; font-size:10px; font-weight:600; }
  .ps { font-size:11px; }
  .spec .nospec { color:var(--muted); font-style:italic; font-size:12px; }
  .mparams { display:flex; gap:10px; align-items:center; flex-shrink:0; flex-wrap:wrap; justify-content:flex-end; }
  .pv { text-align:center; background:#f8fafc; border:1px solid #eef0f2; border-radius:10px;
        padding:8px 16px; min-width:78px; }
  .pv b { display:block; font-size:15px; font-weight:700; color:var(--text); }
  .pv span { font-size:10.5px; color:var(--muted); }
  .pv.rate-lo { background:#f0fdf4; border-color:#bbf7d0; } .pv.rate-lo b { color:#15803d; }
  .pv.rate-mid { background:#fffbeb; border-color:#fde68a; } .pv.rate-mid b { color:#b45309; }
  .pv.rate-hi { background:#fef2f2; border-color:#fecaca; } .pv.rate-hi b { color:#b91c1c; }
  .pv.rate-unknown { background:#f9fafb; border-style:dashed; } .pv.rate-unknown b { color:#9ca3af; }
  .pv.nospec-pv { background:#f9fafb; border-style:dashed; }
  .pv.nospec-pv b { color:#9ca3af; font-size:12px; font-weight:600; }
  .pv.nospec-pv span { font-size:10.5px; }
  /* ---- 页签导航 ---- */
  .tabs { display:flex; gap:4px; margin-bottom:0; border-bottom:2px solid var(--line); }
  .tab { background:transparent; color:var(--muted); border:0; border-radius:10px 10px 0 0;
         padding:11px 22px; font-size:14px; cursor:pointer; font-weight:500;
         border-bottom:2px solid transparent; margin-bottom:-2px; }
  .tab:hover { color:var(--text); background:rgba(0,0,0,.035); }
  .tab.act { color:#4338ca; font-weight:700; border-bottom:2px solid #4338ca; }
  #page-home, #page-models, #page-api { margin-top:22px; }
  /* 接口页 */
  pre.curl { background:#0f172a; color:#e2e8f0; border-radius:10px; padding:14px 16px;
             font-size:12.5px; line-height:1.7; white-space:pre-wrap; word-break:break-all;
             overflow-x:auto; max-width:100%; box-sizing:border-box; margin:0;
             font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }
  .chead { display:flex; align-items:center; gap:10px; margin-bottom:20px; flex-wrap:wrap; }
  .ct { font-size:14.5px; font-weight:700; }
  .kv code.bigcode { background:#0f172a; color:#e2e8f0; padding:9px 15px;
                     border-radius:8px; font-size:13px; word-break:break-all; border:0; }
  .method { font-size:10.5px; font-weight:800; border-radius:5px; padding:3px 8px;
            letter-spacing:.5px; flex-shrink:0; }
  .method.get { background:#dcfce7; color:#15803d; }
  .method.post { background:#dbeafe; color:#1d4ed8; }
  .ep { display:flex; align-items:center; gap:12px; padding:9px 10px;
        border-bottom:1px solid #f3f4f6; }
  .ep:last-child { border-bottom:0; }
  .ep code { font-size:12.5px; background:#f8fafc; padding:4px 10px; border-radius:6px;
             border:1px solid #eef0f2; word-break:break-all; }
  .edesc { font-size:12px; color:var(--muted); }
  /* API Keys 列表 */
  .krow { display:flex; align-items:center; gap:12px; padding:9px 6px; }
  .krow + .krow { border-top:1px solid #f3f4f6; }
  .krow .kname { font-size:13px; font-weight:600; width:130px; flex-shrink:0;
                 overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .krow code { flex:1; background:#f3f4f6; padding:5px 10px; border-radius:6px;
               font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .krow .ku { font-size:11px; color:var(--muted); width:110px; flex-shrink:0; }
  /* 表单字段(Key 生成行 / 模型编辑展开) */
  .add-form { display:flex; align-items:flex-end; gap:14px; flex-wrap:wrap; }
  .fe { display:flex; flex-direction:column; gap:4px; min-width:0; }
  .fe > span { font-size:11px; color:var(--muted); }
  .fe input[type=number], .fe input[type=text], .fe > input { border:1px solid var(--line);
      border-radius:8px; padding:8px 11px; font-size:13px; width:150px; background:#fff; }
  .fe input#key-name { width:200px; }
  /* 编辑展开 */
  .fe { display:flex; flex-direction:column; gap:4px; }
  .fe > span { font-size:11px; color:var(--muted); }
  .fe input[type=number], .fe input[type=text] { border:1px solid var(--line);
      border-radius:8px; padding:8px 11px; font-size:13px; width:150px; }
  .mrow-edit { background:#f8fafc; border:1px dashed var(--line); border-radius:10px;
               padding:12px 14px; margin:6px 4px 10px; display:flex; gap:18px;
               flex-wrap:wrap; align-items:flex-end; }
  .chip { display:inline-flex; align-items:center; gap:4px; border:1px solid var(--line);
          background:#fff; color:var(--text); border-radius:7px; padding:5px 9px;
          font-size:12px; font-weight:500; cursor:pointer; user-select:none; }
  .chip:hover { background:#f8fafc; border-color:#cbd5e1; }
  .chip:has(input:checked) { border-color:var(--green); background:#f0fdf4; }
  .chip input { accent-color:var(--green); margin:0; }
  .chips { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .chip.on { border-color:#4338ca; background:#eef2ff; color:#4338ca; font-weight:700; }
  .chip.on:hover { background:#e0e7ff; }
  .chip b { font-weight:700; opacity:.75; }
  /* 模型分组头里的渠道徽标（同一厂商可能横跨多渠道） */
  .chbadges { display:flex; gap:4px; margin-left:auto; flex-wrap:wrap; }
  .chbadge { font-size:10.5px; font-weight:600; border-radius:5px; padding:2px 7px;
             background:#f1f5f9; color:#475569; border:1px solid #e2e8f0; }
  .chbadge.all { background:#eef2ff; color:#4338ca; border-color:#c7d2fe; }
  .vgroup { margin-bottom:6px; }
  .vg-title { font-size:13px; font-weight:700; padding:8px 12px 6px; color:#374151;
              display:flex; align-items:center; gap:8px; }
  .vg-n { background:#e5e7eb; color:#6b7280; border-radius:99px; font-size:10px;
          padding:1px 8px; font-weight:600; }
  /* 主页面渠道分区 */
  .sect { margin-top:18px; }
  .shead { display:flex; align-items:center; gap:9px; margin-bottom:9px; }
  .sdot { width:22px; height:22px; border-radius:7px; color:#fff; font-size:12px; font-weight:800;
          display:flex; align-items:center; justify-content:center; }
  .st { font-size:15.5px; font-weight:800; }
  .sn { background:#e5e7eb; color:#6b7280; border-radius:99px; font-size:10.5px;
        padding:2px 9px; font-weight:600; }
  .shint { font-size:11.5px; color:var(--muted); }
  /* 流量监测 */
  .trow { display:flex; align-items:center; gap:10px; padding:9px 6px; font-size:12.5px; }
  .trow + .trow { border-top:1px solid #f3f4f6; }
  .trow .tn { flex:1; font-weight:600; min-width:0; overflow:hidden;
              text-overflow:ellipsis; white-space:nowrap; }
  .trow .tv { min-width:84px; text-align:right; font-variant-numeric:tabular-nums; }
  .trow .tth { font-size:11px; color:var(--muted); min-width:84px; text-align:right; }
  .trow.head { font-weight:700; color:var(--muted); font-size:11px; padding:4px 6px; }
  .trow.head .tv, .trow.head .tth { font-weight:700; }
  .hbar { flex:1; height:7px; background:#f3f4f6; border-radius:99px; overflow:hidden;
          display:flex; min-width:60px; max-width:180px; }
  .hbar i { display:block; height:100%; }
  .hbar .hc { background:#0f766e; }   /* 缓存内 */
  .hbar .hu { background:#f59e0b; }   /* 缓存外 */
  .hbar .ho { background:#4338ca; }   /* 输出 */
  .tag-in, .tag-out, .tag-c { font-size:10.5px; font-weight:700; border-radius:5px; padding:1px 6px; }
  .tag-in { background:#ecfdf5; color:#0f766e; }
  .tag-out { background:#eef2ff; color:#4338ca; }
  .tag-c { background:#fff7ed; color:#b45309; }
  /* 渠道积分构成 */
  .credhead { font-size:13px; margin-bottom:6px; }
  .credhead b { font-size:17px; color:#0f766e; font-variant-numeric:tabular-nums; }
  .crsize { color:var(--muted); font-size:12px; }
  .crhit { color:var(--muted); font-size:11px; margin-left:8px; }
  .cpkgs { display:flex; flex-direction:column; gap:4px; }
  .cpkg { display:flex; align-items:center; gap:9px; font-size:11.5px; }
  .cpname { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;
            white-space:nowrap; color:#4b5563; }
  .cpmid { display:flex; align-items:center; gap:6px; width:120px; }
  .cpmid .bar { flex:1; height:6px; background:#e5e7eb; border-radius:99px; overflow:hidden; }
  .cpmid .bar i { display:block; height:100%; border-radius:99px; background:var(--green);
                  transition:width .4s; }
  .cpmid .bar i.mid { background:var(--orange); }
  .cpmid .bar i.low { background:var(--red); }
  .cpmid .pct { font-size:10.5px; color:var(--muted); width:32px; text-align:right;
                font-variant-numeric:tabular-nums; }
  .cpnums { min-width:96px; text-align:right; font-variant-numeric:tabular-nums; }
  .cpnums.low { color:#ef4444; }
</style>
</head>
<body>
<div class="wrap">
  <h1>聚合控制台</h1>
  <div class="sub">多渠道聚合 · WorkBuddy / OpenCodeZen / Qoder / TraeWork · <b id="refreshed"></b></div>
  <div class="tabs">
    <button class="tab act" data-p="home" onclick="showPage('home')">🖥️ 总览与账号</button>
    <button class="tab" data-p="traffic" onclick="showPage('traffic')">📊 流量监测</button>
    <button class="tab" data-p="models" onclick="showPage('models')">🧩 模型</button>
    <button class="tab" data-p="api" onclick="showPage('api')">🔌 接口</button>
  </div>
  <div id="page-home">
  <div class="toolbar">
    <span class="auto" id="auto">30s 自动刷新</span>
    <span style="flex:1"></span>
    <button onclick="loadAll()">刷新全部</button>
  </div>
  <div class="stats" id="stats"></div>

  <!-- 账号管理（与主页面合并）：长条卡 + 可拖动排序 -->
  <div class="sect" id="sec-accounts">
    <div class="shead"><span class="sdot" style="background:#0f172a">✚</span>
      <span class="st">账号管理</span><span class="sn" id="acct-count"></span>
      <span class="shint">登录全程由你在浏览器自行完成，本服务不会操作你的电脑 · 按住卡片可拖动调整展示顺序（自动记住）</span></div>
    <div class="card">
      <div class="acct-head">
        <div class="pa-left">
          <button class="btn primary" onclick="refreshAllCredits()">刷新积分</button>
          <button class="btn" onclick="checkinAll(this)" title="为账号池内全部 WorkBuddy 账号执行每日签到">🎁 一键签到</button>
          <button class="btn" onclick="loadChannels()">↻ 刷新账号</button>
          <span class="auto" id="accounts-updated"></span>
        </div>
        <div class="pa-right" id="add-buttons"></div>
      </div>
      <div id="acct-strip" class="strip-list"><div class="empty">加载中…</div></div>
    </div>
  </div>
  </div>

  <div id="page-traffic" style="display:none">
  <div class="sec-sub" style="margin-top:4px;">按渠道与模型统计用量 · 区分缓存内/外</div>

  <div class="card">
    <div class="toolbar" style="margin:0 0 12px;">
      <div class="chips" id="traffic-range">
        <button class="chip on" data-r="today" onclick="setRange('today')">今日</button>
        <button class="chip" data-r="week" onclick="setRange('week')">本周</button>
        <button class="chip" data-r="month" onclick="setRange('month')">本月</button>
        <button class="chip" data-r="all" onclick="setRange('all')">全部</button>
      </div>
      <input type="date" id="range-start" class="datein" onchange="setRange('custom')">
      <span class="auto">至</span>
      <input type="date" id="range-end" class="datein" onchange="setRange('custom')">
      <span style="flex:1"></span>
      <span class="auto" id="traffic-updated"></span>
      <button class="mini" onclick="loadStats()">↻ 刷新</button>
      <button class="mini danger" onclick="resetStats()">清空统计</button>
    </div>
    <div class="stats" id="traffic-stats"></div>
    <div class="chead" style="margin-top:2px;"><span class="vd" style="background:#0f766e">📊</span>
      <span class="ct">渠道用量</span>
      <span class="note">按住 ⠿ 可拖动调整渠道顺序 · 点 + 展开看模型明细</span>
      <span style="flex:1"></span>
      <button class="mini" onclick="expandAllChannels(true)">全部展开</button>
      <button class="mini" onclick="expandAllChannels(false)">全部收起</button></div>
    <div id="traffic-channels"><div class="empty">加载中…</div></div>
  </div>

  <div class="card">
    <div class="chead"><span class="vd" style="background:#0f766e">📈</span><span class="ct">用量趋势</span>
      <div class="chips" id="metric-switch" style="margin-left:6px;">
        <button class="chip on" data-m="total" onclick="setMetric('total')">Token</button>
        <button class="chip" data-m="credits" onclick="setMetric('credits')">积分</button>
      </div>
      <span class="note" id="chart-note"></span></div>
    <div id="traffic-chart"><div class="empty">加载中…</div></div>
    <div class="legend" id="traffic-legend"></div>
  </div>

  <div class="card">
    <div class="chead nowrap"><span class="vd" style="background:#0f172a">📋</span><span class="ct">请求日志</span>
      <span class="note" id="log-note"></span>
      <span class="log-tools">
        <select id="log-filter-ch" onchange="loadLogs(0)"></select>
        <input id="log-search" placeholder="搜索账号/模型…"
               onkeydown="if(event.key==='Enter')loadLogs(0)">
        <button class="mini" onclick="loadLogs(0)">↻ 查询</button>
        <button class="mini danger" onclick="resetLogs()">清空日志</button>
      </span></div>
    <div id="traffic-recent"><div class="empty">加载中…</div></div>
    <div class="pager" id="log-pager" style="display:none">
      <button class="mini" id="log-prev" onclick="loadLogs(logOffset-logLimit)">‹ 上一页</button>
      <span class="pg-info" id="log-pageinfo"></span>
      <button class="mini" id="log-next" onclick="loadLogs(logOffset+logLimit)">下一页 ›</button>
    </div>
  </div>
  </div>

  <div id="page-models" style="display:none">
  <div class="sec-sub" style="margin-top:4px;">自动发现上游可用模型 · 规格（上下文/最大输出/模态）来自上游动态拉取 · 点渠道标签筛选</div>
  <div class="card">
    <div class="toolbar" style="margin:0 0 10px;">
      <div class="chips" id="model-channels"></div>
      <span style="flex:1"></span>
      <span class="auto" id="models-updated"></span>
      <button class="mini" onclick="refreshModels()">↻ 刷新模型列表</button>
    </div>
    <div class="model-list" id="model-list"><div class="empty">加载中…</div></div>
  </div>
  </div>

  <div id="page-api" style="display:none">
  <div class="card">
    <div class="chead"><span class="vd" style="background:#4338ca">⚡</span><span class="ct">接入信息</span>
      <span class="note" id="api-note"></span></div>
    <div class="kv"><span>Base URL</span><code class="bigcode" id="api-url">—</code>
      <button class="mini" onclick="copyTxt('api-url')">复制</button></div>
    <div class="kv"><span>当前 Key</span><code class="bigcode" id="api-key">—</code>
      <button class="mini" onclick="copyTxt('api-key')">复制</button></div>
    <div class="chead" style="margin:18px 0 8px;"><span class="ct" style="font-size:13px; color:var(--muted);">调用端点</span>
      <span class="note">三类接口任选其一接入;模型名用「模型」页里的模型 ID,流式加 "stream": true</span></div>
    <div id="api-eps"></div>
  </div>
  <div class="card">
    <div class="chead"><span class="vd" style="background:#7c3aed">◈</span><span class="ct">分平台端点</span>
      <span class="note">统一入口 + 各平台专用入口 · 配合「绑定平台」的 Key 分别管理额度</span></div>
    <div id="api-platforms"><div class="empty">加载中…</div></div>
    <div class="kv" style="margin-top:10px;"><span class="note">
      分平台端点同样支持 <code>/responses</code>、<code>/messages</code>、<code>/models</code> 子路径；
      模型名可省略渠道前缀，服务端会自动归入该平台，避免跨平台误用。
    </span></div>
  </div>
  <div class="card">
    <div class="chead"><span class="vd" style="background:#b45309">🔑</span><span class="ct">API Keys</span>
      <span class="note">接口统一发放，密钥按平台分别管理 · 创建立即生效，删除立即失效</span></div>
    <div class="add-form" style="margin-bottom:10px;">
      <div class="fe"><span>用途备注</span><input id="key-name" placeholder="如:我的电脑 / 手机" style="width:190px;"></div>
      <div class="fe"><span>绑定平台</span>
        <select id="key-channel" style="border:1px solid var(--line); border-radius:8px;
                padding:8px 11px; font-size:13px; background:#fff; width:200px;"></select></div>
      <button class="mini2" onclick="createKey()" style="align-self:flex-end;">+ 生成新 Key</button>
      <span class="note" style="align-self:flex-end;" id="master-note"></span>
    </div>
    <div class="note" style="margin-bottom:10px;">
      <b>为什么要绑定平台：</b>不同平台存在同名模型（如 <code>deepseek-v4-pro</code>、
      <code>glm-5.3</code> 在 WorkBuddy 与 Qoder 上都有，但分属腾讯与阿里）。
      绑定后，该 Key 写<b>裸模型名</b>也会自动归属对应平台，不会误连到其他平台；
      要去别的平台必须显式写前缀（如 <code>qoder/glm-5.3</code>），否则 403。
      选「全部渠道」则不绑定，裸名按 WorkBuddy 处理（原有行为）。
    </div>
    <div id="keys-list"><div class="empty">加载中…</div></div>
  </div>
  <div class="card">
    <div class="chead"><span class="vd" style="background:#0f766e">◈</span><span class="ct">各平台模型重名情况</span>
      <span class="note">这些模型在多个平台同名，是串台风险点</span></div>
    <div id="dup-models"><div class="empty">加载中…</div></div>
  </div>
  <div class="card">
    <div class="chead"><span class="vd" style="background:#0f172a">{ }</span><span class="ct">快速开始</span>
      <button class="mini" onclick="copyTxt('api-curl')">复制 curl</button></div>
    <pre class="curl" id="api-curl">—</pre>
    <div class="kv" style="margin-top:10px;"><span class="note">把 Base URL 和 Key 填进任何 OpenAI 兼容客户端(Cherry Studio / LobeChat / NextChat 等)即可使用</span></div>
  </div>
  </div>
  </div>
</div>
<!-- 登录等待浮条（点击添加账号后弹出，不占页面内容） -->
<div id="login-toast" class="login-toast hidden">
  <span class="lt-spin"></span>
  <span class="lt-text" id="login-toast-text">等待授权…</span>
  <button class="mini" id="login-toast-callback" style="display:none" onclick="submitCallback()">粘贴回调链接</button>
  <button class="mini" onclick="reopenLogin()">重新打开窗口</button>
  <button class="mini" onclick="closeLogin()">取消</button>
</div>
<div id="page-toast" class="page-toast hidden"></div>
<!-- 小公告弹窗（签到失败等需要说明的场景） -->
<div id="notice-overlay" class="notice-overlay hidden" onclick="if(event.target===this)closeNotice()">
  <div class="notice-box" id="notice-box">
    <div class="notice-head"><span class="notice-ic" id="notice-ic">!</span>
      <span class="notice-title" id="notice-title">提示</span></div>
    <div class="notice-body" id="notice-body"></div>
    <div class="notice-actions"><button class="mini2" onclick="closeNotice()">知道了</button></div>
  </div>
</div>
<script>
const fmt = n => (n ?? 0).toLocaleString('en-US');
function barHtml(pct, cls) {
  return `<div class="bar"><i class="${cls || ''}" style="width:${Math.max(pct,2)}%"></i></div>`;
}
function badge(a) {
  if (a.cooldown_remaining > 0) return `<span class="badge cool">冷却 ${Math.ceil(a.cooldown_remaining/60)} 分钟</span>`;
  if (a.current) return '<span class="badge cur">● 当前使用</span>';
  return '<span class="badge">备用</span>';
}
/* WorkBuddy 单账号长条卡（主页面与账号管理合并视图） */
function wbCard(a) {
  const c = a.credits || {};
  const exp = a.token_expires_at ? new Date(a.token_expires_at).toLocaleString('zh-CN') : '?';
  const ck = a.checkin || {};
  const now = Date.now();
  const DAY = 86400000;
  // 后端已按 cycle_end 升序（先结束的在前），这里只做渲染
  const pkgs = (c.packages || []).map(p => {
    const pct = p.size ? Math.round(p.remain * 100 / p.size) : 0;
    const barCls = pct <= 20 ? 'low' : pct <= 50 ? 'mid' : '';
    const endTs = p.cycle_end ? new Date(p.cycle_end.replace(' ', 'T')).getTime() : 0;
    const daysLeft = endTs ? Math.ceil((endTs - now) / DAY) : null;
    const soon = daysLeft !== null && daysLeft <= 7;
    const dTxt = daysLeft === null ? '—' :
      daysLeft < 0 ? '已过期' : daysLeft === 0 ? '今天到期' :
      daysLeft === 1 ? '明天到期' : `${daysLeft} 天后到期`;
    return `<div class="pkg">
      <div class="name"><div class="t" title="${p.name}">${p.name}</div>
        <div class="d${soon ? ' soon' : ''}">${dTxt} · ${p.cycle_end || ''}</div></div>
      <div class="mid">${barHtml(pct, barCls)}<span class="pct">${pct}%</span></div>
      <div class="nums${pct <= 20 ? ' low' : ''}"><b>${fmt(p.remain)}</b> <span class="u">/ ${fmt(p.size)} ${p.unit}</span></div>
    </div>`;
  }).join('');
  const list = c.error ? `<div class="err">额度查询失败: ${c.error}</div>`
    : c.packages?.length ? `<div class="pkg-list">${pkgs}</div>` : '<div class="empty">无活跃资源包</div>';
  let creditHtml = `<span class="credit-num">${fmt(c.total_remain)}</span><span>/ ${fmt(c.total_size)} credits</span>`;
  if (!c.error) {
    const expiring = expiringCredits(c);
    if (expiring > 0) creditHtml += `<span class="credit-expiring"> 临期 ${fmt(expiring)}</span>`;
  }
  return `<div class="strip-card${a.cooldown_remaining > 0 ? ' disabled' : ''}" data-key="workbuddy:${a.uid}">
    <div class="sc-grip" title="拖动调整顺序" draggable="true">⠿⠿</div>
    <div class="sc-side">
      <div class="acct-top"><div><span class="badge-ch workbuddy">WorkBuddy</span>
        <span class="acct-name" title="${a.nickname || ''}">${a.nickname || '未知账号'}</span></div></div>
      <div class="acct-uid">UID <code>${(a.uid||'').slice(0,8)}…</code>
        · token 到期 ${exp}${a.token_expired ? ' <b style="color:#ef4444">(已过期,将自动刷新)</b>' : ''}</div>
      <div class="acct-credits">${creditHtml}</div>
      <div class="acct-ops">${badge(a)}${a.current ? '' : `<button class="sw" onclick="switchTo('${a.uid}', this)">设为当前使用</button>`}</div>
      <div class="acct-checkin">今日签到:
        ${ck.today ? (ck.ok ? `<span class="ok">✅ ${ck.msg || '已签到'}${ck.time ? ' ('+ck.time+')' : ''}</span>`
                            : `<span class="no">❌ ${ck.msg || '失败'}</span>`)
                  : '<span class="no">未记录(服务重启后首次签到前)</span>'}</div>
    </div>
    <div class="sc-main">${list}</div>
  </div>`;
}
function render() {
  const d = latestAccounts;
  const accs = d.accounts || [];
  window.__wbAccounts = accs;   // 供统一长条卡渲染
  document.getElementById('refreshed').textContent =
    '更新于 ' + new Date(d.generated_at * 1000).toLocaleTimeString('zh-CN');
  renderStats();
  renderStrip();
}

/* 顶部统计：账号数/积分按平台；签到进度按渠道（各渠道签到状态独立） */
function renderStats() {
  const box = document.getElementById('stats');
  if (!box) return;
  const wb = window.__wbAccounts || [];
  const chs = window.__channelsCache || [];
  const groups = [];
  if (wb.length) groups.push({ kind: 'workbuddy', name: 'WorkBuddy', accs: wb });
  for (const ch of chs) {
    if (ch.kind === 'workbuddy' || ch.kind === 'oczen') continue;   // WB 走 account-status；匿名通道无积分
    const accs = ch.accounts || [];
    if (!accs.length) continue;
    groups.push({ kind: ch.kind, name: ch.name, accs });
  }
  const totalAccs = groups.reduce((s, g) => s + g.accs.length, 0);
  if (!totalAccs) { box.innerHTML = ''; return; }
  // 签到进度：各渠道分别统计（Trae/Qoder 也支持签到；无签到能力的渠道不显示）
  const ckCards = groups.map(g => {
    const n = g.accs.length;
    const ok = g.accs.filter(a => a.checkin?.today && a.checkin?.ok).length;
    return `<div class="stat"><div class="v">${ok}/${n}</div>
      <div class="k">${esc(g.name)} 已签到</div></div>`;
  }).join('');
  const creditCards = groups.map(g => {
    const sum = g.accs.reduce((s, a) => s + (a.credits?.total_remain || 0), 0);
    return `<div class="stat cred-ch"><div class="v">${fmt(sum)}</div>
      <div class="k">${esc(g.name)} 积分</div></div>`;
  }).join('');
  box.innerHTML = `
    <div class="stat"><div class="v">${totalAccs}</div><div class="k">账号总数</div></div>
    ${ckCards}
    ${creditCards}`;
}
let latestAccounts = {accounts: []};
async function load() {
  try { latestAccounts = await (await fetch('/v1/account-status')).json(); }
  catch(e) { const box = document.getElementById('acct-strip');
    if (box) box.innerHTML = `<div class="empty">加载失败: ${e}</div>`; return; }
  render();
}
async function switchTo(uid, btn) {
  btn.disabled = true; btn.textContent = '切换中…';
  try {
    const r = await fetch('/v1/account-switch', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({uid})});
    if (!r.ok) throw new Error((await r.json()).error?.message || r.status);
  } catch(e) { alert('切换失败: ' + e.message); }
  load();
}

/* ---- 模型管理 ---- */
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
const fmtK = n => !n ? '—' : n >= 1048576 ? (n/1048576).toFixed(n%1048576 ? 1 : 0) + 'M' : Math.round(n/1024) + 'K';
const IN_LAB = {text:'文本', image:'图片', video:'视频', pdf:'PDF'};
function specSummary(s) {
  if (!s) return '—';
  const inp = (s.input || []).map(x => IN_LAB[x] || x).join('/');
  const out = (s.output || []).map(x => IN_LAB[x] || x).join('/');
  // 上游未下发任何数值规格（如 TraeWork）：明确说明，而不是显示两个「—」
  const nums = (!s.context_length && !s.max_output_tokens)
    ? '<span class="nospec" title="该渠道上游接口未下发上下文/最大输出，以模型实际运行为准">上游未提供规格</span>'
    : `<span><b>${fmtK(s.context_length)}</b> 上下文</span>·<span><b>${fmtK(s.max_output_tokens)}</b> 最大输出</span>`;
  return `<span class="spec">${nums}·<span>输入 <span class="tag">${inp || '—'}</span></span>·<span>输出 <span class="tag">${out || '—'}</span></span></span>`;
}
async function loadModels() {
  let d;
  try { d = await (await fetch('/v1/models-info')).json(); }
  catch(e) { return; }
  const api = d.api || {};
  document.getElementById('api-url').textContent = api.base_url || '—';
  const k = document.getElementById('api-key');
  k.textContent = api.auth_enabled ? api.api_key : '(未启用鉴权,客户端留空即可)';
  document.getElementById('api-note').textContent = api.auth_enabled ? '' : '如需启用鉴权,启动时加 --api-key your-secret';
  window.latestModels = d.models || [];
  renderModels();
  renderApi();
}

// 当前选中的渠道筛选（'' = 全部）
let modelChannelFilter = localStorage.getItem('wb-model-channel') || '';

function renderModels() {
  const all = window.latestModels || [];
  // 渠道归属：渠道模型带 meta.channel；WorkBuddy 模型无前缀
  const channelOf = m => (m.meta && m.meta.channel) ? m.meta.channel : 'workbuddy';
  const CH_META = {
    workbuddy: {label:'WorkBuddy', color:'#4338ca'},
    oczen:     {label:'OpenCodeZen', color:'#0f766e'},
    qoder:     {label:'Qoder', color:'#7c3aed'},
    trae:      {label:'TraeWork', color:'#0369a1'},
  };
  // 渠道筛选条
  // 展示过滤：WorkBuddy 侧沿用"需有倍率"的过滤（auto/无倍率的官方未上架），渠道侧不套用。
  // 先算可视集合，再据此统计 chip 数量——否则"全部 N"会把被过滤的模型也算进去，
  // 与右侧"X 个模型"文案不一致（实测差 8 个）。
  const isVisible = m => {
    const c = channelOf(m);
    if (c === 'workbuddy') return m.name !== 'auto' && m.meta?.credits;
    return true;
  };
  const counts = {};
  for (const m of all) {
    if (!isVisible(m)) continue;
    const c = channelOf(m); counts[c] = (counts[c] || 0) + 1;
  }
  const totalVisible = all.filter(isVisible).length;
  const chips = document.getElementById('model-channels');
  if (chips) {
    const order = Object.keys(CH_META).filter(c => counts[c]);
    chips.innerHTML = `<button class="chip${modelChannelFilter === '' ? ' on' : ''}"
        onclick="setModelChannel('')">全部 <b>${totalVisible}</b></button>`
      + order.map(c => `<button class="chip${modelChannelFilter === c ? ' on' : ''}"
          onclick="setModelChannel('${c}')">${esc(CH_META[c].label)} <b>${counts[c]}</b></button>`).join('');
  }

  const visible = all.filter(m => {
    const c = channelOf(m);
    if (modelChannelFilter && c !== modelChannelFilter) return false;
    return isVisible(m);
  });
  // 厂商识别：先剥掉渠道前缀，再按模型名关键词归类（WorkBuddy 与各渠道统一口径）
  const vendorOf = m => {
    const raw = String(m.name || '');
    const base = (raw.includes('/') ? raw.split('/').slice(1).join('/') : raw).toLowerCase();
    if (base === 'auto') return '自动路由';
    if (base.startsWith('deepseek')) return 'DeepSeek';
    if (base.startsWith('glm') || base.startsWith('glm5v')) return '智谱 GLM';
    if (base.startsWith('kimi')) return 'Kimi';
    if (base.startsWith('qwen')) return '通义千问';
    if (base.startsWith('minimax')) return 'MiniMax';
    if (base.startsWith('mimo')) return 'MiMo';
    if (base.startsWith('nemotron')) return 'Nemotron';
    if (base.startsWith('muse-spark')) return 'Muse Spark';
    if (base.startsWith('ling')) return 'Ling';
    if (base.startsWith('longcat')) return 'LongCat';
    if (base.startsWith('hy') || base.startsWith('hunyuan')) return '混元';
    if (base.startsWith('gpt') || base.startsWith('o1') || base.startsWith('o3')) return 'OpenAI';
    if (base.startsWith('claude')) return 'Claude';
    // 匿名免费通道里厂商未识别出的型号（big-pickle / space-bunny / jev 等）
    if (raw.startsWith('oczen/')) return '其他免费模型';
    return '其他';
  };
  const VENDOR_META = {
    'DeepSeek': {color:'#4D6BFE', abbr:'D'},
    '智谱 GLM': {color:'#3859FF', abbr:'Z'},
    'Kimi': {color:'#1f2937', abbr:'K'},
    '通义千问': {color:'#615CED', abbr:'Q'},
    '混元': {color:'#0052D9', abbr:'混'},
    'MiniMax': {color:'#ef4444', abbr:'M'},
    'MiMo': {color:'#ea580c', abbr:'Mi'},
    'Nemotron': {color:'#76b900', abbr:'N'},
    'Muse Spark': {color:'#db2777', abbr:'S'},
    'Ling': {color:'#0891b2', abbr:'L'},
    'LongCat': {color:'#f59e0b', abbr:'LC'},
    'OpenAI': {color:'#10a37f', abbr:'AI'},
    'Claude': {color:'#d97757', abbr:'C'},
    '自动路由': {color:'#0f766e', abbr:'A'},
    '其他免费模型': {color:'#0f766e', abbr:'F'},
    '其他': {color:'#6b7280', abbr:'·'},
    'WorkBuddy': {color:'#4338ca', abbr:'W'},
    'OpenCodeZen': {color:'#0f766e', abbr:'Z'},
    'Qoder': {color:'#7c3aed', abbr:'Q'},
    'TraeWork': {color:'#0369a1', abbr:'T'},
  };
  // 倍率统一取值：WorkBuddy 侧是 meta.credits 字符串（如 "x0.79"），渠道侧是 specs.rate 数值。
  // 返回 null 表示上游未下发（显示空白，不猜）。
  const rateNumOf = m => {
    if (typeof m.specs?.rate === 'number') return m.specs.rate;
    const c = m.meta?.credits;
    if (!c) return null;
    const n = parseFloat(String(c).replace(/[^0-9.]/g, ''));
    return isNaN(n) ? null : n;
  };
  const rateSortKey = m => { const r = rateNumOf(m); return r === null ? Infinity : r; };
  const groups = {};
  for (const m of visible) { const v = vendorOf(m); (groups[v] = groups[v] || []).push(m); }
  for (const v in groups) groups[v].sort((a, b) => rateSortKey(a) - rateSortKey(b));
  const ORDER = Object.keys(groups).sort((a, b) => rateSortKey(groups[a][0]) - rateSortKey(groups[b][0]));
  const rowHtml = m => {
    const meta = m.meta || {};
    const tags = (meta.tags || []).map(t => {
      const parts = String(t).split(':');
      const lab = parts[1] || '', col = parts[2] || '#d97706';
      return `<span class="mtag" style="color:${esc(col)}; border-color:${esc(col)}55;">${esc(lab)}</span>`;
    }).join('');
    const srcb = m.source && m.source !== '上游' ? `<span class="badge src">${m.source}</span>` : '';
    // 查看「全部」时，行内标注渠道，避免同名模型（如 GLM-5.3 在 WB 与 Qoder 都有）分不清
    const c = channelOf(m);
    const chTag = (!modelChannelFilter && c !== 'workbuddy')
      ? `<span class="chbadge">${esc((CH_META[c] || {}).label || c)}</span>` : '';
    const rateNum = rateNumOf(m);
    const rateCls = rateNum === null ? '' : rateNum <= 0.1 ? 'rate-lo' : rateNum <= 0.6 ? 'rate-mid' : 'rate-hi';
    // 免费（倍率 0）单独标注，避免和"未知"混淆
    const rateLabel = rateNum === null ? null : (rateNum === 0 ? '免费' : `${rateNum}x`);
    const desc = meta.description ? ` title="${esc(m.name)} · ${esc(meta.description)}${meta.credits ? ' · ' + esc(meta.credits) : ''}"` : '';
    const inp = (m.specs?.input || []).map(x => IN_LAB[x] || x).join('/');
    const out = (m.specs?.output || []).map(x => IN_LAB[x] || x).join('/');
    // 上下文与最大输出都未知（如 TraeWork 上游不下发）：合并为一个提示格，不显示两个「—」
    const nospec = !(m.specs?.context_length) && !(m.specs?.max_output_tokens);
    // 规格来源标注：渠道规格来自上游动态拉取（context_from_api）
    const apiTag = meta.context_from_api ? '<span class="badge ok" title="规格来自上游接口">上游规格</span>' : '';
    const reasonTag = meta.supports_reasoning ? '<span class="badge src" title="支持思考模式">思考</span>' : '';
    return `<div class="mrow" id="mrow-${esc(m.name)}">
      <div class="mmain">
        <div class="mtitle"><span class="t"${desc}>${m.name}</span>${chTag}${srcb}${apiTag}${reasonTag}${tags}</div>
      </div>
      <div class="mparams">
        ${nospec
          ? `<div class="pv nospec-pv" title="该渠道上游接口未下发上下文/最大输出，以模型实际运行为准"><b>上游未提供</b><span>上下文/最大输出</span></div>`
          : `<div class="pv"><b>${fmtK(m.specs?.context_length)}</b><span>上下文</span></div>
             <div class="pv"><b>${fmtK(m.specs?.max_output_tokens)}</b><span>最大输出</span></div>`}
        <div class="pv"><b>${inp || '—'}</b><span>输入</span></div>
        <div class="pv"><b>${out || '—'}</b><span>输出</span></div>
        ${rateLabel !== null
          ? `<div class="pv ${rateCls}" title="${rateNum === 0 ? '该模型免费（倍率 0）' : 'credits 倍率'}">
               <b>${rateLabel}</b><span>倍率</span></div>`
          : `<div class="pv rate-unknown" title="上游未下发倍率"><b>—</b><span>倍率</span></div>`}
      </div>
    </div>`;
  };
  const grouped = ORDER.filter(v => groups[v] && groups[v].length).map(v => {
    const vm = VENDOR_META[v] || VENDOR_META['其他'];
    const ms = groups[v];
    // 该厂商下出现过的渠道（同一厂商可能横跨多个渠道，如 GLM 在 WB 与 Qoder 都有）
    const chSet = [...new Set(ms.map(channelOf))].map(c => (CH_META[c] || {}).label || c);
    const chBadges = chSet.map(c =>
      `<span class="chbadge">${esc(c)}</span>`).join('');
    return `<div class="vcard">
      <div class="vhead">
        <span class="vd" style="background:${vm.color}">${vm.abbr}</span>
        <span class="vname">${esc(v)}</span>
        <span class="vg-n">${ms.length} 个模型</span>
        <span class="chbadges">${chBadges}</span>
      </div>
      <div class="vbody">${ms.map(rowHtml).join('')}</div>
    </div>`;
  }).join('');
  document.getElementById('models-updated').textContent =
    `${visible.length} 个模型` + (modelChannelFilter ? ` · 已筛选 ${(CH_META[modelChannelFilter]||{}).label || modelChannelFilter}` : '');
  document.getElementById('model-list')
.innerHTML =
    grouped || '<div class="empty">该渠道暂无模型（或均已被过滤）</div>';
}
function setModelChannel(c) {
  modelChannelFilter = c;
  localStorage.setItem('wb-model-channel', c);
  renderModels();
}

/* ---- v2 页签与接口页 ---- */
function showPage(p) {
  if (p === 'accounts') p = 'home';   // 旧页签名兼容：主页面与账号管理已合并
  for (const id of ['home', 'traffic', 'models', 'api'])
    document.getElementById('page-' + id).style.display = (id === p ? '' : 'none');
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('act', t.dataset.p === p));
  localStorage.setItem('wb-page', p);
  if (p === 'api') { renderApi(); loadKeys(); }
  if (p === 'traffic') { loadStats(); }
  if (p === 'home') { loadAll(); }
}

/* ---- 主页面：WorkBuddy 之外的渠道分区 ---- */
async function loadAll() { await Promise.all([load(), loadChannels()]); }

function renderChannelCredits(c) {
  if (!c) return '<div class="pkg-note">该渠道不提供积分</div>';
  if (c.error) return `<div class="pkg-note">积分查询失败: ${esc(String(c.error))}</div>`;
  if (!c.packages || !c.packages.length) return '<div class="pkg-note">无活跃资源包</div>';
  const now = Date.now(), DAY = 86400000;
  const unit = c.packages[0].unit || 'credits';
  const head = `<div class="pkg-sum">剩余 <b>${fmt(c.total_remain)}</b> / ${fmt(c.total_size)} ${esc(unit)}
    · 已用 ${fmt((c.total_size||0) - (c.total_remain||0))}</div>`;
  const pkgs = c.packages.map(p => {
    const pct = p.size ? Math.round(p.remain * 100 / p.size) : 0;
    const barCls = pct <= 20 ? 'low' : pct <= 50 ? 'mid' : '';
    const endTs = p.cycle_end ? new Date(p.cycle_end.replace(' ', 'T')).getTime() : 0;
    const daysLeft = endTs ? Math.ceil((endTs - now) / DAY) : null;
    const soon = daysLeft !== null && daysLeft <= 7;
    const dTxt = daysLeft === null ? (p.cycle_end || '—') :
      daysLeft < 0 ? '已过期 · ' + p.cycle_end : daysLeft === 0 ? '今天到期' :
      daysLeft === 1 ? '明天到期 · ' + p.cycle_end : `${daysLeft} 天后到期 · ${p.cycle_end}`;
    return `<div class="pkg">
      <div class="name"><div class="t" title="${esc(p.name)}">${esc(p.name)}</div>
        <div class="d${soon ? ' soon' : ''}">${esc(dTxt)}</div></div>
      <div class="mid">${barHtml(pct, barCls)}<span class="pct">${pct}%</span></div>
      <div class="nums${pct <= 20 ? ' low' : ''}"><b>${fmt(p.remain)}</b> <span class="u">/ ${fmt(p.size)} ${esc(p.unit || unit)}</span></div>
    </div>`;
  }).join('');
  return `${head}<div class="pkg-list">${pkgs}</div>`;
}

/* ---- 渠道账号管理（Qoder / TraeWork / OpenCodeZen）---- */
let loginCtx = null;   // {kind, session, timer}
async function loadChannels() {
  try {
    const d = await (await fetch('/v1/channels')).json();
    const chs = d.channels || [];
    window.__channelsCache = chs;   // 供积分构成弹层与统一渲染复用
    renderStats();                  // 渠道数据到达后刷新顶部统计（含各平台积分）
    document.getElementById('accounts-updated').textContent =
      '更新于 ' + new Date().toLocaleTimeString();

    // 右上：「＋ 渠道」按钮组（多列网格 + 按渠道配色，对齐 wild-work）
    const addBox = document.getElementById('add-buttons');
    if (addBox) {
      addBox.innerHTML = chs.filter(c => c.can_add).map(c =>
        `<button class="btn primary ch-${esc(c.kind)}" onclick="startLogin('${esc(c.kind)}','${esc(c.name)}','${esc(c.login_mode || 'poll')}')">＋ ${esc(c.name)}</button>`
      ).join('');
    }
    renderStrip();
  } catch (e) {
    const box = document.getElementById('acct-strip');
    if (box) box.innerHTML = '<div class="empty">加载失败: ' + esc(String(e)) + '</div>';
  }
}

/* 单张渠道账号长条卡（渠道 badge + 图标操作 + 积分合计 + 资源包长条明细） */
function acctCard(ch, a) {
  const isAnon = !ch.can_add;                       // 匿名渠道（OpenCodeZen）
  const cur = !!a.current;
  const cooldown = a.cooldown_remaining > 0;
  const disabledCls = cooldown ? ' disabled' : '';

  // 积分合计展示（明细放卡片右侧长条区）
  let creditHtml;
  const c = a.credits;
  if (isAnon) {
    creditHtml = '<span class="credit-na" title="匿名通道无积分概念">不适用</span>';
  } else if (!c) {
    creditHtml = '<span class="credit-na">—</span>';
  } else if (c.error) {
    creditHtml = `<span class="credit-na" title="${esc(String(c.error))}">查询失败</span>`;
  } else {
    creditHtml = `<span class="credit-num">${fmt(c.total_remain)}</span><span>可用积分</span>`;
    const expiring = expiringCredits(c);
    if (expiring > 0) creditHtml += `<span class="credit-expiring"> (临期${fmt(expiring)})</span>`;
  }

  // 操作区（图标按钮，与 wild-work 同款）
  const ops = isAnon
    ? '<span class="icon-op off" title="匿名渠道，固定账号，不可停用/删除" onclick="return false">🔒</span>'
    : `${cur ? '<span class="tag cur">当前使用</span>' : `<span class="icon-op" title="设为当前使用" onclick="switchAccount('${esc(ch.kind)}','${esc(a.uid)}',this)">★</span>`}
       <span class="icon-op" title="刷新积分" onclick="refreshCredits('${esc(ch.kind)}',this)">↻</span>
       <span class="icon-op danger" title="删除账号" onclick="delAccount('${esc(ch.kind)}','${esc(a.uid)}',${(ch.accounts || []).length})">✕</span>`;

  const uidShort = String(a.uid || '').slice(0, 16);
  const texp = a.token_expires_at
    ? new Date(a.token_expires_at < 1e12 ? a.token_expires_at * 1000 : a.token_expires_at).toLocaleString('zh-CN')
    : '';
  const detail = isAnon
    ? '<div class="pkg-note">匿名免费通道 · 无需账号与积分</div>'
    : renderChannelCredits(c);
  return `<div class="strip-card${disabledCls}" data-key="${esc(ch.kind)}:${esc(a.uid)}">
    <div class="sc-grip" title="拖动调整顺序" draggable="true">⠿⠿</div>
    <div class="sc-side">
      <div class="acct-top">
        <div><span class="badge-ch ${esc(ch.kind)}">${esc(ch.name)}</span>
          <span class="acct-name" title="${esc(a.nickname || a.uid)}">${esc(a.nickname || uidShort)}</span></div>
        <div class="acct-ops">${ops}</div>
      </div>
      <div class="acct-uid">UID: ${esc(uidShort)}${texp ? ' · token 到期 ' + esc(texp) : ''}${a.token_expired ? ' · <b style="color:#ef4444">token 待刷新</b>' : ''}${cooldown ? ' · 冷却中 ' + Math.ceil(a.cooldown_remaining/60) + ' 分钟' : ''}</div>
      <div class="acct-credits" title="点击查看积分构成摘要"
           onclick="showCreditBreakdown(event,'${esc(ch.kind)}','${esc(a.uid)}')">${creditHtml}</div>
      <div class="acct-checkin"><span class="tag neutral">${(ch.models || []).length} 个模型</span></div>
    </div>
    <div class="sc-main">${detail}</div>
  </div>`;
}

/* 渠道空态长条卡（保留「去添加账号」入口） */
function emptyChannelCard(ch) {
  return `<div class="strip-card empty-card" data-key="empty:${esc(ch.kind)}">
    <div class="sc-grip" style="visibility:hidden">⠿⠿</div>
    <div class="sc-side">
      <div class="acct-top"><div><span class="badge-ch ${esc(ch.kind)}">${esc(ch.name)}</span></div></div>
      <div class="acct-uid">${(ch.models || []).length ? esc(String((ch.models || []).length)) + ' 个模型已就绪，' : ''}登录后即可使用该平台额度</div>
    </div>
    <div class="sc-main" style="display:flex; align-items:center;">
      <button class="mini2" onclick="startLogin('${esc(ch.kind)}','${esc(ch.name)}','${esc(ch.login_mode || 'poll')}')">＋ 添加账号</button>
    </div>
  </div>`;
}

/* ---- 统一长条卡渲染（WorkBuddy + 各渠道），支持拖动排序（localStorage 记忆） ---- */
function acctOrder() {
  try { return JSON.parse(localStorage.getItem('wb-acct-order') || '[]') || []; }
  catch (e) { return []; }
}
function saveAcctOrder(keys) {
  try { localStorage.setItem('wb-acct-order', JSON.stringify(keys)); } catch (e) {}
}
function renderStrip() {
  const box = document.getElementById('acct-strip');
  if (!box) return;
  const items = [];
  for (const a of (window.__wbAccounts || [])) {
    items.push({ key: 'workbuddy:' + a.uid, html: wbCard(a) });
  }
  let count = (window.__wbAccounts || []).length;
  for (const ch of (window.__channelsCache || [])) {
    if (ch.kind === 'workbuddy') continue;   // WorkBuddy 账号已由 account-status 渲染，避免重复
    for (const a of (ch.accounts || [])) {
      items.push({ key: ch.kind + ':' + a.uid, html: acctCard(ch, a) });
      count++;
    }
    if (ch.can_add && !(ch.accounts || []).length) {
      items.push({ key: 'empty:' + ch.kind, html: emptyChannelCard(ch) });
    }
  }
  // 按用户拖动保存的顺序排（未记录的按默认顺序排在后面，sort 稳定）
  const order = acctOrder();
  const rank = k => { const i = order.indexOf(k); return i === -1 ? order.length + 1e9 : i; };
  items.sort((x, y) => rank(x.key) - rank(y.key));
  box.innerHTML = items.length ? items.map(i => i.html).join('')
    : '<div class="empty">还没有账号，点击右上角「＋ 渠道」按钮添加</div>';
  const cnt = document.getElementById('acct-count');
  if (cnt) cnt.textContent = count + ' 个账号';
  bindStripDrag(box);
}
/* 长条卡拖动排序：事件委托到容器，drop 后按 DOM 顺序保存 */
function bindStripDrag(box) {
  if (!box || box.__dragBound) return;
  box.__dragBound = true;
  let dragging = null;
  const clearMarks = () => {
    box.querySelectorAll('.strip-card').forEach(c => c.classList.remove('drag-over'));
  };
  box.addEventListener('dragstart', e => {
    const card = e.target.closest ? e.target.closest('.strip-card') : null;
    if (!card || (e.target.closest && e.target.closest('button,a,input,select,.icon-op'))) {
      e.preventDefault(); return;
    }
    dragging = card;
    card.classList.add('dragging');
    try { e.dataTransfer.setData('text/plain', card.dataset.key || ''); } catch (err) {}
    e.dataTransfer.effectAllowed = 'move';
  });
  box.addEventListener('dragover', e => {
    if (!dragging) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const over = e.target.closest ? e.target.closest('.strip-card') : null;
    clearMarks();
    if (over && over !== dragging) over.classList.add('drag-over');
  });
  box.addEventListener('drop', e => {
    if (!dragging) return;
    e.preventDefault();
    const over = e.target.closest ? e.target.closest('.strip-card') : null;
    if (over && over !== dragging) {
      const rect = over.getBoundingClientRect();
      const before = (e.clientY - rect.top) < rect.height / 2;
      box.insertBefore(dragging, before ? over : over.nextSibling);
      saveAcctOrder(Array.from(box.querySelectorAll('.strip-card'))
        .map(c => c.dataset.key).filter(Boolean));
    }
  });
  box.addEventListener('dragend', () => {
    if (dragging) { dragging.classList.remove('dragging'); dragging = null; }
    clearMarks();
  });
}

/* 临期积分小计（资源包 ≤2 天到期） */
function expiringCredits(c) {
  const now = Date.now(), DAY = 86400000;
  let sum = 0;
  for (const p of (c.packages || [])) {
    if (!p.cycle_end) continue;
    const ts = new Date(p.cycle_end.replace(' ', 'T')).getTime();
    if (!ts) continue;
    if (Math.ceil((ts - now) / DAY) <= 2) sum += (p.remain || 0);
  }
  return sum;
}

/* 点击积分 → 弹出资源包构成明细 */
async function showCreditBreakdown(ev, kind, uid) {
  const chData = (window.__channelsCache || []).find(c => c.kind === kind);
  const acc = chData && (chData.accounts || []).find(x => x.uid === uid);
  const c = acc && acc.credits;
  if (!c || c.error || !(c.packages || []).length) return;
  const lines = c.packages.map(p => {
    const end = p.cycle_end ? ` · ${p.cycle_end}` : '';
    return `  ${p.name}: ${p.remain}/${p.size}${end}`;
  }).join('\\n');
  alert(`${acc.nickname || uid} 的积分构成\\n\\n合计 ${c.total_remain} / ${c.total_size}\\n\\n${lines}`);
}

/* 刷新单个渠道的积分（重新拉取 /v1/channels） */
async function refreshCredits(kind, btn) {
  if (btn) { btn.style.opacity = '.5'; }
  await loadChannels();
}

async function refreshAllCredits() {
  await loadChannels();
}

/* 一键签到：为账号池内全部 WorkBuddy 账号执行每日签到 */
async function checkinAll(btn) {
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '签到中…'; }
  try {
    const r = await (await fetch('/v1/checkin', {method: 'POST'})).json();
    if (r.detail) throw new Error(r.detail.error?.message || JSON.stringify(r.detail));
    pageToast(`🎁 签到完成：成功 ${r.ok_count}/${r.total}`, 4200);
    // 无论成功失败都弹出分组结果（渠道 → 账号 → 状态 + 失败原因注释）
    showCheckinResult(r);
  } catch (e) {
    showNotice('签到失败', String(e.message || e), 'err');
  }
  if (btn) { btn.disabled = false; btn.textContent = label; }
  loadAll();   // 刷新签到状态与积分
}

/* 小公告弹窗：kind = 'warn'(默认) | 'err' | 'ok'；html=true 时 body 按 HTML 渲染 */
function showNotice(title, body, kind, html) {
  const ov = document.getElementById('notice-overlay');
  if (!ov) { alert(title + '\\n\\n' + String(body).replace(/<[^>]*>/g, '')); return; }
  const ic = document.getElementById('notice-ic');
  ic.className = 'notice-ic' + (kind === 'err' ? ' err' : kind === 'ok' ? ' ok' : '');
  ic.textContent = kind === 'err' ? '✕' : kind === 'ok' ? '✓' : '!';
  document.getElementById('notice-title').textContent = title;
  const box = document.getElementById('notice-box');
  if (box) box.classList.toggle('wide', !!html);
  const bd = document.getElementById('notice-body');
  if (html) { bd.innerHTML = body; } else { bd.textContent = body; }
  ov.classList.remove('hidden');
}
function closeNotice() {
  const ov = document.getElementById('notice-overlay');
  if (ov) ov.classList.add('hidden');
}

/* 签到失败原因 → 人话注释（按渠道/错误特征区分） */
function checkinReason(channel, msg) {
  const m = String(msg || '');
  if (/无可用签到活动/.test(m)) {
    return { level: 'info', text: '<b>活动未开始：</b>该渠道的签到活动通常上午 10:00 后才创建，'
      + '在活动生成前签到会返回此提示，属正常现象，稍后再点一次即可。' };
  }
  if (/登录态失效|401/.test(m)) {
    return { level: 'err', text: '<b>需要重新登录：</b>账号登录态已失效，'
      + '请到「账号管理」删除该账号后重新添加。' };
  }
  if (/未开放签到活动/.test(m)) {
    return { level: 'info', text: '<b>无签到资格：</b>该账号未开放签到活动，无需处理。' };
  }
  if (/token 不可用|token 刷新/.test(m)) {
    return { level: 'err', text: '<b>凭证失效：</b>账号 token 不可用，建议重新登录该账号。' };
  }
  if (/HTTP 5|网络|异常|timeout|Timeout/.test(m)) {
    return { level: 'err', text: '<b>网络/上游异常：</b>请求未能完成，稍后重试；'
      + '若持续失败请检查网络或上游状态。' };
  }
  return { level: 'err', text: '<b>签到未成功：</b>' + esc(m || '未知原因') };
}

/* 签到结果弹窗：一级标题=渠道，二级=账号，右侧状态，失败账号下方附原因注释 */
function showCheckinResult(r) {
  const results = r.results || [];
  // 按渠道分组（保持后端返回顺序，同渠道内保持账号顺序）
  const groups = [];
  const idx = {};
  for (const x of results) {
    const ch = x.channel || 'WorkBuddy';
    if (!(ch in idx)) { idx[ch] = groups.length; groups.push({ ch, items: [] }); }
    groups[idx[ch]].items.push(x);
  }
  let html = '';
  for (const g of groups) {
    const okN = g.items.filter(x => x.ok).length;
    html += '<div class="ck-group"><div class="ck-ch">'
         + `<span class="ck-ch-name">${esc(g.ch)}</span>`
         + `<span class="ck-ch-sum">${okN}/${g.items.length} 成功</span></div>`;
    for (const x of g.items) {
      html += '<div class="ck-acct">'
           + `<span class="ck-name">${esc(x.nickname || x.uid || '未知账号')}</span>`
           + (x.uid && x.nickname ? `<span class="ck-uid">${esc(String(x.uid).slice(0, 12))}</span>` : '')
           + `<span class="ck-status ${x.ok ? 'ok' : 'bad'}">`
           + (x.ok ? '✅ ' + esc(x.msg || '签到成功') : '❌ 未成功') + '</span></div>';
      if (!x.ok) {
        const why = checkinReason(g.ch, x.msg);
        html += `<div class="ck-note${why.level === 'info' ? ' info' : ''}">${why.text}</div>`;
      }
    }
    html += '</div>';
  }
  const allOk = !results.some(x => !x.ok);
  showNotice(allOk ? '签到完成' : '签到结果', html, allOk ? 'ok' : 'warn', true);
}

/* ---- 登录浮条辅助：等待状态悬浮于页面底部，不占文档流 ---- */
function pageToast(msg, ms) {
  const t = document.getElementById('page-toast');
  if (!t) { return; }
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add('hidden'), ms || 3200);
}
function loginToastShow(name, statusText, showCallbackBtn) {
  const box = document.getElementById('login-toast');
  if (!box) { return; }
  document.getElementById('login-toast-text').textContent = `添加 ${name} 账号 — ${statusText}`;
  const cbBtn = document.getElementById('login-toast-callback');
  if (cbBtn) cbBtn.style.display = showCallbackBtn ? '' : 'none';
  box.classList.remove('hidden');
}
function loginToastStatus(text) {
  const el = document.getElementById('login-toast-text');
  if (el && loginCtx) el.textContent = `添加 ${loginCtx.name} 账号 — ${text}`;
}
function loginToastHide() {
  const box = document.getElementById('login-toast');
  if (box) box.classList.add('hidden');
}
function reopenLogin() {
  if (loginCtx && loginCtx.url) { window.open(loginCtx.url, '_blank'); }
  else { pageToast('登录会话已结束，请重新点击「＋ 渠道」添加'); }
}

async function startLogin(kind, name, loginMode) {
  /* 在用户手势内先开新窗口（fetch 异步后再开会被弹窗拦截），随后把授权页地址塞进去：
     点击「＋ 渠道」即直接弹出浏览器新窗口完成登录步骤，页面上只显示等待浮条。 */
  const w = window.open('', '_blank');
  try {
    const r = await (await fetch('/v1/channels/' + kind + '/login/start')).json();
    if (!r.url) { if (w) w.close(); alert('发起登录失败: ' + JSON.stringify(r)); return; }
    if (w) { w.location.href = r.url; }
    loginCtx = { kind, session: r.session, timer: null, url: r.url, name };
    const mode = r.login_mode || loginMode || 'poll';
    const needCallback = mode === 'callback';      // 纯手工粘贴
    const showCallbackBtn = needCallback || mode === 'auto_callback';
    const initial = w
      ? (r.prerequisite ? `新窗口已打开；${r.prerequisite}` : '新窗口已打开，请在窗口中完成授权，完成后本页自动确认…')
      : '浏览器拦截了新窗口，请点「重新打开窗口」重试';
    loginToastShow(name, initial, showCallbackBtn);
    if (r.prerequisite_url) { loginCtx.prerequisite_url = r.prerequisite_url; }
    if (loginCtx.timer) clearInterval(loginCtx.timer);
    // 轮询式登录（workbuddy/qoder/trae-auto_callback）定时查状态；纯 callback 式靠粘贴
    if (mode !== 'callback') loginCtx.timer = setInterval(pollLogin, 3000);
  } catch (e) { if (w) w.close(); alert('发起登录失败: ' + e); }
}
function closeLogin() {
  if (loginCtx && loginCtx.timer) clearInterval(loginCtx.timer);
  loginCtx = null;
  loginToastHide();
  loadChannels();
}
async function pollLogin() {
  if (!loginCtx) return;
  try {
    const r = await (await fetch('/v1/channels/' + loginCtx.kind + '/login/poll', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session: loginCtx.session})
    })).json();
    if (r.status === 'ok') {
      loginToastHide();
      pageToast('✅ ' + (r.message || '登录成功'), 4200);
      if (loginCtx.timer) clearInterval(loginCtx.timer);
      loginCtx = null;
      loadChannels();
      return;
    }
    if (r.status === 'error') {
      if (loginCtx.timer) clearInterval(loginCtx.timer);
      loginToastStatus(r.message || '登录失败，请重试');
      return;
    }
    loginToastStatus(r.message || '等待授权…');
  } catch (e) { /* 网络抖动忽略，下轮重试 */ }
}
async function submitCallback() {
  if (!loginCtx) return;
  const cb = prompt('把授权后浏览器地址栏的整条链接粘贴到这里：');
  if (!cb) return;
  const r = await (await fetch('/v1/channels/' + loginCtx.kind + '/login/submit', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session: loginCtx.session, callback: cb.trim()})
  })).json();
  if (r.status === 'ok') {
    loginToastHide();
    pageToast('✅ ' + (r.message || '登录成功'), 4200);
    if (loginCtx.timer) clearInterval(loginCtx.timer);
    loginCtx = null;
    loadChannels();
  } else {
    pageToast('❌ ' + (r.message || r.status || '提交失败'), 4200);
  }
}
async function switchAccount(kind, uid, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '切换中…'; }
  try {
    const r = await (await fetch('/v1/channels/' + kind + '/accounts/switch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({uid})
    })).json();
    if (!r.ok) alert('切换失败: ' + JSON.stringify(r));
  } catch (e) { alert('切换失败: ' + e); }
  loadChannels();
}
async function delAccount(kind, uid, total) {
  if (total !== undefined && total <= 1) {
    alert('这是该渠道唯一的账号，删除后渠道将不可用。\\n如确认要删除，请先在「账号管理」添加替代账号。');
    return;
  }
  if (!confirm('确认删除该账号？此操作不可撤销。')) return;
  const r = await (await fetch('/v1/channels/' + kind + '/accounts/delete', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({uid})
  })).json();
  if (!r.ok) alert('删除失败: ' + JSON.stringify(r));
  loadChannels();
}

/* ---- 流量监测 ---- */
function barTriple(c, u, o) {
  const tot = (c + u + o) || 1;
  return `<div class="hbar">
    <i class="hc" style="width:${c*100/tot}%"></i>
    <i class="hu" style="width:${u*100/tot}%"></i>
    <i class="ho" style="width:${o*100/tot}%"></i>
  </div>`;
}
// name 允许传 HTML（模型标签带平台徽标），故调用方需自行转义
function usageRow(name, b, extra) {
  const inp = (b.cached || 0) + (b.uncached || 0);
  const hit = inp ? ((b.cached || 0) * 100 / inp).toFixed(1) + '%' : '—';
  return `<div class="trow">
    <div class="tn" title="${esc(String(name).replace(/<[^>]*>/g, ''))}">${name}${extra || ''}</div>
    ${barTriple(b.cached, b.uncached, b.completion)}
    <div class="tv">${fmt(b.total)}</div>
    <div class="tth">${fmt(b.cached)}</div>
    <div class="tth">${fmt(b.uncached)}</div>
    <div class="tth">${fmt(b.completion)}</div>
    <div class="tth">${hit}</div>
    <div class="tth cr">${(b.credits || 0).toFixed(2)}</div>
  </div>`;
}
// 渠道配色（曲线图与图例共用，保证同一渠道在各处颜色一致）
const CH_COLORS = {
  // 用户指定配色：WorkBuddy 绿 / Qoder 淡绿偏黄 / TraeWork 灰黑带亮白
  workbuddy: '#16a34a', qoder: '#a3b04a',
  trae: '#3f4653', oczen: '#94a3b8', gemini: '#ea580c', openai: '#10a37f',
};
const CH_NAMES = {workbuddy:'WorkBuddy', qoder:'Qoder', oczen:'OpenCodeZen', trae:'TraeWork'};
function chColor(c) { return CH_COLORS[c] || '#' + ((c.charCodeAt(0)*2654435761) % 0xffffff).toString(16).padStart(6,'0'); }
function chName(c) { return CH_NAMES[c] || c; }

let statsRange = {mode: 'today', start: '', end: ''};   // 时间区间
let statsMetric = 'total';                              // 曲线指标：total | credits

function _ymd(d) {
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0')
       + '-' + String(d.getDate()).padStart(2,'0');
}
function _rangeParams() {
  const today = new Date();
  if (statsRange.mode === 'today') return {start: _ymd(today), end: _ymd(today)};
  if (statsRange.mode === 'week') {
    const d = new Date(today); d.setDate(d.getDate() - ((d.getDay() + 6) % 7));  // 周一
    return {start: _ymd(d), end: _ymd(today)};
  }
  if (statsRange.mode === 'month') {
    const d = new Date(today.getFullYear(), today.getMonth(), 1);
    return {start: _ymd(d), end: _ymd(today)};
  }
  if (statsRange.mode === 'custom') {
    const s = document.getElementById('range-start').value;
    const e = document.getElementById('range-end').value;
    return {start: s || '', end: e || ''};
  }
  return {start: '', end: ''};   // all
}
function setRange(mode) {
  statsRange.mode = mode;
  document.querySelectorAll('#traffic-range .chip').forEach(b =>
    b.classList.toggle('on', b.dataset.r === mode));
  loadStats();
}
function setMetric(m) {
  statsMetric = m;
  document.querySelectorAll('#metric-switch .chip').forEach(b =>
    b.classList.toggle('on', b.dataset.m === m));
  loadStats();
}

/* 折线图（纯 SVG，无外部依赖）：x=日期，y=指标，每条线一个渠道 */
function renderChart(d) {
  const box = document.getElementById('traffic-chart');
  const legend = document.getElementById('traffic-legend');
  const dc = d.day_channel || {};
  const days = Object.keys(dc).sort();
  const metric = statsMetric;
  const isCredit = metric === 'credits';

  if (!days.length) {
    box.innerHTML = '<div class="empty">该区间暂无数据</div>';
    legend.innerHTML = '';
    return;
  }
  // 收集所有渠道
  const chSet = new Set();
  for (const day of days) for (const c of Object.keys(dc[day] || {})) chSet.add(c);
  const chs = [...chSet];
  const val = (day, ch) => {
    const b = (dc[day] || {})[ch];
    if (!b) return 0;
    return isCredit ? (b.credits || 0) : (b.total || 0);
  };
  let maxV = 0;
  for (const day of days) for (const ch of chs) maxV = Math.max(maxV, val(day, ch));
  if (maxV <= 0) {
    box.innerHTML = `<div class="empty">该区间内${isCredit ? '无积分' : '无 token'}消耗</div>`;
    legend.innerHTML = '';
    return;
  }

  const W = 720, H = 240, PL = 58, PR = 14, PT = 14, PB = 34;
  const iw = W - PL - PR, ih = H - PT - PB;
  const n = days.length;
  const x = i => PL + (n === 1 ? iw / 2 : (iw * i) / (n - 1));
  const y = v => PT + ih - (ih * v) / maxV;
  const fmtV = v => isCredit ? v.toFixed(2)
    : (v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1e3 ? (v/1e3).toFixed(0)+'k' : String(Math.round(v)));

  // Y 轴网格（4 条）
  let grid = '';
  for (let i = 0; i <= 4; i++) {
    const gv = (maxV * i) / 4, gy = y(gv);
    grid += `<line x1="${PL}" y1="${gy}" x2="${W-PR}" y2="${gy}" stroke="#eef0f2" stroke-width="1"/>`
          + `<text x="${PL-8}" y="${gy+4}" text-anchor="end" class="cax">${fmtV(gv)}</text>`;
  }
  // X 轴标签（最多 8 个，避免拥挤；单点时显示完整日期）
  const step = Math.max(1, Math.ceil(n / 8));
  let xlab = '';
  for (let i = 0; i < n; i += step) {
    const lab = n === 1 ? days[i] : days[i].slice(5);
    xlab += `<text x="${x(i)}" y="${H-12}" text-anchor="middle" class="cax">${lab}</text>`;
  }

  // 各渠道折线 + 数据点
  let lines = '';
  for (const ch of chs) {
    const col = chColor(ch);
    const pts = days.map((day, i) => `${x(i)},${y(val(day, ch))}`).join(' ');
    // 面积填充（半透明，便于多条线区分）
    const area = `M ${x(0)},${y(0)} L ` + days.map((day, i) => `${x(i)},${y(val(day, ch))}`).join(' L ')
               + ` L ${x(n-1)},${y(0)} Z`;
    lines += `<path d="${area}" fill="${col}" opacity="0.08"/>`
           + `<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2.2"
                stroke-linejoin="round" stroke-linecap="round"/>`;
    days.forEach((day, i) => {
      const v = val(day, ch);
      if (v > 0) {
        lines += `<circle cx="${x(i)}" cy="${y(v)}" r="3.2" fill="#fff" stroke="${col}" stroke-width="2">`
               + `<title>${esc(chName(ch))} · ${esc(day)} · ${fmtV(v)}${isCredit ? ' 积分' : ' tokens'}</title></circle>`;
      }
    });
  }

  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="chart" preserveAspectRatio="xMidYMid meet">
    ${grid}${xlab}${lines}
  </svg>`;

  // 图例（含区间合计，避免额外开表）
  legend.innerHTML = chs.map(ch => {
    let sum = 0;
    for (const day of days) sum += val(day, ch);
    return `<span class="lg"><i style="background:${chColor(ch)}"></i>${esc(chName(ch))}
      <b>${isCredit ? sum.toFixed(4) : fmt(sum)}</b></span>`;
  }).join('');
}

async function loadStats() {
  try {
    const rp = _rangeParams();
    const qs = new URLSearchParams();
    if (rp.start) qs.set('start', rp.start);
    if (rp.end) qs.set('end', rp.end);
    const d = await (await fetch('/v1/stats?' + qs.toString())).json();
    window.__lastStats = d;   // 供渠道缩放重渲染复用（避免重复请求）
    document.getElementById('traffic-updated').textContent =
      '更新于 ' + new Date().toLocaleTimeString();
    // 自定义区间的输入框回填
    if (statsRange.mode === 'custom') {
      if (rp.start) document.getElementById('range-start').value = rp.start;
      if (rp.end) document.getElementById('range-end').value = rp.end;
    }
    const t = d.totals || {};
    const rng = d.range || {};
    // 区间说明：区分「选定区间」（自然天数）与「有数据的天数」，避免误以为筛选失效
    const spanTxt = (rng.span_days && rng.span_days !== rng.days)
      ? `${rng.span_days} 天区间 · ${rng.days} 天有数据`
      : `${rng.days} 天`;
    document.getElementById('chart-note').textContent =
      `${rng.start || '最早'} ~ ${rng.end || '最新'} · ${spanTxt}`;
    // 顶部总览：Token 可统一统计；积分按渠道分开（各平台口径/币值不同，合并无意义）
    const chAll = d.by_channel || {};
    const chKeys = Object.keys(chAll).sort((a, b) =>
      (chAll[b].total || 0) - (chAll[a].total || 0));
    const creditCards = chKeys.map(k => {
      const b = chAll[k];
      return `<div class="stat cred-ch"><div class="v">${(b.credits || 0).toFixed(2)}</div>
        <div class="k">${esc(chName(k))} 积分</div></div>`;
    }).join('');
    document.getElementById('traffic-stats').innerHTML = `
      <div class="stat"><div class="v">${fmt(t.requests)}</div><div class="k">请求数</div></div>
      <div class="stat"><div class="v">${fmt(t.total)}</div><div class="k">总 tokens</div></div>
      ${creditCards || '<div class="stat cred-ch"><div class="v">0.00</div><div class="k">消耗积分</div></div>'}`;

    renderChart(d);

    // 渠道与模型合并：渠道为一级（可缩放展开），其下嵌套该渠道的各模型
    renderChannelGroups(chAll, d.by_model || []);

    // 日志请求（独立于统计的分页日志）
    loadLogs(0);
  } catch (e) {
    document.getElementById('traffic-stats').innerHTML =
      `<div class="empty">加载失败: ${esc(String(e))}</div>`;
  }
}

/* ---- 渠道分组渲染（渠道一级 + 模型二级，支持缩放与拖动排序） ---- */
let chExpanded = {};    // 渠道 → 是否展开（用户点击后记忆）
function chOrder() {
  try { return JSON.parse(localStorage.getItem('wb-ch-order') || '[]') || []; }
  catch (e) { return []; }
}
function saveChOrder(keys) {
  try { localStorage.setItem('wb-ch-order', JSON.stringify(keys)); } catch (e) {}
}
function renderChannelGroups(chAll, byModel) {
  const box = document.getElementById('traffic-channels');
  if (!box) return;
  let keys = Object.keys(chAll || {});
  if (!keys.length) { box.innerHTML = '<div class="empty">暂无数据</div>'; return; }
  // 按用户拖动保存的顺序排（未记录的排在后面，保持默认顺序）
  const order = chOrder();
  const rank = k => { const i = order.indexOf(k); return i === -1 ? order.length + 1e9 : i; };
  keys = keys.slice().sort((a, b) => rank(a) - rank(b));
  // 模型按渠道归组（模型键形如 channel/model）
  const modelsOf = {};
  for (const [mk, b] of byModel) {
    const i = String(mk).indexOf('/');
    const ch = i < 0 ? 'workbuddy' : mk.slice(0, i);
    const name = i < 0 ? mk : mk.slice(i + 1);
    (modelsOf[ch] = modelsOf[ch] || []).push([name, b]);
  }
  box.innerHTML = keys.map(k => {
    const b = chAll[k];
    const ms = modelsOf[k] || [];
    const open = !!chExpanded[k];
    const m = (label, val, cls) =>
      `<span class="m${cls ? ' ' + cls : ''}">${label} <b>${val}</b></span>`;
    const inp = (b.cached || 0) + (b.uncached || 0);
    const hit = inp ? ((b.cached || 0) * 100 / inp).toFixed(1) + '%' : '—';
    return `<div class="ch-group" data-ch="${esc(k)}">
      <div class="ch-head" onclick="toggleChannel('${esc(k)}')">
        <span class="ch-grip" title="按住拖动调整渠道顺序" draggable="true"
              onclick="event.stopPropagation()">⠿</span>
        <span class="ch-name" style="color:${chColor(k)}">${esc(chName(k))}</span>
        <span class="ch-cnt">${ms.length} 模型</span>
        <span class="ch-metrics">
          ${m('合计', fmt(b.total))}
          ${m('缓存内', fmt(b.cached))}
          ${m('缓存外', fmt(b.uncached))}
          ${m('输出', fmt(b.completion))}
          ${m('命中率', hit)}
          ${m('积分', (b.credits || 0).toFixed(2), 'cr')}
        </span>
        <span class="ch-zoom" title="${open ? '收起模型明细' : '展开看模型详情'}"
              onclick="event.stopPropagation();toggleChannel('${esc(k)}')">${open ? '−' : '+'}</span>
      </div>
      <div class="ch-body"${open ? '' : ' style="display:none"'}>
        ${ms.length ? ms.map(([name, mb]) => usageRow(esc(name), mb)).join('')
                    : '<div class="ch-empty">该渠道暂无模型级数据</div>'}
      </div>
    </div>`;
  }).join('');
  bindChannelDrag(box);
}
/* 渠道拖动排序：与账号卡同款（事件委托 + localStorage 记忆） */
function bindChannelDrag(box) {
  if (!box || box.__dragBound) return;
  box.__dragBound = true;
  let dragging = null;
  const clearMarks = () => {
    box.querySelectorAll('.ch-group').forEach(c => c.classList.remove('drag-over'));
  };
  box.addEventListener('dragstart', e => {
    const grip = e.target.closest && e.target.closest('.ch-grip');
    if (!grip) { e.preventDefault(); return; }
    dragging = grip.closest('.ch-group');
    if (!dragging) return;
    dragging.classList.add('dragging');
    try { e.dataTransfer.setData('text/plain', dragging.dataset.ch || ''); } catch (err) {}
    e.dataTransfer.effectAllowed = 'move';
  });
  box.addEventListener('dragover', e => {
    if (!dragging) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const over = e.target.closest ? e.target.closest('.ch-group') : null;
    clearMarks();
    if (over && over !== dragging) over.classList.add('drag-over');
  });
  box.addEventListener('drop', e => {
    if (!dragging) return;
    e.preventDefault();
    const over = e.target.closest ? e.target.closest('.ch-group') : null;
    if (!over || over === dragging) return;
    // 互换位置：拖到哪一项上就与那一项对调，其余项保持原位。
    // 实现：先按 DOM 顺序取出全部渠道键，交换两项后按新顺序重排，
    // 避免用 insertBefore 做「插入挤开」造成其他项依次让位。
    const nodes = Array.from(box.querySelectorAll('.ch-group'));
    const i = nodes.indexOf(dragging), j = nodes.indexOf(over);
    if (i < 0 || j < 0 || i === j) return;
    const keys = nodes.map(n => n.dataset.ch);
    [keys[i], keys[j]] = [keys[j], keys[i]];
    const byKey = {};
    nodes.forEach(n => { byKey[n.dataset.ch] = n; });
    keys.forEach(k => { if (byKey[k]) box.appendChild(byKey[k]); });   // 按新顺序重排
    saveChOrder(keys);
  });
  box.addEventListener('dragend', () => {
    if (dragging) { dragging.classList.remove('dragging'); dragging = null; }
    clearMarks();
  });
}
function toggleChannel(k) {
  chExpanded[k] = !chExpanded[k];
  const byModel = (window.__lastStats && window.__lastStats.by_model) || [];
  renderChannelGroups((window.__lastStats || {}).by_channel || {}, byModel);
}
function expandAllChannels(open) {
  const chs = Object.keys((window.__lastStats || {}).by_channel || {});
  chs.forEach(k => { chExpanded[k] = !!open; });
  renderChannelGroups((window.__lastStats || {}).by_channel || {},
                      (window.__lastStats || {}).by_model || []);
}

/* ---- 日志请求（分页，独立于统计） ---- */
let logOffset = 0, logLimit = 100, logTotal = 0;
async function loadLogs(offset) {
  logOffset = Math.max(0, offset || 0);
  const box = document.getElementById('traffic-recent');
  if (!box) return;
  const qs = new URLSearchParams({limit: logLimit, offset: logOffset});
  const ch = (document.getElementById('log-filter-ch') || {}).value || '';
  const q = (document.getElementById('log-search') || {}).value || '';
  if (ch) qs.set('channel', ch);
  if (q) qs.set('q', q);
  try {
    const d = await (await fetch('/v1/logs?' + qs.toString())).json();
    logTotal = d.total || 0;
    const items = d.items || [];
    const head = `<div class="trow head"><div class="tn">时间 / 渠道 / 账号 / 模型</div>
      <div class="tv">输入</div><div class="tth">缓存内</div><div class="tth">缓存外</div>
      <div class="tth">输出</div><div class="tth">积分</div></div>`;
    box.innerHTML = items.length ? head + items.map(r => `<div class="trow">
        <div class="tn">${new Date(r.t*1000).toLocaleString('zh-CN')} ·
          ${esc(chName(r.channel))}${r.account ? ` <b>${esc(r.account)}</b>` : ''} · ${esc(r.model || '')}
          ${r.stream ? '<span class="tag-out">流</span>' : ''}</div>
        <div class="tv">${fmt(r.prompt)}</div>
        <div class="tth">${fmt(r.cached)}</div>
        <div class="tth">${fmt(r.uncached)}</div>
        <div class="tth">${fmt(r.completion)}</div>
        <div class="tth cr">${(r.credits || 0).toFixed(2)}</div>
      </div>`).join('')
      : '<div class="empty">暂无日志（发起对话后自动记录）</div>';
    const note = document.getElementById('log-note');
    if (note) note.textContent = `共 ${fmt(logTotal)} 条`;
    // 分页控件
    const pager = document.getElementById('log-pager');
    if (pager) {
      const pages = Math.max(1, Math.ceil(logTotal / logLimit));
      const cur = Math.floor(logOffset / logLimit) + 1;
      pager.style.display = logTotal > logLimit ? '' : 'none';
      document.getElementById('log-pageinfo').textContent = `${cur} / ${pages} 页`;
      document.getElementById('log-prev').disabled = logOffset <= 0;
      document.getElementById('log-next').disabled = logOffset + logLimit >= logTotal;
    }
    // 渠道筛选下拉（保留当前选中）
    const sel = document.getElementById('log-filter-ch');
    if (sel && !sel.options.length) {
      const chs = Object.keys((window.__lastStats || {}).by_channel || {});
      sel.innerHTML = '<option value="">全部渠道</option>'
        + chs.map(c => `<option value="${esc(c)}">${esc(chName(c))}</option>`).join('');
    }
  } catch (e) {
    box.innerHTML = `<div class="empty">日志加载失败: ${esc(String(e))}</div>`;
  }
}
async function resetLogs() {
  if (!confirm('确认清空全部请求日志？此操作不可撤销（统计不受影响）。')) return;
  const r = await (await fetch('/v1/logs/reset', {method: 'POST'})).json();
  pageToast(`已清空 ${r.cleared || 0} 条日志`, 3200);
  loadLogs(0);
}
async function resetStats() {
  if (!confirm('确认清空全部流量统计？此操作不可撤销。')) return;
  await fetch('/v1/stats/reset', {method: 'POST'});
  loadStats();
}
function renderApi() {
  const pv = 'v1';
  const host = (document.getElementById('api-url').textContent || '').replace(/\\/v1$/, '');
  const p = (host || 'http://127.0.0.1:8787') + '/' + pv;
  const eps = [
    ['POST', '/chat/completions', 'OpenAI Chat — 通用对话/工具调用'],
    ['POST', '/responses', 'OpenAI Responses — Codex CLI'],
    ['POST', '/messages', 'Anthropic Messages — Claude Code'],
  ];
  document.getElementById('api-eps').innerHTML = eps.map(([m, path, desc]) =>
    `<div class="ep"><span class="method ${m.toLowerCase()}">${m}</span><code>${p}${esc(path)}</code><span class="edesc">${desc}</span></div>`).join('');
  const model = (window.latestModels || []).find(x => !x.disabled);
  const modelName = model ? model.name : 'glm-5.2';
  const key = (document.getElementById('api-key').textContent || '').trim();
  const keyHdr = key.startsWith('(') ? '' : `
  -H "Authorization: Bearer ${key}"`;
  document.getElementById('api-curl').textContent =
    `curl ${p}/chat/completions -H "Content-Type: application/json"${keyHdr} -d '{"model":"${modelName}","messages":[{"role":"user","content":"你好"}],"stream":true}'`;
  loadPlatforms();
}
/* 分平台端点表：统一入口 + 各平台专用入口（配合绑定平台密钥分别管理） */
async function loadPlatforms() {
  const box = document.getElementById('api-platforms');
  if (!box) return;
  try {
    const d = await (await fetch('/v1/platforms')).json();
    box.innerHTML = (d.platforms || []).map(pf => {
      const isAll = !pf.kind;
      const modelTxt = pf.models === null ? '全部模型' : `${pf.models} 个模型`;
      const ep = isAll
        ? `${esc(pf.base_url)}/chat/completions`
        : `${esc(pf.base_url)}/chat/completions`;
      return `<div class="ep">
        <span class="method post" style="${isAll ? 'background:#eef2ff;color:#4338ca;' : ''}">POST</span>
        <code>${ep}</code>
        <span class="edesc">${esc(pf.name)} · ${modelTxt}${pf.prefix ? ` · 模型前缀 ${esc(pf.prefix)}` : ''}
          ${isAll ? '' : '（模型名可省略前缀，自动归入本平台）'}</span>
      </div>`;
    }).join('');
  } catch (e) { box.innerHTML = `<div class="empty">加载失败: ${esc(String(e))}</div>`; }
}
(function initV2() {
  const pg = localStorage.getItem('wb-page');
  if (pg && pg !== 'home') showPage(pg);
  const pv = localStorage.getItem('wb-pv');
  if (pv) { const r = document.querySelector(`input[name="pv"][value="${pv}"]`); if (r) r.checked = true; }
})();
async function refreshModels(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '刷新中…'; }
  try {
    const r = await fetch('/v1/models-refresh');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    document.getElementById('models-updated').textContent =
      '已同步 ' + d.count + ' 个模型 · ' + new Date().toLocaleTimeString('zh-CN');
  } catch(e) { alert('刷新失败: ' + e.message); }
  if (btn) { btn.disabled = false; btn.textContent = '↻ 刷新模型列表'; }
  loadModels();
}
async function toggleModel(name) {
  await fetch('/v1/models/delete', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
  loadModels();
}
function copyTxt(id) {
  navigator.clipboard.writeText(document.getElementById(id).textContent.trim());
}
/* ---- API Keys 管理 ---- */
async function loadKeys() {
  let d;
  try { d = await (await fetch('/v1/keys')).json(); }
  catch(e) { return; }
  const fmtT = ts => ts ? new Date(ts * 1000).toLocaleString('zh-CN') : '从未';
  const CH_LABEL = {workbuddy:'WorkBuddy', oczen:'OpenCodeZen', qoder:'Qoder', trae:'TraeWork'};
  const rows = (d.keys || []).map(k => {
    const ch = k.channel || '';
    const chTag = ch
      ? `<span class="chbadge" title="该 Key 仅限此平台使用">${esc(CH_LABEL[ch] || ch)}</span>`
      : '<span class="chbadge all" title="不限制渠道">全部渠道</span>';
    return `<div class="krow">
      <div class="kname" title="${esc(k.name)}">${esc(k.name)}</div>
      ${chTag}
      <code title="点击复制" style="cursor:pointer;" onclick="copyKey('${esc(k.key)}')">${esc(k.key)}</code>
      <div class="ku">最近使用: ${fmtT(k.last_used)}</div>
      <button class="mini warn" onclick="delKey('${esc(k.key)}')">删除</button>
    </div>`;
  }).join('');
  document.getElementById('keys-list').innerHTML =
    rows || '<div class="empty" style="padding:18px;">还没有创建 Key — 当前所有客户端均可免鉴权调用</div>';
  document.getElementById('master-note').textContent =
    d.master_set ? '启动参数主密钥同时有效' : '';
  // 平台下拉（保持当前选择）
  const sel = document.getElementById('key-channel');
  if (sel && d.channel_options) {
    const keep = sel.value;
    sel.innerHTML = d.channel_options.map(o =>
      `<option value="${esc(o.kind)}">${esc(o.name)}</option>`).join('');
    if (keep) sel.value = keep;
  }
  renderDupModels();
}
/* 重名模型可视化：同一模型名出现在多个平台 → 串台风险点 */
async function renderDupModels() {
  const box = document.getElementById('dup-models');
  if (!box) return;
  const all = window.latestModels || [];
  if (!all.length) { box.innerHTML = '<div class="empty">暂无模型数据</div>'; return; }
  const CH_LABEL = {workbuddy:'WorkBuddy', oczen:'OpenCodeZen', qoder:'Qoder', trae:'TraeWork'};
  const byBare = {};
  for (const m of all) {
    const c = (m.meta && m.meta.channel) || 'workbuddy';
    const bare = m.name.includes('/') ? m.name.split('/').slice(1).join('/') : m.name;
    (byBare[bare] = byBare[bare] || []).push(c);
  }
  const dups = Object.entries(byBare).filter(([, cs]) => new Set(cs).size > 1);
  box.innerHTML = dups.length
    ? dups.map(([bare, cs]) => {
        const uniq = [...new Set(cs)];
        return `<div class="trow">
          <div class="tn"><code>${esc(bare)}</code></div>
          <div class="tth" style="min-width:auto;">${uniq.length} 个平台:</div>
          <div style="flex:1; display:flex; gap:4px; flex-wrap:wrap;">
            ${uniq.map(c => `<span class="chbadge">${esc(CH_LABEL[c] || c)}</span>`).join('')}
          </div>
          <div class="tth" style="min-width:150px; color:#b45309;">
            ${uniq.length > 1 ? '写裸名需靠 Key 绑定区分' : ''}
          </div>
        </div>`;
      }).join('')
    : '<div class="empty">未发现跨平台重名模型</div>';
}
function copyKey(k) { navigator.clipboard.writeText(k); }
async function createKey() {
  const name = document.getElementById('key-name').value.trim() || '未命名';
  const channel = document.getElementById('key-channel')?.value || '';
  try {
    const r = await fetch('/v1/keys', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, channel})});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    document.getElementById('key-name').value = '';
    await loadKeys(); await loadModels();
    const chTxt = d.channel ? `（绑定平台: ${d.channel}）` : '（全部渠道）';
    alert('Key 已创建' + chTxt + '(已自动复制):' + String.fromCharCode(10, 10) + d.key);
    navigator.clipboard.writeText(d.key);
  } catch(e) { alert('创建失败: ' + e.message); }
}
async function delKey(key) {
  if (!confirm('删除后该 Key 立即失效,确定删除?')) return;
  try {
    const r = await fetch('/v1/keys/delete', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({key})});
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch(e) { alert('删除失败: ' + e.message); }
  loadKeys(); loadModels();
}
load();
loadModels();
loadChannels();
setInterval(() => { load(); loadChannels(); }, 30000);
</script>
</body>
</html>"""

DEFAULT_MODELS = [
    "deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v3", "deepseek-r1",
    "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "kimi-k2.7", "kimi-k2.6", "kimi-k2.5",
    "minimax-m3", "minimax-m3-pay",
    "hy3", "hy3-preview-agent",
    "auto",
]

# 模型别名映射（允许客户端用简短名称或常见别名直接请求）
MODEL_ALIASES = {
    "v4.1": "deepseek-v4.1-flash",
    "v4.1-flash": "deepseek-v4.1-flash",
    "deepseek-v4.1": "deepseek-v4.1-flash",
    "v4": "deepseek-v4-pro",
    "v4-pro": "deepseek-v4-pro",
    "v4-flash": "deepseek-v4-flash",
    "r1": "deepseek-r1",
    "v3": "deepseek-v3",
}

# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
    # 渠道扩展字段：Qoder 上下文档位提示（chat 链路会读取）
    "context_length", "context_window",
}

# ---------------------------------------------------------------------------
# 多渠道聚合层：Qoder / TraeWork / OpenCodeZen
#   - 账号存于 auths/<channel>/（JSON），由面板「账号管理」添加；
#   - 模型名以渠道前缀路由（qoder/*、trae/*、oczen/*），
#     无前缀的模型名仍走原 WorkBuddy 主链路，完全向后兼容。
# ---------------------------------------------------------------------------
try:
    import channels as CHANNELS
    from channels import get_channel, all_channels
except Exception as _e:  # 依赖缺失时降级：只保留 WorkBuddy 主链路
    sys.stderr.write(f"[warn] 渠道模块加载失败（仅 WorkBuddy 可用）: {_e}\n")
    CHANNELS = None

    def get_channel(kind):  # type: ignore
        return None

    def all_channels():  # type: ignore
        return []


def _split_channel(model: str) -> tuple[str, str]:
    """拆分渠道前缀：'qoder/glm-5.3' → ('qoder', 'glm-5.3')；无前缀返回 ('', model)。"""
    if not model or "/" not in model:
        return "", model
    prefix, rest = model.split("/", 1)
    if get_channel(prefix) is not None:
        return prefix, rest
    return "", model


def _channel_models_snapshot() -> list[dict]:
    """所有渠道的模型清单（面板与 /v1/models 共用）。

    每个模型带渠道前缀，并附上从上游动态获取的规格（上下文 / 最大输出 / 模态）。
    """
    out: list[dict] = []
    for ch in all_channels():
        try:
            if ch.NEEDS_LOGIN and not ch.accounts():
                continue    # 无账号的渠道不暴露模型，避免客户端选到不可用模型
            for spec in ch.models():
                d = spec.to_dict()
                d["id"] = f"{ch.MODEL_PREFIX}{spec.id}"
                d["channel"] = ch.KIND
                d["channel_name"] = ch.DISPLAY_NAME
                out.append(d)
        except Exception as e:
            sys.stderr.write(f"[warn] 渠道 {ch.KIND} 模型枚举失败: {e}\n")
    return out


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

# 项目版本号：与 git tag 一致，便于运行时确认跑的是哪个版本（见 CHANGELOG.md）
PROJECT_VERSION = "V1.2.2"

app = FastAPI(title="codebuddy2openai", version=PROJECT_VERSION)
# 允许 CLIProxyAPI 管理面板(8317)下的 workbuddy 板块页跨源调用本服务
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8317", "http://127.0.0.1:8317"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
CONFIG: dict = {"api_key": "", "pool": None, "log_path": None,
                "desensitize": False, "no_compact": False}  # pool: CredentialPool | None


# ---------------------------------------------------------------------------
# 流量统计（旁路能力：任何异常都不得影响主链路）
# ---------------------------------------------------------------------------

STATS_FILE = Path(__file__).parent / "usage_stats.json"
STATS = UsageStats(STATS_FILE) if UsageStats else None


def _record_usage(*, channel: str, model: str, usage: dict | None,
                  endpoint: str = "chat", account: str = "", stream: bool = False) -> None:
    """记录一次成功请求的 token 消耗（缓存内 / 缓存外分别记账）。"""
    if STATS is None:
        return
    try:
        STATS.record(channel=channel, model=model, usage=usage, endpoint=endpoint,
                     account=account, stream=stream)
    except Exception as e:
        sys.stderr.write(f"[warn] 流量统计失败: {e}\n")


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(msg: str):
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。"""
    path = CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass  # 日志失败不应影响主流程




def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


# ---- 动态 API Key 管理(面板创建/删除,立即生效) ----
KEYS_FILE = Path(__file__).parent / "api_keys.json"
_keys_cache: dict = {"mtime": 0.0, "keys": []}
_keys_last_used: dict = {}   # key -> 最后使用时间(内存,随增删持久化)


def _load_keys() -> list[dict]:
    """读取动态 key 列表(mtime 缓存,避免每请求解析 JSON)。"""
    try:
        mt = KEYS_FILE.stat().st_mtime
    except OSError:
        return []
    if _keys_cache["mtime"] != mt:
        try:
            _keys_cache["keys"] = json.loads(KEYS_FILE.read_text(encoding="utf-8")).get("keys") or []
            _keys_cache["mtime"] = mt
        except Exception:
            pass
    return _keys_cache["keys"]


def _save_keys(keys: list[dict]):
    for k in keys:
        k["last_used"] = _keys_last_used.get(k["key"])
    tmp = KEYS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"keys": keys}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, KEYS_FILE)
    _keys_cache["keys"] = keys
    _keys_cache["mtime"] = KEYS_FILE.stat().st_mtime


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]) -> dict:
    """鉴权：启动参数主密钥，或面板创建的任一动态 key。

    主密钥未设置且不存在动态 key 时不鉴权（本机自用模式）。
    返回鉴权上下文 {"channel": <绑定的渠道|"">, "key_name": <备注>}：
    绑定渠道的 key 只能访问该渠道的模型与端点（见 _enforce_key_scope）。
    """
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    master = CONFIG.get("api_key") or ""
    keys = _load_keys()
    # 鉴权开关:设置了主密钥,或创建过 Key(api_keys.json 存在)即启用;全删后所有请求 401
    auth_on = bool(master) or KEYS_FILE.exists()
    if not auth_on:
        return {"channel": "", "key_name": "（未启用鉴权）"}
    if master and token == master:
        return {"channel": "", "key_name": "（主密钥）"}
    for k in keys:
        if not k.get("disabled") and k.get("key") == token:
            if token:
                _keys_last_used[token] = int(time.time())
            return {"channel": str(k.get("channel") or ""), "key_name": k.get("name") or ""}
    raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _key_channel_of(authorization: Optional[str], x_api_key: Optional[str]) -> str:
    """取当前请求 key 绑定的渠道（"" = 全部渠道可用）。"""
    return str(_check_auth(authorization, x_api_key).get("channel") or "")


def _enforce_key_scope(channel: str, model: str) -> str:
    """校验并**自动归属**「绑定渠道的 key」的模型，返回最终应使用的模型名。

    同名模型（如 deepseek-v4-pro、glm-5.3 在 WorkBuddy 与 Qoder 都存在）是串台风险源：
    不带前缀的裸名默认按 WorkBuddy 处理，会把「想用阿里的 DeepSeek」静默送到腾讯。

    本函数按 key 的绑定渠道消歧：
      - 未绑定（""）/主密钥：不做限制与改写（保持原行为）
      - 绑定渠道 X + 显式前缀 Y：Y != X → 403（明确拒绝跨平台）
      - 绑定渠道 X + 裸名：自动改写为 `X/<模型>`，即**裸名永远归属该 key 的平台**
        （WorkBuddy 无前缀，保持原样）
    这样一把 Qoder 的 key 无论写 `glm-5.3` 还是 `qoder/glm-5.3`，都只可能打到阿里侧。
    """
    if not channel:
        return model
    if "/" in model:
        prefix = model.split("/", 1)[0]
        if prefix == "workbuddy" or get_channel(prefix) is not None:
            if prefix != channel:
                raise HTTPException(status_code=403, detail={"error": {
                    "message": f"该 API Key 仅限 {channel} 渠道使用，"
                               f"但请求的模型 {model!r} 明确属于 {prefix} 渠道",
                    "type": "permission_error"}})
            return model
    # 裸名（或未知前缀）：按绑定渠道归属
    if channel == "workbuddy":
        return model
    return f"{channel}/{model}"


def _normalize_key_channel(channel: str) -> str:
    """规范化密钥绑定的渠道标识；未知渠道直接拒绝。"""
    c = (channel or "").strip().lower()
    if not c:
        return ""
    if c == "workbuddy":
        return "workbuddy"
    if get_channel(c) is not None:
        return c
    raise HTTPException(status_code=400, detail={"error": {
        "message": f"未知渠道: {channel}", "type": "invalid_request_error"}})


def _pool() -> CredentialPool:
    if CONFIG["pool"] is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
    return CONFIG["pool"]


def _safe_headers(pool: CredentialPool, cred: CredentialManager, rid: str, model_name: str) -> dict | None:
    """取账号请求头；token 刷新失败视为该账号不可用（冷却并返回 None）。"""
    prefix = f"[{rid}] " if rid else ""
    try:
        return cred.get_headers()
    except Exception as e:
        nick = _safe_nickname(cred)
        _log(f"{prefix}✗ 账号[{nick}] 凭据不可用（{e}），冷却并尝试切换")
        pool.report_failure(cred, 401, str(e))
        return None


def _safe_nickname(cred: CredentialManager) -> str:
    try:
        return cred.summary().get("nickname") or cred.path.name
    except Exception:
        return cred.path.name


@app.get("/health")
def health():
    pool: CredentialPool = CONFIG["pool"]
    info: dict = {"status": "ok", "version": PROJECT_VERSION, "platform": sys.platform,
                  "python": sys.version.split()[0],
                  "auth_dirs": [str(d) for d in auth_dirs() if d.is_dir()],
                  "mode": "direct-proxy (native function calling, multi-account pool)"}
    if pool is not None:
        info["accounts"] = pool.snapshot()
        info["accounts_total"] = len(pool)
    return info


# ---------------------------------------------------------------------------
# 账号池可视化面板（/panel + /v1/account-status）
# ---------------------------------------------------------------------------

CREDIT_URL = "https://www.codebuddy.cn/v2/billing/meter/get-user-resource"
CHECKIN_URL = "https://www.codebuddy.cn/v2/billing/meter/daily-checkin"
CHECKIN_STATE_FILE = Path(__file__).parent / "checkin_state.json"
CREDIT_TTL_SECS = 30
_credit_cache: dict = {}          # uid -> {"t": epoch, "data": dict}
_credit_lock = threading.Lock()


def _fetch_credits(cred: CredentialManager) -> dict:
    """查询账号的 credits 资源包（腾讯 get-user-resource），30s TTL 缓存。

    返回 {"total_remain": int, "total_size": int, "packages": [...], "error": str|None}。
    """
    try:
        info = cred.summary()
        uid = info.get("uid") or cred.path.name
    except Exception:
        uid = cred.path.name
    with _credit_lock:
        hit = _credit_cache.get(uid)
        if hit and time.time() - hit["t"] < CREDIT_TTL_SECS:
            return hit["data"]
    try:
        headers = cred.get_headers()
        # 只查 p_tcaca 产品、状态 0/3、周期未结束的资源包；空 body 会让上游
        # 返回历史/其他产品包，叠加总量字段导致积分虚高。
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": time.strftime("%Y-%m-%d %H:%M:%S"),
            "PackageEndTimeRangeEnd": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 365 * 101 * 86400)
            ),
        }
        with httpx.Client(timeout=15) as c:
            r = c.post(CREDIT_URL, headers=headers, json=body)
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(data.get("msg") or f"HTTP {r.status_code}")
        accounts = (((data.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []
        now = time.time()
        packages = []
        for a in accounts:
            # 只保留未到期的活跃资源包
            try:
                cycle_end_ts = time.mktime(time.strptime(a.get("CycleEndTime", ""), "%Y-%m-%d %H:%M:%S"))
            except Exception:
                cycle_end_ts = None
            if cycle_end_ts is not None and cycle_end_ts < now:
                continue
            # 周期口径优先：周期包按 CycleCapacity* 计（上游按周期扣费，
            # Capacity* 是累计口径，含已刷新周期的旧额度）；无周期数据才回退。
            cyc_size = int(a.get("CycleCapacitySize") or 0)
            cyc_remain = int(a.get("CycleCapacityRemain") or 0)
            cyc_used = int(a.get("CycleCapacityUsed") or 0)
            if cyc_size > 0:
                size, used, remain = cyc_size, cyc_used, cyc_remain
            elif cyc_remain > 0 or cyc_used > 0:
                size, used, remain = cyc_remain + cyc_used, cyc_used, cyc_remain
            else:
                size = int(a.get("CapacitySize") or 0)
                used = int(a.get("CapacityUsed") or 0)
                remain = int(a.get("CapacityRemain") or 0)
            remain = max(remain, 0)
            if remain <= 0:
                continue   # 已耗尽的历史资源包不在面板展示
            packages.append({
                "name": a.get("PackageName") or "资源包",
                "remain": remain,
                "used": used,
                "size": size,
                "unit": a.get("CapacityUnit") or "credits",
                "cycle_end": a.get("CycleEndTime", ""),
            })
        # 按周期截止时间升序：即将结束的资源包排在前面
        packages.sort(key=lambda p: p["cycle_end"] or "9999-12-31")
        result = {"total_remain": sum(p["remain"] for p in packages),
                  "total_size": sum(p["size"] for p in packages),
                  "packages": packages, "error": None}
    except Exception as e:
        result = {"total_remain": 0, "total_size": 0, "packages": [], "error": str(e)}
    with _credit_lock:
        _credit_cache[uid] = {"t": time.time(), "data": result}
    return result


def _load_checkin_state() -> dict:
    try:
        return json.loads(CHECKIN_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _record_checkin_state(uid: str, nickname: str, ok: bool, msg: str) -> None:
    """写入签到结果（原子替换），供面板展示。"""
    try:
        state = _load_checkin_state()
        state[uid] = {
            "nickname": nickname,
            "date": time.strftime("%Y-%m-%d"),
            "time": time.strftime("%H:%M:%S"),
            "ok": ok,
            "msg": msg,
        }
        tmp = CHECKIN_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CHECKIN_STATE_FILE)
    except Exception as e:
        sys.stderr.write(f"[warn] 写入签到状态失败: {e}\n")


def _checkin_one(cred: CredentialManager) -> dict:
    """对单个 WorkBuddy 账号执行每日签到。返回 {uid, nickname, ok, msg}。"""
    try:
        info = cred.summary()
    except Exception as e:
        return {"uid": cred.path.name, "nickname": cred.path.name, "ok": False,
                "msg": f"凭据读取失败: {e}"}
    uid = info.get("uid") or cred.path.name
    nickname = info.get("nickname") or uid
    try:
        headers = cred.get_headers()   # 必要时自动刷新 token
        with httpx.Client(timeout=15) as c:
            r = c.post(CHECKIN_URL, headers=headers, json={})
            res = r.json()
        code = res.get("code")
        msg = res.get("msg", "")
        if code == 0:
            out = {"uid": uid, "nickname": nickname, "ok": True, "msg": "签到成功"}
        elif "已签到" in msg or code == 10001:
            out = {"uid": uid, "nickname": nickname, "ok": True, "msg": "今日已签到"}
        else:
            out = {"uid": uid, "nickname": nickname, "ok": False,
                   "msg": f"{msg or '签到失败'} (code={code})"}
    except Exception as e:
        out = {"uid": uid, "nickname": nickname, "ok": False, "msg": f"请求异常: {e}"}
    _record_checkin_state(out["uid"], out["nickname"], out["ok"], out["msg"])
    return out


def _checkin_channel(ch, acc) -> dict:
    """对单个渠道账号执行签到（渠道实现 checkin()）。返回 {uid, nickname, ok, msg}。"""
    uid = acc.uid or ""
    nickname = acc.nickname or uid or "未知账号"
    try:
        r = ch.checkin(acc) or {}
        ok = bool(r.get("ok"))
        msg = str(r.get("msg") or ("签到成功" if ok else "签到失败"))
    except Exception as e:
        ok, msg = False, f"异常: {e}"
    if uid:
        _record_checkin_state(uid, nickname, ok, msg)
    return {"uid": uid, "nickname": nickname, "ok": ok, "msg": msg,
            "channel": getattr(ch, "DISPLAY_NAME", ch.KIND)}


def run_all_checkins() -> list[dict]:
    """执行全部渠道签到：WorkBuddy 账号池 + 所有实现 checkin() 的渠道。

    面板「一键签到」与**服务启动时自动签到**共用本函数，保证行为一致。
    未实现 checkin() 的渠道（OpenCodeZen 匿名通道）自动跳过。
    注意：本函数是内部实现，**不是** HTTP 端点（端点见下方 checkin_all）。
    """
    results: list[dict] = []
    pool: CredentialPool | None = CONFIG.get("pool")
    if pool is not None:
        for c in list(pool.creds):
            try:
                results.append(_checkin_one(c))
            except Exception as e:
                results.append({"uid": getattr(c, "path", ""), "nickname": "WorkBuddy",
                                "ok": False, "msg": f"签到异常: {e}",
                                "channel": "WorkBuddy"})
    for ch in all_channels():
        if not hasattr(ch, "checkin"):
            continue
        try:
            for acc in ch.accounts():
                results.append(_checkin_channel(ch, acc))
        except Exception as e:
            results.append({"uid": "", "nickname": ch.DISPLAY_NAME, "ok": False,
                            "msg": f"渠道签到异常: {e}",
                            "channel": getattr(ch, "DISPLAY_NAME", ch.KIND)})
    return results


@app.post("/v1/checkin")
async def checkin_all():
    """手动一键签到：WorkBuddy 账号池 + 支持签到的渠道（Qoder campaigns / Trae UG）。"""
    results = await asyncio.to_thread(run_all_checkins)
    if not results:
        raise HTTPException(status_code=503, detail={"error": {
            "message": "无可签到账号", "type": "unavailable"}})
    ok_n = sum(1 for r in results if r["ok"])
    return {"ok": True, "total": len(results), "ok_count": ok_n, "results": results}


@app.get("/v1/account-status")
def account_status():
    """面板数据接口：账号池状态 + credits 额度 + 今日签到状态。"""
    pool: CredentialPool = CONFIG["pool"]
    today = time.strftime("%Y-%m-%d")
    state = _load_checkin_state()
    accounts = []
    if pool is not None:
        for i, cred in enumerate(pool.creds):
            s = pool.snapshot()[i] if i < len(pool.snapshot()) else {}
            credits = _fetch_credits(cred)
            cs = state.get(s.get("uid") or "", {})
            accounts.append({
                "nickname": s.get("nickname"),
                "uid": s.get("uid"),
                "file": cred.path.name,
                "current": s.get("current", False),
                "cooldown_remaining": s.get("cooldown_remaining", 0),
                "token_expired": s.get("token_expired", False),
                "token_expires_at": s.get("token_expires_at", 0),
                "checkin": {"today": cs.get("date") == today, "ok": cs.get("ok"),
                            "msg": cs.get("msg"), "time": cs.get("time")},
                "credits": credits,
            })
    return {"accounts": accounts, "generated_at": int(time.time())}


@app.post("/v1/account-switch")
async def account_switch(request: Request):
    """手动切换当前使用的账号。请求体: {"uid": "<账号uid或凭据文件名>"}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    uid = (payload or {}).get("uid", "")
    if not uid:
        raise HTTPException(status_code=400, detail={"error": {"message": "uid is required", "type": "invalid_request_error"}})
    pool = _pool()
    if not pool.set_current(uid):
        raise HTTPException(status_code=404, detail={"error": {"message": f"账号不存在: {uid}", "type": "not_found"}})
    cur = pool.get_current()
    _log(f"⇄ 手动切换当前账号 -> {_safe_nickname(cur)} ({uid})")
    return {"ok": True, "current_uid": uid, "accounts": pool.snapshot()}


@app.get("/v1/stats")
def usage_stats(top_models: int = 30, keep_days: int = 365,
                start: str = "", end: str = ""):
    """流量统计：总用量 + 按渠道/模型/天聚合（含缓存命中率与积分）。

    start / end 为 "YYYY-MM-DD"（含端点）；给定区间时各维度按区间重算。
    """
    if STATS is None:
        return {"error": "统计模块不可用", "totals": {}, "by_channel": {},
                "by_model": [], "by_day": [], "recent": []}
    return STATS.snapshot(top_models=max(1, min(top_models, 200)),
                          keep_days=max(0, min(keep_days, 3650)),
                          start=start.strip() or None,
                          end=end.strip() or None)


@app.post("/v1/stats/reset")
def usage_stats_reset():
    """清空流量统计（聚合数据）。**不影响请求日志**——日志独立落盘，供事后排查。"""
    if STATS is not None:
        STATS.reset()
    return {"ok": True, "note": "已清空统计；请求日志保留（可在「日志请求」中查看或单独清空）"}


@app.get("/v1/logs")
def request_logs(limit: int = 200, offset: int = 0,
                 channel: str = "", model: str = "", q: str = ""):
    """请求日志（分页，最新在前）。

    与统计分离：清空统计不会清掉日志。支持按渠道/模型精确过滤与关键字模糊搜索。
    """
    if STATS is None:
        return {"total": 0, "items": [], "offset": offset, "limit": limit}
    return STATS.read_log(limit=limit, offset=offset, channel=channel.strip(),
                          model=model.strip(), q=q.strip())


@app.post("/v1/logs/reset")
def request_logs_reset():
    """清空请求日志（独立于统计）。"""
    if STATS is None:
        return {"ok": True, "cleared": 0}
    return {"ok": True, "cleared": STATS.clear_log()}


# ---------------------------------------------------------------------------
# 渠道账号管理端点：Qoder / TraeWork / OpenCodeZen / WorkBuddy
# ---------------------------------------------------------------------------

# WorkBuddy 登录状态暂存（一次性 state，进程内存态）
_wb_logins: dict[str, float] = {}
_wb_login_lock = threading.Lock()
WB_LOGIN_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.codebuddy.cn",
    "Referer": "https://www.codebuddy.cn/",
    "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
}


@app.get("/v1/channels/workbuddy/login/start")
def workbuddy_login_start():
    """发起 WorkBuddy 登录：返回官方授权链接（用户自行在浏览器完成）。"""
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{BACKEND}/v2/plugin/auth/state?platform=CLI",
                       headers=WB_LOGIN_HEADERS, json={})
        res = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": {
            "message": f"获取授权链接失败: {e}", "type": "upstream_error"}})
    if res.get("code") != 0 or not res.get("data"):
        raise HTTPException(status_code=502, detail={"error": {
            "message": f"服务端返回错误: {res}", "type": "upstream_error"}})
    state = res["data"]["state"]
    url = res["data"]["authUrl"]
    sid = os.urandom(8).hex()
    with _wb_login_lock:
        _wb_logins[sid] = time.time()
        _wb_logins["_state_" + sid] = state
    return {"url": url, "session": sid,
            "hint": "在浏览器打开该链接完成登录（QQ/微信/手机号），然后回来点「我已授权」"}


@app.post("/v1/channels/workbuddy/login/poll")
async def workbuddy_login_poll(request: Request):
    """轮询 WorkBuddy 登录：成功后拉取账号信息、落盘凭证并重载账号池。"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    sid = str((payload or {}).get("session") or "")
    with _wb_login_lock:
        state = _wb_logins.get("_state_" + sid)
    if not state:
        return {"status": "error", "message": "登录会话不存在或已过期，请重新发起"}
    try:
        with httpx.Client(timeout=30) as c:
            tok = c.get(f"{BACKEND}/v2/plugin/auth/token?state={state}",
                        headers=WB_LOGIN_HEADERS).json()
    except Exception as e:
        return {"status": "pending", "message": f"网络异常，继续等待：{e}"}
    if tok.get("code") != 0 or not tok.get("data"):
        return {"status": "pending", "message": "尚未完成授权…"}

    d = tok["data"]
    access_token = d.get("accessToken", "")
    refresh_token = d.get("refreshToken", "")
    expires_in = d.get("expiresIn", 7200)
    domain = d.get("domain", "www.codebuddy.cn")
    acct: dict = {}
    try:
        with httpx.Client(timeout=30) as c:
            h = dict(WB_LOGIN_HEADERS)
            h["Authorization"] = f"Bearer {access_token}"
            a = c.get(f"{BACKEND}/v2/plugin/login/account?state={state}", headers=h).json()
            if a.get("code") == 0 and a.get("data"):
                acct = a["data"]
    except Exception:
        pass
    uid = acct.get("uid") or "default_user"
    now_ms = int(time.time() * 1000)
    doc = {
        "account": {"uid": uid, "nickname": acct.get("nickname") or "CodeBuddyUser",
                    "enterpriseId": acct.get("enterpriseId") or "",
                    "enterpriseName": acct.get("enterpriseName") or ""},
        "auth": {"accessToken": access_token, "refreshToken": refresh_token,
                 "expiresAt": now_ms + int(expires_in) * 1000, "domain": domain,
                 "lastRefreshTime": now_ms},
    }
    try:
        ad = auth_dirs()[0]
        ad.mkdir(parents=True, exist_ok=True)
        fp = ad / f"{uid}.info"
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, fp)
    except Exception as e:
        return {"status": "error", "message": f"凭据写入失败: {e}"}

    # 重载账号池（新账号立即可用）
    try:
        CONFIG["pool"] = CredentialPool(find_auth_files())
    except Exception as e:
        _log(f"[warn] 账号池重载失败: {e}")
    with _wb_login_lock:
        _wb_logins.pop(sid, None)
        _wb_logins.pop("_state_" + sid, None)
    return {"status": "ok", "message": f"登录成功：{doc['account']['nickname']}", "uid": uid}


@app.get("/v1/channels")
def channels_info():
    """渠道清单 + 各自账号状态 + 模型规格（面板「账号管理」用）。

    含 WorkBuddy 主链路（其账号由 CodeBuddy 凭据目录提供）与聚合渠道三家。
    """
    out = []
    ck_state = _load_checkin_state()
    today = time.strftime("%Y-%m-%d")

    def _ck_of(uid: str) -> dict:
        """按 uid 取今日签到状态（无记录返回未签到）。"""
        cs = ck_state.get(uid) or {}
        return {"today": cs.get("date") == today, "ok": bool(cs.get("ok")),
                "msg": cs.get("msg") or "", "time": cs.get("time") or ""}

    # WorkBuddy：账号来自 CodeBuddy 凭据池
    try:
        pool: CredentialPool | None = CONFIG.get("pool")
        wb_accounts = []
        if pool is not None:
            for i, s in enumerate(pool.snapshot()):
                # 积分：与主页面同源（腾讯 get-user-resource，含资源包构成）
                credits = None
                try:
                    cred_obj = pool.creds[i] if i < len(pool.creds) else None
                    if cred_obj is not None:
                        credits = _fetch_credits(cred_obj)
                except Exception as e:
                    credits = {"error": str(e)}
                wb_accounts.append({
                    "kind": "workbuddy", "uid": s.get("uid") or "",
                    "nickname": s.get("nickname") or "", "channel": "WorkBuddy",
                    "current": bool(s.get("current")),
                    "cooldown_remaining": int(s.get("cooldown_remaining") or 0),
                    "token_expired": bool(s.get("token_expired")),
                    "token_expires_at": int(s.get("token_expires_at") or 0),
                    "file": cred_obj.path.name if cred_obj is not None else "",
                    "credits": credits,
                    "checkin": _ck_of(s.get("uid") or ""),
                    "extra": {},
                })
        wb_models = []
        cred = _cred_for_models()
        if cred is not None:
            for m in _all_models(cred):
                wb_models.append({"id": m["name"], "name": m["name"]})
        out.append({
            "kind": "workbuddy", "name": "WorkBuddy", "prefix": "",
            "needs_login": True, "can_add": True, "can_delete": True,
            "login_mode": "oauth_poll",
            "accounts": wb_accounts, "models": wb_models,
        })
    except Exception as e:
        sys.stderr.write(f"[warn] WorkBuddy 渠道快照失败: {e}\n")

    for ch in all_channels():
        try:
            accounts = ch.snapshot()
        except Exception as e:
            accounts = []
            sys.stderr.write(f"[warn] 渠道 {ch.KIND} 账号快照失败: {e}\n")
        # 注入签到状态（Trae/Qoder 已支持签到，状态同存 checkin_state.json）
        for a in accounts:
            if isinstance(a, dict) and "checkin" not in a:
                a["checkin"] = _ck_of(str(a.get("uid") or ""))
        try:
            models = _channel_models_snapshot_for(ch)
        except Exception:
            models = []
        out.append({
            "kind": ch.KIND,
            "name": ch.DISPLAY_NAME,
            "prefix": ch.MODEL_PREFIX,
            "needs_login": ch.NEEDS_LOGIN,
            "can_add": ch.NEEDS_LOGIN,
            "can_delete": ch.NEEDS_LOGIN,
            "login_mode": getattr(ch, "LOGIN_MODE", "poll"),
            "accounts": accounts,
            "models": models,
        })
    return {"channels": out, "generated_at": int(time.time())}


def _channel_models_snapshot_for(ch) -> list[dict]:
    out = []
    if ch.NEEDS_LOGIN and not ch.accounts():
        return out
    for spec in ch.models():
        d = spec.to_dict()
        d["id"] = f"{ch.MODEL_PREFIX}{spec.id}"
        out.append(d)
    return out


@app.get("/v1/channels/{kind}/login/start")
def channel_login_start(kind: str):
    """发起渠道登录：返回授权链接（由用户自行在浏览器完成，服务不操作本机）。"""
    if kind == "workbuddy":
        return workbuddy_login_start()
    ch = get_channel(kind)
    if ch is None or not ch.login_available():
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"渠道 {kind} 不支持登录或不存在", "type": "not_found"}})
    try:
        try:
            res = ch.login_start(callback_port=CONFIG.get("port", 8787))
        except TypeError:
            res = ch.login_start()   # 旧签名渠道（如 Qoder）不接收回调端口
        if isinstance(res, dict):
            res["login_mode"] = getattr(ch, "LOGIN_MODE", "poll")
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": {
            "message": f"发起登录失败: {e}", "type": "internal_error"}})


@app.post("/v1/channels/{kind}/login/poll")
async def channel_login_poll(kind: str, request: Request):
    """轮询登录状态。请求体: {"session": "..."}。"""
    if kind == "workbuddy":
        return await workbuddy_login_poll(request)
    ch = get_channel(kind)
    if ch is None or not ch.login_available():
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"渠道 {kind} 不支持登录或不存在", "type": "not_found"}})
    # 回调粘贴式渠道（Trae）没有轮询接口：返回明确提示而非 500 异常。
    # 前端本不该对它启动轮询；此处兜底避免日志被 NotImplementedError 刷屏。
    if getattr(ch, "LOGIN_MODE", "poll") == "callback":
        return {"status": "error",
                "message": f"{ch.DISPLAY_NAME} 使用「回调粘贴」方式登录，"
                           f"请把浏览器地址栏的回调链接粘贴到输入框提交"}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    session = str((payload or {}).get("session") or "")
    if not session:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "session is required", "type": "invalid_request_error"}})
    try:
        return ch.login_poll(session)
    except NotImplementedError as e:
        return {"status": "error", "message": str(e)}


@app.post("/v1/channels/{kind}/login/submit")
async def channel_login_callback(kind: str, request: Request):
    """手工粘贴回调链接（面板兜底入口）：请求体 {"session","callback"}。

    注意：浏览器自动回调走 /login/callback（GET/POST 双支持）；
    本端点专供面板用户手工粘贴，路径独立以免与自动回调冲突。
    """
    ch = get_channel(kind)
    if ch is None or not hasattr(ch, "login_submit_callback"):
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"渠道 {kind} 不支持该登录方式", "type": "not_found"}})
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    session = str((payload or {}).get("session") or "")
    callback = str((payload or {}).get("callback") or "")
    if not session or not callback:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "session 与 callback 均为必填", "type": "invalid_request_error"}})
    try:
        return await asyncio.to_thread(ch.login_submit_callback, session, callback)
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def _handle_oauth_callback(kind: str, request: Request):
    """OAuth 回调统一处理：接收授权页投递的凭证，完成登录并返回可关闭的提示页。

    与 wild-work/internal/login_trae 对齐，支持两种投递方式：
      - **POST**：新版授权页（redirect=1）把 refreshToken 放在 JSON/表单 body 里；
      - **GET** ：旧版或跳转式把凭证放在 query 上。
    两者都会带上发起登录时写入的 `session` 参数用于关联登录会话。
    """
    ch = get_channel(kind)
    if ch is None or not hasattr(ch, "login_submit_callback"):
        return HTMLResponse(_callback_page(False, f"渠道 {kind} 不支持该登录方式"), status_code=404)

    # 合并 query 与 body 参数（body 优先，POST 回调的凭证在这里）
    params: dict[str, str] = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.body()
        except Exception:
            body = b""
        if body:
            # 依次尝试 JSON 与表单编码
            parsed: dict = {}
            try:
                obj = json.loads(body)
                if isinstance(obj, dict):
                    parsed = {k: v for k, v in obj.items() if isinstance(v, str)}
            except Exception:
                try:
                    from urllib.parse import parse_qsl
                    parsed = dict(parse_qsl(body.decode("utf-8", "replace")))
                except Exception:
                    parsed = {}
            for k, v in parsed.items():
                if k and v and not params.get(k):
                    params[k] = v

    session = str(params.get("session") or "")
    if not session:
        return HTMLResponse(_callback_page(False, "回调缺少 session 参数，请重新发起登录"),
                            status_code=400)

    # 把合并后的参数重建为 callback URL 交给渠道解析（渠道侧按 query 解析）
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in params.items()})
    callback = f"{request.url.scheme}://{request.url.netloc}{request.url.path}?{qs}"
    try:
        res = await asyncio.to_thread(ch.login_submit_callback, session, callback)
    except Exception as e:
        res = {"status": "error", "message": str(e)}
    ok = res.get("status") == "ok"
    return HTMLResponse(_callback_page(ok, res.get("message") or ""), status_code=200 if ok else 400)


@app.get("/v1/channels/{kind}/login/callback")
async def channel_login_callback_get(kind: str, request: Request):
    """OAuth 回调（GET）：授权页跳转式投递凭证。"""
    return await _handle_oauth_callback(kind, request)


@app.post("/v1/channels/{kind}/login/callback")
async def channel_login_callback_post(kind: str, request: Request):
    """OAuth 回调（POST）：新版授权页把 refreshToken 放在 body 里投递（wild-work 同款）。"""
    return await _handle_oauth_callback(kind, request)


# Trae 的回调不走服务端口：渠道在 login/start 时自起一次性随机端口回调服务器
# （/authorize，wild-work 同款），上游对 auth_callback_url 校验严格，见 channels/trae.py。


def _callback_page(ok: bool, msg: str) -> str:
    """授权回调结果页（用户看到后即可关闭窗口）。"""
    color = "#32f08c" if ok else "#ff6b6b"
    icon = "✓" if ok else "✕"
    title = "登录成功" if ok else "登录失败"
    extra = "此窗口可关闭，请返回控制台查看账号" if ok else "请回到控制台重试，或改用「粘贴回调链接」方式"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{title} · WorkBuddy</title><style>
body{{background:#0a0b0d;color:#fff;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.box{{text-align:center}} .ic{{font-size:64px;color:{color};margin-bottom:18px}}
h1{{font-size:28px;margin:0 0 12px}} p{{color:#9ca3af;font-size:14px;margin:4px 0}}
.msg{{color:#e5e7eb;margin-top:10px}}</style></head><body><div class="box">
<div class="ic">{icon}</div><h1>{title}</h1>
<div class="msg">{msg}</div><p>{extra}</p>
</div></body></html>"""


@app.post("/v1/channels/{kind}/accounts/switch")
async def channel_account_switch(kind: str, request: Request):
    """切换渠道的当前账号。请求体: {"uid": "..."}。

    WorkBuddy 走 CredentialPool（其当前账号决定主链路用哪个号）；
    其余渠道走各自 Channel.set_current（决定候选顺序），均持久化，重启后保留。
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    uid = str((payload or {}).get("uid") or "")
    if not uid:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "uid is required", "type": "invalid_request_error"}})

    if kind == "workbuddy":
        pool: CredentialPool | None = CONFIG.get("pool")
        if pool is None:
            raise HTTPException(status_code=503, detail={"error": {
                "message": "WorkBuddy 账号池不可用", "type": "auth_error"}})
        if not pool.set_current(uid):
            raise HTTPException(status_code=404, detail={"error": {
                "message": f"账号不存在: {uid}", "type": "not_found"}})
        cur = pool.get_current()
        return {"ok": True, "uid": uid, "nickname": _safe_nickname(cur) if cur else ""}

    ch = get_channel(kind)
    if ch is None:
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"渠道 {kind} 不存在", "type": "not_found"}})
    if not ch.set_current(uid):
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"账号不存在: {uid}", "type": "not_found"}})
    acc = ch.current()
    return {"ok": True, "uid": uid, "nickname": (acc.nickname if acc else "") or uid}


@app.post("/v1/channels/{kind}/accounts/delete")
async def channel_account_delete(kind: str, request: Request):
    """删除渠道账号。请求体: {"uid": "..."}。

    删除即从磁盘移除凭据文件：WorkBuddy 删 CodeBuddy 凭据目录下的 .info，
    其余渠道删 auths/<kind>/ 下的账号 JSON。删除后重载账号池。
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    uid = str((payload or {}).get("uid") or "")
    if not uid:
        raise HTTPException(status_code=400, detail={"error": {
            "message": "uid is required", "type": "invalid_request_error"}})

    # WorkBuddy：凭据在 CodeBuddy 官方目录，按 uid 定位 .info 文件删除
    if kind == "workbuddy":
        pool: CredentialPool | None = CONFIG.get("pool")
        if pool is None:
            raise HTTPException(status_code=503, detail={"error": {
                "message": "WorkBuddy 账号池不可用", "type": "auth_error"}})
        target = None
        for c in pool.creds:
            try:
                info = c.summary()
            except Exception:
                info = {}
            if info.get("uid") == uid or c.path.name == uid or c.path.stem == uid:
                target = c
                break
        if target is None:
            raise HTTPException(status_code=404, detail={"error": {
                "message": f"账号不存在: {uid}", "type": "not_found"}})
        if len(pool.creds) <= 1:
            raise HTTPException(status_code=400, detail={"error": {
                "message": "这是 WorkBuddy 唯一的账号，删除后将无法调用；"
                           "请先添加替代账号", "type": "invalid_request_error"}})
        try:
            target.path.unlink(missing_ok=True)
        except Exception as e:
            raise HTTPException(status_code=500, detail={"error": {
                "message": f"凭据删除失败: {e}", "type": "internal_error"}})
        # 重载账号池并清理当前账号指向
        try:
            CONFIG["pool"] = CredentialPool(find_auth_files())
            cur_file = CURRENT_ACCOUNT_FILE
            saved = ""
            try:
                saved = json.loads(cur_file.read_text(encoding="utf-8")).get("uid") or ""
            except Exception:
                pass
            if saved == uid:
                cur_file.unlink(missing_ok=True)
        except Exception as e:
            _log(f"[warn] 账号池重载失败: {e}")
        _log(f"- 删除 WorkBuddy 账号: {uid}（{target.path.name}）")
        return {"ok": True, "uid": uid, "deleted_file": target.path.name}

    ch = get_channel(kind)
    if ch is None:
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"渠道 {kind} 不存在", "type": "not_found"}})
    try:
        ok = ch.delete_account(uid)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {
            "message": str(e), "type": "invalid_request_error"}})
    if not ok:
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"账号不存在: {uid}", "type": "not_found"}})
    return {"ok": True, "uid": uid}


@app.get("/v1/channels/{kind}/credits")
def channel_credits(kind: str):
    """渠道积分（若该渠道提供）。"""
    ch = get_channel(kind)
    if ch is None:
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"渠道 {kind} 不存在", "type": "not_found"}})
    accounts = ch.accounts()
    if not accounts:
        return {"accounts": []}
    out = []
    for acc in accounts:
        credits = {"error": "该渠道不提供积分"}
        if hasattr(ch, "credits"):
            try:
                credits = ch.credits(acc)
            except Exception as e:
                credits = {"error": str(e)}
        out.append({"uid": acc.uid, "nickname": acc.nickname or acc.uid, "credits": credits})
    return {"accounts": out}


# ---------------------------------------------------------------------------
# 模型管理端点：列表/探测/自定义增删/启停
# ---------------------------------------------------------------------------

def _cred_for_models() -> CredentialManager | None:
    pool: CredentialPool | None = CONFIG["pool"]
    return pool.get_current() if pool else None


@app.get("/v1/models-info")
def models_info():
    """面板模型管理数据：合并模型列表 + 探测缓存 + 探测进度 + API 接入信息。"""
    cred = _cred_for_models()
    reg = _load_registry()
    models = _all_models(cred)
    probes = reg.get("probe") or {}
    for m in models:
        m["probe"] = probes.get(m["name"])
        m["disabled"] = False
    for name in reg.get("disabled") or []:
        models.append({"name": name, "source": "", "disabled": True, "probe": probes.get(name)})
    # 聚合渠道模型（Qoder / TraeWork / OpenCodeZen）：规格来自上游动态拉取
    for m in _channel_models_snapshot():
        specs: dict = {"context_length": m.get("context_window") or 0,
                       "max_output_tokens": m.get("max_output_tokens") or 0,
                       "input": m.get("input") or ["text"], "output": ["text"]}
        # 倍率：数值型（WorkBuddy 侧走 meta.credits 字符串，两者前端统一处理）
        if isinstance(m.get("rate"), (int, float)):
            specs["rate"] = m["rate"]
        models.append({
            "name": m["id"], "source": "",   # 渠道由 chTag 统一标注，避免重复徽标
            "specs": specs,
            "meta": {"display_name": m.get("name"),
                     "supports_reasoning": m.get("supports_reasoning"),
                     "supports_tool_call": m.get("supports_tools"),
                     "context_from_api": m.get("context_from_api"),
                     "rate_note": m.get("rate_note") or "",
                     "channel": m.get("channel")},
            "probe": probes.get(m["id"]), "disabled": False, "channel_model": True,
        })
    return {"models": models, "probe_state": dict(_probe_state),
            "api": _api_info(), "custom": reg.get("custom") or []}


@app.post("/v1/models/custom")
async def models_add_custom(request: Request):
    """添加自定义模型。请求体: {"name": "模型名", "alias": "可选别名"}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    name = ((payload or {}).get("name") or "").strip()
    alias = ((payload or {}).get("alias") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"error": {"message": "name is required", "type": "invalid_request_error"}})
    reg = _load_registry()
    reg.setdefault("custom", [])
    if any(c["name"] == name for c in reg["custom"]):
        raise HTTPException(status_code=409, detail={"error": {"message": f"模型已存在: {name}", "type": "conflict"}})
    reg["custom"].append({"name": name, "alias": alias, "added_at": int(time.time())})
    specs = (payload or {}).get("specs")
    if isinstance(specs, dict) and specs:
        merged = _default_specs()
        merged.update(specs)
        reg.setdefault("specs", {})[name] = merged
    reg["disabled"] = [d for d in (reg.get("disabled") or []) if d != name]
    _save_registry(reg)
    _log(f"+ 添加自定义模型: {name}" + (f" (别名 {alias})" if alias else ""))
    return {"ok": True, "custom": reg["custom"]}


@app.post("/v1/models/delete")
async def models_delete(request: Request):
    """删除自定义模型；对内置/上游模型则是禁用⇄恢复开关。请求体: {"name": "..."}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"error": {"message": "name is required", "type": "invalid_request_error"}})
    reg = _load_registry()
    before = len(reg.get("custom") or [])
    reg["custom"] = [c for c in (reg.get("custom") or []) if c["name"] != name]
    if len(reg["custom"]) < before:
        reg["probe"] = {k: v for k, v in (reg.get("probe") or {}).items() if k != name}
        (reg.get("specs") or {}).pop(name, None)
        _save_registry(reg)
        _log(f"- 删除自定义模型: {name}")
        return {"ok": True, "action": "deleted", "custom": reg["custom"]}
    disabled = reg.get("disabled") or []
    if name in disabled:
        reg["disabled"] = [d for d in disabled if d != name]
        action = "enabled"
    else:
        reg["disabled"] = disabled + [name]
        action = "disabled"
    _save_registry(reg)
    _log(f"⏻ 模型{action}: {name}")
    return {"ok": True, "action": action, "disabled": reg["disabled"]}


@app.post("/v1/models/specs")
async def models_specs(request: Request):
    """更新模型参数规格。请求体: {"name": "...", "specs": {"context_length":..., "max_output_tokens":..., "input":[...], "output":[...]}}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    name = ((payload or {}).get("name") or "").strip()
    specs = (payload or {}).get("specs")
    if not name or not isinstance(specs, dict):
        raise HTTPException(status_code=400, detail={"error": {"message": "name and specs are required", "type": "invalid_request_error"}})
    reg = _load_registry()
    merged = _default_specs()
    for k in ("context_length", "max_output_tokens"):
        try:
            merged[k] = int(specs.get(k) or merged[k])
        except (TypeError, ValueError):
            pass
    for k in ("input", "output"):
        v = specs.get(k)
        if isinstance(v, list) and v:
            merged[k] = [str(x) for x in v]
    reg.setdefault("specs", {})[name] = merged
    _save_registry(reg)
    return {"ok": True, "name": name, "specs": merged}


# ---------------------------------------------------------------------------
# API Key 管理(面板创建/删除,立即生效)
# ---------------------------------------------------------------------------

@app.get("/v1/keys")
def keys_list():
    keys = _load_keys()
    for k in keys:
        k["last_used"] = _keys_last_used.get(k["key"]) or k.get("last_used")
        k.setdefault("channel", "")
    master = CONFIG.get("api_key") or ""
    # 可选绑定渠道：供面板下拉选择
    ch_options = [{"kind": "", "name": "全部渠道（不限制）"},
                  {"kind": "workbuddy", "name": "WorkBuddy"}]
    for ch in all_channels():
        # 不过滤 NEEDS_LOGIN：OpenCodeZen 免登录渠道也允许绑定（同名模型按 key 归属，防串台）
        ch_options.append({"kind": ch.KIND, "name": ch.DISPLAY_NAME})
    return {"keys": keys, "master_set": bool(master),
            "auth_enabled": bool(master) or bool(keys),
            "channel_options": ch_options}


@app.post("/v1/keys")
async def keys_create(request: Request):
    """创建新 API Key。请求体: {"name": "用途备注", "channel": "qoder"|"workbuddy"|""}。

    channel 为空表示不限制渠道；指定后该 key 只能访问对应平台的模型。
    返回完整 key(仅此一次展示原文)。
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    name = ((payload or {}).get("name") or "").strip() or "未命名"
    channel = _normalize_key_channel(str((payload or {}).get("channel") or ""))
    import secrets as _secrets
    key = "sk-wb-" + _secrets.token_hex(16)
    keys = _load_keys()
    while any(k["key"] == key for k in keys):   # 极小概率碰撞
        key = "sk-wb-" + _secrets.token_hex(16)
    keys.append({"key": key, "name": name, "channel": channel,
                 "created_at": int(time.time()),
                 "last_used": None, "disabled": False})
    _save_keys(keys)
    _log(f"+ 创建 API Key: {name}（渠道={channel or '全部'}）({key[:12]}…)")
    return {"ok": True, "key": key, "name": name, "channel": channel}


@app.post("/v1/keys/delete")
async def keys_delete(request: Request):
    """删除 API Key。请求体: {"key": "sk-wb-..."}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    key = ((payload or {}).get("key") or "").strip()
    keys = _load_keys()
    new_keys = [k for k in keys if k["key"] != key]
    if len(new_keys) == len(keys):
        raise HTTPException(status_code=404, detail={"error": {"message": "key 不存在", "type": "not_found"}})
    _save_keys(new_keys)
    _log(f"- 删除 API Key: {key[:12]}…")
    return {"ok": True, "keys": new_keys}


@app.get("/v1/models-refresh")
def models_refresh():
    """强制重新拉取上游模型目录(名称/规格/倍率/标签),不发聊天请求、零消耗。"""
    cred = _cred_for_models()
    with _models_lock:
        _upstream_models_cache["t"] = 0.0
    models = _all_models(cred)
    reg = _load_registry()
    for m in models:
        m["probe"] = (reg.get("probe") or {}).get(m["name"])
        m["disabled"] = False
    return {"ok": True, "refreshed": True, "models": models,
            "count": len(models)}


@app.post("/v1/models/probe")
async def models_probe(request: Request):
    """探测单个模型（发一次最小真实请求，消耗少量 credits）。请求体: {"model": "..."}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    model = ((payload or {}).get("model") or "").strip()
    cred = _cred_for_models()
    if cred is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "账号池为空", "type": "auth_error"}})
    result = await asyncio.to_thread(probe_model, cred, model)
    return {"ok": True, "model": model, "result": result}


@app.post("/v1/models/probe-all")
async def models_probe_all():
    """后台顺序探测全部模型；进度通过 /v1/models-info 的 probe_state 轮询。"""
    if _probe_state.get("running"):
        return {"ok": True, "already_running": True, "state": dict(_probe_state)}
    cred = _cred_for_models()
    if cred is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "账号池为空", "type": "auth_error"}})
    threading.Thread(target=_probe_all_worker, daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/panel")
def panel():
    # 禁止缓存：面板 HTML 内嵌 JS 随版本变化，若被浏览器缓存会看到旧界面
    # （曾致「改完不生效」的误判），故显式声明 no-store。
    return HTMLResponse(PANEL_HTML, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    auth = _check_auth(authorization, x_api_key)
    scope = str(auth.get("channel") or "")
    data: list[dict] = []
    # WorkBuddy 主链路模型（无凭据时跳过，不影响渠道模型展示）
    cred = _cred_for_models()
    if cred is not None and (not scope or scope == "workbuddy"):
        data = [{"id": m["name"], "object": "model", "created": 1700000000,
                 "owned_by": "codebuddy"} for m in _all_models(cred)]
    # 追加聚合渠道（Qoder / TraeWork / OpenCodeZen）的模型
    for m in _channel_models_snapshot():
        if scope and m.get("channel") != scope:
            continue    # 绑定渠道的 key 只看到本渠道模型
        data.append({"id": m["id"], "object": "model", "created": 1700000000,
                     "owned_by": m.get("channel", "channel")})
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    auth = _check_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 渠道前缀路由：qoder/*、trae/*、oczen/* 走聚合渠道链路（不经 WorkBuddy 后端）。
    # 必须在取 WorkBuddy 账号池之前分流——否则没有 CodeBuddy 凭据时渠道请求会被误拦。
    raw_model_for_route = str(payload.get("model") or "")
    # 绑定渠道的 key 做消歧：裸名自动归属该 key 的平台（防同名模型串台），
    # 显式写了其他平台前缀则 403。返回值是最终生效的模型名（含渠道前缀）。
    scoped_model = _enforce_key_scope(str(auth.get("channel") or ""), raw_model_for_route)
    if scoped_model != raw_model_for_route:
        payload["model"] = scoped_model
        raw_model_for_route = scoped_model
    ch_kind, ch_model = _split_channel(raw_model_for_route)
    if ch_kind:
        return await _channel_chat(ch_kind, ch_model, payload, raw_model_for_route)

    pool = _pool()

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    raw_model = body.get("model", "auto")
    body["model"] = _resolve_model(raw_model)
    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 可选：脱敏。缓解客户端合规模板（如 Codex CLI / ZCode 注入的说明文字）被后端误判为敏感词。
    # 处理 system / developer 消息、Codex 注入的上下文 user 消息，以及 tools 的 description。
    if CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system", "developer"),
                                desensitize_harness_user=True,
                                desensitize_tools=True,
                                compact_harness=not CONFIG.get("no_compact"),
                                strip_tool_metadata=True)

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log(f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(pool, url, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应。
    # 账号池 failover：当前账号额度/认证类失败时自动切换下一个账号重试。
    collected: dict | None = None
    last_status, last_raw = 503, b'"no credentials available in pool"'
    for _, cred in pool.candidates():
        headers = _safe_headers(pool, cred, rid, model_name)
        if headers is None:
            continue
        try:
            async with httpx.AsyncClient(timeout=300) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        last_raw = await r.aread()
                        last_status = r.status_code
                        _log(f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | 账号[{_safe_nickname(cred)}] | {_truncate(last_raw.decode('utf-8','replace'),200)}")
                        _log(f"[{rid}] ── ERROR BODY ──\n{last_raw.decode('utf-8','replace')}")
                        if _is_failover_error(r.status_code, last_raw):
                            pool.report_failure(cred, r.status_code, last_raw)
                            continue
                        raise HTTPException(status_code=r.status_code, detail=_safe_err_raw(last_raw, r.status_code))
                    collected = await _collect_stream(r)
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})
        pool.report_success(cred)
        break
    if collected is None:
        raise HTTPException(status_code=last_status, detail=_safe_err_raw(last_raw, last_status))
    _log_finish(model_name, t0, collected, rid)
    _record_usage(channel="workbuddy", model=model_name,
                  usage=collected.get("usage"), endpoint="chat", stream=False,
                  account=_safe_nickname(pool.get_current()) if pool.get_current() else "")
    return JSONResponse(content=collected)


async def _channel_sse_text(kind: str, model: str, payload: dict,
                            full_model: str) -> str:
    """向渠道池请求对话，返回**标准 OpenAI SSE 文本**（多账号失败自动轮换）。

    这是渠道链路的公共内核：/chat/completions 直接透传该文本，
    /responses 与 /messages 则喂给各自的转换器改写为对应协议。
    """
    ch = get_channel(kind)
    if ch is None:
        raise HTTPException(status_code=400, detail={"error": {
            "message": f"未知渠道前缀: {kind}", "type": "invalid_request_error"}})

    accounts = ch.candidates()
    if not accounts:
        raise HTTPException(status_code=503, detail={"error": {
            "message": f"{ch.DISPLAY_NAME} 渠道尚未添加账号，请在管理面板「账号管理」中添加",
            "type": "auth_error"}})

    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body["model"] = model          # 渠道内部用不带前缀的模型名
    body["stream"] = True          # 渠道内核恒为流式（非流式由调用方聚合）
    rid = os.urandom(4).hex()
    t0 = time.time()
    last_status, last_raw = 502, b'{"error":{"message":"no account available"}}'

    _log(f"[{rid}] ▶ CHANNEL {full_model} | {ch.DISPLAY_NAME} | 账号候选={len(accounts)}")

    for acc in accounts:
        try:
            status, raw = await asyncio.to_thread(ch.chat_stream, acc, body, model, rid)
        except Exception as e:
            _log(f"[{rid}] ✗ {ch.DISPLAY_NAME}[{acc.nickname or acc.uid}] 异常: {e}")
            ch.report_failure(acc)
            last_status = 502
            # 必须更新 last_raw：否则全部账号异常时会返回初始占位文案，
            # 把真实错误（如上游崩溃/解析失败）掩盖成"账号不可用"。
            last_raw = json.dumps({"error": {
                "message": f"{ch.DISPLAY_NAME} 请求异常: {e}", "type": "upstream_error"
            }}, ensure_ascii=False).encode()
            continue
        if status >= 400:
            last_raw = raw
            last_status = status
            _log(f"[{rid}] ✗ {ch.DISPLAY_NAME}[{acc.nickname or acc.uid}] HTTP {status} "
                 f"| {_truncate(raw.decode('utf-8', 'replace'), 200)}")
            if status in FAILOVER_STATUS_CODES:
                ch.report_failure(acc)
                continue
            raise HTTPException(status_code=status, detail=_safe_err_raw(raw, status))
        ch.report_success(acc)
        text = raw.decode("utf-8", "replace")
        _log(f"[{rid}] ◀ {ch.DISPLAY_NAME}[{acc.nickname or acc.uid}] {time.time()-t0:.1f}s"
             f" | {len(raw)} bytes")
        _record_usage_from_text(ch.KIND, model, text, acc.nickname or acc.uid)
        # 上游不下发积分消耗的渠道（TraeWork）：用余额差分补记消耗
        _diff_credits_best_effort(ch, acc, model)
        return text

    raise HTTPException(status_code=last_status, detail=_safe_err_raw(last_raw, last_status))


def _diff_credits_best_effort(ch, acc, model: str) -> None:
    """余额差分补记积分消耗（仅用于上游不下发积分消耗的渠道，如 TraeWork）。

    上游对话流不带积分，只能查余额与上次快照比差。查余额是额外网络请求，
    故按账号节流（默认 60s 内只查一次）；任何失败都静默忽略，不影响主链路。
    """
    if STATS is None or not hasattr(ch, "credits"):
        return
    uid = acc.uid or ""
    if not uid:
        return
    key = f"{ch.KIND}/{uid}"
    now = time.time()
    with _credit_diff_lock:
        if now - _credit_diff_last.get(key, 0.0) < CREDIT_DIFF_MIN_SECS:
            return
        _credit_diff_last[key] = now
    try:
        c = ch.credits(acc) or {}
        if c.get("error"):
            return
        remain = int(c.get("total_remain") or 0)
        STATS.diff_credits(ch.KIND, uid, remain,
                           account=acc.nickname or uid, model=model)
    except Exception:
        pass


_credit_diff_lock = threading.Lock()
_credit_diff_last: dict[str, float] = {}
# 余额差分的最小查询间隔：查余额是额外上游请求，避免每次对话都打
CREDIT_DIFF_MIN_SECS = 60.0


def _record_usage_from_text(channel: str, model: str, text: str, account: str) -> None:
    """从渠道返回文本中提取 usage 并记账（流式为 SSE 末帧，非流式为单个 JSON）。"""
    usage_obj: dict = {}
    if not text.lstrip().startswith("data:"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                usage_obj = obj["usage"]
        except Exception:
            pass
    else:
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith("data:"):
                continue
            payload_s = s[5:].strip()
            if not payload_s or payload_s == "[DONE]":
                continue
            try:
                o = json.loads(payload_s)
            except Exception:
                continue
            if isinstance(o.get("usage"), dict) and o["usage"]:
                usage_obj = o["usage"]
    _record_usage(channel=channel, model=model, usage=usage_obj, endpoint="chat",
                  account=account, stream=True)


async def _channel_chat(kind: str, model: str, payload: dict, full_model: str):
    """聚合渠道（Qoder / TraeWork / OpenCodeZen）的对话入口。

    - 多账号：按渠道账号池顺序尝试（健康优先、冷却兜底），失败自动换下一个；
    - 非流式：渠道返回的是完整 OpenAI SSE 文本，这里聚合成单个响应；
    - 流式：直接透传渠道产出的标准 SSE。
    """
    client_wants_stream = bool(payload.get("stream"))
    text = await _channel_sse_text(kind, model, payload, full_model)
    t0 = time.time()

    if client_wants_stream:
        async def _gen(t: str = text):
            yield t
        return StreamingResponse(_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
    # 非流式：把渠道产出的 SSE 聚合为单个响应
    chunks = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunks.append(json.loads(data))
        except Exception:
            continue
    if chunks:
        agg = _aggregate_openai_chunks(chunks, full_model)
        _log_finish(full_model, t0, agg, "")
        return JSONResponse(content=agg)
    # 渠道直接返回了非 SSE 的 JSON（异常兜底）
    try:
        return JSONResponse(content=json.loads(text))
    except Exception:
        raise HTTPException(status_code=502, detail=_safe_err_raw(text.encode(), 502))


def _aggregate_openai_chunks(chunks: list, model: str) -> dict:
    """把 OpenAI 形状的流式 chunk 聚合为单个 chat.completion（含 tool_calls 合并）。"""
    cid, created, finish = "", 0, "stop"
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict = {}
    tool_calls: dict[int, dict] = {}
    for obj in chunks:
        if not isinstance(obj, dict):
            continue
        cid = obj.get("id") or cid
        created = obj.get("created") or created
        if isinstance(obj.get("usage"), dict) and obj["usage"]:
            usage = obj["usage"]
        for c in obj.get("choices") or []:
            if not isinstance(c, dict):
                continue
            if c.get("finish_reason"):
                finish = c["finish_reason"]
            d = c.get("delta") or {}
            if isinstance(d.get("content"), str):
                content.append(d["content"])
            if isinstance(d.get("reasoning_content"), str):
                reasoning.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = int(tc.get("index") or 0)
                cur = tool_calls.get(idx)
                if cur is None:
                    cur = {"index": idx, "id": "", "type": "function",
                           "function": {"name": "", "arguments": ""}}
                    tool_calls[idx] = cur
                if tc.get("id"):
                    cur["id"] = tc["id"]
                if tc.get("type"):
                    cur["type"] = tc["type"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    cur["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    cur["function"]["arguments"] += fn["arguments"]
    msg: dict = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        msg["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    resp = {"id": cid or f"chatcmpl-{int(time.time()*1000)}", "object": "chat.completion",
            "created": created or int(time.time()), "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}]}
    if usage:
        resp["usage"] = usage
    return resp


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
         + (f" | tool_calls={tc_names}" if tc_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整响应体
    _log(f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    """
    content_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


async def _stream_upstream(pool: CredentialPool, url: str, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """把后端 SSE 原样转发给客户端（后端已是标准 OpenAI SSE，含 tool_calls）。

    带账号池 failover：当前账号在拿到响应状态阶段遇额度/认证类错误（尚未向客户端
    输出任何字节）时，自动切换下一个账号重试；转发开始后不再切换。
    同时轻量解析流，统计 finish_reason / tool_calls / usage 用于日志，不阻塞转发。
    完整原始 SSE 累积后落盘到日志（调试用）。
    """
    prefix = f"[{rid}] " if rid else ""

    for _, cred in pool.candidates():
        headers = _safe_headers(pool, cred, rid, model_name)
        if headers is None:
            continue
        finish_reason = None
        tool_names: list[str] = []
        usage: dict = {}
        saw_filter = False
        buf = b""
        raw_parts: list[bytes] = []   # 累积完整原始 SSE

        def _feed(chunk: bytes):
            nonlocal finish_reason, saw_filter, buf
            # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if obj.get("usage"):
                    usage.update(obj["usage"])
                for ch in obj.get("choices") or []:
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]
                    for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                        nm = (tc.get("function") or {}).get("name")
                        if nm:
                            tool_names.append(nm)
                # 内容审核拦截常以 content-filter 或特殊中文文案返回
                try:
                    text_repr = data.decode("utf-8", "replace")
                except Exception:
                    text_repr = ""
                if "content-filter" in text_repr or "敏感" in text_repr or "审核" in text_repr:
                    saw_filter = True

        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        err = await r.aread()
                        _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | 账号[{_safe_nickname(cred)}] | {_truncate(err.decode('utf-8','replace'),200)}")
                        _log(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8','replace')}")
                        if _is_failover_error(r.status_code, err):
                            pool.report_failure(cred, r.status_code, err)
                            _log(f"{prefix}↻ 账号[{_safe_nickname(cred)}] 不可用（HTTP {r.status_code}），切换下一个账号重试")
                            continue
                        yield _err_event(err, r.status_code)
                        return
                    stream_buf = b""
                    async for chunk in r.aiter_bytes():
                        if chunk:
                            raw_parts.append(chunk)
                            _feed(chunk)
                            stream_buf += chunk
                            while b"\n" in stream_buf:
                                line, stream_buf = stream_buf.split(b"\n", 1)
                                stripped = line.strip()
                                if not stripped.startswith(b"data:"):
                                    if stripped:
                                        yield line + b"\n"
                                    continue
                                data_bytes = stripped[5:].strip()
                                if data_bytes == b"[DONE]":
                                    yield b"data: [DONE]\n\n"
                                    continue
                                try:
                                    obj = json.loads(data_bytes)
                                    for ch in obj.get("choices") or []:
                                        delta = ch.get("delta") or {}
                                        for empty_key in ("reasoning_content", "refusal", "function_call", "extra_fields", "tool_calls"):
                                            if delta.get(empty_key) in ("", None, []):
                                                delta.pop(empty_key, None)
                                    out_line = f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
                                    yield out_line
                                except Exception:
                                    yield line + b"\n"
        except httpx.HTTPError as e:
            _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
            yield _err_event(str(e).encode(), 502)
            return

        pool.report_success(cred)
        # 流结束：输出完成日志
        elapsed = time.time() - t0 if t0 else 0
        tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
        _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
             + (f" | tool_calls={tool_names}" if tool_names else "")
             + f" | tokens={usage.get('total_tokens', '?')}")
        _record_usage(channel="workbuddy", model=model_name, usage=usage,
                      endpoint="chat", account=_safe_nickname(cred), stream=True)
        # 完整原始 SSE（后端返回的全部内容）
        _log(f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8','replace')}")
        return


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {"error": {"message": r.text[:500], "type": "upstream_error", "code": r.status_code}}


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json, time as _time
    chunk = {
        "error": {"message": msg.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status},
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def _looks_like_content_filter_text(text: str) -> bool:
    text = (text or "").lower()
    return (
        "content-filter" in text
        or "content_filter" in text
        or "敏感内容" in text
        or "内容审核" in text
        or "无法响应您的请求" in text
    )


def _chat_body_desensitize(body: dict, *, force_compact: bool = False) -> dict:
    if not CONFIG.get("desensitize"):
        return body
    return desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
        compact_harness=(force_compact or not CONFIG.get("no_compact")),
        strip_tool_metadata=True,
    )


async def _post_backend_once(url: str, headers: dict, body: dict) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("POST", url, headers=headers, json=body) as r:
            chunks: list[bytes] = []
            async for chunk in r.aiter_bytes():
                if chunk:
                    chunks.append(chunk)
            return r.status_code, b"".join(chunks)


async def _post_backend_with_failover(pool: CredentialPool, url: str, body: dict,
                                      rid: str = "", model_name: str = "?") -> tuple[int, bytes, CredentialManager | None, dict]:
    """带账号池 failover 的后端请求。

    从当前账号开始依次尝试：额度/认证类失败（_is_failover_error）切换下一个账号；
    200 但检测到 content-filter 且处于 no_compact 脱敏模式时，按原逻辑用压缩
    harness 重试一次。返回 (status, raw, cred, final_body)；所有账号不可用时
    cred 为 None 且 status 为最后一次的错误状态。
    """
    prefix = f"[{rid}] " if rid else ""
    last: tuple[int, bytes, CredentialManager | None, dict] = (503, b'"no credentials available in pool"', None, body)
    for _, cred in pool.candidates():
        headers = _safe_headers(pool, cred, rid, model_name)
        if headers is None:
            continue
        status, raw = await _post_backend_once(url, headers, body)
        if status == 200:
            pool.report_success(cred)
            text = raw.decode("utf-8", "replace")
            if _looks_like_content_filter_text(text) and CONFIG.get("desensitize") and CONFIG.get("no_compact"):
                retry_body = _chat_body_desensitize(body, force_compact=True)
                _log(f"{prefix}↻ RESPONSES {model_name} | content filter detected, retry with compact harness")
                _log(f"{prefix}── RESPONSES RETRY CHAT BODY ──\n{json.dumps(retry_body, ensure_ascii=False, indent=2)}")
                retry_status, retry_raw = await _post_backend_once(url, headers, retry_body)
                retry_text = retry_raw.decode("utf-8", "replace")
                if retry_status == 200 and not _looks_like_content_filter_text(retry_text):
                    return retry_status, retry_raw, cred, retry_body
            return status, raw, cred, body
        _log(f"{prefix}✗ HTTP {status} | {model_name} | 账号[{_safe_nickname(cred)}] | {_truncate(raw.decode('utf-8','replace'),200)}")
        if not _is_failover_error(status, raw):
            return status, raw, cred, body
        pool.report_failure(cred, status, raw)
        _log(f"{prefix}↻ 账号[{_safe_nickname(cred)}] 不可用（HTTP {status}），切换下一个账号重试")
        last = (status, raw, cred, body)
    return last


# ---------------------------------------------------------------------------
# Responses API 端点（Codex CLI 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/responses")
async def create_response(request: Request,
                          authorization: Optional[str] = Header(default=None),
                          x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI Responses API 兼容端点。

    Codex CLI 使用 Responses API（wire_api = "responses"）而非 Chat Completions。
    本端点接收 Responses 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Responses 语义事件流返回。
    """
    auth = _check_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    # 绑定渠道的 key：裸名自动归属该平台（防同名模型串台），跨平台前缀 403
    _scoped = _enforce_key_scope(str(auth.get("channel") or ""), str(chat_body.get("model") or ""))
    chat_body["model"] = _scoped

    chat_body, projection_stats = project_responses_chat_body(chat_body)
    raw_model = chat_body.get("model", "auto")
    chat_body["model"] = _resolve_model(raw_model)
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    chat_body = _chat_body_desensitize(chat_body)

    client_wants_stream = payload.get("stream", True)  # Codex CLI 默认 stream
    model_name = payload.get("model", "auto")
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ RESPONSES {model_name} | stream={client_wants_stream} | input_items={len(payload.get('input', []))}")
    _log(
        f"[{rid}] ── RESPONSES PROJECTION ── "
        f"mode={projection_stats.get('mode')} "
        f"| msgs {projection_stats.get('original_messages')}→{projection_stats.get('projected_messages')} "
        f"| chars {projection_stats.get('original_message_chars')}→{projection_stats.get('projected_message_chars')} "
        f"| tools {projection_stats.get('original_tools')}→{projection_stats.get('projected_tools')} "
        f"| tool_chars {projection_stats.get('original_tool_chars')}→{projection_stats.get('projected_tool_chars')} "
        f"| summarized_history={projection_stats.get('summarized_history_messages', 0)} "
        f"| dropped_harness={projection_stats.get('dropped_harness_messages', 0)} "
        f"| anchor_user={projection_stats.get('anchor_user_preserved', False)}"
    )
    _log(f"[{rid}] ── RESPONSES → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    # 渠道路由：模型带渠道前缀时走聚合渠道链路，把渠道的 OpenAI SSE
    # 实时转换为 Responses 事件流（此前只支持 WorkBuddy，绑定渠道的 key 会失败）
    ch_kind, _ch_model = _split_channel(str(chat_body.get("model") or ""))
    if ch_kind:
        text = await _channel_sse_text(ch_kind, _ch_model, chat_body, model_name)
        conv = ResponsesStreamConverter(model=model_name)

        def _responses_events(t: str = text):
            for line in t.splitlines():
                ev = conv.feed_line(line)
                if ev:
                    yield ev.encode("utf-8")
            fin = conv.finish()
            if fin:
                yield fin.encode("utf-8")

        if client_wants_stream:
            return StreamingResponse(_responses_events(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})
        conv2 = ResponsesStreamConverter(model=model_name)
        for line in text.splitlines():
            conv2.feed_line(line)
        return JSONResponse(content=conv2.get_nonstream_response())

    pool = _pool()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_responses(pool, url, chat_body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合后端 SSE → 非流式 Response 对象
    try:
        status_code, raw, cred, final_body = await _post_backend_with_failover(pool, url, chat_body, rid, model_name)
        if status_code != 200:
            _log(f"[{rid}] ✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            raise HTTPException(status_code=status_code, detail=_safe_err_raw(raw, status_code))
        converter = ResponsesStreamConverter(model=model_name)
        for line in raw.decode("utf-8", "replace").splitlines():
            converter.feed_line(line)
        chat_body = final_body
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

    result = converter.get_nonstream_response()
    elapsed = time.time() - t0
    _log(f"[{rid}] ◀ RESPONSES {model_name} | {elapsed:.1f}s")
    _log(f"[{rid}] ── RESPONSE OBJ ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")
    _record_usage(channel="workbuddy", model=model_name, usage=result.get("usage"),
                  endpoint="responses", stream=False,
                  account=_safe_nickname(pool.get_current()) if pool.get_current() else "")
    return JSONResponse(content=result)


async def _stream_responses(pool: CredentialPool, url: str, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 Chat SSE，实时转换为 Responses API 事件流。"""
    converter = ResponsesStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        status_code, raw, cred, _ = await _post_backend_with_failover(pool, url, body, rid, model_name)
        if status_code != 200:
            _log(f"{prefix}✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            error_evt = {"type": "error", "error": {"message": raw.decode('utf-8','replace')[:500], "code": status_code}}
            yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        raw_sse_lines = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                raw_sse_lines.append(line)
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "code": 502}}
        yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    # 发送收尾事件
    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream done")
    _log(f"{prefix}── RESPONSES RAW SSE ──\n" + "\n".join(raw_sse_lines[-30:]))
    _record_usage(channel="workbuddy", model=model_name,
                  usage=converter._usage if hasattr(converter, "_usage") else None,
                  endpoint="responses", stream=True,
                  account=_safe_nickname(pool.get_current()) if pool.get_current() else "")


# ---------------------------------------------------------------------------
# Anthropic Messages API 端点（Claude Code / CC Switch 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def create_message(request: Request,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic Messages API 兼容端点。

    Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages）。
    本端点接收 Anthropic 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Anthropic SSE 事件流返回。
    """
    auth = _check_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 将 Anthropic 格式消息、工具规范在进入后端前统一转换为 OpenAI Chat 格式。
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    # 绑定渠道的 key：裸名自动归属该平台（防同名模型串台），跨平台前缀 403
    _scoped = _enforce_key_scope(str(auth.get("channel") or ""), str(chat_body.get("model") or ""))
    chat_body["model"] = _scoped

    raw_model = chat_body.get("model", "auto")
    chat_body["model"] = _resolve_model(raw_model)
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    if CONFIG.get("desensitize"):
        chat_body = desensitize_body(chat_body, roles=("system", "developer"),
                                     desensitize_harness_user=True,
                                     desensitize_tools=True,
                                     compact_harness=not CONFIG.get("no_compact"),
                                     strip_tool_metadata=True)

    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)}")
    _log(f"[{rid}] ── ANTHROPIC → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    # 渠道路由：模型带渠道前缀时走聚合渠道链路，把渠道的 OpenAI SSE
    # 转换为 Anthropic 事件流（此前只支持 WorkBuddy，绑定渠道的 key 会失败）
    ch_kind, _ch_model = _split_channel(str(chat_body.get("model") or ""))
    if ch_kind:
        text = await _channel_sse_text(ch_kind, _ch_model, chat_body, model_name)
        conv = AnthropicStreamConverter()

        def _anthropic_events(t: str = text):
            for line in t.splitlines():
                ev = conv.feed_line(line)
                if ev:
                    yield ev.encode("utf-8")
            fin = conv.finish()
            if fin:
                yield fin.encode("utf-8")

        return StreamingResponse(_anthropic_events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    pool = _pool()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    return StreamingResponse(
        _stream_anthropic(pool, url, chat_body, model_name, t0, rid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_anthropic(pool: CredentialPool, url: str, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 OpenAI Chat SSE，实时转换为 Anthropic Messages SSE 事件流。

    带账号池 failover：当前账号额度/认证类失败时自动切换下一个账号重试
    （仅在尚未向客户端输出任何事件前切换）。
    """
    prefix = f"[{rid}] " if rid else ""

    for _, cred in pool.candidates():
        headers = _safe_headers(pool, cred, rid, model_name)
        if headers is None:
            continue
        converter = AnthropicStreamConverter(model=model_name)
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        err = await r.aread()
                        _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | 账号[{_safe_nickname(cred)}] | {_truncate(err.decode('utf-8','replace'),200)}")
                        if _is_failover_error(r.status_code, err):
                            pool.report_failure(cred, r.status_code, err)
                            _log(f"{prefix}↻ 账号[{_safe_nickname(cred)}] 不可用（HTTP {r.status_code}），切换下一个账号重试")
                            continue
                        error_evt = {"type": "error", "error": {"message": err.decode('utf-8','replace')[:500], "type": "api_error", "code": r.status_code}}
                        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
                        return
                    async for line in r.aiter_lines():
                        events = converter.feed_line(line)
                        if events:
                            yield events.encode("utf-8")
        except httpx.HTTPError as e:
            _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
            error_evt = {"type": "error", "error": {"message": str(e)[:500], "type": "api_error", "code": 502}}
            yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return

        pool.report_success(cred)
        finish_events = converter.finish()
        if finish_events:
            yield finish_events.encode("utf-8")

        elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream done")
    _record_usage(channel="workbuddy", model=model_name,
                  usage=converter._usage if hasattr(converter, "_usage") else None,
                  endpoint="messages", stream=True,
                  account=_safe_nickname(pool.get_current()) if pool.get_current() else "")


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic token 计数端点（stub）。

    Claude Code 可能在发送消息前调用此端点。
    返回一个简单估算值，不做实际 token 计数。
    """
    _check_auth(authorization, x_api_key)
    return {"input_tokens": 0}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def preflight() -> bool:
    files = find_auth_files()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"版本      : {PROJECT_VERSION}\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"已查目录  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if not files:
        sys.stderr.write("\n[警告] 未找到登录文件。请运行 ./login.sh 或在桌面端完成登录（CodeBuddy/WorkBuddy）。\n")
        ok = False
    else:
        sys.stderr.write(f"账号池    : {len(files)} 个账号\n")
        for f in files:
            try:
                cm = CredentialManager(f)
                info = cm.summary()
                sys.stderr.write(f"  - {info.get('nickname')} / {info.get('enterpriseName') or '(个人)'}"
                                 f" | token过期: {'是(将自动刷新)' if info['token_expired'] else '否'} | {f.name}\n")
            except Exception as e:
                sys.stderr.write(f"[警告] 读取凭据失败 {f}：{e}\n")
                ok = False
    sys.stderr.write("================\n")
    return ok


def main():
    # 跨平台输出编码：Windows 下 stdout/stderr 被重定向到文件/管道时默认跟随 ANSI
    # 代码页（GBK），日志里的 emoji/特殊符号会直接 UnicodeEncodeError 崩掉服务。
    # 统一强制 UTF-8（Python ≥ 3.7），无法编码的字符降级为 replace。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传则不记日志。")
    ap.add_argument("--desensitize", action="store_true",
                    help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
                         "插入零宽空格，缓解被后端内容审核误拦。默认关闭。")
    ap.add_argument("--no-compact", action="store_true",
                    help="配合 --desensitize 使用：跳过 system/harness 压缩，仅做零宽脱敏。"
                         "保留原始 system prompt 完整内容（如 Claude Code 的行为指令），"
                         "但审核误拦风险略高于默认压缩模式。")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2OPENAI_LOG")
    CONFIG["pool"] = CredentialPool(find_auth_files())
    CONFIG["port"] = args.port

    # 加载聚合渠道账号（Qoder / TraeWork / OpenCodeZen）；失败不影响主链路
    for _ch in all_channels():
        try:
            _ch.load_accounts()
        except Exception as _e:
            sys.stderr.write(f"[warn] 渠道 {_ch.KIND} 账号加载失败: {_e}\n")

    # 启动时自动签到（全部渠道，与面板「一键签到」同一实现）：
    # WorkBuddy 账号池 + Trae/Qoder；OpenCodeZen 匿名通道无签到，自动跳过。
    # 失败只告警，不阻断启动（签到属旁路能力）。
    try:
        _ck = run_all_checkins()
        _ok = sum(1 for r in _ck if r.get("ok"))
        sys.stderr.write(f"🎁 启动签到：成功 {_ok}/{len(_ck)}"
                         f"（{'、'.join(sorted({r.get('channel') or 'WorkBuddy' for r in _ck}))}）\n")
        for _r in _ck:
            if not _r.get("ok"):
                sys.stderr.write(f"   ⚠ [{_r.get('channel') or 'WorkBuddy'}] "
                                 f"{_r.get('nickname')}: {_r.get('msg')}\n")
    except Exception as _e:
        sys.stderr.write(f"[warn] 启动签到失败（不影响服务）: {_e}\n")

    if not args.skip_check:
        preflight()

    pool: CredentialPool = CONFIG["pool"]
    sys.stderr.write(f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n")
    if pool is not None and len(pool) > 0:
        sys.stderr.write(f"   账号池    : {len(pool)} 个账号（额度/认证失败自动切换下一个）\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n")
    sys.stderr.write("   POST /v1/responses          (Responses API，Codex CLI 兼容)\n")
    sys.stderr.write("   POST /v1/messages           (Anthropic API，Claude Code / CC Switch 兼容)\n")
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        mode = "零宽脱敏 + 保留全文" if args.no_compact else "零宽脱敏 + 压缩摘要"
        sys.stderr.write(f"   脱敏      : 已启用（{mode}）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log(f"==== converter 启动 ====")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


# /v2 别名路由:兼容习惯使用 /v2 前缀的客户端(与 /v1 同一处理器)
app.add_api_route("/v2/chat/completions", chat_completions, methods=["POST"])
app.add_api_route("/v2/models", list_models, methods=["GET"])


# ---------------------------------------------------------------------------
# 分平台端点：/v1/<渠道>/chat/completions 等
#
# 用途：给不同平台各自一套入口，便于「一平台一把密钥」分别管理。
# 统一的 OpenAI/Anthropic 路径保持不变；这些路径只是把模型自动归到该渠道，
# 并强制校验密钥的渠道绑定，避免某平台的密钥越权消耗其他平台额度。
# ---------------------------------------------------------------------------

def _is_platform(kind: str) -> bool:
    """合法的平台标识：workbuddy 或已注册渠道。"""
    return kind == "workbuddy" or get_channel(kind) is not None


async def _scoped_request(request: Request, kind: str) -> Request:
    """把请求体的 model 归入指定渠道（已带前缀则校验一致性），返回改写后的请求。

    - model 无前缀 → 补上 `<kind>/`（workbuddy 除外，其模型本就无前缀）
    - model 前缀与路径渠道不符 → 403（跨平台越权）
    """
    try:
        body = await request.body()
        payload = json.loads(body) if body else {}
    except Exception:
        return request
    if not isinstance(payload, dict):
        return request
    model = str(payload.get("model") or "")
    if model:
        # 显式渠道前缀（含 workbuddy/ 这种无注册渠道的写法）一律校验一致性
        if "/" in model:
            prefix = model.split("/", 1)[0]
            if prefix == "workbuddy" or get_channel(prefix) is not None:
                if prefix != kind:
                    raise HTTPException(status_code=403, detail={"error": {
                        "message": f"该端点属于 {kind} 渠道，模型 {model!r} 属于 {prefix} 渠道",
                        "type": "permission_error"}})
                return request   # 前缀已匹配，无需改写
        if kind != "workbuddy":
            payload["model"] = f"{kind}/{model}"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(request.scope, _receive)


def _require_platform_key(auth: dict, kind: str) -> None:
    """分平台端点：若密钥绑定了渠道，必须与路径渠道一致。"""
    bound = str(auth.get("channel") or "")
    if bound and bound != kind:
        raise HTTPException(status_code=403, detail={"error": {
            "message": f"该 API Key 仅限 {bound} 渠道使用，不能访问 {kind} 端点",
            "type": "permission_error"}})


@app.post("/v1/{kind}/chat/completions")
async def platform_chat(kind: str, request: Request,
                        authorization: Optional[str] = Header(default=None),
                        x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """分平台对话端点：模型自动归入 <kind> 渠道。"""
    if not _is_platform(kind):
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"未知平台: {kind}", "type": "not_found"}})
    auth = _check_auth(authorization, x_api_key)
    _require_platform_key(auth, kind)
    return await chat_completions(await _scoped_request(request, kind),
                                  authorization, x_api_key)


@app.post("/v1/{kind}/responses")
async def platform_responses(kind: str, request: Request,
                             authorization: Optional[str] = Header(default=None),
                             x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """分平台 Responses 端点（Codex CLI 兼容）。"""
    if not _is_platform(kind):
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"未知平台: {kind}", "type": "not_found"}})
    auth = _check_auth(authorization, x_api_key)
    _require_platform_key(auth, kind)
    return await create_response(await _scoped_request(request, kind),
                                 authorization, x_api_key)


@app.post("/v1/{kind}/messages")
async def platform_messages(kind: str, request: Request,
                            authorization: Optional[str] = Header(default=None),
                            x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """分平台 Anthropic Messages 端点（Claude Code 兼容）。"""
    if not _is_platform(kind):
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"未知平台: {kind}", "type": "not_found"}})
    auth = _check_auth(authorization, x_api_key)
    _require_platform_key(auth, kind)
    return await create_message(await _scoped_request(request, kind),
                                authorization, x_api_key)


@app.get("/v1/{kind}/models")
def platform_models(kind: str,
                    authorization: Optional[str] = Header(default=None),
                    x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """分平台模型清单：只列该渠道的模型。"""
    if not _is_platform(kind):
        raise HTTPException(status_code=404, detail={"error": {
            "message": f"未知平台: {kind}", "type": "not_found"}})
    auth = _check_auth(authorization, x_api_key)
    _require_platform_key(auth, kind)
    data: list[dict] = []
    if kind == "workbuddy":
        cred = _cred_for_models()
        if cred is not None:
            data = [{"id": m["name"], "object": "model", "created": 1700000000,
                     "owned_by": "workbuddy"} for m in _all_models(cred)]
    else:
        for m in _channel_models_snapshot():
            if m.get("channel") == kind:
                data.append({"id": m["id"], "object": "model", "created": 1700000000,
                             "owned_by": kind})
    return {"object": "list", "data": data}


@app.get("/v1/platforms")
def list_platforms():
    """可用平台清单及其端点（面板「接口」页展示用）。"""
    host = CONFIG.get("host") or "127.0.0.1"
    base = f"http://{host}:{CONFIG.get('port', 8787)}/v1"
    out = [{"kind": "", "name": "统一端点（全部渠道）", "prefix": "", "base_url": base,
            "models": None}]
    try:
        cred = _cred_for_models()
        wb_n = len(_all_models(cred)) if cred is not None else 0
    except Exception:
        wb_n = 0
    out.append({"kind": "workbuddy", "name": "WorkBuddy", "prefix": "",
                "base_url": f"{base}/workbuddy", "models": wb_n})
    for ch in all_channels():
        # 不过滤 NEEDS_LOGIN：OpenCodeZen 免登录匿名渠道也有专用端点（/v1/oczen/…），需一并展示
        try:
            n = len(ch.models()) if ch.accounts() else 0
        except Exception:
            n = 0
        out.append({"kind": ch.KIND, "name": ch.DISPLAY_NAME, "prefix": ch.MODEL_PREFIX,
                    "base_url": f"{base}/{ch.KIND}", "models": n})
    return {"platforms": out}


if __name__ == "__main__":
    main()
