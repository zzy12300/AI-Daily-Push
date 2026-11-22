# -*- coding: utf-8 -*-
"""
腾讯云函数 SCF 入口：每日 AI 新闻 → 生成网页（含图）→ 上传 COS → 企业微信推链接

先跑通 news 一个任务。部署方式：
- 运行时：Python 3.9
- 执行方法：index.main_handler
- 环境变量：
    WECOM_WEBHOOK       群机器人 webhook
    DEEPSEEK_API_KEY    DeepSeek key
    TENCENT_SECRET_ID   腾讯云 API 密钥 ID
    TENCENT_SECRET_KEY  腾讯云 API 密钥 Key
    COS_BUCKET          COS 存储桶名
    COS_REGION          COS 地域（如 ap-shanghai）
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import random
import re
import time
import urllib.parse
import urllib.request

# ============ 配置 ============
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# Unsplash 官方搜索 API（按关键词搜一张图）
UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"

# 单词词库：存在 COS 上的 JSON，记录所有已推送过的单词，避免重复
WORD_BANK_KEY = "words/word_bank.json"


# ============ DeepSeek ============
def call_deepseek(api_key, prompt):
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
        "max_tokens": 4000,
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


# ============ 企业微信 ============
def push_wecom(webhook, content):
    body = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": content}}
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ============ COS 上传（V5 签名 PUT Object）============
def cos_sign(secret_key, secret_id, method, path, headers, params, expire=600):
    key_time = "{};{}".format(int(time.time()) - 60, int(time.time()) + expire)
    sign_key = hmac.new(
        secret_key.encode("utf-8"), key_time.encode("utf-8"), hashlib.sha1
    ).hexdigest()

    http_method = method.lower()
    http_uri = path
    http_query = "&".join(
        "{}={}".format(
            urllib.parse.quote(k.lower(), safe=""),
            urllib.parse.quote(str(v), safe=""),
        )
        for k, v in sorted(params.items())
    )
    http_headers = "&".join(
        "{}={}".format(
            urllib.parse.quote(k.lower(), safe=""),
            urllib.parse.quote(str(v), safe=""),
        )
        for k, v in sorted(headers.items())
    )
    http_string = "{}\n{}\n{}\n{}\n".format(
        http_method, http_uri, http_query, http_headers
    )

    string_to_sign = "sha1\n{}\n{}\n".format(
        key_time, hashlib.sha1(http_string.encode("utf-8")).hexdigest()
    )
    signature = hmac.new(
        sign_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).hexdigest()

    authorization = (
        "q-sign-algorithm=sha1"
        "&q-ak={}&q-sign-time={}&q-key-time={}"
        "&q-header-list={}&q-url-param-list={}&q-signature={}"
    ).format(
        secret_id, key_time, key_time,
        ";".join(sorted(headers.keys())),
        ";".join(sorted(params.keys())),
        signature,
    )
    return authorization


def cos_put_object(secret_id, secret_key, bucket, region, key, content, content_type):
    host = "{}.cos.{}.myqcloud.com".format(bucket, region)
    path = "/" + key
    url = "https://" + host + path
    headers = {"host": host}
    params = {}
    authorization = cos_sign(secret_key, secret_id, "put", path, headers, params)

    req = urllib.request.Request(
        url, data=content,
        headers={
            "Host": host,
            "Content-Type": content_type,
            "Authorization": authorization,
        },
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, url


def cos_get_object(secret_id, secret_key, bucket, region, key):
    """从 COS 读取对象内容，返回 bytes；不存在返回 None"""
    host = "{}.cos.{}.myqcloud.com".format(bucket, region)
    path = "/" + key
    url = "https://" + host + path
    headers = {"host": host}
    params = {}
    authorization = cos_sign(secret_key, secret_id, "get", path, headers, params)
    req = urllib.request.Request(
        url, headers={
            "Host": host,
            "Authorization": authorization,
        }, method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def load_word_bank(secret_id, secret_key, bucket, region):
    """读取词库 JSON，返回 {'words': [...], 'count': n}；不存在则初始化
    words 列表元素兼容两种格式：
      - 旧格式：字符串 "budget"
      - 新格式：dict {"word":..., "pos":..., "meaning":..., "example":..., "date":...}
    读取时统一规范化为新格式（旧字符串只保留 word 字段）。"""
    try:
        raw = cos_get_object(secret_id, secret_key, bucket, region, WORD_BANK_KEY)
        if raw:
            bank = json.loads(raw.decode("utf-8"))
            normalized = []
            for w in bank.get("words", []):
                if isinstance(w, str):
                    normalized.append({
                        "word": w, "pos": "", "meaning": "", "example": "", "example_cn": "", "date": ""
                    })
                elif isinstance(w, dict):
                    normalized.append({
                        "word": w.get("word", ""),
                        "pos": w.get("pos", ""),
                        "meaning": w.get("meaning", ""),
                        "example": w.get("example", ""),
                        "example_cn": w.get("example_cn", ""),
                        "date": w.get("date", ""),
                    })
            return {"words": normalized, "count": len(normalized)}
    except Exception:
        pass
    return {"words": [], "count": 0}


def save_word_bank(secret_id, secret_key, bucket, region, bank):
    """写回词库 JSON"""
    data = json.dumps(bank, ensure_ascii=False).encode("utf-8")
    cos_put_object(secret_id, secret_key, bucket, region, WORD_BANK_KEY,
                   data, "application/json")


def get_wikimedia_image_urls(keyword, limit=3):
    """用 Wikimedia Commons API 搜历史图片/照片，返回最多 limit 张可直接下载的缩略图 URL 列表
    历史照片优先用这个源（Unsplash 无历史档案图）。一次请求返回多张候选，减少重复调用。"""
    api = "https://commons.wikimedia.org/w/api.php"
    params = urllib.parse.urlencode({
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": keyword,
        "gsrnamespace": "6",  # 6 = File 命名空间
        "gsrlimit": "12",
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": "800",  # 缩略到 800px，减小内嵌体积
    })
    url = "{}?{}".format(api, params)
    urls = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DailyPushBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pages = data.get("query", {}).get("pages", {})
        if not pages:
            return urls
        items = sorted(pages.values(), key=lambda p: p.get("title", ""))
        for page in items:
            if len(urls) >= limit:
                break
            ii = page.get("imageinfo", [])
            if not ii:
                continue
            info = ii[0]
            mime = info.get("mime", "")
            if mime not in ("image/jpeg", "image/png", "image/gif"):
                continue
            thumb = info.get("thumburl") or info.get("url")
            if thumb:
                urls.append(thumb)
    except Exception:
        pass
    return urls


def get_unsplash_image_url(access_key, keyword, offset=0):
    """用 Unsplash 官方搜索 API 按关键词返回一张图 URL，按相关度排序保证强相关"""
    params = urllib.parse.urlencode({
        "query": keyword,
        "per_page": 1,
        "page": 1 + offset,  # offset 递增换图，避免多条命中同一张
        "orientation": "landscape",
        "order_by": "relevant",  # 相关度优先，保证图片与事件强相关
    })
    url = "{}?{}".format(UNSPLASH_SEARCH_URL, params)
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": "Client-ID " + access_key}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results", [])
        if results:
            return results[0]["urls"]["regular"]
    except Exception:
        pass
    return ""


def download_image(url):
    """下载图片二进制内容，返回 (bytes, content_type)；失败返回 (None, None)"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read(), resp.headers.get("Content-Type", "image/jpeg")
    except Exception:
        return None, None


def get_word_audio_b64(word):
    """下载有道发音音频并转 base64（data URI 用），失败返回空串"""
    audio_url = "https://dict.youdao.com/dictvoice?audio={}&type=2".format(
        urllib.parse.quote(word)
    )
    try:
        req = urllib.request.Request(audio_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "audio/mpeg")
        return base64.b64encode(data).decode("ascii"), ctype
    except Exception:
        return "", "audio/mpeg"


def build_html(news_items, title, date_str, date_info="", festivals=None):
    cards = ""
    for idx, item in enumerate(news_items, 1):
        # 1-3 张图，横向滑动轮播；固定高度 + cover 裁剪，保证尺寸一致
        imgs_b64 = item.get("imgs_b64") or []
        img_html = ""
        if imgs_b64:
            slides = []
            dots = []
            for bi, b64 in enumerate(imgs_b64):
                data_uri = "data:image/jpeg;base64," + b64
                slides.append(
                    '<div style="flex:0 0 100%;width:100%;height:200px">'
                    '<img src="{src}" onclick="showZoom(this.src)" '
                    'style="width:100%;height:100%;object-fit:cover;display:block;cursor:zoom-in" />'
                    '</div>'.format(src=data_uri)
                )
                dots.append(
                    '<span onclick="goSlide(\'s{idx}\',{bi})" '
                    'style="width:8px;height:8px;border-radius:50%;background:{c};'
                    'display:inline-block;margin:0 3px;cursor:pointer"></span>'
                    .format(idx=idx, bi=bi, c="#2b6cf6" if bi == 0 else "#ccc")
                )
            if len(imgs_b64) > 1:
                img_html = (
                    '<div style="margin-bottom:12px">'
                    '<div id="s{idx}" style="display:flex;overflow-x:auto;scroll-snap-type:x mandatory;'
                    '-webkit-overflow-scrolling:touch;border-radius:8px;'
                    'scrollbar-width:none" onscroll="updateDots(\'s{idx}\',\'d{idx}\',{n})">'
                    '{slides}'
                    '</div>'
                    '<div id="d{idx}" style="text-align:center;margin-top:8px">{dots}</div>'
                    '</div>'
                ).format(idx=idx, slides=''.join(slides), dots=''.join(dots), n=len(imgs_b64))
            else:
                img_html = (
                    '<div style="margin-bottom:12px;width:100%;height:200px;overflow:hidden;border-radius:8px">{}</div>'
                    .format(slides[0])
                )
        cards += '<div style="margin-bottom:24px;padding:20px;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08)">'
        cards += img_html
        cards += '<h3 style="margin:0 0 8px;color:#1a1a1a;font-size:18px">{}</h3>'.format(item.get("title", ""))
        cards += '<p style="margin:0;color:#444;font-size:15px;line-height:1.8">{}</p>'.format(item.get("detail", ""))
        cards += '</div>'

    # 顶部副标题：公历日期 + 农历/节气 放在同一行；节日单独走下方简介区块
    subtitle = date_str
    if date_info:
        subtitle = "{} · {}".format(date_str, date_info)
    date_info_html = ""

    # 节日/纪念日简介区块（放在日期下方，独立卡片）
    fest_html = ""
    if festivals:
        fest_items = []
        for fi, f in enumerate(festivals, 1):
            fname = f.get("name", "")
            fintro = f.get("intro", "")
            fest_items.append(
                '<div style="margin-top:10px;background:rgba(255,255,255,.14);border-radius:8px;padding:10px 12px">'
                '<div onclick="toggleFest(\'f{fi}\', this)" style="cursor:pointer;font-size:14px;font-weight:600">'
                '🎉 {name} <span id="f{fi}Arrow" style="font-size:12px;color:rgba(255,255,255,.7)">▾</span></div>'
                '<div id="f{fi}" style="display:none;margin-top:6px;font-size:13px;line-height:1.7;'
                'color:rgba(255,255,255,.95)">{intro}</div>'
                '</div>'.format(fi=fi, name=fname, intro=fintro)
            )
        fest_html = ''.join(fest_items)

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  #zoombox{{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.92);z-index:9999;justify-content:center;align-items:center}}
  #zoombox img{{max-width:100%;max-height:100%;object-fit:contain}}
  #zoombox .close{{position:absolute;top:16px;right:20px;color:#fff;font-size:32px;cursor:pointer;line-height:1}}
</style>
</head>
<body style="margin:0;padding:20px;background:#f5f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif">
<div id="zoombox" onclick="this.style.display='none'">
  <span class="close">×</span>
  <img id="zoomimg" src="" />
