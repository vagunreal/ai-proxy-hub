"""Trae 端到端测试（mock 上游）：模型列表/去重/倍率/规格/对话/流式/工具调用/错误分类/降级。

不需要真实 Trae 账号即可运行：
    .venv/bin/python tests/test_trae_upstream.py
"""
import sys, json, threading, time, tempfile
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
import uvicorn, httpx

MOCK_PORT = 18791
MOCK = f"http://127.0.0.1:{MOCK_PORT}"

app = FastAPI()
S = {"models_status": 200, "pricing_ok": True, "chat": "normal"}

@app.post("/api/ide/v1/get_detail_param")
async def models(r: Request):
    if S["models_status"] != 200:
        return Response(json.dumps({"code":1001,"message":"unauthorized"}), status_code=S["models_status"], media_type="application/json")
    return {"config_info_list": [
        {"config_name":"glm-5.2","display_config":{"display_name":"GLM-5.2"}},
        {"config_name":"deepseek-v4.1-flash","display_config":{"display_name":"DeepSeek V4.1 Flash"}},
        {"config_name":"custom_model_x","display_config":{"display_name":"第三方","is_custom_model":True}},
        {"config_name":"glm-5.2","display_config":{"display_name":"GLM-5.2"}},   # 重复
        {"config_name":"","display_config":{"display_name":"空"}},                # 空名
    ]}

@app.get("/api/remote/v1/models")
async def pricing(r: Request):
    if not S["pricing_ok"]:
        return Response("{}", status_code=500)
    return {"code":0,"data":{"list":[
        {"function":"solo_work_remote","models":[
            {"name":"glm-5.2","display_name":"GLM","features":json.dumps({"consumption_rate":{"enable":True,"data":{"rate":0.29}}})},
            {"name":"deepseek-v4.1-flash","display_name":"DS","features":json.dumps({"consumption_rate":{"enable":True,"data":{"rate":0.11}}})},
        ]},
        {"function":"solo_agent_remote","models":[
            {"name":"glm-5.2","display_name":"GLM","features":json.dumps({"consumption_rate":{"enable":True,"data":{"rate":0.9}}})},
            {"name":"seed-pro","display_name":"Seed","features":json.dumps({"consumption_rate":{"enable":True,"data":{"rate":0.08}}})},
        ]},
    ]}}

@app.post("/api/agent/v3/llm_utils_chat")
async def chat(r: Request):
    mode = S["chat"]
    if mode == "401":
        return Response(json.dumps({"code":1001,"message":"unauthorized"}), status_code=401, media_type="application/json")
    if mode == "solo_error":
        # SSE 流内业务错误
        return Response("event:error\ndata:{\"code\":1005,\"message\":\"积分不足\"}\n\n",
                        media_type="text/event-stream")
    if mode == "tool_calls":
        sse = ('event:output\ndata:{"tool_calls":[{"index":0,"id":"call_1","type":"function",'
               '"function_call":{"name":"get_weather","arguments":"{\\"city\\":"}}]}\n\n'
               'event:output\ndata:{"tool_calls":[{"index":0,"function_call":{"arguments":"\\"北京\\"}"}}]}\n\n'
               'event:token_usage\ndata:{"prompt_tokens":50,"completion_tokens":20,"total_tokens":70}\n\n'
               'event:done\ndata:{"finish_reason":"tool_calls"}\n\n')
        return Response(sse, media_type="text/event-stream")
    # 正常：含思考链 + 内容 + usage
    sse = ('id:1\nevent:metadata\ndata:{"model":"","session_id":"s1"}\n\n'
           'event:output\ndata:{"response":"你","reasoning_content":"思考中"}\n\n'
           'event:output\ndata:{"response":"好"}\n\n'
           'event:token_usage\ndata:{"prompt_tokens":30,"completion_tokens":5,"total_tokens":35}\n\n'
           'event:done\ndata:{"finish_reason":"stop"}\n\n')
    return Response(sse, media_type="text/event-stream")

def run():
    uvicorn.run(app, host="127.0.0.1", port=MOCK_PORT, log_level="error")

threading.Thread(target=run, daemon=True).start()
time.sleep(2.5)

import channels.trae as T
T.AGENT_HOST = MOCK
T.WORK_HOST = MOCK

from channels.common import get_channel, Account
ch = get_channel("trae")
old = ch.store.dir
ch.store.dir = Path(tempfile.mkdtemp()); ch._accounts = []; ch._current_uid = ""
acc = Account(kind="trae", uid="u1", nickname="测试号", access_token="AT",
              refresh_token="RT", expires_at=time.time()+7200, domain="trae.cn",
              extra={"machineId":"m","deviceId":"d","apiHost":MOCK})
ch.save_account(acc)

