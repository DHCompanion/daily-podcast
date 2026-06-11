#!/usr/bin/env python3
"""
Daily Personal Podcast Generator
Fetches news + HackerNews, generates a conversational script via Claude,
converts to MP3 with edge-tts, and updates the RSS feed.
"""

import os
import json
import re
import asyncio
import datetime
import urllib.request
import ssl
from pathlib import Path
from email.utils import formatdate
from time import mktime

import anthropic
import edge_tts
import feedparser


# ──────────────────────────────────────────────
# CONFIG — edit these to taste
# ──────────────────────────────────────────────
PODCAST_TITLE       = "My Daily Briefing"
PODCAST_DESCRIPTION = "A personal daily news podcast covering world news, tech, and AI company updates."
PODCAST_AUTHOR      = "Me"
BASE_URL            = os.environ.get("PODCAST_BASE_URL", "https://YOUR-USERNAME.github.io/daily-podcast")

TTS_VOICE = "en-US-BrianMultilingualNeural"
TTS_RATE  = "+5%"

NEWS_FEEDS = [
    # World & Tech News
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://www.wired.com/feed/rss",
    # Official AI Company Blogs
    "https://www.anthropic.com/feed.xml",
    "https://openai.com/feed.xml",
    "https://blog.perplexity.ai/feed.xml",
]

MAX_NEWS_STORIES = 8
MAX_HACKERNEWS_STORIES = 5
EPISODE_DIR = Path("episodes")
RSS_FILE = Path("feed.xml")
# ──────────────────────────────────────────────


def fetch_news() -> list[dict]:
    """Fetch top headlines from RSS feeds."""
    stories = []
    for url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
                summary = re.sub(r"<[^>]+>", "", summary)[:500]
                stories.append({
                    "source":  feed.feed.get("title", url),
                    "title":   entry.get("title", ""),
                    "summary": summary,
                    "link":    entry.get("link", ""),
                })
        except Exception as e:
            print(f"  [warn] News feed error {url}: {e}")

    seen, unique = set(), []
    for s in stories:
        key = s["title"][:60].lower()
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique[:MAX_NEWS_STORIES]