</div>
<div style="max-width:640px;margin:0 auto">
  <div style="background:linear-gradient(135deg,#2b6cf6,#5b8cff);color:#fff;padding:24px;border-radius:12px;margin-bottom:20px">
    <h1 style="margin:0;font-size:22px">{title}</h1>
    <p style="margin:10px 0 0;opacity:.85;font-size:14px">{subtitle}</p>
    {date_info_html}
    {fest_html}
  </div>
  {cards}
  <p style="text-align:center;color:#999;font-size:12px;margin-top:24px">由 AI 自动生成 · 每日推送</p>
</div>
<script>
  function showZoom(src) {{
    document.getElementById('zoomimg').src = src;
    document.getElementById('zoombox').style.display = 'flex';
  }}
  function toggleFest(id, el) {{
    var body = document.getElementById(id);
    var arrow = document.getElementById(id + 'Arrow');
    if (!body) return;
    if (body.style.display === 'none') {{
      body.style.display = 'block';
      if (arrow) arrow.textContent = '▴';
    }} else {{
      body.style.display = 'none';
      if (arrow) arrow.textContent = '▾';
    }}
  }}
  function goSlide(boxId, i) {{
    var box = document.getElementById(boxId);
    if (!box) return;
    var slide = box.children[i];
    if (!slide) return;
    slide.scrollIntoView({{behavior:'smooth', inline:'start'}});
  }}
  function updateDots(boxId, dotsId, n) {{
    var box = document.getElementById(boxId);
    var dots = document.getElementById(dotsId);
    if (!box || !dots) return;
    var w = box.children[0] ? box.children[0].clientWidth : box.clientWidth;
    var idx = Math.round(box.scrollLeft / w);
    if (idx < 0) idx = 0;
    if (idx >= n) idx = n - 1;
    var spans = dots.children;
    for (var k = 0; k < spans.length; k++) {{
      spans[k].style.background = (k === idx) ? '#2b6cf6' : '#ccc';
    }}
  }}
</script>
</body>
</html>""".format(title=title, subtitle=subtitle, cards=cards,
             date_info_html=date_info_html, fest_html=fest_html)
    return html


def _render_bank_rows(bank, today=""):
    """把词库渲染成 HTML：每个单词一个折叠卡片 + 屏蔽按钮

    - 过滤掉 date == today 的单词（今天的单词不算「往期」，不进词库展示）
    - 每个单词带「屏蔽」按钮，点击后隐藏（用 localStorage 记住）
    """
    rows = ""
    for idx, hw in enumerate(bank.get("words", []), 1):
        hword = hw.get("word", "")
        hpos = hw.get("pos", "")
        hmeaning = hw.get("meaning", "")
        hexample = hw.get("example", "")
        hexample_cn = hw.get("example_cn", "")
        hdate = hw.get("date", "")
        # 跳过今天的单词（今日单词不进词库展示）
        if today and hdate == today:
            continue
        haudio_b64, haudio_type = get_word_audio_b64(hword)
        haudio_uri = "data:{};base64,{}".format(haudio_type, haudio_b64) if haudio_b64 else ""

        # 展开内容：词性 + 释义 + 例句 + 例句翻译
        detail_parts = []
        if hpos:
            detail_parts.append('<div style="font-size:13px;color:#888">词性：{}</div>'.format(hpos))
        if hmeaning:
            detail_parts.append('<div style="font-size:14px;color:#444">释义：{}</div>'.format(hmeaning))
        if hexample:
            detail_parts.append('<div style="font-size:13px;color:#2b6cf6">例句：{}</div>'.format(hexample))
        if hexample_cn:
            detail_parts.append('<div style="font-size:13px;color:#888">翻译：{}</div>'.format(hexample_cn))
        # 屏蔽按钮（放在展开内容底部）
        detail_parts.append(
            '<div style="margin-top:8px;text-align:right">'
            '<button type="button" onclick="hideWord(\'{word}\')" '
            'style="border:1px solid #ddd;background:#fff;color:#999;font-size:12px;border-radius:12px;padding:2px 12px;cursor:pointer">已掌握</button>'
            '</div>'.format(word=hword)
        )
        detail_html = "".join(detail_parts)

        speak_btn = (
            '<button type="button" onclick="playUri(\'h{idx}\')" '
            'style="border:none;padding:3px 12px;background:#2b6cf6;color:#fff;font-size:12px;border-radius:14px;cursor:pointer;flex-shrink:0">🔊</button>'
        ).format(idx=idx) if haudio_uri else ""

        rows += (
            '<div id="row_{word}" style="margin-bottom:8px;background:#fafafa;border-radius:8px;overflow:hidden">'
            # 标题行：折叠按钮 + 单词
            '<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 14px;cursor:pointer" '
            'onclick="toggleWord(\'w{idx}\',\'a{idx}\')">'
            '<div style="display:flex;align-items:center;gap:8px">'
            '<span id="a{idx}" style="color:#2b6cf6;font-size:16px;line-height:1;width:16px;text-align:center">▸</span>'
            '<b style="font-size:15px;color:#1a1a1a">{word}</b>'
            '</div>'
            '{speak_btn}'
            '</div>'
            # 展开内容（默认收起）
            '<div id="w{idx}" style="display:none;padding:8px 14px 12px 38px">{detail_html}</div>'
            '<audio id="h{idx}" src="{audio}" preload="auto" style="display:none"></audio>'
            '</div>'
        ).format(idx=idx, word=hword, speak_btn=speak_btn, detail_html=detail_html,
                 audio=haudio_uri)
    return rows


def build_words_html(words, title, date_str, bank=None):
    """单词专用网页：今日单词（例句+发音）+ 底部折叠词库

    发音用 base64 内嵌音频（data URI），点击按钮用 new Audio(dataURI).play() 播放。
    词库直接内嵌在当前页面，默认收起，点「查看词库」按钮展开/收起（纯 JS 切 display，
    不跳转、不被企业微信拦截）。
    """
    # 今日单词卡片
    cards = ""
    for idx, w in enumerate(words, 1):
        word = w.get("word", "")
        pos = w.get("pos", "")
        meaning = w.get("meaning", "")
        example = w.get("example", "")
        example_cn = w.get("example_cn", "")
        # 发音：下载音频转 base64 内嵌
        audio_b64, audio_type = get_word_audio_b64(word)
        audio_data_uri = "data:{};base64,{}".format(audio_type, audio_b64) if audio_b64 else ""
        example_html = ""
        if example:
            example_html = (
                '<p style="margin:8px 0 0;color:#2b6cf6;font-size:14px">例句：{}</p>'
                .format(example)
            )
        example_cn_html = ""
        if example_cn:
            example_cn_html = (
                '<p style="margin:4px 0 0;color:#888;font-size:13px">翻译：{}</p>'
                .format(example_cn)
            )
        speak_btn = ""
        if audio_data_uri:
            speak_btn = (
                '<button type="button" onclick="playUri(\'{id}\')" '
                'style="border:none;padding:6px 14px;background:#2b6cf6;color:#fff;font-size:13px;border-radius:16px;cursor:pointer">🔊 发音</button>'
            ).format(id="t{}".format(idx))
        cards += (
            '<div style="margin-bottom:20px;padding:20px;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08)">'
            '<div style="display:flex;align-items:center;justify-content:space-between">'
            '<h3 style="margin:0;color:#1a1a1a;font-size:22px">{word}'
            '<span style="font-size:14px;color:#888;font-weight:normal;margin-left:8px">{pos}</span></h3>'
            '{speak_btn}'
            '</div>'
            '<p style="margin:8px 0 0;color:#444;font-size:15px">{meaning}</p>'
            '{example_html}'
            '{example_cn_html}'
            '<audio id="t{idx}" src="{audio}" preload="auto" style="display:none"></audio>'
            '</div>'
        ).format(word=word, pos=pos, meaning=meaning, example_html=example_html,
                 example_cn_html=example_cn_html, speak_btn=speak_btn, audio=audio_data_uri, idx=idx)

    # 底部折叠词库（只展示往期单词，排除今日）
    bank_section = ""
    if bank and bank.get("words"):
        # 统计往期单词数（排除 date == 今天 的单词）
        past_words = [w for w in bank["words"] if w.get("date") != date_str]
        total = len(past_words)
        bank_rows = _render_bank_rows(bank, today=date_str)
        bank_section = (
            '<div style="margin-top:24px;padding:16px 18px;background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08)">'
            '<div style="display:flex;align-items:center;justify-content:space-between;cursor:pointer" onclick="toggleBank()">'
            '<h3 style="margin:0;color:#1a1a1a;font-size:16px">📚 我的词库（已累计 {} 个）</h3>'
            '<div style="display:flex;align-items:center;gap:12px">'
            '<button type="button" onclick="restoreHidden(event)" '
            'style="border:1px solid #ddd;background:#fff;color:#999;font-size:12px;border-radius:14px;padding:3px 12px;cursor:pointer">还原</button>'
            '<span id="bankArrow" style="color:#888;font-size:18px">▼</span>'
            '</div>'
            '</div>'
            '<div id="bankBody" style="display:none;margin-top:8px">{rows}</div>'
            '</div>'
        ).format(total, rows=bank_rows)

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
</head>
<body style="margin:0;padding:20px;background:#f5f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:640px;margin:0 auto">
  <div style="background:linear-gradient(135deg,#2b6cf6,#5b8cff);color:#fff;padding:24px;border-radius:12px;margin-bottom:20px">
    <h1 style="margin:0;font-size:22px">{title}</h1>
    <p style="margin:8px 0 0;opacity:.85;font-size:13px">{date}</p>
  </div>
  {cards}
  {bank_section}
  <p style="text-align:center;color:#999;font-size:12px;margin-top:24px">由 AI 自动生成 · 每日推送</p>
</div>
<script>
  var _lastAudio = null;
  function playUri(id) {{
    var el = document.getElementById(id);
    if (!el || !el.src) return;
    try {{
      if (_lastAudio) {{ _lastAudio.pause(); _lastAudio = null; }}
      var a = new Audio(el.src);
      _lastAudio = a;
      a.play();
    }} catch (e) {{
      try {{ el.play(); }} catch (e2) {{}}
    }}
  }}
  function toggleBank() {{
    var body = document.getElementById('bankBody');
    var arrow = document.getElementById('bankArrow');
    if (!body) return;
    if (body.style.display === 'none') {{
      body.style.display = 'block';
      if (arrow) arrow.textContent = '▲';
    }} else {{
      body.style.display = 'none';
      if (arrow) arrow.textContent = '▼';
    }}
  }}
  function toggleWord(bodyId, arrowId) {{
    var body = document.getElementById(bodyId);
    var arrow = document.getElementById(arrowId);
    if (!body) return;
    if (body.style.display === 'none') {{
      body.style.display = 'block';
      if (arrow) arrow.textContent = '▾';
    }} else {{
      body.style.display = 'none';
      if (arrow) arrow.textContent = '▸';
    }}
  }}
  // 屏蔽（已掌握）：把单词加入 localStorage 隐藏列表，并立即隐藏
  function hideWord(word) {{
    try {{
      var key = 'hidden_words';
      var list = JSON.parse(localStorage.getItem(key) || '[]');
      if (list.indexOf(word) === -1) list.push(word);
      localStorage.setItem(key, JSON.stringify(list));
    }} catch (e) {{}}
    var row = document.getElementById('row_' + word);
    if (row) row.style.display = 'none';
  }}
  // 还原：清空隐藏列表，重新显示所有单词，并全部折叠回初始状态
  function restoreHidden(ev) {{
    if (ev) ev.stopPropagation();
    try {{
      localStorage.removeItem('hidden_words');
    }} catch (e) {{}}
    var bank = document.getElementById('bankBody');
    if (!bank) return;
    // 重新显示所有被屏蔽的单词
    var rows = bank.querySelectorAll('[id^="row_"]');
    for (var i = 0; i < rows.length; i++) {{
      rows[i].style.display = '';
    }}
    // 所有单词折叠回初始状态（展开内容隐藏 + 箭头复位）
    var bodies = bank.querySelectorAll('[id^="w"]');
    for (var j = 0; j < bodies.length; j++) {{
      bodies[j].style.display = 'none';
    }}
    var arrows = bank.querySelectorAll('[id^="a"]');
    for (var k = 0; k < arrows.length; k++) {{
      arrows[k].textContent = '▸';
    }}
  }}
  // 页面加载时，隐藏已屏蔽的单词
  (function applyHidden() {{
    try {{
      var key = 'hidden_words';
      var list = JSON.parse(localStorage.getItem(key) || '[]');
      for (var i = 0; i < list.length; i++) {{
        var row = document.getElementById('row_' + list[i]);
        if (row) row.style.display = 'none';
      }}
    }} catch (e) {{}}
  }})();
</script>
</body>
</html>""".format(title=title, date=date_str, cards=cards, bank_section=bank_section)
    return html


