"""
harness.judge — VLM 判官（verify_identity 的视频版）。

流程：ffmpeg 抽帧 -> 便宜 VLM 按 rubric 逐条判定 -> Verdict(JSON)。
判官用便宜的多模态模型即可（Qwen-VL 级别），不要用旗舰模型烧钱看帧。

rubric 来源两层：
  1. templates/<category>/rubric.yaml 的品类通用项；
  2. 该镜头引用资产的 must_preserve 保真点，动态生成 hard_fail 项。

环境变量（OpenAI-compatible 视觉端点，如百炼 compatible-mode 上的 qwen-vl-plus）：
  JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL

没有配 key 时自动退化为 MockJudge（attempt>=2 放行），保证离线可跑。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile

import httpx
import yaml

from harness.cost import JUDGE_PRICE_IN, JUDGE_PRICE_OUT, CostTracker
from harness.models import AssetStore, RubricItem, Shot, Verdict

JUDGE_SYSTEM = """你是短视频质检员。根据抽帧图片，逐条回答检查项，只输出 JSON：
{"items": {"<key>": true/false, ...},
 "critique": "不通过项的具体画面问题，指出发生在第几张帧、什么现象",
 "suggested_negative_additions": ["可加入负面约束库的短语", ...]}
判定从严：拿不准就判 false。critique 必须具体到画面，供后续自动改写 prompt 使用。"""


def load_rubric(templates_root: str, category: str) -> tuple[float, list[RubricItem]]:
    path = os.path.join(templates_root, category, "rubric.yaml")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    items = [RubricItem(**it) for it in data["items"]]
    return float(data["pass_threshold"]), items


def rubric_for_shot(
    base_items: list[RubricItem], shot: Shot, assets: AssetStore
) -> list[RubricItem]:
    """品类通用项 + 该镜头资产的保真点动态项。"""
    items = list(base_items)
    for ref in shot.asset_refs:
        a = assets.get(ref)
        for i, point in enumerate(a.must_preserve):
            items.append(
                RubricItem(
                    key=f"preserve_{a.asset_id}_{i}",
                    question=f"资产'{a.description}'的保真点是否完整保持：{point}？",
                    weight=5,
                    hard_fail=True,
                )
            )
    return items


class VLMJudge:
    def __init__(
        self,
        templates_root: str,
        category: str,
        frames: int = 4,
        cost_tracker: CostTracker | None = None,
    ):
        self.pass_threshold, self.base_items = load_rubric(templates_root, category)
        self.frames = frames
        self.base_url = os.environ.get("JUDGE_BASE_URL", "")
        self.api_key = os.environ.get("JUDGE_API_KEY", "")
        self.model = os.environ.get("JUDGE_MODEL", "qwen-vl-plus")
        self.cost_tracker = cost_tracker

    # ------------------------------------------------------------------
    def judge(self, shot: Shot, assets: AssetStore, video_path: str, attempt: int) -> Verdict:
        items = rubric_for_shot(self.base_items, shot, assets)

        if not self.base_url:  # 离线演示模式
            return self._mock_verdict(shot, items, attempt)

        frame_paths = extract_frames(video_path, self.frames)
        user_content: list[dict] = [
            {"type": "text", "text": self._task_text(shot, items)}
        ] + [
            {
                "type": "image_url",
                "image_url": {"url": _b64_data_url(p)},
            }
            for p in frame_paths
        ]

        with httpx.Client(timeout=120) as client:
            r = client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": JUDGE_SYSTEM},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0,
                },
            )
            r.raise_for_status()
            body = r.json()
            raw = body["choices"][0]["message"]["content"]
            if self.cost_tracker:
                self.cost_tracker.log_llm_usage(
                    "judge", body.get("usage", {}), JUDGE_PRICE_IN, JUDGE_PRICE_OUT,
                    shot_id=shot.shot_id, model=self.model,
                )

        data = json.loads(_strip_fences(raw))
        return self._score(shot, items, attempt, data)

    # ------------------------------------------------------------------
    def _task_text(self, shot: Shot, items: list[RubricItem]) -> str:
        checks = "\n".join(f"- {it.key}: {it.question}" for it in items)
        return (
            f"分镜要求：场景「{shot.scene}」，主体动作「{shot.subject}」，"
            f"情绪「{shot.emotion}」，运镜「{shot.camera.value} {shot.camera_note}」。\n"
            f"以下是从该镜头视频中等间隔抽取的 {self.frames} 张帧。逐条检查：\n{checks}"
        )

    def _score(self, shot: Shot, items: list[RubricItem], attempt: int, data: dict) -> Verdict:
        results: dict[str, bool] = {it.key: bool(data.get("items", {}).get(it.key, False)) for it in items}
        total_w = sum(it.weight for it in items)
        score = 100.0 * sum(it.weight for it in items if results[it.key]) / total_w
        hard_failed = any(it.hard_fail and not results[it.key] for it in items)
        return Verdict(
            shot_id=shot.shot_id,
            attempt=attempt,
            passed=(score >= self.pass_threshold) and not hard_failed,
            score=round(score, 1),
            item_results=results,
            critique=str(data.get("critique", "")),
            suggested_negative_additions=list(data.get("suggested_negative_additions", [])),
        )

    def _mock_verdict(self, shot: Shot, items: list[RubricItem], attempt: int) -> Verdict:
        """无 API key 的演示判官：第 1 次判挂（触发内环），第 2 次起放行。"""
        passed = attempt >= 2
        results = {it.key: passed or not it.hard_fail for it in items}
        return Verdict(
            shot_id=shot.shot_id,
            attempt=attempt,
            passed=passed,
            score=88.0 if passed else 40.0,
            item_results=results,
            critique="" if passed else "[mock] 第2帧手部出现六指，第3帧衣服颜色由米色漂移为浅灰",
            suggested_negative_additions=[] if passed else ["六指", "服装颜色漂移"],
        )


# ---------------------------------------------------------------------------
def extract_frames(video_path: str, n: int) -> list[str]:
    tmpdir = tempfile.mkdtemp(prefix="frames_")
    pattern = os.path.join(tmpdir, "f_%02d.jpg")
    # 用 thumbnail-ish 等间隔抽帧：先探测时长再均匀取样
    dur = _probe_duration(video_path)
    fps = max(n / max(dur, 0.1), 0.1)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
         "-vf", f"fps={fps}", "-frames:v", str(n), pattern],
        check=True, capture_output=True,
    )
    return sorted(
        os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.endswith(".jpg")
    )


def _probe_duration(video_path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out or 0)


def _b64_data_url(path: str) -> str:
    with open(path, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
