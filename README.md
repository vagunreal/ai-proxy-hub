# ai-proxy-hub

> 多渠道 AI 反代聚合（WorkBuddy / TraeWork / Qoder / OpenCodeZen）

## 📢 版本更新公告

| 版本 | 日期 | 状态 | 更新内容 |
|------|------|------|----------|
| **V1.2.2** | 2026-09-29 | **当前版本** | 面板完善与数据准确性：流量监测重构（渠道+模型合并、+/− 缩放、按渠道分卡积分）、请求日志独立落盘（清统计不清日志，可分页/筛选/搜索）、一键签到（含 TraeWork / Qoder）、Trae 过滤无倍率模型并修正缓存与积分统计、总览统计按平台分组 |
| V1.2.1 | 2026-09-29 | 历史版本 | 修复 Qoder 通道 deepseek 系模型「能连上但对话几轮后必中断」：Qoder 网关不接受 assistant 消息带多个 `tool_calls`（并行工具调用）或 `content:null`，现对发往该通道的 messages 做规范化 |
| V1.2 | 2026-09-29 | 历史版本 | 升级为多渠道聚合反代：新增 TraeWork / Qoder / OpenCodeZen 渠道与平台专用端点（`/v1/{kind}/…`）、API Key 平台绑定（同名模型防串台）；面板改版「聚合控制台」（主页与账号管理合并、可拖动的长条账号卡）；模型规格诚实化（上游未下发的显示"未知/未提供"） |
| V1.1 | 2026-09-14 | 历史版本 | 修复 `--desensitize` 把工作区指令（AGENTS.md / CLAUDE.md）整条替换成占位符的问题；`GET /health` 新增 `version` 字段 |
| V1 | 2026-09-12 | 初始版本 | 首个可用版本；已知问题见 V1.1 的修复说明 |

> 正在运行的版本可用 `GET /health` 的 `version` 字段确认。
> 完整变更、行为对照、升级与回滚步骤见 [CHANGELOG.md](CHANGELOG.md)。

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 及 **TraeWork / Qoder / OpenCodeZen** 等平台的登录凭据，聚合成一个本机 **OpenAI / Anthropic 兼容 API**，并内置：

- **多渠道聚合**：WorkBuddy + TraeWork + Qoder + OpenCodeZen 四渠道统一接入，统一端点或平台专用端点（`/v1/{kind}/…`）均可调用
- **多账号池**：同渠道多个账号聚合为一个服务，额度用尽自动切换下一个
- **可视化面板（聚合控制台）**：浏览器查看各渠道账号的积分资源包明细，账号卡片可拖动排序；支持渠道账号的登录、切换与删除
- **API Key 平台绑定**：每个 Key 可绑定单一平台，裸模型名自动归属，避免同名模型串台
- **每日自动签到**：启动时自动为**全部渠道**账号签到（WorkBuddy + TraeWork + Qoder），
  与面板「一键签到」同一实现

适用客户端：Codex CLI（`/v1/responses`）、Claude Code / CC Switch（`/v1/messages`）、Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI 等（`/v1/chat/completions`）。

> ### ⚠️ 定位与安全边界（务必先读）
>
> **本项目为「个人自用 / 局域网内自用」的反代工具**，默认只监听 `127.0.0.1`，仅本机可访问。
>
> **管理端点不做鉴权**：`/panel`（面板）、`/v1/keys`（API Key 管理，返回密钥明文）、
> `/v1/channels`、`/v1/account-status`、`/v1/stats`、`/v1/logs`、`/v1/models-info`，
> 以及破坏性的 `/v1/stats/reset`、`/v1/logs/reset`、`/v1/checkin` 等，
> **全部无需凭据即可调用**——这是「本机自用」定位下的刻意取舍（免去每次输密码的麻烦）。
>
> 因此：
> - **不要**把 `--host` 改成 `0.0.0.0`，**不要**做公网端口转发/内网穿透，**不要**部署到共享或公网服务器。
> - 如需在局域网内使用，请自行在反向代理（Nginx / Caddy 等）层加 Basic Auth 或 IP 白名单，
>   并确认网络环境可信。
> - 泄露风险包括：他人可读取你的全部 API Key 明文、清空统计与日志、删除渠道账号。
>
> 本工具与上游各平台无任何关联，仅供个人学习研究，使用风险自负。

> 来源：基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 扩展多账号池与面板能力。上游接口（`copilot.tencent.com` / `codebuddy.cn` 的 `/v2/*`）属非公开逆向接口，无稳定性承诺，仅供个人学习研究，请自担风险。

---

