import os
import re
import json
import time
import random
import requests
import feedparser
import subprocess
import tempfile
from datetime import datetime, timedelta
import pytz
from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI
from dotenv import load_dotenv
import logging
import markdown

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()

DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-v4-flash')
FEISHU_WEBHOOK_URL = os.getenv('FEISHU_WEBHOOK_URL')
OBSIDIAN_VAULT_PATH = os.getenv('OBSIDIAN_VAULT_PATH')
ENABLE_WECHAT = os.getenv('ENABLE_WECHAT', 'false').lower() == 'true'
LIMYAI_API_KEY = os.getenv('LIMYAI_API_KEY')
WECHAT_APPID = os.getenv('WECHAT_APPID')
WEBSHARE_PROXY_USER = os.getenv('WEBSHARE_PROXY_USER', '')
WEBSHARE_PROXY_PASS = os.getenv('WEBSHARE_PROXY_PASS', '')

CHANNELS_FILE = 'channels.json'
PROCESSED_FILE = 'processed.json'

def load_processed_videos():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    return set()

def save_processed_video(video_id, processed_set):
    processed_set.add(video_id)
    with open(PROCESSED_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(processed_set), f)

def get_channel_videos_yt_dlp(channel_id):
    """
    Fallback method using yt-dlp to get the latest videos.
    Supports both internal IDs (UC...) and Handles (@...).
    """
    import subprocess
    logging.info(f"YTDLP: Fetching videos for channel {channel_id}...")
    
    # 根据 ID 类型构造不同的 URL
    if channel_id.startswith('@'):
        channel_url = f"https://www.youtube.com/{channel_id}"
    elif channel_id.startswith('UC'):
        channel_url = f"https://www.youtube.com/channel/{channel_id}"
    else:
        # 兼容一些老旧 ID 或其他情况
        channel_url = f"https://www.youtube.com/{channel_id}"

    # 获取最近 10 条视频
    cmd = [
        "yt-dlp",
        "--print", "%(id)s|||%(title)s|||%(upload_date)s|||%(uploader)s",
        "--playlist-end", "10",
        "--flat-playlist",
        "--quiet",
        "--no-warnings",
        channel_url
    ]
    
    videos = []
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        # 如果第一次尝试失败，根据报错尝试另一种 URL 格式
        if result.returncode != 0:
            logging.warning(f"yt-dlp failed for {channel_url}: {result.stderr.strip()}")
            if "HTTP Error 404" in result.stderr or "does not exist" in result.stderr:
                # 尝试备选格式
                alt_url = f"https://www.youtube.com/{channel_id}" if "channel/" in channel_url else f"https://www.youtube.com/channel/{channel_id}"
                logging.info(f"YTDLP: Retrying with alternative URL: {alt_url}")
                cmd[-1] = alt_url
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if not result.stdout.strip():
            logging.info(f"YTDLP: No output for {channel_id}")
            return []

        for line in result.stdout.strip().split('\n'):
            if not line or '|||' not in line:
                continue
            parts = line.split('|||')
            if len(parts) >= 4:
                vid, title, upload_date, author = parts[0], parts[1], parts[2], parts[3]
                
                # Parse upload_date (YYYYMMDD)
                try:
                    published_dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=pytz.utc)
                except:
                    published_dt = datetime.now(pytz.utc)
                
                videos.append({
                    'video_id': vid,
                    'title': title,
                    'link': f'https://www.youtube.com/watch?v={vid}',
                    'published': published_dt,
                    'author': author
                })
        logging.info(f"YTDLP: Found {len(videos)} videos for channel {channel_id}")
    except Exception as e:
        logging.warning(f"Failed to fetch with yt-dlp for channel {channel_id}: {e}")
    
    return videos

