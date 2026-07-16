"""
harness.adapters.base — 模型适配层。

所有图像/视频模型（可灵、Seedance、海螺、fal 上的海外模型…）都实现同一个协议。
上层 orchestrator 只认识 CompiledPrompt -> GenerationResult，永远不直接碰任何厂商 SDK。

国产视频 API 的共同形态是异步任务制：
    submit() 拿 task_id  ->  poll() 轮询  ->  download() 落盘
适配器内部自己处理这三步，对外只暴露一个阻塞的 generate()。
"""
from __future__ import annotations

import abc
import time
from typing import Callable

from harness.models import CompiledPrompt, GenerationResult


class VideoAdapter(abc.ABC):
    """视频生成适配器协议。"""

    #: 适配器家族名，与 CompiledPrompt.model_family / 模板库目录名对应
    family: str = "base"

    @abc.abstractmethod
    def generate(self, prompt: CompiledPrompt, out_dir: str, attempt: int) -> GenerationResult:
        """阻塞式生成一段视频。失败时返回 ok=False 而不是抛异常（除非是配置错误）。"""

    # ------------------------------------------------------------------
    # 通用轮询工具，子类的 generate() 可以复用
    # ------------------------------------------------------------------
    @staticmethod
    def poll_until(
        check: Callable[[], tuple[bool, dict]],
        interval_s: float = 15.0,
        timeout_s: float = 600.0,
    ) -> dict:
        """
        check() 返回 (done, payload)。视频任务通常 1-5 分钟，建议 15s 轮询间隔。
        超时抛 TimeoutError，由 generate() 捕获转成 ok=False。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            done, payload = check()
            if done:
                return payload
            time.sleep(interval_s)
        raise TimeoutError(f"轮询超时（{timeout_s}s）")


class AdapterRegistry:
    _adapters: dict[str, VideoAdapter] = {}

    @classmethod
    def register(cls, adapter: VideoAdapter) -> None:
        cls._adapters[adapter.family] = adapter

    @classmethod
    def get(cls, family: str) -> VideoAdapter:
        if family not in cls._adapters:
            raise KeyError(
                f"未注册的模型适配器: {family}。已注册: {list(cls._adapters)}"
            )
        return cls._adapters[family]
