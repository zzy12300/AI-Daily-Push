---
name: daily-news-push
display_name: "每日 AI 图文推送"
display_name_en: "Daily AI News Push"
description: >-
  搭建「云端定时 → AI 生成 → 配图 → 推送到企业微信」的每日图文推送系统，不依赖 PC 开机。
  用 DeepSeek 生成内容、Unsplash 搜配图、腾讯云 COS 存网页、企业微信群机器人 webhook 推送。
  三个板块：AI 新闻（配图）、英语单词（发音+词库）、历史上的今天（配图）。
  触发词 / Trigger words：每日推送、图文推送、AI 新闻推送、每日早报、定时推送、云函数推送、scf 推送、企业微信推送、每日单词、背单词
description_zh: "搭建每日 AI 图文推送：DeepSeek 生成 + Unsplash 配图 + COS 托管网页 + 企业微信推送，云端定时运行，不依赖 PC。含 AI 新闻、英语单词（发音+词库）、历史上的今天三板块。"
description_en: "Build daily AI news push: DeepSeek + Unsplash images + COS hosting + WeCom webhook, cloud-scheduled, PC-independent. Three boards: AI news, English words (audio+vocabulary bank), today in history."
category: automation
version: "1.2.1"
author: "小v"
disable-model-invocation: false
user-invocable: true
title: "每日 AI 图文推送 / Daily AI News Push"
runtime: generic
tags: ["每日推送", "图文", "AI新闻", "云函数", "企业微信", "DeepSeek", "Unsplash", "COS", "定时任务", "英语单词", "词库", "发音"]
dependencies: []
agent_created: true
---

# 每日 AI 图文推送

> 搭建「云端定时 → AI 生成 → 配图 → 推送到企业微信」的每日图文推送系统。
> 核心价值：**不依赖 PC 开机**，云端到点自动跑，内容 AI 实时生成，推送到企业微信手机端。

## 架构

```
腾讯云函数 SCF (定时触发器 09:00/12:30/15:30)
  └─> DeepSeek 生成文字内容
  └─> 三个板块差异化：
      ├─ AI 新闻：3条×150字，每条 Unsplash 配图
      ├─ 英语单词：3个单词，含例句+例句翻译+发音，无配图
      └─ 历史上的今天：最多3条×300字，按影响力排序，Wikimedia 历史照片优先
  └─> 图片/音频转 base64 内嵌 (微信内置浏览器稳定显示/播放)
  └─> 拼成 HTML 网页
  └─> 上传腾讯云 COS (对象存储)
  └─> 企业微信群机器人 webhook 推「摘要 + 链接」
```

## 三个板块

| 板块 | task | 内容 | 配图 | 特色 |
|---|---|---|---|---|
| AI 新闻 | news | 3条×150字 | 每条1张图 | 标题列表摘要 |
| 英语单词 | words | 3个单词 | 无 | 例句+例句翻译+发音+词库 |
| 历史上的今天 | history | 最多3条×300字 | 每条0-1张图 | 按影响力排序+节日简介+农历/节气 |

## 核心文件

- `scripts/scf_index.py` — 云函数入口（可直接部署，见文件内注释）

## 部署步骤（用户已完成的配置，供参考）

### 1. 前置准备（各平台拿 key）

| 组件 | 用途 | 获取方式 |
|---|---|---|
| 企业微信群机器人 webhook | 推送通道 | 企业微信群 → 群机器人 → 添加 → 复制 webhook |
| DeepSeek API Key | 生成文字 | platform.deepseek.com 充值后创建 |
| Unsplash Access Key | 搜配图 | unsplash.com/developers 创建应用 |
| 腾讯云 SecretId/SecretKey | 上传 COS | 腾讯云访问密钥 |
| COS 存储桶 + 地域 | 存网页 | 腾讯云对象存储，公有读 |

### 2. 云函数部署

- 运行环境：Python 3.9
- 执行方法：`index.main_handler`
- 执行超时时间：**300 秒**（历史板块要 DeepSeek 生成 + 搜图下载 + 上传 COS，60 秒默认值不够）
- 内存：256MB 或 512MB（历史照片 base64 内嵌占内存）
- 环境变量：
  - `WECOM_WEBHOOK`
  - `DEEPSEEK_API_KEY`
  - `UNSPLASH_ACCESS_KEY`
  - `TENCENT_SECRET_ID`
  - `TENCENT_SECRET_KEY`
  - `COS_BUCKET`
  - `COS_REGION`

### 3. 定时触发器（腾讯云 Cron 为 7 段：秒 分 时 日 月 周 年）

| 任务 | 时间 | Cron | 附加信息 |
|---|---|---|---|
| AI 新闻 | 09:00 | `0 0 9 * * * *` | `{"task":"news"}` |
| 英语单词 | 12:30 | `0 30 12 * * * *` | `{"task":"words"}` |
| 历史 | 15:30 | `0 30 15 * * * *` | `{"task":"history"}` |

## 关键技术决策（踩坑记录）

