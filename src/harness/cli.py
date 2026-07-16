"""
harness.cli — 端到端跑一条片。

离线演示（不花钱，不需要任何 API key）：
    python -m harness.cli examples/brief_clothing_ad.json \
        --storyboard examples/storyboard_clothing_ad.json --adapter mock

真实生成（配好环境变量后）：
    python -m harness.cli examples/brief_clothing_ad.json --adapter kling
    # 不带 --storyboard 时会调 LLM 现场生成分镜表（需 LLM_BASE_URL/LLM_API_KEY）
"""
from __future__ import annotations

import argparse
import json
import os
import time

from harness.adapters.base import AdapterRegistry
from harness.adapters.mock import MockVideoAdapter
from harness.assembler import assemble
from harness.cost import CostTracker
from harness.judge import VLMJudge
from harness.models import AssetStore, Brief, Storyboard
from harness.orchestrator import Orchestrator
from harness.prompt_compiler import PromptCompiler
from harness.prompt_surgeon import PromptSurgeon
from harness.storyboard_agent import StoryboardAgent

TEMPLATES_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "templates")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("brief", help="brief JSON 路径")
    ap.add_argument("--storyboard", help="现成分镜表 JSON（跳过 LLM）")
    ap.add_argument("--adapter", default="mock", choices=["mock", "kling"])
    ap.add_argument("--mode", default="draft", choices=["draft", "final"])
    ap.add_argument("--max-attempts", type=int, default=3)
    args = ap.parse_args()

    with open(args.brief, encoding="utf-8") as f:
        brief = Brief.model_validate_json(f.read())
    assets = AssetStore.from_list(brief.assets)

    # 0) run_dir 提前建好：brief/storyboard/成本事件全部落这里，
    #    是 review_app（人工审核 + 成本看板）读取的唯一数据源。
    run_dir = os.path.join("runs", f"{brief.brief_id}_{int(time.time())}")
    os.makedirs(run_dir, exist_ok=True)
    cost_tracker = CostTracker(run_dir)
    with open(os.path.join(run_dir, "brief.json"), "w", encoding="utf-8") as f:
        f.write(brief.model_dump_json(indent=2))

    # 1) 分镜表：现成的或 LLM 现场生成
    if args.storyboard:
        with open(args.storyboard, encoding="utf-8") as f:
            sb = Storyboard.model_validate_json(f.read())
    else:
        sb = StoryboardAgent(cost_tracker=cost_tracker).run(brief)
    with open(os.path.join(run_dir, "storyboard.json"), "w", encoding="utf-8") as f:
        f.write(sb.model_dump_json(indent=2))
    print(f"[storyboard] {len(sb.shots)} 个镜头, 共 {sb.total_duration()}s")

    # 2) 编译 prompt（mock 演示复用 kling 模板库，措辞完全一致，只是不真调 API）
    template_family = "kling"
    compiler = PromptCompiler(TEMPLATES_ROOT, brief.category.value, template_family)
    prompts = compiler.compile_all(sb, assets, brief.aspect_ratio, mode=args.mode)

    # 3) 注册适配器
    AdapterRegistry.register(MockVideoAdapter())
    if args.adapter == "kling":
        from harness.adapters.kling_bailian import KlingBailianAdapter
        AdapterRegistry.register(KlingBailianAdapter())
    adapter = AdapterRegistry.get(args.adapter)

    # 4) 内环
    orch = Orchestrator(
        adapter=adapter,
        judge=VLMJudge(TEMPLATES_ROOT, brief.category.value, cost_tracker=cost_tracker),
        surgeon=PromptSurgeon(cost_tracker=cost_tracker),
        run_dir=run_dir,
        max_attempts=args.max_attempts,
        cost_tracker=cost_tracker,
    )
    report = orch.run(sb, assets, prompts)
    print("[inner-loop]", report.summary())

    # 5) 装配通过的镜头
    shots_by_id = {s.shot_id: s for s in sb.shots}
    order = {s.shot_id: i for i, s in enumerate(sb.shots)}
    passed = sorted(
        (o for o in report.outcomes if o.passed and o.final_video),
        key=lambda o: order[o.shot_id],
    )
    final_duration_s: float | None = None
    if passed:
        final = assemble([o.final_video for o in passed], os.path.join(run_dir, "final.mp4"))
        final_duration_s = sum(shots_by_id[o.shot_id].duration_s for o in passed)
        print(f"[assemble] 成片: {final}")
    else:
        print("[assemble] 没有镜头通过质检，全部升级人工")

    # 6) 成本看板数据：全链路（视频重试 + LLM/判官 token）成本汇总
    cost_summary = cost_tracker.summary(final_duration_s)
    with open(os.path.join(run_dir, "cost.json"), "w", encoding="utf-8") as f:
        json.dump(cost_summary, f, ensure_ascii=False, indent=2)

    print(f"[run] 全部产物与日志在 {run_dir}/")
    print(json.dumps(
        {"cost_rmb": round(cost_summary["total_rmb"], 2), "run_dir": run_dir},
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
