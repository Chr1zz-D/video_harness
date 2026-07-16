"""
harness.assembler — 成片装配（Phase 1 极简版）。

ffmpeg 顺序拼接通过质检的镜头，并**默认烧录 AIGC 显式标识**——
《人工智能生成合成内容标识办法》（2025-09-01 起施行）要求商用交付
打显式标识；隐式元数据标识在这里以 metadata comment 占位，
上生产前请按目标平台（抖音/天猫）的具体规范核对格式。

Phase 2 再替换为剪映草稿文件生成（pyJianYingDraft 路线）或 Remotion，
让人工终审能在剪映里直接微调。
"""
from __future__ import annotations

import os
import subprocess

AIGC_LABEL = "AI生成"


def assemble(video_paths: list[str], out_path: str, burn_label: bool = True) -> str:
    if not video_paths:
        raise ValueError("没有可装配的镜头")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    list_file = out_path + ".txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in video_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    vf = (
        f"drawtext=text='{AIGC_LABEL}':fontcolor=white@0.75:fontsize=28:"
        "box=1:boxcolor=black@0.35:boxborderw=8:x=w-text_w-24:y=24"
    ) if burn_label else "null"

    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-vf", vf,
            "-metadata", "comment=AIGC:generated;pipeline=video-harness",
            "-pix_fmt", "yuv420p",
            out_path,
        ],
        check=True, capture_output=True,
    )
    os.remove(list_file)
    return out_path
