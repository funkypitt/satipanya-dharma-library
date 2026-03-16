#!/usr/bin/env python3
"""
build_bhante_book.py — Livre curé des enseignements de Bhante Bodhidhamma.

Sélectionne les 80-120 meilleurs talks parmi ~462 épisodes (Dharma Talks,
YouTube Talks, DhammaBytes), élimine les doublons, organise thématiquement,
et produit un PDF + EPUB de qualité premium.

4 passes séquentielles :
  1. analyze  — classification thématique de chaque article via Claude
  2. select   — clustering + sélection des meilleurs 80-120 talks
  3. organize — structuration en Parties / Chapitres
  4. generate — production PDF (WeasyPrint) + EPUB (ebooklib)

Usage :
    python build_bhante_book.py analyze
    python build_bhante_book.py select
    python build_bhante_book.py organize
    conda run -n newspapers python build_bhante_book.py generate
"""

import json
import sys
import os
import re
import io
import base64
import time
from pathlib import Path
from html import escape as esc
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    import anthropic
except ImportError:
    anthropic = None  # pas nécessaire pour pass 4 (generate)

# ── Configuration ──────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent
CATALOG_PATH = PROJECT_DIR / "catalog.json"
METADATA_DIR = PROJECT_DIR / "metadata"
ARTICLES_DIR = PROJECT_DIR / "articles"
BOOKS_DIR    = PROJECT_DIR / "site" / "books"

ANALYSIS_PATH   = PROJECT_DIR / "book_analysis.json"
SELECTION_PATH  = PROJECT_DIR / "book_selection.json"
STRUCTURE_PATH  = PROJECT_DIR / "book_structure.json"

CLAUDE_MODEL = "claude-sonnet-4-20250514"

# Séries à inclure dans le livre curé
TARGET_FEEDS = ["dharma-talks", "youtube-talks", "dhammabytes"]

# Vocabulaire contrôlé des thèmes (progression Mahasi/Theravada)
THEMES = [
    "The Buddha's Life & Teaching",
    "Faith & Approaching the Dhamma",
    "The Four Noble Truths",
    "Impermanence (Anicca)",
    "Suffering (Dukkha)",
    "Not-Self (Anatta)",
    "Dependent Origination",
    "Right View (Samma Ditthi)",
    "Right Intention (Samma Sankappa)",
    "Right Speech (Samma Vaca)",
    "Right Action (Samma Kammanta)",
    "Right Livelihood (Samma Ajiva)",
    "Sila & Ethics / The Precepts",
    "Right Effort (Samma Vayama)",
    "Right Mindfulness (Samma Sati)",
    "Satipatthana — Mindfulness of Body",
    "Satipatthana — Mindfulness of Feeling",
    "Satipatthana — Mindfulness of Mind",
    "Satipatthana — Mindfulness of Dhammas",
    "Right Concentration (Samma Samadhi) & Jhana",
    "The Five Hindrances",
    "Metta & Brahma Viharas",
    "Kamma & Rebirth",
    "The Mahasi Method & Vipassana Practice",
    "Daily Life & Integration",
    "Death & Impermanence of Life",
    "Advanced Insight & Nibbana",
]

BATCH_SIZE = 20  # épisodes par appel Claude en pass 1


# ── Utilitaires ────────────────────────────────────────────────

def format_duration(seconds):
    """Formate une durée en h:mm ou 'X min'."""
    if not seconds:
        return ""
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def clean_description(text):
    """Retire la note auto-générée de la description."""
    return text.replace(
        "(This description was generated automatically, inaccuracies may happen in the process.)", ""
    ).strip()


def ep_stem(ep):
    """Extrait le stem (nom sans extension) du transcript_path."""
    tp = ep.get("transcript_path")
    return Path(tp).stem if tp else None


def load_metadata(feed_slug, stem):
    path = METADATA_DIR / feed_slug / f"{stem}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def load_article(feed_slug, stem):
    path = ARTICLES_DIR / feed_slug / f"{stem}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return None


def article_to_html(text):
    """Convertit un article texte brut en HTML avec paragraphes et italiques."""
    paragraphs = text.split("\n\n")
    parts = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        p = esc(p)
        p = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', p)
        p = p.replace("\n", "<br>")
        parts.append(f"<p>{p}</p>")
    return "\n".join(parts)


def load_catalog():
    """Charge le catalogue et retourne un dict slug → feed_data."""
    with open(CATALOG_PATH) as f:
        raw = json.load(f)
    catalog = {}
    for key, fdata in raw.items():
        slug = fdata.get("slug", key.replace("_", "-"))
        catalog[slug] = fdata
    return catalog


def collect_episodes(catalog):
    """Collecte tous les épisodes des séries cibles avec article existant.

    Retourne une liste de dicts avec : stem, feed_slug, title, speaker,
    duration_seconds, url, description_short, description_long, keywords,
    difficulty, article_preview (premiers 500 mots).
    """
    episodes = []
    for slug in TARGET_FEEDS:
        fdata = catalog.get(slug)
        if not fdata:
            continue
        for season in fdata.get("seasons", []):
            for ep in season.get("episodes", []):
                stem = ep_stem(ep)
                if not stem:
                    continue
                article = load_article(slug, stem)
                if not article:
                    continue
                meta = load_metadata(slug, stem)
                title = meta.get("title_clean", ep.get("title", "Untitled"))
                desc_short = clean_description(
                    meta.get("description_short") or ep.get("description_short", "")
                )
                desc_long = clean_description(
                    meta.get("description_long") or ep.get("description_long", "")
                )
                keywords = meta.get("keywords", [])
                difficulty = meta.get("difficulty", "")

                # Premiers 500 mots de l'article
                words = article.split()
                preview = " ".join(words[:500])

                episodes.append({
                    "stem": stem,
                    "feed_slug": slug,
                    "title": title,
                    "speaker": ep.get("speaker", ""),
                    "duration_seconds": ep.get("duration_seconds", 0),
                    "url": ep.get("url", ""),
                    "description_short": desc_short,
                    "description_long": desc_long,
                    "keywords": keywords,
                    "difficulty": difficulty,
                    "article_preview": preview,
                })
    return episodes


def claude_call(client, system_prompt, user_prompt, max_tokens=4096):
    """Appel Claude avec retry automatique."""
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return resp.content[0].text
        except anthropic.RateLimitError:
            wait = 30 * (attempt + 1)
            print(f"    ⏳ Rate limit, attente {wait}s...")
            time.sleep(wait)
        except Exception as e:
            if attempt < 2:
                print(f"    ⚠ Erreur ({e}), retry dans 10s...")
                time.sleep(10)
            else:
                raise
    raise RuntimeError("Échec après 3 tentatives")


