#!/usr/bin/env python3
"""
scrape_essays.py — Scrape written material from satipanya.org.uk into the Dharma Library.

Extracts essays, tips, and written teachings from 4 source pages and integrates
them as 3 new text collections alongside the existing 7 audio collections.

Pass 1: scrape    — Fetch pages, download PDFs/DOCX, extract text → articles/
Pass 2: describe  — Claude AI → descriptions & keywords for new essays
Pass 3: score     — Literary quality scoring via Claude
Pass 4: merge     — Merge 3 new collections into catalog.json

Usage:
    python scrape_essays.py scrape
    python scrape_essays.py describe
    python scrape_essays.py score
    python scrape_essays.py merge
    python scrape_essays.py all
"""

import json, os, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup

# ============================================================
# Constants
# ============================================================

BASE_URL = "https://www.satipanya.org.uk"
PROJECT_DIR = Path(__file__).parent
CATALOG_PATH = PROJECT_DIR / "catalog.json"
TEXT_CATALOG_PATH = PROJECT_DIR / "text_catalog.json"
ARTICLES_DIR = PROJECT_DIR / "articles"
METADATA_DIR = PROJECT_DIR / "metadata"

REQUEST_HEADERS = {
    "User-Agent": "SatipanyaPodcastBot/1.0 (archiving for podcast feed generation)"
}
REQUEST_DELAY = 1.0

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS = 4096
DESCRIPTION_DISCLAIMER = "\n\n(This description was generated automatically, inaccuracies may happen in the process.)"
DESCRIPTION_MAX_CHARS = 3800

WORDS_PER_MINUTE = 250  # Average reading speed

AUDIO_EXTENSIONS = ('.mp3', '.mp4', '.m4a', '.wav', '.ogg')

# ============================================================
# Source page definitions
# ============================================================

SOURCES = {
    "bhante_essays": {
        "name": "Satipanya — Bhante's Essays",
        "slug": "bhante-essays",
        "author": "Bhante Bodhidhamma",
        "description": (
            "Written essays and teachings by Bhante Bodhidhamma on Buddhist "
            "philosophy, meditation practice, ethics, and daily life. Includes "
            "introductory guides, in-depth explorations of the Four Noble Truths, "
            "and practical meditation instructions."
        ),
        "content_type": "text",
        "language": "en",
        "pages": [
            {"url": "/essay/", "method": "pdf_links"},
            {"url": "/essay/daily-life-care/", "method": "html_body"},
        ],
    },
    "noirin_essays": {
        "name": "Satipanya — Noirin's Essays",
        "slug": "noirin-essays",
        "author": "Noirin Sheahan",
        "description": (
            "Written essays and reflections by Noirin Sheahan, co-founder of "
            "Satipanya Buddhist Retreat. Short essays on practice, post-surgery "
            "reflections, and a series on climate change ethics from a Buddhist "
            "perspective."
        ),
        "content_type": "text",
        "language": "en",
        "pages": [
            {"url": "/noirins-teachings/", "method": "doc_links"},
        ],
    },
    "tips_of_the_day": {
        "name": "Satipanya — Tips of the Day",
        "slug": "tips-of-the-day",
        "author": "Bhante Bodhidhamma",
        "description": (
            "Short practical tips for integrating mindfulness and Buddhist practice "
            "into daily life. Covers morning routines, work, relationships, and the "
            "pursuit of true happiness."
        ),
        "content_type": "text",
        "language": "en",
        "pages": [
            {"url": "/tip-o-the-day/", "method": "html_anchors"},
        ],
    },
}


# ============================================================
# Text extraction utilities
# ============================================================

def extract_pdf_text(pdf_bytes):
    """Extract text from PDF bytes using pdfplumber."""
    import pdfplumber
    import io
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n\n".join(text_parts)


