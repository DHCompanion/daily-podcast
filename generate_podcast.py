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

TTS_VOICE = "en-US-SteffanNeural"
TTS_RATE  = "+5%"

NEWS_FEEDS = [
    # World News
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://feeds.npr.org/1004/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://feeds.washingtonpost.com/rss/world",
    # US News
    "https://feeds.npr.org/1003/rss.xml",
    # Tech News
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://www.wired.com/feed/rss",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.feedburner.com/TheHackersNews",
    # Official AI Company Blogs
    "https://www.anthropic.com/feed.xml",
    "https://openai.com/feed.xml",
    "https://blog.perplexity.ai/feed.xml",
    "https://deepmind.google/blog/rss.xml",
    "https://blogs.microsoft.com/ai/feed/",
]

MAX_NEWS_STORIES = 8
MAX_HACKERNEWS_STORIES = 3
MAX_HN_COMMENTS = 3   # top comments to pull per HackerNews story for context
LOOKBACK_DAYS = 3   # how many prior days of episodes to check for duplicate stories
EPISODE_DIR = Path("episodes")
RSS_FILE = Path("feed.xml")
# ──────────────────────────────────────────────


def get_recent_headlines() -> set[str]:
    """Read the last few days of episode scripts to find already-covered stories."""
    recent = set()
    if not EPISODE_DIR.exists():
        return recent

    today = datetime.date.today()
    for days_back in range(1, LOOKBACK_DAYS + 1):
        day = today - datetime.timedelta(days=days_back)
        # We store the raw fetched headlines alongside each episode as a .headlines file
        headlines_file = EPISODE_DIR / f"{day.strftime('%Y-%m-%d')}.headlines"
        if headlines_file.exists():
            try:
                for line in headlines_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip().lower()
                    if line:
                        recent.add(line)
            except Exception as e:
                print(f"  [warn] Could not read {headlines_file}: {e}")
    return recent


def normalize_headline(title: str) -> str:
    """Normalize a headline for fuzzy duplicate matching."""
    # Lowercase, strip punctuation, collapse whitespace
    t = re.sub(r"[^\w\s]", "", title.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t


def fetch_news(recent_headlines: set[str]) -> list[dict]:
    """Fetch top headlines from RSS feeds, skipping recently-covered stories."""
    stories = []
    for url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:4]:
                title = entry.get("title", "")
                norm = normalize_headline(title)

                # Skip if we covered this (or something very similar) recently
                if norm in recent_headlines:
                    continue
                # Also skip if a close variant was covered (first 8 words match)
                norm_prefix = " ".join(norm.split()[:8])
                if any(norm_prefix and norm_prefix in rh for rh in recent_headlines):
                    continue

                summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
                summary = re.sub(r"<[^>]+>", "", summary)[:500]
                stories.append({
                    "source":  feed.feed.get("title", url),
                    "title":   title,
                    "summary": summary,
                    "link":    entry.get("link", ""),
                    "norm":    norm,
                })
        except Exception as e:
            print(f"  [warn] News feed error {url}: {e}")

    # Deduplicate within today's batch too
    seen, unique = set(), []
    for s in stories:
        key = s["norm"][:60]
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique[:MAX_NEWS_STORIES]


def fetch_hn_comments(kid_ids: list, limit: int) -> list[str]:
    """Fetch and clean the top-level comments for a HackerNews story."""
    comments = []
    for kid_id in kid_ids[:limit]:
        try:
            kid_url = f"https://hacker-news.firebaseio.com/v0/item/{kid_id}.json"
            with urllib.request.urlopen(kid_url, timeout=5) as resp:
                kid = json.loads(resp.read().decode("utf-8"))
            if kid.get("dead") or kid.get("deleted"):
                continue
            text = kid.get("text", "")
            if not text:
                continue
            # Strip HTML tags and decode common entities
            text = re.sub(r"<[^>]+>", " ", text)
            text = (text.replace("&#x27;", "'").replace("&quot;", '"')
                        .replace("&amp;", "&").replace("&gt;", ">")
                        .replace("&lt;", "<").replace("&#x2F;", "/"))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                comments.append(text[:400])  # cap length per comment
        except Exception as e:
            print(f"  [warn] HN comment {kid_id} fetch error: {e}")
            continue
    return comments


