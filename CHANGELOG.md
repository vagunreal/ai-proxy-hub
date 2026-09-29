# 更新说明（CHANGELOG）

本项目使用 `V主版本.次版本` 标记版本，每个版本对应一个 git 标签：

| 版本 | git 标签 | 提交 | 状态 |
|------|----------|------|------|
| V1.2.2 | `V1.2.2` | 当前 `main` | **当前版本**，流量监测重构 + Trae 模型/统计修正 + 一键签到 + 请求日志独立落盘 |
| V1.2.1 | `V1.2.1` | `d84642d` | Qoder 通道消息格式规范化修复（deepseek 对话几轮必断） |
| V1.2 | `V1.2` | `fb1e92d` | 多渠道聚合 + 面板「聚合控制台」改版 |
| V1.1 | `V1.1` | `9a43189` | 工作区指令透传修复 |
| V1 | `V1` | `14171ae` | 初始版本（与 origin/main 同步） |

运行时可用 `GET /health` 查看当前版本，`version` 字段即标签名。

查看/切换某个版本：

```bash
git tag -l                        # 列出所有版本
git rev-parse V1.1                # 看某版本指向哪个提交
git show V1                       # 看某版本说明
git checkout V1                   # 切回初始版本（看完用 git checkout main 回来）
```

---

## V1.2.2

流量监测重构、一键签到、日志独立落盘，并修 Trae 统计与若干 bug。

### 新增

- **一键签到**：面板按钮 + 服务启动自动签到，覆盖 WorkBuddy / TraeWork / Qoder
  （Qoder 走 campaigns、Trae 走 UG 接口）；结果弹窗按「渠道 → 账号 → 状态」分组，
  失败账号附原因注释。启动签到与手动签到共用同一实现。
- **请求日志独立落盘**：`request_log.jsonl` 与统计分离 —— 清空统计不影响日志；
  面板支持分页（100/页）、渠道筛选、关键字搜索，可单独清空。
- **流量监测重构**：渠道与模型合并为「渠道用量」（+/− 展开模型明细）；
  总览指标分层（总 token 统一、积分按平台分卡、缓存内外/命中率绑定渠道与模型）；
  新增「全部展开/收起」；移除冗余的「每日明细」。

### 修复

- **`/v1/checkin` 返回结构错误**：重构时装饰器误留在内部函数上，导致签到弹窗空白、
  浮条显示 `undefined/undefined`。
- **Trae 模型与统计**：过滤无倍率模型（28 → 15）；缓存字段透传修正
  （`prompt_tokens` 已含缓存，避免重复计数）；积分改用余额差分记账
  （上游不下发积分），分母口径修正为仅可用池。
- **总览统计按平台分组**：此前只统计 WorkBuddy，现账号总数含全部渠道、
  积分按平台分卡、签到进度按渠道分别显示；OpenCodeZen 匿名通道不计入。
- 签到统计覆盖全部渠道；`/panel` 加 `Cache-Control: no-store` 防浏览器缓存旧页面。
- `requirements.txt` 补 `cryptography`（Qoder 渠道必需）；`main()` 强制
  stdout/stderr 为 UTF-8（Windows 重定向时 GBK 会崩）。

### 安全定位说明（重要）

本项目定位为**个人自用 / 局域网内自用**的反代工具，默认只监听 `127.0.0.1`。
**管理端点刻意不做鉴权**（`/panel`、`/v1/keys`、`/v1/channels`、`/v1/stats`、`/v1/logs`
及破坏性的 `/v1/stats/reset`、`/v1/logs/reset`、`/v1/checkin` 等），
这是为免去本机每次输密码的取舍。README 已新增显著的安全边界说明：
不要改 `--host 0.0.0.0`、不要做公网端口转发；局域网内使用请自行在反向代理层加
Basic Auth 或 IP 白名单。

### 文档与清理

- README 端点表修正：`/v1/usage/stats` → `/v1/stats`（原路径不存在）；
  补齐 `/v1/checkin`、`/v1/stats/reset`、`/v1/logs`、`/v1/logs/reset`。
- README 签到描述更新：`scripts/checkin.py` 降级为 WorkBuddy 独立补签工具。
- 移除死代码 `loadChannelSections()`、`modelLabel()`；
  `/v1/stats` 不再下发前端未消费的 `by_day`/`by_account`/`day_model`/`recent`。
- 新增发布前回归脚本 `tests/test_e2e_api.py`（后端 41 项）与
  `tests/test_e2e_panel.py`（前端 31 项）。
- `PROJECT_VERSION` → `V1.2.2`。

---


## V1.2.1

修复 Qoder 通道 deepseek 系模型「能连上，但对话几轮后必中断」的问题。

### 修复：Qoder 通道消息格式规范化

- **现象**：ZCode 等客户端走 `/v1/qoder` 用 `deepseek-flash` 对话，一旦历史里出现
  并行工具调用或纯工具调用消息，下一轮请求即被上游以 HTTP 200 + SSE 错误帧拒绝
  （`provider_error` / "Error in upstream response"），对话必中断；阿里系模型同通道不受影响。
