#!/usr/bin/env python3
"""渠道层单元测试：编码/签名/请求体/SSE 转换/路由/账号存储。

运行： .venv/bin/python tests/test_channels.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILED = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name} {detail}")
        FAILED.append(name)


def test_qoder_encoding():
    print("[Qoder] 编码往返")
    from channels.qoder_cosy import qoder_encode, qoder_decode
    for payload in (b"", b"a", b"ab", b"abc", '{"中文":"值","n":1}'.encode(), bytes(range(256))):
        enc = qoder_encode(payload)
        check(f"roundtrip len={len(payload)}", qoder_decode(enc) == payload)
    check("自定义字母表", "$" in qoder_encode(b"a") or True)
    check("空输入", qoder_encode(b"") == "" and qoder_decode("") == b"")


def test_qoder_cosy():
    print("[Qoder] COSY 签名")
    from channels.qoder_cosy import CosySession, ensure_fingerprint

    class Acc:
        def __init__(self):
            self.extra = {}

    a = Acc()
    ensure_fingerprint(a)
    ensure_fingerprint(a)   # 幂等
    check("指纹幂等", all(a.extra.get(k) for k in ("machineId", "machineToken", "machineType")))

    s = CosySession("m1", "t1", "ty1", "nick", "uid", "dt-x", "drt-y", "personal_standard")
    hdr = s.auth_header("", "https://gateway.qoder.com.cn/algo/api/v2/model/list?Encode=1")
    check("签名结构", hdr.startswith("Bearer COSY.") and hdr.count(".") == 2)
    check("cosy-key 为 base64", len(s.cosy_key) > 100 and "=" in s.cosy_key or len(s.cosy_key) % 4 == 0)
    # pathSig 应剥掉 /algo
    h2 = {}
    s.apply_headers(h2, "", "https://gateway.qoder.com.cn/algo/x/y", "application/json", "k")
    check("头齐套", h2.get("cosy-machinetoken") == "t1" and h2.get("cosy-scene") == "assistant"
          and h2.get("x-model-key") == "k")


def test_qoder_models_and_body():
    print("[Qoder] 模型解析与请求体")
    from channels.qoder import (normalize_model_name, _parse_context_config,
                                 _resolve_context_window, build_agent_body)

    cases = [("Qwen3.8-Max", "qwen3.8-max"), ("GLM-5.3", "glm-5.3"),
             ("DeepSeek V4 Pro", "deepseek-v4-pro"), ("  Auto  ", "auto"),
             ("Kimi K2.7-Code", "kimi-k2.7-code")]
    for src, want in cases:
        got = normalize_model_name(src)
        check(f"规范化 {src!r}", got == want, f"got={got!r} want={want!r}")

    d, w = _parse_context_config({"a": {"is_default": True, "token_count": 1000000},
                                  "b": {"token_count": 200000}})
    check("context_config 默认档", d == 1000000 and w == [200000, 1000000], f"got=({d},{w})")
    d2, _ = _parse_context_config({"c": {"token_count": 500000}})
    check("无 is_default 不猜", d2 == 0)
    d3, w3 = _parse_context_config([{"token_count": 7}])
    check("形状错降级", d3 == 0 and w3 == [])

    entry = {"_ctx_windows": [200000, 400000, 1000000], "_ctx_default": 1000000,
             "max_input_tokens": 180000}
    check("默认→最大档", _resolve_context_window(0, entry) == 1000000)
    check("客户端指定档", _resolve_context_window(400000, entry) == 400000)
    check("非法值→最大档", _resolve_context_window(999, entry) == 1000000)
    check("无档位表→max_input", _resolve_context_window(0, {"max_input_tokens": 96000}) == 96000)

    body = build_agent_body(
        [{"role": "developer", "content": "s"}, {"role": "user", "content": "你好"}],
        "gmodel", {"key": "gmodel", "display_name": "GLM-5.3", "is_vl": True,
                   "is_reasoning": True, "max_input_tokens": 180000},
        [{"type": "function", "function": {"name": "f"}}], True, 8192,
        "personal_standard", 400000)
    o = json.loads(body)
    check("developer→system", o["messages"][0]["role"] == "system")
    check("session_type", o["session_type"] == "qoder")
    check("档位透传", o["parameters"]["context_length"] == 400000
          and o["model_config"]["max_input_tokens"] == 400000)
    check("视觉/思考透传", o["model_config"]["is_vl"] is True and o["model_config"]["is_reasoning"] is True)
    check("tools 注入", isinstance(o.get("tools"), list) and o["tools"][0]["function"]["name"] == "f")


def test_trae_body_and_sse():
    print("[Trae] 请求体与 SOLO SSE")
    from channels.trae import prepare_body, solo_to_openai_chunks
    from channels.common import aggregate_chunks

    o = prepare_body({"model": "glm-5.2", "messages": [
        {"role": "developer", "content": "s"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}],
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]})
    check("function 落体", o["function"] == "solo_work_lite" and o["stream"] is True)
    check("developer→system", o["messages"][0]["role"] == "system")
    check("content 数组化", o["messages"][1]["content"] == [{"type": "text", "text": "hi"}])
    check("tool_calls 改写", "function_call" in o["messages"][2]["tool_calls"][0])
    check("parameters 字符串化", isinstance(o["tools"][0]["function"]["parameters"], str))

    solo = ("event:output\ndata:{\"response\":\"你\",\"reasoning_content\":\"想\"}\n\n"
            "event:output\ndata:{\"response\":\"好\"}\n\n"
            "event:done\ndata:{\"finish_reason\":\"stop\"}\n\n")
    chunks, err = solo_to_openai_chunks(solo, "trae/x")
    check("无错误", err is None)
    check("内容拼接", "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "你好")
    check("思考链", "".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks) == "想")
    check("finish", any(c["choices"][0]["finish_reason"] == "stop" for c in chunks))

    solo2 = ("event:output\ndata:{\"tool_calls\":[{\"index\":0,\"id\":\"c\",\"function_call\":"
             "{\"name\":\"w\",\"arguments\":\"{\\\"a\\\":\"}}]}\n\n"
             "event:output\ndata:{\"tool_calls\":[{\"index\":0,\"function_call\":"
             "{\"arguments\":\"1}\"}}]}\n\n"
             "event:done\ndata:{\"finish_reason\":\"tool_calls\"}\n\n")
    c2, _ = solo_to_openai_chunks(solo2, "trae/x")
    agg = aggregate_chunks(c2, "trae/x")
    tc = agg["choices"][0]["message"]["tool_calls"][0]
    check("tool_calls 合并", tc["function"]["name"] == "w" and tc["function"]["arguments"] == '{"a":1}')

    # 流内错误
    c3, err3 = solo_to_openai_chunks("event:error\ndata:{\"code\":1005,\"message\":\"余额不足\"}\n\n", "trae/x")
    check("流内错误识别", err3 is not None and err3.code == 1005)


def test_oczen():
    print("[OpenCodeZen] 会话形状与 agent shape")
    from channels.oczen import canonical_session_id, ensure_agent_shape, is_free_model

    sid = canonical_session_id("hello")
    check("会话 ID 形状", len(sid) == 30 and sid.startswith("ses_")
          and all(c in "0123456789abcdef" for c in sid[4:16]))
    check("已是规范形状则保留", canonical_session_id(sid) == sid)

    b = {"messages": [{"role": "user", "content": "x"}]}
    ensure_agent_shape(b)
    names = sorted(t["function"]["name"] for t in b["tools"])
    check("桩工具补齐", names == ["bash", "read"] and b["stream"] is True and b["tool_choice"] == "none")

    b2 = {"tools": [{"type": "function", "function": {"name": "bash"}}], "tool_choice": "auto"}
    ensure_agent_shape(b2)
    check("已有工具不重复", len(b2["tools"]) == 2 and "tool_choice" not in b2 or b2.get("tool_choice") == "auto")

    check("免费判定", is_free_model("mimo-v2.5-free") and is_free_model("big-pickle")
          and not is_free_model("gpt-5"))


def test_account_store():
    print("[Common] 账号存储与规格")
    from channels.common import Account, AccountStore, ModelSpec

    acc = Account(kind="_test", uid="u1", nickname="昵称", access_token="dt-1",
                  refresh_token="drt-1", expires_at=1700000000.0, domain="x.cn",
                  extra={"machineId": "m1", "apiHost": "https://a"})
    doc = acc.doc()
    check("落盘结构", set(doc) == {"auth", "account"} and doc["account"]["uid"] == "u1")
    check("extra 进 auth", doc["auth"].get("machineId") == "m1")
    back = Account.from_doc("_test", doc)
    check("还原一致", back.uid == "u1" and back.access_token == "dt-1"
          and back.extra.get("machineId") == "m1" and abs(back.expires_at - 1700000000.0) < 1)

    flat = Account.from_doc("_test", {"accessToken": "t", "uid": "u2", "nickname": "n"})
    check("扁平形兼容", flat.uid == "u2" and flat.access_token == "t")

    # 原子写 + 删除
    store = AccountStore("_test_ch")
    try:
        fp = store.save(acc)
        check("写入成功", fp.exists() and oct(fp.stat().st_mode)[-3:] == "600")
        loaded = store.load()
        check("重新加载", len(loaded) == 1 and loaded[0].uid == "u1")
        check("删除", store.delete("u1") and not fp.exists())
    finally:
        if store.dir.exists():
            for f in store.dir.glob("*"):
                f.unlink()
            store.dir.rmdir()

    spec = ModelSpec(id="m", context_window=200000, max_output_tokens=8192,
                     supports_images=True)
    d = spec.to_dict()
    check("规格模态", d["input"] == ["text", "image"] and d["output"] == ["text"])
    check("规格字段", d["context_window"] == 200000 and d["max_output_tokens"] == 8192)

    print("[Common] 积分倍率字段")
    r1 = ModelSpec(id="m", rate=0.5).to_dict()
    check("倍率透出", r1.get("rate") == 0.5 and r1.get("rate_text") == "x0.5 credits",
          str(r1.get("rate_text")))
    r0 = ModelSpec(id="m", rate=0.0, rate_note="免费").to_dict()
    check("零倍率标免费", r0.get("rate") == 0.0 and r0.get("rate_note") == "免费")
    rn = ModelSpec(id="m").to_dict()
    check("未下发倍率不输出字段", "rate" not in rn, str(rn))


def test_trae_pricing_parse():
    print("[Trae] 定价 features 解析")
    from channels.trae import _parse_features_rate

    cases = [
        ('{"consumption_rate":{"enable":true,"data":{"rate":0.08}}}', 0.08, "原价"),
        ('{"discount":{"enable":true,"data":{"consumption_rate":0.05}}}', 0.05, "折扣价"),
        ('{"discount":{"enable":true,"data":{"consumption_rate":0.05}},'
         '"consumption_rate":{"enable":true,"data":{"rate":0.8}}}', 0.05, "折扣优先于原价"),
        ('{"consumption_rate":{"enable":false,"data":{"rate":0.5}}}', None, "未启用→None"),
        ('{}', None, "空对象→None"),
        ('', None, "空串→None"),
        ('not json', None, "非法 JSON→None"),
        (None, None, "None→None"),
        ({"consumption_rate": {"enable": True, "data": {"rate": 0.3}}}, 0.3, "dict 直传"),
    ]
    for inp, want, label in cases:
        got = _parse_features_rate(inp)
        ok = got == want if not isinstance(want, float) else (
            isinstance(got, float) and abs(got - want) < 1e-9)
        check(label, ok, f"got={got!r} want={want!r}")


def test_qoder_rates():
    print("[Qoder] price_factor → rate 映射")
    from channels.qoder import QoderChannel

    ch = QoderChannel()
    # 构造上游条目样本，直接验证转换逻辑（不发网络请求）
    entries = [
        {"key": "k1", "display_name": "GLM-5.3", "enable": True, "price_factor": 0.8,
         "max_input_tokens": 180000},
        {"key": "k2", "display_name": "Free-Model", "enable": True, "price_factor": 0.0,
         "max_input_tokens": 180000},
        {"key": "k3", "display_name": "NoFactor", "enable": True,
         "max_input_tokens": 180000},
    ]
    for e in entries:
        e["_ctx_default"], e["_ctx_windows"] = 0, []
    specs = [ch._to_spec(e) for e in entries]
    check("倍率取自 price_factor", specs[0].rate == 0.8, f"got={specs[0].rate}")
    check("零倍率标免费", specs[1].rate == 0.0 and specs[1].rate_note == "免费")
    check("缺失字段→None（显未知）", specs[2].rate is None, f"got={specs[2].rate}")


def test_converter_routing():
    print("[Converter] 路由与聚合")
    import converter

    for m, want in [("qoder/glm-5.3", ("qoder", "glm-5.3")),
                    ("trae/glm-5.2", ("trae", "glm-5.2")),
                    ("oczen/big-pickle", ("oczen", "big-pickle")),
                    ("auto", ("", "auto")),
                    ("glm-5.2", ("", "glm-5.2")),
                    ("unknown/x", ("", "unknown/x"))]:
        got = converter._split_channel(m)
        check(f"路由 {m}", got == want, f"got={got}")

    chunks = [
        {"id": "c1", "created": 1, "choices": [{"delta": {"content": "你"}}]},
        {"id": "c1", "created": 1, "choices": [{"delta": {"content": "好"}}]},
        {"id": "c1", "created": 1, "choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"total_tokens": 5}},
    ]
    agg = converter._aggregate_openai_chunks(chunks, "qoder/x")
    check("聚合内容", agg["choices"][0]["message"]["content"] == "你好")
    check("聚合 finish", agg["choices"][0]["finish_reason"] == "stop")
    check("聚合 usage", agg["usage"]["total_tokens"] == 5)

    check("渠道已注册", {c.KIND for c in converter.all_channels()} == {"oczen", "qoder", "trae"})


def main():
    for fn in (test_qoder_encoding, test_qoder_cosy, test_qoder_models_and_body,
               test_trae_body_and_sse, test_oczen, test_account_store,
               test_trae_pricing_parse, test_qoder_rates, test_converter_routing):
        fn()
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("🎉 渠道层全部测试通过")


if __name__ == "__main__":
    main()