# ============ 任务定义 ============
# 每个任务：标题、目录前缀、默认搜索词、生成 prompt
# 加新板块：往这里加一项 + 云函数加对应定时触发器即可
TASKS = {
    "news": {
        "title": "每日 AI 新闻",
        "dir": "news",
        "default_kw": "artificial intelligence",
        "prompt": (
            "你是科技资讯编辑。请生成今天的 3 条 AI 领域重要新闻，"
            "每条包括一个小标题和约 150 字的详细介绍，总计约 500 字。"
            "格式严格如下（每条一行标题、一段详情）：\n"
            "1. 标题\n标题的详细内容介绍。\n"
            "2. 标题\n详细内容。\n"
            "3. 标题\n详细内容。\n"
            "直接输出，不要额外说明。"
        ),
    },
    "words": {
        "title": "每日英语单词",
        "dir": "words",
        "default_kw": "english vocabulary",
        "prompt": (
            "你是英语学习助手。请生成今天的 3 个实用英语单词，"
            "每个单词严格按以下 5 行格式输出（不要多行、不要编号外的内容）：\n"
            "1. 单词\n词性：n.\n中文：xxx\n例句：This is an example.\n例句翻译：这是一个例句。\n\n"
            "2. 单词\n词性：v.\n中文：xxx\n例句：...\n例句翻译：...\n\n"
            "3. 单词\n词性：adj.\n中文：xxx\n例句：...\n例句翻译：...\n\n"
            "词性请用常见英文简写（n. / v. / adj. / adv. / prep. / conj. / pron. 等），"
            "不要用完整的中文词性名称。每个单词都要有词性、中文释义、一个英文例句和例句的中文翻译。直接输出，不要额外说明。"
        ),
    },
    "history": {
        "title": "历史上的今天",
        "dir": "history",
        "default_kw": "history",
        "prompt": (
            "你是历史编辑。请严格针对【{date_cn}】这一天（月日必须精确是 {month}月{day}日，"
            "不考虑年份），完成两部分内容。\n"
            "\n"
            "【第一部分：历史事件】\n"
            "1. 只写确实发生在 {month}月{day}日 的事件（公历月日严格等于 {month}月{day}日），日期必须精确，绝不能编造或张冠李戴。\n"
            "2. 尽量列出 3 条（当天确凿的重大事件通常不止 3 件，不要遗漏），必须严格按照「历史影响力」"
            "从高到低排序：影响最深、意义最重大、最广为人知的事件排在最前面，次要的排后面。\n"
            "3. 特别强调：只有当你能 100% 确定某事件确实发生在 {month}月{day}日时，才写它。"
            "如果你对某个事件的日期没有十足把握，哪怕它「可能」是这一天，也绝对不要写。"
            "宁可少列一条，也绝不用日期存疑的事件充数；但确凿的事件够 3 件时，务必列满 3 件，不要偷懒只写 1-2 条。\n"
            "4. 选材要优先选「载入史册、全球闻名」的标志性大事：比如改变世界格局的战争与革命、"
            "改写人类文明进程的科技发明（如爱迪生留声机、首次飞行等）、享誉世界的文学家/科学家/政治家的诞生或去世、"
            "震惊世界的重大事件等。宁可只写一件这样的标志性大事，也不要写几件鲜为人知的次要事件来充数。\n"
            "5. 每条包括一个事件标题和约 300 字介绍。\n"
            "格式严格如下（每条三行：一行标题、一段详情、一行英文配图关键词）：\n"
            "1. 年份 事件名称\n事件介绍。\n关键词：montgolfier hot air balloon\n"
            "2. 年份 事件名称\n介绍。\n关键词：...\n"
            "标题格式必须是「年份 + 空格 + 简洁事件名称」，例如「1783年 人类首次热气球载人飞行」，"
            "标题本身要能让人一眼看出是什么事件，不能只有日期。\n"
            "关键词必须是能精准搜到该事件「真实历史照片/插画」的英文短语或专有名词，"
            "要指向该事件本身（如 montgolfier hot air balloon、apollo 11、"
            "F. Scott Fitzgerald、Birmingham church bombing 等），"
            "优先用英文专有名词、人名、地名，便于在维基共享资源检索到历史图片。\n"
            "每个事件必须给 1 个关键词（不要留空），即使历史照片难找，"
            "也要给一个能搜到「代表性画面」的词（例如事件相关的人名、地点、标志物），"
            "确保能搜到配图。绝不要用 history、people、city 之类宽泛词，也绝不要给与事件无关的关键词。\n"
            "\n"
            "【第二部分：当天节日/纪念日简介】\n"
            "严格针对 {month}月{day}日 这一天，只列出「确定在这一天」的节日、纪念日、节气"
            "（公历和农历都要考虑）。注意：如果某个节日/纪念日不是 {month}月{day}日，"
            "绝对不要列出来，宁缺毋滥。\n"
            "如果 {month}月{day}日 恰好是二十四节气中的某一个，必须把这个节气单独列出来，"
            "而且简介要写详细（80~150 字）：说明它是第几个节气、大致交节时间、"
            "气候与物候特征、对应的农事活动、民间习俗，以及它在中国农耕文化中的意义。\n"
            "对每一个都给出：它的诞生日期（或设立时间）和它纪念/庆祝的意义。"
            "每个节日单独一段，格式严格如下（每段两行：一行节日名，一行简介）：\n"
            "节日：世界问候日\n简介：1973年由澳大利亚的姆可马克与米切尔两兄弟发起，将11月21日定为世界问候日，呼吁人们用一句问候传递和平与善意，现已在全球140多个国家流传。\n"
            "注意：简介里必须明确写出「将X月X日定为该节日」这类具体日期，"
            "且这个 X月X日 必须就是 {month}月{day}日（节气则要写出交节日期 {month}月{day}日）。"
            "如果你不能确定某个节日的日期恰好是 {month}月{day}日，就不要列它。\n"
            "直接输出，不要额外说明。"
        ),
    },
}


def parse_news(text, default_title="今日 AI 新闻"):
    """把 DeepSeek 生成的文本解析成 3 条 [{title, detail, img_kw, img_kws}]
    img_kw 保留第一个关键词，img_kws 是逗号分隔的多个关键词列表"""
    items = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    current = None
    for line in lines:
        if line.startswith("节日") or line.startswith("简介"):
            # 遇到节日简介区块，停止解析事件
            break
        if line[:1].isdigit() and len(line) > 1 and line[1] in ".、)）":
            title = line[2:].strip()
            if "：" in title:
                title, detail = title.split("：", 1)
            elif ":" in title:
                title, detail = title.split(":", 1)
            else:
                detail = ""
            current = {"title": title, "detail": detail, "img_kw": "", "img_kws": []}
            items.append(current)
        elif line.startswith("#"):
            title = line.lstrip("#").strip()
            current = {"title": title, "detail": "", "img_kw": "", "img_kws": []}
            items.append(current)
        elif current is not None:
            # 提取配图关键词行：关键词：xxx / 关键词:xxx（支持逗号分隔多个）
            if line.startswith("关键词") or line.startswith("配图") or line.startswith("图片"):
                kw_text = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                kws = [k.strip() for k in kw_text.replace("，", ",").split(",") if k.strip()]
                current["img_kws"] = kws
                current["img_kw"] = kws[0] if kws else ""
            else:
                current["detail"] = (current["detail"] + " " + line).strip()
    if not items:
        items = [{"title": default_title, "detail": text, "img_kw": "", "img_kws": []}]
    return items[:3]


def parse_festivals(text):
    """解析「节日简介」区块，返回 [{name, intro}]
    简介允许跨多行：遇到下一个「节日：」或区块标记（【、第…）才结束，
    中间不以「节日/简介/【/第/数字编号」开头的行会拼接到上一条简介里
    （模型偶尔把气候、习俗写到第二行，旧版只取第一行会丢内容）。"""
    result = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    current = None
    in_section = False
    for line in lines:
        if line.startswith("节日：") or line.startswith("节日:"):
            in_section = True
            current = {"name": line.split("：", 1)[-1].split(":", 1)[-1].strip(),
                       "intro": ""}
            result.append(current)
        elif in_section and (line.startswith("简介：") or line.startswith("简介:")):
            if current is not None:
                current["intro"] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif line.startswith(("【", "第")):
            # 遇到新的大区块标记，停止
            in_section = False
            current = None
        elif in_section and current is not None:
            if re.match(r"^\d+[\.、]", line):
                # 形如「1. 1783年 …」，属于历史事件区，说明已离开节日区
                in_section = False
                current = None
            elif current.get("intro"):
                # 简介续行（上限 400 字，避免异常输入把整段正文吸进来）
                if len(current["intro"]) < 400:
                    current["intro"] += line
    return [f for f in result if f.get("name")]


def merge_festivals(deepseek_festivals, today_date):
    """合并节日来源，保证准确：
    1. 先取「权威注册表」里当天（月,日）的节日（名称 + 简介），这些 100% 准确、优先采用。
    2. DeepSeek 生成的节日，逐个做校验：
       - 名字归一化后若是「节气」，必须等于今天的实际节气（防止把别的节气说成今天）；
       - 简介里必须能提取到「X月X日」且等于今天（与注册表重名的除外，交给去重比详情）；
       不通过即丢弃。
    3. 注册表里没有、但 DeepSeek 生成且通过校验的，才补充进来。
    4. 标记来源（_src），供去重时判断「撞名时简介该用谁的」。
    返回 [{name, intro}]（顺序：注册表优先，模型补充的在后）。"""
    month = today_date.month
    day = today_date.day
    today_term = get_solar_term(today_date)

    items = []
    reg_keys = set()

    # 1) 注册表节日（权威、优先）
    for name, intro in SOLAR_FESTIVALS.get((month, day), []):
        items.append({"name": name, "intro": intro, "_src": "reg"})
        reg_keys.add(_fest_key(name))

    # 2) DeepSeek 生成节日：过滤错误节气 + 日期校验
    for f in deepseek_festivals:
        name = (f.get("name") or "").strip()
        intro = (f.get("intro") or "").strip()
        if not name:
            continue
        key = _fest_key(name)
        if not key:
            continue
        # 是节气名但不是今天的节气 → 判定为错误，丢弃
        if key in SOLAR_TERM_NAMES and key != today_term:
            continue
        # 与注册表重名：允许进入后续去重（撞名时用模型这份更详细的简介）
        if key not in reg_keys and not _festival_date_matches(intro, month, day):
            continue
        items.append({"name": name, "intro": intro, "_src": "ds"})

    return _dedupe_festivals(items)


def _norm_fest_name(name):
    """节日名归一化，用于去重比较。
    去掉括号/方括号内的补充说明（如「小雪（节气）」→「小雪」），
    并去掉空格、书名号、引号等标点，统一大小写。"""
    if not name:
        return ""
    s = name.strip()
    # 去掉圆括号/方括号/中文括号里的内容（可多次出现）
    s = re.sub(r"[（(【\[][^）)】\]]*[）)】\]]", "", s)
    # 去掉常见标点与空白
    s = re.sub(r"[\s·・\.。、《》「」『』\"'’‘“”\-—_/／:：]", "", s)
    # 去掉结尾的「节气」二字（如「小雪节气」→「小雪」），与节气表名称对齐
    if s.endswith("节气"):
        s = s[:-2]
    return s.lower()


