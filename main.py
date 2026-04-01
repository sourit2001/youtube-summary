import os
import json
import feedparser
import requests
from datetime import datetime, timedelta
import pytz
from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI
from dotenv import load_dotenv
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()

DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-chat')
FEISHU_WEBHOOK_URL = os.getenv('FEISHU_WEBHOOK_URL')

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

def get_channel_videos(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    feed = feedparser.parse(url)
    videos = []
    
    if not feed.entries:
        logging.warning("Failed to fetch or no entries found. Possible network proxy issue or invalid channel ID.")
        return videos
        
    for entry in feed.entries:
        video_id = entry.yt_videoid
        title = entry.title
        link = entry.link
        published_dt = datetime.strptime(entry.published, "%Y-%m-%dT%H:%M:%S%z")
        
        videos.append({
            'video_id': video_id,
            'title': title,
            'link': link,
            'published': published_dt,
            'author': entry.author
        })
    return videos

def get_transcript(video_id):
    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        # Try to find a transcript in languages 'zh', 'zh-CN', 'zh-TW', 'en'
        transcript = transcript_list.find_transcript(['zh', 'zh-CN', 'zh-TW', 'en'])
        text = " ".join([t.text for t in transcript.fetch()])
        return text
    except Exception as e:
        logging.warning(f"Could not get transcript for video {video_id}: {e}")
        return None

def summarize_content(text):
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL
    )

    # 截断以保持在上下文限制内 (例如 40k 字符 ~ 15k-20k tokens)
    if len(text) > 40000:
        text = text[:40000]

    system_prompt = '''你是一个顶级的播客分析师和内容提炼专家。
你的任务是阅读一段播客或访谈的字幕内容，并生成一份通俗易懂、具有洞察力的总结。
同时请使用“第一人称（讲述者）”的口吻来进行转述。

请按照以下结构组织你的回答：
1. 【开场白】：类似“最近我听了一个播客，是XXX（或者是这个频道的主理人）采访XXX，主题是关于……”的引入式开场白。
2. 【内容精炼】：把这期播客中最有价值、最有趣的内容用通俗易懂的大白话提炼出来（不要做干瘪的条列，要像讲故事一样连贯，可以适度进行梳理和归纳）。
3. 【结合实际的衍生洞察】：基于播客中的观点，结合当前的AI发展、个人创业或者商业实际，给出一些具有启发性的延展思考或落地行动建议（这一部分非常关键，请发挥你的核心观察能力，写出深度）。

请使用专业但不失亲和力、幽默感的大白话中文输出。排版要清晰、支持Markdown格式，非常适合用作微信朋友圈长文、公众号推文或飞书分享。'''

    try:
        logging.info("Calling DeepSeek API for summarization...")
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"以下是播客文字内容：\n\n{text}"}
            ],
            temperature=0.6,
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"DeepSeek summarization error: {e}")
        return None

def send_to_feishu(title, author, link, summary):
    if not FEISHU_WEBHOOK_URL:
        logging.error("No FEISHU_WEBHOOK_URL configured.")
        return

    # 构造飞书卡片消息
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

def main():
    processed_videos = load_processed_videos()
    
    if not os.path.exists(CHANNELS_FILE):
        logging.error(f"{CHANNELS_FILE} not found.")
        return

    with open(CHANNELS_FILE, 'r', encoding='utf-8') as f:
        channels = json.load(f)

    for channel in channels:
        logging.info(f"Checking channel: {channel['name']}")
        
        try:
            videos = get_channel_videos(channel['channel_id'])
        except Exception as e:
            logging.error(f"Error checking channel {channel['name']}: {e}")
            continue
            
        logging.info(f" -> Found {len(videos)} videos in RSS feed for {channel['name']}.")
        
        recent_videos_count = 0
        for video in videos:
            vid = video['video_id']
            # 只处理最近 7 天内发布的视频
            if datetime.now(pytz.utc) - video['published'] > timedelta(days=7):
                continue
            
            recent_videos_count += 1
                
            if vid in processed_videos:
                continue
            
            logging.info(f"Processing new video: {video['title']}")
            transcript = get_transcript(vid)
            
            if not transcript:
                logging.info(f"No transcript available for {vid}. Skipping.")
                # 记录下来以免无限重复尝试获取没有字幕的视频
                save_processed_video(vid, processed_videos)
                continue
            
            summary = summarize_content(transcript)
            
            if summary:
                send_to_feishu(video['title'], video['author'], video['link'], summary)
                save_processed_video(vid, processed_videos)
            else:
                logging.error(f"Failed to generate summary for {vid}.")
                
        logging.info(f" -> {recent_videos_count} videos passed the 7-day rule for {channel['name']}.")

if __name__ == "__main__":
    main()
