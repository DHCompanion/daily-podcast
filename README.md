# 🎙️ My Daily Briefing — Personal Podcast

A free, automated weekday podcast covering world news, tech headlines,
and the best of r/ClaudeAI and r/ChatGPT. Generated fresh every morning
via GitHub Actions and delivered to your phone through a private RSS feed.

---

## What's in this repo

```
generate_podcast.py        ← main script (fetch → script → audio → RSS)
requirements.txt           ← Python dependencies
index.html                 ← GitHub Pages landing page (shows your RSS URL)
.github/workflows/
  daily_podcast.yml        ← GitHub Actions: runs Mon–Fri at 6 AM ET
episodes/                  ← MP3 files + scripts land here (auto-created)
feed.xml                   ← Your RSS feed (auto-created on first run)
```

---

## One-time Setup (~15 minutes)

### Step 1 — Create the GitHub repo

1. Go to [github.com/new](https://github.com/new)
2. Name it `daily-podcast` (or anything you like)
3. Set it to **Public** (required for free GitHub Pages hosting)
4. Click **Create repository**
5. Upload all files from this folder to the repo root

### Step 2 — Enable GitHub Pages

1. In your repo, go to **Settings → Pages**
2. Under **Source**, select **Deploy from a branch**
3. Branch: `main` | Folder: `/ (root)` → **Save**
4. Wait ~2 minutes, then your site is live at:
   `https://YOUR-USERNAME.github.io/daily-podcast`

### Step 3 — Add your Anthropic API key

1. In your repo, go to **Settings → Secrets and variables → Actions**
2. Click **New repository secret**
3. Name: `ANTHROPIC_API_KEY`
4. Value: your key from [console.anthropic.com](https://console.anthropic.com)
5. Click **Add secret**

### Step 4 — Test it manually

1. Go to **Actions** tab in your repo
2. Click **Generate Daily Podcast** in the left sidebar
3. Click **Run workflow → Run workflow**
4. Watch the logs — it takes about 3–5 minutes
5. When done, `episodes/YYYY-MM-DD.mp3` and `feed.xml` appear in your repo

### Step 5 — Subscribe on your phone

Your RSS feed URL is:
```
https://YOUR-USERNAME.github.io/daily-podcast/feed.xml
```

**iPhone:** Pocket Casts, Overcast, or Apple Podcasts → "Add Show by URL" → paste

**Android:** Pocket Casts or AntennaPod → "Add Podcast" → paste URL

New episodes appear automatically every weekday morning. ✅

---

## Customization

All the easy knobs are at the top of `generate_podcast.py`:

| Variable | What it does |
|---|---|
| `TTS_VOICE` | Change the narrator voice |
| `TTS_RATE` | Speed up/slow down speech |
| `NEWS_FEEDS` | Add/remove RSS news sources |
| `SUBREDDITS` | Change which subreddits to cover |
| `MAX_NEWS_STORIES` | How many news items to include |
| `MAX_REDDIT_POSTS` | Posts per subreddit |

### Changing the schedule

Edit `.github/workflows/daily_podcast.yml`, line:
```yaml
- cron: "0 11 * * 1-5"   # 11:00 UTC = 6 AM ET (Mon–Fri)
```
Use [crontab.guru](https://crontab.guru) to build a custom schedule.
`1-5` = Monday through Friday. Change to `*` for every day.

### Add more voices

Browse all available edge-tts voices:
```bash
pip install edge-tts
edge-tts --list-voices | grep en-US
```

Good conversational picks:
- `en-US-BrianMultilingualNeural` — warm male (current default)
- `en-US-AvaMultilingualNeural` — natural female
- `en-US-AndrewMultilingualNeural` — relaxed male

---

## Cost Estimate

| Item | Monthly cost |
|---|---|
| Claude Haiku API (~22 episodes × ~3,500 tokens) | ~$0.30 |
| edge-tts (Microsoft neural voices) | Free |
| GitHub Actions (well within free tier) | Free |
| GitHub Pages hosting | Free |
| **Total** | **~$0.30/month** |

---

## Troubleshooting

**Action fails with "ModuleNotFoundError"**
→ Make sure `requirements.txt` is in the repo root.

**No audio file generated**
→ Check the Actions logs for the edge-tts step. Ensure the script
  generated text before TTS ran.

**RSS not showing up in podcast app**
→ Wait for GitHub Pages to fully deploy (can take 2–5 min after push).
  Try opening the feed.xml URL in your browser first to confirm it loads.

**Episodes not auto-playing in order**
→ Most podcast apps sort by `pubDate` in the RSS. This is set automatically.