# 同一节日的不同叫法 -> 主名（用于跨来源去重）
FEST_ALIASES = {
    "上元节": "元宵节", "元宵": "元宵节", "灯节": "元宵节",
    "七夕": "七夕节", "乞巧节": "七夕节",
    "盂兰盆节": "中元节",
    "祭灶节": "小年", "小年节": "小年",
    "春龙节": "龙抬头", "青龙节": "龙抬头", "二月二": "龙抬头",
    "老人节": "重阳节", "登高节": "重阳节", "敬老节": "重阳节",
    "踏青节": "清明", "清明节": "清明",
    "农历新年": "春节", "新春": "春节",
    "大年三十": "除夕",
    "腊八": "腊八节",
    "冬节": "冬至",
    "三月三": "上巳节",
}


def _fest_key(name):
    """去重用的归一化 key：基础归一化 + 别名归并 + 节气变体归并。
    「清明节/冬至节/立春节」这类「节气名 + 节」统一归到节气名，
    避免节气表与注册表/DeepSeek 各写一遍导致重复展示。"""
    k = _norm_fest_name(name)
    if not k:
        return ""
    if k in FEST_ALIASES:
        return _norm_fest_name(FEST_ALIASES[k])
    if k.endswith("节") and k[:-1] in SOLAR_TERM_NAMES:
        return k[:-1]
    return k


def _dedupe_festivals(items):
    """按 _fest_key 去重（只在真的撞名时生效，不撞名的条目原样保留）：
    - 名称：保留首次出现的那个，即权威来源（农历/节气/浮动/注册表）的规范叫法，
      不会出现「小雪」被模型写成「小雪（节气）」这种；
    - 简介：撞名时优先用 DeepSeek 写的那份（更详细、更像介绍），
      模型没写或太短（<30 字）时，才退回「更详细者胜出（≥1.2 倍）」的旧规则。"""
    order = []
    best = {}
    for f in items:
        name = (f.get("name") or "").strip()
        intro = (f.get("intro") or "").strip()
        if not name:
            continue
        key = _fest_key(name)
        if not key:
            continue
        if key not in best:
            order.append(key)
            best[key] = {"name": name, "intro": intro, "_tier": f.get("_tier", 9)}
        else:
            old_len = len(best[key].get("intro") or "")
            if f.get("_src") == "ds" and len(intro) >= 30:
                # 撞名且这份是 DeepSeek 写的 → 用它的简介，名称仍保持权威叫法
                best[key]["intro"] = intro
            elif len(intro) >= old_len * 1.2:
                # 明显更详细（≥1.2 倍）才替换，避免几乎等长时覆盖已核对的版本
                best[key]["intro"] = intro
    return [best[k] for k in order]


# 中国官方设立/法定的节日与纪念日（排序层级 2：国家与政治纪念日）
NATIONAL_FESTIVALS = {
    "元旦", "国际劳动节", "国际劳动妇女节", "国际儿童节", "国际护士节",
    "教师节", "记者节", "植树节", "青年节", "建党节", "建军节", "国庆节",
    "全国消防日", "全国爱眼日", "全国防灾减灾日", "全民国家安全教育日",
    "中国医师节", "中国人民警察节", "中国农民丰收节", "烈士纪念日",
    "南京大屠杀死难者国家公祭日", "中国人民抗日战争胜利纪念日",
    "九一八事变纪念日", "七七事变纪念日", "二七纪念日", "五卅纪念日",
    "辛亥革命纪念日", "学雷锋纪念日", "香港回归纪念日", "澳门回归纪念日",
}


def _fest_tier(name):
    """节日重要性层级（越小越靠前）：
    0 中国传统民俗节日、1 二十四节气、2 国家与政治纪念日、
    3 国际组织发起的主题日、4 其他（外国节日、宗教节日等）。
    农历/节气/浮动节日由调用方直接指定层级，这里只判断注册表和模型生成的条目。"""
    k = _norm_fest_name(name)
    if not k:
        return 4
    if k in NATIONAL_FESTIVALS:
        return 2
    if k.startswith(("世界", "国际", "全球", "联合国")):
        return 3
    return 4


_CN_NUMERALS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9}
_CN_DATE_RE = re.compile(r"([一二三四五六七八九十廿卅]{1,3})月([一二三四五六七八九十廿卅]{1,4})日")


def _cn_numeral_to_int(s):
    """中文数字转整数（覆盖 1~31 的日期场景）：十一→11、二十三→23、三十→30、廿二→22。"""
    s = (s or "").strip()
    if not s:
        return None
    if s in _CN_NUMERALS:
        return _CN_NUMERALS[s]
    if s == "十":
        return 10
    if s[0] in ("廿", "卅"):
        base = 20 if s[0] == "廿" else 30
        rest = s[1:]
        return base + (_CN_NUMERALS.get(rest, 0) if rest else 0)
    if "十" in s:
        tens, _, ones = s.partition("十")
        t = _CN_NUMERALS.get(tens, 1) if tens else 1
        o = _CN_NUMERALS.get(ones, 0) if ones else 0
        return t * 10 + o
    return None


_CN_DAY_AFTER_SEP_RE = re.compile(r"([—\-–~～至到])\s*([一二三四五六七八九十廿卅]{1,4})日")


def _normalize_cn_dates(text):
    """把「十一月二十二日」这类中文数字日期统一成「11月22日」，
    区间右端点省略月份时（「十一月二十二日至二十三日」）也一并转换，
    便于后续按同一套规则做日期校验。转换失败的片段原样保留。"""
    def _rep(m):
        mo = _cn_numeral_to_int(m.group(1))
        d = _cn_numeral_to_int(m.group(2))
        if mo and d:
            return "%d月%d日" % (mo, d)
        return m.group(0)

    text = _CN_DATE_RE.sub(_rep, text)

    def _rep_day(m):
        d = _cn_numeral_to_int(m.group(2))
        return m.group(1) + ("%d日" % d if d else m.group(2) + "日")

    return _CN_DAY_AFTER_SEP_RE.sub(_rep_day, text)


_DATE_RANGE_RE = re.compile(
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*[—\-–~～至到]\s*(\d{1,2})\s*日"
)


def _explicit_dates(text):
    """提取简介里「具体到日」的所有日期，返回 [(月, 日), ...]。
    - 「11月22日」→ (11, 22)
    - 「11月16日—22日」这类区间：只收两个端点（16 和 22），
      中间的日子不算 —— 区间写法本身代表跨多天，不能拿它当「就是今天」的依据；
    - 只有「11月」「11月中旬」「11月下旬」这类没到日的写法，一概不收。"""
    pairs = []
    # 先补区间右端点：右端点常省略「月」（11月22日—23日 / 11月22日至23日）
    for m in _DATE_RANGE_RE.finditer(text):
        mo, d1, d2 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        pairs.append((mo, d1))
        pairs.append((mo, d2))
    # 再收普通「X月X日」
    for m in re.finditer(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text):
        pairs.append((int(m.group(1)), int(m.group(2))))
    return pairs


def _festival_date_matches(intro, month, day):
    """判断简介里是否「明确提到今天这一天」。
    提取简介中所有具体到日的「X月X日」，任意一处等于 (month, day) 即通过；
    中文数字日期（十一月二十二日）先归一成阿拉伯数字再比对；
    区间写法（11月22日—23日、11月16日至22日）只认两个端点，中间日期不算；
    只写了「11月」「11月中旬」这类不到日的表述、或一处日期都没有时返回 False（丢弃），
    避免展示日期不符的节日。"""
    if not intro:
        return False
    text = _normalize_cn_dates(intro)
    return (month, day) in _explicit_dates(text)


def parse_words(text):
    """把单词的 DeepSeek 输出解析成 3 个 [{word, pos, meaning, example, example_cn}]"""
    items = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    current = None
    for line in lines:
        if line[:1].isdigit() and len(line) > 1 and line[1] in ".、)）":
            # 新单词条目：1. 单词
            word = line[2:].strip()
            current = {"word": word, "pos": "", "meaning": "", "example": "", "example_cn": ""}
            items.append(current)
        elif current is not None:
            if line.startswith("词性") or line.startswith("词类"):
                current["pos"] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif line.startswith("例句翻译") or line.startswith("例翻译") or line.startswith("翻译"):
                current["example_cn"] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif line.startswith("例句") or line.startswith("例"):
                current["example"] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif line.startswith("中文") or line.startswith("释义"):
                current["meaning"] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif current["example"] == "" and current["meaning"] == "" and current["pos"] == "":
                # 未识别的行，先当词性/释义兜底
                current["meaning"] = line
    # 去掉空条目
    items = [it for it in items if it.get("word")]
    return items[:3]


# ============ 农历 / 节气 / 节日 ============
# 农历数据表：1900-2100 年，每年用一个整数编码（经典 lunar 算法数据）
# 高 4 位表示闰月月份（0=无闰月），低 20 位表示 12 个月大小月（1=大月30天，0=小月29天）
LUNAR_INFO = [
    0x04bd8, 0x04ae0, 0x0a570, 0x054d5, 0x0d260, 0x0d950, 0x16554, 0x056a0, 0x09ad0, 0x055d2,
    0x04ae0, 0x0a5b6, 0x0a4d0, 0x0d250, 0x1d255, 0x0b540, 0x0d6a0, 0x0ada2, 0x095b0, 0x14977,
    0x04970, 0x0a4b0, 0x0b4b5, 0x06a50, 0x06d40, 0x1ab54, 0x02b60, 0x09570, 0x052f2, 0x04970,
    0x06566, 0x0d4a0, 0x0ea50, 0x06e95, 0x05ad0, 0x02b60, 0x186e3, 0x092e0, 0x1c8d7, 0x0c950,
    0x0d4a0, 0x1d8a6, 0x0b550, 0x056a0, 0x1a5b4, 0x025d0, 0x092d0, 0x0d2b2, 0x0a950, 0x0b557,
    0x06ca0, 0x0b550, 0x15355, 0x04da0, 0x0a5b0, 0x14573, 0x052b0, 0x0a9a8, 0x0e950, 0x06aa0,
    0x0aea6, 0x0ab50, 0x04b60, 0x0aae4, 0x0a570, 0x05260, 0x0f263, 0x0d950, 0x05b57, 0x056a0,
    0x096d0, 0x04dd5, 0x04ad0, 0x0a4d0, 0x0d4d4, 0x0d250, 0x0d558, 0x0b540, 0x0b6a0, 0x195a6,
    0x095b0, 0x049b0, 0x0a974, 0x0a4b0, 0x0b27a, 0x06a50, 0x06d40, 0x0af46, 0x0ab60, 0x09570,
    0x04af5, 0x04970, 0x064b0, 0x074a3, 0x0ea50, 0x06b58, 0x055c0, 0x0ab60, 0x096d5, 0x092e0,
    0x0c960, 0x0d954, 0x0d4a0, 0x0da50, 0x07552, 0x056a0, 0x0abb7, 0x025d0, 0x092d0, 0x0cab5,
    0x0a950, 0x0b4a0, 0x0baa4, 0x0ad50, 0x055d9, 0x04ba0, 0x0a5b0, 0x15176, 0x052b0, 0x0a930,
    0x07954, 0x06aa0, 0x0ad50, 0x05b52, 0x04b60, 0x0a6e6, 0x0a4e0, 0x0d260, 0x0ea65, 0x0d530,
    0x05aa0, 0x076a3, 0x096d0, 0x04afb, 0x04ad0, 0x0a4d0, 0x1d0b6, 0x0d250, 0x0d520, 0x0dd45,
    0x0b5a0, 0x056d0, 0x055b2, 0x049b0, 0x0a577, 0x0a4b0, 0x0aa50, 0x1b255, 0x06d20, 0x0ada0,
    0x14b63, 0x09370, 0x049f8, 0x04970, 0x064b0, 0x168a6, 0x0ea50, 0x06b20, 0x1a6c4, 0x0aae0,
    0x0a2e0, 0x0d2e3, 0x0c960, 0x0d557, 0x0d4a0, 0x0da50, 0x05d55, 0x056a0, 0x0a6d0, 0x055d4,
    0x052d0, 0x0a9b8, 0x0a950, 0x0b4a0, 0x0b6a6, 0x0ad50, 0x055a0, 0x0aba4, 0x0a5b0, 0x052b0,
    0x0b273, 0x06930, 0x07337, 0x06aa0, 0x0ad50, 0x14b55, 0x04b60, 0x0a570, 0x054e4, 0x0d160,
    0x0e968, 0x0d520, 0x0daa0, 0x16aa6, 0x056d0, 0x04ae0, 0x0a9d4, 0x0a2d0, 0x0d150, 0x0f252,
    0x0d520,
]

