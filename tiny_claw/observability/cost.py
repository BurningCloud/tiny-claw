"""可观测性层：成本核算（Cost Accounting）。

对应专栏第 5 章。Accountant 在 Harness 层拦截并记录 token 消耗。

为什么必须在 Harness 层做，而不是去看厂商后台账单：
后台账单只能告诉你"这一小时花了多少钱"，不能告诉你"是哪一轮、哪个工具、
哪次死循环烧掉的钱"。把成本打散到每次模型调用，才能把"钱"和"行为"对齐——
这正是 AgentOps 的核心。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Usage 只在注解里出现，运行时不需要它；用 TYPE_CHECKING 避免
    # 在 observability 包加载时就去依赖 schema 包（保持包级解耦）。
    from tiny_claw.schema.message import Usage


# ---------------------------------------------------------------------------
# 计价
# ---------------------------------------------------------------------------

@dataclass
class Pricing:
    """某模型的单价（美元 / 百万 token）。

    字段名沿用 Go 源的语义（per-MTok，即每百万 token 单价），
    按全局 snake_case 约定写作 input_per_mtok / output_per_mtok。
    """

    input_per_mtok: float
    output_per_mtok: float


# 内置价目表。
#
# ⚠️ 价格会变，请以厂商官方报价为准。这里只提供一个默认起点，
# 真实使用时应由配置注入（见 Accountant.set_pricing）。
DEFAULT_PRICING: dict[str, Pricing] = {
    "claude-sonnet-4-5": Pricing(3.0, 15.0),
    "claude-opus-4-1": Pricing(15.0, 75.0),
    "claude-haiku-4-5": Pricing(1.0, 5.0),
    "gpt-4o": Pricing(2.5, 10.0),
    "gpt-4o-mini": Pricing(0.15, 0.6),
    "deepseek-chat": Pricing(0.27, 1.10),
    "qwen-plus": Pricing(0.4, 1.2),
    # 兜底：未知模型按一个偏保守的均价计，避免账单被严重低估。
    "*": Pricing(1.0, 4.0),
}


# ---------------------------------------------------------------------------
# 聚合结构
# ---------------------------------------------------------------------------

@dataclass
class ModelCost:
    """按模型聚合的消耗。"""

    model: str = ""
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    total_latency_ms: int = 0


@dataclass
class Report:
    """成本汇总报告。"""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    by_model: list[ModelCost] = field(default_factory=list)

    def __str__(self) -> str:
        """把报告渲染成表格，供 CLI 收尾打印。"""
        out: list[str] = []
        out.append(
            "模型调用 %d 次 | 输入 %s tokens | 输出 %s tokens | 估算成本 $%.4f"
            % (self.requests, human_num(self.input_tokens),
               human_num(self.output_tokens), self.cost_usd)
        )
        if self.by_model:
            out.append("")
            out.append("  模型                  调用   输入tok    输出tok     成本      平均延迟")
            for m in self.by_model:
                avg = ""
                if m.requests > 0:
                    avg = "%dms" % (m.total_latency_ms // m.requests)
                out.append(
                    "  %-20s %5d %10s %10s  $%7.4f  %8s"
                    % (clip_runes(m.model, 20), m.requests,
                       human_num(m.input_tokens), human_num(m.output_tokens),
                       m.cost_usd, avg)
                )
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Accountant
# ---------------------------------------------------------------------------

class Accountant:
    """在 Harness 层拦截并记录 token 消耗。"""

    def __init__(self, pricing: dict[str, Pricing] | None = None) -> None:
        """创建记账器。

        内置 DEFAULT_PRICING 先全量拷贝，再用传入的 pricing 覆盖/新增个别模型，
        保证调用方传入的价目表永不破坏默认兜底项 "*"。
        """
        self._mu = threading.Lock()
        self._pricing: dict[str, Pricing] = dict(DEFAULT_PRICING)
        if pricing:
            self._pricing.update(pricing)
        self._by_model: dict[str, ModelCost] = {}
        self._requests: int = 0

    def set_pricing(self, model: str, p: Pricing) -> None:
        """覆盖或新增某个模型的单价。"""
        with self._mu:
            self._pricing[model] = p

    def record(self, model: str, usage: "Usage", latency_ms: int) -> None:
        """记录一次模型调用。

        latency_ms 对应 Go 的 latencyMS（time.Duration → 毫秒整数，见全局约定）。
        """
        with self._mu:
            if model == "":
                model = "unknown"

            mc = self._by_model.get(model)
            if mc is None:
                mc = ModelCost(model=model)
                self._by_model[model] = mc

            # 命中不了精确价目表就退回兜底均价 "*"。
            pr = self._pricing.get(model)
            if pr is None:
                pr = self._pricing["*"]

            cost = (
                float(usage.input_tokens) / 1e6 * pr.input_per_mtok
                + float(usage.output_tokens) / 1e6 * pr.output_per_mtok
            )

            mc.requests += 1
            mc.input_tokens += usage.input_tokens
            mc.output_tokens += usage.output_tokens
            mc.cost_usd += cost
            mc.total_latency_ms += latency_ms
            self._requests += 1

    def snapshot(self) -> Report:
        """取当前快照（按成本从高到低排序，最贵的排最前）。"""
        with self._mu:
            r = Report(requests=self._requests)
            for mc in self._by_model.values():
                r.input_tokens += mc.input_tokens
                r.output_tokens += mc.output_tokens
                r.cost_usd += mc.cost_usd
                r.by_model.append(mc)
            r.by_model.sort(key=lambda m: m.cost_usd, reverse=True)
            return r


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def human_num(n: int) -> str:
    """把大整数压成 k / M 的紧凑写法，用于报告对齐。"""
    if n < 1000:
        return "%d" % n
    if n < 1_000_000:
        return "%.1fk" % (n / 1000)
    return "%.2fM" % (n / 1_000_000)


def clip_runes(s: str, max_chars: int) -> str:
    """按字符截断，避免把中文/emoji 切成乱码。"""
    runes = list(s)
    if len(runes) <= max_chars:
        return s
    return "".join(runes[:max_chars]) + "…"
