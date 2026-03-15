#!/usr/bin/env python3
"""Génère le site statique pour la bibliothèque de talks Satipanya."""

import json
import re
import shutil
from pathlib import Path
from html import escape as h
from fpdf import FPDF

# ── Configuration ──────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent
CATALOG_PATH = PROJECT_DIR / "catalog.json"
METADATA_DIR = PROJECT_DIR / "metadata"
ARTICLES_DIR = PROJECT_DIR / "articles"
COVERS_DIR  = PROJECT_DIR / "covers"
SITE_DIR    = PROJECT_DIR / "site"

SITE_TITLE = "Satipanya Dharma Library"
SITE_TAGLINE = "Dharma talks, guided meditations and teachings from Satipanya Buddhist Retreat"
FEEDS_BASE_URL = "https://www.enpleineconscience.ch/satipanya"

# Préfixe URL pour héberger le site dans un sous-dossier (ex: "/satipanya")
# Laisser vide "" pour un hébergement à la racine
SITE_BASE_PATH = "/satipanya"

# Ordre d'affichage des feeds sur la page d'accueil
FEED_ORDER = [
    "dharma-talks",
    "youtube-talks",
    "noirins-teachings",
    "dhammabytes",
    "guided-meditations",
    "foundation-course",
    "international-talks",
]

# Feeds affichés du plus récent au plus ancien (comme un blog/fil d'actualité)
FEEDS_NEWEST_FIRST = {"dharma-talks", "youtube-talks", "noirins-teachings"}

FEED_EMOJI = {
    "guided-meditations": "🧘",
    "foundation-course": "📚",
    "dhammabytes": "💎",
    "dharma-talks": "🪷",
    "noirins-teachings": "🌿",
    "international-talks": "🌍",
    "youtube-talks": "🎬",
}

# ── Utilitaires ────────────────────────────────────────────────

def format_duration(seconds):
    """Formate des secondes en HH:MM:SS ou MM:SS."""
    if not seconds:
        return ""
    s = int(seconds)
    h_val, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h_val:
        return f"{h_val}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def ep_stem(ep):
    """Extrait le nom de fichier (sans extension) du transcript_path."""
    tp = ep.get("transcript_path")
    if tp:
        return Path(tp).stem
    return None


def base(path):
    """Préfixe un chemin absolu avec SITE_BASE_PATH (ex: /satipanya/style.css)."""
    return f"{SITE_BASE_PATH}{path}"


# Mois anglais → numéro (pour parser les dates dans les titres d'épisodes)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _season_sort_key(season):
    """Clé de tri pour ordonner les saisons par date la plus récente.

    Analyse les noms de saisons et titres d'épisodes pour extraire des dates.
    Retourne (max_year, max_month, max_day) — les saisons sans date donnent (0,0,0).

    Si le nom de la saison contient une année explicite (ex: "Gaia House 2011"),
    celle-ci est utilisée directement — les titres d'épisodes ne sont pas consultés
    (ils contiennent parfois des chiffres parasites comme des tailles de fichier).
    """
    name = season.get("name", "")

    # Si le nom de la saison contient une année, l'utiliser directement
    name_years = [int(y) for y in re.findall(r'20[0-2]\d', name)]
    if name_years:
        return (max(name_years), 0, 0)

    # Sinon, chercher des dates dans les titres d'épisodes
    best = (0, 0, 0)
    for ep in season.get("episodes", []):
        text = ep.get("title", "")
        # Pattern: "2024 April.24", "2025 Jan 12", "2022 Dec 11"
        for m in re.finditer(r'(20[0-2]\d)\s+(\w+)\.?\s+(\d{1,2})', text):
            y = int(m.group(1))
            mon = _MONTHS.get(m.group(2).lower().rstrip('.'), 0)
            if mon == 0:
                continue  # Le mot après l'année n'est pas un mois → ignorer
            d = int(m.group(3))
            best = max(best, (y, mon, d))
        # Pattern: "2024 April" ou "2025 Jan" (sans jour)
        for m in re.finditer(r'(20[0-2]\d)\s+(\w+)', text):
            y = int(m.group(1))
            mon = _MONTHS.get(m.group(2).lower().rstrip('.'), 0)
            if mon:
                best = max(best, (y, mon, 0))
    return best


def ep_url(feed_slug, stem):
    """URL relative vers la page d'un épisode."""
    return base(f"/{feed_slug}/{stem}.html")


def load_metadata(feed_slug, stem):
    """Charge le fichier metadata JSON d'un épisode."""
    path = METADATA_DIR / feed_slug / f"{stem}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def load_article(feed_slug, stem):
    """Charge le texte beautifié d'un épisode."""
    path = ARTICLES_DIR / feed_slug / f"{stem}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None


def article_to_html(text):
    """Convertit un article texte en HTML avec paragraphes et italiques."""
    paragraphs = text.split("\n\n")
    html_parts = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        # Convertir *italiques* en <em>
        p = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', h(p))
        # Restaurer les sauts de ligne simples à l'intérieur d'un paragraphe
        p = p.replace("\n", "<br>")
        html_parts.append(f"<p>{p}</p>")
    return "\n".join(html_parts)


def keyword_tags_html(keywords):
    """Génère les tags de mots-clés."""
    if not keywords:
        return ""
    tags = "".join(
        f'<a href="{base("/search.html")}?q={h(kw)}" class="tag">{h(kw)}</a>'
        for kw in keywords
    )
    return f'<div class="tags">{tags}</div>'


# ── PDF ────────────────────────────────────────────────────────

FONT_DIR = Path("/usr/share/fonts/truetype/noto")