def extract_json_from_response(text):
    """Extrait le premier bloc JSON d'une réponse Claude."""
    # Chercher un bloc ```json ... ```
    m = re.search(r'```json\s*\n(.*?)\n```', text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # Chercher un bloc ``` ... ```
    m = re.search(r'```\s*\n(.*?)\n```', text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # Essayer de parser directement
    # Trouver le premier [ ou {
    for i, c in enumerate(text):
        if c in '[{':
            # Trouver la fin correspondante
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Pas de JSON trouvé dans la réponse:\n{text[:500]}")


# ══════════════════════════════════════════════════════════════
# PASS 1 : ANALYZE
# ══════════════════════════════════════════════════════════════

def pass_analyze():
    """Classifie thématiquement chaque article via Claude par lots de 20."""
    print("=" * 60)
    print("PASS 1 : ANALYZE — Classification thématique")
    print("=" * 60)

    catalog = load_catalog()
    episodes = collect_episodes(catalog)
    print(f"\n  {len(episodes)} articles trouvés dans {', '.join(TARGET_FEEDS)}")

    # Charger résultats existants pour reprise
    existing = {}
    if ANALYSIS_PATH.exists():
        with open(ANALYSIS_PATH) as f:
            existing = json.load(f)
        print(f"  {len(existing)} analyses existantes chargées (reprise)")

    # Filtrer les épisodes déjà analysés
    to_analyze = [ep for ep in episodes if ep["stem"] not in existing]
    print(f"  {len(to_analyze)} articles restants à analyser\n")

    if not to_analyze:
        print("  ✓ Tous les articles sont déjà analysés.")
        return

    client = anthropic.Anthropic()
    themes_list = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(THEMES))

    system_prompt = f"""You are a scholar of Theravāda Buddhism, specializing in the Mahasi tradition.
You analyze dharma talks and classify them by theme, quality, and difficulty.

CONTROLLED THEME VOCABULARY (use EXACTLY these strings):
{themes_list}

For each episode, return:
- primary_theme: the main theme (one of the themes above, exact string)
- secondary_themes: 2-3 additional themes from the list
- richness_score: 1-5 (storytelling quality — anecdotes, examples, personal stories)
- completeness_score: 1-5 (how thoroughly the topic is covered)
- difficulty: "introductory" / "intermediate" / "advanced"
- summary: 1-sentence summary of the talk's unique angle (max 40 words)

Return ONLY a JSON array with one object per episode, in the same order.
Each object must have a "stem" field matching the input stem."""

    # Traiter par lots
    results = dict(existing)
    total_batches = (len(to_analyze) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx in range(0, len(to_analyze), BATCH_SIZE):
        batch = to_analyze[batch_idx:batch_idx + BATCH_SIZE]
        batch_num = batch_idx // BATCH_SIZE + 1
        print(f"  Lot {batch_num}/{total_batches} ({len(batch)} épisodes)...")

        # Construire le prompt avec les métadonnées + preview
        episodes_text = []
        for ep in batch:
            kw = ", ".join(ep["keywords"][:10]) if ep["keywords"] else "none"
            episodes_text.append(
                f"---\nSTEM: {ep['stem']}\n"
                f"TITLE: {ep['title']}\n"
                f"FEED: {ep['feed_slug']}\n"
                f"DESCRIPTION: {ep['description_short']}\n"
                f"LONG DESCRIPTION: {ep['description_long'][:500]}\n"
                f"KEYWORDS: {kw}\n"
                f"EXISTING DIFFICULTY: {ep['difficulty']}\n"
                f"FIRST 500 WORDS:\n{ep['article_preview']}\n"
            )

        user_prompt = (
            f"Classify these {len(batch)} dharma talks:\n\n"
            + "\n".join(episodes_text)
        )

        raw = claude_call(client, system_prompt, user_prompt, max_tokens=8192)
        try:
            batch_results = extract_json_from_response(raw)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"    ⚠ Erreur JSON lot {batch_num}: {e}")
            print(f"    Réponse brute (500 premiers chars): {raw[:500]}")
            continue

        # Intégrer les résultats
        for item in batch_results:
            stem = item.get("stem")
            if stem:
                results[stem] = {
                    "primary_theme": item.get("primary_theme", ""),
                    "secondary_themes": item.get("secondary_themes", []),
                    "richness_score": item.get("richness_score", 3),
                    "completeness_score": item.get("completeness_score", 3),
                    "difficulty": item.get("difficulty", "intermediate"),
                    "summary": item.get("summary", ""),
                }

        # Sauvegarder après chaque lot (checkpoint)
        with open(ANALYSIS_PATH, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"    ✓ {len(batch_results)} analyses, total {len(results)}")

    print(f"\n{'=' * 60}")
    print(f"  ✓ ANALYSE TERMINÉE : {len(results)} articles classifiés")
    print(f"  Sauvegardé dans {ANALYSIS_PATH.name}")
    print(f"{'=' * 60}")


# ══════════════════════════════════════════════════════════════
# PASS 2 : SELECT
# ══════════════════════════════════════════════════════════════

def jaccard_similarity(set_a, set_b):
    """Calcule la similarité de Jaccard entre deux ensembles."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def pass_select():
    """Sélectionne les 80-120 meilleurs talks en éliminant les doublons."""
    print("=" * 60)
    print("PASS 2 : SELECT — Sélection des meilleurs talks")
    print("=" * 60)

    # Charger les données
    if not ANALYSIS_PATH.exists():
        print("  ✗ book_analysis.json introuvable. Exécuter d'abord : python build_bhante_book.py analyze")
        sys.exit(1)

    with open(ANALYSIS_PATH) as f:
        analysis = json.load(f)

    catalog = load_catalog()
    episodes = collect_episodes(catalog)
    print(f"\n  {len(episodes)} articles, {len(analysis)} analyses chargées")

    # Enrichir chaque épisode avec son analyse
    analyzed_eps = []
    for ep in episodes:
        a = analysis.get(ep["stem"])
        if not a:
            continue
        ep["analysis"] = a
        analyzed_eps.append(ep)

    print(f"  {len(analyzed_eps)} épisodes avec analyse")

    # ── Étape 2a : Clustering par thème + similarité keywords ──
    print("\n  Étape 2a : Clustering par thème principal...")
    theme_groups = {}
    for ep in analyzed_eps:
        theme = ep["analysis"]["primary_theme"]
        theme_groups.setdefault(theme, []).append(ep)

    # Statistiques
    print(f"  {len(theme_groups)} thèmes distincts :")
    for theme in THEMES:
        group = theme_groups.get(theme, [])
        if group:
            print(f"    {theme}: {len(group)} épisodes")

    # Détecter les paires avec forte similarité de keywords
    overlap_pairs = []
    for theme, group in theme_groups.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            kw_i = set(k.lower() for k in group[i].get("keywords", []))
            for j in range(i + 1, len(group)):
                kw_j = set(k.lower() for k in group[j].get("keywords", []))
                sim = jaccard_similarity(kw_i, kw_j)
                if sim > 0.4:
                    overlap_pairs.append((
                        group[i]["stem"], group[j]["stem"],
                        theme, sim
                    ))

    print(f"  {len(overlap_pairs)} paires avec >40% chevauchement de keywords")

    # ── Étape 2b : Sélection Claude par cluster ──
    print("\n  Étape 2b : Sélection Claude par cluster thématique...")

    client = anthropic.Anthropic()
    selected_stems = set()
    selection_rationale = {}

    # Déterminer les quotas par thème
    # Objectif : 80-120 talks, ~25 thèmes → ~4 par thème en moyenne
    # Thèmes majeurs (>8 épisodes) : 4-6 talks
    # Thèmes moyens (4-8) : 2-4 talks
    # Thèmes mineurs (<4) : 1-2 talks (garder tout si <= 2)
    total_target = 100  # milieu de 80-120

    theme_quotas = {}
    for theme, group in theme_groups.items():
        n = len(group)
        if n <= 2:
            theme_quotas[theme] = n  # garder tout
        elif n <= 5:
            theme_quotas[theme] = min(n, 3)
        elif n <= 10:
            theme_quotas[theme] = min(n, 4)
        elif n <= 20:
            theme_quotas[theme] = min(n, 5)
        else:
            theme_quotas[theme] = min(n, 6)

    projected = sum(theme_quotas.values())
    print(f"  Quotas projetés : {projected} talks (cible : {total_target})")

    # Ajuster les quotas si nécessaire
    if projected < 80:
        # Augmenter les quotas des grands groupes
        for theme in sorted(theme_groups, key=lambda t: len(theme_groups[t]), reverse=True):
            if projected >= 80:
                break
            n = len(theme_groups[theme])
            if theme_quotas[theme] < n:
                theme_quotas[theme] += 1
                projected += 1
    elif projected > 120:
        # Réduire les quotas des grands groupes
        for theme in sorted(theme_groups, key=lambda t: len(theme_groups[t]), reverse=True):
            if projected <= 120:
                break
            if theme_quotas[theme] > 2:
                theme_quotas[theme] -= 1
                projected -= 1

    system_prompt = """You are a senior editor curating a book of dharma talks by Bhante Bodhidhamma.
Your goal is to select the best talks for inclusion, prioritizing:
1. COMPLETENESS: Thorough, well-developed coverage of the topic
2. ENGAGEMENT: Rich storytelling, anecdotes, personal examples
3. UNIQUENESS: Each selected talk should offer a distinct angle
4. ACCESSIBILITY: Prefer talks that stand alone without requiring other context

When comparing similar talks, prefer the one that:
- Covers the topic most thoroughly (completeness_score)
- Has the most engaging delivery (richness_score)
- Offers the most unique perspective

Return a JSON array of objects, each with:
- "stem": the episode stem
- "rationale": 1-sentence justification for inclusion (max 30 words)"""

    call_count = 0
    for theme in THEMES:
        group = theme_groups.get(theme, [])
        if not group:
            continue
        quota = theme_quotas.get(theme, 2)

        if len(group) <= quota:
            # Garder tous — pas besoin de Claude
            for ep in group:
                selected_stems.add(ep["stem"])
                selection_rationale[ep["stem"]] = {
                    "rationale": f"Included: only {len(group)} talk(s) on this theme",
                    "theme": theme,
                }
            continue

        # Envoyer à Claude pour sélection
        print(f"  📋 {theme} : {len(group)} épisodes → sélection de {quota}...")

        # Préparer les données pour Claude
        eps_data = []
        for ep in group:
            a = ep["analysis"]
            # Premiers + derniers 300 mots de l'article
            article = load_article(ep["feed_slug"], ep["stem"])
            if article:
                words = article.split()
                first_300 = " ".join(words[:300])
                last_300 = " ".join(words[-300:]) if len(words) > 600 else ""
            else:
                first_300 = ""
                last_300 = ""

            eps_data.append(
                f"---\nSTEM: {ep['stem']}\n"
                f"TITLE: {ep['title']}\n"
                f"FEED: {ep['feed_slug']}\n"
                f"SUMMARY: {a['summary']}\n"
                f"RICHNESS: {a['richness_score']}/5\n"
                f"COMPLETENESS: {a['completeness_score']}/5\n"
                f"DIFFICULTY: {a['difficulty']}\n"
                f"FIRST 300 WORDS:\n{first_300}\n"
                + (f"LAST 300 WORDS:\n{last_300}\n" if last_300 else "")
            )

        user_prompt = (
            f"THEME: {theme}\n"
            f"SELECT THE BEST {quota} TALKS from the {len(group)} below.\n\n"
            + "\n".join(eps_data)
        )

        raw = claude_call(client, system_prompt, user_prompt, max_tokens=4096)
        call_count += 1

        try:
            selections = extract_json_from_response(raw)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"    ⚠ Erreur JSON pour {theme}: {e}")
            # Fallback : prendre les N meilleurs par score
            scored = sorted(group, key=lambda ep: (
                ep["analysis"]["completeness_score"],
                ep["analysis"]["richness_score"]
            ), reverse=True)
            selections = [{"stem": ep["stem"], "rationale": "Selected by score fallback"} for ep in scored[:quota]]

        for sel in selections[:quota]:
            stem = sel.get("stem", "")
            if stem:
                selected_stems.add(stem)
                selection_rationale[stem] = {
                    "rationale": sel.get("rationale", ""),
                    "theme": theme,
                }

    print(f"\n  {call_count} appels Claude effectués")
    print(f"  {len(selected_stems)} talks sélectionnés")

    # ── Étape 2c : Ajustement final ──
    if len(selected_stems) < 80:
        print(f"\n  ⚠ Seulement {len(selected_stems)} sélectionnés, ajout de talks supplémentaires...")
        # Ajouter par score décroissant
        remaining = [ep for ep in analyzed_eps if ep["stem"] not in selected_stems]
        remaining.sort(key=lambda ep: (
            ep["analysis"]["completeness_score"] + ep["analysis"]["richness_score"]
        ), reverse=True)
        for ep in remaining:
            if len(selected_stems) >= 90:
                break
            selected_stems.add(ep["stem"])
            selection_rationale[ep["stem"]] = {
                "rationale": "Added to meet minimum quota (high quality score)",
                "theme": ep["analysis"]["primary_theme"],
            }

    # Sauvegarder
    result = {
        "total_analyzed": len(analyzed_eps),
        "total_selected": len(selected_stems),
        "theme_quotas": theme_quotas,
        "selections": []
    }
    for ep in analyzed_eps:
        if ep["stem"] in selected_stems:
            info = selection_rationale.get(ep["stem"], {})
            result["selections"].append({
                "stem": ep["stem"],
                "feed_slug": ep["feed_slug"],
                "title": ep["title"],
                "theme": info.get("theme", ep["analysis"]["primary_theme"]),
                "rationale": info.get("rationale", ""),
                "richness_score": ep["analysis"]["richness_score"],
                "completeness_score": ep["analysis"]["completeness_score"],
                "difficulty": ep["analysis"]["difficulty"],
                "summary": ep["analysis"]["summary"],
                "duration_seconds": ep["duration_seconds"],
            })

    with open(SELECTION_PATH, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 60}")
    print(f"  ✓ SÉLECTION TERMINÉE : {len(selected_stems)} talks")
    print(f"  Sauvegardé dans {SELECTION_PATH.name}")
    print(f"{'=' * 60}")


# ══════════════════════════════════════════════════════════════
# PASS 3 : ORGANIZE
# ══════════════════════════════════════════════════════════════

def pass_organize():
    """Organise les talks sélectionnés en Parties et Chapitres."""
    print("=" * 60)
    print("PASS 3 : ORGANIZE — Structuration du livre")
    print("=" * 60)

    if not SELECTION_PATH.exists():
        print("  ✗ book_selection.json introuvable. Exécuter d'abord : python build_bhante_book.py select")
        sys.exit(1)

    with open(SELECTION_PATH) as f:
        selection = json.load(f)
    with open(ANALYSIS_PATH) as f:
        analysis = json.load(f)

    talks = selection["selections"]
    print(f"\n  {len(talks)} talks à organiser")

    client = anthropic.Anthropic()

    system_prompt = """You are a senior book editor organizing a collection of Buddhist dharma talks
into a beautifully structured book that reads as a coherent spiritual journey.

OPENING: The very first chapter of the book (Part I, Chapter 1) MUST be a talk about the
Buddha's own spiritual journey — how his quest led him from dissatisfaction through jhāna
practice and asceticism to the discovery of vipassanā and nibbāna. This sets the tone for
the entire book. Look for talks with titles like "Why Meditate" or about the Buddha's life
and awakening story.

STRUCTURE: Organize into 7-9 Parts following the natural arc of the Buddhist path:

Part I:    The Buddha and His Message — Start with the Buddha's journey, then faith,
           approaching the dhamma. This is the reader's gateway into the teachings.
Part II:   The Nature of Existence — Four Noble Truths, the Three Characteristics
           (anicca, dukkha, anattā), dependent origination. The philosophical foundation.
Part III:  The Path of Ethics — Right Speech, Right Action, Right Livelihood, the Precepts,
           sīla. The moral foundation for practice.
Part IV:   The Path of Meditation — Right Effort, Right Mindfulness, Right Concentration,
           jhāna, the five hindrances. How to meditate.
Part V:    Vipassanā in Practice — Mahasi method, Satipatthana (body, feeling, mind, dhammas),
           practical meditation guidance. The heart of the practice.
Part VI:   The Heart Practices — Mettā, the four Brahma Vihāras, compassion. The emotional
           dimension of the path.
Part VII:  Wisdom in Daily Life — Kamma, rebirth, Right View, Right Intention, integrating
           practice into everyday life. Bringing it all together.
Part VIII: The Goal — Advanced insight, stages of awakening, death, nibbāna. Where the path
           leads.

CRITICAL RULES:
- BALANCE: Each Part must have 10-18 chapters. No Part should exceed 18 chapters.
  If a Part would be too large, split it. If too small, merge with a related Part.
- INTERNAL ORDER: Within each Part, arrange chapters as a TEACHING PROGRESSION:
  start with foundational/introductory talks, then build toward more nuanced/advanced ones.
  The reader should feel guided deeper into each topic as they read through a Part.
- COMPLETENESS: Every single talk must appear exactly once. Do not omit any.
- Part titles: short and evocative (2-6 words)
- Epigraphs: a Pāli quote with English translation, or a thematic phrase (1 line)

Return a JSON object using the short IDs (T001, T002, etc.) as "stem" values:
{
  "parts": [
    {
      "number": 1,
      "title": "Part title",
      "epigraph": "Pāli phrase — English translation",
      "chapters": [
        {"stem": "T001", "title": "Talk title"}
      ]
    }
  ]
}"""

    # Injecter "Why Meditate?" si absent (ouverture idéale du livre)
    selected_stems_set = {t["stem"] for t in talks}
    why_meditate_stem = "S01E01_Why_Meditate"
    if why_meditate_stem not in selected_stems_set:
        catalog = load_catalog()
        all_eps = collect_episodes(catalog)
        for ep in all_eps:
            if ep["stem"] == why_meditate_stem:
                a = analysis.get(ep["stem"], {})
                talks.append({
                    "stem": ep["stem"],
                    "feed_slug": ep["feed_slug"],
                    "title": ep["title"],
                    "theme": a.get("primary_theme", "The Mahasi Method & Vipassana Practice"),
                    "rationale": "Injected as ideal book opening — the Buddha's spiritual journey",
                    "richness_score": a.get("richness_score", 4),
                    "completeness_score": a.get("completeness_score", 4),
                    "difficulty": a.get("difficulty", "introductory"),
                    "summary": a.get("summary", ""),
                    "duration_seconds": ep["duration_seconds"],
                })
                print(f"  + Ajouté 'Why Meditate?' comme ouverture du livre")
                break

    # Utiliser des IDs courts pour éviter les stems trop longs que Claude tronque
    id_to_stem = {}
    stem_to_id = {}
    for i, t in enumerate(talks):
        short_id = f"T{i+1:03d}"
        id_to_stem[short_id] = t["stem"]
        stem_to_id[t["stem"]] = short_id

    # Préparer la liste des talks avec IDs courts
    talks_text = []
    for t in talks:
        short_id = stem_to_id[t["stem"]]
        secondary = ""
        a = analysis.get(t["stem"], {})
        if a:
            secondary = ", ".join(a.get("secondary_themes", [])[:2])
        talks_text.append(
            f"- ID: {short_id}\n"
            f"  TITLE: {t['title']}\n"
            f"  PRIMARY THEME: {t['theme']}\n"
            f"  SECONDARY THEMES: {secondary}\n"
            f"  DIFFICULTY: {t['difficulty']}\n"
            f"  SUMMARY: {t['summary']}"
        )

    why_meditate_id = stem_to_id.get(why_meditate_stem, "")
    user_prompt = (
        f"Organize these {len(talks)} dharma talks by Bhante Bodhidhamma into "
        f"a beautifully structured book with 7-9 Parts.\n\n"
        f"IMPORTANT: Use the short ID (T001, T002, ...) in your response, not the full stem.\n"
        f"The FIRST chapter of Part I MUST be ID {why_meditate_id} ('Why Meditate?') — "
        f"it tells how the Buddha's spiritual quest led to the discovery of vipassanā.\n"
        f"Balance all Parts to 10-18 chapters each. Part VI (Heart Practices) should include "
        f"mettā AND related ethical/relational topics if needed to reach 8+ chapters.\n\n"
        + "\n".join(talks_text)
    )

    print("  Envoi à Claude pour structuration...")
    raw = claude_call(client, system_prompt, user_prompt, max_tokens=16000)

    try:
        structure = extract_json_from_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"  ✗ Erreur JSON: {e}")
        print(f"  Réponse brute (500 premiers chars): {raw[:500]}")
        sys.exit(1)

    # Convertir les IDs courts en stems réels
    talk_lookup = {t["stem"]: t for t in talks}
    for part in structure.get("parts", []):
        for ch in part.get("chapters", []):
            raw_id = ch.get("stem", ch.get("id", ""))
            # Résoudre l'ID court → stem réel
            if raw_id in id_to_stem:
                ch["stem"] = id_to_stem[raw_id]
            elif raw_id in stem_to_id:
                pass  # déjà un stem réel
            else:
                # Essayer de matcher par titre
                for t in talks:
                    if t["title"] == ch.get("title", ""):
                        ch["stem"] = t["stem"]
                        break

    # Valider que tous les stems sont présents
    selected_stems = {t["stem"] for t in talks}
    organized_stems = set()
    for part in structure.get("parts", []):
        for ch in part.get("chapters", []):
            organized_stems.add(ch.get("stem", ""))

    # Retirer les stems invalides
    for part in structure.get("parts", []):
        part["chapters"] = [ch for ch in part["chapters"] if ch.get("stem") in selected_stems]

    # Ajouter les manquants intelligemment (par thème)
    missing = selected_stems - organized_stems
    if missing:
        print(f"  ⚠ {len(missing)} talks manquants, placement par thème...")
        for stem in missing:
            t = talk_lookup.get(stem, {})
            theme = t.get("theme", "")
            title = t.get("title", stem)
            # Trouver la partie la plus appropriée
            best_part = structure["parts"][-1]  # fallback
            for part in structure["parts"]:
                # Heuristique : regarder les thèmes des chapitres existants
                part_themes = set()
                for ch in part["chapters"]:
                    pt = talk_lookup.get(ch["stem"], {})
                    part_themes.add(pt.get("theme", ""))
                if theme in part_themes:
                    best_part = part
                    break
            best_part["chapters"].append({"stem": stem, "title": title})
            print(f"    → '{title}' → Part {best_part['number']}")

    extra = organized_stems - selected_stems
    if extra:
        print(f"  ⚠ {len(extra)} stems inconnus retirés")

    # Sauvegarder
    with open(STRUCTURE_PATH, "w") as f:
        json.dump(structure, f, indent=2, ensure_ascii=False)

    # Statistiques
    total_chapters = sum(len(p["chapters"]) for p in structure["parts"])
    print(f"\n  Structure du livre :")
    for part in structure["parts"]:
        n = len(part["chapters"])
        print(f"    Part {part['number']}: {part['title']} ({n} chapitres)")

    print(f"\n{'=' * 60}")
    print(f"  ✓ ORGANISATION TERMINÉE : {len(structure['parts'])} parties, {total_chapters} chapitres")
    print(f"  Sauvegardé dans {STRUCTURE_PATH.name}")
    print(f"{'=' * 60}")


# ══════════════════════════════════════════════════════════════
# PASS 4 : GENERATE
# ══════════════════════════════════════════════════════════════

# ── QR Code ────────────────────────────────────────────────

def make_qr_data_uri(url, kind="svg"):
    """Génère un data URI pour un QR code pointant vers url."""
    import segno
    qr = segno.make(url)
    buf = io.BytesIO()
    if kind == "svg":
        qr.save(buf, kind='svg', scale=2, border=1)
        mime = "image/svg+xml"
    else:
        qr.save(buf, kind='png', scale=3, border=1)
        mime = "image/png"
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:{mime};base64,{b64}"


# ── CSS PDF Premium ───────────────────────────────────────

PDF_CSS = """\
@page {
    size: A4;
    margin: 28mm 35mm 30mm 30mm;
    @bottom-center {
        content: counter(page);
        font-family: "Noto Sans", sans-serif;
        font-size: 8pt;
        color: #999;
    }
    @top-left {
        content: string(part-title);
        font-family: "Noto Serif", serif;
        font-size: 7.5pt;
        font-style: italic;
        color: #aaa;
        letter-spacing: 0.03em;
    }
    @top-right {
        content: string(chapter-title);
        font-family: "Noto Serif", serif;
        font-size: 7.5pt;
        font-style: italic;
        color: #aaa;
        letter-spacing: 0.03em;
    }
}
@page :first {
    margin: 0;
    @bottom-center { content: none; }
    @top-left { content: none; }
    @top-right { content: none; }
}
@page cover-page {
    margin: 0;
    @bottom-center { content: none; }
    @top-left { content: none; }
    @top-right { content: none; }
}
@page :blank {
    @bottom-center { content: none; }
    @top-left { content: none; }
    @top-right { content: none; }
}
@page part-page {
    @top-left { content: none; }
    @top-right { content: none; }
    @bottom-center { content: none; }
}
@page chapter-first {
    @top-left { content: none; }
    @top-right { content: none; }
}
@page front-matter {
    @top-left { content: none; }
    @top-right { content: none; }
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: "Noto Serif", Georgia, serif;
    font-size: 10.5pt;
    line-height: 1.65;
    color: #1a1a1a;
    text-align: justify;
    hyphens: auto;
    -webkit-hyphens: auto;
    orphans: 3;
    widows: 3;
}

/* ── Running strings ────────────────────────── */
.part-title-string {
    string-set: part-title content();
    font-size: 0; height: 0; margin: 0; padding: 0;
    visibility: hidden;
}
.chapter-title-string {
    string-set: chapter-title content();
    font-size: 0; height: 0; margin: 0; padding: 0;
    visibility: hidden;
}

/* ── Couverture ──────────────────────────────── */
.cover {
    page: cover-page;
    page-break-after: always;
    width: 210mm; height: 297mm;
    display: flex; flex-direction: column;
    justify-content: center; align-items: center;
    text-align: center;
    background: #faf9f7;
    padding: 40mm 30mm;
    position: relative;
}
.cover::before {
    content: ""; position: absolute;
    top: 25mm; left: 30mm; right: 30mm;
    height: 0.6pt; background: #B8860B;
}
.cover::after {
    content: ""; position: absolute;
    bottom: 25mm; left: 30mm; right: 30mm;
    height: 0.6pt; background: #B8860B;
}
.cover-series {
    font-family: "Noto Sans", sans-serif;
    font-size: 9pt;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: #B8860B;
    margin-bottom: 8mm;
}
.cover-title {
    font-family: "Noto Serif", serif;
    font-size: 28pt;
    font-weight: 700;
    line-height: 1.2;
    color: #1a1a1a;
    margin-bottom: 6mm;
    letter-spacing: -0.01em;
    hyphens: none;
}
.cover-subtitle {
    font-family: "Noto Serif", serif;
    font-size: 11pt;
    font-style: italic;
    color: #666;
    max-width: 120mm;
    line-height: 1.6;
    margin-bottom: 15mm;
}
.cover-author {
    font-family: "Noto Sans", sans-serif;
    font-size: 13pt;
    font-weight: 600;
    color: #333;
    letter-spacing: 0.05em;
    margin-bottom: 4mm;
}
.cover-retreat {
    font-family: "Noto Sans", sans-serif;
    font-size: 8.5pt;
    color: #999;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}

/* ── Half-title ──────────────────────────────── */
.half-title {
    page: front-matter;
    page-break-after: always;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    text-align: center;
    min-height: 200mm;
}
.half-title h1 {
    font-family: "Noto Serif", serif;
    font-size: 20pt;
    font-weight: 700;
    color: #1a1a1a;
}

/* ── About the Teacher ───────────────────────── */
.about-teacher {
    page: front-matter;
    page-break-after: always;
    padding-top: 30mm;
}
.about-teacher h2 {
    font-family: "Noto Serif", serif;
    font-size: 16pt;
    font-weight: 700;
    margin-bottom: 8mm;
    color: #1a1a1a;
}
.about-teacher p {
    font-size: 10.5pt;
    line-height: 1.7;
    margin-bottom: 4mm;
    color: #333;
}

/* ── Table des matières ──────────────────────── */
.toc-page {
    page: front-matter;
    page-break-after: always;
    padding-top: 15mm;
}
.toc-page h2 {
    font-family: "Noto Serif", serif;
    font-size: 18pt;
    font-weight: 700;
    margin-bottom: 10mm;
    color: #1a1a1a;
}
.toc-part {
    font-family: "Noto Sans", sans-serif;
    font-size: 8pt;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.15em;
    color: #B8860B;
    margin-top: 6mm;
    margin-bottom: 2mm;
    padding-bottom: 1.5mm;
    border-bottom: 0.4pt solid #e0d8cf;
}
.toc-entry {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    padding: 1.2mm 0;
    border-bottom: 0.2pt dotted #ddd;
    text-decoration: none;
    color: #1a1a1a;
}
.toc-entry:last-child { border-bottom: none; }
.toc-title {
    font-family: "Noto Serif", serif;
    font-size: 9.5pt;
    flex: 1;
    padding-right: 3mm;
}
.toc-entry::after {
    content: target-counter(attr(href url), page);
    font-family: "Noto Sans", sans-serif;
    font-size: 8pt;
    color: #999;
    white-space: nowrap;
    min-width: 8mm;
    text-align: right;
}

/* ── Part title pages ────────────────────────── */
.part-title-page {
    page: part-page;
    page-break-before: always;
    page-break-after: always;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    text-align: center;
    min-height: 200mm;
}
.part-number {
    font-family: "Noto Sans", sans-serif;
    font-size: 9pt;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: #B8860B;
    margin-bottom: 6mm;
}
.part-title {
    font-family: "Noto Serif", serif;
    font-size: 24pt;
    font-weight: 700;
    line-height: 1.25;
    color: #1a1a1a;
    margin-bottom: 10mm;
    hyphens: none;
}
.part-ornament {
    font-size: 14pt;
    color: #B8860B;
    margin-bottom: 10mm;
    letter-spacing: 0.5em;
}
.part-epigraph {
    font-family: "Noto Serif", serif;
    font-size: 10pt;
    font-style: italic;
    color: #777;
    max-width: 100mm;
    line-height: 1.6;
}

/* ── Chapitres ───────────────────────────────── */
.chapter {
    page-break-before: always;
    page: chapter-first;
}
.chapter-header {
    margin-bottom: 8mm;
    padding-bottom: 5mm;
    border-bottom: 0.5pt solid #B8860B;
}
.chapter-number {
    font-family: "Noto Sans", sans-serif;
    font-size: 8pt;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.15em;
    color: #B8860B;
    margin-bottom: 2mm;
}
.chapter-title-display {
    font-family: "Noto Serif", serif;
    font-size: 18pt;
    font-weight: 700;
    line-height: 1.25;
    color: #1a1a1a;
    margin-bottom: 2mm;
    letter-spacing: -0.01em;
}
.chapter-meta {
    font-family: "Noto Sans", sans-serif;
    font-size: 8pt;
    color: #999;
}

/* Source box with QR code */
.source-box {
    margin-bottom: 6mm;
    padding: 3mm 4mm;
    border: 0.3pt solid #ddd;
    border-radius: 2mm;
    font-family: "Noto Sans", sans-serif;
    font-size: 7.5pt;
    color: #888;
    line-height: 1.5;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.source-box .qr-code {
    width: 14mm;
    height: 14mm;
    flex-shrink: 0;
    margin-left: 3mm;
}
.source-box .source-text {
    flex: 1;
}

/* Accroche (description) */
.chapter-lead {
    font-family: "Noto Serif", serif;
    font-size: 10pt;
    font-style: italic;
    color: #555;
    line-height: 1.7;
    margin-bottom: 6mm;
    padding-left: 4mm;
    border-left: 2pt solid #e0d8cf;
}
.chapter-lead p { margin-bottom: 3mm; }
.chapter-lead p:last-child { margin-bottom: 0; }

/* Corps du transcript */
.chapter-body {
    font-size: 10.5pt;
    line-height: 1.65;
}
.chapter-body p {
    margin-bottom: 3.5mm;
    text-indent: 0;
}
.chapter-body p + p {
    text-indent: 5mm;
}
.chapter-body p:first-child {
    text-indent: 0;
}
.chapter-body em {
    font-style: italic;
}

/* Drop cap — initial-letter preferred, float fallback */
.chapter-body .drop-cap::first-letter {
    font-family: "Noto Serif", serif;
    font-size: 3.2em;
    font-weight: 700;
    line-height: 1;
    margin-right: 2mm;
    color: #B8860B;
    initial-letter: 3;
}

/* ── Glossaire ───────────────────────────────── */
.glossary {
    page-break-before: always;
    page: front-matter;
}
.glossary h2 {
    font-family: "Noto Serif", serif;
    font-size: 18pt;
    font-weight: 700;
    margin-bottom: 8mm;
    color: #1a1a1a;
}
.glossary-entry {
    margin-bottom: 2mm;
    font-size: 9.5pt;
    line-height: 1.5;
}
.glossary-term {
    font-weight: 700;
    font-style: italic;
    color: #333;
}

/* ── Colophon ────────────────────────────────── */
.colophon {
    page-break-before: always;
    page: front-matter;
    padding-top: 60mm;
    text-align: center;
}
.colophon p {
    font-family: "Noto Sans", sans-serif;
    font-size: 8pt;
    color: #999;
    line-height: 1.8;
}
.colophon .retreat-name {
    font-family: "Noto Serif", serif;
    font-size: 12pt;
    color: #333;
    margin-bottom: 3mm;
    font-weight: 600;
}
.colophon .colophon-rule {
    width: 30mm; height: 0.4pt;
    background: #B8860B;
    margin: 8mm auto;
}
"""


# ── CSS EPUB ──────────────────────────────────────────────

EPUB_CSS = """\
body {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1em;
    line-height: 1.7;
    color: #1a1a1a;
    margin: 0;
    padding: 0;
}
h1 {
    font-size: 1.6em;
    font-weight: 700;
    line-height: 1.25;
    margin: 0 0 0.3em;
    color: #1a1a1a;
}
h2 {
    font-size: 1.3em;
    font-weight: 700;
    line-height: 1.3;
    margin: 1.5em 0 0.5em;
    color: #1a1a1a;
}
.part-header {
    text-align: center;
    margin: 3em 0;
    page-break-after: always;
}
.part-header .part-number {
    font-size: 0.8em;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: #B8860B;
    margin-bottom: 0.5em;
}
.part-header .part-title {
    font-size: 1.5em;
    font-weight: 700;
    margin-bottom: 1em;
}
.part-header .part-ornament {
    color: #B8860B;
    font-size: 1.2em;
    letter-spacing: 0.5em;
    margin-bottom: 1em;
}
.part-header .part-epigraph {
    font-style: italic;
    color: #777;
    font-size: 0.9em;
}
.chapter-meta {
    font-size: 0.8em;
    color: #999;
    margin-bottom: 1em;
}
.source-box {
    border: 1px solid #ddd;
    border-radius: 4px;
    padding: 0.5em;
    margin-bottom: 1em;
    font-size: 0.8em;
    color: #888;
    overflow: hidden;
}
.source-box .qr-code {
    float: right;
    width: 60px;
    height: 60px;
    margin-left: 0.5em;
}
.chapter-lead {
    font-style: italic;
    color: #555;
    border-left: 3px solid #e0d8cf;
    padding-left: 1em;
    margin-bottom: 1.5em;
    line-height: 1.7;
}
.chapter-lead p { margin-bottom: 0.5em; }
.chapter-body p {
    margin-bottom: 0.8em;
    text-align: justify;
}
.chapter-body em { font-style: italic; }
.glossary-entry {
    margin-bottom: 0.3em;
    font-size: 0.95em;
}
.glossary-term {
    font-weight: bold;
    font-style: italic;
}
.colophon {
    text-align: center;
    margin-top: 3em;
    color: #999;
    font-size: 0.85em;
    line-height: 1.8;
}
"""


# ── Glossaire Pali ────────────────────────────────────────

# Termes Pali courants avec définitions courtes
PALI_GLOSSARY = {
    "anicca": "Impermanence; the characteristic that all conditioned phenomena are transient",
    "anattā": "Not-self; the characteristic that no permanent, unchanging self can be found",
    "dukkha": "Suffering, unsatisfactoriness; the inherent stress of conditioned existence",
    "nibbāna": "The unconditioned; the cessation of suffering and the goal of the Buddhist path",
    "sati": "Mindfulness; present-moment awareness",
    "sammā": "Right, correct, proper; prefix used in the Noble Eightfold Path",
    "sammā sati": "Right Mindfulness; the seventh factor of the Noble Eightfold Path",
    "sammā samādhi": "Right Concentration; the eighth factor of the Noble Eightfold Path",
    "sammā diṭṭhi": "Right View; the first factor of the Noble Eightfold Path",
    "vipassanā": "Insight meditation; direct observation of the three characteristics",
    "samatha": "Calm, tranquility meditation; concentration practice",
    "jhāna": "Absorption; deep states of meditative concentration",
    "sīla": "Moral conduct, virtue, ethics",
    "mettā": "Loving-kindness; goodwill toward all beings",
    "karuṇā": "Compassion; the wish for beings to be free from suffering",
    "muditā": "Sympathetic joy; rejoicing in others' happiness",
    "upekkhā": "Equanimity; balanced, impartial awareness",
    "brahma vihāra": "Divine abode; the four sublime states (mettā, karuṇā, muditā, upekkhā)",
    "kamma": "Action, especially intentional action; the law of cause and effect",
    "saṃsāra": "The cycle of rebirth; the round of existence",
    "tilakkhana": "The three characteristics: anicca, dukkha, anattā",
    "satipaṭṭhāna": "Foundation of mindfulness; the four fields of mindful observation",
    "paṭiccasamuppāda": "Dependent origination; the twelve-link chain of causation",
    "nīvaraṇa": "Hindrance; the five mental obstructions to meditation",
    "kāmacchanda": "Sensual desire; the first of the five hindrances",
    "byāpāda": "Ill-will, aversion; the second hindrance",
    "thīna-middha": "Sloth and torpor; the third hindrance",
    "uddhacca-kukkucca": "Restlessness and worry; the fourth hindrance",
    "vicikicchā": "Skeptical doubt; the fifth hindrance",
    "vedanā": "Feeling tone; pleasant, unpleasant, or neutral quality of experience",
    "saṅkhāra": "Mental formations, volitional activities; conditioned phenomena",
    "viññāṇa": "Consciousness; awareness of sense objects",
    "nāma-rūpa": "Mind and matter; mentality-materiality",
    "magga": "Path; specifically the Noble Eightfold Path",
    "phala": "Fruit; the result of path attainment",
    "ariya": "Noble; one who has attained a stage of awakening",
    "sotāpanna": "Stream-enterer; the first stage of awakening",
    "sakadāgāmī": "Once-returner; the second stage of awakening",
    "anāgāmī": "Non-returner; the third stage of awakening",
    "arahant": "Fully awakened one; one who has completed the path",
    "dhamma": "Teaching, truth, natural law; the Buddha's doctrine",
    "saṅgha": "Community; the assembly of noble disciples",
    "pañcasīla": "The five precepts; the basic ethical training rules",
    "bhāvanā": "Mental cultivation, meditation",
    "paññā": "Wisdom; direct insight into the nature of reality",
    "cetanā": "Intention, volition; the mental factor of will",
    "taṇhā": "Craving, thirst; the origin of suffering",
    "upādāna": "Clinging, attachment; grasping at the five aggregates",
    "khandha": "Aggregate; the five components of psychophysical existence",
    "āyatana": "Sense base; the six internal and external bases of perception",
}


def build_glossary_from_keywords(selection_path, analysis_path):
    """Construit un glossaire à partir des keywords des articles sélectionnés."""
    with open(selection_path) as f:
        selection = json.load(f)
    with open(analysis_path) as f:
        analysis = json.load(f)

    # Collecter tous les keywords des articles sélectionnés
    all_keywords = set()
    catalog = load_catalog()
    for sel in selection["selections"]:
        meta = load_metadata(sel["feed_slug"], sel["stem"])
        for kw in meta.get("keywords", []):
            all_keywords.add(kw.strip())

    # Matcher contre le glossaire Pali
    matched = {}
    for term, definition in PALI_GLOSSARY.items():
        # Chercher des correspondances dans les keywords
        term_lower = term.lower().replace("ā", "a").replace("ī", "i").replace("ū", "u").replace("ṭ", "t").replace("ṇ", "n").replace("ṃ", "m").replace("ñ", "n").replace("ḍ", "d")
        for kw in all_keywords:
            kw_lower = kw.lower().replace("ā", "a").replace("ī", "i").replace("ū", "u").replace("ṭ", "t").replace("ṇ", "n").replace("ṃ", "m").replace("ñ", "n").replace("ḍ", "d")
            if term_lower in kw_lower or kw_lower in term_lower:
                matched[term] = definition
                break

    # Ajouter tous les termes du glossaire (c'est un livre de dhamma, tout est pertinent)
    return dict(sorted(PALI_GLOSSARY.items(), key=lambda x: x[0].lower()))


def pass_generate():
    """Génère le PDF et l'EPUB du livre."""
    print("=" * 60)
    print("PASS 4 : GENERATE — Production PDF + EPUB")
    print("=" * 60)

    # Vérifier les dépendances
    try:
        from weasyprint import HTML
    except ImportError:
        print("  ✗ WeasyPrint non disponible. Utiliser :")
        print("    conda run -n newspapers python build_bhante_book.py generate")
        sys.exit(1)

    try:
        from ebooklib import epub
    except ImportError:
        print("  ✗ ebooklib non disponible. Installer : pip install ebooklib")
        sys.exit(1)

    try:
        import segno
    except ImportError:
        print("  ✗ segno non disponible. Installer : pip install segno")
        sys.exit(1)

    # Charger les données
    if not STRUCTURE_PATH.exists():
        print("  ✗ book_structure.json introuvable. Exécuter d'abord : python build_bhante_book.py organize")
        sys.exit(1)

    with open(STRUCTURE_PATH) as f:
        structure = json.load(f)
    with open(SELECTION_PATH) as f:
        selection = json.load(f)
    with open(ANALYSIS_PATH) as f:
        analysis = json.load(f)

    catalog = load_catalog()

    # Construire un index stem → episode data
    ep_index = {}
    for slug in TARGET_FEEDS:
        fdata = catalog.get(slug)
        if not fdata:
            continue
        for season in fdata.get("seasons", []):
            for ep in season.get("episodes", []):
                stem = ep_stem(ep)
                if stem:
                    ep_index[stem] = {**ep, "feed_slug": slug}

    sel_index = {s["stem"]: s for s in selection["selections"]}

    BOOKS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Générer le PDF ──
    print("\n  Génération du PDF...")
    pdf_path = BOOKS_DIR / "bhante-bodhidhamma.pdf"
    n_pdf = _build_pdf(structure, ep_index, sel_index, analysis, pdf_path)
    if n_pdf:
        size_mb = pdf_path.stat().st_size / (1024 * 1024)
        print(f"  ✓ PDF : {n_pdf} chapitres, {size_mb:.1f} MB → {pdf_path}")

    # ── Générer l'EPUB ──
    print("\n  Génération de l'EPUB...")
    epub_path = BOOKS_DIR / "bhante-bodhidhamma.epub"
    n_epub = _build_epub(structure, ep_index, sel_index, analysis, epub_path)
    if n_epub:
        size_mb = epub_path.stat().st_size / (1024 * 1024)
        print(f"  ✓ EPUB : {n_epub} chapitres, {size_mb:.1f} MB → {epub_path}")

    print(f"\n{'=' * 60}")
    print(f"  ✓ GÉNÉRATION TERMINÉE")
    print(f"{'=' * 60}")


def _build_pdf(structure, ep_index, sel_index, analysis, output_path):
    """Construit le PDF premium avec WeasyPrint."""
    from weasyprint import HTML

    book_title = "The Teachings of Bhante Bodhidhamma"
    book_subtitle = "Selected Talks on Vipassana, the Noble Eightfold Path, and the Way to Liberation"

    # ── Couverture ──
    cover_html = f"""
    <div class="cover">
        <div class="cover-series">Satipanya Buddhist Retreat</div>
        <div class="cover-title">{esc(book_title)}</div>
        <div class="cover-subtitle">{esc(book_subtitle)}</div>
        <div class="cover-author">Bhante Bodhidhamma</div>
        <div class="cover-retreat">Shropshire · United Kingdom</div>
    </div>
    """

    # ── Half-title ──
    half_title_html = f"""
    <div class="half-title">
        <h1>{esc(book_title)}</h1>
    </div>
    """

    # ── About the Teacher ──
    about_html = """
    <div class="about-teacher">
        <h2>About the Teacher</h2>
        <p>Bhante Bodhidhamma is a Theravāda Buddhist monk and the founder of Satipanya
        Buddhist Retreat in Shropshire, United Kingdom. Ordained in the Burmese tradition,
        he trained extensively in the Mahasi Sayadaw method of vipassanā meditation.</p>
        <p>For over three decades, Bhante has taught insight meditation, offering retreats,
        courses, and weekly talks at Satipanya and internationally. His teaching style
        combines rigorous adherence to the classical Theravāda framework with warmth,
        humour, and a deep understanding of the challenges facing modern practitioners.</p>
        <p>The talks collected in this volume span his entire teaching career, from
        foundational explanations of the Noble Eightfold Path to subtle explorations
        of advanced insight practice. They represent his commitment to making the
        Buddha's path of liberation accessible to all sincere seekers.</p>
    </div>
    """

    # ── Table des matières ──
    toc_html = '<div class="toc-page"><h2>Contents</h2>\n'
    global_ch = 0
    for part in structure["parts"]:
        toc_html += f'<div class="toc-part">Part {part["number"]} — {esc(part["title"])}</div>\n'
        for ch in part["chapters"]:
            title = ch.get("title", ch["stem"])
            toc_html += f"""
            <a class="toc-entry" href="#ch-{global_ch}">
                <span class="toc-title">{esc(title)}</span>
            </a>\n"""
            global_ch += 1
    toc_html += "</div>\n"

    # ── Parties et chapitres ──
    body_html = ""
    global_ch = 0
    total_chapters = 0

    for part in structure["parts"]:
        # Page de titre de partie
        epigraph = esc(part.get("epigraph", ""))
        body_html += f"""
        <div class="part-title-page">
            <div class="part-number">Part {part["number"]}</div>
            <div class="part-title">{esc(part["title"])}</div>
            <div class="part-ornament">◆ ◆ ◆</div>
            <div class="part-epigraph">{epigraph}</div>
        </div>
        <span class="part-title-string">Part {part["number"]} — {esc(part["title"])}</span>
        """

        for ch_data in part["chapters"]:
            stem = ch_data["stem"]
            ep = ep_index.get(stem, {})
            sel = sel_index.get(stem, {})
            slug = ep.get("feed_slug", "")

            title = ch_data.get("title", ep.get("title", stem))
            speaker = ep.get("speaker", "Bhante Bodhidhamma")
            dur = format_duration(ep.get("duration_seconds", 0))
            url = ep.get("url", "")

            # Charger l'article
            article = load_article(slug, stem) if slug else None
            if not article:
                continue

            meta = load_metadata(slug, stem) if slug else {}
            desc = clean_description(
                meta.get("description_long") or ep.get("description_long", "")
            )

            # Source box avec QR code PNG
            source_html = ""
            if url:
                qr_uri = make_qr_data_uri(url, kind="png")
                source_text = f"Originally given as <em>{esc(title)}</em> at Satipanya Buddhist Retreat"
                source_html = f"""
                <div class="source-box">
                    <div class="source-text">{source_text}</div>
                    <img class="qr-code" src="{qr_uri}" alt="QR">
                </div>
                """

            # Accroche
            lead_html = ""
            if desc:
                desc_paras = [f"<p>{esc(p.strip())}</p>"
                              for p in desc.split("\n\n") if p.strip()]
                lead_html = f'<div class="chapter-lead">{"".join(desc_paras)}</div>'

            # Corps avec drop cap
            body = article_to_html(article)
            # Appliquer drop-cap au premier paragraphe
            body = body.replace("<p>", '<p class="drop-cap">', 1)

            global_ch_num = global_ch + 1
            body_html += f"""
            <section class="chapter" id="ch-{global_ch}">
                <span class="chapter-title-string">{esc(title)}</span>
                <div class="chapter-header">
                    <div class="chapter-number">Chapter {global_ch_num}</div>
                    <div class="chapter-title-display">{esc(title)}</div>
                    <div class="chapter-meta">{esc(speaker)} · {dur}</div>
                </div>
                {source_html}
                {lead_html}
                <div class="chapter-body">
                    {body}
                </div>
            </section>
            """
            global_ch += 1
            total_chapters += 1

    # ── Glossaire ──
    glossary = build_glossary_from_keywords(SELECTION_PATH, ANALYSIS_PATH)
    glossary_html = '<div class="glossary"><h2>Glossary of Pāli Terms</h2>\n'
    for term, definition in glossary.items():
        glossary_html += f"""
        <div class="glossary-entry">
            <span class="glossary-term">{esc(term)}</span> — {esc(definition)}
        </div>
        """
    glossary_html += "</div>\n"

    # ── Colophon ──
    colophon_html = f"""
    <div class="colophon">
        <div class="retreat-name">Satipanya Buddhist Retreat</div>
        <p>{esc(book_title)}</p>
        <p>{esc(book_subtitle)}</p>
        <div class="colophon-rule"></div>
        <p>{total_chapters} selected talks · Bhante Bodhidhamma</p>
        <p style="margin-top: 5mm;">
            Transcriptions produced locally using Swiss low-carbon electricity.<br>
            Analysis and curation by AI. Editing and review by cloud-hosted AI.
        </p>
        <p style="margin-top: 5mm;">
            <a href="https://www.satipanya.org.uk">satipanya.org.uk</a>
        </p>
    </div>
    """

    # ── Assemblage final ──
    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><style>{PDF_CSS}</style></head>
<body>
{cover_html}
{half_title_html}
{about_html}
{toc_html}
{body_html}
{glossary_html}
{colophon_html}
</body></html>"""

    HTML(string=full_html).write_pdf(str(output_path))
    return total_chapters


def _build_epub(structure, ep_index, sel_index, analysis, output_path):
    """Construit l'EPUB."""
    from ebooklib import epub

    book_title = "The Teachings of Bhante Bodhidhamma"
    book_subtitle = "Selected Talks on Vipassana, the Noble Eightfold Path, and the Way to Liberation"

    book = epub.EpubBook()
    book.set_identifier("satipanya-bhante-bodhidhamma-curated")
    book.set_title(f"{book_title}")
    book.set_language("en")
    book.add_author("Bhante Bodhidhamma")
    book.add_metadata("DC", "publisher", "Satipanya Buddhist Retreat")
    book.add_metadata("DC", "description", book_subtitle)

    # CSS
    style = epub.EpubItem(
        uid="style", file_name="style/default.css",
        media_type="text/css", content=EPUB_CSS.encode()
    )
    book.add_item(style)

    # Collecter les QR codes comme images EPUB
    qr_images = {}

    spine = ["nav"]
    toc_entries = []
    total_chapters = 0
    global_ch = 0

    for part in structure["parts"]:
        # Page de titre de partie
        part_ch = epub.EpubHtml(
            title=f"Part {part['number']} — {part['title']}",
            file_name=f"part{part['number']:02d}.xhtml",
            lang="en",
        )
        epigraph = esc(part.get("epigraph", ""))
        part_ch.content = f"""<html><head></head><body>
<div class="part-header">
    <div class="part-number">Part {part['number']}</div>
    <div class="part-title">{esc(part['title'])}</div>
    <div class="part-ornament">◆ ◆ ◆</div>
    <div class="part-epigraph">{epigraph}</div>
</div>
</body></html>"""
        part_ch.add_item(style)
        book.add_item(part_ch)
        spine.append(part_ch)

        part_chapters = []

        for ch_data in part["chapters"]:
            stem = ch_data["stem"]
            ep = ep_index.get(stem, {})
            slug = ep.get("feed_slug", "")

            title = ch_data.get("title", ep.get("title", stem))
            speaker = ep.get("speaker", "Bhante Bodhidhamma")
            dur = format_duration(ep.get("duration_seconds", 0))
            url = ep.get("url", "")

            article = load_article(slug, stem) if slug else None
            if not article:
                continue

            meta = load_metadata(slug, stem) if slug else {}
            desc = clean_description(
                meta.get("description_long") or ep.get("description_long", "")
            )

            # QR code comme image PNG pour EPUB
            qr_html = ""
            if url:
                qr_fname = f"images/qr_{global_ch:03d}.png"
                try:
                    import segno
                    qr = segno.make(url)
                    buf = io.BytesIO()
                    qr.save(buf, kind='png', scale=3, border=1)
                    qr_data = buf.getvalue()

                    qr_img = epub.EpubItem(
                        uid=f"qr_{global_ch}",
                        file_name=qr_fname,
                        media_type="image/png",
                        content=qr_data,
                    )
                    book.add_item(qr_img)
                    qr_html = f'<img class="qr-code" src="{qr_fname}" alt="QR code">'
                except Exception:
                    pass

            # Source box
            source_html = ""
            if url:
                source_text = f"Originally given as <em>{esc(title)}</em> at Satipanya Buddhist Retreat"
                source_html = f"""
                <div class="source-box">
                    {qr_html}
                    {source_text}
                </div>
                """

            # Accroche
            lead_html = ""
            if desc:
                desc_paras = "".join(
                    f"<p>{esc(p.strip())}</p>"
                    for p in desc.split("\n\n") if p.strip()
                )
                lead_html = f'<div class="chapter-lead">{desc_paras}</div>'

            body_html = article_to_html(article)

            ch = epub.EpubHtml(
                title=title,
                file_name=f"ch{global_ch:03d}.xhtml",
                lang="en",
            )
            ch.content = f"""<html><head></head><body>
<h1>{esc(title)}</h1>
<div class="chapter-meta">{esc(speaker)} · {dur}</div>
{source_html}
{lead_html}
<div class="chapter-body">{body_html}</div>
</body></html>"""
            ch.add_item(style)
            book.add_item(ch)
            part_chapters.append(ch)
            spine.append(ch)
            global_ch += 1
            total_chapters += 1

        # Ajouter au TOC sous cette partie
        if part_chapters:
            section = epub.Section(f"Part {part['number']} — {part['title']}")
            toc_entries.append((section, [part_ch] + part_chapters))

    # ── Glossaire ──
    glossary = build_glossary_from_keywords(SELECTION_PATH, ANALYSIS_PATH)
    glossary_items = []
    for term, definition in glossary.items():
        glossary_items.append(
            f'<div class="glossary-entry">'
            f'<span class="glossary-term">{esc(term)}</span> — {esc(definition)}'
            f'</div>'
        )

    glossary_ch = epub.EpubHtml(
        title="Glossary of Pāli Terms",
        file_name="glossary.xhtml",
        lang="en",
    )
    glossary_ch.content = f"""<html><head></head><body>
<h1>Glossary of Pāli Terms</h1>
{"".join(glossary_items)}
</body></html>"""
    glossary_ch.add_item(style)
    book.add_item(glossary_ch)
    spine.append(glossary_ch)

    # ── Colophon ──
    colophon = epub.EpubHtml(title="About", file_name="colophon.xhtml", lang="en")
    colophon.content = f"""<html><head></head><body>
<div class="colophon">
<p><strong>Satipanya Buddhist Retreat</strong></p>
<p>{esc(book_title)}</p>
<p>{esc(book_subtitle)}</p>
<p>{total_chapters} selected talks · Bhante Bodhidhamma</p>
<p>Transcriptions produced locally using Swiss low-carbon electricity.
Analysis and curation by AI. Editing and review by cloud-hosted AI.</p>
<p><a href="https://www.satipanya.org.uk">satipanya.org.uk</a></p>
</div></body></html>"""
    colophon.add_item(style)
    book.add_item(colophon)
    spine.append(colophon)

    # Finaliser
    toc_entries.append(glossary_ch)
    toc_entries.append(colophon)
    book.toc = toc_entries
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    epub.write_epub(str(output_path), book, {})
    return total_chapters


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

PASSES = {
    "analyze": pass_analyze,
    "select": pass_select,
    "organize": pass_organize,
    "generate": pass_generate,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in PASSES:
        print("Usage : python build_bhante_book.py <pass>")
        print(f"Passes disponibles : {', '.join(PASSES.keys())}")
        print()
        print("  analyze  — Classification thématique (Claude API)")
        print("  select   — Sélection des 80-120 meilleurs talks (Claude API)")
        print("  organize — Structuration en Parties/Chapitres (Claude API)")
        print("  generate — Production PDF + EPUB (WeasyPrint + ebooklib)")
        sys.exit(1)

    PASSES[sys.argv[1]]()


if __name__ == "__main__":
    main()
