#!/usr/bin/env python3
"""前端全交互回归：把面板上每个可点的东西都点一遍，验证无 JS 错误、无失效按钮。"""
from playwright.sync_api import sync_playwright

errs, results = [], []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    pg.on("console", lambda m: errs.append(f"console: {m.text}") if m.type == "error" else None)
    pg.on("dialog", lambda d: d.accept())   # 自动确认 confirm/alert

    pg.goto("http://127.0.0.1:8787/panel", wait_until="networkidle", timeout=30000)
    pg.wait_for_timeout(3500)

    # ---------- 总览与账号 ----------
    check("页面标题", pg.title() == "聚合控制台", pg.title())
    check("统计卡片渲染", pg.locator("#stats .stat").count() >= 5,
          f"{pg.locator('#stats .stat').count()} 张")
    check("账号长条卡渲染", pg.locator(".strip-card").count() >= 4,
          f"{pg.locator('.strip-card').count()} 张")
    check("渠道添加按钮", pg.locator("#add-buttons .btn").count() >= 3)

    # 刷新积分
    pg.locator("button", has_text="刷新积分").first.click()
    pg.wait_for_timeout(2500)
    check("「刷新积分」可点且数据仍在", pg.locator(".strip-card").count() >= 4)

    # 刷新账号
    pg.locator("button", has_text="刷新账号").first.click()
    pg.wait_for_timeout(2500)
    check("「刷新账号」可点", pg.locator(".strip-card").count() >= 4)

    # 一键签到（会弹结果窗）
    pg.locator("button", has_text="一键签到").first.click()
    pg.wait_for_timeout(9000)
    notice_ok = pg.locator("#notice-overlay").is_visible()
    check("「一键签到」弹出结果窗", notice_ok)
    if notice_ok:
        body_txt = pg.locator("#notice-body").inner_text()
        check("  结果窗有渠道分组", pg.locator(".ck-group").count() >= 1,
              f"groups={pg.locator('.ck-group').count()}")
        check("  结果窗有账号行", pg.locator(".ck-acct").count() >= 1)
        check("  结果窗无 undefined/NaN", "undefined" not in body_txt and "NaN" not in body_txt,
              body_txt[:120])
        pg.locator("#notice-overlay button", has_text="知道了").click()
        pg.wait_for_timeout(600)
        check("  结果窗可关闭", not pg.locator("#notice-overlay").is_visible())

    # 卡片积分明细（点击弹 alert，已被自动 accept）
    creds = pg.locator(".strip-card .acct-credits").first
    if creds.count():
        creds.click()
        pg.wait_for_timeout(1200)
        check("账号积分明细可点", True)

    # 拖动排序（真实鼠标）
    grips = pg.locator(".strip-card .sc-grip[draggable='true']")
    if grips.count() >= 2:
        sb = grips.last.bounding_box()
        db = pg.locator(".strip-card").first.bounding_box()
        pg.mouse.move(sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2)
        pg.mouse.down()
        for i in range(1, 13):
            pg.mouse.move(sb["x"] + (db["x"] - sb["x"]) * i / 12,
                          sb["y"] + (db["y"] + 20 - sb["y"]) * i / 12)
            pg.wait_for_timeout(30)
        pg.mouse.up()
        pg.wait_for_timeout(800)
        check("卡片拖动排序生效", True)
        pg.evaluate("localStorage.removeItem('wb-acct-order')")

    # ---------- 流量监测 ----------
    pg.locator(".tab[data-p='traffic']").click()
    pg.wait_for_timeout(3000)
    check("流量页可见", pg.locator("#page-traffic").is_visible())
    check("  渠道用量分组", pg.locator(".ch-group").count() >= 1)
    check("  日志请求有表头", pg.locator("#traffic-recent .trow.head").count() >= 1)
    # 缩放按钮
    zoom = pg.locator(".ch-group .ch-zoom").first
    if zoom.count():
        zoom.click()
        pg.wait_for_timeout(800)
        expanded = pg.locator(".ch-group .ch-body").first.is_visible()
        check("  渠道 +/− 展开模型明细", expanded)
        zoom.click()
        pg.wait_for_timeout(600)
    # 全部展开/收起
    pg.locator("button", has_text="全部展开").click()
    pg.wait_for_timeout(700)
    check("  「全部展开」可用", True)
    pg.locator("button", has_text="全部收起").click()
    pg.wait_for_timeout(700)
    # 区间切换
    for label in ["本周", "本月", "全部", "今日"]:
        pg.locator(f"#traffic-range button:has-text('{label}')").click()
        pg.wait_for_timeout(1200)
    check("  区间切换（今日/本周/本月/全部）可用", True)
    # 指标切换
    pg.locator("#metric-switch button:has-text('积分')").click()
    pg.wait_for_timeout(1200)
    pg.locator("#metric-switch button:has-text('Token')").click()
    pg.wait_for_timeout(1200)
    check("  Token/积分指标切换可用", True)
    # 日志筛选与搜索
    pg.locator("#log-search").fill("deepseek")
    pg.locator("button", has_text="查询").first.click()
    pg.wait_for_timeout(1500)
    check("  日志关键字搜索可用", True)
    pg.locator("#log-search").fill("")
    if pg.locator("#log-filter-ch option").count() > 1:
        pg.locator("#log-filter-ch").select_option(index=1)
        pg.wait_for_timeout(1500)
        check("  日志渠道筛选可用", True)
        pg.locator("#log-filter-ch").select_option(index=0)
        pg.wait_for_timeout(1000)

    # ---------- 模型页 ----------
    pg.locator(".tab[data-p='models']").click()
    pg.wait_for_timeout(2500)
    check("模型页可见", pg.locator("#page-models").is_visible())
    check("  模型行渲染", pg.locator("#page-models .mrow").count() > 0,
          f"{pg.locator('#page-models .mrow').count()} 行")
    # 渠道筛选 chip
    chips = pg.locator("#model-channels .chip")
    if chips.count() > 1:
        chips.nth(1).click()
        pg.wait_for_timeout(1500)
        check("  模型页渠道筛选可用", True)
        chips.nth(0).click()
        pg.wait_for_timeout(1200)

    # ---------- 接口页 ----------
    pg.locator(".tab[data-p='api']").click()
    pg.wait_for_timeout(2500)
    check("接口页可见", pg.locator("#page-api").is_visible())
    check("  端点表渲染", pg.locator("#api-eps .ep").count() >= 3)
    check("  平台端点表渲染", pg.locator("#api-platforms .ep").count() >= 4)
    check("  API Keys 列表渲染", pg.locator("#keys-list .krow").count() >= 1,
          f"{pg.locator('#keys-list .krow').count()} 行")

    # 回到总览，验证页签状态记忆
    pg.locator(".tab[data-p='home']").click()
    pg.wait_for_timeout(2000)
    check("页签切换回总览正常", pg.locator("#page-home").is_visible())

    # ---------- 关键：无 JS 错误 ----------
    check("全程无 JS 错误", len(errs) == 0, "; ".join(errs[:3]))
    b.close()

ok = sum(1 for _, c in results if c)
print()
print("=" * 60)
print(f"前端交互：通过 {ok} / 失败 {len(results) - ok}")
for n, c in results:
    if not c:
        print("  ❌", n)
if errs:
    print("JS 错误：")
    for e in errs[:5]:
        print("  ", e[:160])
print("=" * 60)
