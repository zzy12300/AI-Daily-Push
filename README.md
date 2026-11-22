# 每日 AI 图文推送（daily-news-push）

> 纯 Python 标准库实现，零第三方依赖。部署到腾讯云函数 SCF，按点自动生成图文网页并推送到企业微信，**不需要电脑开机**。

---

## 1. 项目解决什么问题

想每天收到高质量内容，但：

| 痛点 | 本项目的解法 |
|---|---|
| 手动找资讯 / 找单词 / 查历史上的今天太费时间 | 定时自动生成，推送前你什么都不用做 |
| 内容需要自己写、自己排版 | DeepSeek 实时生成，脚本自动排版成网页 |
| 定制的日报脚本依赖本机定时任务，电脑关机就断 | 跑在云端（腾讯云函数 SCF），7×24 独立运行 |
| 手机上看纯文字没意思 | 自动配图（Unsplash / Wikimedia）、单词带真人发音 |
| 企业微信里外链图片常被拦截（裂图） | 图片、音频全部 base64 内嵌进 HTML，稳定显示与播放 |

产物：一个可点开的图文网页链接 + 企业微信群内的一条摘要消息。

---

## 2. 主要功能

### 三个内容板块（`task`）

| task | 名称 | 内容规格 | 配图 | 特色 |
|---|---|---|---|---|
| `news` | 每日 AI 新闻 | 3 条 × 约 150 字 | 每条 1 张（Unsplash） | 标题列表摘要 |
| `words` | 每日英语单词 | 3 个单词，含词性 / 中文释义 / 英文例句 / 例句翻译 | 无 | 有道真人发音 + 累积词库 + 已掌握屏蔽 |
| `history` | 历史上的今天 | 最多 3 条 × 约 300 字，按历史影响力排序 | 每条 0–1 张（Wikimedia 历史照片优先，Unsplash 兜底） | 农历日期 / 24 节气 / 节日纪念日简介 |

### 其他能力

- **企业微信推送**：群机器人 markdown 消息，含标题摘要 + 带时间戳 `?t=` 的网页链接（防缓存）。
- **网页托管**：HTML 上传腾讯云 COS，按 `板块/日期.html` 归档，例如 `news/2026-11-22.html`。
- **单词词库**：持久化在 COS `words/word_bank.json`，自动去重、自动补全历史单词缺失字段、今日单词次日入库。
- **节日体系**：农历传统节日 + 24 节气 + 约 68 个公历固定节日/国际日/纪念日（覆盖 64 个日期）+ 8 个浮动日期节日（母亲节、父亲节、感恩节、复活节等按算法计算）；DeepSeek 生成的节日需通过日期校验才采用。
- **农历算法**：内置 1900–2100 年农历数据表，不依赖外部接口。
- **时区安全**：统一按北京时间（UTC+8）计算日期，避免云函数 UTC 环境导致日期错位。

### 技术特征

- 运行环境：Python 3.9，仅用标准库（`urllib` / `hmac` / `hashlib` / `json` / `base64` / `datetime`），无需 `pip install`。
- 外部服务：DeepSeek（文本）、Unsplash + Wikimedia Commons（图片）、有道词典（发音）、腾讯云 COS（存储）、企业微信群机器人（推送）。

---

## 3. 安装方法

### 3.1 目录结构

```
zzy's first skill/
├── README.md
└── daily-news-push/
    ├── SKILL.md
    ├── _meta.json
    └── scripts/
        └── scf_index.py     ← 云函数入口，部署时重命名为 index.py
```

> 腾讯云 SCF 要求代码包内含 `index.py` 且暴露 `main_handler(event, context)`。
> 因此部署前把 `scf_index.py` **重命名为 `index.py`**（或保持文件名、把执行方法改为 `scf_index.main_handler`）。

### 3.2 第一步：准备 5 个平台密钥

