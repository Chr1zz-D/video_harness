"""
harness.storyboard_agent — brief -> storyboard.json。

这是整条流水线里 LLM 唯一"发挥创造力"的环节。
输出必须通过 Storyboard schema 校验；校验失败时把 pydantic 报错喂回去修复，
最多修 N 次，修不好就升级人工——绝不带病进入生成阶段。

LLM 走 OpenAI-compatible 接口（DeepSeek / Qwen / 任何兼容端点都行，中文剧本
用国产模型文案更地道且便宜）。环境变量：
  LLM_BASE_URL   e.g. https://api.deepseek.com/v1  或  https://dashscope.aliyuncs.com/compatible-mode/v1
  LLM_API_KEY
  LLM_MODEL      e.g. deepseek-chat / qwen-plus
"""
from __future__ import annotations

import json
import os

import httpx
from pydantic import ValidationError

from harness.cost import LLM_PRICE_IN, LLM_PRICE_OUT, CostTracker
from harness.models import Brief, Storyboard

SYSTEM = """你是一名短视频导演兼分镜师，为{platform}平台创作{category}类内容。
你只输出一个 JSON 对象，不输出任何其他文字、不加 markdown 代码块。

创作要求：
1. 总时长必须等于 brief 里的 total_duration_s，单镜头 2-10 秒；
2. emotion 字段必须写出情绪的**外化表现**（表情、肢体、呼吸），因为下游 AI 视频模型
   不会自己脑补情绪——你不写具体，画面就是面瘫；
3. subject 具体到动作层面（"左手拎起风衣下摆转身"，而不是"展示衣服"）；
4. 引用了商品/角色的镜头，必须在 asset_refs 里写 asset_id，且描述与资产的
   must_preserve 保真点不冲突；
5. camera 只能取: static/push_in/pull_out/pan/tilt/tracking/handheld；
6. 遵守 hard_constraints 的每一条，违反即废稿。

JSON schema（字段必须完全一致）：
{schema}
"""

USER_TMPL = """brief 如下：
{brief_json}

可引用的已锁定资产：
{assets_block}

请输出 storyboard JSON（顶层字段: brief_id, version, shots）。"""

REPAIR_TMPL = """你上一版输出未通过 schema 校验，错误如下：
{errors}

请修复并重新输出完整 JSON（只输出 JSON）。"""


class StoryboardAgent:
    def __init__(self, max_repairs: int = 2, cost_tracker: CostTracker | None = None):
        self.base_url = os.environ.get("LLM_BASE_URL", "")
        self.api_key = os.environ.get("LLM_API_KEY", "")
        self.model = os.environ.get("LLM_MODEL", "deepseek-chat")
        self.max_repairs = max_repairs
        self.cost_tracker = cost_tracker

    # ------------------------------------------------------------------
    def run(self, brief: Brief) -> Storyboard:
        if not self.base_url:
            raise RuntimeError("缺少 LLM_BASE_URL / LLM_API_KEY（离线演示请用 --storyboard 传入现成分镜表）")

        assets_block = "\n".join(
            f"- {a.asset_id} [{a.kind.value}] {a.description}（保真点: {', '.join(a.must_preserve) or '无'}）"
            for a in brief.assets
        ) or "（无）"

        messages = [
            {
                "role": "system",
                "content": SYSTEM.format(
                    platform=brief.target_platform,
                    category=brief.category.value,
                    schema=json.dumps(Storyboard.model_json_schema(), ensure_ascii=False),
                ),
            },
            {
                "role": "user",
                "content": USER_TMPL.format(
                    brief_json=brief.model_dump_json(exclude={"assets"}),
                    assets_block=assets_block,
                ),
            },
        ]

        last_err = ""
        for _ in range(self.max_repairs + 1):
            raw, usage = self._chat(messages)
            if self.cost_tracker:
                self.cost_tracker.log_llm_usage(
                    "llm_storyboard", usage, LLM_PRICE_IN, LLM_PRICE_OUT, model=self.model
                )
            try:
                sb = Storyboard.model_validate_json(_strip_fences(raw))
            except ValidationError as e:
                last_err = str(e)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": REPAIR_TMPL.format(errors=last_err[:2000])})
                continue

            # schema 之外的业务校验
            problems = self._business_checks(sb, brief)
            if problems:
                last_err = "\n".join(problems)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": REPAIR_TMPL.format(errors=last_err)})
                continue
            return sb

        raise RuntimeError(f"分镜表修复 {self.max_repairs} 次仍不合格，升级人工。最后错误:\n{last_err}")

    # ------------------------------------------------------------------
    @staticmethod
    def _business_checks(sb: Storyboard, brief: Brief) -> list[str]:
        problems = []
        if abs(sb.total_duration() - brief.total_duration_s) > 2:
            problems.append(
                f"总时长 {sb.total_duration()}s 与 brief 要求 {brief.total_duration_s}s 偏差超过 2s"
            )
        valid_ids = {a.asset_id for a in brief.assets}
        for shot in sb.shots:
            for ref in shot.asset_refs:
                if ref not in valid_ids:
                    problems.append(f"{shot.shot_id} 引用了未锁定的资产 '{ref}'")
        # 电商广告：商品资产至少要在一半的镜头里出现
        product_ids = {a.asset_id for a in brief.assets if a.kind.value == "product"}
        if product_ids and brief.category.value.startswith("ads"):
            hit = sum(1 for s in sb.shots if set(s.asset_refs) & product_ids)
            if hit < len(sb.shots) / 2:
                problems.append(f"商品仅出现在 {hit}/{len(sb.shots)} 个镜头，广告片商品露出不足")
        return problems

    # ------------------------------------------------------------------
    def _chat(self, messages: list[dict]) -> tuple[str, dict]:
        with httpx.Client(timeout=120) as client:
            r = client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "messages": messages, "temperature": 0.7},
            )
            r.raise_for_status()
            body = r.json()
            return body["choices"][0]["message"]["content"], body.get("usage", {})


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
