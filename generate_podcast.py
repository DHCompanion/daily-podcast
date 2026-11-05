#!/usr/bin/env python3
"""
Daily Personal Podcast Generator
Fetches news + HackerNews, generates a conversational script via Claude,
converts to MP3 with edge-tts, and updates the RSS feed.
"""

import os
import html
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

HOST_NAME = "Andrew"
TTS_VOICE = "en-US-AndrewNeural"  # Multilingual variants are ~3x slower and time out
TTS_RATE  = "+5%"
MODEL     = "claude-haiku-4-5-20251001"

NEWS_FEEDS = [
    # World News
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://feeds.npr.org/1004/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    # US News
    "https://feeds.npr.org/1003/rss.xml",
    # Tech News
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://www.wired.com/feed/rss",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.feedburner.com/TheHackersNews",
]

AI_FEEDS = [
    # Official lab announcements
    # Anthropic has no official RSS; this community feed scrapes anthropic.com/news
    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml",
    "https://openai.com/news/rss.xml",
    "https://deepmind.google/blog/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://mistral.ai/rss.xml",
    "https://huggingface.co/blog/feed.xml",
    # Independent AI coverage (catches news from labs without feeds: Meta, xAI, Perplexity...)
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://arstechnica.com/ai/feed/",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "https://the-decoder.com/feed/",
    "https://simonwillison.net/atom/everything/",
]

ITEMS_PER_FEED     = 6    # entries considered from each feed
MAX_AGE_HOURS      = 72   # ignore feed entries older than this (covers the weekend gap)
TOP_NEWS_STORIES   = 3    # deep-dive news stories per episode
MAX_AI_STORIES     = 4
HN_CANDIDATES      = 30   # top HN stories scanned before picking
HN_STORIES         = 3    # deep-dive HN stories per episode
MAX_HN_COMMENTS    = 6    # top comments pulled per picked HN story
LOOKBACK_EPISODES  = 5    # prior episodes checked for repeat stories
EPISODE_DIR = Path("episodes")
RSS_FILE = Path("feed.xml")
# ──────────────────────────────────────────────

STOPWORDS = set("""a an the and or but of in on at to for from by with as is are was were be been
has have had it its this that these those after before over under into about amid against new says
say said will could would may might not no how why what who when where than more most up out""".split())


JOKE_TOKEN = "[[DAD_JOKE]]"