def get_channel_videos_rss(channel_id):
    """
    使用 YouTube RSS Feed 获取频道最新视频列表。
    如果 RSS 失败（返回 404/500 等），则降级使用 yt-dlp。
    注意：RSS 官方接口只支持 UCid，不支持 @handle。如果有 @handle 则直接降级。
    """
    if channel_id.startswith('@'):
        logging.info(f"Handle detected ({channel_id}), skipping RSS and using YT-DLP directly.")
        return get_channel_videos_yt_dlp(channel_id)

    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36'
    }
    videos = []
    try:
        logging.info(f"Fetching RSS: {url}")
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code != 200:
            logging.warning(f"RSS feed returned status {response.status_code} for channel {channel_id}. Content: {response.text[:200]}")
            # RSS 失败，尝试降级到 yt-dlp
            return get_channel_videos_yt_dlp(channel_id)

        feed = feedparser.parse(response.content)
        logging.info(f"RSS: Found {len(feed.entries)} entries for channel {channel_id}")

        for entry in feed.entries:
            video_id = entry.get('yt_videoid', '')
            if not video_id:
                # Fallback: parse from link
                link = entry.get('link', '')
                match = re.search(r'v=([a-zA-Z0-9_-]+)', link)
                video_id = match.group(1) if match else ''

            if not video_id:
                continue

            title = entry.get('title', '')
            link = entry.get('link', f'https://www.youtube.com/watch?v={video_id}')
            author = entry.get('author', '') or (entry.get('authors', [{}])[0].get('name', channel_id))

            # 解析发布时间
            published_parsed = entry.get('published_parsed')
            if published_parsed:
                published_dt = datetime(*published_parsed[:6], tzinfo=pytz.utc)
            else:
                published_dt = datetime.now(pytz.utc)

            videos.append({
                'video_id': video_id,
                'title': title,
                'link': link,
                'published': published_dt,
                'author': author
            })

    except Exception as e:
        logging.warning(f"Failed to fetch RSS for channel {channel_id}: {e}")
        # 异常情况下也尝试降级
        return get_channel_videos_yt_dlp(channel_id)

    return videos