def fetch_hackernews() -> list[dict]:
    """Fetch top stories from HackerNews via the public API (no auth needed)."""
    stories = []
    try:
        # Get top story IDs
        url = "https://hacker-news.firebaseio.com/v0/topstories.json"
        req = urllib.request.Request(url, headers={"User-Agent": "daily-podcast-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            story_ids = json.loads(resp.read().decode("utf-8"))[:15]
        
        # Fetch details for each story
        for story_id in story_ids[:MAX_HACKERNEWS_STORIES]:
            try:
                story_url = f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
                with urllib.request.urlopen(story_url, timeout=5) as resp:
                    story = json.loads(resp.read().decode("utf-8"))
                
                if story.get("type") not in ["story", "poll"]:
                    continue
                if story.get("dead") or story.get("deleted"):
                    continue
                
                stories.append({
                    "source": "HackerNews",
                    "title": story.get("title", ""),
                    "summary": f"{story.get('score', 0)} points, {story.get('descendants', 0)} comments",
                    "link": story.get("url", f"https://news.ycombinator.com/item?id={story_id}"),
                })
            except Exception as e:
                print(f"  [warn] HN story {story_id} fetch error: {e}")
                continue
    except Exception as e:
        print(f"  [warn] HackerNews fetch error: {e}")
    
    return stories


def build_prompt(news: list[dict], hn_stories: list[dict], date_str: str) -> str:
    news_block = "\n\n".join(
        f"SOURCE: {s['source']}\nHEADLINE: {s['title']}\nSUMMARY: {s['summary']}"
        for s in news
    )

    hn_block = ""
    if hn_stories:
        hn_block = "\n\n--- HackerNews Top Stories ---\n"
        for s in hn_stories:
            hn_block += f"  TITLE: {s['title']}\n  STATS: {s['summary']}\n"

    return f"""You are a warm, witty, and knowledgeable podcast host creating a personal daily briefing for {date_str}.

Your listener wants a CONVERSATIONAL, engaging 20-25 minute podcast covering:
1. Top world news & tech stories from official sources
2. Trending stories on HackerNews

CONTENT PROVIDED:
=== NEWS STORIES (including official AI company blogs) ===
{news_block}
{hn_block}

INSTRUCTIONS:
- Write the FULL spoken script — everything the host says, word for word
- Tone: friendly, conversational, like a smart friend catching you up — not stiff or robotic
- Use natural spoken language: contractions, rhetorical questions, brief jokes where fitting
- Structure:
    • Punchy 30-second cold open / hook
    • Quick "what we're covering today" (20 seconds)
    • World news segment (~5 minutes of spoken content)
    • Tech news + AI company updates (~7 minutes)
    • HackerNews highlights (~5 minutes)
    • Brief sign-off (~30 seconds)
- Do NOT include stage directions, music cues, or section headers — just the spoken words
- Aim for approximately 2,800–3,500 words (equates to 20-25 min at conversational pace)
- Connect stories where relevant; add brief context or your take
- For HackerNews, summarize the trending topics and what the tech community is excited about
- Pay special attention to announcements from Anthropic, OpenAI, and Perplexity — don't bury them

Write the full script now:"""


def generate_script(news: list[dict], hn_stories: list[dict], date_str: str) -> str:
    """Call Claude Haiku to write the podcast script."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = build_prompt(news, hn_stories, date_str)

    print("  Generating script with Claude Haiku...")
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


async def text_to_speech(script: str, output_path: Path):
    """Convert script to MP3 using edge-tts."""
    print(f"  Generating audio → {output_path.name}")
    communicate = edge_tts.Communicate(script, voice=TTS_VOICE, rate=TTS_RATE)
    await communicate.save(str(output_path))


def get_mp3_duration_seconds(path: Path) -> int:
    return int(path.stat().st_size / 16000)


def format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def make_item_xml(mp3_url: str, title: str, description: str,
                  pub_date_rfc: str, file_size: int, duration: str) -> str:
    return f"""  <item>
    <title>{xml_escape(title)}</title>
    <description>{xml_escape(description)}</description>
    <pubDate>{pub_date_rfc}</pubDate>
    <guid>{mp3_url}</guid>
    <enclosure url="{mp3_url}" length="{file_size}" type="audio/mpeg"/>
    <itunes:duration>{duration}</itunes:duration>
  </item>"""


def update_rss(episode_path: Path, title: str, description: str, pub_date: datetime.datetime):
    """Add a new episode to feed.xml using string manipulation."""
    mp3_url      = f"{BASE_URL}/episodes/{episode_path.name}"
    pub_date_rfc = formatdate(mktime(pub_date.timetuple()))
    duration     = format_duration(get_mp3_duration_seconds(episode_path))
    file_size    = episode_path.stat().st_size

    new_item = make_item_xml(mp3_url, title, description, pub_date_rfc, file_size, duration)

    if RSS_FILE.exists():
        content = RSS_FILE.read_text(encoding="utf-8")
        if "<item>" in content:
            updated = content.replace("<item>", new_item + "\n  <item>", 1)
        else:
            updated = content.replace("</channel>", new_item + "\n</channel>")
        RSS_FILE.write_text(updated, encoding="utf-8")
    else:
        feed = f"""<?xml version='1.0' encoding='utf-8'?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel>
  <title>{xml_escape(PODCAST_TITLE)}</title>
  <description>{xml_escape(PODCAST_DESCRIPTION)}</description>
  <link>{BASE_URL}</link>
  <language>en-us</language>
  <itunes:author>{xml_escape(PODCAST_AUTHOR)}</itunes:author>
  <itunes:explicit>false</itunes:explicit>
{new_item}
</channel>
</rss>"""
        RSS_FILE.write_text(feed, encoding="utf-8")

    print(f"  RSS feed updated → {RSS_FILE}")


def main():
    today    = datetime.date.today()
    date_str = today.strftime("%A, %B %-d, %Y")
    ep_slug  = today.strftime("%Y-%m-%d")
    ep_title = f"Daily Briefing — {date_str}"
    ep_desc  = f"World news, tech, and AI company updates from official sources and HackerNews for {date_str}."

    EPISODE_DIR.mkdir(exist_ok=True)
    mp3_path = EPISODE_DIR / f"{ep_slug}.mp3"

    if mp3_path.exists():
        print(f"Episode already exists: {mp3_path}. Delete it to regenerate.")
        return

    print(f"\n{'='*50}")
    print(f"  Generating episode: {ep_title}")
    print(f"{'='*50}\n")

    print("[1/4] Fetching news...")
    news = fetch_news()
    print(f"  Got {len(news)} news stories")

    print("[2/4] Fetching HackerNews...")
    hn_stories = fetch_hackernews()
    print(f"  Got {len(hn_stories)} HackerNews stories")

    print("[3/4] Writing podcast script...")
    script = generate_script(news, hn_stories, date_str)
    word_count = len(script.split())
    print(f"  Script: {word_count} words (~{word_count // 140} min)")

    (EPISODE_DIR / f"{ep_slug}.txt").write_text(script)

    print("[4/4] Converting to audio...")
    asyncio.run(text_to_speech(script, mp3_path))
    size_mb = mp3_path.stat().st_size / 1_000_000
    print(f"  Audio: {mp3_path} ({size_mb:.1f} MB)")

    update_rss(mp3_path, ep_title, ep_desc, datetime.datetime.now())
    print(f"\n✅ Done! Episode ready: {mp3_path}")


if __name__ == "__main__":
    main()
