"""
harness.adapters.mock — 免费的本地假模型。

用 ffmpeg 生成一段带文字的测试视频，让整条流水线（编译->生成->抽帧->判官->装配）
可以在不花一分钱、不配任何 API key 的情况下端到端跑通。

也用于 CI：pipeline 的回归测试永远跑 mock，真金白银只在生产环境烧。
"""
from __future__ import annotations

import hashlib
import os
import subprocess

from harness.adapters.base import VideoAdapter
from harness.models import CompiledPrompt, GenerationResult


class MockVideoAdapter(VideoAdapter):
    family = "mock"

    #: 模拟真实世界的"抽卡"失败率。attempt 越往后越容易过，
    #: 用来测试内环 retry / prompt surgeon 逻辑。
    def __init__(self, flaky: bool = True):
        self.flaky = flaky

    def generate(self, prompt: CompiledPrompt, out_dir: str, attempt: int) -> GenerationResult:
        os.makedirs(out_dir, exist_ok=True)
        w, h = (720, 1280) if prompt.aspect_ratio == "9:16" else (1280, 720)
        out_path = os.path.join(out_dir, f"{prompt.shot_id}_a{attempt}.mp4")

        # 用 prompt 哈希决定背景色，保证同 prompt 可复现
        color = "0x" + hashlib.md5(prompt.positive.encode()).hexdigest()[:6]
        label = prompt.shot_id.replace("_", " ")

        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"color=c={color}:s={w}x{h}:d={prompt.duration_s}",
            "-vf", (
                f"drawtext=text='{label} attempt {attempt}':"
                "fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2"
            ),
            "-pix_fmt", "yuv420p",
            out_path,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            return GenerationResult(
                shot_id=prompt.shot_id, attempt=attempt, ok=False,
                error=f"ffmpeg failed: {e.stderr.decode()[:500]}",
            )

        return GenerationResult(
            shot_id=prompt.shot_id,
            attempt=attempt,
            ok=True,
            video_path=out_path,
            cost_estimate_rmb=0.0,
            provider_task_id=f"mock-{prompt.shot_id}-{attempt}",
        )
