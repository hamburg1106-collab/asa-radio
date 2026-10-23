"""あさラジ: RSSでニュースを集め、Geminiで台本と2人の声を作り、ポッドキャストのフィードを更新する。

使い方:
  python make_episode.py --out site            # 本番（GEMINI_API_KEY が必要）
  python make_episode.py --out site --dry-run  # APIを使わず、仮の台本と無音で流れだけ確認
"""
import argparse
import base64
import email.utils
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

SHOW = {
    "title": "あさラジ",
    "description": "AI・テックと経済のニュースを、毎朝10分の掛け合いでお届けする自分用ラジオ。",
    "base_url": "https://hamburg1106-collab.github.io/asa-radio/",
    "keep": 14,  # 残すエピソード数
}

FEEDS = [
    # NHKのRSS(cat5)は2026-08で更新が止まっているので使わない
    ("Googleニュース 生成AI", "https://news.google.com/rss/search?q=%E7%94%9F%E6%88%90AI+when:1d&hl=ja&gl=JP&ceid=JP:ja"),
    ("ITmedia AI+", "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml"),
    ("Publickey", "https://www.publickey1.jp/atom.xml"),
    ("ITmedia ビジネス", "https://rss.itmedia.co.jp/rss/2.0/business.xml"),
    ("Googleニュース 経済", "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ja&gl=JP&ceid=JP:ja"),
]

# 台本上の名前 → TTSに渡す名前と声。声は聞き比べで差し替える前提
HOSTS = {
    "ソラ": {"tts": "Sora", "voice": "Sulafat", "role": "進行役。落ち着いていて要点をまとめるのがうまい"},
    "ユウ": {"tts": "Yu", "voice": "Puck", "role": "聞き役。素朴な疑問を投げ、生活やお金への影響を聞きたがる"},
}

TEXT_MODEL = os.environ.get("TEXT_MODEL", "gemini-3.5-flash")
TTS_MODELS = os.environ.get("TTS_MODELS", "gemini-3.8-flash-tts,gemini-3.8-flash-lite-tts").split(",")
API = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
TTS_CHUNK_CHARS = 900  # 1回のTTSに渡す文字数の目安（約3分）
TTS_GAP_SEC = 21       # 無料枠は1分3回まで
SAMPLE_RATE = 24000


def log(*a):
    print(*a, flush=True)


# ---------- ニュース収集 ----------

