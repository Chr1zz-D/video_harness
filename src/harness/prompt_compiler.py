"""
harness.prompt_compiler — 分镜表 × 资产 × (品类,模型)模板库 -> 模型专属 prompt。

这是一个**确定性程序**，没有任何 LLM 调用。提示词工程的知识全部沉淀在
templates/<category>/<model_family>/ 下的 YAML 里，是版本化的公司资产：
  prompt.yaml    正向模板（含运镜词表映射）
  negative.yaml  负面约束库（品类通用 + 逐条带 reason 注释）

翻车一次 -> negative.yaml 加一条 -> 全品类所有后续生成受益。
这就是"弱化对人写强提示词依赖"的落点。
"""
from __future__ import annotations

import json
import os

import yaml

from harness.models import AssetStore, CompiledPrompt, Shot, Storyboard


#: 负面约束折叠进正文后允许占用的字符预算（可灵 prompt 上限 2500，超出静默截断，
#: 约束太长会挤掉正向描述——所以裁剪是必须的，裁剪顺序由外环学到的疗效决定）
NEGATIVE_CHAR_BUDGET = 400


class PromptCompiler:
    def __init__(
        self,
        templates_root: str,
        category: str,
        model_family: str,
        learned_stats_path: str = "learned/constraint_stats.json",
    ):
        base = os.path.join(templates_root, category, model_family)
        with open(os.path.join(base, "prompt.yaml"), encoding="utf-8") as f:
            self.tpl = yaml.safe_load(f)
        with open(os.path.join(base, "negative.yaml"), encoding="utf-8") as f:
            neg = yaml.safe_load(f)
        self.base_negative: list[str] = [item["term"] for item in neg["constraints"]]
        self.model_family = model_family
        self.learned = self._load_learned(learned_stats_path)
        if self.learned:
            # 外环疗效排序：flip_rate 高且证据足的排前，预算裁剪时最后被裁；
            # 库文件自身顺序作为无数据时的先验（越靠前越重要）
            prior = {t: -i for i, t in enumerate(self.base_negative)}
            self.base_negative.sort(
                key=lambda t: (-self._weight(t), -prior.get(t, -999))
            )

    @staticmethod
    def _load_learned(path: str) -> dict:
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _weight(self, term: str) -> float:
        s = self.learned.get(term)
        if not s or s.get("added", 0) < 2:  # 证据不足只观察，不参与排序
            return 0.0
        return float(s.get("flip_rate", 0.0))

    # ------------------------------------------------------------------
    def compile_shot(
        self,
        shot: Shot,
        assets: AssetStore,
        aspect_ratio: str,
        mode: str = "draft",
        extra_negative: list[str] | None = None,
    ) -> CompiledPrompt:
        camera_phrase = self.tpl["camera_map"].get(shot.camera.value, "")

        # 引用的资产：保真点写进正向 prompt，参考图走图像条件化
        asset_lines, ref_images = [], []
        for ref in shot.asset_refs:
            a = assets.get(ref)
            asset_lines.append(a.description)
            if a.must_preserve:
                asset_lines.append("必须完全保持：" + "、".join(a.must_preserve))
            ref_images.append(a.path)

        positive = self.tpl["positive_template"].format(
            scene=shot.scene,
            subject=shot.subject,
            emotion=shot.emotion,
            camera=camera_phrase,
            camera_note=shot.camera_note,
            assets="。".join(asset_lines),
            style=self.tpl.get("style_suffix", ""),
        )
        positive = " ".join(positive.split())  # 压掉模板留下的多余空白

        # 镜头专属约束（分镜标注的 + 内环外科医生本轮追加的）永远保留——
        # 它们针对眼前的废片；库约束按外环疗效排序后填满剩余预算，装不下的裁掉。
        shot_terms = list(dict.fromkeys(shot.extra_negative + (extra_negative or [])))
        budget = NEGATIVE_CHAR_BUDGET - sum(len(t) + 2 for t in shot_terms)
        lib_terms: list[str] = []
        for t in self.base_negative:
            if t in shot_terms:
                continue
            if budget - (len(t) + 2) < 0:
                break  # 排序保证被裁的是疗效证据最弱的
            lib_terms.append(t)
            budget -= len(t) + 2
        negative_terms = shot_terms + lib_terms

        return CompiledPrompt(
            shot_id=shot.shot_id,
            model_family=self.model_family,
            positive=positive,
            negative=", ".join(negative_terms),
            reference_image_paths=ref_images,
            duration_s=shot.duration_s,
            aspect_ratio=aspect_ratio,
            mode=mode,  # type: ignore[arg-type]
        )

    def compile_all(
        self, sb: Storyboard, assets: AssetStore, aspect_ratio: str, mode: str = "draft"
    ) -> list[CompiledPrompt]:
        return [self.compile_shot(s, assets, aspect_ratio, mode) for s in sb.shots]
