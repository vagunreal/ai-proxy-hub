"""channels — 多渠道聚合层。

把 WorkBuddy（原 converter.py 主链路）之外的三个渠道——Qoder、TraeWork、
OpenCodeZen——以独立模块形式接入同一套 OpenAI 兼容 API。

设计要点：
  - 每个渠道一个模块，实现 Channel 基类（load_accounts / chat / models /
    refresh_token / login_*），互不干扰；渠道内部状态只在模块内维护。
  - 凭据落盘在项目 auths/<kind>/ 目录（JSON，格式与 wild-work 兼容），
    便于用户迁移与手工检查。
  - 对外统一输出「标准 OpenAI SSE」，与 workbuddy 主链路一致，
    这样 /v1/responses、/v1/messages 的适配层可以原样复用。
  - 模型规格（上下文窗口 / 最大输出 / 输入模态）一律从上游动态获取，
    静态表仅作兜底——这是与 wild-work 旧实现的关键差异。
"""

from __future__ import annotations

from .common import Account, Channel, ModelSpec, get_channel, all_channels, register

__all__ = ["Account", "Channel", "ModelSpec", "get_channel", "all_channels", "register"]

# 导入即注册（顺序无关；任一渠道导入失败不影响其余渠道可用）
for _mod in ("oczen", "qoder", "trae"):
    try:
        __import__(f"{__name__}.{_mod}")
    except Exception as _e:  # pragma: no cover - 依赖缺失时降级
        import sys as _sys
        _sys.stderr.write(f"[channels] 渠道 {_mod} 加载失败: {_e}\n")