| 组件 | 环境变量 | 获取方式 |
|---|---|---|
| 企业微信群机器人 | `WECOM_WEBHOOK` | 企业微信群 → 群机器人 → 添加 → 复制 Webhook 地址 |
| DeepSeek | `DEEPSEEK_API_KEY` | platform.deepseek.com 创建 API Key |
| Unsplash | `UNSPLASH_ACCESS_KEY` | unsplash.com/developers 创建应用（免费额度 50 次/小时） |
| 腾讯云 API | `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY` | 腾讯云控制台 → 访问管理 → API 密钥 |
| 腾讯云 COS | `COS_BUCKET` / `COS_REGION` | 对象存储创建存储桶（**公有读**），地域如 `ap-shanghai` |

### 3.3 第二步：创建云函数

| 配置项 | 值 |
|---|---|
| 运行环境 | Python 3.9 |
| 执行方法 | `index.main_handler` |
| 执行超时 | **300 秒**（默认 60 秒不够：历史板块要「生成 + 搜图 + 下载 + 上传」） |
| 内存 | 256 MB 或 512 MB（图片 base64 内嵌占内存） |
| 代码上传 | 将 `index.py` 打成 zip 上传，或在线编辑器直接粘贴 |

### 3.4 第三步：配置环境变量

在函数配置 → 环境变量中填 7 个：

```
WECOM_WEBHOOK         = https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
DEEPSEEK_API_KEY      = sk-xxx
UNSPLASH_ACCESS_KEY   = xxx
TENCENT_SECRET_ID     = AKIDxxx
TENCENT_SECRET_KEY    = xxx
COS_BUCKET            = your-bucket-1250000000
COS_REGION            = ap-shanghai
```

> 代码强制校验前 5 个（`WECOM_WEBHOOK` / `DEEPSEEK_API_KEY` / `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY` / `COS_BUCKET`），缺任一项直接返回错误。
> `COS_REGION` 缺省为 `ap-shanghai`。`UNSPLASH_ACCESS_KEY` 缺失不报错，但新闻/历史板块会因搜不到图而无配图。

### 3.5 第四步：配置定时触发器

腾讯云 Cron 为 **7 段**：`秒 分 时 日 月 周 年`

| 板块 | 时间 | Cron 表达式 | 附加信息 |
|---|---|---|---|
| AI 新闻 | 09:00 | `0 0 9 * * * *` | `{"task":"news"}` |
| 英语单词 | 12:30 | `0 30 12 * * * *` | `{"task":"words"}` |
| 历史上的今天 | 15:30 | `0 30 15 * * * *` | `{"task":"history"}` |

> 「附加信息」会以 `{"Type":"Timer","TriggerName":"words","Message":"{\"task\":\"words\"}"}` 形式传入，`Message` 是**字符串**。函数内 `_resolve_task_name()` 已做兼容解析，无需额外处理。
> 若触发器名称本身命名为 `news` / `words` / `history`，即使附加信息丢失也能兜底识别。

---

## 4. 使用方法

### 方式一：定时自动运行（推荐）

配置好触发器后，每天 09:00 / 12:30 / 15:30 自动生成并推送，无需人工干预。

### 方式二：控制台手动测试

在函数控制台「测试」页新建测试事件，直接传入 task 名即可立刻跑一次：

```json
{"task": "news"}
```

```json
{"task": "words"}
```

```json
{"task": "history"}
```

### 方式三：本地调试 / 运维（读写 COS 词库）

`scf_index.py` 内的 `load_word_bank` / `save_word_bank` / `cos_get_object` / `cos_put_object` 可直接在本地调用，用真实密钥操作 COS，例如清空词库、补全单词字段、排查页面问题，无需登录控制台。

### 修改生成内容

编辑 `TASKS` 字典（`scf_index.py` 第 678 行起）中对应板块的 `prompt` 即可改变生成风格与结构；`title` 决定推送标题，`dir` 决定 COS 归档目录，`default_kw` 是搜图兜底关键词。

### 新增板块