# 农历月份名称
LUNAR_MONTHS = ["正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊"]
LUNAR_DAYS = [
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
]

# 24 节气日期（近似，每月两个，公历日期）
SOLAR_TERMS = {
    (1, 5): "小寒", (1, 20): "大寒",
    (2, 4): "立春", (2, 19): "雨水",
    (3, 5): "惊蛰", (3, 20): "春分",
    (4, 5): "清明", (4, 20): "谷雨",
    (5, 5): "立夏", (5, 21): "小满",
    (6, 5): "芒种", (6, 21): "夏至",
    (7, 7): "小暑", (7, 22): "大暑",
    (8, 7): "立秋", (8, 23): "处暑",
    (9, 7): "白露", (9, 23): "秋分",
    (10, 8): "寒露", (10, 23): "霜降",
    (11, 7): "立冬", (11, 22): "小雪",
    (12, 7): "大雪", (12, 21): "冬至",
}

# 24 节气简介（节气名 -> 简介）
SOLAR_TERM_INTRO = {
    "立春": "二十四节气之首，一般在公历 2 月 4 日前后，标志着春季开始、万物复苏，民间有「咬春」等习俗。",
    "雨水": "公历 2 月 19 日前后，降水增多、气温回升，草木萌动，进入春耕备耕时节。",
    "惊蛰": "公历 3 月 5 日前后，春雷始鸣、蛰虫苏醒，万物复苏，是春耕开始的重要节气。",
    "春分": "公历 3 月 20 日前后，昼夜平分，此后昼长夜短，气温回升，农事进入繁忙期。",
    "清明": "公历 4 月 5 日前后，气清景明、万物生长，是祭祖扫墓、踏青郊游的传统节日。",
    "谷雨": "公历 4 月 20 日前后，雨生百谷，降水明显增多，是播种移苗的最佳时节。",
    "立夏": "公历 5 月 5 日前后，夏季开始，万物繁茂，民间有「立夏称人」等习俗。",
    "小满": "公历 5 月 21 日前后，麦类等夏熟作物籽粒开始饱满但未成熟，故称「小满」。",
    "芒种": "公历 6 月 5 日前后，麦类等有芒作物成熟收割、夏播作物抢种，是农事最繁忙的节气。",
    "夏至": "公历 6 月 21 日前后，北半球白昼最长的一天，此后昼渐短夜渐长，标志盛夏来临。",
    "小暑": "公历 7 月 7 日前后，天气开始炎热，但尚未到最热，进入伏天的前奏。",
    "大暑": "公历 7 月 22 日前后，一年中最炎热的时节，高温酷暑，注意防暑降温。",
    "立秋": "公历 8 月 7 日前后，秋季开始，暑去凉来，但仍有「秋老虎」的余威。",
    "处暑": "公历 8 月 23 日前后，暑气渐消，天气转凉，是夏秋交替的节气。",
    "白露": "公历 9 月 7 日前后，天气转凉、露水增多，昼夜温差加大，进入仲秋。",
    "秋分": "公历 9 月 23 日前后，昼夜再次平分，此后昼短夜长，是秋收的重要时节。",
    "寒露": "公历 10 月 8 日前后，露水更凉、寒意渐浓，气温进一步下降。",
    "霜降": "公历 10 月 23 日前后，天气渐冷、初霜出现，是秋季的最后一个节气。",
    "立冬": "公历 11 月 7 日前后，冬季开始，万物收藏，民间有「补冬」的习俗。",
    "小雪": "公历 11 月 22 日前后，开始降雪但雪量不大，气温持续走低。",
    "大雪": "公历 12 月 7 日前后，降雪增多、雪量增大，进入深冬。",
    "冬至": "公历 12 月 21 日前后，北半球白昼最短的一天，此后昼渐长，民间有吃饺子、汤圆的习俗，素有「冬至大如年」之说。",
    "小寒": "公历 1 月 5 日前后，天气寒冷但未到极点，是进入严寒的标志。",
    "大寒": "公历 1 月 20 日前后，一年中最寒冷的时节，此后便是立春，冬去春来。",
}


def get_solar_term_intro(today):
    """返回当天节气 [{name, intro}]（若有）"""
    term = get_solar_term(today)
    if not term:
        return []
    intro = SOLAR_TERM_INTRO.get(term, "")
    return [{"name": term, "intro": intro}]


# ============ 节气精确计算（天文算法） ============
# 上面 SOLAR_TERMS 只是「近似日期」，用于兜底和牛顿迭代的初值。
# 实际节气每年会漂移 1~2 天（如冬至多在 12 月 21 日，2026 年是 12 月 22 日），
# 直接用固定日期会出现「该显示时没显示、不该显示时显示」的错位，因此这里按
# 「太阳视黄经」精确推算当年每个节气的时刻，再换算成北京时间取日期。

# 节气名 -> 太阳视黄经（度），春分为 0°
SOLAR_TERM_LONGITUDE = {
    "春分": 0, "清明": 15, "谷雨": 30, "立夏": 45, "小满": 60, "芒种": 75,
    "夏至": 90, "小暑": 105, "大暑": 120, "立秋": 135, "处暑": 150, "白露": 165,
    "秋分": 180, "寒露": 195, "霜降": 210, "立冬": 225, "小雪": 240, "大雪": 255,
    "冬至": 270, "小寒": 285, "大寒": 300, "立春": 315, "雨水": 330, "惊蛰": 345,
}
SOLAR_TERM_NAMES = set(SOLAR_TERM_LONGITUDE)
# 节气名 -> 近似（月, 日），作为迭代初值
_SOLAR_TERM_APPROX = {name: md for md, name in SOLAR_TERMS.items()}
_JD_EPOCH = dt.datetime(2000, 1, 1, 12)  # JD 2451545.0 对应 2000-01-01 12:00 UT
_SOLAR_TERM_CACHE = {}


def _sun_apparent_longitude(jd):
    """儒略日 -> 太阳视黄经（度）。Meeus 简化公式，精度约 0.01°（折算数分钟）。"""
    t = (jd - 2451545.0) / 36525.0
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = math.radians(357.52911 + 35999.05029 * t - 0.0001537 * t * t)
    c = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(m)
         + (0.019993 - 0.000101 * t) * math.sin(2 * m)
         + 0.000289 * math.sin(3 * m))
    omega = math.radians(125.04 - 1934.136 * t)
    return (l0 + c - 0.00569 - 0.00478 * math.sin(omega)) % 360.0


def _solar_term_date(year, name):
    """返回某年某节气在中国标准时间（UTC+8）下的日期。失败返回 None。"""
    key = (year, name)
    if key in _SOLAR_TERM_CACHE:
        return _SOLAR_TERM_CACHE[key]
    md = _SOLAR_TERM_APPROX.get(name)
    if not md:
        return None
    target = SOLAR_TERM_LONGITUDE[name]
    try:
        guess = dt.datetime(year, md[0], md[1])
        jd = 2451545.0 + (guess - _JD_EPOCH).total_seconds() / 86400.0
        for _ in range(10):
            diff = (target - _sun_apparent_longitude(jd) + 180.0) % 360.0 - 180.0
            if abs(diff) < 1e-7:
                break
            jd += diff / 0.98564736
        d = (_JD_EPOCH + dt.timedelta(days=jd - 2451545.0) + dt.timedelta(hours=8)).date()
    except Exception:
        d = dt.date(year, md[0], md[1])  # 计算异常时退回近似日期
    _SOLAR_TERM_CACHE[key] = d
    return d


def get_solar_term(today):
    """返回 today 当天的节气名（无则 None）。精确计算，带缓存。"""
    if not isinstance(today, dt.date):
        return None
    computed = False
    for name in SOLAR_TERM_LONGITUDE:
        d = _solar_term_date(today.year, name)
        if d is None:
            continue
        computed = True
        if d == today:
            return name
    # 只有在精确计算完全不可用时，才退回固定近似表
    if not computed:
        return SOLAR_TERMS.get((today.month, today.day))
    return None



# 浮动日期节日（公历）：规则为「某月第 N 个星期几」，日期每年变动，需动态计算。
# 结构：[(节日名, 简介, 月份, 第几个(1=第一个), 星期几(0=周一...6=周日))]
FLOATING_FESTIVALS = [
    ("感恩节", "美国传统节日，定于每年 11 月的第四个星期四。源于 1621 年普利茅斯清教徒为感谢丰收举行的盛宴，1863 年林肯总统正式定为全国性节日，有吃火鸡、家庭团聚、纽约梅西感恩节大游行等习俗。", 11, 4, 3),
    ("母亲节", "现代母亲节由美国人安娜·贾维斯倡议，1914 年美国国会将 5 月的第二个星期日定为母亲节，后传遍世界，感恩母亲的养育之恩。", 5, 2, 6),
    ("父亲节", "1909 年由美国人索诺拉·多德倡议，纪念其父亲，1910 年首次庆祝，后定于 6 月的第三个星期日，感恩父爱。", 6, 3, 6),
    ("总统日", "美国联邦假日，定于每年 2 月的第三个星期一，纪念首任总统乔治·华盛顿诞辰（2 月 22 日），也常一并纪念林肯。", 2, 3, 0),
    ("马丁·路德·金纪念日", "美国联邦假日，定于每年 1 月的第三个星期一，纪念 1986 年设立的民权运动领袖马丁·路德·金诞辰（1929 年 1 月 15 日）。", 1, 3, 0),
    ("哥伦布日", "美国联邦假日，定于每年 10 月的第二个星期一，纪念 1492 年 10 月 12 日哥伦布抵达美洲。", 10, 2, 0),
    ("阵亡将士纪念日", "美国联邦假日，定于每年 5 月的最后一个星期一，缅怀为国捐躯的军人，源于南北战争后悼念阵亡将士。", 5, 0, 0),
    ("劳动节（美国）", "美国与加拿大的劳动节定于每年 9 月的第一个星期一，源于 19 世纪工人争取权益的运动，与 5 月 1 日国际劳动节不同。", 9, 1, 0),
]


def _nth_weekday(year, month, nth, weekday):
    """返回某年某月第 nth 个 weekday（星期几）的公历日。
    nth 从 1 开始（0 表示该月最后一个）；weekday 0=周一...6=周日。返回 datetime.date 或 None。"""
    import calendar as _cal
    if nth == 0:
        # 最后一个 weekday
        last_day = _cal.monthrange(year, month)[1]
        last_weekday = _cal.weekday(year, month, last_day)  # 0=周一
        offset = (last_weekday - weekday) % 7
        return dt.date(year, month, last_day - offset)
    first_weekday = _cal.monthrange(year, month)[0]  # 0=周一
    offset = (weekday - first_weekday) % 7
    day = 1 + offset + (nth - 1) * 7
    if day > _cal.monthrange(year, month)[1]:
        return None
    return dt.date(year, month, day)


def _easter_date(year):
    """用「匿名算法」（Meeus/Jones/Butcher）计算复活节星期日的日期。
    复活节是春分月圆后的第一个星期日，天主教/新教通行此算法。返回 datetime.date。"""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


