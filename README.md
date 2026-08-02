# Podcast Pipeline · 英文播客 → 中文讲稿流水线

把英文播客自动转为中文讲稿，产出 **TTS 中文音频 + HTML 阅读页面**，并部署到 Cloudflare（Pages + R2）。

> 本仓库只包含**流水线方法论**（脚本、提示词、文档、部署配置）。
> 每期播客的具体内容（转录、讲稿、MP3）属于私有数据，**不在仓库内**——由你运行流水线后在本地 `content/` 生成。

---

## 它做什么

```
英文播客 URL / MP3
        │
        ▼  process.py（抓取 / ASR 转录）
原始转录.txt
        │
        ▼  纠错关口（按来源分级，scripts/纠错提示词.md）
转录_纠错.txt
        │
        ▼  写讲稿（Claude 交互，scripts/讲稿提示词.md）
讲书稿.md  ──▶ validator.py 结构体检（引言段 / 章节粒度 / 格式）
        │
        ▼  tts.py（Fish Audio，按 ## 标题切片，句边界，章间静音）
{播客名}.mp3
        │
        ▼  html_gen.py（讲稿 → 含播放器的 HTML 阅读页）
content.html
        │
        ▼  catalog.py（台账 + site.json + 首页）→ R2 上传音频 → Pages 部署
公开网站
```

## 工作流程（加新一期）

完整步骤见 [`CLAUDE.md`](./CLAUDE.md)，简要：

1. **抓取** `python scripts/process.py "https://转录来源链接"`
2. **纠错** 按 `来源.md` 判断来源类型，本地 ASR 强制纠错，官方字幕免纠错
3. **写讲稿** 按 `scripts/讲稿提示词.md`（必写引言段、章节 400–900 字、漏点清单、说话人三级）
4. **TTS + HTML** `python scripts/process.py --name "播客名" --tts-only`（自动跑结构体检）
5. **台账 + 发布** `python scripts/catalog.py finish "播客名"`（一键完成台账/site/首页/R2/部署）

## 目录结构

```
podcast-pipeline/
├── CLAUDE.md              # 完整工作流文档（最详细）
├── scripts/
│   ├── process.py         # 入口：抓取 + TTS 编排
│   ├── fetcher.py         # 网页抓取 + ASR 转录
│   ├── tts.py             # Fish Audio TTS
│   ├── html_gen.py        # 讲稿 → HTML（含音频播放器）
│   ├── validator.py       # 讲稿质量 + 结构体检
│   ├── catalog.py         # 台账 + site.json + 首页维护
│   ├── config.py          # 配置加载
│   ├── diarize.py         # 说话人分离（pyannote）
│   ├── 讲稿提示词.md      # 写讲稿规则
│   ├── 纠错提示词.md      # 转录纠错规则
│   └── 流水线文档.md      # 技术文档
├── site/
│   ├── deploy.sh          # Cloudflare Pages + R2 部署脚本
│   └── wrangler.toml      # Cloudflare 配置
├── content/               # ← 你的私有数据（gitignore，本地生成）
└── .env.example           # 环境变量模板
```

## 配置

```bash
cp .env.example .env
# 填入 FISH_KEY（Fish Audio，必填）、可选 HF_TOKEN（说话人分离）、R2_PUBLIC_URL
```

| 变量 | 说明 | 必填 |
|------|------|------|
| `FISH_KEY` | Fish Audio API key（TTS） | ✅ |
| `FISH_VOICE` | Fish Audio 音色 ID | ✅ |
| `HF_TOKEN` | HuggingFace token（说话人分离，可选） | – |
| `R2_PUBLIC_URL` | R2 桶公开访问地址（播放器流式音频） | – |

完整变量说明见 `.env.example`。

## 依赖

```bash
pip install -r requirements.txt
# 可选组件（ASR 引擎、网页正文提取、反爬抓取等）见 requirements.txt 注释
```

Cloudflare 部署需要 `wrangler`（`npx wrangler login` 完成 OAuth 登录即可，无需 API token）。

## 技术栈

- **抓取/转录**：httpx + curl_cffi（反爬降级）+ Parakeet/Whisper ASR + pyannote 说话人分离
- **讲稿**：Claude Code 按 `scripts/讲稿提示词.md` 生成（非外部 LLM API）
- **TTS**：Fish Audio（按 `## ` 标题切片、句边界、章间静音、断点续传）
- **部署**：Cloudflare Pages（HTML 站点）+ R2（音频流式播放，支持 Range）

## License

MIT
