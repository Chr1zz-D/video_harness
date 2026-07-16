# video-harness

AIGC 短视频工业化流水线（Phase 1 骨架）。设计目标：**输入一份结构化 brief，
稳定输出达到验收标准的成片，人只在关键 gate 介入。**

首个品类为服饰电商广告（`ads_clothing`），架构对短剧（`short_drama`）预留了
资产锚定与模板库的扩展位。

## 架构

```
brief.json ──► StoryboardAgent (LLM, 唯一创作环节, schema 强校验+自动修复)
                    │
                    ▼
            storyboard.json ◄── 全流水线唯一事实源
                    │
   AssetStore（商品图/角色卡, must_preserve 保真点锁定）
                    │
                    ▼
            PromptCompiler（确定性程序: 分镜 × 资产 × 模板库 -> 模型专属 prompt）
                    │            templates/<品类>/<模型>/prompt.yaml + negative.yaml
                    ▼
            Orchestrator 内环（封顶 max_attempts 次）
              ┌─────────────────────────────────┐
              │ VideoAdapter.generate()          │  可灵/mock，可插拔
              │        │                         │
              │ VLMJudge（抽帧 -> rubric 逐条判）│  rubric.yaml + 资产保真点动态项
              │        │ 不通过                  │
              │ PromptSurgeon（批注 -> 改写）────┘  全过程落 run log
              └─────────────────────────────────┘
                    │ 通过
                    ▼
            assembler（ffmpeg 拼接 + AIGC 显式标识烧录）──► final.mp4
```

两层循环：
- **内环**（本仓库已实现）：单镜头级 生成→判官→外科医生→重试，硬性封顶；
- **外环**（Phase 2）：周级批任务挖 `runs/*/logs/*.jsonl`，统计哪些负面约束
  真正提升通过率，由 LLM 提议 `negative.yaml` 的 diff，**人审后合入**。
  run log 的 schema（`ShotRunLog`）已经为此准备好。

## 快速开始

```bash
pip install -e .            # 或 pip install pydantic httpx pyyaml
# 需要系统安装 ffmpeg

# 离线演示：不花一分钱、不需要任何 API key，端到端跑通全流程
PYTHONPATH=src python -m harness.cli examples/brief_clothing_ad.json \
    --storyboard examples/storyboard_clothing_ad.json --adapter mock
```

产物在 `runs/<brief_id>_<ts>/`：
- `shots/` 每镜头每次尝试的视频
- `logs/*.jsonl` 内环全量记录（prompt、结果、判官批注）——外环学习的原料
- `report.json` / `final.mp4`

真实生成：按 `config/settings.example.yaml` 配好环境变量后
`--adapter kling`，去掉 `--storyboard` 则由 LLM 现场生成分镜表。

## 审核面板 + 成本看板

跑完一条片后，`runs/<run_id>/` 里有全部产物，但逐个翻 `report.json` /
`logs/*.jsonl` / 视频文件不现实——**审核面板**把它们拼成一个网页：
每个镜头能看分镜要求、多次尝试的视频、判官逐条打分与批注，人工在这里
"通过"或"打回"；**成本看板**把 `cost.json` 里的全链路花费（视频生成含
重试 + 分镜/判官/外科医生的 LLM token）汇总成每成片秒成本。

```bash
pip install -e ".[review]"      # 额外装 fastapi + uvicorn
python -m harness.review_app
# 打开 http://127.0.0.1:8000，从左侧选一个 run
```

务必在**跑 cli 时所在的同一个目录**下启动审核面板（两者都默认用相对路径
`runs/`），否则视频路径对不上。要指定 runs 目录或端口：

```bash
REVIEW_RUNS_DIR=/abs/path/to/runs REVIEW_PORT=8080 python -m harness.review_app
```

人工审核流程：

1. 镜头卡片里选一次尝试 -> 点"通过"（记录该 attempt）或"打回"（填理由）；
   决定落盘到 `runs/<run_id>/human_review.json`，不影响机审的 `report.json`；
2. 点顶部"生成人工确认版成片"：按分镜顺序，**人工决定优先于机审结果**
   （未表态的镜头退回机审通过与否）拼出 `runs/<run_id>/final_human.mp4`；