1. **图片用 base64 内嵌，不用 COS 外链**：企业微信内置浏览器会拦截 `myqcloud.com` 未备案域名的图片加载（裂图）。base64 内嵌图直接写在 HTML 里，稳定显示。
2. **发音用 base64 内嵌音频**：企业微信内置浏览器对 `<audio>` 标签、`window.open`、`<a href>`、`location.href` 全部拦截（或静音）。唯一可行方案是把有道发音音频下载后转 base64（`data:audio/mpeg;base64,...`）内嵌进 HTML，点击用 `new Audio(dataUri).play()` 播放。音频每条约 10KB，可接受。
3. **跳转一律避免**：企业微信内置浏览器封死所有页面跳转（window.open / a href / location.href）。词库不能做成「跳转到另一页」，而是**内嵌在当前页面用折叠面板**（JS 切 `display` 实现展开/收起，不跳转）。
4. **图片去重**：用 MD5 去重，避免同关键词返回重复图。
5. **点图放大用页面内遮罩**：不用 `window.open`（企业微信拦截），用 JS 遮罩层实现全屏预览。
6. **保存到相册无法实现**：企业微信内置浏览器封死了所有「网页内保存图片」的 JS 路径（download/blob/window.open 均无效）。如需保存，用手机系统浏览器打开链接后长按。
7. **推送链接加时间戳 `?t=`**：防缓存，确保每次点开最新页面。
8. **历史板块图片强相关**：Unsplash 无历史档案照片，历史板块改用 **Wikimedia Commons** 搜历史照片（含老照片/插画），搜不到再回退 Unsplash，最后兜底默认词，保证每条事件至少有图。Wikimedia 搜索/下载设短超时（10s）避免拖垮函数。
9. **时区修复**：云函数运行在 UTC，日期用 `utcnow() + timedelta(hours=8)` 转北京时间，避免「日期错乱」。
10. **农历算法自研**：内置 1900-2100 年农历数据表 + 节气表，验证过春节/中秋/冬至等日期准确。
11. **定时触发器的 task 在 `event["Message"]` 里**：腾讯云定时触发器不会把「附加信息」原样传给函数，而是包一层 `{"Type":"Timer","TriggerName":"words","Time":"...","Message":"{\"task\":\"words\"}"}`，且 `Message` 是**字符串**。直接 `event.get("task")` 永远取不到，会全部回退成默认 `news`（表现为三个时段都推 AI 新闻）。必须用 `_resolve_task_name()` 解析，兼容：控制台测试事件（裸 `{"task":...}`）、定时触发器（`Message` 包裹）、纯字符串；并以 `TriggerName` 兜底。

## 历史板块节日体系

历史板块网页顶部展示当天的「农历日期 + 节气 + 节日/纪念日」，节日带可点击简介。节日来源分四层，保证准确：

1. **农历传统节日**（14 个，农历算法动态匹配，不会搞错）：春节、元宵、龙抬头、上巳、端午、七夕、中元、中秋、重阳、寒衣、下元、腊八、小年、除夕。
2. **24 节气**（配简介，可点击展开）。
3. **公历固定节日/纪念日**（约 60 个「权威注册表」，日期与简介均联网核对）：中国官方节日（元旦、劳动节、国庆、建党、建军、教师节、记者节、消防日等）+ 主流国际日（世界水日、读书日、环境日、禁毒日、艾滋病日等）。
4. **浮动日期节日**（算法动态计算，日期每年变动）：感恩节、母亲节、父亲节、复活节及派生节日、美国总统日、马丁·路德·金纪念日、哥伦布日、阵亡将士纪念日等。

合并逻辑（`merge_festivals`）：注册表优先，DeepSeek 动态生成的节日做「日期校验」（简介里提取「X月X日」必须等于当天，否则丢弃），挡住模型乱列的日期错误节日。

## 单词词库机制

- **词库持久化**：存在 COS 的 `words/word_bank.json`，记录所有推送过的单词（word/pos/meaning/example/example_cn/date），去重保证不重复。
- **今日单词不进词库**：词库展示时过滤 `date == 今天` 的单词，即「我的词库 = 往期所有单词」，今天推送的明天才进词库。
- **补全机制**：旧单词缺字段（词性/释义/例句/例句翻译）时，脚本自动调 DeepSeek 批量补全。
- **屏蔽（已掌握）**：词库每个单词展开后有「已掌握」按钮，点击隐藏该单词。用 `localStorage` 记录（仅本机生效，换设备/清缓存会失效）。
- **还原**：词库标题栏有「还原」按钮，清空屏蔽记录并把所有单词折叠回初始状态。
- **词库在页面内折叠展示**：整体用 `toggleBank()` 折叠/展开；每个单词用 `toggleWord()` 单独折叠/展开（不跳转，规避企业微信拦截）。

## 修改内容的方法

改 `scripts/scf_index.py` 中 `TASKS` 字典的 `prompt`（控制生成内容）和 `main_handler` 里的逻辑，加新板块同理：

1. 在 `TASKS` 加一项（title/dir/default_kw/prompt）
2. 在 `main_handler` 加对应处理分支
3. 加对应定时触发器（附加信息带 task 名）
4. 云函数更新代码 + 部署

## 本地调试 COS

本地可用脚本直接读写 COS（用 secrets.txt 里的密钥 + `scf_index.py` 里的 `load_word_bank`/`save_word_bank`/`cos_get_object`/`cos_put_object`），无需登录控制台，例如清空词库、补全数据、排查问题。

## 成本

- DeepSeek：每天 3 次约 0.005 元，10 元够跑数年
- Unsplash：免费额度（50 次/小时）
- COS：免费额度内
- 云函数：免费额度内
