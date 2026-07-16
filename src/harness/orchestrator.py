"""
harness.orchestrator — 内环编排：生成 -> 判官 -> 外科医生 -> 重生成，硬性封顶。

成本纪律写死在这里：
  - max_attempts 封顶（默认 3），超限升级人工而不是无限烧钱；
  - 每次尝试的 (prompt, result, verdict) 全量落 run log（外环学习的原料）；
  - 成本台账逐镜头累计，跑完输出"每成片秒成本"。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from harness.adapters.base import VideoAdapter
from harness.cost import CostTracker
from harness.judge import VLMJudge
from harness.models import (
    AssetStore,
    CompiledPrompt,
    ShotRunLog,
    Storyboard,
    Verdict,
)
from harness.prompt_surgeon import PromptSurgeon


@dataclass
class ShotOutcome:
    shot_id: str
    passed: bool
    final_video: str | None
    attempts: int
    cost_rmb: float
    escalated: bool = False  # 超限升级人工


@dataclass
class RunReport:
    run_dir: str
    outcomes: list[ShotOutcome] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return sum(o.cost_rmb for o in self.outcomes)

    @property
    def finished_seconds(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)  # 占位，装配后按实际时长算

    def summary(self) -> str:
        ok = sum(1 for o in self.outcomes if o.passed)
        esc = [o.shot_id for o in self.outcomes if o.escalated]
        lines = [
            f"镜头通过 {ok}/{len(self.outcomes)}，总成本估算 ¥{self.total_cost:.2f}",
        ]
        if esc:
            lines.append(f"升级人工: {', '.join(esc)}")
        return "\n".join(lines)


class Orchestrator:
    def __init__(
        self,
        adapter: VideoAdapter,
        judge: VLMJudge,
        surgeon: PromptSurgeon,
        run_dir: str,
        max_attempts: int = 3,
        cost_tracker: CostTracker | None = None,
    ):
        self.adapter = adapter
        self.judge = judge
        self.surgeon = surgeon
        self.run_dir = run_dir
        self.max_attempts = max_attempts
        self.cost_tracker = cost_tracker
        os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
        os.makedirs(os.path.join(run_dir, "shots"), exist_ok=True)

    # ------------------------------------------------------------------
    def run(
        self,
        storyboard: Storyboard,
        assets: AssetStore,
        prompts: list[CompiledPrompt],
    ) -> RunReport:
        report = RunReport(run_dir=self.run_dir)
        shots_by_id = {s.shot_id: s for s in storyboard.shots}

        for prompt in prompts:
            shot = shots_by_id[prompt.shot_id]
            outcome = self._run_shot(shot, assets, prompt)
            report.outcomes.append(outcome)

        with open(os.path.join(self.run_dir, "report.json"), "w", encoding="utf-8") as f:
            json.dump(
                [o.__dict__ for o in report.outcomes], f, ensure_ascii=False, indent=2
            )
        return report

    # ------------------------------------------------------------------
    def _run_shot(self, shot, assets: AssetStore, prompt: CompiledPrompt) -> ShotOutcome:
        cost = 0.0
        current = prompt
        out_dir = os.path.join(self.run_dir, "shots", shot.shot_id)

        for attempt in range(1, self.max_attempts + 1):
            gen = self.adapter.generate(current, out_dir, attempt)
            cost += gen.cost_estimate_rmb
            if self.cost_tracker:
                self.cost_tracker.log(
                    "video", gen.cost_estimate_rmb, shot_id=shot.shot_id,
                    attempt=attempt, ok=gen.ok,
                )

            verdict: Verdict | None = None
            if gen.ok and gen.video_path:
                verdict = self.judge.judge(shot, assets, gen.video_path, attempt)

            self._log(ShotRunLog(
                shot_id=shot.shot_id, attempt=attempt,
                prompt=current, generation=gen, verdict=verdict,
            ))

            if gen.ok and verdict and verdict.passed:
                return ShotOutcome(
                    shot_id=shot.shot_id, passed=True,
                    final_video=gen.video_path, attempts=attempt, cost_rmb=cost,
                )

            # 失败：让外科医生修 prompt 再来（生成失败没有 verdict 就原样重试）
            if verdict is not None:
                current = self.surgeon.revise(current, verdict)

        return ShotOutcome(
            shot_id=shot.shot_id, passed=False, final_video=None,
            attempts=self.max_attempts, cost_rmb=cost, escalated=True,
        )

    def _log(self, entry: ShotRunLog) -> None:
        path = os.path.join(self.run_dir, "logs", f"{entry.shot_id}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
