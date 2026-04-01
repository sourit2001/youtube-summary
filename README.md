# YouTube 播客自动总结与飞书推送 (AI & Entrepreneurship)

每天自动监控指定的 YouTube 频道（如顶级的创业、AI访谈频道），提取最新视频的字幕，使用 DeepSeek 等大语言模型进行高价值长文总结，并生成对话摘要卡片发送至飞书。

## 快速开始

1. **环境准备**
   ```bash
   pip install -r requirements.txt
   ```

2. **配置环境变量**
   复制 `.env.example` 为 `.env`：
   ```bash
   cp .env.example .env
   ```
   并在 `.env` 中填入你的 API 配置和飞书 Webhook 地址。如果你使用的是 SiliconFlow 的 DeepSeek，你可以修改 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL` 环境变量以匹配 SiliconFlow。

3. **配置关注的频道**
   编辑 `channels.json`，其中包含了你想要关注的频道 ID 列表。（默认我为你加了 Lex Fridman, Y Combinator, My First Million, Lenny's Podcast, Latent Space 等全球顶尖 AI 和商业频道）。如果是新的频道，你可以通过 YouTube 频道页面源码搜索 `channelId` 来找到它。

4. **运行测试**
   ```bash
   python main.py
   ```
   初次运行会获取每个频道最近发布且带有字幕的视频进行总结。处理过的视频会被记录在 `processed.json` 中，下次运行会跳过。

## 如何通过 GitHub Actions 每天自动执行？

你可以在你的 GitHub 仓库中创建 `.github/workflows/daily-sync.yml` 文件：

```yaml
name: YouTube Podcast Summarizer
on:
  schedule:
    - cron: '0 0 * * *' # 每天 UTC 0 点运行
  workflow_dispatch:

jobs:
  run-summarizer:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run script
        env:
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
          FEISHU_WEBHOOK_URL: ${{ secrets.FEISHU_WEBHOOK_URL }}
          # 可选配置其他变量
        run: python main.py
      - name: Commit processed.json updates
        run: |
          git config --local user.email "action@github.com"
          git config --local user.name "GitHub Action"
          git add processed.json
          git commit -m "chore: update processed videos" || echo "No changes to commit"
          git push
```
*(注意：需要在仓库的 Settings > Secrets 中添加对应的环境变量，并且赋予 GITHUB_TOKEN Write 权限以便回写 processed.json)*
