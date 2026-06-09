#!/usr/bin/env python3
"""
Daily Personal Podcast Generator
Fetches news + Reddit posts, generates a conversational script via Claude,
converts to MP3 with edge-tts, and updates the RSS feed.
"""

import os
import json
import re
import asyncio
import datetime
from pathlib import Path
from email.utils import formatdate
from time import mktime
import urllib.request
import ssl

import anthropic
import edge_tts
import feedparser


# ──────────────────────────────────────────────
# CONFIG — edit these to taste
# ──────────────────────────────────────────────
PODCAST_TITLE       = "The Daily Briefing with Walter"
PODCAST_DESCRIPTION = "A personal daily news podcast covering world news, tech, and AI communities."
PODCAST_AUTHOR      = "Me"
BASE_URL            = os.environ.get("PODCAST_BASE_URL", "https://YOUR-USERNAME.github.io/daily-podcast")

TTS_VOICE = "en-US-BrianMultilingualNeural"
TTS_RATE  = "+5%"

NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://www.wired.com/feed/rss",
    "http://news.ycombinator.com/rss",
]

SUBREDDITS = [
    "ClaudeAI",
    "ChatGPT",
    "vibecoding"
]

MAX_NEWS_STORIES = 6
MAX_REDDIT_POSTS = 5
EPISODE_DIR      = Path("episodes")
RSS_FILE         = Path("feed.xml")
# ──────────────────────────────────────────────


def fetch_news() -> list[dict]:
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


def fetch_reddit(subreddit: str) -> list[dict]:
    """Fetch hot posts via Reddit's JSON API with a browser-like user agent."""
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=15&raw_json=1"
    # Reddit blocks generic bots — use a realistic browser UA
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    posts = []
    try:
        req = urllib.request.Request(url, headers=headers)
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for child in data["data"]["children"]:
            p = child["data"]
            if p.get("stickied") or p.get("distinguished"):
                continue
            posts.append({
                "title":    p.get("title", ""),
                "score":    p.get("score", 0),
                "comments": p.get("num_comments", 0),
                "selftext": (p.get("selftext") or "")[:400],
                "url":      "https://reddit.com" + p.get("permalink", ""),
            })
            if len(posts) >= MAX_REDDIT_POSTS:
                break
    except Exception as e:
        print(f"  [warn] Reddit fetch error r/{subreddit}: {e}")
    return posts


def build_prompt(news: list[dict], reddit_data: dict[str, list[dict]], date_str: str) -> str:
    news_block = "\n\n".join(
        f"SOURCE: {s['source']}\nHEADLINE: {s['title']}\nSUMMARY: {s['summary']}"
        for s in news
    )

    reddit_block = ""
    for sub, posts in reddit_data.items():
        if posts:
            reddit_block += f"\n\n--- r/{sub} ---\n"
            for p in posts:
                reddit_block += f"  POST: {p['title']} (↑{p['score']} | {p['comments']} comments)\n"
                if p["selftext"]:
                    reddit_block += f"  TEXT: {p['selftext'][:200]}\n"
        else:
            reddit_block += f"\n\n--- r/{sub} ---\n  (no posts available today)\n"

    return f"""You are Walter, a warm, witty, and knowledgeable podcast host creating a personal daily briefing for {date_str}. You always refer to yourself as Alex throughout the show.

Your listener wants a CONVERSATIONAL, engaging 20-25 minute podcast covering:
1. Top world news & tech stories
2. What's buzzing on r/ClaudeAI, r/ChatGPT and r/vibecoding

CONTENT PROVIDED:
=== NEWS STORIES ===
{news_block}

=== REDDIT HIGHLIGHTS ===
{reddit_block}

INSTRUCTIONS:
- Write the FULL spoken script — everything the host says, word for word
- Tone: friendly, conversational, like a smart friend catching you up — not stiff or robotic
- Use natural spoken language: contractions, rhetorical questions, brief jokes where fitting
- Structure:
    • Punchy 30-second cold open / hook
    • Quick "what we're covering today" (20 seconds)
    • World news segment (~8 minutes of spoken content)
    • Tech news segment (~5 minutes)
    • Reddit community roundup — r/ClaudeAI (~3 min) then r/ChatGPT (~3 min)
    • Brief sign-off (~30 seconds)
- Do NOT include stage directions, music cues, or section headers — just the spoken words
- Aim for approximately 2,800–3,500 words (equates to 20-25 min at conversational pace)
- Connect stories where relevant; add brief context or your take
- For Reddit, summarize the community vibe and top discussions — don't just list post titles

Write the full script now:"""


def generate_script(news: list[dict], reddit_data: dict[str, list[dict]], date_str: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = build_prompt(news, reddit_data, date_str)
    print("  Generating script with Claude Haiku...")
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


async def text_to_speech(script: str, output_path: Path):
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
    """Add a new episode to feed.xml using string manipulation — avoids ElementTree namespace bugs."""
    mp3_url      = f"{BASE_URL}/episodes/{episode_path.name}"
    pub_date_rfc = formatdate(mktime(pub_date.timetuple()))
    duration     = format_duration(get_mp3_duration_seconds(episode_path))
    file_size    = episode_path.stat().st_size

    new_item = make_item_xml(mp3_url, title, description, pub_date_rfc, file_size, duration)

    # If feed exists and is valid XML, insert new item after <channel> metadata
    if RSS_FILE.exists():
        content = RSS_FILE.read_text(encoding="utf-8")
        # Insert new item just before the first existing <item> (newest first)
        # or before </channel> if no items yet
        if "<item>" in content:
            updated = content.replace("<item>", new_item + "\n  <item>", 1)
        else:
            updated = content.replace("</channel>", new_item + "\n</channel>")
        RSS_FILE.write_text(updated, encoding="utf-8")
    else:
        # Build the feed from scratch as a plain string — no ElementTree namespaces
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
    ep_desc  = f"World news, tech headlines, and the best of r/ClaudeAI and r/ChatGPT for {date_str}."

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
    print(f"  Got {len(news)} stories")

    print("[2/4] Fetching Reddit posts...")
    reddit_data = {}
    for sub in SUBREDDITS:
        posts = fetch_reddit(sub)
        reddit_data[sub] = posts
        print(f"  r/{sub}: {len(posts)} posts")

    print("[3/4] Writing podcast script...")
    script = generate_script(news, reddit_data, date_str)
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