class TranscriptPDF(FPDF):
    """PDF avec en-tête et pied de page Satipanya."""

    def __init__(self, title, speaker, feed_name):
        super().__init__()
        self._title = title
        self._speaker = speaker
        self._feed_name = feed_name
        self.add_font("NotoSerif", "", str(FONT_DIR / "NotoSerif-Regular.ttf"))
        self.add_font("NotoSerif", "I", str(FONT_DIR / "NotoSerif-Italic.ttf"))
        self.add_font("NotoSerif", "B", str(FONT_DIR / "NotoSerif-Bold.ttf"))
        self.set_auto_page_break(auto=True, margin=25)

    def header(self):
        if self.page_no() == 1:
            return  # première page a son propre en-tête
        self.set_font("NotoSerif", "I", 8)
        self.set_text_color(120, 113, 108)
        self.cell(0, 8, self._title, align="L")
        self.ln(12)

    def footer(self):
        self.set_y(-20)
        self.set_font("NotoSerif", "I", 8)
        self.set_text_color(120, 113, 108)
        self.cell(0, 8, f"Satipanya Buddhist Retreat — {self._feed_name}", align="L")
        self.cell(0, 8, str(self.page_no()), align="R")


def generate_pdf(output_path, title, speaker, feed_name, duration_str, article_text):
    """Génère un PDF élégant pour un transcript beautifié."""
    pdf = TranscriptPDF(title, speaker, feed_name)
    pdf.add_page()

    # ── Page de titre intégrée ──
    pdf.ln(15)
    pdf.set_font("NotoSerif", "B", 22)
    pdf.set_text_color(45, 41, 38)
    pdf.multi_cell(0, 11, title, align="L")
    pdf.ln(4)
    pdf.set_font("NotoSerif", "", 11)
    pdf.set_text_color(120, 113, 108)
    pdf.cell(0, 7, f"{speaker}  ·  {feed_name}  ·  {duration_str}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    # Ligne décorative
    pdf.set_draw_color(184, 134, 11)  # saffron
    pdf.set_line_width(0.5)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(10)

    # ── Corps du texte ──
    pdf.set_text_color(45, 41, 38)
    paragraphs = article_text.split("\n\n")
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # Découper en segments normal / *italique*
        parts = re.split(r'(\*[^*]+\*)', para)
        for part in parts:
            if part.startswith("*") and part.endswith("*"):
                pdf.set_font("NotoSerif", "I", 10.5)
                pdf.write(6.5, part[1:-1])
            else:
                pdf.set_font("NotoSerif", "", 10.5)
                pdf.write(6.5, part)
        pdf.ln(10)

    # ── Note de fin ──
    pdf.ln(5)
    pdf.set_draw_color(184, 134, 11)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 40, pdf.get_y())
    pdf.ln(4)
    pdf.set_font("NotoSerif", "I", 8)
    pdf.set_text_color(120, 113, 108)
    pdf.multi_cell(0, 5,
        "Transcriptions produced locally using Swiss low-carbon electricity. "
        "Corrections and rewriting by cloud-hosted AI.")

    pdf.output(str(output_path))


# ── CSS ────────────────────────────────────────────────────────