def get_transcript_ytdlp(video_id):
    """
    使用 yt-dlp 下载字幕作为备用方案，绕过 IP 封锁。
    """
    logging.info(f"Falling back to yt-dlp for transcript: {video_id}")
    with tempfile.TemporaryDirectory() as tmpdir:
        # 不指定 --sub-format，让 yt-dlp 自动选择最佳格式
        cmd = [
            "yt-dlp",
            "--write-auto-sub",
            "--skip-download",
            "--sub-langs", "zh-Hans,zh,en",
            "--output", os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "--quiet",
            "--no-warnings",
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            # 查找下载的字幕文件（vtt / json3 / srv1 等）
            sub_files = [f for f in os.listdir(tmpdir) if '.' in f]
            if not sub_files:
                logging.warning(f"yt-dlp: No subtitle file found for {video_id}")
                return None

            # 优先选中文字幕
            preferred = sorted(sub_files, key=lambda f: (0 if 'zh' in f else 1))
            sub_path = os.path.join(tmpdir, preferred[0])
            logging.info(f"yt-dlp: Using subtitle file: {preferred[0]}")

            with open(sub_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 尝试解析 JSON3 格式
            if sub_path.endswith('.json3'):
                try:
                    data = json.loads(content)
                    texts = []
                    for event in data.get('events', []):
                        segs = event.get('segs', [])
                        line = ''.join(s.get('utf8', '') for s in segs).strip()
                        if line and line != '\n':
                            texts.append(line)
                    text = ' '.join(texts)
                except Exception:
                    text = ''
            else:
                # 解析 VTT / SRV 格式，去掉时间码和标签，提取纯文字
                lines = content.split('\n')
                texts = []
                seen = set()
                for line in lines:
                    line = line.strip()
                    if not line or line.startswith('WEBVTT') or '-->' in line or line.startswith('NOTE') or line.isdigit():
                        continue
                    clean = re.sub(r'<[^>]+>', '', line)
                    clean = re.sub(r'\s+', ' ', clean).strip()
                    if clean and clean not in seen:
                        seen.add(clean)
                        texts.append(clean)
                text = ' '.join(texts)

            if len(text) < 100:
                logging.warning(f"yt-dlp: Subtitle too short ({len(text)} chars), skipping {video_id}")
                return None

            logging.info(f"yt-dlp: Got {len(text)} chars of transcript for {video_id}")
            return text

        except Exception as e:
            logging.warning(f"yt-dlp transcript fallback failed for {video_id}: {e}")
            return None

def get_transcript(video_id):
    # 先尝试 youtube-transcript-api（支持 Webshare 代理）
    try:
        from youtube_transcript_api.proxies import WebshareProxyConfig
        if WEBSHARE_PROXY_USER and WEBSHARE_PROXY_PASS:
            api = YouTubeTranscriptApi(
                proxy_config=WebshareProxyConfig(
                    proxy_username=WEBSHARE_PROXY_USER,
                    proxy_password=WEBSHARE_PROXY_PASS,
                )
            )
            logging.info(f"Using Webshare proxy for transcript: {video_id}")
        else:
            api = YouTubeTranscriptApi()

        transcript_list = api.list(video_id)
        # 优先中文，退而求英文
        transcript = transcript_list.find_transcript(['zh', 'zh-CN', 'zh-TW', 'en'])
        text = " ".join([t.text for t in transcript.fetch()])
        return text
    except Exception as e:
        err_str = str(e)
        if 'IpBlocked' in err_str or 'RequestBlocked' in err_str or 'blocked' in err_str.lower() or 'Connection' in err_str:
            logging.warning(f"youtube-transcript-api IP blocked for {video_id}, trying yt-dlp fallback...")
            return get_transcript_ytdlp(video_id)
        logging.warning(f"Could not get transcript for video {video_id}: {e}")
        # 其他错误也尝试 yt-dlp
        return get_transcript_ytdlp(video_id)

def summarize_content(text):
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL
    )

    if len(text) > 40000:
        text = text[:40000]

    system_prompt = '''你是一位擅长拆解科技产品与商业案例的资深内容作者，同时也是一位擅长写微信公众号文章的内容创作者。
你的任务是将一段英文 YouTube 播客或视频字幕内容，转化为一篇适合中文微信公众号发布的深度好文。

文章的整体基调要求：
- **避免过度随意**：不要使用过多的网络热梗或过于随性的口语，保持一种“有料、有思考、客观且专业”的质感。
- **兼顾传播力与信息量**：标题要足够吸睛，但内容必须扎实，优先让读者弄清产品如何被做出来、为何有人使用以及如何获得增长。
- **语气平衡**：保持客观、专业和具体。判断必须基于字幕中的事实，不为了显得深刻而拔高主题。

请严格按照以下结构输出（直接输出纯文本，不要包含代码块）：

【爆款标题】
创作一个极具吸引力且符合专业调性的微信公众号标题（不超过60个字）。利用痛点、悬念或反差来引发好奇，但禁止使用低质标题党词汇（如“震惊”、“必看”）。

【正文内容】
请以产品案例分析文章的形式进行撰写，整篇文章应该像一篇一气呵成的优质自媒体稿，而非简单的内容摘要。
正文长度控制在1000字以内，优先保留产品事实、具体做法、关键案例和数据，不要铺陈过多背景。

如果字幕主要讨论某个产品、创业项目或产品团队，正文必须把大部分篇幅用于回答以下问题：
- **如何发现用户需求**：团队从什么具体场景、痛点、观察、访谈、反馈或数据中发现需求，最初假设如何被验证和修正。
- **产品特点是什么**：产品解决什么具体问题，目标用户是谁，核心功能、使用流程、差异化和取舍是什么。
- **如何推广和增长**：早期用户从哪里来，使用了哪些渠道、内容、社区、口碑、销售或合作策略，以及字幕提到的增长数据。
尽量写清“谁在什么场景遇到什么问题—团队如何验证—做出了什么—如何触达用户”的因果链。字幕没有提供的信息要明确略过，禁止自行补全。

严格减少抽象大道理、宏大叙事、行业趋势和价值升华。此类内容合计不得超过正文的10%，并且只能用于解释上述产品事实。

写作建议（请将这些重点自然融入行文，不要作为标题出现）：
- **具体的开场**：优先用用户痛点、产品诞生契机、关键数据或反常识的产品决策切入，不要用空泛的行业趋势开场。
- **提炼而非搬运**：不要机械复述播客流程，要围绕需求发现、产品设计和推广增长组织材料，保留能支撑结论的细节。
- **克制评论**：只有字幕证据充分且有助于理解产品时才做简短判断，不另设升华段落。

降低 AI 写作痕迹：
- 像一个认真看完播客、了解产品的人讲述这件事。先写字幕中的人物、场景、动作、数字、选择和结果，再给出必要判断。
- 每个段落必须带来一项新信息或推进一层因果。不要换用近义词重复同一个观点，也不要为了凑足1000字扩写空话；材料不足时宁可写短。
- 使用自然、克制的现代中文。句子可以长短交替，段落不必整齐，不刻意制造金句、排比或连续短句造成的戏剧感。
- 不要预告文章结构，不使用“首先、其次、最后”“让我们来看看”“值得注意的是”“更重要的是”等机械衔接。
- 避免“不是……而是……”“看似……实则……”“真正的关键是”“本质上”“说到底”等故作深刻的翻案表达。直接说清判断和依据。
- 不用宏大比喻、商业黑话和抽象名词包装普通事实，不在结尾复述全文或强行升华。
- 不得虚构字幕没有提供的经历、心理、对白、数据或现场细节。
- 完成初稿后在内部检查并改掉上述痕迹，只输出最终文章，不展示检查过程。

排版要求：
- 严格禁止使用“一、二、三”等生硬的结构化序号或AI味浓重的标题。
- 文字要精炼，句子长短自然变化。段落之间留出空行，方便手机端阅读。
- 不使用 Emoji。确有必要强调的关键数据或字幕原话，可以少量使用加粗或引用格式（>）。'''

    try:
        logging.info("Calling DeepSeek API for summarization...")
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"以下是播客文字内容：\n\n{text}"}
            ],
            temperature=0.7,
        )
        content = response.choices[0].message.content

        # 解析标题和正文
        title_match = re.search(r'【爆款标题】\s*(.*?)\s*【正文内容】\s*(.*)', content, re.DOTALL)
        if title_match:
            viral_title = title_match.group(1).strip()
            body_content = title_match.group(2).strip()
        else:
            viral_title = ""
            body_content = content

        return {"title": viral_title, "content": body_content}
    except Exception as e:
        logging.error(f"DeepSeek summarization error: {e}")
        return None

