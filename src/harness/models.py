"""
harness.models — 全流水线的唯一事实源（single source of truth）。

设计原则（与 reportOS grounding 同构）：
  1. LLM 只在 brief -> Storyboard 这一步发挥创造力，输出必须通过 schema 校验；
  2. 后续所有环节（编译、生成、判官、装配）都是确定性程序对这些模型的消费；
  3. 资产（商品图/角色卡）在 session 开始时锁定，生成阶段只能引用，不能再描述。
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# 资产层（AssetStore ≈ FactStore）
# ---------------------------------------------------------------------------

class AssetKind(str, Enum):
    product = "product"        # 商品实拍图（电商广告的保真锚点）
    character = "character"    # 角色三视图/定妆照（短剧一致性锚点）
    style_ref = "style_ref"    # 风格参考帧
    logo = "logo"


class Asset(BaseModel):
    """一个被锁定的参考资产。生成阶段只允许以 asset_id 引用。"""
    asset_id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    kind: AssetKind
    path: str = Field(description="本地路径或 OSS URL")
    description: str = Field(description="给 LLM 看的简述，如 '女款米色风衣，双排扣，左胸有品牌刺绣'")
    must_preserve: list[str] = Field(
        default_factory=list,
        description="判官必须逐条核对的保真点，如 ['双排扣', '左胸刺绣logo', '米色']",
    )


class AssetStore(BaseModel):
    assets: dict[str, Asset] = Field(default_factory=dict)

    def get(self, asset_id: str) -> Asset:
        if asset_id not in self.assets:
            raise KeyError(f"未锁定的资产引用: {asset_id}（所有资产必须在 brief 阶段登记）")
        return self.assets[asset_id]

    @classmethod
    def from_list(cls, assets: list[Asset]) -> "AssetStore":
        return cls(assets={a.asset_id: a for a in assets})


# ---------------------------------------------------------------------------
# Brief（客户输入）
# ---------------------------------------------------------------------------

class Category(str, Enum):
    ads_clothing = "ads_clothing"      # 服饰电商广告
    ads_generic = "ads_generic"        # 通用商品广告
    short_drama = "short_drama"        # 短剧


class Brief(BaseModel):
    brief_id: str
    category: Category
    title: str
    target_platform: Literal["douyin", "tmall", "kuaishou", "other"] = "douyin"
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    total_duration_s: int = Field(ge=5, le=180)
    tone: str = Field(description="整体调性，如 '轻奢、都市、秋冬氛围'")
    key_message: str = Field(description="必须传达的核心信息/卖点")
    assets: list[Asset] = Field(default_factory=list)
    hard_constraints: list[str] = Field(
        default_factory=list,
        description="客户红线，如 '不得出现竞品'、'模特必须亚洲面孔'",
    )
    language: str = "zh"


# ---------------------------------------------------------------------------
# 分镜表（LLM 的唯一输出物，schema 强制补全维度）
# ---------------------------------------------------------------------------

class CameraMove(str, Enum):
    static = "static"
    push_in = "push_in"
    pull_out = "pull_out"
    pan = "pan"
    tilt = "tilt"
    tracking = "tracking"
    handheld = "handheld"


class Shot(BaseModel):
    shot_id: str = Field(pattern=r"^shot_\d{2}$")
    duration_s: int = Field(ge=2, le=15, description="国产模型单段以 5s/10s 为宜")
    scene: str = Field(description="场景与环境，含光线/时间/天气")
    subject: str = Field(description="画面主体与动作，具体到肢体动作层面")
    emotion: str = Field(description="人物情绪与表现方式——必填，这是 AI 最容易丢的维度")
    camera: CameraMove
    camera_note: str = Field(default="", description="景别与运镜补充，如 '中景起幅推至特写'")
    asset_refs: list[str] = Field(
        default_factory=list,
        description="本镜头引用的 asset_id 列表；引用了商品/角色的镜头会走图像条件化生成",
    )
    dialogue: Optional[str] = Field(default=None, description="台词/旁白文本，交给 TTS")
    bgm_cue: str = Field(default="", description="音乐情绪提示，如 '弦乐渐强'")
    on_screen_text: Optional[str] = Field(default=None, description="画面字幕/贴片文案")
    extra_negative: list[str] = Field(
        default_factory=list,
        description="本镜头特有的负面约束（品类通用负面约束在模板库里，不在这里重复）",
    )

    @field_validator("emotion")
    @classmethod
    def emotion_must_be_concrete(cls, v: str) -> str:
        if len(v.strip()) < 4:
            raise ValueError("emotion 必须具体（如 '压抑的委屈，眼眶泛红但强忍'），不接受 '开心' 这种一词描述")
        return v


class Storyboard(BaseModel):
    brief_id: str
    version: int = 1
    shots: list[Shot]

    @field_validator("shots")
    @classmethod
    def at_least_one_shot(cls, v: list[Shot]) -> list[Shot]:
        if not v:
            raise ValueError("分镜表不能为空")
        return v

    def total_duration(self) -> int:
        return sum(s.duration_s for s in self.shots)


# ---------------------------------------------------------------------------
# 生成与质检的中间产物
# ---------------------------------------------------------------------------

class CompiledPrompt(BaseModel):
    """prompt 编译器的输出：某个 shot 在某个模型上的完整生成参数。"""
    shot_id: str
    model_family: str                      # e.g. "kling", "seedance", "mock"
    positive: str
    negative: str
    reference_image_paths: list[str] = Field(default_factory=list)
    duration_s: int
    aspect_ratio: str
    mode: Literal["draft", "final"] = "draft"


class GenerationResult(BaseModel):
    shot_id: str
    attempt: int
    ok: bool
    video_path: Optional[str] = None
    keyframe_paths: list[str] = Field(default_factory=list)
    provider_task_id: Optional[str] = None
    cost_estimate_rmb: float = 0.0
    provider_usage: dict = Field(
        default_factory=dict,
        description="厂商返回的真实计费口径（如可灵 usage: duration/size/fps），对账以此为准",
    )
    error: Optional[str] = None


class RubricItem(BaseModel):
    key: str
    question: str          # 判官逐条回答的问题
    weight: float = 1.0
    hard_fail: bool = False  # 该项不过直接判死（如商品 logo 变形）


class Verdict(BaseModel):
    shot_id: str
    attempt: int
    passed: bool
    score: float = Field(ge=0, le=100)
    item_results: dict[str, bool] = Field(default_factory=dict)
    critique: str = Field(description="给 prompt 外科医生看的失败原因，要具体到画面")
    suggested_negative_additions: list[str] = Field(default_factory=list)


class ShotRunLog(BaseModel):
    """内环每一次尝试的完整记录——外环学习吃的就是这个。"""
    shot_id: str
    attempt: int
    prompt: CompiledPrompt
    generation: GenerationResult
    verdict: Optional[Verdict] = None