## 功能总览

| 功能 | 说明 |
|------|------|
| 多渠道聚合 | WorkBuddy / TraeWork / Qoder / OpenCodeZen 统一接入；平台专用端点 `/v1/{kind}/chat/completions` 等，模型名可省略渠道前缀 |
| API Key 平台绑定 | Key 绑定单一平台后裸模型名自动归属该平台，显式前缀与绑定不符返回 403，同名模型（如 `glm-5.3` 在多平台存在）不串台 |
| 多账号池 | 自动扫描本机所有凭据文件（WSL + Windows 挂载目录），按账号 uid 去重后入池 |
| 粘性调度 | 正常时一直使用当前账号；某账号失败才切换，成功后粘住 |
| 故障自动切换 | 遇 HTTP 401/402/403/429 或错误文本含"额度/余额/配额/quota"等关键词时，该账号冷却 30 分钟，自动切到下一个健康账号 |
| 手动切换 | 面板点击"设为当前使用"，或调用 `POST /v1/account-switch` |
| 兜底硬试 | 所有账号都在冷却时仍按顺序重试（额度可能已恢复），失败才透传错误 |
| 聚合控制台 | `GET /panel`：主页与账号管理合并；账号长条卡直接平铺积分资源包明细（含临期标注），可拖动排序（自动记忆）；渠道添加按钮按渠道配色；30s 自动刷新 |
| 渠道账号管理 | 面板内完成各渠道登录（授权链接/回调粘贴/状态轮询）、账号切换、删除；积分资源包统计（含临期口径） |
| 每日签到 | 服务启动时自动签到全部渠道（WorkBuddy 每日签到 / Qoder campaigns / TraeWork UG），
也可在面板点「🎁 一键签到」；结果写入 `checkin_state.json` 供面板展示 |
| token 自动刷新 | 每个账号临近过期自动刷新并原子回写凭据文件 |
| 流量监测 | 面板按渠道与模型统计用量（渠道可展开看模型明细），区分缓存内与缓存外 token |
| 请求日志 | 独立 JSONL 落盘（清空统计不影响日志），支持分页 / 渠道筛选 / 关键字搜索 |
| 三协议兼容 | OpenAI Chat / OpenAI Responses / Anthropic Messages，均支持流式与工具调用 |
| 脱敏 | `--desensitize` 对 system 提示词做零宽字符脱敏 + 压缩，缓解后端内容审核误拦；**不含工作区指令**——`<system-reminder>` 里的 AGENTS.md / CLAUDE.md 原样透传 |
| 模型管理 | 自动从各上游发现可用模型；每个模型可编辑参数规格；自定义模型与别名；一键可用性探测 |

---

## 快速开始

> 项目可解压/克隆到**任意目录**运行：所有脚本（`start.sh` / `login.sh`）均以脚本自身位置定位，
> 运行时文件（`checkin_state.json`、日志）也生成在项目目录内，不依赖固定安装路径。
> 唯二的例外是"凭据目录"（由 WorkBuddy 桌面端决定，可用 `CODEBUDDY_AUTH_DIR` 改）和
> systemd 的 `ExecStart`（systemd 要求绝对路径，见下文模板注释）。

### 1. 安装依赖

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

依赖仅三个：`fastapi`、`uvicorn[standard]`、`httpx`（Python ≥ 3.10）。

### 2. 登录账号（生成凭据）

```bash
./login.sh
```

浏览器打开脚本输出的授权链接，用 QQ / 微信 / 手机号完成登录后回车。凭据会以 `{uid}.info` 保存到本机凭据目录。

**多账号 = 重复执行 `./login.sh` 登录不同账号即可**，每个账号一个文件，互不覆盖。
也可以直接使用 WorkBuddy / CodeBuddy 桌面端的登录态（脚本会自动探测桌面端凭据目录）。

### 3. 启动服务

```bash
./start.sh --desensitize
# 或直接：
.venv/bin/python converter.py --port 8787 --desensitize
```

启动时会先执行一轮**全渠道签到**（WorkBuddy + TraeWork + Qoder，与面板一键签到同一实现），
再打印预检信息（账号池里有哪些账号、token 是否过期）。签到失败只告警，不影响服务启动。

### 4. 打开面板（聚合控制台）

浏览器访问：<http://127.0.0.1:8787/panel>

- **总览与账号**（主页）：顶部统计（账号总数 / 总剩余 credits / 今日签到），下方为各渠道账号的
  长条卡片——左侧账号信息与操作按钮（设为当前 / 刷新 / 删除），右侧直接平铺积分资源包明细
  （包名 + 到期天数 + 进度条 + 余量），2 天内到期的积分红色"临期"标注
