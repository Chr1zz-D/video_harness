"""
harness.prompt_surgeon — 内环的"prompt 外科医生"。

拿判官的 critique + 失败的 prompt，产出下一次尝试的修订版。
两级策略：
  1. 确定性修补（零成本）：判官 suggested_negative_additions 直接追加进负面约束；
  2. LLM 修补（可选）：配置了 LLM 时让它基于 critique 微调正向描述——
     只允许改写措辞和补约束，禁止改变分镜语义（时长/主体/资产引用不可动）。

所有修订都会被 orchestrator 记进 run log，外环周任务从 log 里挖
"哪些补丁真的提升了通过率"，人审后合入 negative.yaml 模板库。
"""
from __future__ import annotations

import os

import httpx

from harness.cost import LLM_PRICE_IN, LLM_PRICE_OUT, CostTracker
from harness.models import CompiledPrompt, Verdict

SURGEON_SYSTEM = """你是提示词修复专家。给你一个 AI 视频生成的失败 prompt 和质检批注，
请输出修订后的正向 prompt（只输出 prompt 文本，不解释）。
规则：不得改变镜头语义（场景/主体/动作/时长），只允许：
- 把批注中崩坏的部位描述得更明确稳定（如手部动作改为更简单的姿态）；
- 强化保真表述；
- 精简可能诱发画面混乱的形容词。"""


class PromptSurgeon:
    def __init__(self, use_llm: bool = True, cost_tracker: CostTracker | None = None):
        self.base_url = os.environ.get("LLM_BASE_URL", "")
        self.api_key = os.environ.get("LLM_API_KEY", "")
        self.model = os.environ.get("LLM_MODEL", "deepseek-chat")
        self.use_llm = use_llm and bool(self.base_url)
        self.cost_tracker = cost_tracker

    def revise(self, prompt: CompiledPrompt, verdict: Verdict) -> CompiledPrompt:
        revised = prompt.model_copy(deep=True)

        # 1) 确定性：追加判官建议的负面约束（去重）
        existing = [t.strip() for t in revised.negative.split(",") if t.strip()]
        for term in verdict.suggested_negative_additions:
            if term not in existing:
                existing.append(term)
        revised.negative = ", ".join(existing)

        # 2) LLM：基于 critique 微调正向描述
        if self.use_llm and verdict.critique:
            try:
                revised.positive = self._llm_revise(prompt.positive, verdict.critique, prompt.shot_id)
            except Exception:  # noqa: BLE001 — LLM 修补失败不阻塞，保底走确定性修补
                pass
        return revised

    def _llm_revise(self, positive: str, critique: str, shot_id: str) -> str:
        with httpx.Client(timeout=60) as client:
            r = client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0.3,
                    "messages": [
                        {"role": "system", "content": SURGEON_SYSTEM},
                        {"role": "user", "content": f"失败 prompt：\n{positive}\n\n质检批注：\n{critique}"},
                    ],
                },
            )
            r.raise_for_status()
            body = r.json()
            if self.cost_tracker:
                self.cost_tracker.log_llm_usage(
                    "llm_surgeon", body.get("usage", {}), LLM_PRICE_IN, LLM_PRICE_OUT,
                    shot_id=shot_id, model=self.model,
                )
            return body["choices"][0]["message"]["content"].strip()
