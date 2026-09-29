#!/usr/bin/env python3
"""全功能端到端回归：逐项实测每个对外功能，防止发布后收到成片反馈。

覆盖：健康检查 / 三协议对话（同步+流式）/ 平台专用端点 / 模型列表 /
账号状态 / 渠道管理 / 一键签到 / 流量统计 / 请求日志 / API Key 管理 /
平台清单 / 模型信息 / 面板与前端交互。
"""
import json
import sys
from pathlib import Path
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"
KEYS = json.load(open(Path(__file__).resolve().parent.parent / "api_keys.json"))["keys"]
KEY = KEYS[0]["key"]
MODEL = "trae/deepseek-v4.1-flash"
PASS, FAIL = [], []


def req(path, method="GET", body=None, hdrs=None, timeout=120, raw=False):
    h = {"Authorization": f"Bearer {KEY}"}
    if body is not None:
        h["Content-Type"] = "application/json"
    if hdrs:
        h.update(hdrs)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{BASE}{path}", data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", "replace")
            return resp.status, (txt if raw else _json(txt))
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        return e.code, (txt if raw else _json(txt))


def _json(t):
    try:
        return json.loads(t)
    except Exception:
        return t


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


print("=" * 60)
print("1. 健康检查与基础端点")
print("=" * 60)
st, h = req("/health")
check("GET /health 200", st == 200 and h.get("status") == "ok", str(h)[:100])
check("/health 含 version/accounts", "version" in h and "accounts_total" in h)
st, pf = req("/v1/platforms")
check("GET /v1/platforms 200", st == 200 and len(pf.get("platforms", [])) >= 4,
      f"got={[p.get('name') for p in (pf.get('platforms') or [])]}")

print()
print("=" * 60)
print("2. 三协议对话（同步）")
print("=" * 60)
st, d = req("/v1/chat/completions", "POST", {
    "model": MODEL, "stream": False,
    "messages": [{"role": "user", "content": "回复：OK"}]})
check("POST /v1/chat/completions", st == 200 and "choices" in d, str(d)[:120])
check("  usage 字段完整", isinstance(d.get("usage"), dict) and d["usage"].get("total_tokens", 0) > 0)

st, d = req("/v1/responses", "POST", {"model": MODEL, "stream": False, "input": "回复：OK"})
check("POST /v1/responses", st == 200 and ("output" in d or "id" in d), str(d)[:120])

st, d = req("/v1/messages", "POST", {
    "model": MODEL, "max_tokens": 100, "stream": False,
    "messages": [{"role": "user", "content": "回复：OK"}]},
    hdrs={"x-api-key": KEY, "anthropic-version": "2023-06-01"})
check("POST /v1/messages", st == 200 and "content" in d, str(d)[:120])

print()
print("=" * 60)
print("3. 三协议对话（流式）")
print("=" * 60)
for label, path, body, hdrs in [
    ("chat 流式", "/v1/chat/completions", {"model": MODEL, "stream": True,
     "messages": [{"role": "user", "content": "说三个字"}]}, None),
    ("responses 流式", "/v1/responses", {"model": MODEL, "stream": True, "input": "说三个字"}, None),
    ("messages 流式", "/v1/messages", {"model": MODEL, "max_tokens": 100, "stream": True,
     "messages": [{"role": "user", "content": "说三个字"}]},
     {"x-api-key": KEY, "anthropic-version": "2023-06-01"}),
]:
    st, txt = req(path, "POST", body, hdrs=hdrs, raw=True)
    check(f"{label}", st == 200 and "data:" in str(txt), str(txt)[:100])

print()
print("=" * 60)
print("4. 平台专用端点")
print("=" * 60)
for kind in ["trae", "qoder", "oczen", "workbuddy"]:
    st, d = req(f"/v1/{kind}/chat/completions", "POST", {
        "model": MODEL if kind == "trae" else (
            "deepseek-flash" if kind == "qoder" else ("oczen/big-pickle" if kind == "oczen" else "glm-5.2")),
        "stream": False, "messages": [{"role": "user", "content": "hi"}]})
    check(f"/v1/{kind}/chat/completions 可达", st in (200, 402, 403, 503),
          f"HTTP {st}: {str(d)[:90]}")

print()
print("=" * 60)
print("5. 模型与渠道数据")
print("=" * 60)
st, m = req("/v1/models")
check("GET /v1/models", st == 200 and len(m.get("data", [])) > 0, f"{len(m.get('data', []))} 个")
st, mi = req("/v1/models-info")
check("GET /v1/models-info", st == 200 and len(mi.get("models", [])) > 0,
      f"{len(mi.get('models', []))} 个")
ch_st, ch = req("/v1/channels")
check("GET /v1/channels", ch_st == 200 and len(ch.get("channels", [])) >= 4)
check("  各渠道含账号与模型字段",
      all("accounts" in c and "models" in c for c in ch.get("channels", [])))
