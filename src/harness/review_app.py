"""
harness.review_app — 人工审核 + 成本看板的 Web 面板（Phase 2）。

只读 cli.py 落在 runs/<run_id>/ 下的产物：
    brief.json / storyboard.json / report.json / cost.json /
    logs/<shot_id>.jsonl / logs/cost_events.jsonl / shots/<shot_id>/*.mp4 / final.mp4
不侵入内环逻辑，纯离线可用。

人工审核的决定落盘到 runs/<run_id>/human_review.json，这正是架构图里
"人只在关键 gate 介入"的落点：机审（VLMJudge）负责把废片挡在装配之前，
人审在这里对结果做最终确认或打回，通过后可一键生成"人工确认版"成片。

启动：
    pip install -e ".[review]"
    python -m harness.review_app
    # 或指定端口/运行目录: REVIEW_PORT=8080 REVIEW_RUNS_DIR=/path/to/runs python -m harness.review_app
    # 打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from harness.assembler import assemble
from harness.cost import CostTracker

_HERE = os.path.dirname(__file__)
RUNS_ROOT = os.path.abspath(
    os.environ.get("REVIEW_RUNS_DIR") or os.path.join(_HERE, "..", "..", "runs")
)
STATIC_DIR = os.path.join(_HERE, "review_static")

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]+$")

app = FastAPI(title="video-harness 审核面板")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# 路径安全：run_id / shot_id 只允许字母数字下划线短横线，且解析后必须落在 RUNS_ROOT 内
# ---------------------------------------------------------------------------

def _check_id(value: str, label: str) -> None:
    if not _SAFE_ID.match(value):
        raise HTTPException(400, f"非法{label}: {value}")


def _run_dir(run_id: str) -> str:
    _check_id(run_id, "run_id")
    path = os.path.abspath(os.path.join(RUNS_ROOT, run_id))
    if os.path.commonpath([path, RUNS_ROOT]) != RUNS_ROOT or not os.path.isdir(path):
        raise HTTPException(404, f"run 不存在: {run_id}")
    return path


def _read_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# 只读接口
# ---------------------------------------------------------------------------

@app.get("/api/runs")
def list_runs():
    if not os.path.isdir(RUNS_ROOT):
        return []
    runs = []
    for name in sorted(os.listdir(RUNS_ROOT), reverse=True):
        path = os.path.join(RUNS_ROOT, name)
        report_path = os.path.join(path, "report.json")
        if not os.path.isdir(path) or not os.path.exists(report_path):
            continue
        brief = _read_json(os.path.join(path, "brief.json"), {})
        report = _read_json(report_path, [])
        cost = _read_json(os.path.join(path, "cost.json"), {})
        runs.append({
            "run_id": name,
            "title": brief.get("title") or brief.get("brief_id") or name,
            "category": brief.get("category"),
            "shot_count": len(report),
            "passed_count": sum(1 for o in report if o.get("passed")),
            "total_rmb": cost.get("total_rmb"),
            "has_final": os.path.exists(os.path.join(path, "final.mp4")),
            "has_final_human": os.path.exists(os.path.join(path, "final_human.mp4")),
        })
    return runs


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run_dir = _run_dir(run_id)
    brief = _read_json(os.path.join(run_dir, "brief.json"))
    storyboard = _read_json(os.path.join(run_dir, "storyboard.json"))
    report = _read_json(os.path.join(run_dir, "report.json"), [])
    cost = _read_json(os.path.join(run_dir, "cost.json"))
    human_review = _read_json(os.path.join(run_dir, "human_review.json"), {})

    outcomes_by_shot = {o["shot_id"]: o for o in report}
    shots_meta = {s["shot_id"]: s for s in (storyboard or {}).get("shots", [])}

    shots = []
    all_shot_ids = list(shots_meta) or list(outcomes_by_shot)
    for shot_id in all_shot_ids:
        shot_dir = os.path.join(run_dir, "shots", shot_id)
        attempts = sorted(
            int(m.group(1))
            for fn in (os.listdir(shot_dir) if os.path.isdir(shot_dir) else [])
            if (m := re.match(rf"^{re.escape(shot_id)}_a(\d+)\.mp4$", fn))
        )
        shots.append({
            "shot_id": shot_id,
            "meta": shots_meta.get(shot_id),
            "outcome": outcomes_by_shot.get(shot_id),
            "human": human_review.get(shot_id),
            "attempts_available": attempts,
        })

    if not cost:
        # 兼容成本台账功能上线之前跑的 run：只有视频成本，没有 LLM/判官明细
        cost = {
            "total_rmb": round(sum(o.get("cost_rmb", 0.0) for o in report), 4),
            "by_stage_rmb": {"video": round(sum(o.get("cost_rmb", 0.0) for o in report), 4)},
            "by_shot_rmb": {o["shot_id"]: round(o.get("cost_rmb", 0.0), 4) for o in report},
            "final_duration_s": None,
            "rmb_per_final_second": None,
            "event_count": 0,
            "legacy": True,
        }

    return {
        "run_id": run_id,
        "brief": brief,
        "cost": cost,
        "human_review": human_review,
        "shots": shots,
        "has_final": os.path.exists(os.path.join(run_dir, "final.mp4")),
        "has_final_human": os.path.exists(os.path.join(run_dir, "final_human.mp4")),
    }


@app.get("/api/runs/{run_id}/shots/{shot_id}/logs")
def get_shot_logs(run_id: str, shot_id: str):
    run_dir = _run_dir(run_id)
    _check_id(shot_id, "shot_id")
    return _read_jsonl(os.path.join(run_dir, "logs", f"{shot_id}.jsonl"))


@app.get("/api/runs/{run_id}/cost/events")
def get_cost_events(run_id: str):
    run_dir = _run_dir(run_id)
    return CostTracker.load_events(run_dir)


@app.get("/api/runs/{run_id}/media/shot/{shot_id}/{attempt}")
def get_shot_media(run_id: str, shot_id: str, attempt: int):
    run_dir = _run_dir(run_id)
    _check_id(shot_id, "shot_id")
    path = os.path.join(run_dir, "shots", shot_id, f"{shot_id}_a{attempt}.mp4")
    if not os.path.exists(path):
        raise HTTPException(404, "视频不存在")
    return FileResponse(path, media_type="video/mp4")


@app.get("/api/runs/{run_id}/media/final")
def get_final_media(run_id: str):
    run_dir = _run_dir(run_id)
    path = os.path.join(run_dir, "final.mp4")
    if not os.path.exists(path):
        raise HTTPException(404, "该 run 还没有机审通过的成片")
    return FileResponse(path, media_type="video/mp4")


@app.get("/api/runs/{run_id}/media/final_human")
def get_final_human_media(run_id: str):
    run_dir = _run_dir(run_id)
    path = os.path.join(run_dir, "final_human.mp4")
    if not os.path.exists(path):
        raise HTTPException(404, "该 run 还没有生成人工确认版成片")
    return FileResponse(path, media_type="video/mp4")


# ---------------------------------------------------------------------------
# 写接口：人工审核决定 + 人工确认版装配
# ---------------------------------------------------------------------------

class ReviewDecision(BaseModel):
    decision: str  # "approved" | "rejected"
    attempt: int | None = None  # approved 时必填：确认的是第几次尝试
    note: str = ""
    reviewer: str = ""


@app.post("/api/runs/{run_id}/shots/{shot_id}/review")
def review_shot(run_id: str, shot_id: str, body: ReviewDecision):
    run_dir = _run_dir(run_id)
    _check_id(shot_id, "shot_id")
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision 必须是 approved 或 rejected")
    if body.decision == "approved":
        if body.attempt is None:
            raise HTTPException(400, "通过时必须指定 attempt（确认的是第几次尝试）")
        video_path = os.path.join(run_dir, "shots", shot_id, f"{shot_id}_a{body.attempt}.mp4")
        if not os.path.exists(video_path):
            raise HTTPException(404, f"attempt {body.attempt} 的视频不存在")

    review_path = os.path.join(run_dir, "human_review.json")
    reviews = _read_json(review_path, {})
    reviews[shot_id] = {
        "decision": body.decision,
        "attempt": body.attempt,
        "note": body.note,
        "reviewer": body.reviewer,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(review_path, reviews)
    return reviews[shot_id]


@app.post("/api/runs/{run_id}/assemble")
def assemble_human(run_id: str):
    """人工确认版装配：人工决定优先于机审结果，未经人工表态的镜头退回机审结果。"""
    run_dir = _run_dir(run_id)
    storyboard = _read_json(os.path.join(run_dir, "storyboard.json"))
    report = _read_json(os.path.join(run_dir, "report.json"), [])
    human_review = _read_json(os.path.join(run_dir, "human_review.json"), {})
    if not storyboard:
        raise HTTPException(409, "该 run 没有 storyboard.json，无法确定镜头顺序")

    outcomes_by_shot = {o["shot_id"]: o for o in report}
    videos: list[str] = []
    used: list[dict] = []
    skipped: list[dict] = []

    for shot in storyboard["shots"]:
        shot_id = shot["shot_id"]
        human = human_review.get(shot_id)
        if human and human["decision"] == "rejected":
            skipped.append({"shot_id": shot_id, "reason": "人工打回"})
            continue
        if human and human["decision"] == "approved":
            path = os.path.join(run_dir, "shots", shot_id, f"{shot_id}_a{human['attempt']}.mp4")
            videos.append(path)
            used.append({"shot_id": shot_id, "source": "human", "attempt": human["attempt"]})
            continue
        outcome = outcomes_by_shot.get(shot_id)
        if outcome and outcome.get("passed") and outcome.get("final_video"):
            # 不直接信任 report.json 里的路径（可能相对于 cli 当时的 cwd）——
            # 按命名约定从 run_dir 重建，与人工确认分支保持一致、与 cwd 无关。
            attempt = outcome.get("attempts")
            path = os.path.join(run_dir, "shots", shot_id, f"{shot_id}_a{attempt}.mp4")
            if not os.path.exists(path):
                skipped.append({"shot_id": shot_id, "reason": f"预期视频缺失: {path}"})
                continue
            videos.append(path)
            used.append({"shot_id": shot_id, "source": "judge", "attempt": attempt})
        else:
            skipped.append({"shot_id": shot_id, "reason": "机审未通过且未经人工确认"})

    if not videos:
        raise HTTPException(409, "没有任何镜头可用于装配（既未机审通过，也未人工确认）")

    out_path = os.path.join(run_dir, "final_human.mp4")
    assemble(videos, out_path)
    return {"path": out_path, "used": used, "skipped": skipped}


# ---------------------------------------------------------------------------
# 前端页面
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


def run() -> None:
    import uvicorn
    port = int(os.environ.get("REVIEW_PORT", "8000"))
    os.makedirs(RUNS_ROOT, exist_ok=True)
    print(f"[review_app] runs 目录: {RUNS_ROOT}")
    print(f"[review_app] http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    run()