# 复活节相关的浮动节日（日期 = 复活节 ± 若干天）
EASTER_RELATED = [
    ("复活节", "基督教最重要的节日，纪念耶稣基督复活，定于每年春分月圆后的第一个星期日（公历 3 月 22 日至 4 月 25 日之间），象征重生与希望。", 0),
    ("耶稣受难日", "基督教节日，纪念耶稣被钉十字架受难，定于复活节前的星期五，是西方多个国家的重要假日。", -2),
    ("圣灰星期三", "基督教大斋期（四旬期）的开始，定于复活节前 46 天（星期三），信徒以撒灰表示忏悔。", -46),
]


def get_floating_festivals(today):
    """返回当天对应的浮动日期节日 [{name, intro}]"""
    result = []
    for name, intro, month, nth, weekday in FLOATING_FESTIVALS:
        d = _nth_weekday(today.year, month, nth, weekday)
        if d and d.month == today.month and d.day == today.day:
            result.append({"name": name, "intro": intro})
    # 复活节及派生节日
    easter = _easter_date(today.year)
    for name, intro, delta in EASTER_RELATED:
        d = easter + dt.timedelta(days=delta)
        if d.month == today.month and d.day == today.day:
            result.append({"name": name, "intro": intro})
    return result


# 公历节日/纪念日「权威注册表」（月, 日）-> [(节日名, 简介), ...]
# 仅保留公认、无争议、重要的节日，日期与设立信息均已联网核对（来源：中国政府网/国防部/联合国官网等）。
# 结构：每个节日是 (名称, 简介) 元组。
SOLAR_FESTIVALS = {
    (1, 1): [
        ("元旦", "公历 1 月 1 日，即新年。1912 年中华民国改用公历纪年后，以 1 月 1 日为「元旦」；1949 年新中国沿用，现为全国法定节假日。"),
    ],
    (1, 10): [
        ("中国人民警察节", "2020 年国务院批准，自 2021 年起将每年 1 月 10 日设立为「中国人民警察节」，致敬人民警察队伍。"),
    ],
    (2, 2): [
        ("世界湿地日", "纪念 1971 年 2 月 2 日《湿地公约》在伊朗拉姆萨尔签署，1996 年《湿地公约》常委会确定 2 月 2 日为世界湿地日，呼吁保护湿地生态。"),
    ],
    (2, 4): [
        ("世界癌症日", "2000 年国际抗癌联盟（UICC）发起，定于 2 月 4 日，倡导癌症预防、早发现和治疗，减轻全球癌症负担。"),
    ],
    (2, 7): [
        ("二七纪念日", "纪念 1923 年 2 月 7 日京汉铁路工人大罢工（「二七大罢工」），是中国工人运动史上的重大事件。"),
    ],
    (2, 14): [
        ("情人节", "西方传统节日，源于公元 3 世纪罗马圣瓦伦丁殉道传说，后演变为情侣互表爱意的日子，现已在全球流行。"),
    ],
    (3, 5): [
        ("学雷锋纪念日", "1963 年 3 月 5 日毛泽东题词「向雷锋同志学习」发表，此后将每年 3 月 5 日定为学雷锋纪念日，弘扬雷锋精神。"),
    ],
    (3, 8): [
        ("国际劳动妇女节", "为纪念 1909 年 3 月 8 日美国芝加哥女工为争取平等权利举行罢工示威而设立，1910 年第二届国际社会主义妇女代表会议确定 3 月 8 日为国际妇女节；1977 年联合国大会确认。"),
    ],
    (3, 12): [
        ("植树节", "1979 年 2 月全国人大常委会决定，将 3 月 12 日（孙中山逝世纪念日）定为中国植树节，号召全民义务植树。"),
    ],
    (3, 15): [
        ("国际消费者权益日", "1983 年国际消费者联盟确定 3 月 15 日为国际消费者权益日，纪念 1962 年美国总统肯尼迪提出消费者四项基本权利。"),
    ],
    (3, 21): [
        ("世界睡眠日", "2001 年国际精神卫生和神经科学基金会发起，定于 3 月 21 日（春分前后），呼吁关注睡眠健康与睡眠质量。"),
    ],
    (3, 22): [
        ("世界水日", "1993 年联合国大会确立 3 月 22 日为世界水日，唤起公众节水意识、加强水资源保护。"),
    ],
    (3, 23): [
        ("世界气象日", "纪念 1950 年世界气象组织公约生效，1960 年起每年 3 月 23 日庆祝，关注气象与气候变化。"),
    ],
    (3, 24): [
        ("世界防治结核病日", "纪念 1882 年 3 月 24 日德国科学家科赫发现结核杆菌，世界卫生组织 1995 年设立，推动结核病防治。"),
    ],
    (4, 1): [
        ("愚人节", "西方传统节日，4 月 1 日人们相互开玩笑、恶作剧。起源说法不一，现已在世界多地流行。"),
    ],
    (4, 7): [
        ("世界卫生日", "1948 年世界卫生组织（WHO）成立，将 4 月 7 日定为世界卫生日，每年聚焦一个全球公共卫生主题。"),
    ],
    (4, 15): [
        ("全民国家安全教育日", "2015 年全国人大常委会通过《国家安全法》，将每年 4 月 15 日定为全民国家安全教育日，增强全民国家安全意识。"),
    ],
    (4, 22): [
        ("世界地球日", "1970 年 4 月 22 日美国发起首次大规模环保运动，现已成为全球规模最大的环保节日，呼吁保护地球生态环境。"),
    ],
    (4, 23): [
        ("世界读书日", "1995 年联合国教科文组织确立 4 月 23 日为「世界图书与版权日」，纪念塞万提斯、莎士比亚等文豪，倡导全民阅读。"),
    ],
    (4, 26): [
        ("世界知识产权日", "2000 年世界知识产权组织（WIPO）设立 4 月 26 日为世界知识产权日，倡导尊重知识、崇尚创新、保护知识产权。"),
    ],
    (5, 1): [
        ("国际劳动节", "纪念 1886 年 5 月 1 日美国芝加哥工人为争取八小时工作制举行的大罢工，1889 年第二国际成立大会确定 5 月 1 日为国际劳动节；中国 1949 年起定为法定假日。"),
    ],
    (5, 4): [
        ("青年节", "为纪念 1919 年 5 月 4 日五四爱国运动而设立，1949 年中央人民政府政务院正式宣布 5 月 4 日为中国青年节。"),
    ],
    (5, 8): [
        ("世界红十字日", "纪念红十字运动创始人亨利·杜南诞辰（1828 年 5 月 8 日），1948 年将 5 月 8 日定为世界红十字日，弘扬人道、博爱、奉献精神。"),
    ],
    (5, 12): [
        ("国际护士节", "纪念现代护理学创始人南丁格尔诞辰（1820 年 5 月 12 日），1912 年设立，致敬全球护理工作者。"),
        ("全国防灾减灾日", "2009 年国务院批准，将 5 月 12 日（2008 年汶川地震发生日）定为全国防灾减灾日，增强全民防灾减灾意识。"),
    ],
    (5, 15): [
        ("国际家庭日", "1993 年联合国大会确立 5 月 15 日为国际家庭日，强调家庭在社会发展中的重要作用。"),
    ],
    (5, 18): [
        ("国际博物馆日", "1977 年国际博物馆协会发起，定于 5 月 18 日，促进博物馆事业发展与公众文化交流。"),
    ],
    (5, 30): [
        ("五卅纪念日", "纪念 1925 年 5 月 30 日上海「五卅运动」，是中国工人运动和反帝爱国运动的重要事件。"),
    ],
    (5, 31): [
        ("世界无烟日", "1987 年世界卫生组织设立，1989 年起定于 5 月 31 日，倡导戒烟控烟、减少烟草危害。"),
    ],
    (6, 1): [
        ("国际儿童节", "1949 年 11 月国际民主妇女联合会在莫斯科会议决定 6 月 1 日为国际儿童节，保障儿童权利；中国 1949 年 12 月起实行。"),
    ],
    (6, 5): [
        ("世界环境日", "1972 年联合国人类环境会议确定 6 月 5 日为世界环境日，呼吁全球关注并保护地球环境。"),
    ],
    (6, 6): [
        ("全国爱眼日", "1996 年卫生部等 12 部门联合确定 6 月 6 日为全国爱眼日，普及科学用眼、保护视力。"),
    ],
    (6, 8): [
        ("世界海洋日", "2008 年联合国大会确立 6 月 8 日为世界海洋日，呼吁保护海洋生态、可持续利用海洋资源。"),
    ],
    (6, 14): [
        ("世界献血者日", "2004 年世界卫生组织等联合设立 6 月 14 日（发现 ABO 血型的兰德斯泰纳诞辰日）为世界献血者日，感谢无偿献血者。"),
    ],
    (6, 23): [
        ("国际奥林匹克日", "纪念 1894 年 6 月 23 日国际奥委会在巴黎成立，1948 年设立，弘扬奥林匹克精神。"),
    ],
    (6, 26): [
        ("国际禁毒日", "1987 年联合国将 6 月 26 日定为「禁止药物滥用和非法贩运国际日」（国际禁毒日），呼吁抵制毒品。"),
    ],
    (7, 1): [
        ("建党节", "纪念 1921 年 7 月中国共产党第一次全国代表大会召开（一大 7 月 23 日开幕，习惯以 7 月 1 日为党的诞生纪念日），1941 年中共中央正式确立 7 月 1 日为建党纪念日。"),
        ("香港回归纪念日", "1997 年 7 月 1 日香港回归祖国，结束英国殖民统治，此后每年 7 月 1 日为香港回归纪念日。"),
    ],
    (7, 7): [
        ("七七事变纪念日", "纪念 1937 年 7 月 7 日卢沟桥事变，日本发动全面侵华战争，中国全民族抗战由此开始。"),
    ],
    (7, 11): [
        ("世界人口日", "1989 年联合国开发计划署提议，1990 年联合国将 7 月 11 日定为世界人口日，关注人口问题与可持续发展。"),
    ],
    (8, 1): [
        ("建军节", "纪念 1927 年 8 月 1 日南昌起义，1933 年中华苏维埃共和国临时中央政府将 8 月 1 日定为中国工农红军成立纪念日，后定为中国人民解放军建军节。"),
    ],
    (8, 19): [
        ("中国医师节", "2017 年国务院批复，自 2018 年起将每年 8 月 19 日设立为「中国医师节」，致敬广大医师。"),
    ],
    (9, 3): [
        ("中国人民抗日战争胜利纪念日", "1945 年 9 月 2 日日本签署投降书，2014 年全国人大常委会将 9 月 3 日定为中国人民抗日战争胜利纪念日暨世界反法西斯战争胜利纪念日。"),
    ],
    (9, 10): [
        ("教师节", "1985 年第六届全国人大常委会确定 9 月 10 日为教师节，尊师重教、感恩教师。"),
    ],
    (9, 18): [
        ("九一八事变纪念日", "纪念 1931 年 9 月 18 日日本发动九一八事变、侵占中国东北，中国人民抗日战争由此开始。"),
    ],
    (9, 21): [
        ("国际和平日", "1981 年联合国设立，2001 年起定于 9 月 21 日，呼吁全球停火、倡导和平与非暴力。"),
    ],
    (9, 27): [
        ("世界旅游日", "1970 年国际官方旅游组织联盟确立 9 月 27 日为世界旅游日，促进全球旅游业发展。"),
    ],
    (9, 30): [
        ("烈士纪念日", "2014 年全国人大常委会将 9 月 30 日（国庆前夕）定为烈士纪念日，缅怀为国家牺牲的烈士。"),
    ],
    (10, 1): [
        ("国庆节", "1949 年 10 月 1 日中华人民共和国成立，此后每年 10 月 1 日为国庆节，是举国同庆的重要法定节日。"),
    ],
    (10, 4): [
        ("世界动物日", "为纪念方济各（动物守护圣人），1931 年起定 10 月 4 日为世界动物日，呼吁保护动物、尊重生命。"),
    ],
    (10, 10): [
        ("辛亥革命纪念日", "纪念 1911 年 10 月 10 日武昌起义，辛亥革命推翻清王朝、终结中国两千多年的封建帝制。"),
    ],
    (10, 16): [
        ("世界粮食日", "1979 年联合国粮农组织设立 10 月 16 日为世界粮食日，关注粮食安全、消除饥饿。"),
    ],
    (10, 24): [
        ("联合国日", "纪念 1945 年 10 月 24 日《联合国宪章》正式生效、联合国成立，此后每年 10 月 24 日为联合国日。"),
    ],
    (11, 8): [
        ("记者节", "2000 年国务院批准将 11 月 8 日定为记者节（纪念 1937 年 11 月 8 日范长江创立中国青年新闻记者协会），致敬新闻工作者。"),
    ],
    (11, 9): [
        ("全国消防日", "因火警电话号码「119」与 11 月 9 日谐音，1992 年起公安部将 11 月 9 日定为全国消防日，开展消防安全宣传、增强全民防火意识。"),
    ],
    (11, 11): [
        ("退伍军人节", "美国联邦假日，纪念 1918 年 11 月 11 日第一次世界大战停战（「一战停战日」），致敬所有退伍军人。"),
    ],
    (11, 14): [
        ("世界糖尿病日", "1991 年国际糖尿病联盟与世界卫生组织设立，定于 11 月 14 日（胰岛素发现者班廷诞辰日），关注糖尿病防治。"),
    ],
    (11, 17): [
        ("国际大学生节", "1946 年国际学生联合会确立 11 月 17 日为国际大学生节，纪念反法西斯的学生运动，倡导和平与进步。"),
    ],
    (11, 20): [
        ("世界儿童日", "1954 年联合国设立，定于 11 月 20 日，纪念《儿童权利宣言》与《儿童权利公约》通过，呼吁保障儿童权利。"),
    ],
    (11, 21): [
        ("世界问候日", "1973 年 11 月 21 日，第四次中东战争期间，澳大利亚的姆可马克与米切尔两兄弟自费印发宣传材料，呼吁以问候传递和平、以沟通替代冲突，世界问候日由此诞生，现已有 140 多个国家响应。"),
        ("世界电视日", "1996 年 12 月 18 日第 51 届联合国大会通过决议，将首届世界电视论坛召开日 11 月 21 日定为世界电视日，强调电视在传播信息与文化中的重要作用。"),
        ("世界渔业日", "1997 年 11 月 21 日，来自 32 个国家的小规模渔业工作者在印度新德里首次聚集，成立「世界渔业论坛」并签署宣言，此后每年 11 月 21 日为世界渔业日，呼吁关注渔业资源可持续发展、反对过度捕捞与非法捕鱼、维护渔民权益。"),
    ],
    (11, 22): [
        ("世界针灸日", "2011 年，世界针灸学会联合会（WFAS）将 11 月 22 日定为世界针灸日（其前一周为世界针灸周），旨在推广中医针灸这一传统医学，弘扬针灸在防病治病中的作用；中医针灸已于 2010 年列入联合国教科文组织人类非物质文化遗产代表作名录。"),
    ],
    (12, 1): [
        ("世界艾滋病日", "1988 年世界卫生组织将 12 月 1 日定为世界艾滋病日，提高公众对艾滋病的认识、反对歧视。"),
    ],
    (12, 3): [
        ("国际残疾人日", "1992 年联合国大会确立 12 月 3 日为国际残疾人日，促进残疾人权利与福祉。"),
    ],
    (12, 10): [
        ("世界人权日", "纪念 1948 年 12 月 10 日联合国大会通过《世界人权宣言》，捍卫普世人权与基本自由。"),
    ],
    (12, 13): [
        ("南京大屠杀死难者国家公祭日", "2014 年全国人大常委会将 12 月 13 日定为南京大屠杀死难者国家公祭日，悼念 1937 年南京大屠杀遇难同胞。"),
    ],
    (12, 20): [
        ("澳门回归纪念日", "1999 年 12 月 20 日澳门回归祖国，结束葡萄牙殖民统治，此后每年 12 月 20 日为澳门回归纪念日。"),
    ],
    (12, 25): [
        ("圣诞节", "12 月 25 日，基督教纪念耶稣诞生的节日，现已演变为全球性的文化节庆。"),
    ],
}


