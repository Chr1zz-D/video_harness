# SPEC — 原生多镜头模式（multi_shot）接入

> 状态：待实现（Phase 2）。本 spec 供 Claude Code 实现用，API 事实以
> docs/VERIFIED_API_FACTS.md 为准。

## 背景

百炼可灵 v3 原生支持一次调用生成多镜头视频：
`input.multi_shot=true, shot_type="customize", multi_prompt=[{index,prompt,duration}...]`，
1~6 段，每段 prompt ≤512 字符，段时长 ∈ [1, parameters.duration]。

这与我们现有的"逐镜头生成 + 逐镜头质检内环"是两种不同的生成策略：

| | 逐镜头（现状） | 原生多镜头（本 spec） |
|---|---|---|
| 跨镜头一致性 | 靠参考图/首尾帧串联，工程侧负担 | 模型内部保证，**质量上限更高** |
| 重试粒度 | 单镜头，废片成本低 | 整段重生成，**一段崩全段重来** |
| 判官粒度 | 每镜头独立 verdict | 需按镜头切分后分段判 |
| prompt 预算 | 2500 字符/镜头 | 512 字符/段（**负面约束折叠空间骤减**） |
| 适用品类 | 电商广告（镜头间弱耦合） | 短剧（角色/场景强连续性） |

## 需求

1. `CompiledPrompt` 增加可选 `segment_prompts: list[SegmentPrompt]`
   （SegmentPrompt: index/prompt/duration）。当存在时适配器走 multi_shot 通道。
2. `PromptCompiler` 新增 `compile_sequence(shots: list[Shot], ...)`：
   把 ≤6 个连续镜头编成一个 multi_shot 请求；每段 512 字符预算内
   **只保留该镜头 hard_fail 相关的负面约束**（按 negative.yaml 中的
   约束排序裁剪，排序依据 Phase 2 外环产出的通过率统计，暂用文件顺序）。
3. `Orchestrator` 新增 sequence 模式：
   - 判官对整段视频先做**镜头边界切分**（按 multi_prompt 的 duration
     累加时间戳用 ffmpeg 切段），逐段跑现有 `VLMJudge.judge`；
   - 任一段 hard_fail → 整段进入重试（把失败段的批注合并给外科医生，
     修订对应 segment 的 prompt）；重试封顶沿用 max_attempts；
   - 全段通过率与成本记入同一套 run log（ShotRunLog 增加 sequence_id）。
4. 品类路由：`short_drama` 默认 sequence 模式（每 4-6 镜头一组），
   `ads_*` 默认逐镜头模式。在品类模板目录加 `pipeline.yaml` 声明。

## 验收标准

- mock 适配器支持 multi_shot（拼接多段不同颜色测试片），CI 全绿；
- 用 examples/ 新增的短剧 brief 跑通 sequence 模式端到端；
- run log 里能区分 sequence 重试与单镜头重试，成本台账口径不混。

## 未决问题（实现前实测确认）

- multi_prompt 各段能否携带独立 media 参考（文档示例未覆盖）；
  若不能，短剧角色一致性优先评估"可灵主体ID（element_list）"注册通道。