def fetch_hackernews() -> list[dict]:
    """Fetch top stories from HackerNews via the public API (no auth needed)."""
    stories = []
    try:
        url = "https://hacker-news.firebaseio.com/v0/topstories.json"
        req = urllib.request.Request(url, headers={"User-Agent": "daily-podcast-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            story_ids = json.loads(resp.read().decode("utf-8"))[:15]

        for story_id in story_ids[:MAX_HACKERNEWS_STORIES]:
            try:
                story_url = f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
                with urllib.request.urlopen(story_url, timeout=5) as resp:
                    story = json.loads(resp.read().decode("utf-8"))

                if story.get("type") not in ["story", "poll"]:
                    continue
                if story.get("dead") or story.get("deleted"):
                    continue

                # Pull the top few comments for discussion context
                comments = fetch_hn_comments(story.get("kids", []), MAX_HN_COMMENTS)

                stories.append({
                    "source": "HackerNews",
                    "title": story.get("title", ""),
                    "summary": f"{story.get('score', 0)} points, {story.get('descendants', 0)} comments",
                    "link": story.get("url", f"https://news.ycombinator.com/item?id={story_id}"),
                    "comments": comments,
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
            comments = s.get("comments", [])
            if comments:
                hn_block += "  TOP COMMENTS FROM THE DISCUSSION:\n"
                for c in comments:
                    hn_block += f"    - {c}\n"
            hn_block += "\n"

    return f"""You are a warm, witty, and knowledgeable podcast host creating a personal daily briefing for {date_str}.

Your listener wants a CONVERSATIONAL, engaging podcast covering:
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
    • Punchy cold open / hook
    • Quick "what we're covering today"
    • World news segment
    • Tech news + AI company updates
    • HackerNews highlights
    • Brief sign-off
- Do NOT include ANY section headers, markers, hashtags, or stage directions — just the spoken words
- Never use ##, #, dashes, or any formatting — pure spoken dialogue only
- If you naturally want to transition between topics, use conversational bridges like "Now let's talk about..." instead of markers
- NEVER mention or reference how long the episode is, how many minutes it will take, or any time estimates — do not say things like "over the next 20 minutes" or "in the next few minutes"

DEPTH REQUIREMENTS (very important — do not write a thin, rushed script):
- The complete script MUST be at least 1,800 words, and ideally 2,000 to 2,400 words. This is a firm floor — keep writing until you reach it.
- Spend real time on EACH story. For every story, don't just state the headline — explain what happened, why it matters, who's affected, and add your own brief context, analysis, or reaction. Aim for roughly 3 to 5 substantial spoken sentences per story at minimum.
- Draw connections between related stories, pose rhetorical questions, and give the listener something to think about — this is what fills out the runtime naturally.
- Do NOT summarize everything quickly and wrap up early. A rushed 5-minute episode is a failure. Treat every story as worth a proper, unhurried discussion.
- For HackerNews, don't just list titles — explain what each project or discussion is about and why the tech community cares. Use the TOP COMMENTS provided to convey what people are actually saying: the debates, the praise, the skepticism, the interesting angles. Paraphrase the gist of the discussion in your own words rather than quoting usernames.
- Pay special attention to announcements from Anthropic, OpenAI, and Perplexity — give these extra depth, don't bury them.
- Connect stories where relevant; add brief context or your take.

Remember: the word-count target above is an instruction to YOU for writing — never say the word count or any length/time reference out loud in the script.

Write the full script now, and keep going until you've covered every story with real depth and hit the word-count floor:"""


def generate_script(news: list[dict], hn_stories: list[dict], date_str: str) -> str:
    """Call Claude Haiku to write the podcast script."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = build_prompt(news, hn_stories, date_str)

    print("  Generating script with Claude Haiku...")
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


async def text_to_speech(script: str, output_path: Path):
    """Convert script to MP3 using edge-tts with robust error handling."""
    print(f"  Generating audio → {output_path.name}")

    # Clean problematic characters
    script = script.replace("\u2014", "-").replace("\u2026", "...")
    script = script.replace("\u201c", '"').replace("\u201d", '"')
    script = script.replace("\u2018", "'").replace("\u2019", "'")

    try:
        communicate = edge_tts.Communicate(script, voice=TTS_VOICE, rate=TTS_RATE)
        await communicate.save(str(output_path))
    except Exception as e:
        print(f"  [warn] TTS failed: {e}")
        print("  Attempting chunked TTS generation...")
        sentences = re.split(r'(?<=[.!?])\s+', script)
        chunks = []
        current_chunk = ""

        for sentence in sentences:
            if len(current_chunk) + len(sentence) < 800:
                current_chunk += sentence + " "
            else:
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                current_chunk = sentence + " "
        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        import tempfile
        temp_files = []
        for i, chunk in enumerate(chunks):
            try:
                temp_path = Path(tempfile.gettempdir()) / f"chunk_{i}_{int(datetime.datetime.now().timestamp())}.mp3"
                communicate = edge_tts.Communicate(chunk, voice=TTS_VOICE, rate=TTS_RATE)
                await communicate.save(str(temp_path))
                temp_files.append(temp_path)
                print(f"  Generated chunk {i+1}/{len(chunks)}")
            except Exception as chunk_err:
                print(f"  [warn] Chunk {i} failed: {chunk_err}")
                continue

        if not temp_files:
            raise RuntimeError("Could not generate any audio. Check the script for problematic characters.")

        print("  Combining audio chunks...")
        try:
            import subprocess
            concat_file = Path(tempfile.gettempdir()) / f"concat_{int(datetime.datetime.now().timestamp())}.txt"
            with open(concat_file, 'w') as f:
                for temp_file in temp_files:
                    f.write(f"file '{temp_file}'\n")

            subprocess.run([
                'ffmpeg', '-f', 'concat', '-safe', '0',
                '-i', str(concat_file), '-c', 'copy', str(output_path)
            ], check=True, capture_output=True)

            for temp_file in temp_files:
                temp_file.unlink()
            concat_file.unlink()
        except Exception:
            print("  ffmpeg not available, using first chunk")
            import shutil
            shutil.copy(temp_files[0], output_path)
            for temp_file in temp_files:
                temp_file.unlink()


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

    print("[1/5] Checking recent episodes for duplicate stories...")
    recent_headlines = get_recent_headlines()
    print(f"  Found {len(recent_headlines)} headlines from last {LOOKBACK_DAYS} days")

    print("[2/5] Fetching news...")
    news = fetch_news(recent_headlines)
    print(f"  Got {len(news)} fresh news stories")

    print("[3/5] Fetching HackerNews...")
    hn_stories = fetch_hackernews()
    print(f"  Got {len(hn_stories)} HackerNews stories")

    # Save today's headlines so future runs can skip them
    headlines_file = EPISODE_DIR / f"{ep_slug}.headlines"
    all_titles = [normalize_headline(s["title"]) for s in news]
    headlines_file.write_text("\n".join(all_titles), encoding="utf-8")

    print("[4/5] Writing podcast script...")
    script = generate_script(news, hn_stories, date_str)
    word_count = len(script.split())
    print(f"  Script: {word_count} words (~{word_count // 140} min)")

    (EPISODE_DIR / f"{ep_slug}.txt").write_text(script)

    print("[5/5] Converting to audio...")
    asyncio.run(text_to_speech(script, mp3_path))
    size_mb = mp3_path.stat().st_size / 1_000_000
    print(f"  Audio: {mp3_path} ({size_mb:.1f} MB)")

    update_rss(mp3_path, ep_title, ep_desc, datetime.datetime.now())
    print(f"\n✅ Done! Episode ready: {mp3_path}")


if __name__ == "__main__":
    main()