P=F=0
def ck(n, c, d=""):
    global P,F
    if c: P+=1; print(f"  ✓ {n}")
    else: F+=1; print(f"  ✗ {n} {d}")

print("=== 1) 模型列表（去重/过滤自定义/过滤空名）===")
ms = ch.fetch_models(acc)
names = [m.id for m in ms]
ck("返回 2 个模型", len(ms)==2, str(names))
ck("含 glm-5.2", "glm-5.2" in names)
ck("排除 custom_model_ 前缀", not any("custom_model" in n for n in names))
ck("排除空名", "" not in names)

print()
print("=== 2) 倍率（主 function 优先）===")
glm = next(m for m in ms if m.id=="glm-5.2")
ds  = next(m for m in ms if m.id=="deepseek-v4.1-flash")
ck("glm-5.2 倍率取主 function(0.29)", glm.rate==0.29, f"got={glm.rate}")
ck("deepseek 倍率 0.11", ds.rate==0.11, f"got={ds.rate}")

print()
print("=== 3) 规格（Trae 上游不下发，应为 0=未知）===")
d = glm.to_dict()
ck("上下文=0（显示未知）", d["context_window"]==0)
ck("最大输出=0（显示未知）", d["max_output_tokens"]==0)
ck("context_from_api=False", d["context_from_api"] is False)

print()
print("=== 4) 对话（流式，含思考链）===")
st, raw = ch.chat_stream(acc, {"model":"glm-5.2","messages":[{"role":"user","content":"hi"}],"stream":True}, "glm-5.2")
txt = raw.decode()
ck("HTTP 200", st==200, f"status={st}")
ck("含 content 增量", '"content": "你"' in txt or '"content":"你"' in txt)
ck("含 reasoning_content", "思考中" in txt)
ck("含 [DONE]", "[DONE]" in txt)
ck("model 回填", '"model": "glm-5.2"' in txt or '"model":"glm-5.2"' in txt)

print()
print("=== 5) 对话（非流式聚合）===")
st, raw = ch.chat_stream(acc, {"model":"glm-5.2","messages":[{"role":"user","content":"hi"}],"stream":False}, "glm-5.2")
o = json.loads(raw)
m = o["choices"][0]["message"]
ck("HTTP 200", st==200)
ck("聚合内容=你好", m.get("content")=="你好", f"got={m.get('content')!r}")
ck("聚合思考链", m.get("reasoning_content")=="思考中")
ck("聚合 usage", o.get("usage",{}).get("total_tokens")==35, str(o.get("usage")))

print()
print("=== 6) tool_calls 流式合并 ===")
S["chat"]="tool_calls"
st, raw = ch.chat_stream(acc, {"model":"glm-5.2","messages":[{"role":"user","content":"天气"}],"stream":False}, "glm-5.2")
o = json.loads(raw); tc = o["choices"][0]["message"].get("tool_calls",[{}])[0]
ck("工具名", tc.get("function",{}).get("name")=="get_weather", str(tc))
ck("参数拼接完整", tc.get("function",{}).get("arguments")=='{"city":"北京"}', str(tc.get("function",{}).get("arguments")))
ck("finish_reason=tool_calls", o["choices"][0]["finish_reason"]=="tool_calls")

print()
print("=== 7) 流内业务错误（1005 积分不足 → 402）===")
S["chat"]="solo_error"
st, raw = ch.chat_stream(acc, {"model":"glm-5.2","messages":[{"role":"user","content":"x"}],"stream":False}, "glm-5.2")
ck("映射为 402（积分不足）", st==402, f"got={st}")
ck("错误信息可读", b"1005" in raw or "积分" in raw.decode("utf-8","replace"))

print()
print("=== 8) 上游 401 透传 ===")
S["chat"]="401"
st, raw = ch.chat_stream(acc, {"model":"glm-5.2","messages":[{"role":"user","content":"x"}],"stream":False}, "glm-5.2")
ck("HTTP 401 透传", st==401, f"got={st}")

print()
print("=== 9) 定价接口失败时降级（不崩）===")
S["pricing_ok"]=False
ms2 = ch.fetch_models(acc)
ck("仍返回模型列表", len(ms2)>=2, str(len(ms2)))
ck("倍率为 None（未知）", all(m.rate is None for m in ms2), str([m.rate for m in ms2]))

print()
print("=== 10) 模型列表 401 时降级 ===")
S["models_status"]=401
try:
    ch.fetch_models(acc); ck("应抛异常", False)
except Exception as e:
    ck("抛异常（由上层捕获降级）", "401" in str(e))
ck("models() 不崩溃", isinstance(ch.models(), list))

ch.store.dir = old; ch._accounts=[]; ch._current_uid=""
try: ch.load_accounts()
except: pass

print()
print(f"{'🎉 全部通过' if F==0 else f'❌ {F} 项失败'} ({P} 项)")
sys.exit(1 if F else 0)