- 账号卡片顶部有拖动手柄 ⠿⠿，按住可调整卡片顺序，顺序自动保存在浏览器本地
- 右上「＋ 渠道」按钮按渠道配色，点击发起登录（授权链接 / 回调粘贴 / 状态轮询）
- 其余页签：流量监测（按渠道/模型/每日）、模型（规格与费率）、接口（端点与 Key 管理）
- 30 秒自动刷新

### 5. 客户端接入

```
Base URL: http://127.0.0.1:8787/v1
API Key:  未设置 --api-key 时留空即可
```

#### Codex CLI

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

```bash
export CODEBUDDY2OPENAI_KEY=any-value
codex --profile workbuddy "your task"
```

#### Claude Code / CC Switch

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

> Codex CLI 与 Claude Code 均建议开启 `--desensitize`；若仍被审核拦截，`/v1/responses` 会自动以压缩模式重试一次。

---

## 凭据目录（自动扫描）

服务按以下顺序扫描所有存在的目录，并按账号 uid 去重：

1. 环境变量 `CODEBUDDY_AUTH_DIR` 指定的目录
2. Linux/WSL：`~/.local/share/CodeBuddyExtension/Data/Public/auth`
3. WSL 下自动探测 Windows 宿主机：`/mnt/c/Users/*/AppData/Local/CodeBuddyExtension/Data/Public/auth`
4. macOS：`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth`
5. Windows 原生：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth`

凭据文件为 JSON（`.info` 后缀），结构：

```json
{
  "account": { "uid": "...", "nickname": "...", "enterpriseId": "" },
  "auth": { "accessToken": "...", "refreshToken": "...", "expiresAt": 0, "domain": "www.codebuddy.cn" }
}
```

> ⚠️ 凭据文件等同账号密码，请勿提交到版本库或分享给他人。本项目已通过 `.gitignore` 排除
> `*.token` / `*.key` / `secrets.*`，以及运行时状态 `auths/`（渠道凭据）、`api_keys.json`、
> `current_account.json`、`usage_stats.json`、`checkin_state.json`、`models_registry.json`。

---

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/chat/completions` | OpenAI Chat 兼容（原生 tools/tool_calls，流式/非流式） |
| POST | `/v1/responses` | OpenAI Responses 兼容（Codex CLI） |
| POST | `/v1/messages` | Anthropic Messages 兼容（Claude Code / CC Switch） |
| POST | `/v1/messages/count_tokens` | Anthropic token 计数（stub） |
| GET  | `/v1/models` | 模型列表（可按 Key 绑定平台过滤） |
| GET  | `/health` | 健康检查 + 账号池概览 |
| GET  | `/panel` | 聚合控制台：总览与账号 / 流量监测 / 模型 / 接口 |
| GET  | `/v1/platforms` | 平台专用端点清单（面板「接口」页展示用） |
| POST | `/v1/{kind}/chat/completions` | 平台专用端点（`kind` = workbuddy / trae / qoder / oczen），`/responses`、`/messages` 同理 |
| GET  | `/v1/channels` | 渠道清单 + 各自账号状态 + 积分（面板「账号管理」数据源） |
| POST | `/v1/channels/{kind}/login/start` / `login/poll` / `login/submit` | 渠道账号登录（发起 / 轮询 / 粘贴回调） |
| POST | `/v1/channels/{kind}/accounts/switch` / `accounts/delete` | 渠道账号切换 / 删除 |
| GET  | `/v1/account-status` | WorkBuddy 账号状态 + credits 额度 + 签到状态 |
| POST | `/v1/account-switch` | 手动切换当前 WorkBuddy 账号，请求体 `{"uid": "<账号uid>"}` |
| GET/POST | `/v1/keys` | API Key 管理（创建可绑定平台；`POST /v1/keys/delete` 删除） |
| POST | `/v1/checkin` | 一键签到：WorkBuddy 账号池 + TraeWork + Qoder（OpenCodeZen 无签到自动跳过） |
| GET  | `/v1/stats` | 流量统计（总用量 + 按渠道/模型/天聚合，支持 `start`/`end` 区间） |
| POST | `/v1/stats/reset` | 清空流量统计（**不影响请求日志**） |
| GET  | `/v1/logs` | 请求日志（分页 `limit`/`offset`，支持 `channel`/`q` 过滤；独立于统计） |
| POST | `/v1/logs/reset` | 清空请求日志（独立于统计） |
| GET  | `/v1/models-info` | 面板模型管理数据：模型列表(含参数规格) + 探测进度 + API 接入信息 |
| POST | `/v1/models/custom` | 添加自定义模型，`{"name", "alias"?, "specs"?}` |
| POST | `/v1/models/specs` | 更新模型参数规格，`{"name", "specs": {context_length, max_output_tokens, input, output}}` |
| POST | `/v1/models/delete` | 删除自定义模型 / 禁用⇄恢复内置模型，`{"name"}` |
| POST | `/v1/models/probe` | 可用性探测单个模型，`{"model"}` |
| POST | `/v1/models/probe-all` | 后台顺序探测全部模型(进度见 `/v1/models-info`) |