1. 在 `TASKS` 增加一项（`title` / `dir` / `default_kw` / `prompt`）；
2. 在 `main_handler` 增加对应处理分支；
3. 新增定时触发器，附加信息带 `task` 名；
4. 更新云函数代码并部署。

---

## 5. 输入输出示例

### 5.1 输入：事件对象 `event`

函数签名 `main_handler(event, context)`。`event` 支持三种格式：

**① 控制台测试事件（推荐手动测试用）**

```json
{"task": "words"}
```

**② 定时触发器事件（自动运行时由腾讯云注入）**

```json
{
  "Type": "Timer",
  "TriggerName": "words",
  "Time": "2026-11-22T12:30:00Z",
  "Message": "{\"task\":\"words\"}"
}
```

**③ 纯字符串**

```json
"history"
```

> 三种都能正确解析；无法识别时回退为 `news`。

### 5.2 输出：函数返回值

**成功（`news` / `history`）**

```json
{
  "errcode": 0,
  "errmsg": "ok",
  "url": "https://your-bucket-1250000000.cos.ap-shanghai.myqcloud.com/news/2026-11-22.html"
}
```

**成功（`words`，额外返回词库总量）**

```json
{
  "errcode": 0,
  "errmsg": "ok",
  "url": "https://your-bucket-1250000000.cos.ap-shanghai.myqcloud.com/words/2026-11-22.html",
  "word_bank_count": 42
}
```

**失败**

```json
{"errcode": -1, "errmsg": "缺少环境变量: WECOM_WEBHOOK,DEEPSEEK_API_KEY"}
```

可能的 `errmsg` 前缀：`缺少环境变量:` / `DeepSeek 调用失败:` / `COS 上传失败:` / `推送失败:`。

### 5.3 输出：企业微信群收到的消息

**AI 新闻 / 历史上的今天**

```markdown
**每日 AI 新闻**（2026年11月22日）
1. 某模型发布新版本
2. 某开源项目突破十万星
3. 某公司公布季度财报

[点击查看完整图文 >>](https://.../news/2026-11-22.html?t=1763800000)
```

**英语单词**（摘要只带单词 + 词性 + 中文，详情在网页）

```markdown
**每日英语单词**（2026-11-22）
1. resilient adj. — 有韧性的
2. nurture v. — 培育
3. subtle adj. — 微妙的

[点击查看例句 + 发音 + 词库 >>](https://.../words/2026-11-22.html?t=1763800000)
```

### 5.4 输出：生成的网页结构

| 板块 | 页面内容 |
|---|---|
| `news` | 日期标题 + 3 条新闻（标题 / 正文 / base64 内嵌配图），点图用页面内遮罩放大 |
| `words` | 今日 3 个单词卡片（词性、释义、例句、例句翻译、点击播放有道发音）+ 底部可折叠「我的词库」（含「已掌握」隐藏、「还原」按钮，localStorage 记录） |
| `history` | 顶部农历日期 / 节气 / 节日（可点击展开简介）+ 最多 3 条历史事件（年份标题 / 约 300 字正文 / 历史照片） |

> 页面内所有交互（展开词库、点图放大、播放发音）均用纯 JS 实现，**不做任何页面跳转**——企业微信内置浏览器会拦截 `window.open` / `<a href>` / `location.href`。

---

## 附：常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 三个时段推的都是 AI 新闻 | 定时触发器的 task 未解析到 | 确认「附加信息」填了 `{"task":"words"}`，或把触发器名设为 `words` / `history` |
| 历史板块执行超时 | 默认超时 60 秒不够 | 改执行超时为 300 秒 |
| 企业微信里图片裂开 | 用了 COS 外链 | 必须 base64 内嵌（代码已实现，勿改成外链） |
| 日期比预期差一天 | 云函数跑在 UTC | 已用 `utcnow() + 8h` 修正，勿改动 |
| 点单词没声音 | 浏览器策略限制自动播放 | 需用户点击触发，代码已用 `new Audio(dataUri).play()` 绑定点击 |
