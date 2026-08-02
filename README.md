[English](./README.en.md) | **中文**

# Podcast Pipeline · 英文播客 → 中文讲稿流水线

> 把英文播客自动转为中文讲稿，产出 **TTS 中文音频** 与 **自带播放器的 HTML 阅读页面**，一键部署到 Cloudflare（Pages + R2）。

一个完整的「抓取 → 转录 → 纠错 → 讲稿 → 朗读 → 排版 → 发布」流水线。讲稿由 Claude 按内置提示词生成，TTS 走 Fish Audio，阅读页面是精心设计的暖调书卷风（含音频播放器、折叠目录、章节编号）。

> 📌 本仓库只含**方法论**（脚本、提示词、文档、部署配置）。每期播客的具体内容（转录、讲稿、MP3）属私有数据，**不在仓库内**——由你跑流水线后在本地 `content/` 生成。

## 预览

阅读页面长什么样？直接用浏览器打开示例文件：

👉 **[`examples/reader-page-example.html`](./examples/reader-page-example.html)** — 下载后用浏览器打开，含完整播放器 + 折叠目录 + 章节编号。

讲稿的输入格式参考 [`examples/sample-script.md`](./examples/sample-script.md) —— 一份带引言 + 三个章节的示例，喂给 `html_gen.py` 即可渲染出上面的页面。

## 特性

- 🎙️ **多源抓取**：网页字幕（含 JS 渲染页面、反爬站点降级）、RSS、本地 MP3（ASR 转录）
- 📝 **分级纠错**：按来源类型决定——本地 ASR 强制纠错，官方一手字幕免纠错
- ✍️ **讲稿由 Claude 生成**：按 `讲稿提示词.md` 的引言段、章节粒度（400–900 字）、说话人三级、漏点清单
- 🔊 **Fish Audio TTS**：按 `## ` 标题切片、句边界断句、章间静音、断点续传、`--force-tts` 全量重生
- 📖 **自包含阅读页**：暖调书卷设计、内置播放器（播放/倍速/音量/下载）、折叠目录、章节编号自动生成
- ☁️ **Cloudflare 部署**：Pages 托管站点，R2 流式分发音频（支持 Range 拖动进度）
- 🤖 **一键收尾**：`catalog.py finish` 自动完成台账 + 站点 + 首页 + R2 上传 + 部署

## 流水线

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
├── README.md              # 中文说明（你正在看）
├── README.en.md           # English README
├── CLAUDE.md              # 完整工作流文档（最详细）
├── LICENSE                # MIT
├── requirements.txt       # Python 依赖
├── .env.example           # 环境变量模板
├── examples/              # 输出样例（可浏览器直接打开）
│   ├── reader-page-example.html   # 阅读页渲染样例
│   └── sample-script.md           # 讲稿格式样例
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
└── content/               # ← 你的私有数据（gitignore，本地生成）
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
- **阅读页**：原生 HTML/CSS/JS，零依赖、自包含，`html_gen.py` 从讲稿生成
- **部署**：Cloudflare Pages（HTML 站点）+ R2（音频流式播放，支持 Range）

## License

MIT — 见 [`LICENSE`](./LICENSE)。
