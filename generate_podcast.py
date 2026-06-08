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
import xml.etree.ElementTree as ET
from pathlib import Path
from email.utils import formatdate
from time import mktime
import urllib.request
import urllib.parse
import ssl

import anthropic
import edge_tts
import feedparser


# ──────────────────────────────────────────────
# CONFIG — edit these to taste
# ──────────────────────────────────────────────
PODCAST_TITLE       = "My Daily Briefing"
PODCAST_DESCRIPTION = "A personal daily news podcast covering world news, tech, and AI communities."
PODCAST_AUTHOR      = "Me"
BASE_URL            = os.environ.get("PODCAST_BASE_URL", "https://YOUR-USERNAME.github.io/daily-podcast")

# Voice — full list at: https://tts.trafficmanager.net/cognitiveservices/voices/list
# Good conversational picks:
#   "en-US-BrianMultilingualNeural"  — warm male (default)
#   "en-US-AvaMultilingualNeural"    — natural female
#   "en-US-AndrewMultilingualNeural" — relaxed male
TTS_VOICE = "en-US-BrianMultilingualNeural"
TTS_RATE  = "+5%"   # slight speed boost feels more natural

NEWS_FEEDS = [
    # World news
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    # Tech
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://www.wired.com/feed/rss",
]

SUBREDDITS = [
    "ClaudeAI",
    "ChatGPT",
]

MAX_NEWS_STORIES  = 6   # top stories to pass to Claude
MAX_REDDIT_POSTS  = 5   # top posts per subreddit
EPISODE_DIR       = Path("episodes")
RSS_FILE          = Path("feed.xml")
# ──────────────────────────────────────────────

# Register XML namespaces so ElementTree writes them correctly
ET.register_namespace("itunes",  "http://www.itunes.com/dtds/podcast-1.0.dtd")
ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def fetch_news() -> list[dict]:
    """Pull top headlines from RSS feeds."""
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

    # Deduplicate by title
    seen, unique = set(), []
    for s in stories:
        key = s["title"][:60].lower()
        if key not in seen:
            seen.add(key)
            unique.append(s)

    return unique[:MAX_NEWS_STORIES]


def fetch_reddit(subreddit: str) -> list[dict]:
    """Fetch hot posts from a subreddit using the public JSON API."""
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=10"
    req = urllib.request.Request(url, headers={"User-Agent": "daily-podcast-bot/1.0"})
    posts = []
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for child in data["data"]["children"]:
            p = child["data"]
            if p.get("stickied"):
                continue
            posts.append({
                "title":    p.get("title", ""),
                "score":    p.get("score", 0),
                "comments": p.get("num_comments", 0),
                "selftext": p.get("selftext", "")[:400],
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
        reddit_block += f"\n\n--- r/{sub} ---\n"
        for p in posts:
            reddit_block += (
                f"  POST: {p['title']} "
                f"(↑{p['score']} | {p['comments']} comments)\n"
            )
            if p["selftext"]:
                reddit_block += f"  TEXT: {p['selftext'][:200]}\n"

    return f"""You are a warm, witty, and knowledgeable podcast host creating a personal daily briefing for {date_str}.

Your listener wants a CONVERSATIONAL, engaging 20-25 minute podcast covering:
1. Top world news & tech stories
2. What's buzzing on r/ClaudeAI and r/ChatGPT

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
    """Call Claude Haiku to write the podcast script."""
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
    """Convert script to MP3 using edge-tts (Microsoft neural voices, free)."""
    print(f"  Generating audio → {output_path.name}")
    communicate = edge_tts.Communicate(script, voice=TTS_VOICE, rate=TTS_RATE)
    await communicate.save(str(output_path))


def get_mp3_duration_seconds(path: Path) -> int:
    """Rough MP3 duration estimate from file size."""
    size_bytes = path.stat().st_size
    return int(size_bytes / 16000)  # ~128 kbps


def format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_rss_from_scratch(channel: ET.Element):
    """Populate a brand-new channel element with feed metadata."""
    ET.SubElement(channel, "title").text       = PODCAST_TITLE
    ET.SubElement(channel, "description").text = PODCAST_DESCRIPTION
    ET.SubElement(channel, "link").text        = BASE_URL
    ET.SubElement(channel, "language").text    = "en-us"
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text   = PODCAST_AUTHOR
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = "false"


def update_rss(episode_path: Path, title: str, description: str, pub_date: datetime.datetime):
    """Add a new episode item to feed.xml, creating the file if needed."""

    if RSS_FILE.exists():
        tree = ET.parse(RSS_FILE)
        root = tree.getroot()
        channel = root.find("channel")
        if channel is None:
            raise ValueError("Existing feed.xml has no <channel> element — delete it and rerun.")
    else:
        # Build a clean RSS 2.0 root with properly registered namespaces
        root = ET.Element("rss")
        root.set("version", "2.0")
        root.set("xmlns:itunes",  "http://www.itunes.com/dtds/podcast-1.0.dtd")
        root.set("xmlns:content", "http://purl.org/rss/1.0/modules/content/")
        channel = ET.SubElement(root, "channel")
        build_rss_from_scratch(channel)

    # Build the new <item>
    duration_sec = get_mp3_duration_seconds(episode_path)
    mp3_url      = f"{BASE_URL}/episodes/{episode_path.name}"
    pub_date_rfc = formatdate(mktime(pub_date.timetuple()))

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text       = title
    ET.SubElement(item, "description").text = description
    ET.SubElement(item, "pubDate").text     = pub_date_rfc
    ET.SubElement(item, "guid").text        = mp3_url

    enclosure = ET.SubElement(item, "enclosure")
    enclosure.set("url",    mp3_url)
    enclosure.set("length", str(episode_path.stat().st_size))
    enclosure.set("type",   "audio/mpeg")

    ET.SubElement(item, f"{{{ITUNES_NS}}}duration").text = format_duration(duration_sec)

    # Write out with pretty indentation
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(str(RSS_FILE), xml_declaration=True, encoding="utf-8")
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

    script_path = EPISODE_DIR / f"{ep_slug}.txt"
    script_path.write_text(script)

    print("[4/4] Converting to audio...")
    asyncio.run(text_to_speech(script, mp3_path))
    size_mb = mp3_path.stat().st_size / 1_000_000
    print(f"  Audio: {mp3_path} ({size_mb:.1f} MB)")

    update_rss(mp3_path, ep_title, ep_desc, datetime.datetime.now())

    print(f"\n✅ Done! Episode ready: {mp3_path}")


if __name__ == "__main__":
    main()