def send_to_feishu(title, author, link, summary):
    if not FEISHU_WEBHOOK_URL:
        logging.error("No FEISHU_WEBHOOK_URL configured.")
        return

    card_msg = {
        "msg_type": "interactive",
        "card": {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🎙️ {author} 新播客总结"
                },
                "template": "blue"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "content": f"**标题**: [{title}]({link})\n**日期**: {datetime.now().strftime('%Y-%m-%d')}",
                        "tag": "lark_md"
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "text": {
                        "content": summary,
                        "tag": "lark_md"
                    }
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "content": "观看原视频",
                                "tag": "plain_text"
                            },
                            "type": "primary",
                            "url": link
                        }
                    ]
                }
            ]
        }
    }

    try:
        res = requests.post(FEISHU_WEBHOOK_URL, json=card_msg)
        if res.status_code == 200:
            logging.info(f"Successfully sent {title} to Feishu.")
        else:
            logging.error(f"Failed to send to Feishu: {res.text}")
    except Exception as e:
        logging.error(f"Error sending to Feishu: {e}")

def save_to_obsidian(title, author, link, summary, published_date):
    if not OBSIDIAN_VAULT_PATH:
        logging.info("No OBSIDIAN_VAULT_PATH configured. Skipping Obsidian save.")
        return

    if not os.path.exists(OBSIDIAN_VAULT_PATH):
        try:
            os.makedirs(OBSIDIAN_VAULT_PATH, exist_ok=True)
        except Exception as e:
            logging.error(f"Failed to create Obsidian folder: {e}")
            return

    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
    filename = f"{safe_title}.md"
    filepath = os.path.join(OBSIDIAN_VAULT_PATH, filename)

    obsidian_content = f"""---
title: "{title}"
author: "{author}"
source: "{link}"
date: {published_date.strftime('%Y-%m-%d')}
tags: [Podcast, AI, Entrepreneurship]
---

# {title}

{summary}

---
原文链接: [{link}]({link})
"""

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(obsidian_content)
        logging.info(f"Successfully saved note to Obsidian: {filename}")
    except Exception as e:
        logging.error(f"Failed to save note to Obsidian: {e}")

