"""
harness.adapters.kling_bailian — 可灵 v3（Kling），阿里云百炼（DashScope）通道。

本文件已按官方文档逐字段核对（阿里云帮助中心《可灵-视频生成API文档》，
文档更新于 2026-05-28，核对于 2026-07-14）。与最初凭经验假设的写法相比，
真实 API 有几个关键差异，详见 docs/VERIFIED_API_FACTS.md。核心结论：

  1. 模型名是 "kling/kling-v3-video-generation"（基础）和
     "kling/kling-v3-omni-video-generation"（全能参考版），不是 "kling-video"；
  2. **没有 negative_prompt 字段**——负面约束必须折叠进 prompt 正文
     （prompt 上限 2500 字符，超出自动截断）；
  3. 参考图走 input.media 数组（type: first_frame/last_frame/refer），
     **只接受 HTTP/HTTPS URL，不支持 base64 data URL**——本地资产必须先
     上传 OSS 拿到公网 URL；
  4. omni 模型支持 refer 参考图 + prompt 内 <<<image_1>>> 引用语法，
     这正是我们资产锚定层需要的图像条件化通道；
  5. 图生视频场景 aspect_ratio 不生效（以首帧为准），文生视频/参考生视频必填；
  6. watermark=true 会在右下角打"可灵AI"水印（响应同时返回
     video_url 和 watermark_video_url，链接 30 天有效，必须及时下载）；
  7. 成功响应带 usage（duration/size/fps）——这是成本对账的真实口径，
     必须落库，cost_estimate 只是看板粗估。

仅"中国内地（北京）"地域可用，API Key 必须属于该地域。
环境变量：
  DASHSCOPE_API_KEY   必填
  KLING_MODE_STD      设为 "1" 时 draft 用 std(720P)，默认 draft/final 都用 pro(1080P)
"""
from __future__ import annotations

import os
from typing import Callable

import httpx

from harness.adapters.base import VideoAdapter
from harness.models import CompiledPrompt, GenerationResult

CREATE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

MODEL_BASE = "kling/kling-v3-video-generation"
MODEL_OMNI = "kling/kling-v3-omni-video-generation"

PROMPT_CHAR_LIMIT = 2500  # 官方上限，超出自动截断——宁可我们主动裁负面约束

# 粗估单价（元/秒，pro 档），仅供成本看板；真实成本以 usage + 后台账单为准
ROUGH_PRICE_RMB_PER_S = {"draft": 0.8, "final": 1.2}


class AssetNotUploadedError(RuntimeError):
    """参考图还是本地路径。百炼可灵只收 HTTP/HTTPS URL，先上传 OSS。"""