def normalize_headline(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    t = re.sub(r"[^\w\s]", "", title.lower())
    return re.sub(r"\s+", " ", t).strip()


def keywords(title: str) -> set[str]:
    """Content-word stems (first 5 chars) so 'murders'/'murderer' or 'Israel's'/'Israeli' match."""
    return {w[:5] for w in normalize_headline(title).split() if w not in STOPWORDS and len(w) > 2}


def is_similar(a: set[str], b: set[str]) -> bool:
    # ponytail: keyword overlap only catches near-rewordings; the Claude selection step
    # handles same-event-different-words repeats semantically.
    shared = len(a & b)
    return shared >= 3 and shared / max(1, min(len(a), len(b))) >= 0.5


def get_recent_coverage() -> tuple[list[str], set[str]]:
    """Headlines + "hn:<id>"/"joke:<id>" tokens from the last few episodes (by episode, not calendar day)."""
    today = datetime.date.today().isoformat()
    files = sorted(f for f in EPISODE_DIR.glob("*.headlines") if f.stem != today)[-LOOKBACK_EPISODES:]
    titles, ids = [], set()
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(("hn:", "joke:")):
                ids.add(line)
            elif line:
                titles.append(line)
    return titles, ids


def fetch_feeds(urls: list[str], recent_kw: list[set[str]]) -> list[dict]:
    """Fresh, non-repeat entries from RSS feeds."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=MAX_AGE_HOURS)
    stories = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            if not feed.entries:
                print(f"  [warn] No entries from {url} (status {feed.get('status', '?')})")
            for entry in feed.entries[:ITEMS_PER_FEED]:
                title = html.unescape(entry.get("title", "")).strip()
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if not title:
                    continue
                if published and datetime.datetime(*published[:6], tzinfo=datetime.timezone.utc) < cutoff:
                    continue
                kw = keywords(title)
                if any(is_similar(kw, r) for r in recent_kw):
                    continue
                summary = entry.get("summary", "") or entry.get("description", "")
                summary = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", summary))).strip()[:600]
                stories.append({
                    "source":  feed.feed.get("title", url),
                    "title":   title,
                    "summary": summary,
                    "link":    entry.get("link", ""),
                    "kw":      kw,
                })
        except Exception as e:
            print(f"  [warn] Feed error {url}: {e}")
    return stories


def hn_get(path: str, timeout: int = 5):
    req = urllib.request.Request(f"https://hacker-news.firebaseio.com/v0/{path}.json",
                                 headers={"User-Agent": "daily-podcast-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def clean_hn_html(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def fetch_hn_comments(kid_ids: list, limit: int) -> list[str]:
    """Top-level comments for a HackerNews story."""
    comments = []
    for kid_id in kid_ids[:limit * 2]:  # some will be dead/deleted
        if len(comments) >= limit:
            break
        try:
            kid = hn_get(f"item/{kid_id}")
            if kid and not kid.get("dead") and not kid.get("deleted") and kid.get("text"):
                comments.append(clean_hn_html(kid["text"])[:500])
        except Exception as e:
            print(f"  [warn] HN comment {kid_id} fetch error: {e}")
    return comments


def fetch_hackernews(recent_kw: list[set[str]], recent_ids: set[str]) -> list[dict]:
    """Top HN stories, minus ones we've already covered."""
    stories = []
    try:
        for story_id in hn_get("topstories", timeout=10)[:HN_CANDIDATES]:
            try:
                if f"hn:{story_id}" in recent_ids:
                    continue
                story = hn_get(f"item/{story_id}")
                if not story or story.get("type") not in ("story", "poll") or story.get("dead") or story.get("deleted"):
                    continue
                kw = keywords(story.get("title", ""))
                if any(is_similar(kw, r) for r in recent_kw):
                    continue
                stories.append({
                    "id":      str(story_id),
                    "source":  "HackerNews",
                    "title":   story.get("title", ""),
                    "summary": f"{story.get('score', 0)} points, {story.get('descendants', 0)} comments",
                    "text":    clean_hn_html(story.get("text", ""))[:1000],
                    "link":    story.get("url", f"https://news.ycombinator.com/item?id={story_id}"),
                    "kids":    story.get("kids", []),
                    "kw":      kw,
                })
            except Exception as e:
                print(f"  [warn] HN story {story_id} fetch error: {e}")
    except Exception as e:
        print(f"  [warn] HackerNews fetch error: {e}")
    return stories


def fetch_dad_joke(seen: set[str]) -> tuple[str, str]:
    """(id, joke) from icanhazdadjoke.com, skipping recently used ones. ("", "") if the site is down."""
    for attempt in range(5):
        try:
            req = urllib.request.Request("https://icanhazdadjoke.com/",
                                         headers={"Accept": "application/json", "User-Agent": "daily-podcast-bot/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if f"joke:{data['id']}" not in seen:
                return data["id"], data["joke"].strip()
        except Exception as e:
            print(f"  [warn] Dad joke fetch error: {e}")
    return "", ""


def insert_joke(script: str, joke: str) -> str:
    """Put the website's joke into the script verbatim, so Claude never rewrites or invents it."""
    if not joke:
        return script.replace(JOKE_TOKEN, "")
    if JOKE_TOKEN in script:
        return script.replace(JOKE_TOKEN, joke, 1).replace(JOKE_TOKEN, "")
    # Token missing: tell it right before the final goodbye paragraph
    head, sep, tail = script.rstrip().rpartition("\n\n")
    return f"{head}\n\nOne more thing before I go, here's a dad joke for you. {joke}\n\n{tail}" if sep else f"{script}\n\n{joke}"


def numbered(items: list[dict]) -> str:
    return "\n".join(f"[{i}] ({s['source']}) {s['title']} | {s['summary'][:200]}" for i, s in enumerate(items))


def pick_stories(client, news: list[dict], ai: list[dict], hn: list[dict], recent_titles: list[str]) -> dict:
    """Ask Claude to choose today's stories, skipping anything already covered."""
    prompt = f"""You are the producer of a daily news podcast. Choose today's stories from the numbered candidates.

ALREADY COVERED IN RECENT EPISODES (do NOT pick the same event again, even if worded differently,
unless there is a major new development, like a verdict, a death toll, a resolution):
{chr(10).join(recent_titles) or "(none)"}

=== NEWS CANDIDATES ===
{numbered(news)}

=== AI CANDIDATES ===
{numbered(ai)}

=== HACKERNEWS CANDIDATES (in rank order) ===
{numbered(hn)}

Pick:
- "news": the {TOP_NEWS_STORIES} most significant news stories. Each must be a DIFFERENT event. Favor stories
  several outlets are covering, and real consequence over curiosity pieces. Mix world, US, and tech if warranted.
- "ai": up to {MAX_AI_STORIES} genuinely notable AI stories (model launches, major research, policy, big business moves).
  Favor official lab announcements. Skip tutorials, minor product tweaks, and duplicates of the same event.
- "hn": the {HN_STORIES} most interesting HackerNews stories for a tech-savvy listener; skip any that duplicate a news/AI pick.

Reply with ONLY JSON, no prose: {{"news": [indices], "ai": [indices], "hn": [indices]}}"""
    try:
        msg = client.messages.create(model=MODEL, max_tokens=200, messages=[{"role": "user", "content": prompt}])
        picks = json.loads(re.search(r"\{.*\}", msg.content[0].text, re.S).group(0))
    except Exception as e:
        print(f"  [warn] Story selection failed ({e}); falling back to feed order")
        picks = {}

    def choose(key, pool, n):
        idx = [i for i in picks.get(key, []) if isinstance(i, int) and 0 <= i < len(pool)]
        return [pool[i] for i in dict.fromkeys(idx)][:n] or pool[:n]

    return {"news": choose("news", news, TOP_NEWS_STORIES),
            "ai":   choose("ai", ai, MAX_AI_STORIES),
            "hn":   choose("hn", hn, HN_STORIES)}


def related_coverage(story: dict, pool: list[dict]) -> list[dict]:
    """Other outlets' takes on the same event, for a deeper segment."""
    return [s for s in pool if s is not story and len(story["kw"] & s["kw"]) >= 2][:3]


def build_prompt(news: list[dict], news_pool: list[dict], ai: list[dict], hn: list[dict],
                 dad_joke: str, date_str: str) -> str:
    news_block = ""
    for i, s in enumerate(news, 1):
        news_block += f"\nTOP STORY {i}\nSOURCE: {s['source']}\nHEADLINE: {s['title']}\nSUMMARY: {s['summary']}\n"
        for r in related_coverage(s, news_pool):
            news_block += f"  ALSO REPORTED BY {r['source']}: {r['title']} | {r['summary']}\n"

    ai_block = "\n".join(f"SOURCE: {s['source']}\nHEADLINE: {s['title']}\nSUMMARY: {s['summary']}\n" for s in ai) \
        or "(no notable AI news today; skip this segment gracefully)"

    hn_block = ""
    for s in hn:
        hn_block += f"\nTITLE: {s['title']}\nLINK: {s['link']}\nSTATS: {s['summary']}\n"
        if s.get("text"):
            hn_block += f"POST TEXT: {s['text']}\n"
        if s.get("comments"):
            hn_block += "TOP COMMENTS:\n" + "".join(f"  - {c}\n" for c in s["comments"])

    joke_rules = f"""THE DAD JOKE (the ending): tee it up naturally in a sentence or two, then write the exact
placeholder {JOKE_TOKEN} on its own line. The joke's text is inserted there word for word, so do NOT write,
paraphrase, retell, or explain the joke yourself, and never make up a different joke. After the placeholder,
react briefly (groan at yourself, or stand by it proudly), then a quick, warm goodbye.""" if dad_joke else \
        "THE DAD JOKE: none today. Do not tell or invent any joke; just end with a quick, warm goodbye."
    joke_line = f'DAD JOKE (from icanhazdadjoke.com, for your reference only): "{dad_joke}"' if dad_joke else ""

    return f"""You are {HOST_NAME}, the warm, witty, and sharp host of a personal daily briefing podcast. Today is {date_str}.

=== TODAY'S TOP NEWS STORIES ===
{news_block}
=== AI NEWS ===
{ai_block}

=== HACKERNEWS PICKS ===
{hn_block}
{joke_line}

FLOW (a guide, not a checklist; the listener should never hear the seams):
  Open by talking about the biggest story like you just can't wait to tell someone, then say who you are
  ({HOST_NAME}) and what else is coming up. Work through the three top stories, then the AI news, then the
  HackerNews picks, and close with the dad joke (if one is provided) as the very last thing before you say goodbye.

TOP STORIES (the heart of the show, go deep): for each of the three, tell the story: what happened, how we
got here, who it affects, how different outlets are framing it (use the ALSO REPORTED BY material), and what
to watch next. Roughly 350 to 450 spoken words each.

AI NEWS: explain each item plainly, why it matters, and how it fits the competitive landscape.
Give official lab announcements (Anthropic, OpenAI, Google, and others) the most depth.

HACKERNEWS (go deep): for each of the three, explain what the project or article actually is, why the tech
crowd cares, and what the discussion is saying: the debates, praise, skepticism, and interesting tangents
from the TOP COMMENTS. Paraphrase; never quote usernames. Roughly 200 to 300 spoken words each.

{joke_rules}

STYLE:
- Write the FULL spoken script, word for word, as one person talking to a friend over coffee, not
  presenting a list. Let stories flow into each other with real connective thought ("which actually ties
  into...", "okay, switching gears completely..."), share your own reactions and opinions, ask the listener
  questions, and vary sentence length. Never march through items like "First... Second... Next up...".
- Never use segment labels or announce sections ("AI news roundup", "HackerNews deep dive number two").
- Pure spoken words only: no headers, markers, hashtags, bullet points, stage directions, or "---" separators.
- Never mention the episode's length, time, or word count.
- The full script should be 2,000 to 2,600 words. Don't rush or wrap up early.

Write the full script now:"""


def generate_script(client, prompt: str) -> str:
    print(f"  Generating script with {MODEL}...")
    message = client.messages.create(model=MODEL, max_tokens=8192,
                                     messages=[{"role": "user", "content": prompt}])
    return message.content[0].text

async def text_to_speech(script: str, output_path: Path):
    """Convert script to MP3 with edge-tts, in chunks (long single requests fail)."""
    print(f"  Generating audio → {output_path.name}")

    script = script.replace("—", ", ").replace("–", "-").replace("…", "...")
    script = script.replace("“", '"').replace("”", '"')
    script = script.replace("‘", "'").replace("’", "'")

    # Split on sentence boundaries into ~2,500-char chunks
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", script):
        if current and len(current) + len(sentence) > 2500:
            chunks.append(current.strip())
            current = ""
        current += sentence + " "
    if current.strip():
        chunks.append(current.strip())

    audio = bytearray()
    for i, chunk in enumerate(chunks, 1):
        for attempt in range(1, 5):
            try:
                part = bytearray()
                async for msg in edge_tts.Communicate(chunk, voice=TTS_VOICE, rate=TTS_RATE).stream():
                    if msg["type"] == "audio":
                        part += msg["data"]
                if not part:
                    raise RuntimeError("no audio received")
                audio += part
                print(f"  Chunk {i}/{len(chunks)} ok")
                break
            except Exception as e:
                print(f"  [warn] Chunk {i} attempt {attempt} failed: {e}")
                await asyncio.sleep(5 * attempt)
        else:
            # Fail the run rather than publish an episode with a hole in it
            raise RuntimeError(f"TTS chunk {i} failed after retries: {chunk[:80]!r}")

    # edge-tts emits headerless MP3 frames, so plain concatenation is a valid MP3
    output_path.write_bytes(audio)


def get_mp3_duration_seconds(path: Path) -> int:
    return int(path.stat().st_size / 6000)  # edge-tts default: 48 kbps mono MP3


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
    ep_desc  = f"Top stories, AI news, HackerNews deep dives, and a dad joke for {date_str}."

    EPISODE_DIR.mkdir(exist_ok=True)
    mp3_path = EPISODE_DIR / f"{ep_slug}.mp3"

    if mp3_path.exists():
        print(f"Episode already exists: {mp3_path}. Delete it to regenerate.")
        return

    print(f"\n{'='*50}")
    print(f"  Generating episode: {ep_title}")
    print(f"{'='*50}\n")

    print("[1/6] Checking recent episodes for repeat stories...")
    recent_titles, recent_ids = get_recent_coverage()
    recent_kw = [keywords(t) for t in recent_titles]
    print(f"  {len(recent_titles)} stories covered in the last {LOOKBACK_EPISODES} episodes")

    print("[2/6] Fetching news, AI news, HackerNews, and a dad joke...")
    news_pool = fetch_feeds(NEWS_FEEDS, recent_kw)
    ai_pool   = fetch_feeds(AI_FEEDS, recent_kw)
    hn_pool   = fetch_hackernews(recent_kw, recent_ids)
    joke_id, dad_joke = fetch_dad_joke(recent_ids)
    print(f"  Dad joke: {dad_joke or '(site unavailable, skipping)'}")
    print(f"  Candidates: {len(news_pool)} news, {len(ai_pool)} AI, {len(hn_pool)} HN")

    print("[3/6] Picking today's stories...")
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    picks = pick_stories(client, news_pool, ai_pool, hn_pool, recent_titles)
    for key, items in picks.items():
        for s in items:
            print(f"  {key:>4}: {s['title']}")
    for s in picks["hn"]:
        s["comments"] = fetch_hn_comments(s["kids"], MAX_HN_COMMENTS)

    # Record only what we actually cover, so future runs skip it
    covered = [s["title"] for k in ("news", "ai", "hn") for s in picks[k]]
    covered += [f"hn:{s['id']}" for s in picks["hn"]]
    covered += [f"joke:{joke_id}"] if joke_id else []
    (EPISODE_DIR / f"{ep_slug}.headlines").write_text("\n".join(covered) + "\n", encoding="utf-8")

    print("[4/6] Writing podcast script...")
    prompt = build_prompt(picks["news"], news_pool, picks["ai"], picks["hn"], dad_joke, date_str)
    script = insert_joke(generate_script(client, prompt), dad_joke)
    word_count = len(script.split())
    print(f"  Script: {word_count} words (~{word_count // 140} min)")
    (EPISODE_DIR / f"{ep_slug}.txt").write_text(script)

    print("[5/6] Converting to audio...")
    asyncio.run(text_to_speech(script, mp3_path))
    size_mb = mp3_path.stat().st_size / 1_000_000
    print(f"  Audio: {mp3_path} ({size_mb:.1f} MB)")

    print("[6/6] Updating RSS...")
    update_rss(mp3_path, ep_title, ep_desc, datetime.datetime.now())
    print(f"\n✅ Done! Episode ready: {mp3_path}")


if __name__ == "__main__":
    main()