def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_date(s):
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_feed(source, url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (asa-radio)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    items = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        f = {c.tag.split("}")[-1]: c for c in el}
        link = f.get("link")
        href = (link.get("href") or link.text or "") if link is not None else ""
        text = lambda k: (f[k].text or "") if k in f else ""
        items.append({
            "source": source,
            "title": strip_html(text("title")),
            "link": href.strip(),
            "summary": strip_html(text("description") or text("summary") or text("content"))[:300],
            "date": parse_date(text("pubDate") or text("date") or text("updated") or text("published")),
        })
    return items


def collect_news(hours=30, limit=60):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen, news = set(), []
    for source, url in FEEDS:
        try:
            items = fetch_feed(source, url)
        except Exception as e:  # 1つ落ちても他で続ける
            log(f"  ! {source} 取得失敗: {e}")
            continue
        fresh = [i for i in items if i["date"] is None or i["date"] >= since]
        log(f"  {source}: {len(fresh)}件")
        for i in fresh[:12]:  # 件数の多い配信元で偏らないように
            key = re.sub(r"\W", "", i["title"])[:20]
            if i["title"] and key not in seen:
                seen.add(key)
                news.append(i)
    return news[:limit]


# ---------- Gemini ----------

class ApiError(Exception):
    def __init__(self, model, status, body):
        super().__init__(f"{model} HTTP {status}: {body[:300]}")
        self.status = status


def gemini(model, body, retries=4):
    key = os.environ["GEMINI_API_KEY"]
    data = json.dumps(body).encode()
    for n in range(retries):
        req = urllib.request.Request(API.format(model), data=data, headers={
            "x-goog-api-key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            err = ApiError(model, e.code, e.read().decode(errors="replace"))
            # 429は使いすぎなので待っても無駄。503/500だけ待って再試行
            if e.code in (500, 503) and n < retries - 1:
                log(f"  {err} → {20 * (n + 1)}秒後に再試行")
                time.sleep(20 * (n + 1))
                continue
            raise err


def text_of(resp):
    parts = resp["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def research(news, today):
    """Google検索で各話題を裏取り・補足する。使えなければRSSだけで進める"""
    listing = "\n".join(f"- [{n['source']}] {n['title']} / {n['summary']}" for n in news)
    prompt = f"""今日は{today}。以下は直近のニュース見出しです。
AI・テックと経済・お金の話題から、聞き手の生活や仕事に関係の深いものを5本選び、
それぞれGoogle検索で事実関係を確認し、背景・数字・今後の見通しをメモにしてください。
確認できなかったことは「未確認」と書くこと。

{listing}"""
    try:
        resp = gemini(TEXT_MODEL, {
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
        }, retries=2)
        notes = text_of(resp)
        log(f"  検索メモ {len(notes)}字")
        return notes
    except Exception as e:
        log(f"  ! 検索での補足を省略: {e}")
        return ""


def write_script(news, notes, today):
    listing = "\n".join(
        f"{i}. [{n['source']}] {n['title']} / {n['summary']} / {n['link']}" for i, n in enumerate(news))
    hosts = "\n".join(f"- {k}: {v['role']}" for k, v in HOSTS.items())
    prompt = f"""あなたは朝のニュースラジオ「{SHOW['title']}」の構成作家です。今日は{today}。
次の2人の掛け合いで、約10分（全体で2800〜3200字）の台本を書いてください。

{hosts}

ルール:
- AI・テックと経済・お金の話題を合わせて5本。冒頭のあいさつ（曜日に触れる）→各話題→今日のまとめ、の順
- 各話題は「何が起きたか→なぜ大事か→暮らし・仕事への影響」を会話で自然に
- 材料にない事実を作らない。不確かなことは「まだはっきりしない」と言う
- 記事の文章を写さず、話し言葉で言い換える。出典名（NHKによると、など）は自然に入れる
- 1回の発言は長くても120字程度。相づちだけの発言を連発しない
- 読み上げるので、記号・英略語は読みやすく（例: 「AI」はそのままでよい、「%」は「パーセント」）
- topics には取り上げた話題を、材料の番号（index）付きで入れる

# 材料（RSS）
{listing}

# 検索メモ
{notes or "（なし）"}"""
    schema = {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING", "description": "今日の回のタイトル（30字以内）"},
            "topics": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "headline": {"type": "STRING"}, "index": {"type": "INTEGER"}},
                "required": ["headline", "index"]}},
            "turns": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "speaker": {"type": "STRING", "enum": list(HOSTS)}, "text": {"type": "STRING"}},
                "required": ["speaker", "text"]}},
        },
        "required": ["title", "topics", "turns"],
    }
    resp = gemini(TEXT_MODEL, {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
    })
    script = json.loads(text_of(resp))
    for t in script["topics"]:
        n = news[t["index"]] if 0 <= t["index"] < len(news) else {}
        t["source"], t["link"] = n.get("source", ""), n.get("link", "")
    return script


def chunk_turns(turns):
    chunks, cur, size = [], [], 0
    for t in turns:
        if cur and size + len(t["text"]) > TTS_CHUNK_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t["text"])
    if cur:
        chunks.append(cur)
    return chunks


def tts(chunk):
    lines = "\n".join(f"{HOSTS[t['speaker']]['tts']}: {t['text']}" for t in chunk)
    text = ("朝のニュースラジオの2人の会話です。明るく落ち着いたトーンで、"
            "ラジオらしい自然なテンポと間で読んでください。\n\n" + lines)
    speakers = [{"speaker": h["tts"], "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": h["voice"]}}}
                for h in HOSTS.values()]
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": speakers}},
        },
    }
    last = None
    for model in TTS_MODELS:  # 枠切れ(429)なら次のモデルへ
        try:
            resp = gemini(model.strip(), body)
            part = resp["candidates"][0]["content"]["parts"][0]
            return base64.b64decode(part["inlineData"]["data"]), model.strip()
        except ApiError as e:
            last = e
            if e.status != 429:
                raise
            log(f"  {e} → 次のモデルへ")
    raise last


# ---------- 音声・フィード ----------

def ffmpeg_bin():
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg  # ローカル確認用
    return imageio_ffmpeg.get_ffmpeg_exe()


def to_mp3(pcm, out):
    subprocess.run([
        ffmpeg_bin(), "-y", "-loglevel", "error",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "-",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "44100", "-ac", "1", "-b:a", "64k", str(out),
    ], input=pcm, check=True)


