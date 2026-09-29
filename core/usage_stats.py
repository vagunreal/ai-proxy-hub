"""流量统计：按渠道 / 模型 / 天记录 token 消耗，区分缓存内（命中）与缓存外。

统一三种 usage 口径：
  - OpenAI Chat:      prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens
  - OpenAI Responses: input_tokens / output_tokens / input_tokens_details.cached_tokens
  - Anthropic:        input_tokens（不含缓存）+ cache_read_input_tokens + cache_creation_input_tokens

术语：
  - cached（缓存内）   = 命中提示缓存的输入 token，计费远低于全价；
  - uncached（缓存外） = 未命中的输入 token（prompt - cached），按全价计费；
  - cache_write        = 写入缓存的 token（Anthropic 口径才有，OpenAI 侧通常为 0）。

落盘 usage_stats.json（原子写）。统计属旁路能力：调用方统一 try/except，
任何统计异常都不得影响主链路。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

MAX_RECENT = 60          # 内存态最近请求保留条数（供首屏快速展示）
KEEP_DAYS = 120          # 按天聚合保留天数
LOG_MAX_LINES = 20000    # 请求日志文件最大行数（超出后截断保留最新）


def _get(d: dict, *keys: str) -> int:
    """取第一个存在且为正数的整数值（0 视为缺失，继续找下一个键）。"""
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v:
            return int(v)
    return 0


def normalize_usage(u: dict | None) -> dict:
    """把三种协议的 usage 归一化为统一口径。"""
    zero = {"prompt": 0, "completion": 0, "cached": 0, "uncached": 0,
            "cache_write": 0, "total": 0}
    if not isinstance(u, dict) or not u:
        return dict(zero)

    details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}

    # 判定协议口径（三家都用 input_tokens/prompt_tokens，语义不同）：
    #   - OpenAI Chat：有 prompt_tokens，输入含缓存
    #   - OpenAI Responses：有 input_tokens_details，输入含缓存
    #   - Anthropic：有 cache_read/creation_input_tokens，且 input_tokens **不含**缓存
    # WorkBuddy 会三种字段混着下发，故先看最明确的标记（details / cache_* 键是否存在），
    # 不能只看「有 input_tokens 就算 Anthropic」。
    has_anthropic_marker = ("cache_read_input_tokens" in u) or ("cache_creation_input_tokens" in u)
    is_anthropic = has_anthropic_marker and ("prompt_tokens" not in u) and (
        "input_tokens_details" not in u)

    if is_anthropic:
        cached = _get(u, "cache_read_input_tokens") or _get(details, "cached_tokens")
        cache_write = _get(u, "cache_creation_input_tokens")
        prompt = _get(u, "input_tokens") + cached + cache_write
    else:
        # 缓存命中口径各家不同，按可靠性依次尝试（0 视为缺失继续找）
        cached = (_get(details, "cached_tokens") or _get(u, "cached_tokens")
                  or _get(u, "prompt_cache_hit_tokens")
                  or _get(u, "cache_read_input_tokens"))
        cache_write = _get(u, "cache_creation_input_tokens")
        prompt = _get(u, "prompt_tokens", "input_tokens")

    completion = _get(u, "completion_tokens", "output_tokens")
    total = _get(u, "total_tokens") or (prompt + completion)
    # 上游偶发只下发 total_tokens（缺 prompt_tokens）时反推输入，避免整条记录输入为 0
    if prompt == 0 and total > completion:
        prompt = total - completion
    return {
        "prompt": prompt,
        "completion": completion,
        "cached": cached,
        # 缓存外 = 全价部分：扣除命中缓存与缓存写入（写入按溢价单独计费，不计入全价）
        "uncached": max(prompt - cached - cache_write, 0),
        "cache_write": cache_write,
        "total": total,
        "credits": normalize_credits(u),
    }


def normalize_credits(u: dict | None) -> float:
    """提取本次请求消耗的积分（各渠道字段名不同）。

    - Qoder:   usage.credits / original_credits
    - WorkBuddy: usage.credit（CodeBuddy 计费单位）
    未下发时返回 0.0（如 OpenCodeZen 免费通道）。
    """
    if not isinstance(u, dict):
        return 0.0
    for key in ("credits", "credit", "original_credits", "cost", "total_cost"):
        v = u.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v:
            return round(float(v), 6)
    return 0.0


def _blank() -> dict:
    return {"requests": 0, "prompt": 0, "completion": 0, "cached": 0,
            "uncached": 0, "cache_write": 0, "total": 0, "credits": 0.0}


def _add(bucket: dict, n: dict, requests: int = 1) -> None:
    bucket["requests"] += requests
    for k in ("prompt", "completion", "cached", "uncached", "cache_write", "total"):
        bucket[k] += n.get(k, 0)
    bucket["credits"] = round(bucket.get("credits", 0.0) + n.get("credits", 0.0), 6)


class UsageStats:
    """线程安全的用量累加器（内存态 + JSON 落盘）。"""

    def __init__(self, path: Path, log_path: Path | None = None):
        self.path = Path(path)
        # 请求日志独立落盘（JSONL，append-only）：与统计分离，
        # 「清空统计」不会清掉日志，便于事后排查历史请求。
        self.log_path = Path(log_path) if log_path else self.path.with_name("request_log.jsonl")
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._data = self._load()

    # -- 请求日志（独立于统计，append-only） --------------------------------

    def _append_log(self, rec: dict) -> None:
        """追加一条请求日志（JSONL）。失败静默——日志属旁路能力。

        文件超过 LOG_MAX_LINES 时截断保留最新一半，避免无限增长。
        """
        try:
            with self._log_lock:
                if not hasattr(self, "_log_lines"):
                    self._log_lines = self._count_log_lines()
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
                self._log_lines += 1
                if self._log_lines > LOG_MAX_LINES:
                    self._truncate_log()
        except Exception:
            pass

    def _count_log_lines(self) -> int:
        try:
            if self.log_path.exists():
                with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                    return sum(1 for _ in f)
        except Exception:
            pass
        return 0

    def _truncate_log(self) -> None:
        """保留最新一半日志（调用方已持 _log_lock）。"""
        try:
            keep = LOG_MAX_LINES // 2
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            with open(self.log_path, "w", encoding="utf-8") as f:
                f.writelines(lines[-keep:])
            self._log_lines = min(len(lines), keep)
        except Exception:
            pass

    def read_log(self, limit: int = 200, offset: int = 0,
                 channel: str = "", model: str = "", q: str = "") -> dict:
        """分页读取请求日志（倒序，最新在前）。

        返回 {"total": 过滤后条数, "items": [...], "offset", "limit"}。
        channel/model 为精确匹配；q 为跨字段模糊匹配（账号/模型/端点）。
        """
        items: list[dict] = []
        try:
            if self.log_path.exists():
                with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            o = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(o, dict):
                            continue
                        if channel and o.get("channel") != channel:
                            continue
                        if model and o.get("model") != model:
                            continue
                        if q:
                            hay = " ".join(str(o.get(k) or "") for k in
                                           ("channel", "model", "account", "endpoint"))
                            if q.lower() not in hay.lower():
                                continue
                        items.append(o)
        except Exception:
            return {"total": 0, "items": [], "offset": offset, "limit": limit}
        items.reverse()                       # 最新在前
        total = len(items)
        limit = max(1, min(int(limit or 200), 1000))
        offset = max(0, int(offset or 0))
        return {"total": total, "items": items[offset:offset + limit],
                "offset": offset, "limit": limit}

    def clear_log(self) -> int:
        """清空请求日志，返回清掉的条数。"""
        try:
            with self._log_lock:
                n = 0
                if self.log_path.exists():
                    with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                        n = sum(1 for _ in f)
                    self.log_path.write_text("", encoding="utf-8")
                self._log_lines = 0
                return n
        except Exception:
            return 0

    # -- 持久化 -------------------------------------------------------------

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "totals" in raw:
                for key in ("by_channel", "by_model", "by_day",
                            "by_day_channel", "by_day_model", "by_account"):
                    raw.setdefault(key, {})
                raw.setdefault("recent", [])
                # 余额差分快照：必须跨重启保留，否则每次重启后首见账号不记账，
                # 期间的真实消耗会永久丢失。
                raw.setdefault("_credit_snaps", {})
                raw.setdefault("started_at", time.time())
                return raw
        except Exception:
            pass
        return {"totals": _blank(), "by_channel": {}, "by_model": {},
                "by_day": {}, "by_day_channel": {}, "by_day_model": {},
                "by_account": {}, "recent": [], "started_at": time.time()}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # -- 记录 ---------------------------------------------------------------

    def record(self, *, channel: str, model: str, usage: dict | None,
               endpoint: str = "chat", account: str = "", stream: bool = False) -> None:
        """记录一次**成功**请求的 token 消耗。"""
        n = normalize_usage(usage)
        model = (model or "unknown").strip() or "unknown"
        channel = (channel or "workbuddy").strip() or "workbuddy"
        # 模型键带渠道前缀（channel/model）：不同平台的同名模型（如 deepseek-v4-pro
        # 在 WorkBuddy 与 Qoder 都存在）必须分开统计，否则会混成一条、无法区分。
        model_key = f"{channel}/{model}"
        day = time.strftime("%Y-%m-%d")
        rec = {
            "t": int(time.time()),
            "channel": channel,
            "model": model,
            "model_key": model_key,
            "account": account or "",
            "endpoint": endpoint,
            "stream": bool(stream),
            "prompt": n["prompt"],
            "completion": n["completion"],
            "cached": n["cached"],
            "uncached": n["uncached"],
            "total": n["total"],
            "credits": n["credits"],
        }
        # 请求日志独立落盘（不受「清空统计」影响，也不随 MAX_RECENT 截断）
        self._append_log(rec)
        with self._lock:
            d = self._data
            _add(d["totals"], n)
            _add(d["by_channel"].setdefault(channel, _blank()), n)
            _add(d["by_model"].setdefault(model_key, _blank()), n)
            _add(d["by_day"].setdefault(day, _blank()), n)
            # 按天 × 渠道：前端"每日消耗"要能区分软件（此前只有按天总量，无法分辨）
            _add(d["by_day_channel"].setdefault(day, {}).setdefault(channel, _blank()), n)
            # 按天 × 渠道 × 模型：曲线图按渠道拆分且避免同名混淆
            _add(d["by_day_model"].setdefault(day, {}).setdefault(model_key, _blank()), n)
            d["recent"].insert(0, rec)
            del d["recent"][MAX_RECENT:]
            # 按天裁剪：保留最近 KEEP_DAYS 天（三张按天表同步裁剪）
            if len(d["by_day"]) > KEEP_DAYS:
                for old in sorted(d["by_day"])[:-KEEP_DAYS]:
                    d["by_day"].pop(old, None)
                    d["by_day_channel"].pop(old, None)
                    d["by_day_model"].pop(old, None)
            try:
                self._save()
            except Exception:
                pass

    def reset(self) -> None:
        with self._lock:
            self._data = {"totals": _blank(), "by_channel": {}, "by_model": {},
                          "by_day": {}, "by_day_channel": {}, "by_day_model": {},
                          "by_account": {}, "recent": [], "_credit_snaps": {},
                          "started_at": time.time()}
            try:
                self._save()
            except Exception:
                pass

    # -- 余额差分记账（上游不下发积分消耗的渠道，如 TraeWork） ----------------

    def diff_credits(self, channel: str, uid: str, remain: int,
                     account: str = "", model: str = "") -> float:
        """按「可用余额差分」记一笔积分消耗（对齐 wild-work 的 DiffCredits）。

        上游对话流里不带积分（TraeWork 实测），只能定期查余额、与上次快照比差：
          - 余额下降 → 记消耗（本次 = 上次 - 本次）；
          - 余额上升 → 不记（签到/赠送属于入项）；
          - 首次见该账号 → 只存快照不记账，否则存量余额会被记成一次巨额消耗。

        返回本次记录的消耗量（0 表示无消耗或首见）。
        """
        key = f"{channel}/{uid}"
        day = time.strftime("%Y-%m-%d")
        with self._lock:
            snaps = self._data.setdefault("_credit_snaps", {})
            prev = snaps.get(key)
            snaps[key] = {"remain": int(remain), "t": int(time.time())}
            spend = 0.0
            if prev is not None:
                delta = int(prev.get("remain", remain)) - int(remain)
                if delta > 0:
                    spend = float(delta)
                    d = self._data
                    d["totals"]["credits"] = round(d["totals"].get("credits", 0.0) + spend, 6)
                    bch = d["by_channel"].setdefault(channel, _blank())
                    bch["credits"] = round(bch.get("credits", 0.0) + spend, 6)
                    if model:
                        mk = f"{channel}/{model}"
                        bm = d["by_model"].setdefault(mk, _blank())
                        bm["credits"] = round(bm.get("credits", 0.0) + spend, 6)
                    bd = d["by_day"].setdefault(day, _blank())
                    bd["credits"] = round(bd.get("credits", 0.0) + spend, 6)
                    bdc = d["by_day_channel"].setdefault(day, {}).setdefault(channel, _blank())
                    bdc["credits"] = round(bdc.get("credits", 0.0) + spend, 6)
                    # 按天×模型也必须记：区间模式（今日/本周/本月）的按模型数据
                    # 是从 by_day_model 重算的，漏写会导致区间下模型积分恒为 0。
                    if model:
                        bdm = d.setdefault("by_day_model", {}).setdefault(day, {}).setdefault(
                            f"{channel}/{model}", _blank())
                        bdm["credits"] = round(bdm.get("credits", 0.0) + spend, 6)
                    if account:
                        ba = d["by_account"].setdefault(account, _blank())
                        ba["credits"] = round(ba.get("credits", 0.0) + spend, 6)
                    d.setdefault("recent", []).insert(0, {
                        "t": int(time.time()), "channel": channel,
                        "model": model or "", "model_key": f"{channel}/{model}" if model else "",
                        "account": account or uid, "endpoint": "credits-diff",
                        "stream": False, "prompt": 0, "completion": 0, "cached": 0,
                        "uncached": 0, "total": 0, "credits": spend,
                    })
                    del d["recent"][MAX_RECENT:]
            try:
                self._save()
            except Exception:
                pass
            return spend

    def credit_snapshot(self, channel: str, uid: str) -> int | None:
        """读取上次记录的该账号可用余额（无记录返回 None）。"""
        with self._lock:
            s = (self._data.get("_credit_snaps") or {}).get(f"{channel}/{uid}")
            return int(s["remain"]) if s else None

    # -- 读取 ---------------------------------------------------------------

    def snapshot(self, top_models: int = 30, keep_days: int = 30,
                 start: str | None = None, end: str | None = None) -> dict:
        """统计快照。

        start/end 为 "YYYY-MM-DD"（含端点，None 表示不限）。指定区间时，
        各维度（总量/渠道/模型/按天）都按区间重算——数据源是 by_day_* 系列表，
        因此区间筛选与图表口径天然一致。
        """
        with self._lock:
            d = json.loads(json.dumps(self._data))   # 深拷贝，避免前端读时被改

        def with_rate(b: dict) -> dict:
            out = dict(b)
            out["cache_hit_rate"] = round(b["cached"] * 100.0 / b["prompt"], 1) if b["prompt"] else 0.0
            out["credits"] = round(out.get("credits", 0.0), 4)
            return out

        def in_range(day: str) -> bool:
            if start and day < start:
                return False
            if end and day > end:
                return False
            return True

        all_days = sorted(d["by_day"])
        days = [x for x in all_days if in_range(x)]
        days = days[-keep_days:] if keep_days else days
        ranged = bool(start or end)

        # 请求区间的自然天数（含无数据的天），用于区分「选了几天」与「几天有数据」
        span_days = 0
        if start and end:
            try:
                s_d = time.strptime(start, "%Y-%m-%d")
                e_d = time.strptime(end, "%Y-%m-%d")
                span_days = int((time.mktime(e_d) - time.mktime(s_d)) / 86400) + 1
            except Exception:
                span_days = 0

        if ranged:
            # 区间模式：从 by_day_channel / by_day_model 重算（这两张表按天存，可精确切片）
            totals = _blank()
            by_channel: dict[str, dict] = {}
            by_model: dict[str, dict] = {}
            for day in days:
                _add(totals, d["by_day"].get(day, _blank()), requests=0)
                totals["requests"] += d["by_day"].get(day, {}).get("requests", 0)
                for ch, b in (d.get("by_day_channel", {}).get(day) or {}).items():
                    _add(by_channel.setdefault(ch, _blank()), b, requests=0)
                    by_channel[ch]["requests"] += b.get("requests", 0)
                for mk, b in (d.get("by_day_model", {}).get(day) or {}).items():
                    _add(by_model.setdefault(mk, _blank()), b, requests=0)
                    by_model[mk]["requests"] += b.get("requests", 0)
            by_day = [(day, d["by_day"][day]) for day in days]
        else:
            totals = d["totals"]
            by_channel = dict(d["by_channel"])
            by_model = dict(d["by_model"])
            by_day = [(day, d["by_day"][day]) for day in days]

        by_model_sorted = sorted(by_model.items(), key=lambda kv: kv[1].get("total", 0), reverse=True)
        by_account = sorted(d.get("by_account", {}).items(),
                            key=lambda kv: kv[1].get("credits", 0), reverse=True)

        # 曲线图数据：按天 × 渠道（每渠道一条线），以及按天 × 模型
        series_days = days
        day_channel = {day: dict((d.get("by_day_channel") or {}).get(day) or {}) for day in series_days}
        day_model = {day: dict((d.get("by_day_model") or {}).get(day) or {}) for day in series_days}

        return {
            "started_at": d.get("started_at", 0),
            "updated_at": time.time(),
            "range": {"start": start or "", "end": end or "",
                      "days": len(days),          # 有数据的天数
                      "span_days": span_days},    # 所选区间的自然天数（含空白天）
            "totals": with_rate(totals),
            "by_channel": {k: with_rate(v) for k, v in sorted(
                by_channel.items(), key=lambda kv: kv[1].get("total", 0), reverse=True)},
            "by_model": [[k, with_rate(v)] for k, v in by_model_sorted[:top_models]],
            # 图表专用：按天 × 渠道（键为 "YYYY-MM-DD"）
            "day_channel": {k: {c: with_rate(b) for c, b in v.items()} for k, v in day_channel.items()},
            # 注：by_day / by_account / day_model 仍存于落盘文件（供区间重算与后续扩展），
            # 但前端已不消费，故不再随快照下发，避免每次请求白传数 KB。
        }
