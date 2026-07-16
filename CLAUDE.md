# CLAUDE.md

## Shell 命令纪律
- 禁止在前台运行长驻进程（uvicorn/fastapi run/npm run dev/watch 模式）。
  需要验证服务时：后台启动 + sleep + curl 探活 + kill，全套写在一条命令里。
- 所有可能长时间运行的命令加 timeout 包裹，如 `timeout 120 pytest`。
- 禁止任何交互式命令（会等待输入导致卡死）；apt 用 -y，pip 不要用需要确认的操作。
- pip 统一加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

## 本项目验证方式
- 回归测试永远跑 mock 适配器，禁止在测试中调真实视频 API 烧钱：
  `PYTHONPATH=src python -m harness.cli examples/brief_clothing_ad.json \
      --storyboard examples/storyboard_clothing_ad.json --adapter mock`
- API 事实（模型名/字段/计费）以 docs/VERIFIED_API_FACTS.md 为准，不要凭记忆写。