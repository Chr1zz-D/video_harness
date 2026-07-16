"""
harness.cost — 全链路成本台账（Phase 2：成本看板的数据源）。

覆盖三类花费：
  1. video        —— 每次视频生成尝试（含重试），来自 GenerationResult.cost_estimate_rmb；
  2. llm_storyboard / llm_surgeon —— 文本 LLM 调用（StoryboardAgent / PromptSurgeon），按 token 估算；
  3. judge         —— VLMJudge 的视觉判官调用，按 token 估算。

token 单价是粗估默认值，可用环境变量覆盖；真实对账以厂商账单为准（视频侧同样的
处理方式见 adapters/kling_bailian.py 的 ROUGH_PRICE_RMB_PER_S）——这里的目标是
"心里有数"，不是"分毫不差"。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _price(env_key: str, default: float) -> float:
    try:
        return float(os.environ.get(env_key, default))
    except ValueError:
        return default


# 粗估单价：元 / 1K tokens。仅用于成本看板估算，不是真实计费口径。
LLM_PRICE_IN = _price("LLM_PRICE_IN_PER_1K", 0.002)      # deepseek-chat 量级
LLM_PRICE_OUT = _price("LLM_PRICE_OUT_PER_1K", 0.008)
JUDGE_PRICE_IN = _price("JUDGE_PRICE_IN_PER_1K", 0.004)   # qwen-vl-plus 量级（含图像 token）
JUDGE_PRICE_OUT = _price("JUDGE_PRICE_OUT_PER_1K", 0.012)


@dataclass
class CostEvent:
    stage: str                          # "video" | "llm_storyboard" | "llm_surgeon" | "judge"
    shot_id: str | None
    cost_rmb: float
    detail: dict = field(default_factory=dict)


class CostTracker:
    """跑一条片期间的全量花费事件流，边跑边落盘 logs/cost_events.jsonl。"""

    def __init__(self, run_dir: str):
        self.run_dir = run_dir
        self.events: list[CostEvent] = []
        os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
        self._path = os.path.join(run_dir, "logs", "cost_events.jsonl")

    def log(self, stage: str, cost_rmb: float, shot_id: str | None = None, **detail) -> None:
        ev = CostEvent(stage=stage, shot_id=shot_id, cost_rmb=cost_rmb, detail=detail)
        self.events.append(ev)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"stage": ev.stage, "shot_id": ev.shot_id, "cost_rmb": ev.cost_rmb, **ev.detail},
                ensure_ascii=False,
            ) + "\n")

    def log_llm_usage(
        self,
        stage: str,
        usage: dict,
        price_in: float,
        price_out: float,
        shot_id: str | None = None,
        model: str = "",
    ) -> float:
        """按 OpenAI 兼容响应里的 usage.{prompt_tokens,completion_tokens} 估算成本并记账。"""
        tin = usage.get("prompt_tokens", 0)
        tout = usage.get("completion_tokens", 0)
        cost = tin / 1000 * price_in + tout / 1000 * price_out
        self.log(stage, cost, shot_id=shot_id, model=model, tokens_in=tin, tokens_out=tout)
        return cost

    def total(self) -> float:
        return sum(e.cost_rmb for e in self.events)

    def by_stage(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.events:
            out[e.stage] = out.get(e.stage, 0.0) + e.cost_rmb
        return out

    def by_shot(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.events:
            if e.shot_id:
                out[e.shot_id] = out.get(e.shot_id, 0.0) + e.cost_rmb
        return out

    def summary(self, final_duration_s: float | None) -> dict:
        total = self.total()
        return {
            "total_rmb": round(total, 4),
            "by_stage_rmb": {k: round(v, 4) for k, v in self.by_stage().items()},
            "by_shot_rmb": {k: round(v, 4) for k, v in self.by_shot().items()},
            "final_duration_s": final_duration_s,
            "rmb_per_final_second": (
                round(total / final_duration_s, 4) if final_duration_s else None
            ),
            "event_count": len(self.events),
        }

    @staticmethod
    def load_events(run_dir: str) -> list[dict]:
        path = os.path.join(run_dir, "logs", "cost_events.jsonl")
        if not os.path.exists(path):
            return []
        events = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events
