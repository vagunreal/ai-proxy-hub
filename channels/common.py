"""channels/common.py — 渠道公共设施。

包含：
  - ModelSpec：统一的模型规格（上下文 / 最大输出 / 输入模态 / 能力标记）。
    规格一律优先取上游动态值，静态表只在拉取失败时兜底。
  - Account：单个账号的凭据与元信息（含落盘路径）。
  - AccountStore：auths/<kind>/ 目录的扫描 / 读写（原子写）。
  - Channel：渠道基类（账号加载、对话、模型、刷新、登录），
    子类按需覆写；默认实现尽量保守（不做无谓的上游调用）。
  - SSE 工具：把「上游自定义事件流」转成标准 OpenAI SSE，
    以及把 SSE 聚合为单个 chat.completion（非流式）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

AUTHS_DIR = Path(__file__).resolve().parent.parent / "auths"


def log(msg: str) -> None:
    """统一日志出口：stderr（与 converter.py 的 _log 独立，避免循环依赖）。"""
    sys.stderr.write(f"[channels] {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# 模型规格
# ---------------------------------------------------------------------------

@dataclass
class ModelSpec:
    """单个模型对客户端暴露的规格。

    字段全部对应客户端「添加模型」时要填的内容，因此必须真实：
    context_window / max_output_tokens 优先用上游下发值，拿不到就不猜
    （留 0，前端显示「未知」），绝不用静态估值冒充上游真值。
    """

    id: str                      # 客户端模型名（不含渠道前缀）
    name: str = ""               # 展示名
    context_window: int = 0      # 上下文窗口（tokens）；0 = 未知
    max_output_tokens: int = 0   # 最大输出（tokens）；0 = 未知
    supports_images: bool = False
    supports_reasoning: bool = False
    supports_tools: bool = False
    context_from_api: bool = False  # 规格是否来自上游接口（False=兜底值）
    # 积分倍率：None = 上游未下发（面板显示「未知」）；0.0 = 官方免费。
    rate: float | None = None
    rate_note: str = ""          # 倍率的补充说明（如「免费」「折扣」）
    # 渠道私有透传字段（如 Qoder 的 model key、context 档位表）
    extra: dict = field(default_factory=dict)

    def input_modalities(self) -> list[str]:
        return ["text", "image"] if self.supports_images else ["text"]

    def rate_text(self) -> str:
        """倍率的展示文案（前端与 /v1/models 共用）。"""
        if self.rate is None:
            return ""
        return f"x{self.rate:g} credits"

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name or self.id,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "input": self.input_modalities(),
            "output": ["text"],
            "supports_reasoning": self.supports_reasoning,
            "supports_tools": self.supports_tools,
            "context_from_api": self.context_from_api,
        }
        if self.rate is not None:
            d["rate"] = self.rate
            d["rate_text"] = self.rate_text()
        if self.rate_note:
            d["rate_note"] = self.rate_note
        if self.extra:
            d["extra"] = self.extra
        return d


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------

@dataclass
class Account:
    """渠道账号：凭据 + 元信息。

    token / refresh_token 由渠道自行解释（Qoder 是 dt-/drt-，Trae 是 JWT）。
    extra 承接渠道私有字段（机器指纹、ApiHost 等）。
    """

    kind: str
    uid: str
    nickname: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0      # Unix 秒；0 = 未知（不主动刷新）
    domain: str = ""
    enterprise_id: str = ""
    file: Optional[Path] = None
    extra: dict = field(default_factory=dict)

    # 运行时状态（不落盘）
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def expired(self, skew: float = 60.0) -> bool:
        if not self.expires_at:
            return False
        return time.time() >= (self.expires_at - skew)

    def doc(self) -> dict:
        """落盘结构：与 wild-work auth 文件同构（嵌套 auth/account），便于互相迁移。"""
        auth: dict = {
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": int(self.expires_at * 1000) if self.expires_at else 0,
            "domain": self.domain,
        }
        auth.update({k: v for k, v in self.extra.items() if k not in ("uid", "nickname", "enterpriseId")})
        return {
            "auth": auth,
            "account": {
                "uid": self.uid,
                "nickname": self.nickname,
                "enterpriseId": self.enterprise_id,
            },
        }

    @classmethod
    def from_doc(cls, kind: str, raw: dict, file: Optional[Path] = None) -> "Account":
        """从落盘结构还原；兼容嵌套形与扁平形（同 wild-work auth.Parse）。"""
        if isinstance(raw.get("auth"), dict):
            a = dict(raw["auth"])
            acc = dict(raw.get("account") or {})
        else:
            a = dict(raw)
            acc = dict(raw)
        expires_ms = a.get("expiresAt") or 0
        expires_at = float(expires_ms) / 1000 if expires_ms else 0.0
        consumed = {
            "accessToken", "refreshToken", "expiresAt", "domain",
            "uid", "nickname", "enterpriseId",
        }
        extra = {k: v for k, v in a.items() if k not in consumed}
        return cls(
            kind=kind,
            uid=str(acc.get("uid") or a.get("uid") or ""),
            nickname=str(acc.get("nickname") or a.get("nickname") or ""),
            access_token=str(a.get("accessToken") or ""),
            refresh_token=str(a.get("refreshToken") or ""),
            expires_at=expires_at,
            domain=str(a.get("domain") or ""),
            enterprise_id=str(acc.get("enterpriseId") or a.get("enterpriseId") or ""),
            file=file,
            extra=extra,
        )


class AccountStore:
    """auths/<channel>/ 下的账号文件读写（一个账号一个 JSON 文件）。"""

    def __init__(self, kind: str):
        self.kind = kind
        self.dir = AUTHS_DIR / kind

    def load(self) -> list[Account]:
        out: list[Account] = []
        if not self.dir.is_dir():
            return out
        for f in sorted(self.dir.glob("*.json")):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                log(f"跳过无法解析的账号文件 {f.name}: {e}")
                continue
            acc = Account.from_doc(self.kind, raw, f)
            if acc.uid or acc.access_token:
                out.append(acc)
        return out

    def save(self, acc: Account) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        fname = f"{self.kind}-{acc.uid or 'account'}.json"
        fp = self.dir / fname
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(acc.doc(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, fp)
        os.chmod(fp, 0o600)
        acc.file = fp
        return fp

    def delete(self, uid: str) -> bool:
        for f in self.dir.glob("*.json"):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                acc = Account.from_doc(self.kind, raw, f)
            except Exception:
                continue
            if acc.uid == uid or f.name == uid:
                f.unlink(missing_ok=True)
                return True
        return False


# ---------------------------------------------------------------------------
# SSE 工具
# ---------------------------------------------------------------------------

def sse_chunk(model: str, delta: dict, *, cid: str = "", finish: Optional[str] = None,
              created: Optional[int] = None) -> bytes:
    """构造一帧标准 OpenAI SSE。"""
    obj = {
        "id": cid or "chatcmpl-channels",
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


def aggregate_chunks(chunks: Iterable[dict], model: str) -> dict:
    """把 OpenAI 形状的流式 chunk 列表聚合为单个 chat.completion。

    同时兼容 three-delta 形态：content / reasoning_content / tool_calls（按 index 合并）。
    """
    cid = ""
    created = 0
    content: list[str] = []
    reasoning: list[str] = []
    finish = "stop"
    usage: dict = {}
    tool_calls: dict[int, dict] = {}
    order: list[int] = []
    for obj in chunks:
        if not isinstance(obj, dict):
            continue
        cid = obj.get("id") or cid
        created = obj.get("created") or created
        if isinstance(obj.get("usage"), dict) and obj["usage"]:
            usage = obj["usage"]
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            d = ch.get("delta") or {}
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
                    order.append(idx)
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
    if order:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(order)]
    resp = {
        "id": cid or f"chatcmpl-{int(time.time()*1000)}",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
    }
    if usage:
        resp["usage"] = usage
    return resp


def iter_sse_lines(resp) -> Iterable[str]:
    """httpx 流式响应逐行产出（保留 SSE 行边界）。"""
    for line in resp.iter_lines():
        yield line


def rewrite_model_in_sse(line: str, model: str) -> str:
    """把 SSE 行内的 "model":"xxx" 替换成客户端请求的模型名。"""
    if not model or '"model"' not in line:
        return line
    return re.sub(r'"model"\s*:\s*"[^"]*"', f'"model": "{model}"', line, count=1)


# ---------------------------------------------------------------------------
# 渠道基类
# ---------------------------------------------------------------------------

class Channel:
    """渠道基类。

    子类必须提供 KIND / DISPLAY_NAME / MODEL_PREFIX，并按需覆写：
      - load_accounts()     账号加载（默认从 auths/<kind>/ 读）
      - ensure_token(acc)   确保 token 有效（含刷新）
      - chat_stream(acc, body, model) -> (status, headers, iterator|bytes)
      - fetch_models(acc)   上游模型（含规格）
      - logout/delete       账号删除
      - login_*             登录流程（生成链接 / 轮询），无登录的渠道不实现
    基类提供账号池（多账号粘性 + 失败冷却）与规格缓存。
    """

    KIND: str = ""
    DISPLAY_NAME: str = ""
    MODEL_PREFIX: str = ""       # 模型名前缀（如 "qoder/"）
    NEEDS_LOGIN: bool = True     # 匿名渠道（OpenCodeZen）为 False
    # 登录方式：poll = 生成链接后轮询上游拿凭证；callback = 用户把回调链接粘回来。
    # 前端据此决定显示「我已授权，检查状态」还是「粘贴回调链接」，
    # 也决定是否启动轮询定时器（callback 式渠道没有 login_poll，轮询会报错）。
    LOGIN_MODE: str = "poll"     # "poll" | "callback"

    def __init__(self):
        self.store = AccountStore(self.KIND)
        self._accounts: list[Account] = []
        self._lock = threading.RLock()
        self._cooldown: dict[str, float] = {}   # uid -> 冷却截止
        self._specs_cache: list[ModelSpec] = []
        self._specs_at: float = 0.0
        self._credits_cache: dict[str, dict] = {}   # uid -> {"t": epoch, "data": dict}
        # 当前选定账号（持久化到 auths/<kind>/.current）：决定候选顺序与面板「当前」标记
        self._current_uid: str = self._load_current_uid()

    # -- 账号 ---------------------------------------------------------------

    @property
    def _current_file(self) -> Path:
        return self.store.dir / ".current"

    def _load_current_uid(self) -> str:
        try:
            return json.loads(self._current_file.read_text(encoding="utf-8")).get("uid") or ""
        except Exception:
            return ""

    def _save_current_uid(self, uid: str) -> None:
        try:
            self.store.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._current_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"uid": uid}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._current_file)
        except Exception as e:
            log(f"{self.KIND} 当前账号落盘失败: {e}")

    def current(self) -> Optional[Account]:
        """当前选定账号；未选定或已失效时返回第一个账号。"""
        with self._lock:
            if not self._accounts:
                return None
            for a in self._accounts:
                if a.uid == self._current_uid:
                    return a
            return self._accounts[0]

    def set_current(self, uid: str) -> bool:
        """切换当前账号（按 uid 或凭据文件名匹配）；清除其冷却。"""
        with self._lock:
            for a in self._accounts:
                if a.uid == uid or (a.file and a.file.name == uid):
                    self._current_uid = a.uid
                    self._cooldown.pop(a.uid, None)
                    self._save_current_uid(a.uid)
                    return True
        return False

    def load_accounts(self) -> list[Account]:
        with self._lock:
            self._accounts = self.store.load()
            self._current_uid = self._load_current_uid()
            return list(self._accounts)

    def accounts(self) -> list[Account]:
        with self._lock:
            return list(self._accounts)

    def save_account(self, acc: Account) -> Path:
        fp = self.store.save(acc)
        with self._lock:
            self._accounts = [a for a in self._accounts if a.uid != acc.uid] + [acc]
        return fp

    def delete_account(self, uid: str) -> bool:
        ok = self.store.delete(uid)
        with self._lock:
            self._accounts = [a for a in self._accounts if a.uid != uid]
            self._cooldown.pop(uid, None)
            if self._current_uid == uid:
                # 当前账号被删：回落到剩余账号的第一个
                self._current_uid = self._accounts[0].uid if self._accounts else ""
                self._save_current_uid(self._current_uid)
        return ok

    def _in_cooldown(self, uid: str) -> bool:
        return time.time() < self._cooldown.get(uid, 0.0)

    def candidates(self) -> list[Account]:
        """候选顺序：当前账号（健康）→ 其他健康 → 冷却兜底。"""
        with self._lock:
            cur = self.current()
            rest = [a for a in self._accounts if cur is None or a.uid != cur.uid]
            healthy = [a for a in rest if not self._in_cooldown(a.uid)]
            cooling = [a for a in rest if self._in_cooldown(a.uid)]
            order: list[Account] = []
            if cur is not None:
                # 当前账号即便冷却也排最前（用户显式选定，优先尊重其意愿）
                order.append(cur)
            order.extend(healthy)
            order.extend(cooling)
            return order

    def report_failure(self, acc: Account, secs: float = 300.0) -> None:
        with self._lock:
            self._cooldown[acc.uid] = time.time() + secs

    def report_success(self, acc: Account) -> None:
        with self._lock:
            self._cooldown.pop(acc.uid, None)

    # -- 积分（面板展示用；30s 缓存，避免刷新时反复打上游） -------------------

    CREDITS_TTL = 30.0

    def credits(self, acc: Account) -> dict:
        """渠道积分查询（子类覆写）。默认返回「该渠道不提供积分」。"""
        return {"error": "该渠道不提供积分"}

    def credits_cached(self, acc: Account, force: bool = False) -> dict:
        """带 TTL 缓存的积分查询；失败不写缓存（下次仍会重试）。"""
        now = time.time()
        with self._lock:
            hit = self._credits_cache.get(acc.uid)
            if hit and not force and (now - hit["t"]) < self.CREDITS_TTL:
                return hit["data"]
        try:
            data = self.credits(acc)
        except Exception as e:
            data = {"error": str(e)}
        # 只缓存成功结果，失败下次立即重试
        if isinstance(data, dict) and not data.get("error"):
            with self._lock:
                self._credits_cache[acc.uid] = {"t": now, "data": data}
        return data

    def snapshot(self) -> list[dict]:
        """面板用账号状态（含积分与积分构成）。"""
        out = []
        with self._lock:
            accounts = list(self._accounts)
            cooldowns = dict(self._cooldown)
            cur_uid = self._current_uid or (accounts[0].uid if accounts else "")
        for i, a in enumerate(accounts):
            credits = None
            if self.NEEDS_LOGIN or self.KIND == "workbuddy":
                try:
                    credits = self.credits_cached(a)
                except Exception as e:
                    credits = {"error": str(e)}
            out.append({
                "kind": self.KIND,
                "uid": a.uid,
                "nickname": a.nickname or a.uid,
                "channel": self.DISPLAY_NAME,
                "current": a.uid == cur_uid,
                "can_switch": True,
                "cooldown_remaining": max(0, int(cooldowns.get(a.uid, 0) - time.time())),
                "token_expired": a.expired(),
                "token_expires_at": int(a.expires_at) if a.expires_at else 0,
                "file": a.file.name if a.file else "",
                "credits": credits,
                "extra": {k: v for k, v in a.extra.items() if not k.lower().endswith("token")},
            })
        return out

    # -- 模型 ---------------------------------------------------------------

    def models(self, force: bool = False, ttl: float = 300.0) -> list[ModelSpec]:
        """渠道模型清单（带 TTL 缓存）；无账号或拉取失败时回退上次成功值。"""
        with self._lock:
            if not force and self._specs_cache and (time.time() - self._specs_at) < ttl:
                return list(self._specs_cache)
        accounts = self.candidates()
        if not accounts:
            with self._lock:
                return list(self._specs_cache)
        for acc in accounts:
            try:
                self.ensure_token(acc)
                specs = self.fetch_models(acc)
                if specs:
                    with self._lock:
                        self._specs_cache = list(specs)
                        self._specs_at = time.time()
                    self.report_success(acc)
                    return list(specs)
            except Exception as e:
                log(f"{self.KIND} 拉取模型失败（{acc.nickname or acc.uid}）: {e}")
                continue
        with self._lock:
            return list(self._specs_cache)

    def static_models(self) -> list[ModelSpec]:
        """上游不可达时的兜底清单；默认空（宁缺毋滥）。"""
        return []

    # -- 能力（子类覆写） ---------------------------------------------------

    def ensure_token(self, acc: Account) -> None:
        """确保 token 有效；无效则刷新（原地更新并落盘由子类决定）。"""
        return None

    def chat_stream(self, acc: Account, body: dict, model: str,
                    rid: str = "") -> tuple[int, bytes]:
        """发起对话，返回 (status, payload)。

        约定实现为「阻塞读完整上游流后返回标准 OpenAI SSE 字节流」或
        「返回 (status, 上游响应对象) 由调用方流式处理」——两者其一。
        简化起见：统一返回 (status, bytes)，bytes 为完整的标准 SSE 文本；
        status>=400 时 bytes 为上游错误正文。
        """
        raise NotImplementedError

    def fetch_models(self, acc: Account) -> list[ModelSpec]:
        return self.static_models()

    def refresh_token(self, acc: Account) -> None:
        return None

    # -- 登录（子类覆写） ---------------------------------------------------

    def login_available(self) -> bool:
        return self.NEEDS_LOGIN

    def login_start(self) -> dict:
        """发起登录，返回 {url, session}（session 供 poll 使用）。"""
        raise NotImplementedError

    def login_poll(self, session: str) -> dict:
        """轮询登录状态：{status: pending|ok|error, message, account}。

        仅 LOGIN_MODE == "poll" 的渠道实现；callback 式（Trae）不实现，
        调用方应先看 LOGIN_MODE，不要盲目轮询。
        """
        raise NotImplementedError(
            f"{self.DISPLAY_NAME} 使用回调粘贴式登录（LOGIN_MODE=callback），不支持轮询")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Channel] = {}
_REGISTRY_LOCK = threading.Lock()


def register(ch: Channel) -> Channel:
    with _REGISTRY_LOCK:
        _REGISTRY[ch.KIND] = ch
    return ch


def get_channel(kind: str) -> Optional[Channel]:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(kind)


def all_channels() -> list[Channel]:
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())