### 账号池调度细节

- 候选顺序：当前账号 → 其他健康账号 → 冷却中账号（兜底硬试）
- 触发切换的状态码：`401 / 402 / 403 / 429`；或响应文本含 `额度 / 余额 / 积分不足 / 配额 / quota / insufficient / exceeded / 限流 / 频率`
- 切换只发生在**尚未向客户端输出任何字节之前**——客户端不会收到半截流再断开
- 失败账号冷却 30 分钟后恢复候选；成功账号会被"粘住"

---

## 启动参数与环境变量

```
--host            监听地址（默认 127.0.0.1）
--port            监听端口（默认 8787）
--api-key         要求客户端携带的 API key（默认不校验；也可用环境变量 CODEBUDDY2OPENAI_KEY）
--log PATH        请求/响应日志写入该文件（默认关闭）
--desensitize     启用零宽脱敏 + system 压缩（缓解内容审核误拦，建议开启）
--no-compact      配合 --desensitize：跳过压缩只做脱敏，保留完整 system 提示词
--skip-check      跳过启动预检
```

> **脱敏范围（重要）**：`--desensitize` 只重写 system / developer 消息，以及 Codex CLI 注入的运行时块
> （`<environment_context>` / `<permissions instructions>` / `<collaboration_mode>` / `<skills_instructions>`）。
> ZCode CLI 与 Claude Code 放在 `<system-reminder>` 里的**工作区指令**（AGENTS.md、CLAUDE.md、记忆索引）
> **原样透传**——早先版本会把这整条消息替换成一句占位符，模型因此完全看不到规则（典型症状：代理后面的
> agent 不遵守 AGENTS.md，Windows 上不用 `pwsh` 而用 `powershell`）。如需连 system 提示词也保持原样，
> 用 `--no-compact`。另外 `--desensitize` 会剥离 tools 的 `description` 字段（参数 schema 保留）。

| 环境变量 | 说明 |
|----------|------|
| `CODEBUDDY_AUTH_DIR` | 额外指定凭据目录（优先扫描） |
| `CODEBUDDY2OPENAI_KEY` | 等效 `--api-key` |
| `CODEBUDDY2OPENAI_LOG` | 等效 `--log` |

---

## systemd 常驻部署（Linux / WSL2）

`~/.config/systemd/user/ai-proxy-hub.service`：

```ini
[Unit]
Description=Multi-channel AI Reverse Proxy (WorkBuddy/Trae/Qoder/OpenCodeZen)
After=network.target

[Service]
Type=simple
# 下面两行的路径改成你实际解压项目的目录（systemd 要求绝对路径，%h 代表用户 home）
WorkingDirectory=%h/ai-proxy-hub
ExecStart=%h/ai-proxy-hub/start.sh --desensitize
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now ai-proxy-hub
journalctl --user -u ai-proxy-hub -f     # 查看日志
```

## Docker 部署（可选）

```bash
# 先在宿主机完成登录（或把凭据目录挂进容器），再：
docker compose up -d
```

`docker-compose.yml` 默认挂载 macOS 凭据路径，其他平台请修改 volumes 中的 auth 目录。容器内多账号同理——挂载的目录里有几个 `.info` 文件就有几个账号。

---

## 签到脚本单独使用（可选）

常规签到已由服务统一处理（启动自动签到 + 面板「一键签到」，覆盖全部渠道）。
`scripts/checkin.py` 保留为**独立补签工具**，仅处理 WorkBuddy 账号（不依赖服务运行）：

```bash
.venv/bin/python scripts/checkin.py
```

自动遍历凭据目录中所有 WorkBuddy 账号：token 临近过期先刷新 → 调用每日签到接口 →
结果打印并写入 `checkin_state.json`（按 uid 去重，同账号多份凭据只签一次）。
需要 TraeWork / Qoder 签到时请用面板「一键签到」或重启服务。

---

## 测试

