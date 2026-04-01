#!/usr/bin/env python3
"""
podcastify.py — Satipanya Buddhist Retreat Audio Archive → Podcast Pipeline

Converts the audio archive at satipanya.org.uk and the YouTube channel into 7
structured podcast feeds with transcripts, AI-generated descriptions, cover art,
and RSS feeds.

Pass 1: catalog     — Scrape all pages, build catalog.json
Pass 2: probe       — Get duration/size via ffprobe on remote URLs
Pass 3: transcribe  — WhisperX transcription → .srt + .txt
Pass 4: describe    — Claude AI → episode descriptions & metadata
Pass 5: covers      — Generate podcast cover images
Pass 6: feeds       — Build RSS 2.0 podcast feeds

Usage:
    python podcastify.py catalog
    python podcastify.py probe
    python podcastify.py transcribe
    python podcastify.py describe
    python podcastify.py covers
    python podcastify.py feeds
    python podcastify.py all
"""

import json, os, re, sys, time, subprocess
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Tuple
from pathlib import Path
from urllib.parse import urljoin, unquote, quote

import requests
from bs4 import BeautifulSoup, Tag

# ============================================================
# Constants
# ============================================================

BASE_URL = "https://www.satipanya.org.uk"
PROJECT_DIR = Path(__file__).parent
CATALOG_PATH = PROJECT_DIR / "catalog.json"
TRANSCRIPTS_DIR = PROJECT_DIR / "transcripts"
METADATA_DIR = PROJECT_DIR / "metadata"
COVERS_DIR = PROJECT_DIR / "covers"
FEEDS_DIR = PROJECT_DIR / "feeds"

AUDIO_EXTENSIONS = ('.mp3', '.mp4', '.m4a', '.wav', '.ogg')

REQUEST_HEADERS = {
    "User-Agent": "SatipanyaPodcastBot/1.0 (archiving for podcast feed generation)"
}
REQUEST_DELAY = 1.0  # seconds between page fetches (polite crawling)


# ============================================================
# Feed definitions
# ============================================================

FEEDS = {
    "guided_meditations": {
        "name": "Satipanya — Guided Meditations",
        "slug": "guided-meditations",
        "author": "Bhante Bodhidhamma",
        "description": (
            "Guided Vipassana meditation practices and Pali chanting "
            "from Satipanya Buddhist Retreat, in the tradition of Mahasi Sayadaw."
        ),
        "language": "en",
        "category": "Religion & Spirituality",
        "subcategory": "Buddhism",
    },
    "dhammabytes": {
        "name": "Satipanya — DhammaBytes",
        "slug": "dhammabytes",
        "author": "Bhante Bodhidhamma",
        "description": (
            "Short teachings on core Buddhist concepts — Pali terminology, "
            "the Perfections, Dependent Origination, the Discourses, and "
            "selections from 'In The Buddha's Words' by Bhikkhu Bodhi."
        ),
        "language": "en",
        "category": "Religion & Spirituality",
        "subcategory": "Buddhism",
    },
    "foundation_course": {
        "name": "Satipanya — A Foundation Course in Buddhism",
        "slug": "foundation-course",
        "author": "Bhante Bodhidhamma",
        "description": (
            "A structured two-part course covering the Four Noble Truths, "
            "Dependent Origination, Kamma, morality, the Sangha, and mental "
            "development in the Theravāda Buddhist tradition."
        ),
        "language": "en",
        "category": "Religion & Spirituality",
        "subcategory": "Buddhism",
    },
    "dharma_talks": {
        "name": "Satipanya — Dharma Talks",
        "slug": "dharma-talks",
        "author": "Bhante Bodhidhamma",
        "description": (
            "Dhamma talks by Bhante Bodhidhamma on meditation practice, "
            "Buddhist philosophy, ethics, and daily life. Includes Full Moon "
            "Observance Day talks, retreat recordings, and special series."
        ),
        "language": "en",
        "category": "Religion & Spirituality",
        "subcategory": "Buddhism",
    },
    "noirins_teachings": {
        "name": "Satipanya — Noirin's Teachings",
        "slug": "noirins-teachings",
        "author": "Noirin Sheahan",
        "description": (
            "Dhamma talks and retreat teachings by Noirin Sheahan, co-founder "
            "of Satipanya Buddhist Retreat. Covers the Four Noble Truths, "
            "meditation practice, the Brahmavihāra, and the Satipaṭṭhāna."
        ),
        "language": "en",
        "category": "Religion & Spirituality",
        "subcategory": "Buddhism",
    },
    "international_talks": {
        "name": "Satipanya — International Talks",
        "slug": "international-talks",
        "author": "Bhante Bodhidhamma",
        "description": (
            "Dhamma talks with accompanying translations in French, Czech, "
            "and Italian, from retreats and teachings across Europe."
        ),
        "language": "en",
        "category": "Religion & Spirituality",
        "subcategory": "Buddhism",
    },
    "youtube_channel": {
        "name": "Satipanya — YouTube Talks",
        "slug": "youtube-talks",
        "author": "Bhante Bodhidhamma",
        "description": (
            "Dharma talks and teachings from the Satipanya Insight YouTube "
            "channel — short to medium-length talks on Buddhist philosophy, "
            "meditation practice, the Dhammapada, Satipaṭṭhāna, and daily life."
        ),
        "language": "en",
        "category": "Religion & Spirituality",
        "subcategory": "Buddhism",
    },
}

YOUTUBE_CHANNEL_ID = "UC9VUwz45IDTMak1Qk-pMCVw"
YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@satipanya-insight/videos"


# ============================================================
# Page configurations — what to scrape and how to map it
# ============================================================
#
# parse_mode:
#   "flat"     → all audio on the page = one season
#   "sections" → h2/h3/h4 headings define seasons automatically
#
# For "sections" mode, season_number is auto-assigned based on
# heading order within the page. For feeds with multiple pages
# using sections mode, seasons are numbered sequentially across pages.

PAGES = [
    # ── Feed: guided_meditations ─────────────────────────────
    {
        "url": "/audio-video/",
        "feed_id": "guided_meditations",
        "season_name": "Guided Meditations",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
    },
    {
        "url": "/chanting/",
        "feed_id": "guided_meditations",
        "season_name": "Chanting",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
    },

    # ── Feed: dhammabytes ────────────────────────────────────
    {
        "url": "/audio-video/dhammabytes/",
        "feed_id": "dhammabytes",
        "parse_mode": "sections",
        "speaker": "Bhante Bodhidhamma",
    },

    # ── Feed: foundation_course ──────────────────────────────
    {
        "url": "/audio-video/a-foundation-course-in-buddhism/",
        "feed_id": "foundation_course",
        "season_name": "Part 1 — The Four Noble Truths",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
    },
    {
        "url": "/audio-video/a-foundation-course-in-buddhism-part-2/",
        "feed_id": "foundation_course",
        "season_name": "Part 2 — Deepening Understanding",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
    },

    # ── Feed: dharma_talks ───────────────────────────────────
    {
        "url": "/audio-video/a-collection-of-talks/",
        "feed_id": "dharma_talks",
        "parse_mode": "sections",
        "speaker": "Bhante Bodhidhamma",
    },
    {
        "url": "/audio-video/gaia-house-retreat-2011/",
        "feed_id": "dharma_talks",
        "season_name": "Gaia House Retreat 2011",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
    },
    {
        "url": "/audio-video/christmas-at-sharpham-2005/",
        "feed_id": "dharma_talks",
        "season_name": "Christmas at Sharpham 2005",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
    },
    {
        "url": "/christmas-and-new-year-20067/",
        "feed_id": "dharma_talks",
        "season_name": "Christmas & New Year 2006/7",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
    },
    {
        "url": "/audio-video/recent-talks/",
        "feed_id": "dharma_talks",
        "parse_mode": "sections",
        "speaker": "Bhante Bodhidhamma",
    },
    {
        "url": "/coronavirus-lockdown-talks-and-meditation/",
        "feed_id": "dharma_talks",
        "season_name": "Coronavirus Lockdown 2020",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
    },
    {
        "url": "/full-moon-observance-day/",
        "feed_id": "dharma_talks",
        "parse_mode": "sections",
        "speaker": "Bhante Bodhidhamma",
    },
    {
        "url": "/recent-courses-2021/",
        "feed_id": "dharma_talks",
        "season_name": "Recent Courses 2020–2023",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
    },
    {
        "url": "/audio-video/encouragements/",
        "feed_id": "dharma_talks",
        "season_name": "Dramatisations",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
    },

    # ── Feed: noirins_teachings ──────────────────────────────
    {
        "url": "/noirins-teachings/",
        "feed_id": "noirins_teachings",
        "parse_mode": "sections",
        "speaker": "Noirin Sheahan",
        "heading_tags": ["strong"],  # page uses <strong> for section headings
    },

    # ── Feed: international_talks ────────────────────────────
    {
        "url": "/audio-video/talks-with-an-accompanying-french-translation/",
        "feed_id": "international_talks",
        "parse_mode": "sections",
        "speaker": "Bhante Bodhidhamma",
        "language": "fr",
    },
    {
        "url": "/audio-video/talks-with-an-accompanying-czech-translation/",
        "feed_id": "international_talks",
        "season_name": "Talks in Czech",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
        "language": "cs",
    },
    {
        "url": "/audio-video/talks-with-an-accompanying-italian-translation/",
        "feed_id": "international_talks",
        "season_name": "Talks in Italian",
        "parse_mode": "flat",
        "speaker": "Bhante Bodhidhamma",
        "language": "it",
    },
]