def get_youtube_thumbnail_url(video_id):
    urls = [
        f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    ]
    for url in urls:
        try:
            res = requests.head(url, timeout=10)
            if res.status_code == 200:
                return url
        except:
            continue
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

def publish_to_wechat_draft(viral_title, youtube_title, video_link, author, summary_markdown, cover_url):
    """
    将总结发布到微信公众号草稿箱。
    viral_title: DeepSeek 生成的爆款标题（用于微信文章标题 + 正文顶部大字展示）
    youtube_title: 原 YouTube 视频标题（作为副标题参考）
    video_link: 原 YouTube 视频链接（显示为来源）
    summary_markdown: 正文 Markdown 内容
    """
    if not ENABLE_WECHAT:
        return

    if not LIMYAI_API_KEY or not WECHAT_APPID:
        logging.error("WeChat: LIMYAI_API_KEY or WECHAT_APPID not configured in .env.")
        return

    display_title = viral_title if viral_title else youtube_title
    logging.info(f"WeChat: Publishing 《{display_title}》 to draft box via LimyAI API...")

    # 将 Markdown 转为 HTML
    html_body = markdown.markdown(summary_markdown, extensions=['nl2br'])

    # 构造带爆款标题的完整 HTML
    styled_html = f"""
    <section style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; font-size: 16px; color: #333; line-height: 1.85; letter-spacing: 0.5px; padding: 10px 15px; max-width: 677px; margin: 0 auto;">

        <!-- 爆款标题区域 -->
        <h1 style="font-size: 22px; font-weight: 700; color: #111; line-height: 1.4; margin: 0 0 8px 0; padding: 0;">
            {display_title}
        </h1>
        <p style="font-size: 13px; color: #888; margin: 0 0 24px 0; padding-bottom: 16px; border-bottom: 2px solid #07C160;">
            📺 来源：<a href="{video_link}" style="color: #07C160; text-decoration: none;">{youtube_title}</a>
        </p>

        <!-- 正文区域 -->
        <div class="article-body">
            {html_body}
        </div>

        <style>
            .article-body p {{ margin-bottom: 18px; }}
            .article-body strong {{ color: #1a1a1a; font-weight: 600; }}
            .article-body blockquote {{
                margin: 20px 0;
                padding: 14px 18px;
                border-left: 4px solid #07C160;
                background-color: #f0faf4;
                color: #444;
                font-size: 15px;
                border-radius: 0 6px 6px 0;
                font-style: normal;
            }}
            .article-body ul, .article-body ol {{
                padding-left: 22px;
                margin-bottom: 18px;
            }}
            .article-body li {{ margin-bottom: 10px; }}
            .article-body h1, .article-body h2, .article-body h3 {{
                color: #111;
                font-weight: 700;
                margin-top: 28px;
                margin-bottom: 12px;
                line-height: 1.4;
            }}
            .article-body h2 {{ font-size: 18px; }}
            .article-body h3 {{ font-size: 16px; }}
        </style>
    </section>
    """

    url = "https://wx.limyai.com/api/openapi/wechat-publish"
    headers = {
        "X-API-Key": LIMYAI_API_KEY,
        "Content-Type": "application/json"
    }

    data = {
        "wechatAppid": WECHAT_APPID,
        "title": display_title[:64],  # 微信标题上限 64 字符
        "content": styled_html,
        "author": author[:8],
        "coverImage": cover_url,
        "contentFormat": "html",
        "articleType": "news"
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        result = response.json()
        if result.get("success"):
            logging.info(f"WeChat: 文章 《{display_title}》 已成功发布到公众号草稿箱！")
            logging.info(f"WeChat: Publication ID: {result['data'].get('publicationId')}")
        else:
            logging.error(f"WeChat: 发布失败 - {result.get('error')} (Code: {result.get('code')})")
    except Exception as e:
        logging.error(f"WeChat: API调用出错: {e}")

def main():
    processed_videos = load_processed_videos()

    if not os.path.exists(CHANNELS_FILE):
        logging.error(f"{CHANNELS_FILE} not found.")
        return

    with open(CHANNELS_FILE, 'r', encoding='utf-8') as f:
        channels = json.load(f)

    for channel in channels:
        logging.info(f"Checking channel: {channel['name']} (ID: {channel['channel_id']})")

        try:
            videos = get_channel_videos_rss(channel['channel_id'])
        except Exception as e:
            logging.error(f"Error checking channel {channel['name']}: {e}")
            continue

        logging.info(f" -> Found {len(videos)} videos in RSS feed for {channel['name']}.")

        recent_videos_count = 0
        for video in videos:
            vid = video['video_id']

            # 只处理最近 7 天内发布的视频
            age = datetime.now(pytz.utc) - video['published']
            if age > timedelta(days=7):
                logging.debug(f"Skipping old video ({age.days}d old): {video['title']}")
                continue

            recent_videos_count += 1

            if vid in processed_videos:
                logging.info(f"Already processed: {video['title']}")
                continue

            logging.info(f"Processing new video: {video['title']}")

            # 随机等待 5~15 秒，模拟人类浏览节奏，降低被 YouTube 封 IP 的概率
            wait_sec = random.uniform(5, 15)
            logging.info(f"Waiting {wait_sec:.1f}s before fetching transcript...")
            time.sleep(wait_sec)

            transcript = get_transcript(vid)

            if not transcript:
                logging.info(f"No transcript available for {vid}. Skipping.")
                continue

            summary_data = summarize_content(transcript)

            if summary_data:
                viral_title = summary_data.get('title', '')
                body_content = summary_data.get('content', '')

                logging.info(f"Viral title generated: {viral_title}")

                # 发送到飞书（注明来源播客频道 + 原视频链接，微信文章里不放）
                source_info = f"📺 **来源频道**：{channel['name']}\n🎬 **原视频**：[{video['title']}]({video['link']})\n\n"
                feishu_content = f"{source_info}**🔥 爆款标题：{viral_title}**\n\n---\n\n{body_content}" if viral_title else f"{source_info}{body_content}"
                send_to_feishu(video['title'], video['author'], video['link'], feishu_content)

                # 保存到 Obsidian（包含来源频道 + 爆款标题，方便日后检索）
                obsidian_source = f"- **来源频道**：{channel['name']}\n- **原视频标题**：{video['title']}\n- **原视频链接**：{video['link']}\n"
                obsidian_content = f"## 来源\n\n{obsidian_source}\n## 🔥 爆款标题\n\n{viral_title}\n\n---\n\n{body_content}" if viral_title else f"## 来源\n\n{obsidian_source}\n\n---\n\n{body_content}"
                save_to_obsidian(video['title'], video['author'], video['link'], obsidian_content, video['published'])

                # 发布到微信公众号草稿箱
                cover_url = get_youtube_thumbnail_url(vid)
                publish_to_wechat_draft(
                    viral_title=viral_title,
                    youtube_title=video['title'],
                    video_link=video['link'],
                    author=video['author'],
                    summary_markdown=body_content,
                    cover_url=cover_url
                )

                save_processed_video(vid, processed_videos)
            else:
                logging.error(f"Failed to generate summary for {vid}.")

        logging.info(f" -> {recent_videos_count} recent videos (within 7 days) found for {channel['name']}.")

        # 每个频道处理完后，额外等待 10~30 秒再切换下一个频道
        channel_wait = random.uniform(10, 30)
        logging.info(f"Channel done. Cooling down {channel_wait:.1f}s...")
        time.sleep(channel_wait)

if __name__ == "__main__":
    main()