def build_feed(site, episodes):
    base = SHOW["base_url"]
    items = []
    for ep in episodes:
        notes = "".join(f'<li><a href="{html.escape(t["link"])}">{html.escape(t["headline"])}</a>（{html.escape(t["source"])}）</li>'
                        for t in ep["topics"])
        items.append(f"""    <item>
      <title>{html.escape(ep['date_label'] + ' ' + ep['title'])}</title>
      <guid isPermaLink="false">asa-radio-{ep['id']}</guid>
      <pubDate>{ep['pub_date']}</pubDate>
      <enclosure url="{base}episodes/{ep['id']}.mp3" length="{ep['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{ep['seconds']}</itunes:duration>
      <description><![CDATA[<ul>{notes}</ul>]]></description>
    </item>""")
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{SHOW['title']}</title>
    <link>{base}</link>
    <atom:link href="{base}feed.xml" rel="self" type="application/rss+xml"/>
    <description>{SHOW['description']}</description>
    <language>ja</language>
    <itunes:author>{SHOW['title']}</itunes:author>
    <itunes:image href="{base}cover.png"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>yes</itunes:block>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (site / "feed.xml").write_text(feed, encoding="utf-8")

    rows = "".join(f"""<section><h2>{html.escape(ep['date_label'])} {html.escape(ep['title'])}</h2>
<audio controls preload="none" src="episodes/{ep['id']}.mp3"></audio>
<ul>{''.join(f'<li><a href="{html.escape(t["link"])}">{html.escape(t["headline"])}</a></li>' for t in ep['topics'])}</ul></section>"""
                   for ep in episodes)
    (site / "index.html").write_text(f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{SHOW['title']}</title><style>body{{font-family:sans-serif;max-width:640px;margin:auto;padding:16px;line-height:1.6}}
audio{{width:100%}}h2{{font-size:1rem;margin-top:2rem}}</style></head><body>
<h1>{SHOW['title']}</h1><p>ポッドキャストアプリで「URLで追加」: <code>{base}feed.xml</code></p>{rows}</body></html>
""", encoding="utf-8")
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    site = Path(args.out)
    (site / "episodes").mkdir(parents=True, exist_ok=True)
    cover = Path(__file__).with_name("cover.png")
    if cover.exists():
        shutil.copy(cover, site / "cover.png")

    now = datetime.now(JST)
    weekday = "月火水木金土日"[now.weekday()]
    today = f"{now.year}年{now.month}月{now.day}日（{weekday}）"
    ep_id = now.strftime("%Y%m%d")

    log("1. ニュース収集")
    news = collect_news()
    if len(news) < 5:
        sys.exit(f"ニュースが{len(news)}件しか取れなかったので中止")

    if args.dry_run:
        script = {"title": "（試運転）", "topics": [{"headline": n["title"], "source": n["source"], "link": n["link"]} for n in news[:5]],
                  "turns": [{"speaker": "ソラ", "text": n["title"]} for n in news[:5]]}
        pcm = b"\x00\x00" * SAMPLE_RATE * 3
    else:
        log("2. 検索で裏取り")
        notes = research(news, today)
        log("3. 台本づくり")
        script = write_script(news, notes, today)
        log(f"  「{script['title']}」 {sum(len(t['text']) for t in script['turns'])}字 / {len(script['turns'])}発言")
        log("4. 読み上げ")
        pcm, gap = b"", b"\x00\x00" * int(SAMPLE_RATE * 0.4)
        for i, chunk in enumerate(chunk_turns(script["turns"])):
            if i:
                time.sleep(TTS_GAP_SEC)
            audio, model = tts(chunk)
            log(f"  {i + 1}本目 {len(audio) / 2 / SAMPLE_RATE:.0f}秒 ({model})")
            pcm += (gap if pcm else b"") + audio

    log("5. mp3とフィード")
    mp3 = site / "episodes" / f"{ep_id}.mp3"
    to_mp3(pcm, mp3)
    (site / "episodes" / f"{ep_id}.json").write_text(json.dumps(script, ensure_ascii=False, indent=1), encoding="utf-8")

    index_path = site / "episodes.json"
    episodes = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    episodes = [e for e in episodes if e["id"] != ep_id]
    episodes.insert(0, {
        "id": ep_id, "title": script["title"], "date_label": f"{now.month}/{now.day}({weekday})",
        "pub_date": email.utils.format_datetime(now), "bytes": mp3.stat().st_size,
        "seconds": round(len(pcm) / 2 / SAMPLE_RATE),
        "topics": [{k: t.get(k, "") for k in ("headline", "source", "link")} for t in script["topics"]],
    })
    for old in episodes[SHOW["keep"]:]:
        for ext in ("mp3", "json"):
            (site / "episodes" / f"{old['id']}.{ext}").unlink(missing_ok=True)
    episodes = episodes[:SHOW["keep"]]
    index_path.write_text(json.dumps(episodes, ensure_ascii=False, indent=1), encoding="utf-8")
    build_feed(site, episodes)
    log(f"完了: {mp3}（{episodes[0]['seconds']}秒）")


if __name__ == "__main__":
    main()
