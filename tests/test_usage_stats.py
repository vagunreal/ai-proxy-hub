#!/usr/bin/env python3
"""流量统计单元测试：三种 usage 口径归一化 + 累加 + 落盘。

运行： .venv/bin/python tests/test_usage_stats.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.usage_stats import UsageStats, normalize_usage, _blank   # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name} {detail}")
        FAILED.append(name)


def test_normalize():
    print("[归一化] OpenAI Chat 口径")
    n = normalize_usage({"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
                         "prompt_tokens_details": {"cached_tokens": 800}})
    check("prompt/completion", n["prompt"] == 1000 and n["completion"] == 200)
    check("cached", n["cached"] == 800)
    check("uncached=prompt-cached", n["uncached"] == 200, f"got={n['uncached']}")

    print("[归一化] OpenAI Responses 口径")
    n = normalize_usage({"input_tokens": 500, "output_tokens": 50, "total_tokens": 550,
                         "input_tokens_details": {"cached_tokens": 400}})
    check("input→prompt", n["prompt"] == 500 and n["completion"] == 50)
    check("cached", n["cached"] == 400 and n["uncached"] == 100)

    print("[归一化] Anthropic 口径")
    n = normalize_usage({"input_tokens": 300, "output_tokens": 80,
                         "cache_read_input_tokens": 700, "cache_creation_input_tokens": 50})
    check("prompt 含缓存", n["prompt"] == 300 + 700 + 50, f"got={n['prompt']}")
    check("cached=读取量", n["cached"] == 700)
    check("cache_write", n["cache_write"] == 50)
    check("uncached", n["uncached"] == 300)
    check("total 自动推导", n["total"] == 300 + 700 + 50 + 80, f"got={n['total']}")

    print("[归一化] WorkBuddy 混合字段（回归：cache_read_input_tokens:0 不得覆盖真实命中）")
    n = normalize_usage({
        "prompt_tokens": 2606, "completion_tokens": 12, "total_tokens": 2618,
        "prompt_tokens_details": {"cached_tokens": 2560},
        "prompt_cache_hit_tokens": 2560, "prompt_cache_miss_tokens": 46,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "prompt_cache_write_tokens": 0,
    })
    check("缓存内取真实命中值", n["cached"] == 2560, f"got={n['cached']}")
    check("输入取 prompt_tokens", n["prompt"] == 2606, f"got={n['prompt']}")
    check("缓存外=输入-缓存内", n["uncached"] == 46, f"got={n['uncached']}")

    print("[归一化] 积分消耗（各渠道字段名不同）")
    q = normalize_usage({"prompt_tokens": 15, "completion_tokens": 186, "total_tokens": 201,
                         "credits": 0.003372996, "original_credits": 0.003372996})
    check("Qoder credits", abs(q["credits"] - 0.003373) < 1e-6, f"got={q['credits']}")
    w = normalize_usage({"prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 21,
                         "credit": 0.01})
    check("WorkBuddy credit", w["credits"] == 0.01, f"got={w['credits']}")
    f = normalize_usage({"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
    check("免费渠道无积分 → 0", f["credits"] == 0.0)
    check("credits 为 0 不误取 original", normalize_usage(
        {"prompt_tokens": 1, "credits": 0, "original_credits": 0.5})["credits"] == 0.5)

    print("[归一化] 边界")
    z = normalize_usage(None)
    check("None → 全零", all(v == 0 for v in z.values()))
    check("空 dict → 全零", all(v == 0 for v in normalize_usage({}).values()))
    neg = normalize_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 300}})
    check("cached>prompt 时 uncached 钳 0", neg["uncached"] == 0, f"got={neg['uncached']}")


def test_accumulate():
    print("[累加] 多渠道/多模型/按天")
    with tempfile.TemporaryDirectory() as td:
        st = UsageStats(Path(td) / "usage.json")
        st.record(channel="workbuddy", model="glm-5.2",
                  usage={"prompt_tokens": 1000, "completion_tokens": 100,
                         "prompt_tokens_details": {"cached_tokens": 900}})
        st.record(channel="workbuddy", model="glm-5.2",
                  usage={"prompt_tokens": 2000, "completion_tokens": 200,
                         "prompt_tokens_details": {"cached_tokens": 1500}})
        st.record(channel="oczen", model="big-pickle",
                  usage={"prompt_tokens": 500, "completion_tokens": 50,
                         "prompt_tokens_details": {"cached_tokens": 0}})
        # 带积分的渠道请求
        st.record(channel="qoder", model="qwen3.7-flash",
                  usage={"prompt_tokens": 15, "completion_tokens": 186, "total_tokens": 201,
                         "credits": 0.003372996})

        s = st.snapshot()
        t = s["totals"]
        check("请求数", t["requests"] == 4)
        check("积分累计", abs(t["credits"] - 0.0034) < 1e-4, f"got={t['credits']}")
        check("按渠道积分", abs(s["by_channel"]["qoder"]["credits"] - 0.0034) < 1e-4,
              f"got={s['by_channel']['qoder']['credits']}")
        check("免费渠道积分为 0", s["by_channel"]["oczen"]["credits"] == 0.0)
        check("每日积分（按天×渠道口径）",
              s["day_channel"][list(s["day_channel"])[0]]["qoder"]["credits"] > 0,
              f"got={s['day_channel']}")
        check("prompt 合计", t["prompt"] == 3515, f"got={t['prompt']}")
        check("cached 合计", t["cached"] == 2400, f"got={t['cached']}")
        check("uncached 合计", t["uncached"] == 1115, f"got={t['uncached']}")
        check("output 合计", t["completion"] == 536, f"got={t['completion']}")
        check("命中率", abs(t["cache_hit_rate"] - 68.3) < 0.2, f"got={t['cache_hit_rate']}")

        check("按渠道分组", set(s["by_channel"]) == {"workbuddy", "oczen", "qoder"})
        check("workbuddy 计数", s["by_channel"]["workbuddy"]["requests"] == 2)
        check("oczen 计数", s["by_channel"]["oczen"]["requests"] == 1)
        check("qoder 计数", s["by_channel"]["qoder"]["requests"] == 1)
        check("按模型排序（用量降序，键带渠道前缀）",
              s["by_model"][0][0] == "workbuddy/glm-5.2", f"got={s['by_model'][0][0]}")
        check("按天有数据（day_channel 覆盖当日）", len(s["day_channel"]) == 1)
        check("请求日志（recent 内存态，前端已改用 /v1/logs）",
              len(st._data.get("recent") or []) == 4
              and st._data["recent"][0]["channel"] == "qoder",
              f"got={len(st._data.get('recent') or [])}")
        check("每日×渠道（by_day_channel）",
              isinstance(s.get("day_channel"), dict) and len(s["day_channel"]) == 1
              and set(s["day_channel"][list(s["day_channel"])[0]]) >= {"workbuddy", "qoder"},
              f"got={s.get('day_channel')}")
        check("按天×模型明细仍在落盘（不下发但保留）",
              isinstance(st._data.get("by_day_model"), dict)
              and len(st._data["by_day_model"]) == 1,
              f"got={list(st._data.get('by_day_model', {}))}")
        check("同名模型分平台（key 带渠道前缀）",
              all("/" in k for k, _ in s["by_model"]), f"got={[k for k,_ in s['by_model']]}")

        # 落盘 + 重载
        st2 = UsageStats(Path(td) / "usage.json")
        s2 = st2.snapshot()
        check("持久化一致", s2["totals"]["requests"] == 4 and s2["totals"]["cached"] == 2400)
        check("积分持久化", abs(s2["totals"]["credits"] - 0.0034) < 1e-4,
              f"got={s2['totals']['credits']}")

        # 重置
        st2.reset()
        s3 = st2.snapshot()
        check("重置清空", s3["totals"]["requests"] == 0 and not s3["by_channel"])

        # 损坏文件降级
        (Path(td) / "usage.json").write_text("{bad json", encoding="utf-8")
        st3 = UsageStats(Path(td) / "usage.json")
        check("损坏文件降级为空", st3.snapshot()["totals"]["requests"] == 0)


def test_range_filter():
    print("[区间筛选] 按日期切片统计")
    with tempfile.TemporaryDirectory() as td:
        st = UsageStats(Path(td) / "u.json")
        st.record(channel="qoder", model="glm-5.3",
                  usage={"prompt_tokens": 1000, "completion_tokens": 100, "credits": 1.0})
        st.record(channel="workbuddy", model="glm-5.3",
                  usage={"prompt_tokens": 2000, "completion_tokens": 200, "credits": 2.0})
        # 手工注入历史天（record 只会写当天）
        with st._lock:
            for day, ch, mk, tot, cr in (
                ("2026-01-01", "workbuddy", "workbuddy/a", 500, 0.5),
                ("2026-01-02", "qoder", "qoder/b", 700, 0.7),
                ("2026-01-03", "workbuddy", "workbuddy/c", 900, 0.9),
            ):
                d0 = st._data
                d0["by_day"].setdefault(day, _blank())
                d0["by_day"][day]["total"] += tot
                d0["by_day"][day]["credits"] += cr
                d0["by_day"][day]["requests"] += 1
                d0["by_day_channel"].setdefault(day, {}).setdefault(ch, _blank())
                d0["by_day_channel"][day][ch]["total"] += tot
                d0["by_day_channel"][day][ch]["credits"] += cr
                d0["by_day_model"].setdefault(day, {}).setdefault(mk, _blank())
                d0["by_day_model"][day][mk]["total"] += tot

        s_all = st.snapshot()
        check("全部：4 天（3 历史 + 今天）", s_all["range"]["days"] == 4, f"got={s_all['range']}")
        check("全部：总量含所有天", s_all["totals"]["total"] > 3000, f"got={s_all['totals']['total']}")

        s1 = st.snapshot(start="2026-01-01", end="2026-01-02")
        check("区间 01-01~01-02：2 天", s1["range"]["days"] == 2, f"got={s1['range']}")
        check("区间总量 = 500+700", s1["totals"]["total"] == 1200, f"got={s1['totals']['total']}")
        check("区间积分 = 0.5+0.7", abs(s1["totals"]["credits"] - 1.2) < 1e-6,
              f"got={s1['totals']['credits']}")
        check("区间内渠道正确", set(s1["by_channel"]) == {"workbuddy", "qoder"},
              f"got={set(s1['by_channel'])}")
        check("区间内模型正确", {k for k, _ in s1["by_model"]} == {"workbuddy/a", "qoder/b"},
              f"got={[k for k, _ in s1['by_model']]}")
        check("区间内按天 = 2 条（day_channel）", len(s1["day_channel"]) == 2,
              f"got={len(s1['day_channel'])}")
        check("day_channel 只含区间内天",
              set(s1["day_channel"]) == {"2026-01-01", "2026-01-02"}, f"got={set(s1['day_channel'])}")

        s2 = st.snapshot(start="2026-01-03", end="2026-01-03")
        check("单日区间总量 = 900", s2["totals"]["total"] == 900, f"got={s2['totals']['total']}")
        check("单日区间渠道只剩 workbuddy", set(s2["by_channel"]) == {"workbuddy"},
              f"got={set(s2['by_channel'])}")

        s3 = st.snapshot(start="2020-01-01", end="2020-01-02")
        check("无数据区间 → 全 0", s3["totals"]["total"] == 0 and not s3["by_channel"],
              f"got={s3['totals']['total']}")
        check("无数据区间 days=0", s3["range"]["days"] == 0, f"got={s3['range']}")

        s4 = st.snapshot(start="2026-01-02")
        check("只给 start（到最新）", s4["range"]["days"] == 3, f"got={s4['range']}")


def test_credit_diff_by_model():
    """余额差分记账必须同时写 by_day_model：区间模式（今日/本周）的按模型
    数据从该表重算，漏写会导致「渠道有积分、模型显示 0」的假象。"""
    print("[余额差分] 按天×模型积分（区间模式下模型积分不为 0）")
    with tempfile.TemporaryDirectory() as td:
        st = UsageStats(Path(td) / "usage.json", log_path=Path(td) / "log.jsonl")
        st.diff_credits("trae", "u1", 1000, account="acc", model="m1")   # 首见建快照
        st.diff_credits("trae", "u1", 800, account="acc", model="m1")    # 消耗 200
        all_s = st.snapshot()
        day_s = st.snapshot(start="2020-01-01", end="2099-12-31")        # 区间模式
        check("全部模式 渠道积分 = 200", all_s["by_channel"]["trae"]["credits"] == 200,
              f"got={all_s['by_channel']['trae']['credits']}")
        check("全部模式 模型积分 = 200", all_s["by_model"][0][1]["credits"] == 200,
              f"got={all_s['by_model'][0][1]['credits']}")
        check("区间模式 渠道积分 = 200", day_s["by_channel"]["trae"]["credits"] == 200,
              f"got={day_s['by_channel']['trae']['credits']}")
        check("区间模式 模型积分 = 200（不得为 0）",
              day_s["by_model"][0][1]["credits"] == 200,
              f"got={day_s['by_model'][0][1]['credits']}")
        # 余额上升（签到）不计消耗
        st.diff_credits("trae", "u1", 1300, account="acc", model="m1")
        check("余额上升不计消耗", st.snapshot()["by_channel"]["trae"]["credits"] == 200)


def test_isolated():
    print("[静默失败] 统计异常不影响主链路")
    with tempfile.TemporaryDirectory() as td:
        st = UsageStats(Path(td) / "usage.json")
        # 传入诡异 usage 不应抛异常
        for bad in ({"prompt_tokens": "abc"}, {"prompt_tokens": None},
                    {"prompt_tokens_details": "notadict"}, {"total_tokens": -5}):
            try:
                st.record(channel="x", model="m", usage=bad)
            except Exception as e:
                check(f"容忍 {bad}", False, str(e))
        check("容忍异常 usage", True)


def main():
    for fn in (test_normalize, test_accumulate, test_range_filter, test_credit_diff_by_model, test_isolated):
        fn()
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("🎉 流量统计测试全部通过")


if __name__ == "__main__":
    main()