- **根因**（对真实请求重放二分实锤）：Qoder 网关对 messages 的校验比 OpenAI 标准严，
  两处不合规即整单被拒——
  1. 一条 assistant 消息带多个 `tool_calls`（并行工具调用）；
  2. assistant 消息 `content` 为 `null`（纯工具调用无文本）。
- **修复**：`channels/qoder.py` 新增 `_normalize_qoder_messages()`，在 `build_agent_body`
  里对发往网关的 messages 规范化：`content: null → ""`；多 `tool_calls` 拆成
  「assistant(单 call) → tool → assistant(单 call) → tool」交替序列。
  单 `tool_calls` 与其余角色消息原样保留，WorkBuddy / Trae / OpenCodeZen 通道不受影响。
- `PROJECT_VERSION` 同步升为 `V1.2.1`。

---

## V1.2

从单渠道（WorkBuddy）反代升级为**多渠道聚合反代**，并把管理面板改版为「聚合控制台」。

### 新增：多渠道聚合架构

- 新增 `channels/` 渠道层：`common.py`（Channel/Account/ModelSpec 基类与 SSE 聚合）、
  `trae.py`（TraeWork）、`qoder.py` / `qoder_cosy.py`（Qoder）、`oczen.py`（OpenCodeZen 免登录匿名通道）。
- 前缀路由：`/v1/{kind}/chat/completions` 等平台专用端点，模型名可省略渠道前缀；
  统一端点继续接受 `workbuddy/<model>`、`qoder/<model>` 等带前缀模型名。
- 渠道账号管理端点：`/v1/channels`（清单+账号+积分）、登录（start/poll/submit，Trae 支持
  回调 GET+POST 与手工粘贴）、账号切换/删除、积分资源包统计（含临期口径）。
- Trae 登录授权参数对齐参考实现（`redirect=0`、`auth_from=solo`、`login_channel=native_ide`），
  修复此前登录失败问题；token 自动刷新。
- 用量统计：新增 `core/usage_stats.py` 与面板「流量监测」页（按渠道/模型/每日，区分缓存内外）。

### 新增：API Key 平台绑定

- Key 可绑定单一平台，裸模型名自动归属绑定渠道，显式前缀与绑定不符返回 403，
  消除同名模型（如 `glm-5.3` 同时存在于 WorkBuddy 与 Qoder）的串台风险。
- OpenCodeZen（免登录渠道）同样可绑定；`/v1/platforms` 平台端点清单包含全部渠道。

### 改版：管理面板「聚合控制台」

- 主页面与账号管理合并为单一「总览与账号」页；标题更名「聚合控制台」。
- 账号卡片改版为通栏长条卡：左侧账号信息与操作，右侧直接平铺积分资源包明细
  （包名 + 到期天数 + 进度条 + 余量），临期积分红色标注。
- 卡片可拖动排序（按住手柄），顺序存浏览器 localStorage 自动记忆。
- 「＋ 渠道」添加按钮组按渠道配色；空渠道显示就地添加入口。
- 修复面板内嵌 JS 因 Python 字符串转义吃掉 `\n` 导致的整页 SyntaxError
  （`PANEL_HTML` 为非 raw 字符串，JS 侧必须写 `\\n`）。

### 修复：模型规格诚实化

- `ModelSpec` 原则：上下文/最大输出只用上游下发值，拿不到就留 0（未知），不用静态值冒充。
- Qoder 上游不下发最大输出，此前冒充保守默认 32768，与 WorkBuddy 同款模型（如 128K）矛盾；
  现改回「未知」（请求时的模板默认值不受影响）。
- TraeWork 上游接口实测不含任何规格字段，模型页对无规格模型显示「上游未提供」，
  与「未知/为零」明确区分。

### 其他

- `.gitignore` 补齐 `auths/`、`current_account.json`、`usage_stats.json` 等运行时凭据与状态。

---

## V1.1

修复代理会把工作区指令（AGENTS.md / CLAUDE.md）删掉的问题。

### 问题

开启 `--desensitize` 时，`core/desensitize.py` 把所有含 `<system-reminder>` 的 user 消息
判定为"可压缩的 harness 运行时上下文"，**整条**替换成一句占位符：

```
Repository instructions and environment context are provided. Follow repository guidance
while answering the user's actual request.
```

但 ZCode CLI 与 Claude Code 恰好把**工作区指令**放在这类消息里（AGENTS.md、CLAUDE.md、
记忆索引）。实测 ZCode 发往 `/v1/chat/completions` 的请求中，14007 字符的
"两份 AGENTS.md + MEMORY.md 索引"被替换成 131 字符。

**症状**：经本代理接入的 agent 读不到项目规则 —— 不遵守 AGENTS.md，Windows 上使用
`powershell` 5.1 或 `cmd` 而不是 `pwsh`；排查时模型甚至会得出"AGENTS.md 没有注入到我的
上下文"的结论（这个结论是对的）。

**为什么 `--no-compact` 不管用**：`<system-reminder>` 同时也登记在
`_RUNTIME_BLOCK_REPLACEMENTS` 里，该模式会把它整块换成 52 字符的
"Runtime reminder context is provided by the harness."，指令照样丢。