def extract_docx_text(docx_bytes):
    """Extract text from DOCX bytes using python-docx."""
    from docx import Document
    import io
    doc = Document(io.BytesIO(docx_bytes))
    paragraphs = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def extract_doc_text(doc_bytes):
    """Extract text from DOC bytes using antiword (fallback: skip)."""
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as f:
        f.write(doc_bytes)
        f.flush()
        try:
            result = subprocess.run(
                ["antiword", f.name], capture_output=True, text=True, timeout=30
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
        finally:
            os.unlink(f.name)


def download_file(url, label=""):
    """Download a file, handling Dropbox dl=0 → dl=1."""
    if "dropbox.com" in url and "dl=0" in url:
        url = url.replace("dl=0", "dl=1")
    print(f"    ↓ {label or url[-60:]}", end="", flush=True)
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=60)
        resp.raise_for_status()
        print(f" ({len(resp.content) // 1024} KB)")
        return resp.content
    except Exception as e:
        print(f" FAILED: {e}")
        return None


def slugify(title, max_len=80):
    """Convert a title to a filesystem-safe slug."""
    s = re.sub(r'[^\w\s-]', '', title)[:max_len].strip()
    s = re.sub(r'[\s]+', '_', s)
    return s


def compute_reading_stats(text):
    """Compute word count and reading time."""
    words = len(text.split())
    minutes = max(1, round(words / WORDS_PER_MINUTE))
    return words, minutes


# ============================================================
# Pass 1: Scrape
# ============================================================

