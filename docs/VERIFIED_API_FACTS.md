# VERIFIED_API_FACTS — 已核对的外部 API 事实

> 核对日期：2026-07-14。来源：阿里云帮助中心《可灵-视频生成API文档》
> （help.aliyun.com/zh/model-studio/kling-video-generation-api-reference/，
> 文档更新时间 2026-05-28）及公开报道。
> **本文件是给 Claude Code 当事实依据用的：涉及以下内容时以本文件为准，
> 不要凭训练记忆写模型名、字段名和计费假设。** 每季度重新核对一次。

## 1. 可灵 v3 @ 阿里云百炼（已按官方文档逐字段核对）

| 事实 | 值 |
|---|---|
| 模型名 | `kling/kling-v3-video-generation`（基础）、`kling/kling-v3-omni-video-generation`（全能参考） |
| 地域 | 仅"中国内地（北京）"，API Key 必须同地域 |
| 建单 | `POST https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis`，请求头必须带 `X-DashScope-Async: enable` |
| 轮询 | `GET https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}`，建议 15s 间隔，查询接口 RPS 上限 20，task_id 有效期 24h |
| 状态机 | PENDING → RUNNING → SUCCEEDED / FAILED / CANCELED / UNKNOWN |
| prompt | ≤2500 字符，超出**自动截断**（不报错，静默截）；**没有 negative_prompt 字段** |
| 参考素材 | `input.media[]`，type: `first_frame` / `last_frame` / `refer`（仅omni） / `base`（仅omni） / `feature`（仅omni）；**url 只接受 HTTP/HTTPS，不支持 base64** |
| 图片限制 | JPEG/JPG/PNG（不支持透明通道），宽高 [300,8000]px，宽高比 1:2.5~2.5:1，≤10MB |
| omni 引用语法 | prompt 内用 `<<<image_1>>>` `<<<video_1>>>` `<<<element_1>>>` 引用素材，顺序 = media 数组顺序；refer 图 + element 主体合计 ≤7 |
| 多镜头 | `multi_shot: true` + `shot_type: intelligence/customize`；customize 模式 `multi_prompt[]` 1~6 段，每段 ≤512 字符，段时长 ∈ [1, parameters.duration] |
| mode | `pro`（默认，1080P）/ `std`（720P） |
| duration | 整数 [3,15]，默认 5；**直接影响费用** |
| audio | bool，开启原生音频**显著加价**（外部报道口径：v2.6 时代 5s pro 无声 2.5 元 vs 有声 5 元，翻倍量级） |
| watermark | `true` 时右下角固定"可灵AI"水印；响应同时返回 `video_url`（无水印）与 `watermark_video_url` |
| aspect_ratio | 16:9 / 9:16 / 1:1；**图生视频不生效**（以首帧为准），文生视频/参考生视频必填 |
| 视频链接 | 30 天有效，官方明确"不建议作为长期存储依赖，请及时下载" |
| 计费口径 | 成功响应的 `usage`（duration/size/fps/audio/SR）是真实口径；粗估 pro 档 0.6~1.2 元/秒（券商研报口径），对账以后台账单为准 |
| 失败扣费 | 可灵**官方开放平台**文档称失败任务（含审核不过）不扣积分；**百炼通道的失败扣费规则未在本文档确认**，上量前用 1-2 条实测 |

### 对我们架构的三个直接影响

1. **负面约束库的落地方式变了**：没有 negative_prompt 字段，编译产物的
   `negative` 由适配器折叠进正文（"画面中严禁出现：…"）。2500 字符预算里
   正文和约束抢空间——外环学习任务未来要做的不是无限加约束，而是
   **按历史通过率给约束排序，裁剪保留 top-N**。
2. **资产必须有公网 URL**：本地商品图先上传 OSS。适配器留了
   `url_resolver` 回调位，Phase 2 接 OSS SDK（阿里云 oss2，上传后
   生成带签名的临时 URL）。
3. **原生多镜头是架构级机会**：customize 模式一次调用生成 ≤6 段连续镜头，
   跨镜头一致性由模型内部保证——见 docs/SPEC_multishot.md 的取舍分析。

## 2. 其他厂商（截至 2026-07，二手信源，接入前需再核对）

- **Seedance 2.0（火山引擎）**：质量榜第一；企业 API 公测**申请制**，
  纯生成 46 元/百万 tokens（≈15s/720p ≈ 14-15 元，"1秒1元"），含视频输入
  28 元；有报道称"暂不对工具方开放，仅限自用"——以公司名义申请前
  必须与火山商务确认使用范围。token 公式：宽×高×帧率×时长×条数。
- **Sora 2**：约 30 积分/10s，无免费层；**Veo 3.1**：企业向，走
  Google Cloud/Vertex，单价约为可灵 10 倍——均不适合本项目主力位。
- **判官 VLM**：百炼 compatible-mode 上的 qwen-vl 系列即可，与可灵
  共用同一个 DASHSCOPE_API_KEY，一个账号两种角色。

## 3. 已知未决问题（下次核对清单）

- [ ] 百炼可灵的**逐档价格表**（控制台可见，文档未列）——上量前抄录进本文件
- [ ] 百炼通道失败任务是否扣费（实测 1-2 条）
- [ ] `multi_prompt` 各段是否支持独立 media 引用（文档示例未覆盖）
- [ ] 可灵"主体ID"（element_list）的注册流程与短剧角色一致性的适配度