# ============================================================
# Scraper utilities
# ============================================================

def is_audio_url(href: str) -> bool:
    """Check if a URL points to an audio/video file."""
    if not href:
        return False
    # Strip query string and fragment
    path = href.split('?')[0].split('#')[0].lower()
    return any(path.endswith(ext) for ext in AUDIO_EXTENSIONS)


def normalize_url(href: str, base_url: str) -> str:
    """Resolve relative URL to absolute, preserve spaces encoded."""
    # urljoin handles relative paths
    full = urljoin(base_url, href)
    return full


def title_from_url(url: str) -> str:
    """Extract a human-readable title from the filename in a URL."""
    path = unquote(url.split('?')[0])
    filename = path.rsplit('/', 1)[-1]
    # Remove extension
    name = filename.rsplit('.', 1)[0]
    # Remove leading track numbers like "01 ", "01-", "01_"
    name = re.sub(r'^\d+[.\s_-]+', '', name)
    # Replace hyphens and underscores with spaces
    name = name.replace('-', ' ').replace('_', ' ')
    # Clean up multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def clean_title(text: str) -> str:
    """Clean up raw title text extracted from HTML."""
    if not text:
        return ""
    # Remove jPlayer button text (PlayPauseStopMuteUnmute)
    text = re.sub(r'(Play|Pause|Stop|Mute|Unmute|Update Required)+', '', text)
    # Remove file size info like "(15.9 MB)" or "21.9 MB"
    text = re.sub(r'\s*[\(\[]?[\d.]+ [KMG]B[\)\]]?\s*', '', text)
    # Remove "right-click to download" type instructions
    text = re.sub(r'(?i)\s*(right.click|download|stream).*$', '', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def clean_season_name(name: str) -> str:
    """Normalize season name extracted from headings."""
    if not name:
        return name
    # Strip trailing colons, periods, whitespace
    name = name.strip().rstrip(':.')
    # Remove "(posted ...)" date notes
    name = re.sub(r'\s*\(posted[^)]*\)', '', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    return name


DEFAULT_HEADING_TAGS = ['h2', 'h3', 'h4']


def find_nearest_heading(element: Tag, heading_tags=None) -> str:
    """Find the nearest preceding heading in document order."""
    tags = heading_tags or DEFAULT_HEADING_TAGS
    heading = element.find_previous(tags)
    if heading:
        # Use separator=' ' to avoid concatenated words from nested elements
        return heading.get_text(separator=' ', strip=True)
    return ""


def find_link_title(a_tag: Tag) -> str:
    """Extract the best title for an audio link from its context."""
    # 1. Direct text content of the link
    text = a_tag.get_text(strip=True)
    if text and len(text) > 3 and not text.lower().startswith(('download', 'stream', 'click')):
        return clean_title(text)

    # 2. Title or aria-label attribute
    for attr in ('title', 'aria-label'):
        val = a_tag.get(attr, '').strip()
        if val:
            return clean_title(val)

    # 3. Nearest preceding <strong> or <b> sibling/text
    prev = a_tag.find_previous(['strong', 'b', 'em'])
    if prev and prev.parent == a_tag.parent:
        return clean_title(prev.get_text(strip=True))

    # 4. Parent <li> or <p> text (minus the link itself)
    parent = a_tag.parent
    if parent and parent.name in ('li', 'p', 'td'):
        parent_text = parent.get_text(strip=True)
        link_text = a_tag.get_text(strip=True)
        remaining = parent_text.replace(link_text, '').strip(' -–—:•')
        if remaining and len(remaining) > 3:
            return clean_title(remaining)

    # 5. Fall back to URL-derived title
    return ""


def find_audio_element_title(audio_tag: Tag) -> str:
    """Extract title for an <audio> or <source> element."""
    # Check parent containers for text
    parent = audio_tag.parent
    while parent and parent.name not in ('body', 'html', '[document]'):
        if parent.name in ('div', 'figure', 'li', 'p'):
            # Look for text siblings
            for child in parent.children:
                if isinstance(child, Tag) and child.name in ('p', 'figcaption', 'strong', 'span'):
                    text = child.get_text(strip=True)
                    if text and len(text) > 3:
                        return clean_title(text)
        parent = parent.parent
    return ""


def fetch_page(url: str) -> BeautifulSoup:
    """Fetch a page and return parsed BeautifulSoup."""
    print(f"  Fetching {url} ...")
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, 'html.parser')


# ============================================================
# Core scraper
# ============================================================

def extract_audio_entries(soup: BeautifulSoup, base_url: str,
                         heading_tags=None) -> List[Dict]:
    """
    Extract all audio entries from a page in DOM order.
    Returns list of dicts: {url, title, section, file_format}
    """
    entries = []
    seen_urls = set()
    htags = heading_tags or DEFAULT_HEADING_TAGS

    # Strategy 1: <a> tags linking to audio files
    for a in soup.find_all('a', href=True):
        href = a['href']
        if not is_audio_url(href):
            continue

        url = normalize_url(href, base_url)

        # Skip external hosts (Dropbox, YouTube, etc.)
        if 'satipanya.org.uk' not in url and not url.startswith(BASE_URL):
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = find_link_title(a) or title_from_url(url)
        section = find_nearest_heading(a, htags)
        fmt = url.split('?')[0].rsplit('.', 1)[-1].lower()

        entries.append({
            'url': url,
            'title': title,
            'section': section,
            'file_format': fmt,
        })

    # Strategy 2: <audio> elements with src or <source> children
    for audio in soup.find_all('audio'):
        # Direct src attribute
        src = audio.get('src', '')
        # Or <source> child
        if not src:
            source = audio.find('source', src=True)
            if source:
                src = source['src']

        if not src or not is_audio_url(src):
            continue

        url = normalize_url(src, base_url)
        if 'satipanya.org.uk' not in url and not url.startswith(BASE_URL):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = find_audio_element_title(audio) or title_from_url(url)
        section = find_nearest_heading(audio, htags)
        fmt = url.split('?')[0].rsplit('.', 1)[-1].lower()

        entries.append({
            'url': url,
            'title': title,
            'section': section,
            'file_format': fmt,
        })

    return entries


def group_into_seasons(entries: List[Dict], parse_mode: str,
                       season_name: str = "") -> List[Dict]:
    """
    Group audio entries into seasons.
    - "flat": all entries → one season with the given name
    - "sections": group by section heading, each heading = one season
    """
    if not entries:
        return []

    if parse_mode == "flat":
        return [{
            "name": season_name,
            "episodes": entries,
        }]

    # sections mode: group consecutive entries by their section heading
    seasons = []
    current_section = None
    current_episodes = []

    for entry in entries:
        section = entry.get('section') or season_name or "Miscellaneous"
        if section != current_section:
            if current_episodes:
                seasons.append({
                    "name": current_section,
                    "episodes": list(current_episodes),
                })
            current_section = section
            current_episodes = []
        current_episodes.append(entry)

    if current_episodes:
        seasons.append({
            "name": current_section,
            "episodes": current_episodes,
        })

    return seasons


# ============================================================
# YouTube channel scraping (yt-dlp)
# ============================================================

def fetch_youtube_videos():
    """Récupère les métadonnées de toutes les vidéos du channel YouTube via yt-dlp."""
    print("\n  Fetching YouTube channel metadata via yt-dlp...")
    try:
        result = subprocess.run(
            ['yt-dlp', '--flat-playlist', '--dump-json',
             f'https://www.youtube.com/channel/{YOUTUBE_CHANNEL_ID}/videos'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            print(f"  ERROR yt-dlp: {result.stderr[:200]}")
            return []
    except FileNotFoundError:
        print("  ERROR: yt-dlp not installed. pip install yt-dlp")
        return []
    except subprocess.TimeoutExpired:
        print("  ERROR: yt-dlp timeout")
        return []

    entries = []
    for line in result.stdout.strip().split('\n'):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = data.get('id', '')
        title = data.get('title', 'Untitled')
        duration = data.get('duration') or 0
        entries.append({
            'url': f'https://www.youtube.com/watch?v={video_id}',
            'title': title,
            'file_format': 'mp4',
            'section': 'YouTube Talks',
            'youtube_id': video_id,
            'youtube_duration': duration,
        })

    # yt-dlp renvoie les vidéos de la plus récente à la plus ancienne ;
    # on inverse pour avoir l'ordre chronologique
    entries.reverse()
    print(f"  Found {len(entries)} YouTube videos")
    return entries


# ============================================================
# Pass 1: Catalog
# ============================================================

def pass_catalog():
    """
    Scrape all configured pages and build the master catalog.
    Output: catalog.json with all feeds, seasons, and episodes.
    """
    print("=" * 60)
    print("PASS 1: Building catalog from satipanya.org.uk")
    print("=" * 60)

    # Global dedup: first page to claim a URL wins
    global_seen_urls = set()

    # Accumulate seasons per feed (preserving page order)
    feed_seasons: Dict[str, List[Dict]] = {fid: [] for fid in FEEDS}

    for i, page_cfg in enumerate(PAGES):
        page_url = BASE_URL + page_cfg["url"]
        feed_id = page_cfg["feed_id"]
        parse_mode = page_cfg.get("parse_mode", "flat")
        season_name = page_cfg.get("season_name", "")
        speaker = page_cfg.get("speaker", "Bhante Bodhidhamma")
        language = page_cfg.get("language", "en")

        print(f"\n[{i+1}/{len(PAGES)}] {page_cfg['url']} → {feed_id}")

        try:
            soup = fetch_page(page_url)
        except Exception as e:
            print(f"  ERROR fetching page: {e}")
            continue

        # Extract all audio entries
        heading_tags = page_cfg.get("heading_tags", None)
        raw_entries = extract_audio_entries(soup, page_url, heading_tags)
        print(f"  Found {len(raw_entries)} audio links")

        # Global dedup
        entries = []
        for entry in raw_entries:
            if entry['url'] not in global_seen_urls:
                global_seen_urls.add(entry['url'])
                entry['speaker'] = speaker
                entry['language'] = language
                entry['page_url'] = page_url
                entries.append(entry)
            else:
                print(f"  DEDUP: skipping {entry['title'][:50]}...")

        if not entries:
            print("  No new entries after dedup")
            if i < len(PAGES) - 1:
                time.sleep(REQUEST_DELAY)
            continue

        # Group into seasons
        seasons = group_into_seasons(entries, parse_mode, season_name)
        # Clean season names and filter out empty seasons
        for s in seasons:
            s['name'] = clean_season_name(s['name'])
        seasons = [s for s in seasons if s['episodes']]
        for s in seasons:
            ep_count = len(s['episodes'])
            print(f"  Season: \"{s['name']}\" ({ep_count} episodes)")

        feed_seasons[feed_id].extend(seasons)

        # Polite delay
        if i < len(PAGES) - 1:
            time.sleep(REQUEST_DELAY)

    # ── YouTube channel (7th feed) ──
    yt_entries = fetch_youtube_videos()
    if yt_entries:
        for entry in yt_entries:
            entry['speaker'] = 'Bhante Bodhidhamma'
            entry['language'] = 'en'
            entry['page_url'] = YOUTUBE_CHANNEL_URL
        yt_seasons = group_into_seasons(yt_entries, "flat", "YouTube Talks")
        feed_seasons["youtube_channel"].extend(yt_seasons)

    # ── Post-processing: merge duplicate seasons, absorb "Miscellaneous" ──
    for feed_id, seasons in feed_seasons.items():
        # Merge consecutive seasons with the same name
        merged = []
        for s in seasons:
            if merged and merged[-1]['name'] == s['name']:
                merged[-1]['episodes'].extend(s['episodes'])
            else:
                merged.append(s)

        # Absorb "Miscellaneous" into the next season (if it exists)
        final = []
        for i, s in enumerate(merged):
            if s['name'] == 'Miscellaneous' and i + 1 < len(merged):
                merged[i + 1]['episodes'] = s['episodes'] + merged[i + 1]['episodes']
                print(f"  [{feed_id}] Absorbed {len(s['episodes'])} "
                      f"'Miscellaneous' episodes into '{merged[i+1]['name']}'")
            else:
                final.append(s)

        feed_seasons[feed_id] = final

    # ── Charger le catalogue existant pour fusion incrémentale ──
    # Indexer les épisodes existants par URL pour préserver les données
    # acquises lors des passes précédentes (probe, transcribe, describe)
    MERGE_FIELDS = [
        'duration_seconds', 'file_size_bytes', 'transcript_path',
        'description_short', 'description_long',
    ]
    existing_by_url = {}
    if CATALOG_PATH.exists():
        with open(CATALOG_PATH, 'r') as f:
            old_catalog = json.load(f)
        for feed_data in old_catalog.values():
            for season in feed_data.get('seasons', []):
                for ep in season.get('episodes', []):
                    existing_by_url[ep['url']] = ep

    # ── Build final catalog structure ────────────────────────
    catalog = {}
    total_episodes = 0
    merged_count = 0
    new_count = 0

    for feed_id, feed_def in FEEDS.items():
        seasons = feed_seasons[feed_id]

        # Auto-number seasons
        feed_episodes = 0
        numbered_seasons = []
        for s_idx, season in enumerate(seasons, start=1):
            # Number episodes within each season
            episodes = []
            for e_idx, entry in enumerate(season['episodes'], start=1):
                ep = {
                    "url": entry['url'],
                    "title": entry['title'],
                    "speaker": entry.get('speaker', feed_def['author']),
                    "language": entry.get('language', feed_def['language']),
                    "file_format": entry.get('file_format', 'mp3'),
                    "page_url": entry.get('page_url', ''),
                    "section": entry.get('section', ''),
                    "season_number": s_idx,
                    "episode_number": e_idx,
                    "duration_seconds": 0.0,
                    "file_size_bytes": 0,
                    "transcript_path": "",
                    "description_short": "",
                    "description_long": "",
                }

                # Fusionner avec les données existantes (probe, transcripts, descriptions)
                old_ep = existing_by_url.get(entry['url'])
                if old_ep:
                    for field in MERGE_FIELDS:
                        if old_ep.get(field):
                            ep[field] = old_ep[field]
                    merged_count += 1
                else:
                    new_count += 1

                episodes.append(ep)
                feed_episodes += 1

            numbered_seasons.append({
                "name": season['name'],
                "number": s_idx,
                "episode_count": len(episodes),
                "episodes": episodes,
            })

        catalog[feed_id] = {
            **feed_def,
            "season_count": len(numbered_seasons),
            "episode_count": feed_episodes,
            "seasons": numbered_seasons,
        }
        total_episodes += feed_episodes

    if existing_by_url:
        print(f"\n  Merge: {merged_count} existing episodes preserved, "
              f"{new_count} new episodes detected")

    # ── Preserve collections not managed by this script ─────
    if existing_by_url:
        for key in old_catalog:
            if key not in catalog:
                catalog[key] = old_catalog[key]

    # ── Save catalog ─────────────────────────────────────────
    with open(CATALOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    # ── Summary ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("CATALOG SUMMARY")
    print("=" * 60)
    for feed_id, feed_data in catalog.items():
        print(f"\n  {feed_data['name']}")
        print(f"    {feed_data['season_count']} seasons, "
              f"{feed_data['episode_count']} episodes")
        for season in feed_data['seasons']:
            print(f"      S{season['number']:02d}: {season['name']} "
                  f"({season['episode_count']} ep)")
    print(f"\n  TOTAL: {total_episodes} episodes across {len(FEEDS)} feeds")
    print(f"  Saved to: {CATALOG_PATH}")

    return catalog


# ============================================================
# Pass 2: Probe (remote ffprobe for duration & file size)
# ============================================================

def pass_probe():
    """Probe remote audio files for duration and file size."""
    print("=" * 60)
    print("PASS 2: Probing remote files for duration & size")
    print("=" * 60)

    if not CATALOG_PATH.exists():
        print("ERROR: catalog.json not found. Run 'catalog' first.")
        return

    with open(CATALOG_PATH, 'r') as f:
        catalog = json.load(f)

    total = sum(fd['episode_count'] for fd in catalog.values())
    probed = 0
    skipped = 0

    def probe_one(url, max_retries=2):
        """Probe a single URL for file size and duration, with retries."""
        size = 0
        duration = 0.0
        for attempt in range(max_retries):
            if attempt > 0:
                wait = 3 * attempt
                print(f"    retry {attempt}/{max_retries-1}, wait {wait}s...")
                time.sleep(wait)
            # HEAD for file size
            if size == 0:
                try:
                    head = requests.head(url, headers=REQUEST_HEADERS,
                                         timeout=15, allow_redirects=True)
                    size = int(head.headers.get('Content-Length', 0))
                except Exception as e:
                    print(f"    HEAD failed: {e}")
            # ffprobe for duration
            if duration == 0:
                try:
                    result = subprocess.run(
                        ['ffprobe', '-v', 'error',
                         '-show_entries', 'format=duration',
                         '-of', 'csv=p=0', url],
                        capture_output=True, text=True, timeout=60
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        duration = float(result.stdout.strip())
                    elif result.stderr.strip():
                        print(f"    ffprobe stderr: {result.stderr.strip()[:120]}")
                except subprocess.TimeoutExpired:
                    print(f"    ffprobe timeout (60s)")
                except Exception as e:
                    print(f"    ffprobe failed: {e}")
            if size > 0 and duration > 0:
                break
        return size, duration

    def probe_youtube(url):
        """Probe une URL YouTube via yt-dlp pour obtenir la durée."""
        try:
            result = subprocess.run(
                ['yt-dlp', '--dump-json', '--no-download', url],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                return 0, float(data.get('duration', 0))
        except Exception as e:
            print(f"    yt-dlp probe failed: {e}")
        return 0, 0.0

    save_every = 50
    for feed_id, feed_data in catalog.items():
        for season in feed_data['seasons']:
            for ep in season['episodes']:
                if ep['duration_seconds'] > 0:
                    skipped += 1
                    continue

                probed += 1
                url = ep['url']
                remaining = total - skipped - probed + 1
                print(f"  [{probed}/{total-skipped}] {ep['title'][:50]}...",
                      flush=True)

                if 'youtube.com' in url or 'youtu.be' in url:
                    size, duration = probe_youtube(url)
                else:
                    size, duration = probe_one(url)
                ep['file_size_bytes'] = size
                ep['duration_seconds'] = duration
                if duration > 0:
                    print(f"    {duration:.0f}s, {size/(1024*1024):.1f} MB",
                          flush=True)
                else:
                    print(f"    FAILED (size={size})", flush=True)

                time.sleep(1.0)  # polite — CDN rate limiting

                # Incremental save
                if probed % save_every == 0:
                    with open(CATALOG_PATH, 'w', encoding='utf-8') as f:
                        json.dump(catalog, f, indent=2, ensure_ascii=False)
                    print(f"  [checkpoint saved at {probed}]", flush=True)

    # Final save
    with open(CATALOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    probed_ok = sum(
        1 for fd in catalog.values()
        for s in fd['seasons']
        for ep in s['episodes']
        if ep['duration_seconds'] > 0
    )
    print(f"\n  Probed: {probed_ok}/{total} episodes have duration")
    print(f"  Saved to: {CATALOG_PATH}")


# ============================================================
# Pass 3: Transcribe (WhisperX)
# ============================================================

def pass_transcribe():
    """Download audio temporarily and transcribe with WhisperX."""
    print("=" * 60)
    print("PASS 3: Transcribing with WhisperX")
    print("=" * 60)

    if not CATALOG_PATH.exists():
        print("ERROR: catalog.json not found. Run 'catalog' first.")
        return

    try:
        import whisperx
        import torch
    except ImportError:
        print("ERROR: whisperx not installed. pip install whisperx")
        return

    TRANSCRIPTS_DIR.mkdir(exist_ok=True)

    with open(CATALOG_PATH, 'r') as f:
        catalog = json.load(f)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    print(f"  Device: {device}, compute: {compute_type}")

    # Load model once
    print("  Loading WhisperX model (large-v3)...")
    model = whisperx.load_model("large-v3", device,
                                compute_type=compute_type)

    total = sum(fd['episode_count'] for fd in catalog.values())
    done = 0

    for feed_id, feed_data in catalog.items():
        feed_slug = feed_data['slug']
        feed_transcript_dir = TRANSCRIPTS_DIR / feed_slug
        feed_transcript_dir.mkdir(exist_ok=True)

        for season in feed_data['seasons']:
            for ep in season['episodes']:
                done += 1
                # Check if already transcribed
                ep_slug = re.sub(r'[^\w\s-]', '', ep['title'])[:80].strip()
                ep_slug = re.sub(r'[\s]+', '_', ep_slug)
                srt_path = feed_transcript_dir / f"S{ep['season_number']:02d}E{ep['episode_number']:02d}_{ep_slug}.srt"
                txt_path = srt_path.with_suffix('.txt')

                if srt_path.exists() and txt_path.exists():
                    ep['transcript_path'] = str(srt_path.relative_to(PROJECT_DIR))
                    print(f"  [{done}/{total}] SKIP (exists): {ep['title'][:50]}")
                    continue

                # Skip episodes with no duration (broken/protected on server)
                if ep.get('duration_seconds', 0) == 0:
                    print(f"  [{done}/{total}] SKIP (no duration): {ep['title'][:50]}")
                    continue

                print(f"  [{done}/{total}] {ep['title'][:50]}...")

                # Download audio
                is_youtube = 'youtube.com' in ep['url'] or 'youtu.be' in ep['url']
                tmp_path = PROJECT_DIR / f"_tmp_audio.{'wav' if is_youtube else ep['file_format']}"
                downloaded = False

                if is_youtube:
                    # Télécharger l'audio YouTube via yt-dlp
                    yt_tmp = PROJECT_DIR / "_tmp_yt_audio"
                    result = subprocess.run(
                        ['yt-dlp', '-f', 'bestaudio/best',
                         '-x', '--audio-format', 'wav',
                         '--postprocessor-args', 'ffmpeg:-ar 16000 -ac 1',
                         '-o', str(yt_tmp) + '.%(ext)s',
                         '--no-playlist', ep['url']],
                        capture_output=True, text=True, timeout=600
                    )
                    # yt-dlp nomme le fichier avec l'extension réelle
                    yt_wav = yt_tmp.with_suffix('.wav')
                    if result.returncode == 0 and yt_wav.exists() and yt_wav.stat().st_size > 1000:
                        yt_wav.rename(tmp_path)
                        downloaded = True
                    else:
                        err = result.stderr.strip()[:200] if result.stderr else f"exit {result.returncode}"
                        print(f"    yt-dlp download failed: {err}", flush=True)
                        # Nettoyer les fichiers temporaires yt-dlp
                        for p in PROJECT_DIR.glob("_tmp_yt_audio*"):
                            p.unlink(missing_ok=True)
                else:
                    # Download direct via curl (CDN files)
                    tmp_path = PROJECT_DIR / f"_tmp_audio.{ep['file_format']}"
                    dl_url = ep['url']
                    if ' ' in dl_url:
                        from urllib.parse import urlparse, quote as urlquote
                        p = urlparse(dl_url)
                        dl_url = p._replace(path=urlquote(p.path)).geturl()
                    for attempt in range(4):
                        result = subprocess.run(
                            ['curl', '-sS', '-L', '-f', '--http1.1',
                             '--retry', '2', '--retry-delay', '10',
                             '--max-time', '600',
                             '-H', f'User-Agent: {REQUEST_HEADERS["User-Agent"]}',
                             '-o', str(tmp_path), dl_url],
                            capture_output=True, text=True, timeout=660
                        )
                        if result.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 1000:
                            downloaded = True
                            break
                        else:
                            err = result.stderr.strip()[:120] if result.stderr else f"exit {result.returncode}"
                            file_size = tmp_path.stat().st_size if tmp_path.exists() else 0
                            if result.returncode == 0 and file_size < 1000:
                                print(f"    Broken file on server ({file_size}B), skipping",
                                      flush=True)
                                break
                            elif result.returncode == 22:
                                print(f"    HTTP error, skipping: {err}", flush=True)
                                break
                            elif '429' in err or 'rate' in err.lower():
                                wait = 60 * (2 ** attempt)
                                print(f"    Rate limited, waiting {wait}s...", flush=True)
                                time.sleep(wait)
                            elif attempt < 3:
                                wait = 15 * (attempt + 1)
                                print(f"    Download retry {attempt+1}: {err}", flush=True)
                                time.sleep(wait)
                            else:
                                print(f"    Download failed: {err}", flush=True)

                if not downloaded:
                    tmp_path.unlink(missing_ok=True)
                    continue
                time.sleep(5)  # polite delay between downloads

                # Convert to WAV if needed (WhisperX prefers WAV/MP3)
                wav_path = PROJECT_DIR / "_tmp_audio.wav"
                if is_youtube:
                    # YouTube: déjà en WAV 16kHz mono
                    audio_input = str(tmp_path)
                elif ep['file_format'] == 'mp4':
                    subprocess.run(
                        ['ffmpeg', '-y', '-i', str(tmp_path),
                         '-vn', '-acodec', 'pcm_s16le', '-ar', '16000',
                         '-ac', '1', str(wav_path)],
                        capture_output=True
                    )
                    audio_input = str(wav_path)
                else:
                    audio_input = str(tmp_path)

                try:
                    # Use catalog language to avoid misdetection (Pali chanting → "sa")
                    ep_lang = ep.get('language', 'en') or 'en'
                    # Map language codes to WhisperX-supported alignment languages
                    ALIGN_LANGS = {'en', 'fr', 'de', 'es', 'it', 'pt', 'nl', 'ja',
                                   'zh', 'ko', 'cs', 'pl', 'ru', 'uk', 'ar', 'hi'}

                    # Transcribe with explicit language
                    result = model.transcribe(audio_input, batch_size=16,
                                              language=ep_lang)
                    detected = result.get("language", ep_lang)
                    align_lang = detected if detected in ALIGN_LANGS else ep_lang
                    if align_lang not in ALIGN_LANGS:
                        align_lang = 'en'  # ultimate fallback

                    # Align
                    align_model, align_meta = whisperx.load_align_model(
                        language_code=align_lang, device=device
                    )
                    aligned = whisperx.align(
                        result["segments"], align_model, align_meta,
                        audio_input, device, return_char_alignments=False
                    )
                    segments = aligned["segments"]

                    # Get duration from last segment
                    if segments and ep['duration_seconds'] == 0:
                        ep['duration_seconds'] = segments[-1].get('end', 0)

                    # Write SRT
                    with open(srt_path, 'w', encoding='utf-8') as f:
                        for i, seg in enumerate(segments, 1):
                            start = seg['start']
                            end = seg['end']
                            text = seg['text'].strip()
                            f.write(f"{i}\n")
                            f.write(f"{_format_srt_time(start)} --> "
                                    f"{_format_srt_time(end)}\n")
                            f.write(f"{text}\n\n")

                    # Write plain text
                    with open(txt_path, 'w', encoding='utf-8') as f:
                        for seg in segments:
                            f.write(seg['text'].strip() + '\n')

                    ep['transcript_path'] = str(srt_path.relative_to(PROJECT_DIR))
                    print(f"    OK: {len(segments)} segments, {align_lang}")

                except Exception as e:
                    print(f"    Transcription failed: {e}")

                finally:
                    # Cleanup temp files
                    for p in (tmp_path, wav_path):
                        if p.exists():
                            p.unlink()

    # Save updated catalog
    with open(CATALOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    transcribed = sum(
        1 for fd in catalog.values()
        for s in fd['seasons']
        for ep in s['episodes']
        if ep.get('transcript_path')
    )
    print(f"\n  Transcribed: {transcribed}/{total} episodes")
    print(f"  Saved to: {TRANSCRIPTS_DIR}/")


def _format_srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ============================================================
# Pass 4: Describe (Claude AI)
# ============================================================

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS = 4096
DESCRIPTION_DISCLAIMER = "\n\n(This description was generated automatically, inaccuracies may happen in the process.)"
DESCRIPTION_MAX_CHARS = 3800  # iTunes <description> recommended max ~4000, leave room for disclaimer

ANALYSIS_SYSTEM_PROMPT = """You are an expert in Theravāda Buddhism, specifically the Mahasi Sayadaw
tradition of Vipassana insight meditation as taught at Satipanya Buddhist Retreat in Wales.

You are writing podcast episode descriptions for talks by Bhante Bodhidhamma (or Noirin Sheahan
where indicated). Your descriptions must:

1. Use correct Pali diacritics throughout (ā ī ū ṭ ḍ ṇ ṅ ñ): satipaṭṭhāna, dukkha, saṅkhāra,
   paṭicca samuppāda, khandha, pāramī, brahmavihāra, jhāna, vipassanā, samādhi, sīla, etc.

2. Reference suttas correctly with full Pali name and Nikāya number:
   e.g., "Kāyagatāsati Sutta (MN 119)", "Satipaṭṭhāna Sutta (MN 10)",
   "Dhammacakkappavattana Sutta (SN 56.11)"

3. Respect Bhante Bodhidhamma's specific terminology preferences:
   - "Right Awareness" (not "Right Mindfulness") for sammā sati
   - "Awakening" (not "Enlightenment") for bodhi
   - "Unsatisfactoriness" alongside "suffering" for dukkha
   - "Not-self" (not "no-self") for anattā
   - Uses "the Awakened" or "the Buddha" respectfully

4. Stay within Theravāda Buddhist framing — do not project Mahāyāna concepts.

5. Be accessible to newcomers while precise enough for experienced practitioners.

For each episode you will produce:
- title_clean: The cleaned-up episode title with correct Pali diacritics
- description_short: 1-2 sentences for podcast app listing (max 200 chars)
- description_long: 2-4 paragraphs covering: what the talk addresses, which suttas/teachings
  are discussed, key concepts explained, practical relevance for meditation and daily life.
  MUST NOT exceed 3800 characters (iTunes limit). Stay concise and informative.
- keywords: list of topic tags (Buddhist concepts, Pali terms, themes)
- difficulty: "introductory" | "intermediate" | "advanced"

Respond in JSON format only."""


def pass_describe():
    """Generate AI descriptions for each episode using Claude."""
    print("=" * 60)
    print("PASS 4: Generating descriptions with Claude")
    print("=" * 60)

    if not CATALOG_PATH.exists():
        print("ERROR: catalog.json not found. Run 'catalog' first.")
        return

    try:
        import anthropic
    except ImportError:
        print("ERROR: anthropic not installed. pip install anthropic")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return

    client = anthropic.Anthropic(api_key=api_key)
    METADATA_DIR.mkdir(exist_ok=True)

    with open(CATALOG_PATH, 'r') as f:
        catalog = json.load(f)

    total = sum(fd['episode_count'] for fd in catalog.values())
    done = 0
    api_calls = 0

    for feed_id, feed_data in catalog.items():
        feed_slug = feed_data['slug']
        feed_meta_dir = METADATA_DIR / feed_slug
        feed_meta_dir.mkdir(exist_ok=True)

        for season in feed_data['seasons']:
            for ep in season['episodes']:
                done += 1
                ep_slug = re.sub(r'[^\w\s-]', '', ep['title'])[:80].strip()
                ep_slug = re.sub(r'[\s]+', '_', ep_slug)
                meta_path = feed_meta_dir / f"S{ep['season_number']:02d}E{ep['episode_number']:02d}_{ep_slug}.json"

                if meta_path.exists():
                    # Load existing metadata into catalog
                    with open(meta_path, 'r') as f:
                        meta = json.load(f)
                    desc_long = meta.get('description_long', '')
                    if DESCRIPTION_DISCLAIMER.strip() not in desc_long:
                        desc_long += DESCRIPTION_DISCLAIMER
                    ep['description_short'] = meta.get('description_short', '')
                    ep['description_long'] = desc_long
                    print(f"  [{done}/{total}] SKIP (exists): {ep['title'][:50]}")
                    continue

                # Read transcript if available
                transcript = ""
                if ep.get('transcript_path'):
                    txt_path = PROJECT_DIR / ep['transcript_path'].replace('.srt', '.txt')
                    if txt_path.exists():
                        transcript = txt_path.read_text(encoding='utf-8')

                if not transcript:
                    print(f"  [{done}/{total}] SKIP (no transcript): {ep['title'][:50]}")
                    continue

                print(f"  [{done}/{total}] {ep['title'][:50]}...")

                # Truncate very long transcripts (Claude context limit)
                if len(transcript) > 80000:
                    transcript = transcript[:40000] + "\n\n[...]\n\n" + transcript[-40000:]

                user_prompt = f"""Episode information:
- Title: {ep['title']}
- Speaker: {ep['speaker']}
- Feed: {feed_data['name']}
- Season: {season['name']}
- Language: {ep['language']}

Transcript:
{transcript}

Generate the episode metadata as JSON with keys: title_clean, description_short,
description_long, keywords (list), difficulty."""

                try:
                    response = client.messages.create(
                        model=CLAUDE_MODEL,
                        max_tokens=CLAUDE_MAX_TOKENS,
                        system=ANALYSIS_SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": user_prompt}],
                    )
                    content = response.content[0].text

                    # Parse JSON from response (handle markdown code blocks)
                    json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
                    if json_match:
                        meta = json.loads(json_match.group(1))
                    else:
                        meta = json.loads(content)

                    # Save metadata file
                    with open(meta_path, 'w', encoding='utf-8') as f:
                        json.dump(meta, f, indent=2, ensure_ascii=False)

                    # Enforce description length and add disclaimer
                    desc_long = meta.get('description_long', '')
                    if len(desc_long) > DESCRIPTION_MAX_CHARS:
                        desc_long = desc_long[:DESCRIPTION_MAX_CHARS].rsplit('\n', 1)[0]
                    desc_long += DESCRIPTION_DISCLAIMER
                    meta['description_long'] = desc_long

                    desc_short = meta.get('description_short', '')
                    if len(desc_short) > 200:
                        desc_short = desc_short[:197] + '...'
                    meta['description_short'] = desc_short

                    # Re-save with enforced limits
                    with open(meta_path, 'w', encoding='utf-8') as f:
                        json.dump(meta, f, indent=2, ensure_ascii=False)

                    # Update catalog
                    ep['description_short'] = desc_short
                    ep['description_long'] = desc_long
                    ep['title'] = meta.get('title_clean', ep['title'])

                    api_calls += 1
                    print(f"    OK: {meta.get('difficulty', '?')}, "
                          f"{len(meta.get('keywords', []))} keywords")

                except Exception as e:
                    print(f"    Claude API failed: {e}")
                    # Exponential backoff on rate limits
                    if '429' in str(e) or '529' in str(e):
                        wait = min(60, 10 * (2 ** (api_calls % 5)))
                        print(f"    Rate limited, waiting {wait}s...")
                        time.sleep(wait)

                time.sleep(1.0)  # rate limiting

    # Save updated catalog
    with open(CATALOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    described = sum(
        1 for fd in catalog.values()
        for s in fd['seasons']
        for ep in s['episodes']
        if ep.get('description_short')
    )
    print(f"\n  Described: {described}/{total} episodes ({api_calls} API calls)")
    print(f"  Saved to: {METADATA_DIR}/")


# ============================================================
# Pass 5: Cover Art
# ============================================================

ASSETS_DIR = PROJECT_DIR / "assets"

# Per-feed visual config: background photo icon + subtitle text
COVER_CONFIG = {
    "guided_meditations": {
        "icon": "icon-d-full.png",  # meditation Buddha
        "subtitle": "Guided Vipassana Meditation\n& Pali Chanting",
    },
    "dhammabytes": {
        "icon": "icon-a-full.png",  # serene Buddha face
        "subtitle": "Short Teachings on\nCore Buddhist Concepts",
    },
    "foundation_course": {
        "icon": "icon-e-full.png",  # stupa
        "subtitle": "A Structured Course in\nTheravāda Buddhism",
    },
    "dharma_talks": {
        "icon": "icon-c-full.png",  # garden Bodhisattva
        "subtitle": "Dhamma Talks on Practice,\nPhilosophy & Daily Life",
    },
    "noirins_teachings": {
        "icon": "icon-b-full.png",  # Bodhisattva on lotus
        "subtitle": "Teachings by\nNoirin Sheahan",
    },
    "international_talks": {
        "icon": "icon-a-full.png",  # Buddha face
        "subtitle": "Talks with French,\nCzech & Italian Translation",
    },
    "youtube_channel": {
        "icon": "icon-c-full.png",  # garden Bodhisattva
        "subtitle": "Dharma Talks from the\nSatipanya YouTube Channel",
    },
}


def pass_covers():
    """Generate podcast cover art using Satipanya branding assets."""
    print("=" * 60)
    print("PASS 5: Generating cover art from Satipanya assets")
    print("=" * 60)

    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
    except ImportError:
        print("ERROR: Pillow not installed. pip install Pillow")
        return

    COVERS_DIR.mkdir(exist_ok=True)

    if not CATALOG_PATH.exists():
        print("ERROR: catalog.json not found. Run 'catalog' first.")
        return

    with open(CATALOG_PATH, 'r') as f:
        catalog = json.load(f)

    # Cover dimensions (Apple Podcasts: min 1400x1400, max 3000x3000)
    SIZE = 3000

    # Satipanya brand colours (derived from the navy logo seal)
    BG_COLOR = (18, 15, 52)       # deep indigo-navy
    TEXT_COLOR = (230, 220, 195)   # warm cream
    ACCENT_COLOR = (140, 130, 180) # visible soft indigo for lines & brand

    # Load shared assets
    logo_seal = Image.open(ASSETS_DIR / "logo-left.png").convert("RGBA")
    logo_wheel = Image.open(ASSETS_DIR / "logo-right.png").convert("RGBA")

    # Fonts (Noto Serif for elegance, DejaVu Sans for readability)
    try:
        font_title = ImageFont.truetype(
            "/usr/share/fonts/truetype/noto/NotoSerif-Bold.ttf", 170)
        font_subtitle = ImageFont.truetype(
            "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf", 96)
        font_brand = ImageFont.truetype(
            "/usr/share/fonts/truetype/noto/NotoSerif-Medium.ttf", 72)
        font_small = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 56)
    except OSError:
        print("  WARNING: fonts not found, using defaults")
        font_title = ImageFont.load_default()
        font_subtitle = font_title
        font_brand = font_title
        font_small = font_title

    for feed_id, feed_data in catalog.items():
        cover_path = COVERS_DIR / f"{feed_data['slug']}.png"
        cfg = COVER_CONFIG.get(feed_id, COVER_CONFIG["dharma_talks"])

        if cover_path.exists():
            print(f"  SKIP (exists): {feed_data['slug']}")
            continue

        print(f"  Generating: {feed_data['slug']}...")

        img = Image.new('RGB', (SIZE, SIZE), BG_COLOR)
        draw = ImageDraw.Draw(img)

        # ── Background photo icon (large, faded, as texture) ─────
        icon_path = ASSETS_DIR / cfg["icon"]
        if icon_path.exists():
            icon = Image.open(icon_path).convert("RGBA")
            # Scale to fill a large portion of the cover
            icon_size = int(SIZE * 0.75)
            icon = icon.resize((icon_size, icon_size), Image.LANCZOS)
            # Desaturate but keep moderately bright as background wash
            icon_rgb = icon.convert("RGB")
            icon_rgb = ImageEnhance.Brightness(icon_rgb).enhance(0.45)
            icon_rgb = ImageEnhance.Color(icon_rgb).enhance(0.25)
            # Subtle blur for background feel
            icon_rgb = icon_rgb.filter(ImageFilter.GaussianBlur(radius=6))
            # Paste centered, shifted up
            offset_x = (SIZE - icon_size) // 2
            offset_y = (SIZE - icon_size) // 2 - 150
            # Use alpha mask for soft edges
            alpha = icon.split()[3]
            alpha = alpha.resize((icon_size, icon_size), Image.LANCZOS)
            img.paste(icon_rgb, (offset_x, offset_y), alpha)

        # ── Gradient overlay (darken bottom half for text) ────────
        gradient = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
        grad_draw = ImageDraw.Draw(gradient)
        for y in range(SIZE // 3, SIZE):
            progress = (y - SIZE // 3) / (SIZE * 2 // 3)
            a = int(220 * progress)
            grad_draw.line([(0, y), (SIZE, y)],
                           fill=(BG_COLOR[0], BG_COLOR[1], BG_COLOR[2], a))
        img = Image.alpha_composite(img.convert('RGBA'), gradient).convert('RGB')

        # Re-create draw after composite
        draw = ImageDraw.Draw(img)

        # ── Thin decorative border ───────────────────────────────
        b = 50
        draw.rectangle([b, b, SIZE - b, SIZE - b], outline=ACCENT_COLOR, width=4)
        draw.rectangle([b + 20, b + 20, SIZE - b - 20, SIZE - b - 20],
                       outline=ACCENT_COLOR, width=2)

        # ── Satipanya seal logo (upper area, lightened for visibility) ─
        seal_size = 650
        seal = logo_seal.resize((seal_size, seal_size), Image.LANCZOS)
        # Tint the seal to cream/gold so it shows on dark background
        r, g, b, a = seal.split()
        # The seal is dark blue on transparent — invert the RGB to make it light
        from PIL import ImageOps
        seal_light = ImageOps.invert(seal.convert('RGB')).convert('RGBA')
        seal_light.putalpha(a)
        # Tint towards warm cream
        tint_layer = Image.new('RGBA', seal.size, (TEXT_COLOR[0], TEXT_COLOR[1], TEXT_COLOR[2], 0))
        tint_layer.putalpha(a)
        seal_final = Image.blend(seal_light, tint_layer, 0.3)
        # Slightly reduce opacity so it doesn't overpower
        final_a = seal_final.split()[3].point(lambda x: int(x * 0.85))
        seal_final.putalpha(final_a)
        seal_x = (SIZE - seal_size) // 2
        seal_y = 200
        img.paste(seal_final, (seal_x, seal_y), seal_final)

        # ── Feed title (below logo) ──────────────────────────────
        title = feed_data['name'].replace("Satipanya — ", "")
        # Word-wrap the title
        title_lines = _wrap_text(draw, title, font_title, SIZE - 300)
        y = seal_y + seal_size + 120
        for line in title_lines:
            bbox = draw.textbbox((0, 0), line, font=font_title)
            w = bbox[2] - bbox[0]
            draw.text(((SIZE - w) / 2, y), line, fill=TEXT_COLOR, font=font_title)
            y += bbox[3] - bbox[1] + 30

        # ── Subtitle / description ───────────────────────────────
        y += 60
        for sub_line in cfg["subtitle"].split('\n'):
            bbox = draw.textbbox((0, 0), sub_line, font=font_subtitle)
            w = bbox[2] - bbox[0]
            draw.text(((SIZE - w) / 2, y), sub_line,
                      fill=(TEXT_COLOR[0], TEXT_COLOR[1], TEXT_COLOR[2]),
                      font=font_subtitle)
            y += bbox[3] - bbox[1] + 20

        # ── Decorative separator line ────────────────────────────
        y += 50
        line_w = 400
        draw.line([(SIZE // 2 - line_w, y), (SIZE // 2 + line_w, y)],
                  fill=ACCENT_COLOR, width=3)

        # ── Bottom brand text ────────────────────────────────────
        brand = "Satipanya Buddhist Retreat"
        bbox = draw.textbbox((0, 0), brand, font=font_brand)
        w = bbox[2] - bbox[0]
        draw.text(((SIZE - w) / 2, SIZE - 250), brand,
                  fill=ACCENT_COLOR, font=font_brand)

        # ── Small dharma wheel accent next to brand ──────────────
        wheel_size = 80
        wheel = logo_wheel.resize((wheel_size, wheel_size), Image.LANCZOS)
        wheel_x = (SIZE - w) // 2 - wheel_size - 30
        wheel_y = SIZE - 250 + (bbox[3] - bbox[1] - wheel_size) // 2
        img.paste(wheel, (wheel_x, wheel_y), wheel)
        wheel_x2 = (SIZE + w) // 2 + 30
        img.paste(wheel, (wheel_x2, wheel_y), wheel)

        img.save(cover_path, "PNG")
        print(f"    Saved: {cover_path}")

    print(f"\n  Covers saved to: {COVERS_DIR}/")


def _wrap_text(draw, text, font, max_width):
    """Word-wrap text to fit within max_width pixels."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    return lines


# ============================================================
# Pass 6: RSS Feed Generation
# ============================================================

# Feeds dont les saisons doivent être triées chronologiquement (plus récent d'abord)
_FEEDS_CHRONOLOGICAL = {"dharma-talks", "noirins-teachings"}

# Mois anglais → numéro (pour parser les dates dans les titres d'épisodes)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _season_sort_key(season):
    """Clé de tri : date la plus récente d'une saison (année du nom > titres d'épisodes)."""
    name = season.get("name", "")
    if "most recent" in name.lower():
        return (9999, 12, 31)
    name_years = [int(y) for y in re.findall(r'20[0-2]\d', name)]
    if name_years:
        return (max(name_years), 0, 0)
    best = (0, 0, 0)
    for ep in season.get("episodes", []):
        text = ep.get("title", "")
        for m in re.finditer(r'(20[0-2]\d)\s+(\w+)\.?\s+(\d{1,2})', text):
            y, mon = int(m.group(1)), _MONTHS.get(m.group(2).lower().rstrip('.'), 0)
            if mon:
                best = max(best, (y, mon, int(m.group(3))))
        for m in re.finditer(r'(20[0-2]\d)\s+(\w+)', text):
            y, mon = int(m.group(1)), _MONTHS.get(m.group(2).lower().rstrip('.'), 0)
            if mon:
                best = max(best, (y, mon, 0))
    return best


def pass_feeds():
    """Generate RSS 2.0 podcast feeds with iTunes extensions."""
    print("=" * 60)
    print("PASS 6: Generating RSS podcast feeds")
    print("=" * 60)

    if not CATALOG_PATH.exists():
        print("ERROR: catalog.json not found. Run 'catalog' first.")
        return

    import xml.etree.ElementTree as ET
    from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

    FEEDS_DIR.mkdir(exist_ok=True)

    # Enregistrer les préfixes de namespace pour un XML lisible
    ET.register_namespace('itunes', "http://www.itunes.com/dtds/podcast-1.0.dtd")
    ET.register_namespace('podcast', "https://podcastindex.org/namespace/1.0")
    ET.register_namespace('content', "http://purl.org/rss/1.0/modules/content/")

    with open(CATALOG_PATH, 'r') as f:
        catalog = json.load(f)

    # Namespaces
    ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
    PODCAST_NS = "https://podcastindex.org/namespace/1.0"
    CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

    for feed_id, feed_data in catalog.items():
        feed_path = FEEDS_DIR / f"{feed_data['slug']}.xml"
        print(f"  Generating: {feed_data['slug']}.xml ...")

        rss = Element('rss', {
            'version': '2.0',
        })

        channel = SubElement(rss, 'channel')

        # Channel metadata
        SubElement(channel, 'title').text = feed_data['name']
        SubElement(channel, 'description').text = feed_data['description']
        SubElement(channel, 'language').text = feed_data['language']
        SubElement(channel, 'link').text = f"{BASE_URL}/audio-video/"
        SubElement(channel, 'generator').text = "podcastify.py — Satipanya Podcast Pipeline"

        # iTunes metadata
        SubElement(channel, f'{{{ITUNES_NS}}}author').text = feed_data['author']
        SubElement(channel, f'{{{ITUNES_NS}}}summary').text = feed_data['description']
        SubElement(channel, f'{{{ITUNES_NS}}}explicit').text = 'false'
        SubElement(channel, f'{{{ITUNES_NS}}}type').text = 'serial'

        owner = SubElement(channel, f'{{{ITUNES_NS}}}owner')
        SubElement(owner, f'{{{ITUNES_NS}}}name').text = "Satipanya Buddhist Trust"
        SubElement(owner, f'{{{ITUNES_NS}}}email').text = "info@satipanya.org.uk"

        cat = SubElement(channel, f'{{{ITUNES_NS}}}category',
                         text=feed_data.get('category', 'Religion & Spirituality'))
        SubElement(cat, f'{{{ITUNES_NS}}}category',
                   text=feed_data.get('subcategory', 'Buddhism'))

        # Cover image (try .png then .jpg)
        for ext in ('png', 'jpg'):
            cover_path = COVERS_DIR / f"{feed_data['slug']}.{ext}"
            if cover_path.exists():
                SubElement(channel, f'{{{ITUNES_NS}}}image',
                           href=f"covers/{feed_data['slug']}.{ext}")
                break

        # Episodes (newest first for podcast apps, but we use serial type)
        # For serial type, episodes should be in order (oldest first)
        # We assign synthetic pubDates to maintain our intended order
        from datetime import datetime, timedelta
        base_date = datetime(2005, 1, 1)  # start date for synthetic pubDates
        ep_global_idx = 0

        skipped_dead = 0
        seasons = feed_data['seasons']
        if feed_data['slug'] in _FEEDS_CHRONOLOGICAL:
            seasons = sorted(seasons, key=_season_sort_key)  # oldest first for serial feeds
        for season in seasons:
            for ep in season['episodes']:
                # Exclure les liens morts (0 durée = fichier absent ou cassé sur le serveur)
                if ep.get('duration_seconds', 0) == 0:
                    skipped_dead += 1
                    continue
                ep_global_idx += 1
                item = SubElement(channel, 'item')

                SubElement(item, 'title').text = ep['title']

                # Description
                desc = ep.get('description_long') or ep.get('description_short') or ep['title']
                SubElement(item, 'description').text = desc
                SubElement(item, f'{{{CONTENT_NS}}}encoded').text = f"<p>{desc}</p>"

                # Enclosure (the audio file)
                mime = 'audio/mpeg' if ep['file_format'] == 'mp3' else f"audio/{ep['file_format']}"
                if ep['file_format'] == 'mp4':
                    mime = 'video/mp4'
                SubElement(item, 'enclosure', {
                    'url': ep['url'],
                    'type': mime,
                    'length': str(ep.get('file_size_bytes', 0)),
                })

                # GUID (use URL as unique ID)
                SubElement(item, 'guid', isPermaLink='true').text = ep['url']

                # Synthetic pubDate to maintain order
                pub_date = base_date + timedelta(days=ep_global_idx)
                SubElement(item, 'pubDate').text = pub_date.strftime(
                    '%a, %d %b %Y 12:00:00 +0000'
                )

                # iTunes episode metadata
                SubElement(item, f'{{{ITUNES_NS}}}author').text = ep.get('speaker', feed_data['author'])
                SubElement(item, f'{{{ITUNES_NS}}}summary').text = ep.get('description_short', ep['title'])
                SubElement(item, f'{{{ITUNES_NS}}}season').text = str(ep['season_number'])
                SubElement(item, f'{{{ITUNES_NS}}}episode').text = str(ep['episode_number'])
                SubElement(item, f'{{{ITUNES_NS}}}episodeType').text = 'full'
                SubElement(item, f'{{{ITUNES_NS}}}explicit').text = 'false'

                if ep.get('duration_seconds', 0) > 0:
                    dur = int(ep['duration_seconds'])
                    h, m, s = dur // 3600, (dur % 3600) // 60, dur % 60
                    SubElement(item, f'{{{ITUNES_NS}}}duration').text = f"{h:02d}:{m:02d}:{s:02d}"

                # Transcript link (Podcasting 2.0)
                if ep.get('transcript_path'):
                    SubElement(item, f'{{{PODCAST_NS}}}transcript', {
                        'url': ep['transcript_path'],
                        'type': 'application/srt',
                    })

        # Write feed
        tree = ElementTree(rss)
        indent(tree, space='  ')
        tree.write(feed_path, encoding='unicode', xml_declaration=True)

        included = ep_global_idx
        msg = f"    {included} episodes, {feed_data['season_count']} seasons → {feed_path.name}"
        if skipped_dead:
            msg += f" ({skipped_dead} dead links excluded)"
        print(msg)

    print(f"\n  Feeds saved to: {FEEDS_DIR}/")


# ============================================================
# Pass 7: Beautify (Claude AI — spoken → written text)
# ============================================================

BEAUTIFY_DIR = PROJECT_DIR / "articles"

BEAUTIFY_CHUNK_MAX_WORDS = 3000
BEAUTIFY_CHUNK_CONTEXT_LINES = 6  # lignes de chevauchement contextuel

BEAUTIFY_SYSTEM_PROMPT = """You are an expert editor specialising in Theravāda Buddhism. \
Your task is to transform a raw speech-to-text transcript of a dharma talk into a \
polished, readable written text.

SPEAKER CONTEXT:
- The speaker is {speaker}, teaching in the Mahāsi Sayadaw tradition of Vipassanā \
insight meditation at Satipanya Buddhist Retreat in Wales, UK.
- The talk is titled: "{title}"

RULES:
1. Transform spoken English into clear, elegant written English suitable for publication.
2. Organise the text into logical paragraphs. A new paragraph should begin when the \
topic shifts, a new argument begins, or after a natural pause in thought.
3. Remove speech artefacts: hesitations, false starts, filler words ("you see", "sort of", \
"you know", "I mean", "kind of", "right"), unnecessary repetitions, and self-corrections \
(keep only the corrected version).
4. FAITHFULLY PRESERVE the meaning, ideas, teaching content, and the speaker's \
personal voice and warmth. Do not add, interpret, or editorialize.
5. NEVER summarise or cut substantive content. Every idea expressed must remain.
6. Keep vivid metaphors, anecdotes, humour, and characteristic expressions that give \
the talk its personality.
7. Preserve exactly: Pali terms (use proper diacritics where possible — e.g. dukkha, \
anicca, anattā, satipaṭṭhāna, vipassanā, jhāna, Nibbāna, kamma), sutta references, \
names, dates, and numbers.
8. For Pali chanting at the beginning of talks (namo tassa…), keep it as-is in italics \
with a translation if one was given.
9. Do not add headings, titles, or section markers. Just flowing paragraphs.
10. Do not add any editorial notes, footnotes, or comments.
11. If the text contains sections marked [PRECEDING CONTEXT]…[END CONTEXT], \
do NOT include them in your output. They provide continuity context only.

OUTPUT FORMAT:
- Plain text with paragraph breaks (double newlines between paragraphs).
- Use *italics* for Pali terms on first use only.
- No markdown headings, bullet points, or other formatting."""


def _chunk_for_beautify(text: str) -> list:
    """Découpe le texte en chunks respectant les frontières de phrases."""
    lines = text.strip().split('\n')
    total_words = len(text.split())

    if total_words <= BEAUTIFY_CHUNK_MAX_WORDS:
        return [text]

    chunks = []
    current_lines = []
    current_words = 0

    for line in lines:
        line_words = len(line.split())
        if current_words + line_words > BEAUTIFY_CHUNK_MAX_WORDS and current_lines:
            chunks.append('\n'.join(current_lines))
            # Chevauchement contextuel
            context = current_lines[-BEAUTIFY_CHUNK_CONTEXT_LINES:]
            current_lines = []
            current_words = 0
            if context:
                ctx_text = '\n'.join(context)
                current_lines.append(
                    f"[PRECEDING CONTEXT]\n{ctx_text}\n[END CONTEXT]"
                )
                current_words = sum(len(l.split()) for l in context)

        current_lines.append(line)
        current_words += line_words

    if current_lines:
        chunks.append('\n'.join(current_lines))

    return chunks


def pass_beautify():
    """Transform raw transcripts into polished readable articles using Claude."""
    print("=" * 60)
    print("PASS 7: Beautifying transcripts with Claude")
    print("=" * 60)

    if not CATALOG_PATH.exists():
        print("ERROR: catalog.json not found. Run 'catalog' first.")
        return

    try:
        import anthropic
    except ImportError:
        print("ERROR: anthropic not installed. pip install anthropic")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return

    client = anthropic.Anthropic(api_key=api_key)
    BEAUTIFY_DIR.mkdir(exist_ok=True)

    with open(CATALOG_PATH, 'r') as f:
        catalog = json.load(f)

    total = sum(fd['episode_count'] for fd in catalog.values())
    done = 0
    api_calls = 0
    total_input_tokens = 0
    total_output_tokens = 0

    for feed_id, feed_data in catalog.items():
        feed_slug = feed_data['slug']
        feed_article_dir = BEAUTIFY_DIR / feed_slug
        feed_article_dir.mkdir(exist_ok=True)

        for season in feed_data['seasons']:
            for ep in season['episodes']:
                done += 1
                # Utiliser le stem du transcript (cohérent avec build_site.py)
                if ep.get('transcript_path'):
                    stem = Path(ep['transcript_path']).stem
                else:
                    ep_slug = re.sub(r'[^\w\s-]', '', ep['title'])[:80].strip()
                    ep_slug = re.sub(r'[\s]+', '_', ep_slug)
                    stem = f"S{ep['season_number']:02d}E{ep['episode_number']:02d}_{ep_slug}"
                article_path = feed_article_dir / f"{stem}.txt"

                if article_path.exists():
                    print(f"  [{done}/{total}] SKIP (exists): {ep['title'][:50]}")
                    continue

                # Lire la transcription brute
                if not ep.get('transcript_path'):
                    print(f"  [{done}/{total}] SKIP (no transcript): {ep['title'][:50]}")
                    continue

                txt_path = PROJECT_DIR / ep['transcript_path'].replace('.srt', '.txt')
                if not txt_path.exists():
                    print(f"  [{done}/{total}] SKIP (no txt): {ep['title'][:50]}")
                    continue

                transcript = txt_path.read_text(encoding='utf-8').strip()
                if len(transcript) < 100:
                    print(f"  [{done}/{total}] SKIP (too short): {ep['title'][:50]}")
                    continue

                print(f"  [{done}/{total}] {ep['title'][:50]}...")

                speaker = ep.get('speaker', feed_data['author'])
                system_prompt = BEAUTIFY_SYSTEM_PROMPT.format(
                    speaker=speaker, title=ep['title']
                )

                chunks = _chunk_for_beautify(transcript)
                beautified_parts = []

                for i, chunk in enumerate(chunks):
                    if len(chunks) > 1:
                        print(f"    chunk {i+1}/{len(chunks)}...", end=' ', flush=True)

                    user_msg = chunk
                    if len(chunks) > 1 and i > 0:
                        user_msg = (
                            f"Continue beautifying this talk (part {i+1}/{len(chunks)}). "
                            f"Maintain the same tone and style as previous parts.\n\n"
                            f"---\n{chunk}\n---"
                        )
                    else:
                        user_msg = (
                            f"Please transform this transcript into polished written text:\n\n"
                            f"---\n{chunk}\n---"
                        )

                    for attempt in range(3):
                        try:
                            response = client.messages.create(
                                model=CLAUDE_MODEL,
                                max_tokens=8192,
                                system=system_prompt,
                                messages=[{"role": "user", "content": user_msg}],
                            )
                            result_text = response.content[0].text
                            total_input_tokens += response.usage.input_tokens
                            total_output_tokens += response.usage.output_tokens
                            api_calls += 1
                            beautified_parts.append(result_text)
                            if len(chunks) > 1:
                                print(f"OK", flush=True)
                            break
                        except Exception as e:
                            if attempt < 2:
                                wait = 5 * (attempt + 1)
                                print(f"    retry {attempt+1}: {e}")
                                time.sleep(wait)
                            else:
                                print(f"    FAILED: {e}")

                    if len(beautified_parts) <= i:
                        break  # chunk failed, skip this episode
                    time.sleep(0.5)

                if len(beautified_parts) == len(chunks):
                    article = '\n\n'.join(beautified_parts)
                    article_path.write_text(article, encoding='utf-8')
                    word_count = len(article.split())
                    print(f"    OK: {word_count} words, {len(chunks)} chunk(s)")
                else:
                    print(f"    INCOMPLETE: {len(beautified_parts)}/{len(chunks)} chunks")

                # Sauvegarde incrémentale du compteur toutes les 50 épisodes
                if api_calls % 50 == 0 and api_calls > 0:
                    cost = (total_input_tokens / 1e6) * 3.0 + (total_output_tokens / 1e6) * 15.0
                    print(f"    [progress: {api_calls} API calls, ~${cost:.2f} so far]")

    cost = (total_input_tokens / 1e6) * 3.0 + (total_output_tokens / 1e6) * 15.0
    print(f"\n  Beautified: {api_calls} episodes ({api_calls} API calls)")
    print(f"  Tokens: {total_input_tokens:,} in + {total_output_tokens:,} out")
    print(f"  Estimated cost: ${cost:.2f}")
    print(f"  Saved to: {BEAUTIFY_DIR}/")


# ============================================================
# CLI
# ============================================================

PASS_MAP = {
    "catalog": pass_catalog,
    "probe": pass_probe,
    "transcribe": pass_transcribe,
    "describe": pass_describe,
    "covers": pass_covers,
    "feeds": pass_feeds,
    "beautify": pass_beautify,
}

ALL_PASSES = ["catalog", "probe", "transcribe", "describe", "covers", "feeds", "beautify"]


def main():
    if len(sys.argv) < 2:
        print("Usage: python podcastify.py <pass>")
        print(f"  Passes: {', '.join(ALL_PASSES)}, all")
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "all":
        for pass_name in ALL_PASSES:
            print()
            PASS_MAP[pass_name]()
    elif command in PASS_MAP:
        PASS_MAP[command]()
    else:
        print(f"Unknown pass: {command}")
        print(f"  Available: {', '.join(ALL_PASSES)}, all")
        sys.exit(1)


if __name__ == "__main__":
    main()