CSS = """\
/* ── Reset ───────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

/* ── Variables ───────────────────────────────────────── */
:root {
  --bg: #FDFBF7;
  --bg-card: #FFFFFF;
  --bg-alt: #F7F4EF;
  --bg-hero: linear-gradient(135deg, #2D2926 0%, #44403C 100%);
  --text: #2D2926;
  --text-secondary: #78716C;
  --text-inverse: #FDFBF7;
  --accent: #B8860B;
  --accent-hover: #96700A;
  --accent-light: #FEF3C7;
  --accent-subtle: #F5E6CF;
  --border: #E7E5E4;
  --border-light: #F0EEEB;
  --link: #5B7B6F;
  --link-hover: #3D5A4F;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
  --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
  --shadow-md: 0 4px 6px rgba(0,0,0,0.05), 0 2px 4px rgba(0,0,0,0.04);
  --radius: 10px;
  --radius-sm: 6px;
  --font-serif: 'Crimson Pro', Georgia, 'Times New Roman', serif;
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --max-w: 1120px;
  --content-w: 700px;
}

/* ── Base ────────────────────────────────────────────── */
html { scroll-behavior: smooth; }
body {
  font-family: var(--font-sans);
  color: var(--text);
  background: var(--bg);
  line-height: 1.6;
  font-size: 15px;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--link); text-decoration: none; transition: color .15s; }
a:hover { color: var(--link-hover); }
img { max-width: 100%; height: auto; display: block; }

/* ── Header ──────────────────────────────────────────── */
.site-header {
  background: var(--bg-card);
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 100;
  backdrop-filter: blur(12px);
  background: rgba(253,251,247,0.92);
}
.header-inner {
  max-width: var(--max-w);
  margin: 0 auto;
  padding: 0 1.5rem;
  height: 56px;
  display: flex; align-items: center; justify-content: space-between;
}
.site-logo {
  font-family: var(--font-serif);
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--text);
  letter-spacing: -0.01em;
}
.site-logo:hover { color: var(--accent); }
.header-nav { display: flex; align-items: center; gap: 1.5rem; }
.header-nav a { font-size: 0.875rem; color: var(--text-secondary); font-weight: 500; }
.header-nav a:hover { color: var(--accent); }
.header-search {
  display: flex; align-items: center;
  background: var(--bg-alt);
  border: 1px solid var(--border);
  border-radius: 100px;
  padding: 0.375rem 0.75rem;
  gap: 0.5rem;
  transition: border-color .15s, box-shadow .15s;
}
.header-search:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-light);
}
.header-search input {
  border: none; background: none; outline: none;
  font-size: 0.875rem; font-family: var(--font-sans);
  color: var(--text); width: 180px;
}
.header-search input::placeholder { color: var(--text-secondary); }
.header-search svg { flex-shrink: 0; color: var(--text-secondary); }

/* ── Hero ────────────────────────────────────────────── */
.hero {
  background: var(--bg-hero);
  color: var(--text-inverse);
  padding: 4rem 1.5rem;
  text-align: center;
}
.hero h1 {
  font-family: var(--font-serif);
  font-size: clamp(2rem, 5vw, 3rem);
  font-weight: 600;
  margin-bottom: 0.75rem;
  letter-spacing: -0.02em;
}
.hero p {
  font-size: 1.1rem;
  opacity: 0.85;
  max-width: 560px;
  margin: 0 auto 2rem;
  line-height: 1.7;
}
.hero-stats {
  display: flex; justify-content: center; gap: 2.5rem;
  font-size: 0.875rem; opacity: 0.7;
}
.hero-stats strong { display: block; font-size: 1.5rem; opacity: 1; font-weight: 700; }

/* ── Container ───────────────────────────────────────── */
.container { max-width: var(--max-w); margin: 0 auto; padding: 0 1.5rem; }

/* ── Feed Cards (homepage) ───────────────────────────── */
.feeds-section { padding: 3rem 0 4rem; }
.feeds-section h2 {
  font-family: var(--font-serif);
  font-size: 1.5rem;
  font-weight: 600;
  margin-bottom: 1.5rem;
  text-align: center;
  color: var(--text);
}
.feeds-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 1.25rem;
}
.feed-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  transition: box-shadow .2s, transform .2s;
  display: flex; flex-direction: column;
}
.feed-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}
a.feed-card { color: inherit; text-decoration: none; }
.feed-card-header {
  padding: 1.5rem 1.5rem 1rem;
  display: flex; align-items: flex-start; gap: 1rem;
}
.feed-card-emoji { font-size: 2rem; line-height: 1; flex-shrink: 0; }
.feed-card-header h3 {
  font-family: var(--font-serif);
  font-size: 1.25rem;
  font-weight: 600;
  line-height: 1.3;
}
.feed-card-header h3 a { color: var(--text); }
.feed-card-header h3 a:hover { color: var(--accent); }
.feed-card-body {
  padding: 0 1.5rem 1.5rem;
  flex: 1;
}
.feed-card-body p {
  font-size: 0.9rem;
  color: var(--text-secondary);
  line-height: 1.6;
  margin-bottom: 1rem;
}
.feed-card-meta {
  font-size: 0.8rem;
  color: var(--text-secondary);
  display: flex; gap: 1rem;
}
.feed-card-meta span {
  display: flex; align-items: center; gap: 0.3rem;
}
.feed-card-meta .subscribe {
  margin-left: auto;
  color: var(--accent);
  transition: color .15s;
}
.feed-card-meta .subscribe:hover { color: var(--accent-hover); }

/* ── Feed Page ───────────────────────────────────────── */
.feed-header {
  padding: 2.5rem 0 2rem;
  border-bottom: 1px solid var(--border);
  margin-bottom: 2rem;
}
.feed-header h1 {
  font-family: var(--font-serif);
  font-size: clamp(1.75rem, 4vw, 2.25rem);
  font-weight: 600;
  margin-bottom: 0.5rem;
}
.feed-header p {
  color: var(--text-secondary);
  max-width: var(--content-w);
  line-height: 1.7;
}
.season-section { margin-bottom: 2.5rem; }
.season-title {
  font-family: var(--font-serif);
  font-size: 1.2rem;
  font-weight: 600;
  color: var(--accent);
  margin-bottom: 1rem;
  padding-bottom: 0.5rem;
  border-bottom: 2px solid var(--accent-light);
}
.episode-list { list-style: none; }
.episode-item {
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm);
  padding: 1rem 1.25rem;
  margin-bottom: 0.5rem;
  background: var(--bg-card);
  transition: border-color .15s, box-shadow .15s;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.5rem 1rem;
  align-items: start;
}
.episode-item:hover {
  border-color: var(--accent-light);
  box-shadow: var(--shadow-sm);
}
.episode-title {
  font-family: var(--font-serif);
  font-size: 1.05rem;
  font-weight: 600;
  line-height: 1.4;
}
.episode-title a { color: var(--text); }
.episode-title a:hover { color: var(--accent); }
.episode-desc {
  grid-column: 1 / -1;
  font-size: 0.875rem;
  color: var(--text-secondary);
  line-height: 1.6;
}
.episode-duration {
  font-size: 0.8rem;
  color: var(--text-secondary);
  white-space: nowrap;
  padding-top: 0.15rem;
}

/* ── Episode Page ────────────────────────────────────── */
.episode-header { padding: 2.5rem 0 1.5rem; }
.episode-header h1 {
  font-family: var(--font-serif);
  font-size: clamp(1.5rem, 4vw, 2rem);
  font-weight: 600;
  line-height: 1.3;
  margin-bottom: 0.75rem;
}
.episode-meta {
  display: flex; flex-wrap: wrap; gap: 0.5rem 1.5rem;
  font-size: 0.875rem;
  color: var(--text-secondary);
  margin-bottom: 1.5rem;
}
.episode-meta span { display: flex; align-items: center; gap: 0.35rem; }

/* Audio player */
.audio-player {
  background: var(--bg-alt);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.25rem;
  margin-bottom: 2rem;
}
.audio-player audio {
  width: 100%;
  height: 40px;
}
.youtube-embed {
  position: relative;
  padding-bottom: 56.25%;
  height: 0;
  overflow: hidden;
  border-radius: 8px;
}
.youtube-embed iframe {
  position: absolute;
  top: 0; left: 0;
  width: 100%; height: 100%;
  border-radius: 8px;
}
.audio-source {
  margin-top: 0.5rem;
  font-size: 0.75rem;
  color: var(--text-secondary);
}
.audio-source a { color: var(--text-secondary); text-decoration: underline; }

/* Description */
.episode-description {
  margin-bottom: 2rem;
  line-height: 1.8;
  color: var(--text);
}
.episode-description p { margin-bottom: 1rem; }
.episode-description p:last-child { margin-bottom: 0; }

/* Tags */
.tags { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 2rem; }
.tag {
  display: inline-block;
  background: var(--accent-light);
  color: var(--accent);
  font-size: 0.75rem;
  font-weight: 500;
  padding: 0.2rem 0.6rem;
  border-radius: 100px;
  transition: background .15s, color .15s;
}
.tag:hover { background: var(--accent); color: white; }

/* Transcript */
.transcript-section {
  border-top: 1px solid var(--border);
  padding-top: 2rem;
  margin-bottom: 3rem;
}
.transcript-section h2 {
  font-family: var(--font-serif);
  font-size: 1.25rem;
  font-weight: 600;
  margin-bottom: 1.5rem;
  color: var(--text);
}
.transcript-text {
  font-family: var(--font-serif);
  font-size: 1.1rem;
  line-height: 2;
  color: #3D3835;
  max-width: var(--content-w);
}
.transcript-text p {
  margin-bottom: 1.25rem;
  text-indent: 0;
}
.transcript-text em { font-style: italic; }
.transcript-unavailable {
  color: var(--text-secondary);
  font-style: italic;
  padding: 2rem;
  text-align: center;
  background: var(--bg-alt);
  border-radius: var(--radius);
}
.transcript-header {
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 0.75rem;
  margin-bottom: 1.5rem;
}
.transcript-header h2 { margin-bottom: 0; }
.btn-pdf {
  display: inline-flex; align-items: center; gap: 0.4rem;
  font-size: 0.8rem; font-weight: 500;
  color: var(--accent);
  background: var(--accent-light);
  padding: 0.35rem 0.85rem;
  border-radius: 100px;
  transition: background .15s, color .15s;
}
.btn-pdf:hover { background: var(--accent); color: white; }

/* Episode navigation */
.episode-nav {
  display: flex; justify-content: space-between;
  padding: 1.5rem 0;
  border-top: 1px solid var(--border);
  margin-top: 2rem;
  gap: 1rem;
}
.episode-nav a {
  font-size: 0.875rem;
  color: var(--text-secondary);
  display: flex; align-items: center; gap: 0.35rem;
  max-width: 45%;
}
.episode-nav a:hover { color: var(--accent); }
.episode-nav .next { margin-left: auto; text-align: right; }

/* ── Breadcrumb ──────────────────────────────────────── */
.breadcrumb {
  font-size: 0.8rem;
  color: var(--text-secondary);
  padding: 1rem 0 0;
}
.breadcrumb a { color: var(--text-secondary); }
.breadcrumb a:hover { color: var(--accent); }
.breadcrumb span { margin: 0 0.4rem; opacity: 0.5; }

/* ── Search Page ─────────────────────────────────────── */
.search-page { padding: 2.5rem 0 4rem; }
.search-page h1 {
  font-family: var(--font-serif);
  font-size: 1.75rem;
  margin-bottom: 1.5rem;
}
.search-input-large {
  width: 100%;
  max-width: 600px;
  padding: 0.75rem 1rem 0.75rem 2.75rem;
  font-size: 1rem;
  font-family: var(--font-sans);
  border: 2px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-card) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='%2378716C' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='8'/%3E%3Cline x1='21' y1='21' x2='16.65' y2='16.65'/%3E%3C/svg%3E") no-repeat 0.75rem center;
  outline: none;
  transition: border-color .15s, box-shadow .15s;
}
.search-input-large:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-light);
}
.search-info {
  font-size: 0.875rem;
  color: var(--text-secondary);
  margin: 1rem 0;
}
.search-results { list-style: none; margin-top: 1rem; }
.search-result-item {
  border-bottom: 1px solid var(--border-light);
  padding: 1.25rem 0;
}
.search-result-item:last-child { border-bottom: none; }
.search-result-title {
  font-family: var(--font-serif);
  font-size: 1.1rem;
  font-weight: 600;
  margin-bottom: 0.25rem;
}
.search-result-title a { color: var(--text); }
.search-result-title a:hover { color: var(--accent); }
.search-result-meta {
  font-size: 0.8rem;
  color: var(--text-secondary);
  margin-bottom: 0.4rem;
}
.search-result-excerpt {
  font-size: 0.9rem;
  color: var(--text-secondary);
  line-height: 1.6;
}

/* ── Footer ──────────────────────────────────────────── */
.site-footer {
  background: var(--bg-alt);
  border-top: 1px solid var(--border);
  padding: 2rem 1.5rem;
  text-align: center;
  font-size: 0.8rem;
  color: var(--text-secondary);
}
.site-footer a { color: var(--text-secondary); text-decoration: underline; }
.site-footer a:hover { color: var(--accent); }

/* ── Responsive ──────────────────────────────────────── */
@media (max-width: 768px) {
  .header-inner { height: 48px; }
  .header-nav { gap: 1rem; }
  .header-search input { width: 120px; }
  .hero { padding: 2.5rem 1.25rem; }
  .hero-stats { gap: 1.5rem; }
  .feeds-grid { grid-template-columns: 1fr; }
  .episode-item { grid-template-columns: 1fr; }
  .episode-duration { order: -1; }
  .episode-nav { flex-direction: column; }
  .episode-nav a { max-width: 100%; }
}
@media (max-width: 480px) {
  .header-search { display: none; }
  .hero h1 { font-size: 1.75rem; }
  .feed-card-header { padding: 1.25rem 1.25rem 0.75rem; }
  .feed-card-body { padding: 0 1.25rem 1.25rem; }
}
"""

