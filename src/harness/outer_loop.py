"""
harness.outer_loop — 外环学习器：把内环的一次性发现变成跨视频的持久资产。

解决的问题：内环里外科医生追加的约束只救了当前镜头，跑完就散场——
下一条视频还会在同一个坑里再烧一次钱。外环把 run log 里的证据挖出来，
变成两个持久产物：

  1. learned/constraint_stats.json — 每条约束词的"疗效"统计（编译器直接消费，
     按疗效排序 + 预算裁剪，学到的东西自动改变后续所有生成，无需人写 prompt）；
  2. learned/proposal_<date>.md — 带证据的入库提案（哪些词该正式进
     negative.yaml、哪些失败模式约束治不了该走资产层），**人审后**用
     apply 命令合入。

归因方法（flip attribution）：
  同一镜头 attempt k → k+1 之间，外科医生新增了哪些约束词（diff 负面串），
  同时哪些 rubric 项从 False 翻成 True——共现即记一次 credit。
  这不是严格因果（判官有噪声、正向描述也可能同时被改写），所以：
  - 统计里保留样本量，flip_rate 只在 added ≥ min_evidence 时才参与排序；
  - 入库走人审 gate，学习器只提证据不做决定；
  - 严格因果要靠 A/B（同 prompt ± 单条约束对照生成），留作升级路径，
    烧钱换置信度，等某条约束的入库决策真值几百块钱再上。

用法：
  python -m harness.outer_loop propose runs/            # 挖日志出提案
  python -m harness.outer_loop apply learned/approved_terms.yaml \
      --target templates/ads_clothing/kling/negative.yaml   # 人审后合入
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict
from datetime import date

import yaml

from harness.models import ShotRunLog

LEARNED_DIR = "learned"
MIN_EVIDENCE = 2  # added 次数达到才参与排序/入库提案，低于此只观察


# ---------------------------------------------------------------------------
# 挖掘与归因
# ---------------------------------------------------------------------------

def load_logs(runs_dir: str) -> dict[str, list[ShotRunLog]]:
    """按 (run, shot) 分组，attempt 升序。

    只认 shot_*.jsonl——logs/ 目录里可能还有其他模块的产物
    （如 CostTracker 的 cost_events.jsonl），不属于内环记录，跳过。
    单行解析失败也跳过而不是崩：外环是离线批任务，
    宁可少吃一条脏数据，不能因为一条脏数据罢工。
    """
    grouped: dict[str, list[ShotRunLog]] = defaultdict(list)
    for path in glob.glob(os.path.join(runs_dir, "*", "logs", "shot_*.jsonl")):
        run_id = path.split(os.sep)[-3]
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = ShotRunLog.model_validate_json(line)
                except Exception:  # noqa: BLE001
                    continue
                grouped[f"{run_id}/{entry.shot_id}"].append(entry)
    for key in grouped:
        grouped[key].sort(key=lambda e: e.attempt)
    return grouped


def _terms(negative: str) -> set[str]:
    return {t.strip() for t in negative.split(",") if t.strip()}


def mine(runs_dir: str) -> tuple[dict, dict]:
    """
    返回 (constraint_stats, failure_modes)：
      constraint_stats[term] = {added, flips, flip_rate, rubric_keys_flipped}
      failure_modes[rubric_key] = {fails, healed_by_terms, unhealed}
        —— unhealed 高的项就是"约束治不了"的分诊对象
    """
    stats: dict[str, dict] = defaultdict(
        lambda: {"added": 0, "flips": 0, "rubric_keys_flipped": defaultdict(int)}
    )
    failures: dict[str, dict] = defaultdict(
        lambda: {"fails": 0, "healed": 0, "unhealed": 0}
    )

    for _, attempts in load_logs(runs_dir).items():
        for prev, curr in zip(attempts, attempts[1:]):
            if prev.verdict is None or curr.verdict is None:
                continue
            new_terms = _terms(curr.prompt.negative) - _terms(prev.prompt.negative)
            flipped = [
                k for k, ok in curr.verdict.item_results.items()
                if ok and prev.verdict.item_results.get(k) is False
            ]
            for term in new_terms:
                stats[term]["added"] += 1
                if flipped:
                    stats[term]["flips"] += 1
                    for k in flipped:
                        stats[term]["rubric_keys_flipped"][k] += 1

        # 失败模式统计：最后一次尝试仍挂的 rubric 项 = 约束（和改写）没治好
        for entry in attempts:
            if entry.verdict is None:
                continue
            for k, ok in entry.verdict.item_results.items():
                if not ok:
                    failures[k]["fails"] += 1
        last = attempts[-1]
        if last.verdict is not None:
            for k, ok in last.verdict.item_results.items():
                if not ok:
                    failures[k]["unhealed"] += 1
                elif failures[k]["fails"] > 0:
                    failures[k]["healed"] += 1

    for term, s in stats.items():
        s["flip_rate"] = round(s["flips"] / s["added"], 3) if s["added"] else 0.0
        s["rubric_keys_flipped"] = dict(s["rubric_keys_flipped"])
    return dict(stats), dict(failures)


# ---------------------------------------------------------------------------
# 产物输出
# ---------------------------------------------------------------------------

def propose(runs_dir: str, out_dir: str = LEARNED_DIR) -> str:
    stats, failures = mine(runs_dir)
    os.makedirs(out_dir, exist_ok=True)

    # 1) 权重文件：编译器直接消费（含证据不足的词，权重为 0 只观察不参与排序）
    with open(os.path.join(out_dir, "constraint_stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    # 2) 入库提案（人审对象）
    candidates = sorted(
        ((t, s) for t, s in stats.items() if s["added"] >= MIN_EVIDENCE and s["flip_rate"] > 0),
        key=lambda kv: (-kv[1]["flip_rate"], -kv[1]["added"]),
    )
    triage = sorted(
        ((k, v) for k, v in failures.items() if v["unhealed"] > 0),
        key=lambda kv: -kv[1]["unhealed"],
    )

    lines = [f"# 外环学习提案 {date.today().isoformat()}", ""]
    lines.append("## 一、建议入库的约束词（人审后跑 apply 合入 negative.yaml）\n")
    if candidates:
        lines.append("| 约束词 | 被追加次数 | 伴随翻盘次数 | flip_rate | 翻盘的 rubric 项 |")
        lines.append("|---|---|---|---|---|")
        for t, s in candidates:
            keys = ", ".join(f"{k}×{n}" for k, n in s["rubric_keys_flipped"].items())
            lines.append(f"| {t} | {s['added']} | {s['flips']} | {s['flip_rate']} | {keys} |")
        approved = {
            "terms": [
                {"term": t, "reason": f"外环学习入库：{s['added']}次追加中{s['flips']}次伴随翻盘"}
                for t, s in candidates
            ]
        }
        with open(os.path.join(out_dir, "approved_terms.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(approved, f, allow_unicode=True, sort_keys=False)
        lines.append("\n（已生成 approved_terms.yaml 草稿——**删掉你不认可的条目**再 apply）")
    else:
        lines.append(f"暂无达到证据门槛（added ≥ {MIN_EVIDENCE} 且有翻盘）的候选词。")

    lines.append("\n## 二、分诊：负面约束治不了的失败模式（走资产层/动作简化/换模型）\n")
    if triage:
        lines.append("| rubric 项 | 累计失败 | 修好 | 到最后仍挂 | 建议方向 |")
        lines.append("|---|---|---|---|---|")
        hints = {
            "character_consistency": "上参考图/主体ID，不是加约束",
            "product_integrity": "商品实拍图走 omni refer 通道",
            "anatomy": "简化分镜里的手部动作设计",
            "emotion_delivery": "改正向模板的情绪外化措辞",
        }
        for k, v in triage:
            hint = hints.get(k, "人工看样片定位")
            lines.append(f"| {k} | {v['fails']} | {v['healed']} | {v['unhealed']} | {hint} |")
    else:
        lines.append("无——所有失败模式都在内环内被治愈。")

    lines.append(
        "\n> 归因说明：flip 是共现证据不是严格因果（同一步正向描述可能也被改写、"
        "判官有噪声）。样本量小时以观察为主；高价值决策上 A/B 对照。"
    )

    proposal_path = os.path.join(out_dir, f"proposal_{date.today().isoformat()}.md")
    with open(proposal_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return proposal_path


def apply(approved_yaml: str, target_negative_yaml: str) -> int:
    """把人审过的词条合入模板库（去重），返回新增条数。"""
    with open(approved_yaml, encoding="utf-8") as f:
        approved = yaml.safe_load(f) or {}
    with open(target_negative_yaml, encoding="utf-8") as f:
        lib = yaml.safe_load(f)

    existing = {c["term"] for c in lib["constraints"]}
    added = 0
    for item in approved.get("terms", []):
        if item["term"] not in existing:
            lib["constraints"].append({"term": item["term"], "reason": item["reason"]})
            added += 1

    if added:
        with open(target_negative_yaml, "w", encoding="utf-8") as f:
            yaml.dump(lib, f, allow_unicode=True, sort_keys=False)
    return added


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("propose", help="挖 run log，输出提案与权重文件")
    p1.add_argument("runs_dir")
    p1.add_argument("--out", default=LEARNED_DIR)
    p2 = sub.add_parser("apply", help="人审后把 approved_terms.yaml 合入约束库")
    p2.add_argument("approved_yaml")
    p2.add_argument("--target", required=True, help="目标 negative.yaml 路径")
    args = ap.parse_args()

    if args.cmd == "propose":
        path = propose(args.runs_dir, args.out)
        print(f"提案已生成: {path}")
        print(f"权重文件: {os.path.join(args.out, 'constraint_stats.json')}（编译器自动消费）")
    else:
        n = apply(args.approved_yaml, args.target)
        print(f"合入 {n} 条新约束到 {args.target}")


if __name__ == "__main__":
    main()
