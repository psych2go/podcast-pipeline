# CLAUDE.md

## 这是什么

将英文播客转为中文讲稿（TTS音频 + HTML阅读页面），部署到 Cloudflare 网站。

## 工作流程（加新播客时按此步骤执行）

### 第 1 步：抓取

```bash
python scripts/process.py "https://转录来源链接"
```

这会自动从 URL 提取标题作为播客名，抓取转录原文保存到 `content/{播客名}/原始转录.txt`，并生成 `来源.md`（记录转录来源/类型，后续纠错关口、台账、site.json 都要读它，**保留不删**）。

> 如果来源是 HappyScribe 等 JS 渲染页面，fetcher 会自动用 curl_cffi 降级抓取。
> 如果来源是 MP3 文件（需 ASR 转录）：`python scripts/process.py "音频.mp3" --name "播客名"`

### 第 2 步：纠错（Claude 交互，按来源分级）

读 `原始转录.txt` 和 `来源.md`，按 `scripts/纠错提示词.md` 的**纠错关口**分级处理：
- 本地 ASR 转写（MP3）→ **强制纠错**，产出 `转录_纠错.txt`
- 第三方转录站 → 抽查，可靠则免纠错（在 `来源.md` 标注「转录质量」）
- 官方一手字幕 → 免纠错（在 `来源.md` 标注「转录质量」）

### 第 3 步：写讲稿（Claude 交互）

读 `转录_纠错.txt`（无则回退 `原始转录.txt`，回退前提是 `来源.md` 已标注转录质量可接受），按 `scripts/讲稿提示词.md` 写 `讲书稿.md`。

### 第 3.5 步：讲稿核验（Claude 交互，重要期推荐）

把 `讲书稿.md` 与转录文件并排对照通读，按 `scripts/讲稿提示词.md` 的「转录对照核验」逐项检查：脑补内容、张冠李戴、数字保真、论点覆盖、金句术语。发现的问题直接修进讲稿，再进 TTS。

### 第 4 步：TTS 出音频 + 生成 HTML

```bash
python scripts/process.py --name "播客名" --tts-only
```

产出：`{播客名}.mp3` + `{播客名} - content.html`

> 运行前自动做**结构体检**（`validator.py`）：检查引言段、章节粒度（400–900 字/章）、`SPEAKER_XX` 残留、中文夹空格等，只报告不修改。有告警就回到第 3 步修讲稿再跑。

### 第 5 步：清理中间文件

删除 `转录_纠错.txt`、`audio/` 分章节目录（如有）。**保留 `来源.md`**——它是台账和 site.json 自动生成的元数据来源（第 6/7 步要读）。

### 第 6 步：更新台账

用脚本把该期追加到 `content/播客目录.md`（自动算中文字数和时长，转录来源从 `来源.md` 读取）：

```bash
python scripts/catalog.py add "播客名"
# 只想看数字不写入：python scripts/catalog.py stats "播客名"
```

### 第 7 步：发布到 site/

用脚本拷贝 content.html 到 site/ 并重建 site.json：

```bash
python scripts/catalog.py sync-site
# 只同步某一期的 html：python scripts/catalog.py sync-site --only "播客名"
```

重建首页 index.html 的统计和卡片列表（读取 site.json，自动生成）：

```bash
python scripts/catalog.py gen-index
```

### 第 8 步：上传音频到 R2

R2 桶 `podcast-audio` 已开启**公开访问**（`wrangler r2 bucket dev-url enable podcast-audio`）。音频通过公开 URL 供播放器流式播放（支持 Range 分片，可拖动进度）。对象键**不带**桶名前缀：

```bash
npx wrangler r2 object put "播客名/播客名.mp3" \
  --file "content/播客名/播客名.mp3" --ct audio/mpeg
```

> 音频 URL = `https://{R2_PUBLIC_URL}/{播客名}/{播客名}.mp3`，`html_gen.py` 生成页面时自动用它拼播放器地址。

### 第 9 步：部署到 Cloudflare

```bash
cd site
npx wrangler pages deploy . --project-name podcast-scripts --branch main
```

> 部署只含 HTML/首页（音频在 R2，不再放进 site/），体积轻、速度快。

> **快捷方式**：第 6–9 步（台账 + site + 首页 + R2 + 部署）可一键完成，前提是音频已生成、中间文件已清理：
>
> ```bash
> python scripts/catalog.py finish "播客名"
> # 先看会执行什么：python scripts/catalog.py finish "播客名" --dry-run
> ```

---

## 命名约定