# ── Icônes SVG ─────────────────────────────────────────

SVG_SEARCH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>'
SVG_ARROW_LEFT = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>'
SVG_ARROW_RIGHT = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>'
SVG_SPEAKER = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>'
SVG_CLOCK = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>'
SVG_FOLDER = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'
SVG_EPISODES = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>'
SVG_BOOK = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>'
SVG_DOWNLOAD = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
SVG_HEADPHONES = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>'


# ── Templates HTML ─────────────────────────────────────

def html_base(title, body_html, breadcrumbs=None, extra_head="", body_class=""):
    """Enveloppe le contenu dans le template de base HTML."""
    bc = ""
    if breadcrumbs:
        items = [f'<a href="{url}">{label}</a>' for label, url in breadcrumbs]
        bc = f'<div class="container"><nav class="breadcrumb">{"<span>›</span>".join(items)}</nav></div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{h(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Crimson+Pro:ital,wght@0,400;0,600;0,700;1,400&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="icon" type="image/png" href="{base('/favicon.png')}">
  <link rel="apple-touch-icon" href="{base('/apple-touch-icon.png')}">
  <link rel="stylesheet" href="{base('/style.css')}">
  {extra_head}
</head>
<body class="{body_class}">
  <header class="site-header">
    <div class="header-inner">
      <a href="{base('/')}" class="site-logo">Satipanya</a>
      <nav class="header-nav">
        <a href="{base('/')}">Collections</a>
        <a href="{base('/search.html')}">Search</a>
        <form class="header-search" action="{base('/search.html')}" method="get">
          {SVG_SEARCH}
          <input type="search" name="q" placeholder="Search talks…" autocomplete="off">
        </form>
      </nav>
    </div>
  </header>
  {bc}
  <main>
{body_html}
  </main>
  <footer class="site-footer">
    <p>Talks by <a href="https://www.satipanya.org.uk" target="_blank" rel="noopener">Satipanya Buddhist Retreat</a>.
    Site compiled from the original audio archive.</p>
    <p>Transcriptions produced locally using Swiss low-carbon electricity. Corrections and rewriting by cloud-hosted AI.
    <br>Source code and full archive on <a href="https://github.com/funkypitt/satipanya-dharma-library">GitHub</a>.</p>
  </footer>
