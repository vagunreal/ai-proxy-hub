#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 凭据目录由 converter.py / checkin.py 自动扫描：
#   WSL ~/.local/share/CodeBuddyExtension/... + Windows 宿主机挂载目录，按账号 uid 去重。

# 启动时自动签到由 converter.py 统一执行（全部渠道：WorkBuddy + Trae + Qoder，
# 与面板「一键签到」同一实现）；此处不再单独调用 scripts/checkin.py 以免重复签到。

echo "🚀 启动 ai-proxy-hub 服务..."
exec "$DIR/.venv/bin/python" "$DIR/converter.py" "$@"
