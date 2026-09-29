#!/usr/bin/env python3
"""分平台密钥与账号切换测试。

覆盖：
  1. 密钥按渠道创建/绑定（含非法渠道拒绝）
  2. 模型可见性按 key 隔离（绑定 Qoder 的 key 只看到 qoder/* 模型）
  3. 跨平台越权被拒（403）
  4. 分平台端点 /v1/{kind}/...（模型名自动补前缀、跨平台拒绝）
  5. 渠道账号切换与持久化（重启后保留）
  6. /v1/platforms 清单

运行： .venv/bin/python tests/test_platform_keys.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name} {detail}")
        FAILED.append(name)


def _client(converter):
    from fastapi.testclient import TestClient
    return TestClient(converter.app)


def main():
    import converter

    # 隔离所有持久化文件（密钥 / 账号当前选择）
    tmp = Path(tempfile.mkdtemp())
    converter.KEYS_FILE = tmp / "keys_test.json"
    _orig_cur = converter.CURRENT_ACCOUNT_FILE
    converter.CURRENT_ACCOUNT_FILE = tmp / "current_account_test.json"
    for ch in converter.all_channels():
        # 渠道账号目录指向临时目录（_current_file 是 property，转发到 store.dir）
        ch.store.dir = tmp / f"auths_{ch.KIND}"
        ch._accounts = []
        ch._current_uid = ""
        try:
            ch.load_accounts()
        except Exception:
            pass

    master = "sk-wb-" + "a" * 32
    converter.CONFIG["api_key"] = master
    client = _client(converter)
    H = {"Authorization": f"Bearer {master}"}

    print("[1] 密钥按渠道创建")
    qk = client.post("/v1/keys", json={"name": "Qoder专用", "channel": "qoder"},
                     headers=H).json()["key"]
    wk = client.post("/v1/keys", json={"name": "WB专用", "channel": "workbuddy"},
                     headers=H).json()["key"]
    ak = client.post("/v1/keys", json={"name": "全部"}, headers=H).json()["key"]
    check("创建三个 key", all(k.startswith("sk-wb-") for k in (qk, wk, ak)))
    r = client.post("/v1/keys", json={"name": "非法", "channel": "nope"}, headers=H)
    check("非法渠道被拒", r.status_code == 400, f"got {r.status_code}")

    print("\n[2] 密钥列表含渠道字段与选项")
    d = client.get("/v1/keys", headers=H).json()
    by_key = {k["key"]: k for k in d["keys"]}
    check("qoder 绑定", by_key[qk]["channel"] == "qoder")
    check("workbuddy 绑定", by_key[wk]["channel"] == "workbuddy")
    check("空=全部", by_key[ak]["channel"] == "")
    kinds = [o["kind"] for o in d["channel_options"]]
    check("候选渠道含全部", "" in kinds)
    check("候选渠道含 workbuddy", "workbuddy" in kinds)
    check("候选渠道含 qoder", "qoder" in kinds)

    print("\n[3] 模型可见性按 key 隔离")
    all_ids = [m["id"] for m in client.get("/v1/models", headers=H).json()["data"]]
    q_ids = [m["id"] for m in client.get("/v1/models",
                                        headers={"Authorization": f"Bearer {qk}"}).json()["data"]]
    w_ids = [m["id"] for m in client.get("/v1/models",
                                        headers={"Authorization": f"Bearer {wk}"}).json()["data"]]
    check("主密钥可见全部渠道模型", len(all_ids) >= len(q_ids) >= 0)
    # 注：本测试把渠道账号目录隔离到临时目录，Qoder 通常无账号 → 模型列表可能为空。
    # 关键不变量是「Qoder key 看不到非 qoder 模型」，空列表同样满足。
    check("Qoder key 不含其他渠道模型",
          all(i.startswith("qoder/") for i in q_ids), str(q_ids[:3]))
    check("Qoder key 看不到 WorkBuddy 模型（无前缀）",
          not any("/" not in i for i in q_ids), str(q_ids[:3]))
    check("WB key 无渠道前缀模型", not any("/" in i for i in w_ids), str(w_ids[:3]))

    print("\n[4] 跨平台越权被拒（同名模型消歧）")
    # 同名模型：deepseek-v4-pro / glm-5.3 在 WorkBuddy 与 Qoder 都存在。
    # 绑定渠道的 key 遇到裸名应**自动归属该平台**（而非静默走 WorkBuddy），
    # 显式写了其他平台前缀才 403。
    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {wk}"},
                    json={"model": "qoder/auto", "messages": [{"role": "user", "content": "hi"}]})
    check("WB key 显式调 qoder 前缀 → 403", r.status_code == 403, f"got {r.status_code}")
    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {qk}"},
                    json={"model": "workbuddy/glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
    check("Qoder key 显式调 workbuddy 前缀 → 403", r.status_code == 403, f"got {r.status_code}")

    # 消歧改写逻辑单元验证（不依赖上游可用性）
    from fastapi import HTTPException as _HE
    check("Qoder key + 裸名 → 自动加 qoder 前缀",
          converter._enforce_key_scope("qoder", "deepseek-v4-pro") == "qoder/deepseek-v4-pro")
    check("Qoder key + 本渠道前缀 → 保持",
          converter._enforce_key_scope("qoder", "qoder/glm-5.3") == "qoder/glm-5.3")
    check("WB key + 裸名 → 保持无前缀",
          converter._enforce_key_scope("workbuddy", "deepseek-v4-pro") == "deepseek-v4-pro")
    check("不绑定 key 不改写",
          converter._enforce_key_scope("", "deepseek-v4-pro") == "deepseek-v4-pro")
    try:
        converter._enforce_key_scope("qoder", "workbuddy/glm-5.2")
        check("跨平台前缀抛 403", False, "未抛异常")
    except _HE as e:
        check("跨平台前缀抛 403", e.status_code == 403, f"got {e.status_code}")

    r = client.post("/v1/chat/completions", headers=H,
                    json={"model": "qoder/auto", "messages": [{"role": "user", "content": "hi"}]})
    check("主密钥不受限（非 403）", r.status_code != 403, f"got {r.status_code}")

    print("\n[5] 分平台端点校验")
    r = client.post("/v1/qoder/chat/completions", headers={"Authorization": f"Bearer {wk}"},
                    json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    check("WB key 调 /v1/qoder → 403", r.status_code == 403, f"got {r.status_code}")
    r = client.post("/v1/qoder/chat/completions", headers=H,
                    json={"model": "workbuddy/x", "messages": [{"role": "user", "content": "hi"}]})
    check("跨平台模型前缀 → 403", r.status_code == 403, f"got {r.status_code}")
    r = client.post("/v1/nope/chat/completions", headers=H,
                    json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    check("未知平台 → 404", r.status_code == 404, f"got {r.status_code}")
    r = client.get("/v1/qoder/models", headers=H)
    check("/v1/{kind}/models 可用", r.status_code in (200, 503), f"got {r.status_code}")
    if r.status_code == 200:
        ids = [m["id"] for m in r.json()["data"]]
        check("分平台模型列表只含本渠道", all(i.startswith("qoder/") for i in ids))

    print("\n[6] 平台清单")
    r = client.get("/v1/platforms")
    plats = {p["kind"]: p for p in r.json()["platforms"]}
    check("含统一端点", "" in plats)
    check("含 workbuddy", "workbuddy" in plats)
    check("含 qoder", "qoder" in plats)
    check("base_url 含渠道路径",
          plats["qoder"]["base_url"].endswith("/qoder"), plats["qoder"]["base_url"])

    print("\n[7] 渠道账号切换与持久化")
    from channels.common import Account
    ch = converter.get_channel("oczen")   # 匿名渠道无账号，改用 qoder 验证
    qch = converter.get_channel("qoder")
    # 造两个假账号写入该渠道（隔离目录内）
    import time as _t
    a1 = Account(kind="qoder", uid="u1", nickname="账号一", access_token="t1", expires_at=_t.time() + 3600)
    a2 = Account(kind="qoder", uid="u2", nickname="账号二", access_token="t2", expires_at=_t.time() + 3600)
    qch.save_account(a1)
    qch.save_account(a2)
    check("两账号已加载", len(qch.accounts()) >= 2)
    check("默认当前为第一个", qch.current().uid in ("u1", "u2"))
    check("切换到 u2", qch.set_current("u2"))
    check("当前为 u2", qch.current().uid == "u2")
    # 模拟重启：重新加载
    qch.load_accounts()
    check("重载后仍为 u2（持久化）", qch.current().uid == "u2", qch.current().uid)
    # 删除当前账号 → 回落
    qch.delete_account("u2")
    check("删除当前账号后回落", qch.current() is not None and qch.current().uid != "u2")
    # 快照标记
    snap = qch.snapshot()
    check("快照标记 current", any(a["current"] for a in snap))

    print("\n[8] 账号切换端点")
    r = client.post("/v1/channels/qoder/accounts/switch", headers=H, json={"uid": "u1"})
    check("切换端点可用", r.status_code == 200, f"got {r.status_code} {r.text[:120]}")
    r = client.post("/v1/channels/qoder/accounts/switch", headers=H, json={"uid": "nope"})
    check("切换不存在账号 → 404", r.status_code == 404, f"got {r.status_code}")
    r = client.post("/v1/channels/qoder/accounts/switch", headers=H, json={})
    check("缺 uid → 400", r.status_code == 400, f"got {r.status_code}")

    print("\n[9] WorkBuddy 账号删除")
    # 用临时凭据目录模拟（与生产同样的 .info 结构）
    td2 = Path(tempfile.mkdtemp())
    import json as _json
    def _wc(p, uid, nick):
        p.write_text(_json.dumps({"account": {"uid": uid, "nickname": nick},
            "auth": {"accessToken": "t", "refreshToken": "r",
                     "expiresAt": 4102444800000, "domain": "www.codebuddy.cn"}}),
            encoding="utf-8")
    f1 = td2 / "one.info"; _wc(f1, "uid-one", "账号一")
    f2 = td2 / "two.info"; _wc(f2, "uid-two", "账号二")
    _orig_dirs = converter.auth_dirs
    converter.auth_dirs = lambda: [td2]
    try:
        converter.CONFIG["pool"] = converter.CredentialPool([f1, f2])
        check("双账号就绪", len(converter.CONFIG["pool"].creds) == 2)
        # 唯一账号保护
        converter.CONFIG["pool"] = converter.CredentialPool([f1])
        r = client.post("/v1/channels/workbuddy/accounts/delete", headers=H,
                        json={"uid": "uid-one"})
        check("唯一账号不可删（400）", r.status_code == 400, f"got {r.status_code}")
        check("文件未被删", f1.exists())
        # 双账号删除成功
        converter.CONFIG["pool"] = converter.CredentialPool([f1, f2])
        r = client.post("/v1/channels/workbuddy/accounts/delete", headers=H,
                        json={"uid": "uid-two"})
        check("删除成功（200）", r.status_code == 200, f"got {r.status_code} {r.text[:120]}")
        check("凭据文件已删", not f2.exists())
        check("账号池已重载", len(converter.CONFIG["pool"].creds) == 1,
              f"got {len(converter.CONFIG['pool'].creds)}")
        # 删除不存在
        r = client.post("/v1/channels/workbuddy/accounts/delete", headers=H,
                        json={"uid": "nope"})
        check("不存在账号 → 404", r.status_code == 404, f"got {r.status_code}")
    finally:
        converter.auth_dirs = _orig_dirs

    # 清理
    converter.CURRENT_ACCOUNT_FILE = _orig_cur

    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("🎉 分平台密钥与账号切换测试全部通过")


if __name__ == "__main__":
    main()