</body>
</html>"""


# ── Générateurs de pages ───────────────────────────────

def build_homepage(catalog):
    """Génère la page d'accueil."""
    # Calcul stats globales
    total_episodes = 0
    total_hours = 0
    total_articles = 0
    for slug in FEED_ORDER:
        fdata = catalog.get(slug)
        if not fdata:
            continue
        for season in fdata.get("seasons", []):
            for ep in season.get("episodes", []):
                dur = ep.get("duration_seconds", 0)
                if dur == 0 or not ep_stem(ep):
                    continue
                total_episodes += 1
                total_hours += dur / 3600
                stem = ep_stem(ep)
                if stem and (ARTICLES_DIR / slug / f"{stem}.txt").exists():
                    total_articles += 1

    # Feed cards
    cards = []
    for slug in FEED_ORDER:
        fdata = catalog.get(slug)
        if not fdata:
            continue
        emoji = FEED_EMOJI.get(slug, "📖")
        ep_count = 0
        for season in fdata.get("seasons", []):
            for ep in season.get("episodes", []):
                if ep.get("duration_seconds", 0) > 0:
                    ep_count += 1
        # Subscribe link: YouTube channel or podcast://
        if slug == "youtube-talks":
            subscribe_url = "https://www.youtube.com/@satipanya-insight"
            subscribe_label = f'{SVG_HEADPHONES} YouTube'
        else:
            subscribe_url = f"podcast://{FEEDS_BASE_URL.replace('https://', '')}/{slug}.xml"
            subscribe_label = f'{SVG_HEADPHONES} Subscribe'
        # Liens livres (toujours inclus — générés par build_books.py)
        book_links = (
            f'<a href="{base(f"/books/{slug}.pdf")}" class="subscribe">{SVG_BOOK} PDF</a>'
            f'<a href="{base(f"/books/{slug}.epub")}" class="subscribe">{SVG_BOOK} EPUB</a>'
        )
        cards.append(f"""
      <div class="feed-card">
        <div class="feed-card-header">
          <div class="feed-card-emoji">{emoji}</div>
          <h3><a href="{base(f"/{slug}/")}">{h(fdata['name'].replace('Satipanya — ', ''))}</a></h3>
        </div>
        <div class="feed-card-body">
          <p>{h(fdata.get('description', ''))}</p>
          <div class="feed-card-meta">
            <span>{SVG_EPISODES} {ep_count} talks</span>
            <span>{SVG_FOLDER} {fdata.get('season_count', 1)} seasons</span>
            <a href="{subscribe_url}" class="subscribe">{subscribe_label}</a>
            {book_links}
          </div>
        </div>
      </div>""")

    body = f"""
    <section class="hero">
      <div class="container">
        <h1>{SITE_TITLE}</h1>
        <p>{SITE_TAGLINE}</p>
        <div class="hero-stats">
          <div><strong>{total_episodes}</strong> talks</div>
          <div><strong>{int(total_hours)}</strong> hours</div>
          <div><strong>{total_articles}</strong> transcripts</div>
        </div>
      </div>
    </section>
    <section class="feeds-section">
      <div class="container">
        <h2>Browse by Collection</h2>
        <div class="feeds-grid">
          {"".join(cards)}
        </div>
      </div>
    </section>"""

    return html_base(SITE_TITLE, body, body_class="page-home")


