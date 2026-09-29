# 部署提示词（复制下面整段发给 AI）

---

我要部署一个本机的多渠道 AI 反代服务，项目文件已经放在我电脑上了。请帮我完成部署并跑起来。

## 项目是什么

`ai-proxy-hub` —— 把多个 AI 平台的登录凭据聚合成本机统一的 OpenAI / Anthropic 兼容 API，自带 Web 管理面板（聚合控制台）。

支持的渠道：WorkBuddy（腾讯 CodeBuddy）、TraeWork、Qoder（阿里）、OpenCodeZen（免费匿名通道）。

**定位：个人自用 / 局域网自用，默认只监听 127.0.0.1，管理端点不做鉴权。** 不要改成 `0.0.0.0`，不要做公网端口转发。

## 环境要求

- Python ≥ 3.10（推荐 3.12）
- 系统需有 `node`（仅用于跑面板 JS 质量门测试，可选）
- 依赖只有 4 个：fastapi、uvicorn[standard]、httpx、cryptography（都在 requirements.txt 里）

## 请按顺序做这几件事

### 1. 解压并进入项目目录

把压缩包解压到任意目录（路径不要有中文和空格更稳妥），然后在该目录下操作。

### 2. 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
# Windows 用：.venv\Scripts\python.exe
# Linux/macOS/WSL 用：.venv/bin/python
.venv/bin/pip install -r requirements.txt
```

### 3. 登录账号（至少登录一个渠道）

**WorkBuddy（腾讯）**：

```bash
./login.sh
```

按提示在浏览器完成登录。也可以直接用 WorkBuddy / CodeBuddy 桌面端的登录态（脚本会自动探测凭据目录）。

**TraeWork / Qoder**：启动服务后在面板里点右上角「＋ TraeWork」/「＋ Qoder」按钮，会弹出浏览器窗口，按提示完成授权即可（Trae 需要先在浏览器里登录 trae.cn）。

### 4. 启动服务

```bash
./start.sh --desensitize
```

或直接：

```bash
.venv/bin/python converter.py --port 8787 --desensitize
```

启动时会自动为所有已登录渠道执行一次签到（WorkBuddy / Trae / Qoder），失败只告警不影响启动。

### 5. 打开面板确认

浏览器访问 <http://127.0.0.1:8787/panel>，应该能看到「聚合控制台」。检查：

- 「总览与账号」页能看到账号卡片、积分、签到状态
- 「接口」页能看到 API Key（默认是自动生成的，也可以自己新建）
- 四个页签都能正常切换

### 6. 接入客户端

把下面两项填进任意 OpenAI 兼容客户端（Cherry Studio / LobeChat / NextChat / Open WebUI / ZCode 等）：

```
Base URL: http://127.0.0.1:8787/v1
API Key:  （见面板「接口」页，或启动参数 --api-key 指定的值）
```

不同协议端点：
- OpenAI Chat：`POST /v1/chat/completions`
- OpenAI Responses（Codex CLI）：`POST /v1/responses`
- Anthropic Messages（Claude Code）：`POST /v1/messages`

模型名建议带渠道前缀，例如 `trae/deepseek-v4.1-flash`、`qoder/deepseek-flash`；也可以用平台专用端点（如 `/v1/trae/chat/completions`）然后写裸模型名。

## 验证部署是否成功

```bash
# 健康检查
curl http://127.0.0.1:8787/health

# 列出模型
curl http://127.0.0.1:8787/v1/models -H "Authorization: Bearer <你的KEY>"

# 真实调用一次
curl -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的KEY>" \
  -d '{"model":"trae/deepseek-v4.1-flash","stream":false,"messages":[{"role":"user","content":"回复OK"}]}'
```

如果第三条能返回内容，说明部署成功。

## 如果出问题，请按这个顺序排查

1. **导入报错** → 检查依赖是否装全（尤其 `cryptography`，缺了 Qoder 渠道会加载失败）
2. **启动报错** → 看终端输出；用 `--skip-check` 可跳过启动预检
3. **面板打不开** → 确认端口没被占用（`ss -tlnp | grep 8787`）
4. **调用报 503** → 该渠道还没登录账号
5. **调用报 401** → API Key 不对，去面板「接口」页核对
6. **Trae 登录失败/授权页报错** → 先确认浏览器已登录 trae.cn
7. **Windows 上跑** → 建议用 PowerShell 7（`pwsh`）而非旧版 PowerShell 5.1

## 想常驻运行（可选）

**Linux / WSL2（systemd user 服务）**：

`~/.config/systemd/user/ai-proxy-hub.service`：

```ini
[Unit]
Description=Multi-channel AI Reverse Proxy
After=network.target

[Service]
Type=simple
# 改成你实际解压的目录
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
journalctl --user -u ai-proxy-hub -f
```

**Windows**：用任务计划程序开机启动，或手动双击 `start.sh`（需 Git Bash / WSL）。

## 注意事项

- 凭据文件等同账号密码，不要分享给他人、不要提交到版本库
- 上游接口是非公开逆向接口，无稳定性承诺，随时可能因上游改版失效
- 请使用小号或可承受损失的账号，不要用主力账号
- 仅供个人学习研究，风险自负

---

**现在请开始，遇到报错把完整输出贴给我。**