def lunar_date(year, month, day):
    """公历转农历，返回 (农历月, 农历日, 是否闰月, 农历月名, 农历日名)"""
    # 1900-01-31 是农历 1900 年正月初一
    base = dt.date(1900, 1, 31)
    target = dt.date(year, month, day)
    offset = (target - base).days

    # 定位农历年份
    i = 1900
    while i < 2100 and offset >= _lunar_year_days(i):
        offset -= _lunar_year_days(i)
        i += 1
    lunar_year = i

    # 定位农历月
    leap_month = LUNAR_INFO[lunar_year - 1900] & 0xf  # 低 4 位：闰月月份（0=无闰月）
    leap_month = leap_month - 1 if leap_month else -1  # 转 0-based，-1 表示无闰月

    lunar_month = 0
    is_leap = False
    while lunar_month < 12:
        days = _lunar_month_days(lunar_year, lunar_month + 1, is_leap)
        if offset < days:
            break
        offset -= days
        if not is_leap and lunar_month == leap_month:
            is_leap = True
        else:
            is_leap = False
            lunar_month += 1

    lunar_day = offset + 1
    real_month = lunar_month + 1
    month_name = ("闰" + LUNAR_MONTHS[real_month - 1]) if is_leap else LUNAR_MONTHS[real_month - 1]
    day_name = LUNAR_DAYS[lunar_day - 1]
    return (real_month, lunar_day, is_leap, month_name, day_name)


def _lunar_year_days(year):
    """某农历年的总天数"""
    info = LUNAR_INFO[year - 1900]
    days = 0
    # bit 15 ~ bit 4 对应 12 个月大小（1=30天，0=29天）
    for i in range(12):
        days += 30 if (info & (0x8000 >> i)) else 29
    # 闰月
    leap_month = info & 0xf
    if leap_month:
        days += 30 if (info & 0x10000) else 29
    return days


def _lunar_month_days(year, month, is_leap):
    """某农历月天数；month 为 1-based"""
    info = LUNAR_INFO[year - 1900]
    if not is_leap:
        return 30 if (info & (0x8000 >> (month - 1))) else 29
    leap_month = info & 0xf
    if leap_month == month:
        return 30 if (info & 0x10000) else 29
    return 29


# 农历传统节日（农历月, 农历日）-> [(节日名, 简介), ...]
# 日期固定（如正月初一=春节），不会因年份变动，简介为公认准确信息。
LUNAR_FESTIVALS = {
    (1, 1): [
        ("春节", "农历正月初一，中国最重要的传统节日，俗称「过年」。源于上古岁首祈年祭祀，已有数千年历史，是阖家团圆、辞旧迎新的日子，现为全国法定节假日。"),
    ],
    (1, 15): [
        ("元宵节", "农历正月十五，又称上元节、灯节，是春节后的第一个月圆之夜，有赏花灯、猜灯谜、吃元宵（汤圆）等习俗，象征团圆美满。"),
    ],
    (2, 2): [
        ("龙抬头", "农历二月初二，又称春龙节、青龙节，「二月二，龙抬头」，有剃龙头、吃龙须面等习俗，象征春回大地、万物复苏。"),
    ],
    (3, 3): [
        ("上巳节", "农历三月初三，古称「三月三」，是汉族传统节日，有踏青、沐浴、曲水流觞等习俗，也是多个少数民族的重要节日。"),
    ],
    (5, 5): [
        ("端午节", "农历五月初五，为纪念战国时期楚国诗人屈原（亦有纪念伍子胥、曹娥等说法）而设，有赛龙舟、吃粽子、挂艾草、佩香囊等习俗，2009 年列入联合国非遗名录。"),
    ],
    (7, 7): [
        ("七夕节", "农历七月初七，又称乞巧节，源于牛郎织女的传说，是中国传统的情人节，有乞巧、拜织女等习俗。"),
    ],
    (7, 15): [
        ("中元节", "农历七月十五，又称盂兰盆节、鬼节，民间祭祖、普度的传统节日，表达对祖先的追思。"),
    ],
    (8, 15): [
        ("中秋节", "农历八月十五，象征团圆的传统节日，有赏月、吃月饼、燃灯等习俗，与春节、清明、端午并称中国四大传统节日，现为法定节假日。"),
    ],
    (9, 9): [
        ("重阳节", "农历九月初九，又称登高节、敬老节，「九九」谐音「久久」寓意长久，有登高、赏菊、插茱萸、敬老等习俗，1989 年中国将其定为老人节。"),
    ],
    (10, 1): [
        ("寒衣节", "农历十月初一，又称「十月朝」「祭祖节」，民间有烧寒衣、祭祖的习俗，是北方地区重要的祭祖节日，与清明、中元并称「三大鬼节」。"),
    ],
    (10, 15): [
        ("下元节", "农历十月十五，道教水官解厄之日，与上元节（元宵）、中元节并称「三元」。民间有祭水官、祈福禳灾的习俗，现已较冷门。"),
    ],
    (12, 8): [
        ("腊八节", "农历腊月初八，源于佛教释迦牟尼成道日，民间有喝腊八粥、腌腊八蒜的习俗，标志着进入年关。"),
    ],
    (12, 23): [
        ("小年", "农历腊月二十三（北方）或二十四（南方），又称「祭灶节」，民间祭灶神、扫尘、准备年货，标志着春节的序幕正式拉开。"),
    ],
}


def get_lunar_festivals(today):
    """根据公历日期，返回当天对应的农历传统节日列表 [{name, intro}]"""
    result = []
    try:
        ln = lunar_date(today.year, today.month, today.day)
        if ln:
            lm, ld = ln[0], ln[1]  # 农历月、日
            for name, intro in LUNAR_FESTIVALS.get((lm, ld), []):
                result.append({"name": name, "intro": intro})
            # 除夕：农历腊月最后一天（腊月三十或廿九），即「下一天是正月初一」
            nxt = today + dt.timedelta(days=1)
            ln_next = lunar_date(nxt.year, nxt.month, nxt.day)
            if ln_next and ln_next[0] == 1 and ln_next[1] == 1:
                result.append({
                    "name": "除夕",
                    "intro": "农历年的最后一天（腊月三十或廿九），俗称「大年三十」，是辞旧迎新、全家团聚吃年夜饭、守岁的重要日子，也是春节的前夜。",
                })
    except Exception:
        pass
    return result


def get_date_info(today):
    """根据公历日期返回「农历日期 + 节气」信息字符串（不含节日，节日单独走简介区块）"""
    parts = []
    # 农历（阴历）
    try:
        y, m, d = today.year, today.month, today.day
        ln = lunar_date(y, m, d)
        if ln:
            parts.append("农历{}月{}".format(ln[3], ln[4]))
    except Exception:
        pass
    # 节气（精确计算，逐年 1~2 天漂移）
    term = get_solar_term(today)
    if term:
        parts.append(term)
    return " · ".join(parts)


def _resolve_task_name(event):
    """从事件里解析 task 名，兼容三种格式：
    1. 控制台测试事件：{"task":"words"}
    2. 腾讯云定时触发器：{"Type":"Timer","TriggerName":"words","Message":"{\\"task\\":\\"words\\"}"}
       —— 自定义内容在 Message 里，且是字符串
    3. 事件本身是 JSON 字符串：'{task:words}'
    取不到或非法时回退 "news"。
    """
    candidates = []
    if isinstance(event, dict):
        if event.get("task"):
            candidates.append(event.get("task"))
        for k in ("Message", "message", "msg"):
            if event.get(k):
                candidates.append(event.get(k))
        # 触发器名为 news/words/history 时也可作为兜底线索
        trig = event.get("TriggerName") or event.get("triggerName")
        if trig:
            candidates.append(trig)
    elif isinstance(event, str):
        candidates.append(event)

    for c in candidates:
        if isinstance(c, dict):
            t = c.get("task")
            if t and str(t).strip() in TASKS:
                return str(t).strip()
        elif isinstance(c, str):
            s = c.strip()
            if not s:
                continue
            if s in TASKS:
                return s
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    t = obj.get("task")
                    if t and str(t).strip() in TASKS:
                        return str(t).strip()
                    trig = obj.get("TriggerName") or obj.get("triggerName")
                    if trig and str(trig).strip() in TASKS:
                        return str(trig).strip()
            except Exception:
                pass
    return "news"