def scrape_essay_page(page_url):
    """Fetch and parse a page from the website."""
    full_url = urljoin(BASE_URL, page_url)
    print(f"  Fetching {full_url}...")
    resp = requests.get(full_url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser"), full_url


def find_content_div(soup):
    """Find the main content container in a Satipanya page."""
    return (soup.find("div", class_="entry-content")
            or soup.find("div", class_="wpautop")
            or soup.find("div", class_="span8")
            or soup.find("article")
            or soup.find("main")
            or soup)


def scrape_bhante_essays_pdf_links(soup, page_url):
    """Extract essays from /essay/ page — PDFs organized under h3 headings."""
    episodes = []
    content = find_content_div(soup)

    # Track which heading section we're in
    current_section = "Introductory Resources"
    season_map = {
        "Introductory Resources": 1,
        "Essays by Bhante Bodhidhamma": 2,
        "Introductory essays": 3,
        "Satipanya": 5,
    }

    # Look for all links in the content
    seen_urls = set()
    for element in content.find_all(["h3", "h2", "a", "p"]):
        if element.name in ("h2", "h3"):
            heading_text = element.get_text(strip=True)
            if "essays by bhante" in heading_text.lower():
                current_section = "Essays by Bhante Bodhidhamma"
            elif "introductory essays" in heading_text.lower():
                current_section = "Introductory essays"
            elif "satipanya" in heading_text.lower() and "retreat" in heading_text.lower():
                current_section = "Satipanya"
            elif "newsbytes" in heading_text.lower() or "upcoming" in heading_text.lower():
                current_section = "SKIP"
            continue

        if current_section == "SKIP":
            continue

        # Find links to documents
        links = element.find_all("a") if element.name == "p" else [element] if element.name == "a" else []
        for link in links:
            href = link.get("href", "")
            if not href:
                continue
            title = link.get_text(strip=True)
            if not title or len(title) < 3:
                continue

            abs_url = urljoin(page_url, href)

            # Skip audio files
            if any(abs_url.lower().endswith(ext) for ext in AUDIO_EXTENSIONS):
                continue
            # Skip non-document links that aren't PDFs/DOCX
            is_pdf = abs_url.lower().endswith('.pdf')
            is_docx = abs_url.lower().endswith('.docx')
            is_doc = abs_url.lower().endswith('.doc') and not is_docx
            # Skip links to subpages that we handle separately
            if "/essay/daily-life-care" in abs_url:
                continue
            # Skip external links (Adobe, etc.)
            if "adobe.com" in abs_url or "amazon" in abs_url or "amzn" in abs_url:
                continue
            # Skip if it's a subpage link (not a document)
            if not (is_pdf or is_docx or is_doc):
                # Check if it's a page link we should scrape as HTML
                if "/essay/" in abs_url and abs_url not in seen_urls:
                    # It's a subpage — skip for now, those are handled differently
                    pass
                continue

            if abs_url in seen_urls:
                continue
            seen_urls.add(abs_url)

            season_num = season_map.get(current_section, 1)
            episodes.append({
                "url": abs_url,
                "title": title,
                "speaker": "Bhante Bodhidhamma",
                "language": "en",
                "file_format": "pdf" if is_pdf else ("docx" if is_docx else "doc"),
                "content_type": "text",
                "page_url": page_url,
                "section": current_section,
                "season_number": season_num,
            })

    return episodes


def scrape_daily_life_html(soup, page_url):
    """Extract the inline essay from /essay/daily-life-care/."""
    episodes = []
    content = find_content_div(soup)
    if content is soup:
        return episodes

    # Get all text from the content area
    paragraphs = []
    for elem in content.find_all(["p", "h2", "h3", "h4", "ol", "ul"]):
        text = elem.get_text(strip=True)
        if not text:
            continue
        if elem.name in ("h2", "h3", "h4"):
            paragraphs.append(f"\n{text}\n")
        elif elem.name in ("ol", "ul"):
            for li in elem.find_all("li"):
                li_text = li.get_text(strip=True)
                if li_text:
                    paragraphs.append(f"- {li_text}")
        else:
            paragraphs.append(text)

    full_text = "\n\n".join(paragraphs)
    if len(full_text) < 100:
        return episodes

    # Also look for the PDF link on this page
    pdf_url = ""
    for link in content.find_all("a"):
        href = link.get("href", "")
        if href.lower().endswith(".pdf"):
            pdf_url = urljoin(page_url, href)
            break

    episodes.append({
        "url": pdf_url or page_url,
        "title": "Meditation in Ordinary Daily Life",
        "speaker": "Bhante Bodhidhamma",
        "language": "en",
        "file_format": "html",
        "content_type": "text",
        "page_url": page_url,
        "section": "Meditation in Daily Life",
        "season_number": 4,
        "_inline_text": full_text,  # Already extracted, no download needed
    })

    return episodes


def scrape_noirin_doc_links(soup, page_url):
    """Extract text documents from /noirins-teachings/ — filter out audio."""
    episodes = []
    content = find_content_div(soup)

    season_map = {
        "Short Essays": 1,
        "Post-Surgery Essays": 2,
        "Climate Change Essays": 3,
    }

    seen_urls = set()

    # Walk through elements to detect sections based on <strong> headers
    current_section = "SKIP"  # Skip bio/intro at top

    for element in content.descendants:
        if not hasattr(element, 'name'):
            continue

        # Detect section boundaries from <strong> and heading tags
        if element.name in ("h2", "h3", "h4", "strong", "b"):
            heading = element.get_text(strip=True).lower()

            # Short Essays section
            if "short essays" in heading or "tips:" in heading:
                current_section = "Short Essays"
            # 70th Birthday — include as Short Essays
            elif "birthday reflection" in heading:
                current_section = "Short Essays"
            # Post-surgery section
            elif "after noirin underwent surgery" in heading or "post-laryngectomy" in heading:
                current_section = "Post-Surgery Essays"
            # Climate change section
            elif "climate change" in heading or "ethical maxim" in heading:
                current_section = "Climate Change Essays"
            # Sections to skip
            elif "sharing the path" in heading:
                current_section = "SKIP"
            elif any(w in heading for w in ["meditation guidance", "talks a.", "talks b.",
                                              "retreat talks", "full moon", "newsbytes",
                                              "upcoming", "pre-laryngectomy",
                                              "post-laryngectomy - talks"]):
                current_section = "SKIP"

        if element.name != "a":
            continue
        if current_section == "SKIP":
            continue

        href = element.get("href", "")
        if not href:
            continue

        title = element.get_text(strip=True)
        if not title or len(title) < 3:
            continue

        abs_url = urljoin(page_url, href)

        # Skip audio/video
        if any(abs_url.lower().endswith(ext) for ext in AUDIO_EXTENSIONS):
            continue
        # Skip YouTube
        if "youtube.com" in abs_url or "youtu.be" in abs_url:
            continue
        # Skip Amazon
        if "amazon" in abs_url or "amzn" in abs_url:
            continue
        # Skip EPUB (that's the book "Sharing the Path")
        if abs_url.lower().endswith(".epub"):
            continue

        is_pdf = abs_url.lower().endswith('.pdf')
        is_docx = abs_url.lower().endswith('.docx')
        is_dropbox = "dropbox.com" in abs_url

        if not (is_pdf or is_docx or is_dropbox):
            continue

        if abs_url in seen_urls:
            continue
        seen_urls.add(abs_url)

        # Determine format for Dropbox links
        file_format = "pdf"
        if is_docx:
            file_format = "docx"
        elif is_dropbox:
            if ".docx" in abs_url.lower():
                file_format = "docx"

        episodes.append({
            "url": abs_url,
            "title": title,
            "speaker": "Noirin Sheahan",
            "language": "en",
            "file_format": file_format,
            "content_type": "text",
            "page_url": page_url,
            "section": current_section,
            "season_number": season_map.get(current_section, 1),
        })

    return episodes


def scrape_tips_anchors(soup, page_url):
    """Extract individual tips from /tip-o-the-day/ — split by anchors/headings."""
    episodes = []
    content = find_content_div(soup)

    # Category boundaries from the TOC
    categories = {
        "Daily Life Practice": (1, 16, 1),
        "Relationships": (17, 21, 2),
        "Work": (22, 27, 3),
        "Seeking True Happiness": (28, 34, 4),
        "Miscellaneous": (35, 999, 5),
    }

    # Find all h3 headings (tip titles) and collect text between them
    headings = content.find_all("h3")
    tip_number = 0

    # Filter out sidebar headings
    SKIP_HEADINGS = {"newsbytes", "upcoming retreats", "upcoming events"}

    for i, heading in enumerate(headings):
        title = heading.get_text(strip=True)
        if not title or len(title) < 2:
            continue
        if title.lower() in SKIP_HEADINGS:
            continue
        tip_number += 1

        # Collect text until next h3 or hr
        paragraphs = []
        sibling = heading.find_next_sibling()
        while sibling:
            if sibling.name == "h3":
                break
            if sibling.name == "hr":
                break
            text = sibling.get_text(strip=True)
            if text and "back to contents" not in text.lower():
                paragraphs.append(text)
            sibling = sibling.find_next_sibling()

        full_text = "\n\n".join(paragraphs)
        if len(full_text) < 20:
            continue

        # Determine season from tip number
        season_num = 5  # default: Miscellaneous
        section = "Miscellaneous"
        for cat_name, (start, end, snum) in categories.items():
            if start <= tip_number <= end:
                season_num = snum
                section = cat_name
                break

        # Extract author from title if present (e.g., "Title by Noirin Sheahan")
        speaker = "Bhante Bodhidhamma"
        author_match = re.search(r'\s+by\s+([\w\s]+)$', title, re.IGNORECASE)
        if author_match:
            author_name = author_match.group(1).strip()
            if author_name:
                speaker = author_name
                title = title[:author_match.start()].strip()

        episodes.append({
            "url": page_url + f"#anchor{tip_number - 1}",
            "title": title,
            "speaker": speaker,
            "language": "en",
            "file_format": "html",
            "content_type": "text",
            "page_url": page_url,
            "section": section,
            "season_number": season_num,
            "_inline_text": full_text,
        })

    return episodes


def pass_scrape():
    """Pass 1: Scrape all text sources and extract article text."""
    print("=" * 60)
    print("PASS 1: Scraping text sources")
    print("=" * 60)

    # Load existing text catalog if resuming
    text_catalog = {}
    if TEXT_CATALOG_PATH.exists():
        with open(TEXT_CATALOG_PATH, encoding="utf-8") as f:
            text_catalog = json.load(f)

    for feed_key, source in SOURCES.items():
        slug = source["slug"]
        print(f"\n── {source['name']} ──")

        articles_dir = ARTICLES_DIR / slug
        articles_dir.mkdir(parents=True, exist_ok=True)

        all_episodes = []

        for page_def in source["pages"]:
            page_url = page_def["url"]
            method = page_def["method"]

            try:
                soup, full_url = scrape_essay_page(page_url)
                time.sleep(REQUEST_DELAY)
            except Exception as e:
                print(f"  ERROR fetching {page_url}: {e}")
                continue

            if method == "pdf_links":
                episodes = scrape_bhante_essays_pdf_links(soup, full_url)
            elif method == "html_body":
                episodes = scrape_daily_life_html(soup, full_url)
            elif method == "doc_links":
                episodes = scrape_noirin_doc_links(soup, full_url)
            elif method == "html_anchors":
                episodes = scrape_tips_anchors(soup, full_url)
            else:
                print(f"  Unknown method: {method}")
                continue

            print(f"  Found {len(episodes)} items from {page_url}")
            all_episodes.extend(episodes)

        # Assign episode numbers per season and download/extract text
        season_eps = {}
        for ep in all_episodes:
            sn = ep["season_number"]
            season_eps.setdefault(sn, []).append(ep)

        for sn, eps in sorted(season_eps.items()):
            for i, ep in enumerate(eps, 1):
                ep["episode_number"] = i
                stem = f"S{sn:02d}E{ep['episode_number']:02d}_{slugify(ep['title'])}"
                ep["stem"] = stem

                article_path = articles_dir / f"{stem}.txt"

                # Skip if already extracted
                if article_path.exists() and article_path.stat().st_size > 50:
                    print(f"  SKIP (exists): {stem}")
                    text = article_path.read_text(encoding="utf-8")
                    wc, rm = compute_reading_stats(text)
                    ep["word_count"] = wc
                    ep["reading_minutes"] = rm
                    continue

                # Extract text
                text = ""
                if "_inline_text" in ep:
                    text = ep.pop("_inline_text")
                elif ep["file_format"] in ("pdf",):
                    data = download_file(ep["url"], ep["title"][:50])
                    if data:
                        try:
                            text = extract_pdf_text(data)
                        except Exception as e:
                            print(f"    PDF extraction failed: {e}")
                    time.sleep(REQUEST_DELAY)
                elif ep["file_format"] == "docx":
                    data = download_file(ep["url"], ep["title"][:50])
                    if data:
                        try:
                            text = extract_docx_text(data)
                        except Exception as e:
                            print(f"    DOCX extraction failed: {e}")
                    time.sleep(REQUEST_DELAY)
                elif ep["file_format"] == "doc":
                    data = download_file(ep["url"], ep["title"][:50])
                    if data:
                        text = extract_doc_text(data)
                    time.sleep(REQUEST_DELAY)

                if text and len(text.strip()) > 50:
                    article_path.write_text(text.strip(), encoding="utf-8")
                    print(f"  ✓ {stem} ({len(text)} chars)")
                    wc, rm = compute_reading_stats(text)
                    ep["word_count"] = wc
                    ep["reading_minutes"] = rm
                else:
                    print(f"  ✗ {stem} — no text extracted")
                    ep["word_count"] = 0
                    ep["reading_minutes"] = 0

                # Clean up internal field
                ep.pop("_inline_text", None)

        # Build collection structure
        seasons = {}
        for ep in all_episodes:
            ep.pop("_inline_text", None)
            sn = ep["season_number"]
            if sn not in seasons:
                seasons[sn] = {
                    "number": sn,
                    "name": ep["section"],
                    "episodes": [],
                }
            seasons[sn]["episodes"].append(ep)

        # Build season name from section
        season_list = [seasons[k] for k in sorted(seasons.keys())]

        text_catalog[feed_key] = {
            "name": source["name"],
            "slug": slug,
            "author": source["author"],
            "description": source["description"],
            "content_type": "text",
            "language": source["language"],
            "episode_count": len(all_episodes),
            "seasons": season_list,
        }

        total_with_text = sum(1 for ep in all_episodes if ep.get("word_count", 0) > 0)
        print(f"  Total: {len(all_episodes)} items, {total_with_text} with text")

    # Save text catalog
    with open(TEXT_CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(text_catalog, f, indent=2, ensure_ascii=False)
    print(f"\n✓ Text catalog saved: {TEXT_CATALOG_PATH}")


# ============================================================
# Pass 2: Describe
# ============================================================

ANALYSIS_SYSTEM_PROMPT = """You are an expert in Theravāda Buddhism, specifically the Mahasi Sayadaw
tradition of Vipassana insight meditation as taught at Satipanya Buddhist Retreat in Wales.

You are writing descriptions for written essays and teachings by Bhante Bodhidhamma
(or Noirin Sheahan where indicated). Your descriptions must:

1. Use correct Pali diacritics throughout (ā ī ū ṭ ḍ ṇ ṅ ñ): satipaṭṭhāna, dukkha, saṅkhāra,
   paṭicca samuppāda, khandha, pāramī, brahmavihāra, jhāna, vipassanā, samādhi, sīla, etc.

2. Reference suttas correctly with full Pali name and Nikāya number:
   e.g., "Kāyagatāsati Sutta (MN 119)", "Satipaṭṭhāna Sutta (MN 10)"

3. Respect Bhante Bodhidhamma's specific terminology preferences:
   - "Right Awareness" (not "Right Mindfulness") for sammā sati
   - "Awakening" (not "Enlightenment") for bodhi
   - "Unsatisfactoriness" alongside "suffering" for dukkha
   - "Not-self" (not "no-self") for anattā

4. Stay within Theravāda Buddhist framing — do not project Mahāyāna concepts.

5. Be accessible to newcomers while precise enough for experienced practitioners.

For each essay you will produce:
- title_clean: The cleaned-up title with correct Pali diacritics
- description_short: 1-2 sentences for listing (max 200 chars)
- description_long: 2-4 paragraphs covering: what the essay addresses, which suttas/teachings
  are discussed, key concepts explained, practical relevance for meditation and daily life.
  MUST NOT exceed 3800 characters. Stay concise and informative.
- keywords: list of topic tags (Buddhist concepts, Pali terms, themes)
- difficulty: "introductory" | "intermediate" | "advanced"

Respond in JSON format only."""


def pass_describe():
    """Pass 2: Generate Claude descriptions for new essays."""
    print("=" * 60)
    print("PASS 2: Generating descriptions with Claude")
    print("=" * 60)

    if not TEXT_CATALOG_PATH.exists():
        print("ERROR: text_catalog.json not found. Run 'scrape' first.")
        return

    try:
        import anthropic
    except ImportError:
        print("ERROR: pip install anthropic")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return

    client = anthropic.Anthropic(api_key=api_key)

    with open(TEXT_CATALOG_PATH, encoding="utf-8") as f:
        text_catalog = json.load(f)

    total = sum(c["episode_count"] for c in text_catalog.values())
    done = 0
    api_calls = 0

    for feed_key, fdata in text_catalog.items():
        slug = fdata["slug"]
        meta_dir = METADATA_DIR / slug
        meta_dir.mkdir(parents=True, exist_ok=True)

        for season in fdata["seasons"]:
            for ep in season["episodes"]:
                done += 1
                stem = ep.get("stem", "")
                if not stem:
                    continue

                meta_path = meta_dir / f"{stem}.json"

                if meta_path.exists():
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                    ep["description_short"] = meta.get("description_short", "")
                    desc_long = meta.get("description_long", "")
                    if DESCRIPTION_DISCLAIMER.strip() not in desc_long:
                        desc_long += DESCRIPTION_DISCLAIMER
                    ep["description_long"] = desc_long
                    print(f"  [{done}/{total}] SKIP (exists): {ep['title'][:50]}")
                    continue

                # Read article text
                article_path = ARTICLES_DIR / slug / f"{stem}.txt"
                if not article_path.exists():
                    print(f"  [{done}/{total}] SKIP (no text): {ep['title'][:50]}")
                    continue

                article_text = article_path.read_text(encoding="utf-8")
                if len(article_text) < 50:
                    print(f"  [{done}/{total}] SKIP (too short): {ep['title'][:50]}")
                    continue

                print(f"  [{done}/{total}] {ep['title'][:50]}...", end=" ", flush=True)

                # Truncate if needed
                if len(article_text) > 80000:
                    article_text = article_text[:40000] + "\n\n[...]\n\n" + article_text[-40000:]

                user_prompt = f"""Essay information:
- Title: {ep['title']}
- Author: {ep['speaker']}
- Collection: {fdata['name']}
- Section: {season['name']}
- Language: {ep['language']}

Full text:
{article_text}

Generate the essay metadata as JSON with keys: title_clean, description_short,
description_long, keywords (list), difficulty."""

                try:
                    response = client.messages.create(
                        model=CLAUDE_MODEL,
                        max_tokens=CLAUDE_MAX_TOKENS,
                        system=ANALYSIS_SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": user_prompt}],
                    )
                    content = response.content[0].text

                    json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
                    if json_match:
                        meta = json.loads(json_match.group(1))
                    else:
                        meta = json.loads(content)

                    # Enforce limits
                    desc_long = meta.get("description_long", "")
                    if len(desc_long) > DESCRIPTION_MAX_CHARS:
                        desc_long = desc_long[:DESCRIPTION_MAX_CHARS].rsplit('\n', 1)[0]
                    desc_long += DESCRIPTION_DISCLAIMER
                    meta["description_long"] = desc_long

                    desc_short = meta.get("description_short", "")
                    if len(desc_short) > 200:
                        desc_short = desc_short[:197] + "..."
                    meta["description_short"] = desc_short

                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, indent=2, ensure_ascii=False)

                    ep["description_short"] = desc_short
                    ep["description_long"] = desc_long
                    api_calls += 1
                    print(f"✓")

                except Exception as e:
                    print(f"✗ {e}")

                time.sleep(0.5)

    # Save updated catalog
    with open(TEXT_CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(text_catalog, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Descriptions generated: {api_calls} API calls")


# ============================================================
# Pass 3: Score
# ============================================================

SCORE_RUBRIC = """You are an expert literary critic evaluating the quality of a written Buddhist essay.

Rate the LITERARY QUALITY of this essay on a scale from 0 to 100, based on these 7 equally-weighted criteria:

1. **Clarity & structure of argumentation** — Is the essay well-organized? Does it build logically?
2. **Use of metaphors, analogies, illustrations** — Does the author use vivid imagery to convey meaning?
3. **Vocabulary richness & precision** — Is the language varied and precise, or repetitive and vague?
4. **Rhetorical devices & oratory skill** — Are there effective rhetorical techniques?
5. **Storytelling & narrative engagement** — Does the essay draw the reader in?
6. **Depth of insight expressed through language** — Does the language convey genuine wisdom?
7. **Overall eloquence & natural flow** — Does it read well? Is the expression graceful?

IMPORTANT NOTES:
- These are WRITTEN essays, so expect more polished language than spoken transcripts.
- Very short tips (under 200 words) should be evaluated on density and quality, not length.
- A brilliant, well-structured essay might score 80-95.
- An average essay with decent structure but unremarkable language might score 40-60.

Respond with ONLY a JSON object, no other text:
{"score": <integer 0-100>, "reason": "<one sentence justification>"}"""


def pass_score():
    """Pass 3: Score literary quality of text essays."""
    print("=" * 60)
    print("PASS 3: Scoring literary quality")
    print("=" * 60)

    if not TEXT_CATALOG_PATH.exists():
        print("ERROR: text_catalog.json not found. Run 'scrape' first.")
        return

    try:
        import anthropic
    except ImportError:
        print("ERROR: pip install anthropic")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return

    client = anthropic.Anthropic(api_key=api_key)

    with open(TEXT_CATALOG_PATH, encoding="utf-8") as f:
        text_catalog = json.load(f)

    to_score = []
    for feed_key, fdata in text_catalog.items():
        slug = fdata["slug"]
        for season in fdata["seasons"]:
            for ep in season["episodes"]:
                if "lite_score" in ep:
                    continue
                stem = ep.get("stem", "")
                if not stem:
                    continue
                article_path = ARTICLES_DIR / slug / f"{stem}.txt"
                if not article_path.exists():
                    continue
                to_score.append((feed_key, slug, ep, article_path))

    print(f"Essays to score: {len(to_score)}")
    if not to_score:
        print("Nothing to do.")
        return

    scored = 0
    errors = 0
    for i, (feed_key, slug, ep, article_path) in enumerate(to_score):
        label = f"[{i+1}/{len(to_score)}]"
        print(f"  {label} {ep['title'][:50]}...", end=" ", flush=True)

        try:
            text = article_path.read_text(encoding="utf-8")
            if len(text) < 50:
                print("⏭ too short")
                ep["lite_score"] = 5
                scored += 1
                continue

            if len(text) > 80000:
                text = text[:40000] + "\n\n[... truncated ...]\n\n" + text[-40000:]

            user_msg = f"# Essay title: {ep['title']}\n\n# Full text:\n\n{text}"
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=200,
                messages=[{"role": "user", "content": f"{SCORE_RUBRIC}\n\n{user_msg}"}],
            )

            raw = response.content[0].text.strip()
            match = re.search(r'\{[^}]+\}', raw)
            if match:
                result = json.loads(match.group())
                score = int(result.get("score", -1))
                reason = result.get("reason", "")
                if 0 <= score <= 100:
                    ep["lite_score"] = score
                    ep["lite_reason"] = reason
                    print(f"✓ {score}/100 — {reason[:60]}")
                    scored += 1
                else:
                    print("✗ invalid score")
                    errors += 1
            else:
                print(f"✗ unexpected response")
                errors += 1

        except Exception as e:
            print(f"✗ {e}")
            errors += 1

        # Save after each (resumable)
        with open(TEXT_CATALOG_PATH, "w", encoding="utf-8") as f:
            json.dump(text_catalog, f, indent=2, ensure_ascii=False)

        if i < len(to_score) - 1:
            time.sleep(0.3)

    print(f"\n✓ Scored {scored}, errors {errors}")


# ============================================================
# Pass 4: Merge
# ============================================================

def pass_merge():
    """Pass 4: Merge text collections into main catalog.json."""
    print("=" * 60)
    print("PASS 4: Merging into catalog.json")
    print("=" * 60)

    if not TEXT_CATALOG_PATH.exists():
        print("ERROR: text_catalog.json not found. Run 'scrape' first.")
        return

    with open(CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)

    with open(TEXT_CATALOG_PATH, encoding="utf-8") as f:
        text_catalog = json.load(f)

    for feed_key, fdata in text_catalog.items():
        ep_count = sum(
            len(s["episodes"]) for s in fdata.get("seasons", [])
        )
        if ep_count == 0:
            print(f"  SKIP {feed_key}: no episodes")
            continue

        # Merge: add or update the collection
        if feed_key in catalog:
            # Update existing — merge episodes by URL
            existing = catalog[feed_key]
            existing_urls = set()
            for season in existing.get("seasons", []):
                for ep in season.get("episodes", []):
                    existing_urls.add(ep.get("url", ""))

            for season in fdata.get("seasons", []):
                for ep in season.get("episodes", []):
                    if ep.get("url") not in existing_urls:
                        # Find or create the matching season
                        target_season = None
                        for es in existing.get("seasons", []):
                            if es.get("number") == season.get("number"):
                                target_season = es
                                break
                        if target_season is None:
                            target_season = {
                                "number": season.get("number"),
                                "name": season.get("name"),
                                "episodes": [],
                            }
                            existing.setdefault("seasons", []).append(target_season)
                        target_season["episodes"].append(ep)

            existing["episode_count"] = sum(
                len(s["episodes"]) for s in existing.get("seasons", [])
            )
            print(f"  ✓ Updated {feed_key}: {existing['episode_count']} episodes")
        else:
            catalog[feed_key] = fdata
            print(f"  ✓ Added {feed_key}: {ep_count} episodes")

    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Catalog saved: {CATALOG_PATH}")


# ============================================================
# Main
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "scrape":
        pass_scrape()
    elif command == "describe":
        pass_describe()
    elif command == "score":
        pass_score()
    elif command == "merge":
        pass_merge()
    elif command == "all":
        pass_scrape()
        pass_describe()
        pass_score()
        pass_merge()
    else:
        print(f"Unknown command: {command}")
        print("Usage: scrape | describe | score | merge | all")
        sys.exit(1)


if __name__ == "__main__":
    main()
