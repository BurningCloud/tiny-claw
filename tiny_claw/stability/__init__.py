"""stability：Agent 的"缰绳"与"刹车系统"。

这一层解决的是同一类问题：模型本身没问题，但它没有"自我监控"的能力——
不知道自己已原地打转十次，也不知道刚才那条命令差点删库。这些必须由
Harness 从外部兜住（用运行时机制兜，不用提示词兜）。

包含两块：
- reminder：死循环检测、循环/心跳提醒文案、错误自愈顾问。
- middleware：风险分级（NONE/LOW/ASK/DENY）、正则分类器、高危命令拦截、
  以及人工审批通道（终端 / 静态 / 函数式 / 预先授权豁免）。

对应专栏第 4、14、15、16 讲。
"""

from tiny_claw.stability.reminder import (
    DEFAULT_RULES,
    Reminder,
    RecoveryAdvisor,
    Rule,
    build_heartbeat,
    build_loop_alert,
    fingerprint,
)
from tiny_claw.stability.middleware import (
    ApprovalRequest,
    Approver,
    BlockedError,
    Classifier,
    Decision,
    FuncApprover,
    Guard,
    RegexClassifier,
    RiskLevel,
    StaticApprover,
    TerminalApprover,
    default_classifier,
)

__all__ = [
    # reminder
    "Reminder",
    "build_loop_alert",
    "build_heartbeat",
    "fingerprint",
    "Rule",
    "DEFAULT_RULES",
    "RecoveryAdvisor",
    # middleware
    "RiskLevel",
    "Decision",
    "Classifier",
    "RegexClassifier",
    "default_classifier",
    "BlockedError",
    "ApprovalRequest",
    "Approver",
    "StaticApprover",
    "FuncApprover",
    "TerminalApprover",
    "Guard",
]