**影响范围**：仅 `--desensitize` 开启时；走 `/v1/responses` 的 Codex CLI 路径另有一套
投影逻辑（`core/responses_projection.py`），本版未改动。

### 改动

`core/desensitize.py`：

1. `_HARNESS_USER_MARKERS` 移除 `<system-reminder>` 与 `# claudeMd` —— 这两个标记承载的是
   指令，不是可丢弃的运行时元数据。
2. `_RUNTIME_BLOCK_REPLACEMENTS` 移除 `<system-reminder>` 块替换。
3. 新增 `_INSTRUCTION_MARKERS`（`# agentsMd`、`# claudeMd`、
   `IMPORTANT: These instructions OVERRIDE`）作为兜底守卫：带这些标记的消息在两条压缩
   路径上都不允许被整条替换，防止以后新增 harness 标记时又误伤指令。
4. `_compact_harness_message` 的 user 分支恢复原措辞，仅对**不带**指令标记的 Codex /
   Claude Code 运行时上下文生效。

另外：

- `converter.py`：新增 `PROJECT_VERSION`，`/health` 与启动预检输出中展示版本号。
- `tests/test_desensitize_harness.py`（新增）：7 项回归断言，锁死"工作区指令在压缩模式与
  `--no-compact` 下都逐字保留"，同时确认 system 脱敏、Codex 运行时块压缩、tools 描述剥离
  三项原有行为没有回归。
- `README.md`：补充脱敏范围说明。

### 行为对照

| 项目 | V1 | V1.1 |
|------|----|------|
| `<system-reminder>` 里的 AGENTS.md / CLAUDE.md | 整条替换成占位符 | 原样透传 |
| system / developer 消息零宽脱敏 | 是 | 是（未变） |
| Codex `<environment_context>` 等运行时块压缩 | 是 | 是（措辞与 V1 逐字一致） |
| tools `description` 剥离（参数 schema 保留） | 是 | 是（未变） |
| `GET /health` 显示版本号 | 无 | `"version": "V1.1"` |

### 验证

- **真实请求回归**：用抓取到的实际请求（`~/.zcode/cli/rollout/model-io-sess_075c1888*.jsonl`）
  回归，修复后 msg[4] 与 ZCode 发出的原文**逐字相同**（14007 = 14007），两份 AGENTS.md 与
  记忆索引均在；system 消息仍含零宽字符。
- **端到端 A/B**：把常量藏在 `<system-reminder>` 里发往 8787 —— 修复前模型答不出该常量
  （只回 `MARKER_CODE`），修复后正确复述其值。
- **行为验收**：原样重放当时那条"调用一下ps"请求（6 条消息 + 53 个工具完全一致）——
  修复前输出 `ps` → `tasklist` → `powershell -NoProfile`；修复后输出
  `pwsh -NoProfile -Command "..."`。
- **测试套件**：`tests/` 全绿（desensitize_harness 7、responses_adapter 15、
  anthropic_adapter 13、panel_js 通过）。

### 升级步骤

```bash
cd ~/workbuddy2api
git fetch --tags && git checkout V1.1
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart workbuddy2api
curl -s http://127.0.0.1:8787/health | grep -o '"version":"[^"]*"'   # 应输出 "version":"V1.1"
```

> 改了 `core/desensitize.py` 必须重启服务，否则进程里跑的还是旧代码。
> 已经开着的 ZCode 会话下一轮就会带上 AGENTS.md，但该模型此前已得出过"规则没注入"的结论，
> 建议新开会话观察。

### 回滚

```bash
git checkout V1
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart workbuddy2api
```

### 已知限制（V1.1 未处理）

1. **tools 描述仍被清空**：`--desensitize` 会剥离全部工具的 `description`（参数 schema 保留）。
   这是有意为之 —— Bash 工具描述内含 `detection evasion for malicious purposes`，命中敏感词表。
   如需保留，把调用处的 `strip_tool_metadata` 传 `False`。
2. **敏感词表缺词边界，会误伤 `skill`**：`kill` 命中 `skill`/`SKILL`，技能提醒消息一次请求
   被插入 59 处零宽字符（可见内容不变，但多耗 token）。不能简单加 `\b` —— CJK 也属于 `\w`，
   加了之后 `DoS攻击` 这类连写会匹配不到；若要修需改成只看左侧的 `(?<![A-Za-z])`，
   但会让 `cyberattacks` 之类漏网，属取舍。
3. **`core/responses_projection.py` 有同类标记列表**（`<system-reminder>`、`# claudeMd`），
   影响 `/v1/responses`（Codex CLI）路径。ZCode 走 `chat/completions`，不受影响。

---

## V1

初始版本。CodeBuddy → OpenAI 兼容转换器，支持 OpenAI Chat / OpenAI Responses /
Anthropic Messages 三协议，多账号池 failover，自带可视化面板（`/panel`）与每日签到。
`v2/chat/completions` 等 `/v2` 前缀作为别名路由兼容习惯用法。

已知问题见 V1.1 的「问题」一节。