def build_feed_page(slug, fdata, catalog):
    """Génère la page d'une collection/feed."""
    name = fdata["name"].replace("Satipanya — ", "")
    desc = fdata.get("description", "")

    newest_first = slug in FEEDS_NEWEST_FIRST
    seasons_list = list(fdata.get("seasons", []))
    if newest_first:
        seasons_list = sorted(seasons_list, key=_season_sort_key, reverse=True)

    seasons_html = []
    for season in seasons_list:
        episodes = list(season.get("episodes", []))
        if newest_first:
            episodes = list(reversed(episodes))
        items = []
        for ep in episodes:
            dur = ep.get("duration_seconds", 0)
            stem = ep_stem(ep)
            if dur == 0 or not stem:
                continue
            meta = load_metadata(slug, stem)
            title = meta.get("title_clean", ep.get("title", "Untitled"))
            desc_short = meta.get("description_short", ep.get("description_short", ""))
            url = ep_url(slug, stem)
            items.append(f"""
          <li class="episode-item">
            <div class="episode-title"><a href="{url}">{h(title)}</a></div>
            <div class="episode-duration">{SVG_CLOCK} {format_duration(dur)}</div>
            <div class="episode-desc">{h(desc_short)}</div>
          </li>""")

        if items:
            s_name = season.get("name", f"Season {season.get('number', '?')}")
            seasons_html.append(f"""
        <section class="season-section">
          <h2 class="season-title">{h(s_name)}</h2>
          <ul class="episode-list">{"".join(items)}</ul>
        </section>""")

    body = f"""
    <div class="container">
      <div class="feed-header">
        <h1>{FEED_EMOJI.get(slug, '📖')} {h(name)}</h1>
        <p>{h(desc)}</p>
      </div>
      {"".join(seasons_html)}
    </div>"""

    breadcrumbs = [("Home", base("/")), (name, base(f"/{slug}/"))]
    return html_base(f"{name} — {SITE_TITLE}", body, breadcrumbs=breadcrumbs)


def build_episode_page(slug, ep, prev_ep, next_ep, feed_name):
    """Génère la page d'un épisode."""
    stem = ep_stem(ep)
    meta = load_metadata(slug, stem) if stem else {}
    article = load_article(slug, stem) if stem else None

    title = meta.get("title_clean", ep.get("title", "Untitled"))
    desc_long = meta.get("description_long") or ep.get("description_long", "")
    desc_short = meta.get("description_short") or ep.get("description_short", "")
    keywords = meta.get("keywords", [])
    speaker = ep.get("speaker", "")
    dur = ep.get("duration_seconds", 0)
    audio_url = ep.get("url", "")
    clean_feed = feed_name.replace("Satipanya — ", "")

    # Description HTML
    desc_html = ""
    if desc_long:
        # Retirer la note auto-générée
        desc_text = desc_long.replace("(This description was generated automatically, inaccuracies may happen in the process.)", "").strip()
        desc_paras = [f"<p>{h(p.strip())}</p>" for p in desc_text.split("\n\n") if p.strip()]
        desc_html = f'<div class="episode-description">{"".join(desc_paras)}</div>'
    elif desc_short:
        desc_html = f'<div class="episode-description"><p>{h(desc_short)}</p></div>'

    # Audio / Video
    audio_html = ""
    if audio_url:
        # YouTube: embed iframe; otherwise: audio player
        yt_match = re.search(r'youtube\.com/watch\?v=([A-Za-z0-9_-]+)', audio_url)
        if yt_match:
            yt_id = yt_match.group(1)
            audio_html = f"""
      <div class="audio-player">
        <div class="youtube-embed">
          <iframe src="https://www.youtube.com/embed/{yt_id}" frameborder="0"
                  allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                  allowfullscreen></iframe>
        </div>
        <div class="audio-source">Source: <a href="{h(audio_url)}" target="_blank">YouTube</a></div>
      </div>"""
        else:
            audio_html = f"""
      <div class="audio-player">
        <audio controls preload="none" src="{h(audio_url)}">
          Your browser does not support the audio element.
        </audio>
        <div class="audio-source">Source: <a href="{h(audio_url)}" target="_blank">satipanya.org.uk</a></div>
      </div>"""

    # Transcript
    transcript_html = ""
    pdf_link = ""
    if article and stem:
        pdf_name = f"{stem}.pdf"
        pdf_link = f'<a href="{base(f"/{slug}/{pdf_name}")}" class="btn-pdf" download>{SVG_DOWNLOAD} PDF</a>'
        transcript_html = f"""
      <section class="transcript-section">
        <div class="transcript-header">
          <h2>Transcript</h2>
          {pdf_link}
        </div>
        <div class="transcript-text">
          {article_to_html(article)}
        </div>
      </section>"""
    else:
        transcript_html = """
      <section class="transcript-section">
        <h2>Transcript</h2>
        <p class="transcript-unavailable">Written transcript not yet available for this talk.</p>
      </section>"""

    # Navigation prev/next
    nav_html = ""
    nav_parts = []
    if prev_ep:
        p_stem = ep_stem(prev_ep)
        p_meta = load_metadata(slug, p_stem) if p_stem else {}
        p_title = p_meta.get("title_clean", prev_ep.get("title", "Previous"))
        nav_parts.append(f'<a href="{ep_url(slug, p_stem)}" class="prev">{SVG_ARROW_LEFT} {h(p_title)}</a>')
    else:
        nav_parts.append('<span></span>')
    if next_ep:
        n_stem = ep_stem(next_ep)
        n_meta = load_metadata(slug, n_stem) if n_stem else {}
        n_title = n_meta.get("title_clean", next_ep.get("title", "Next"))
        nav_parts.append(f'<a href="{ep_url(slug, n_stem)}" class="next">{h(n_title)} {SVG_ARROW_RIGHT}</a>')
    if any(s.strip() for s in nav_parts):
        nav_html = f'<nav class="episode-nav">{"".join(nav_parts)}</nav>'

    body = f"""
    <div class="container">
      <div class="episode-header">
        <h1>{h(title)}</h1>
        <div class="episode-meta">
          <span>{SVG_SPEAKER} {h(speaker)}</span>
          <span>{SVG_CLOCK} {format_duration(dur)}</span>
          <span>{SVG_FOLDER} <a href="{base(f"/{slug}/")}">{h(clean_feed)}</a></span>
        </div>
      </div>
      {audio_html}
      {desc_html}
      {keyword_tags_html(keywords)}
      {transcript_html}
      {nav_html}
    </div>"""

    breadcrumbs = [("Home", base("/")), (clean_feed, base(f"/{slug}/")), (title, base(f"/{slug}/{stem}.html"))]
    return html_base(f"{title} — {SITE_TITLE}", body, breadcrumbs=breadcrumbs)