class KlingBailianAdapter(VideoAdapter):
    family = "kling"

    def __init__(self, url_resolver: Callable[[str], str] | None = None) -> None:
        """
        url_resolver: 把本地资产路径换成公网 URL 的回调（OSS 上传器）。
        Phase 1 没接 OSS 时保持 None——遇到本地路径会抛清晰错误而不是静默失败。
        """
        self.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("缺少 DASHSCOPE_API_KEY 环境变量（北京地域）")
        self.url_resolver = url_resolver
        self.draft_std = os.environ.get("KLING_MODE_STD", "") == "1"

    # ------------------------------------------------------------------
    def generate(self, prompt: CompiledPrompt, out_dir: str, attempt: int) -> GenerationResult:
        os.makedirs(out_dir, exist_ok=True)
        try:
            payload = self._build_payload(prompt)
        except AssetNotUploadedError as e:
            return GenerationResult(shot_id=prompt.shot_id, attempt=attempt, ok=False, error=str(e))

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",  # 必填，缺失会报不支持同步调用
        }

        try:
            with httpx.Client(timeout=60) as client:
                r = client.post(CREATE_URL, headers=headers, json=payload)
                r.raise_for_status()
                body = r.json()
                if "code" in body:  # 创建失败的异常响应
                    raise RuntimeError(f"{body.get('code')}: {body.get('message')}")
                task_id = body["output"]["task_id"]  # 有效期 24h，勿重复建单

                def check() -> tuple[bool, dict]:
                    resp = client.get(
                        TASK_URL.format(task_id=task_id),
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    ).json()
                    status = resp.get("output", {}).get("task_status", "")
                    if status == "SUCCEEDED":
                        return True, resp
                    if status in ("FAILED", "CANCELED", "UNKNOWN"):
                        out = resp.get("output", {})
                        raise RuntimeError(
                            f"任务{status}: {out.get('code', '')} {out.get('message', '')}"
                        )
                    return False, resp  # PENDING / RUNNING

                result = self.poll_until(check, interval_s=15, timeout_s=900)
                output = result["output"]
                usage = result.get("usage", {})

                # 无水印链接 30 天有效——立即落盘，不做长期存储依赖
                video_url = output["video_url"]
                out_path = os.path.join(out_dir, f"{prompt.shot_id}_a{attempt}.mp4")
                with client.stream("GET", video_url) as resp:
                    resp.raise_for_status()
                    with open(out_path, "wb") as f:
                        for chunk in resp.iter_bytes():
                            f.write(chunk)

        except Exception as e:  # noqa: BLE001 — 生成失败是常态，转 ok=False 交给内环
            return GenerationResult(
                shot_id=prompt.shot_id, attempt=attempt, ok=False, error=str(e)[:800]
            )

        return GenerationResult(
            shot_id=prompt.shot_id,
            attempt=attempt,
            ok=True,
            video_path=out_path,
            provider_task_id=task_id,
            cost_estimate_rmb=ROUGH_PRICE_RMB_PER_S[prompt.mode] * prompt.duration_s,
            provider_usage=usage,  # 真实计费口径（duration/size/fps），对账用这个
        )

    # ------------------------------------------------------------------
    def _build_payload(self, prompt: CompiledPrompt) -> dict:
        has_refs = bool(prompt.reference_image_paths)
        model = MODEL_OMNI if has_refs else MODEL_BASE

        # 负面约束折叠进正文（本 API 无 negative_prompt 字段）
        text = prompt.positive
        if prompt.negative:
            text = f"{text}。画面中严禁出现：{prompt.negative}"

        inp: dict = {}
        if has_refs:
            media, bindings = [], []
            for i, path in enumerate(prompt.reference_image_paths, start=1):
                media.append({"type": "refer", "url": self._to_public_url(path)})
                bindings.append(f"<<<image_{i}>>>")
            # omni 引用语法：media 顺序即 <<<image_N>>> 顺序
            text = f"以参考图{('、'.join(bindings))}中的商品/人物为准，保持完全一致。{text}"
            inp["media"] = media

        inp["prompt"] = text[:PROMPT_CHAR_LIMIT]

        params: dict = {
            "mode": "std" if (prompt.mode == "draft" and self.draft_std) else "pro",
            "duration": max(3, min(prompt.duration_s, 15)),  # 官方取值 [3,15]
            "audio": False,   # 音频显著加价，Phase 1 音轨走独立 TTS/配乐层
            "watermark": False,  # 显式标识由装配层统一处理（见 assembler + docs/COMPLIANCE.md）
        }
        # 图生视频以首帧为准无需 aspect_ratio；文生视频/参考生视频必填
        params["aspect_ratio"] = prompt.aspect_ratio

        return {"model": model, "input": inp, "parameters": params}

    def _to_public_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        if self.url_resolver:
            return self.url_resolver(path)
        raise AssetNotUploadedError(
            f"参考图 '{path}' 是本地路径。百炼可灵的 media.url 只接受 HTTP/HTTPS，"
            "请先接 OSS 上传（给 KlingBailianAdapter 传 url_resolver 回调），"
            "或在 brief 里直接填公网 URL。"
        )