def main_handler(event, context):
    webhook = os.getenv("WECOM_WEBHOOK", "")
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    secret_id = os.getenv("TENCENT_SECRET_ID", "")
    secret_key = os.getenv("TENCENT_SECRET_KEY", "")
    bucket = os.getenv("COS_BUCKET", "")
    region = os.getenv("COS_REGION", "ap-shanghai")

    missing = [n for n, v in [
        ("WECOM_WEBHOOK", webhook), ("DEEPSEEK_API_KEY", api_key),
        ("TENCENT_SECRET_ID", secret_id), ("TENCENT_SECRET_KEY", secret_key),
        ("COS_BUCKET", bucket),
    ] if not v]
    if missing:
        return {"errcode": -1, "errmsg": "缺少环境变量: " + ",".join(missing)}

    # 用北京时间（UTC+8）计算今天，避免云函数 UTC 时区导致日期偏差
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d")
    today_date = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    today_cn = today_date.strftime("%Y年%m月%d日")
    # 农历 + 节气 + 节日信息
    date_info = get_date_info(today_date)

    # 0. 确定任务（兼容定时触发器/控制台测试/纯字符串三种事件格式）
    task_name = _resolve_task_name(event)
    task = TASKS[task_name]

    # 1. 生成内容（替换 prompt 中的日期占位符）
    prompt = task["prompt"].format(
        date_cn=today_cn,
        month=today_date.month,
        day=today_date.day,
    )
    try:
        content_text = call_deepseek(api_key, prompt)
    except Exception as e:
        return {"errcode": -1, "errmsg": "DeepSeek 调用失败: " + str(e)}

    # 单词板块走独立逻辑（无配图，含例句+读音）
    if task_name == "words":
        words = parse_words(content_text)

        # 词库去重：读取已用单词，过滤掉重复的，并补充新单词
        bank = load_word_bank(secret_id, secret_key, bucket, region)
        used_words = set(w["word"].lower() for w in bank.get("words", []) if w.get("word"))
        deduped = [w for w in words if w.get("word") and w["word"].lower() not in used_words]

        # 若去重后不足 3 个，则重新请求 DeepSeek 补充（最多重试 3 次）
        attempt = 0
        while len(deduped) < 3 and attempt < 3:
            attempt += 1
            # 在 prompt 里附带已用单词，让模型避开
            avoid = ", ".join(sorted(used_words))
            extra_prompt = task["prompt"] + "\n\n注意：以下单词已经推送过，请务必避开、不要重复：\n" + avoid
            try:
                extra_text = call_deepseek(api_key, extra_prompt)
            except Exception:
                break
            extra_words = parse_words(extra_text)
            for w in extra_words:
                if w.get("word") and w["word"].lower() not in used_words and len(deduped) < 3:
                    deduped.append(w)
                    used_words.add(w["word"].lower())

        words = deduped[:3]

        # 把本次推送的单词写回词库（完整对象，带日期）
        existing = {w["word"].lower(): w for w in bank.get("words", []) if w.get("word")}
        for w in words:
            if w.get("word"):
                wl = w["word"].lower()
                existing[wl] = {
                    "word": w["word"],
                    "pos": w.get("pos", ""),
                    "meaning": w.get("meaning", ""),
                    "example": w.get("example", ""),
                    "example_cn": w.get("example_cn", ""),
                    "date": today,
                }
        bank["words"] = [existing[k] for k in sorted(existing.keys())]

        # 补全旧单词缺失的词性/释义/例句/例句翻译
        incomplete = [w for w in bank["words"] if not (w.get("pos") and w.get("meaning") and w.get("example") and w.get("example_cn"))]
        if incomplete:
            inc_words = [w["word"] for w in incomplete]
            fill_prompt = (
                "请为以下英语单词补充信息，每个单词严格按 5 行格式输出：\n"
                "1. 单词\n词性：n.\n中文：xxx\n例句：This is an example.\n例句翻译：这是一个例句。\n\n"
                "词性用英文简写（n./v./adj./adv. 等）。单词列表：\n" + ", ".join(inc_words)
            )
            try:
                fill_text = call_deepseek(api_key, fill_prompt)
                fill_items = parse_words(fill_text)
                fill_map = {it["word"].lower(): it for it in fill_items if it.get("word")}
                for w in bank["words"]:
                    wl = w["word"].lower()
                    if wl in fill_map and not (w.get("pos") and w.get("meaning") and w.get("example") and w.get("example_cn")):
                        fi = fill_map[wl]
                        w["pos"] = w.get("pos") or fi.get("pos", "")
                        w["meaning"] = w.get("meaning") or fi.get("meaning", "")
                        w["example"] = w.get("example") or fi.get("example", "")
                        w["example_cn"] = w.get("example_cn") or fi.get("example_cn", "")
            except Exception:
                pass  # 补全失败不阻断

        bank["count"] = len(bank["words"])
        try:
            save_word_bank(secret_id, secret_key, bucket, region, bank)
        except Exception:
            pass  # 词库保存失败不影响本次推送

        # 生成今日单词网页（词库内嵌，底部折叠面板）
        html = build_words_html(words, task["title"], today, bank=bank)
        key = "{}/{}.html".format(task["dir"], today)
        try:
            status, public_url = cos_put_object(
                secret_id, secret_key, bucket, region, key,
                html.encode("utf-8"), "text/html; charset=utf-8"
            )
        except Exception as e:
            return {"errcode": -1, "errmsg": "COS 上传失败: " + str(e)}

        # 推送摘要：只保留单词 + 词性 + 中文翻译，只有一个链接（今日页面）
        ts = int(time.time())
        link_url = "{}?t={}".format(public_url, ts)
        summary_lines = ["**{}**（{}）".format(task["title"], today)]
        for i, w in enumerate(words, 1):
            line = "{}. {} {}".format(i, w.get("word", ""), w.get("pos", ""))
            if w.get("meaning"):
                line += " — {}".format(w["meaning"])
            summary_lines.append(line)
        summary_lines.append("")
        summary_lines.append("[点击查看例句 + 发音 + 词库 >>]({})".format(link_url))
        push_content = "\n".join(summary_lines)

        try:
            result = push_wecom(webhook, push_content)
        except Exception as e:
            return {"errcode": -1, "errmsg": "推送失败: " + str(e)}

        return {"errcode": result.get("errcode"), "errmsg": result.get("errmsg"),
                "url": public_url, "word_bank_count": bank["count"]}

    # 新闻/历史板块：带图网页
    items = parse_news(content_text, default_title=task["title"])

    # 解析当天节日/纪念日：农历传统节日 + 24节气 + 权威注册表优先 + DeepSeek 生成（经日期校验）兜底
    if task_name == "history":
        deepseek_festivals = parse_festivals(content_text)
        reg_fests = []
        for f in merge_festivals(deepseek_festivals, today_date):
            d = dict(f)
            d["_tier"] = _fest_tier(d.get("name", ""))
            reg_fests.append(d)
        # 各来源带上重要性层级：0 传统民俗 > 1 节气 > 2 国家政治纪念日 > 3 国际主题日 > 4 其他
        lunar_fests = [dict(f, _tier=0) for f in get_lunar_festivals(today_date)]
        term_fests = [dict(f, _tier=1) for f in get_solar_term_intro(today_date)]
        float_fests = [dict(f, _tier=4) for f in get_floating_festivals(today_date)]
        ordered = lunar_fests + term_fests + float_fests + reg_fests
        # 归一化去重：只有真的撞名才合并，名字用权威叫法、简介用 DeepSeek 那份
        fests = _dedupe_festivals(ordered)
        # 按重要程度从高到低排（稳定排序，同级保持来源顺序），最多展示 4 条
        fests.sort(key=lambda f: f.get("_tier", 9))
        festivals_info = [
            {"name": f["name"], "intro": f["intro"]} for f in fests[:4]
        ]
    else:
        festivals_info = []

    # 2. 每条必搜 1 张图（多级兜底，保证一定有图）：
    #    历史板块：Wikimedia 历史照片(强相关) → Unsplash 精准词 → Unsplash 标题词 → Unsplash 默认词
    #    非历史板块：Unsplash 精准词 → 标题词 → 默认词
    unsplash_key = os.getenv("UNSPLASH_ACCESS_KEY", "")
    used_md5 = set()  # 已用图片的 MD5，用于去重
    for i, item in enumerate(items):
        item["imgs_b64"] = []
        kws = item.get("img_kws") or []
        kw = kws[0].strip() if kws else ""

        # 构造候选关键词列表（按优先级：精准词 → 标题核心词 → 默认词）
        cand_kws = []
        if kw:
            cand_kws.append(kw)
        # 标题作为降级词（去掉「年份」前缀，取事件名称部分）
        title_kw = item.get("title", "").strip()
        if title_kw:
            # 去掉类似「1783年 」的前缀，保留事件名
            import re as _re
            m = _re.match(r"^\d{3,4}年\s*(.*)$", title_kw)
            if m:
                title_kw = m.group(1).strip()
            if title_kw and title_kw not in cand_kws:
                cand_kws.append(title_kw)
        if task.get("default_kw") and task["default_kw"] not in cand_kws:
            cand_kws.append(task["default_kw"])

        # 逐级尝试搜图，直到拿到一张成功下载且不重复的图
        src = ""
        got = False
        for ck in cand_kws:
            if got:
                break
            # 历史板块：每个关键词都先试 Wikimedia，再试 Unsplash
            if task_name == "history":
                urls = get_wikimedia_image_urls(ck, limit=3)
                for u in urls:
                    img_bytes, ctype = download_image(u)
                    if not img_bytes:
                        continue
                    md5 = hashlib.md5(img_bytes).hexdigest()
                    if md5 in used_md5:
                        continue
                    used_md5.add(md5)
                    item["imgs_b64"].append(base64.b64encode(img_bytes).decode("ascii"))
                    got = True
                    break
            if got:
                break
            # Unsplash（历史回退 / 非历史）
            if unsplash_key:
                for attempt in range(3):
                    src = get_unsplash_image_url(unsplash_key, ck, offset=attempt)
                    if not src:
                        break
                    img_bytes, ctype = download_image(src)
                    if not img_bytes:
                        continue
                    md5 = hashlib.md5(img_bytes).hexdigest()
                    if md5 in used_md5:
                        continue
                    used_md5.add(md5)
                    item["imgs_b64"].append(base64.b64encode(img_bytes).decode("ascii"))
                    got = True
                    break

    # 3. 生成 HTML 并上传 COS（历史板块带农历/节日信息）
    page_date_info = date_info if task_name == "history" else ""
    html = build_html(items, task["title"], today_cn, date_info=page_date_info, festivals=festivals_info)
    key = "{}/{}.html".format(task["dir"], today)
    try:
        status, public_url = cos_put_object(
            secret_id, secret_key, bucket, region, key,
            html.encode("utf-8"), "text/html; charset=utf-8"
        )
    except Exception as e:
        return {"errcode": -1, "errmsg": "COS 上传失败: " + str(e)}

    # 4. 推送摘要 + 链接（加时间戳防缓存）
    ts = int(time.time())
    link_url = "{}?t={}".format(public_url, ts)
    summary_lines = ["**{}**（{}）".format(task["title"], today_cn)]
    for i, it in enumerate(items, 1):
        summary_lines.append("{}. {}".format(i, it["title"]))
    summary_lines.append("")
    summary_lines.append("[点击查看完整图文 >>]({})".format(link_url))
    push_content = "\n".join(summary_lines)

    try:
        result = push_wecom(webhook, push_content)
    except Exception as e:
        return {"errcode": -1, "errmsg": "推送失败: " + str(e)}

    return {"errcode": result.get("errcode"), "errmsg": result.get("errmsg"),
            "url": public_url}