def build_search_page():
    """Génère la page de recherche."""
    body = """
    <div class="container search-page">
      <h1>Search the Dharma Library</h1>
      <input type="search" id="search-input" class="search-input-large"
             placeholder="Search by topic, Pali term, speaker, keyword…" autofocus>
      <div id="search-info" class="search-info"></div>
      <ul id="search-results" class="search-results"></ul>
    </div>"""

    search_js = """
<script src="https://cdn.jsdelivr.net/npm/minisearch@7.1.1/dist/umd/index.min.js"></script>
<script>
(function() {
  let ms, docs;
  const input = document.getElementById('search-input');
  const resultsEl = document.getElementById('search-results');
  const infoEl = document.getElementById('search-info');

  // Charger l'index
  fetch('__BASE__/search-index.json')
    .then(r => r.json())
    .then(data => {
      docs = data;
      ms = new MiniSearch({
        fields: ['title', 'description', 'keywords', 'speaker', 'transcript_excerpt'],
        storeFields: ['title', 'feed', 'speaker', 'duration', 'description_short', 'url'],
        searchOptions: {
          boost: { title: 3, keywords: 2, description: 1.5 },
          fuzzy: 0.2,
          prefix: true
        }
      });
      ms.addAll(docs);
      infoEl.textContent = docs.length + ' talks indexed';
      // Chercher si query dans l'URL
      const q = new URLSearchParams(location.search).get('q');
      if (q) { input.value = q; doSearch(q); }
    });

  input.addEventListener('input', (e) => doSearch(e.target.value));

  function doSearch(query) {
    if (!ms || !query.trim()) {
      resultsEl.innerHTML = '';
      if (ms) infoEl.textContent = docs.length + ' talks indexed';
      return;
    }
    const results = ms.search(query, { combineWith: 'AND' });
    infoEl.textContent = results.length + ' result' + (results.length !== 1 ? 's' : '');
    resultsEl.innerHTML = results.slice(0, 50).map(r => `
      <li class="search-result-item">
        <div class="search-result-title"><a href="${r.url}">${esc(r.title)}</a></div>
        <div class="search-result-meta">${esc(r.feed)} · ${esc(r.speaker)} · ${r.duration}</div>
        <div class="search-result-excerpt">${esc(r.description_short)}</div>
      </li>
    `).join('');
  }

  function esc(s) {
    if (!s) return '';
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }
})();
</script>"""

    page = html_base("Search — " + SITE_TITLE, body, extra_head="", body_class="page-search")
    search_js_resolved = search_js.replace("__BASE__", SITE_BASE_PATH)
    return page.replace("</body>", search_js_resolved + "\n</body>")


def build_search_index(catalog):
    """Construit l'index JSON pour la recherche côté client."""
    items = []
    idx = 0
    for slug in FEED_ORDER:
        fdata = catalog.get(slug)
        if not fdata:
            continue
        feed_name = fdata["name"].replace("Satipanya — ", "")
        for season in fdata.get("seasons", []):
            for ep in season.get("episodes", []):
                dur = ep.get("duration_seconds", 0)
                if dur == 0:
                    continue
                stem = ep_stem(ep)
                if not stem:
                    continue
                meta = load_metadata(slug, stem)
                article = load_article(slug, stem)

                title = meta.get("title_clean", ep.get("title", ""))
                desc_short = meta.get("description_short") or ep.get("description_short", "")
                desc_long = meta.get("description_long") or ep.get("description_long", "")
                # Retirer la note auto-générée
                desc_long = desc_long.replace(
                    "(This description was generated automatically, inaccuracies may happen in the process.)", ""
                ).strip()
                keywords = " ".join(meta.get("keywords", []))

                # Extrait du transcript pour la recherche
                transcript_excerpt = ""
                if article:
                    transcript_excerpt = article[:2000]

                items.append({
                    "id": idx,
                    "title": title,
                    "feed": feed_name,
                    "speaker": ep.get("speaker", ""),
                    "duration": format_duration(dur),
                    "description": f"{desc_short} {desc_long}",
                    "description_short": desc_short,
                    "keywords": keywords,
                    "transcript_excerpt": transcript_excerpt,
                    "url": ep_url(slug, stem),
                })
                idx += 1
    return items