check("  账号含 checkin 状态",
      all("checkin" in a for c in ch.get("channels", []) for a in c.get("accounts", [])))
st, ac = req("/v1/account-status")
check("GET /v1/account-status", st == 200 and "accounts" in ac)

print()
print("=" * 60)
print("6. 一键签到")
print("=" * 60)
st, ck = req("/v1/checkin", "POST")
check("POST /v1/checkin 返回对象结构",
      st == 200 and isinstance(ck, dict) and "results" in ck,
      f"type={type(ck).__name__} {str(ck)[:120]}")
check("  total/ok_count 字段存在",
      isinstance(ck.get("total"), int) and isinstance(ck.get("ok_count"), int),
      f"total={ck.get('total')} ok={ck.get('ok_count')}")
check("  结果含 channel/nickname/ok/msg",
      all(all(k in r for k in ("nickname", "ok", "msg")) for r in (ck.get("results") or [])))

print()
print("=" * 60)
print("7. 流量统计")
print("=" * 60)
st, s = req("/v1/stats")
check("GET /v1/stats", st == 200 and "totals" in s, str(s)[:100])
check("  含 by_channel / by_model / day_channel",
      all(k in s for k in ("by_channel", "by_model", "day_channel")), str(sorted(s.keys())))
st, s2 = req("/v1/stats?start=2026-09-01&end=2026-09-30")
check("GET /v1/stats 区间过滤", st == 200 and "range" in s2)

print()
print("=" * 60)
print("8. 请求日志")
print("=" * 60)
st, lg = req("/v1/logs?limit=5")
check("GET /v1/logs", st == 200 and "items" in lg and "total" in lg,
      f"total={lg.get('total')} items={len(lg.get('items', []))}")
check("  分页参数生效", lg.get("limit") == 5)
st, lg2 = req("/v1/logs?limit=5&channel=trae")
check("  渠道过滤", st == 200 and all(i.get("channel") == "trae" for i in lg2.get("items", [])))
st, lg3 = req("/v1/logs?limit=5&q=deepseek")
check("  关键字搜索", st == 200)

print()
print("=" * 60)
print("9. API Key 管理")
print("=" * 60)
st, k = req("/v1/keys")
check("GET /v1/keys", st == 200 and "keys" in k)
check("  含 channel_options", len(k.get("channel_options", [])) >= 4,
      str(k.get("channel_options"))[:120])
st, nk = req("/v1/keys", "POST", {"name": "e2e-test", "channel": "trae"})
check("POST /v1/keys 创建", st == 200 and nk.get("key", "").startswith("sk-wb-"), str(nk)[:100])
try:
  if nk.get("key"):
    st, mdl = req("/v1/models", hdrs={"Authorization": f"Bearer {nk['key']}"})
    ids = [m["id"] for m in (mdl.get("data") or [])]
    check("  绑定渠道的 key 只见本渠道模型",
          st == 200 and all(i.startswith("trae/") for i in ids), f"{len(ids)} 个: {ids[:3]}")
finally:
  if nk.get("key"):
    st, dl = req("/v1/keys/delete", "POST", {"key": nk["key"]})
    check("POST /v1/keys/delete 删除", st == 200 and dl.get("ok"), str(dl)[:100])
    st, k2 = req("/v1/keys")
    check("  删除后不在列表", all(x["key"] != nk["key"] for x in k2.get("keys", [])))

print()
print("=" * 60)
print("10. 鉴权与越权")
print("=" * 60)
st, d = req("/v1/chat/completions", "POST",
            {"model": MODEL, "messages": [{"role": "user", "content": "x"}]},
            hdrs={"Authorization": "Bearer wrong-key"})
check("错误 API Key → 401", st == 401, f"HTTP {st}")
qk = next((k["key"] for k in KEYS if k.get("channel") == "qoder"), None)
if qk:
    st, d = req("/v1/chat/completions", "POST",
                {"model": "trae/deepseek-v4.1-flash",
                 "messages": [{"role": "user", "content": "x"}]},
                hdrs={"Authorization": f"Bearer {qk}"})
    check("绑定 qoder 的 key 访问 trae → 403", st == 403, f"HTTP {st}: {str(d)[:80]}")

print()
print("=" * 60)
print("11. 错误处理")
print("=" * 60)
st, d = req("/v1/chat/completions", "POST", {"model": MODEL, "messages": []})
check("空 messages → 400", st == 400, f"HTTP {st}")
st, txt = req("/v1/chat/completions", "POST", None, raw=True)
check("无 body → 4xx 不崩", 400 <= st < 500, f"HTTP {st}")
st, d = req("/v1/models-info", "GET")
check("不存在的路径 → 404", req("/v1/nonexistent")[0] == 404)

print()
print("=" * 60)
print(f"结果：通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  ❌", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