文件夹与最终 MP3 都用原始标题命名（URL 自动提取；`? * : [ ] | \` 等不安全字符会被清理，保留逗号/空格/中文）。

## content/ 目录规范

每期播客数据存放在 `content/{播客名}/` 下：

**标准文件（每期必有，4 个）：**
```
content/{播客名}/
├── {播客名}.mp3                ← TTS 生成的中文音频
├── {播客名} - 原始转录.txt     ← 英文转录原文
├── {播客名} - 讲书稿.md        ← Claude 写的中文讲稿
└── {播客名} - content.html      ← 阅读页面（含音频播放器 + 折叠目录）
```

**如果该期是从 MP3 经 ASR 转录而来（而非网页抓取字幕），额外增加：**
```
├── {播客名} - 原始音频.mp3     ← 原始的英文 MP3（ASR 输入源）
```

### 规则
1. 每期固定 4 个核心文件，MP3 源文件按需添加
2. **禁止保留中间产物**：`转录_纠错.txt`、`audio/` 分章节目录。`来源.md` 需保留（台账/站点自动化的元数据来源）
3. `{播客名}` 与文件夹名完全一致
4. 文本文件命名格式：`{播客名} - {类型}.{ext}`（` - ` 前后各一个空格）
5. TTS 音频直接用 `{播客名}.mp3`，不加 ` - 讲书稿` 后缀
6. 原始音频用 `{播客名} - 原始音频.mp3`

## 目录结构

```
podcast-pipeline/
├── .env                   ← API key（FISH_KEY）
├── CLAUDE.md              ← 本文件
├── requirements.txt
├── scripts/               ← 流水线代码
│   ├── process.py         ← 入口（抓取 + TTS 编排）
│   ├── fetcher.py         ← 网页抓取 + ASR 转录
│   ├── tts.py             ← Fish Audio TTS
│   ├── html_gen.py        ← 讲书稿.md → content.html（含音频播放器）
│   ├── validator.py       ← 讲稿质量校验 + 结构体检
│   ├── catalog.py         ← 台账 + site.json + 首页维护
│   ├── config.py          ← 配置加载
│   ├── diarize.py         ← 说话人分离
│   ├── 讲稿提示词.md      ← Claude 写讲稿规则
│   ├── 纠错提示词.md      ← Claude 转录纠错规则
│   └── 流水线文档.md      ← 技术文档
├── content/               ← 播客数据
│   ├── 播客目录.md        ← 台账
│   └── {播客名}/
│       ├── {播客名}.mp3
│       ├── {播客名} - 原始转录.txt
│       ├── {播客名} - 讲书稿.md
│       ├── {播客名} - content.html
│       └── 来源.md        ← 元数据（转录来源/质量，保留）
└── site/                  ← 部署站点（仅 HTML，音频在 R2）
    ├── index.html         ← 首页
    ├── {播客名}/content.html
    ├── site.json
    └── wrangler.toml
```

## 配置（.env）

```env
FISH_KEY=xxx                 # Fish Audio API key
FISH_VOICE=xxx               # Fish Audio 音色 ID
HF_TOKEN=hf_xxx              # HuggingFace token（可选，说话人分离）
R2_PUBLIC_URL=https://pub-xxx.r2.dev   # R2 桶公开访问地址（播放器流式音频）
```

**说明**：
- Cloudflare（R2 + Pages）认证走 `wrangler login` 的 OAuth 凭证，不再需要 `CLOUDFLARE_API_TOKEN`。
- `R2_PUBLIC_URL` 为空时，播放器回退到相对路径 `{播客名}.mp3`（音频需随站点部署）；配置后使用绝对 URL 走 R2 流式播放。

**安全提醒**：`.env` 已加入 `.gitignore`，不会提交到 Git。

## 关键约束

- 讲稿由 Claude Code 终端按 `scripts/讲稿提示词.md` 直接生成，含**必写引言段**（2–3 句）、**章节粒度 400–900 字/章**、**漏点清单**（写完对照）、**说话人三级**（能用名字用名字，无依据才角色化）、**结尾自然收束**
- 写稿前先过**纠错关口**（按 `来源.md` 判断来源类型决定是否纠错）；重要期写完后做**转录对照核验**（第 3.5 步）
- TTS 按 `## ` 标题切分章节，章节间插 ~0.8s 静音；默认朗读章节标题
- `process.py --tts-only` 会自动跑**结构体检**（引言段/章节粒度/中文夹空格等，只报告不修改）
- 正文禁用 Markdown 符号、破折号 `——`、分隔线 `---`
- 速度 speed=1.0，切片按句子边界；TTS 默认断点续传，`--force-tts` 全量重生
- 目标篇幅 ≈ 原文 15-20%，以讲透为准（长播客 1-2 万字）
- 阅读页 content.html 内置**音频播放器**（播放/进度/倍速/音量/下载），音频经 R2 公开 URL 流式播放（`R2_PUBLIC_URL`）