```bash
# 账号池端到端测试（内置本地 mock 后端，不依赖真实账号）
.venv/bin/python tests/test_account_pool.py

# 渠道层 / 平台 Key / 流量统计 / 协议适配器 / 面板 JS 质量门
.venv/bin/python tests/test_channels.py
.venv/bin/python tests/test_platform_keys.py
.venv/bin/python tests/test_usage_stats.py
.venv/bin/python tests/test_anthropic_adapter.py
.venv/bin/python tests/test_responses_adapter.py
.venv/bin/python tests/test_panel_js.py     # 需要系统安装 node
```

`tests/test_account_pool.py` 覆盖：uid 去重、粘性/冷却/切换顺序、failover 状态码与文本判定、非流式与流式端到端切换、全部账号失败时的错误透传、面板数据接口。`test_panel_js.py` 用 `node --check` + DOM stub 冒烟防止面板内嵌 JS 被 Python 转义写坏。

---

## 与 CLIProxyAPI 集成（可选）

本服务可作为 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 的一个 OpenAI 兼容渠道，把账号池暴露进统一网关：

```yaml
openai-compatibility:
  - name: "workbuddy"
    base-url: "http://127.0.0.1:8787/v1"
    api-key-entries:
      - api-key: "any-non-empty-string"   # 本服务未设 --api-key 时不校验
        proxy-url: "direct"               # 本机回环必须直连，避免走全局代理
    models:
      - name: "glm-5.2"
        alias: "glm-5.2"
      # ...其余模型同理
```

---

## 目录结构

```
ai-proxy-hub/
├── converter.py               # 主服务入口：三协议转换 + 多渠道路由 + 账号池 + 聚合控制台（FastAPI）
├── channels/                  # 渠道层（多渠道聚合）
│   ├── common.py              #   Channel/Account/ModelSpec 基类、SSE 聚合、凭据存储
│   ├── trae.py                #   TraeWork 渠道（登录/模型/对话/积分/签到）
│   ├── qoder.py               #   Qoder 渠道（机器码会话/上下文档位/积分倍率）
│   ├── qoder_cosy.py          #   Qoder 请求编码与 会话构造
│   └── oczen.py               #   OpenCodeZen 匿名免费通道（免登录）
├── core/                      # 核心模块
│   ├── responses_adapter.py   #   Responses API ↔ Chat 转换
│   ├── responses_projection.py#   Responses 请求投影/压缩
│   ├── anthropic_adapter.py   #   Anthropic Messages ↔ Chat 转换
│   ├── desensitize.py         #   内容审核脱敏（零宽字符 + system 压缩）
│   └── usage_stats.py         #   流量统计（渠道/模型/每日聚合）
├── scripts/                   # 工具脚本
│   ├── checkin.py             #   WorkBuddy 独立补签工具（常规签到由服务统一处理）
│   └── oauth_login.py         #   OAuth 设备流登录，生成 {uid}.info 凭据
├── tests/                     # 测试（账号池 / 协议适配器 / 渠道层 / 平台 Key / 面板 JS 质量门等）
├── start.sh                   # 启动脚本（签到由 converter.py 统一执行）
├── login.sh                   # 登录脚本
├── requirements.txt           # fastapi / uvicorn / httpx
├── Dockerfile / docker-compose.yml
└── *.json                     # 运行时状态（签到/模型注册表/API Keys/统计，自动生成，不入库）
```

## 来源与致谢

本项目是 **[HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai)** 的二次开发,参考并继承了其核心思路与基础实现(CodeBuddy/WorkBuddy 登录凭据读取、直连后端协议转换、脱敏模块),在此感谢原作者。

在原项目基础上,本仓库扩展了:

- 多账号池:额度用尽自动切换、手动切换、粘性调度与冷却
- 每日自动签到(多账号遍历)
- 可视化额度面板(`/panel`,credits 资源包展示)
- OpenAI Responses / Anthropic Messages 协议端点的补强

原项目的单账号部署方式、协议设计等文档以 [上游仓库](https://github.com/HanHan666666/codebuddy2openai) 为准。

同时感谢 **[wild-work](https://github.com/rockswang/wild-work)** 项目：本项目的多渠道聚合架构
（渠道层抽象、平台前缀路由）、TraeWork 授权参数、Qoder 上下文档位与积分口径，以及面板的
账号卡片交互设计，均参考并借鉴了该项目的实现思路。

---

<sub>
Keywords: codebuddy to openai · codebuddy2openai · workbuddy api proxy · workbuddy openai adapter · codex cli workbuddy · claude code workbuddy · tencent code assistant openai compatible api
</sub>