# ── Construction du site ───────────────────────────────

def build_site():
    """Point d'entrée : génère tout le site statique."""
    print("=" * 60)
    print("Building Satipanya Dharma Library static site")
    print("=" * 60)

    # Charger le catalogue
    with open(CATALOG_PATH) as f:
        raw_catalog = json.load(f)

    # Réindexer par slug
    catalog = {}
    for key, fdata in raw_catalog.items():
        slug = fdata.get("slug", key.replace("_", "-"))
        catalog[slug] = fdata

    # Nettoyer et créer le répertoire de sortie (préserver books/)
    if SITE_DIR.exists():
        for item in SITE_DIR.iterdir():
            if item.name == "books":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    else:
        SITE_DIR.mkdir(parents=True)

    # CSS
    (SITE_DIR / "style.css").write_text(CSS, encoding="utf-8")
    print(f"  ✓ style.css")

    # Covers
    covers_out = SITE_DIR / "covers"
    covers_out.mkdir()
    for png in COVERS_DIR.glob("*.png"):
        shutil.copy2(png, covers_out / png.name)
    print(f"  ✓ covers/ ({len(list(covers_out.glob('*.png')))} images)")

    # Favicon (depuis le logo Satipanya)
    favicon_src = COVERS_DIR / "favicon-source.png"
    if favicon_src.exists():
        import subprocess
        subprocess.run(["convert", str(favicon_src), "-resize", "32x32",
                        str(SITE_DIR / "favicon.png")], check=True)
        subprocess.run(["convert", str(favicon_src), "-resize", "180x180",
                        str(SITE_DIR / "apple-touch-icon.png")], check=True)
        print(f"  ✓ favicon.png + apple-touch-icon.png")

    # RSS feeds (copiés depuis feeds/ pour tout avoir dans site/)
    feeds_src = PROJECT_DIR / "feeds"
    if feeds_src.is_dir():
        n_feeds = 0
        for xml in feeds_src.glob("*.xml"):
            shutil.copy2(xml, SITE_DIR / xml.name)
            n_feeds += 1
        if n_feeds:
            print(f"  ✓ {n_feeds} RSS feeds (*.xml)")

    # Homepage
    (SITE_DIR / "index.html").write_text(build_homepage(catalog), encoding="utf-8")
    print(f"  ✓ index.html")

    # Pages de feeds et épisodes
    total_episodes = 0
    total_pdfs = 0
    prev_pdfs = 0
    for slug in FEED_ORDER:
        fdata = catalog.get(slug)
        if not fdata:
            continue
        feed_dir = SITE_DIR / slug
        feed_dir.mkdir()
        feed_name = fdata["name"]

        # Page feed
        (feed_dir / "index.html").write_text(
            build_feed_page(slug, fdata, catalog), encoding="utf-8"
        )

        # Collecter tous les épisodes valides (pour navigation prev/next)
        newest_first = slug in FEEDS_NEWEST_FIRST
        seasons_iter = list(fdata.get("seasons", []))
        if newest_first:
            seasons_iter = sorted(seasons_iter, key=_season_sort_key, reverse=True)
        valid_eps = []
        for season in seasons_iter:
            episodes = list(season.get("episodes", []))
            if newest_first:
                episodes = list(reversed(episodes))
            for ep in episodes:
                if ep.get("duration_seconds", 0) > 0 and ep_stem(ep):
                    valid_eps.append(ep)

        # Pages épisodes
        for i, ep in enumerate(valid_eps):
            prev_ep = valid_eps[i - 1] if i > 0 else None
            next_ep = valid_eps[i + 1] if i < len(valid_eps) - 1 else None
            stem = ep_stem(ep)
            page_html = build_episode_page(slug, ep, prev_ep, next_ep, feed_name)
            (feed_dir / f"{stem}.html").write_text(page_html, encoding="utf-8")
            total_episodes += 1

            # PDF si un article beautifié existe
            article = load_article(slug, stem)
            if article:
                meta = load_metadata(slug, stem)
                title = meta.get("title_clean", ep.get("title", ""))
                dur_str = format_duration(ep.get("duration_seconds", 0))
                clean_feed = feed_name.replace("Satipanya — ", "")
                generate_pdf(
                    feed_dir / f"{stem}.pdf",
                    title, ep.get("speaker", ""), clean_feed, dur_str, article,
                )
                total_pdfs += 1

        print(f"  ✓ {slug}/ (index + {len(valid_eps)} episodes, {total_pdfs - prev_pdfs} PDFs)")
        prev_pdfs = total_pdfs

    # Search index
    search_data = build_search_index(catalog)
    (SITE_DIR / "search-index.json").write_text(
        json.dumps(search_data, ensure_ascii=False), encoding="utf-8"
    )
    index_size_kb = (SITE_DIR / "search-index.json").stat().st_size / 1024
    print(f"  ✓ search-index.json ({len(search_data)} entries, {index_size_kb:.0f} KB)")

    # Search page
    (SITE_DIR / "search.html").write_text(build_search_page(), encoding="utf-8")
    print(f"  ✓ search.html")

    print(f"\n{'=' * 60}")
    print(f"Site built: {total_episodes} episode pages, {total_pdfs} PDFs, 6 feed pages")
    print(f"Output: {SITE_DIR}/")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    build_site()