3. 成本看板可以看到某条片是"重试烧出来的"还是"一次过"——`by_shot_rmb`
   和事件明细表能定位到具体哪个镜头/哪个阶段最费钱。

`cost.json` 的 token 单价是粗估默认值（见 `harness/cost.py` 顶部注释），
可用 `LLM_PRICE_IN_PER_1K` / `LLM_PRICE_OUT_PER_1K` /
`JUDGE_PRICE_IN_PER_1K` / `JUDGE_PRICE_OUT_PER_1K` 环境变量按实际单价覆盖；
视频成本仍以 `provider_usage` 和厂商账单为真实口径。

## 事实与合规文档（docs/，先读再写码）

- **docs/VERIFIED_API_FACTS.md** — 已按官方文档逐字段核对的可灵百炼 API
  事实（2026-07-14 核对）。涉及模型名/字段名/计费的代码改动以此为准，
  不要凭记忆写。三个关键事实：无 negative_prompt 字段（约束折叠进正文，
  2500 字符预算）；参考图只收 HTTP/HTTPS URL（本地资产先上 OSS）；
  成功响应的 `usage` 是真实计费口径。
- **docs/COMPLIANCE.md** — AIGC 双重标识义务（显式角标 + GB 45438-2025
  元数据隐式标识）、责任主体划分、广告法检查清单。当前 assembler 的
  隐式标识只是占位，**不满足国标**，Phase 2 必做。
- **docs/SPEC_multishot.md** — 可灵原生多镜头模式的接入 spec
  （短剧品类的一致性正解），含取舍分析与验收标准，可直接交 Claude Code 实现。

其余人工确认项：Seedance 2.0 为企业公测申请制且有"不对工具方开放"的
说法，申请前与火山商务确认；上量前先用 1-2 条 5s 视频实测百炼通道的
真实扣费与失败扣费规则。

## 目录

```
src/harness/
  models.py             # 全部 pydantic schema（storyboard 即 spec）
  storyboard_agent.py   # brief -> storyboard, 校验失败自动修复, 修不好升级人工
  prompt_compiler.py    # 确定性编译, 零 LLM
  judge.py              # VLM 判官: 抽帧 + rubric + 资产保真点
  prompt_surgeon.py     # 批注 -> prompt 修订（确定性补约束 + 可选 LLM 改写）
  orchestrator.py       # 内环 + run log
  assembler.py          # ffmpeg 装配 + AIGC 标识
  cost.py               # 全链路成本台账（视频重试 + LLM/判官 token 估算）
  review_app.py          # 审核面板 + 成本看板的 FastAPI 后端（只读 runs/ 产物）
  review_static/         # 审核面板前端（中文 GUI，vanilla JS，无构建步骤）
  adapters/
    base.py             # VideoAdapter 协议 + 注册表 + 轮询工具
    kling_bailian.py    # 可灵 via 阿里云百炼（异步任务: 建单->轮询->落盘）
    mock.py             # 本地 ffmpeg 假模型, CI 与演示用
templates/ads_clothing/
  kling/prompt.yaml     # 正向模板 + 运镜词表
  kling/negative.yaml   # 负面约束库（每条带 reason, 版本化的公司资产）
  rubric.yaml           # 判官验收标准（hard_fail 一票否决项）
```

## Phase 2 待办

- [ ] 图先行：文生图关键帧 -> 人审/机审 -> 图生视频（把废片消灭在几分钱的图像阶段）
- [ ] 外环学习任务（挖 run log -> 提议模板 diff -> 人审合入）
- [ ] 剪映草稿输出（pyJianYingDraft 路线），替代 ffmpeg 直拼，人工终审可微调
- [ ] TTS 层（火山/MiniMax/ElevenLabs 适配器，接 Shot.dialogue）
- [x] FastAPI review 面板（看图/片 + 通过/打回）—— 见上文「审核面板 + 成本看板」，
      中文 GUI，`harness/review_app.py` + `harness/review_static/`
- [x] 成本看板：每成片秒全链路成本（含重试与 LLM token）—— `harness/cost.py`
      的 `CostTracker`，落盘 `runs/<run_id>/cost.json`，审核面板里可视化
```
