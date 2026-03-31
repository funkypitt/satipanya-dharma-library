#!/usr/bin/env python3
"""Score la qualité littéraire de chaque talk via Claude et enregistre lite_score dans catalog.json.

Usage:
    python score_literary.py                  # Score tous les épisodes non encore notés
    python score_literary.py --collection dharma_talks   # Une seule collection
    python score_literary.py --dry-run        # Affiche ce qui serait scoré sans appeler l'API
    python score_literary.py --force          # Re-score même les épisodes déjà notés

Le script est résumable : il sauvegarde catalog.json après chaque épisode scoré.
"""

import json
import os
import re
import sys
import time
import argparse
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("ERREUR : pip install anthropic")
    sys.exit(1)

# ── Configuration ──────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent
CATALOG_PATH = PROJECT_DIR / "catalog.json"
ARTICLES_DIR = PROJECT_DIR / "articles"

CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TRANSCRIPT_CHARS = 80_000  # Tronquer les transcripts très longs (garde début + fin)

RUBRIC = """You are an expert literary critic evaluating the quality of a spoken dharma talk transcript.

Rate the LITERARY QUALITY of this talk on a scale from 0 to 100, based on these 7 equally-weighted criteria:

1. **Clarity & structure of argumentation** — Is the talk well-organized? Does it build logically?
2. **Use of metaphors, analogies, illustrations** — Does the speaker use vivid imagery to convey meaning?
3. **Vocabulary richness & precision** — Is the language varied and precise, or repetitive and vague?
4. **Rhetorical devices & oratory skill** — Are there effective rhetorical techniques (questions, contrasts, emphasis)?
5. **Storytelling & narrative engagement** — Does the talk draw the listener in? Are anecdotes used well?
6. **Depth of insight expressed through language** — Does the language convey genuine wisdom and understanding?
7. **Overall eloquence & natural flow** — Does it read well? Is the expression graceful?

IMPORTANT NOTES:
- This is a TRANSCRIPT of spoken language, so some disfluencies are normal. Focus on the underlying quality.
- Guided meditations with mostly repetitive instructions should score lower on literary merit.
- Pure chanting with minimal spoken content should score very low (10-20).
- A brilliant, well-structured dharma talk with vivid illustrations might score 80-95.
- An average talk with decent structure but unremarkable language might score 40-60.
- A rambling or poorly structured talk with flat language might score 20-40.

Respond with ONLY a JSON object, no other text:
{"score": <integer 0-100>, "reason": "<one sentence justification>"}"""


def parse_srt(srt_path: Path) -> str:
    """Extrait le texte brut d'un fichier SRT."""
    text = srt_path.read_text(encoding="utf-8", errors="replace")
    # Supprime numéros de séquence et timecodes
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\d+$", line):
            continue
        if re.match(r"\d{2}:\d{2}:\d{2}", line):
            continue
        lines.append(line)
    return " ".join(lines)


def truncate_text(text: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Tronque en gardant début + fin si trop long."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n\n[... transcript truncated ...]\n\n" + text[-half:]


def score_episode(client: anthropic.Anthropic, title: str, transcript_text: str) -> dict:
    """Appelle Claude pour scorer un épisode. Retourne {"score": int, "reason": str}."""
    user_msg = f"# Talk title: {title}\n\n# Transcript:\n\n{transcript_text}"

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=200,
        messages=[
            {"role": "user", "content": f"{RUBRIC}\n\n{user_msg}"}
        ],
    )

    raw = response.content[0].text.strip()
    # Extraire le JSON même s'il y a du texte autour
    match = re.search(r'\{[^}]+\}', raw)
    if match:
        result = json.loads(match.group())
        score = int(result.get("score", -1))
        reason = result.get("reason", "")
        if 0 <= score <= 100:
            return {"score": score, "reason": reason}

    print(f"  ⚠ Réponse inattendue de Claude : {raw[:200]}")
    return None


def main():
    parser = argparse.ArgumentParser(description="Score littéraire des talks via Claude")
    parser.add_argument("--collection", type=str, help="Scorer une seule collection (ex: dharma_talks)")
    parser.add_argument("--dry-run", action="store_true", help="Afficher sans appeler l'API")
    parser.add_argument("--force", action="store_true", help="Re-scorer les épisodes déjà notés")
    args = parser.parse_args()

    # Vérifier la clé API
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERREUR : ANTHROPIC_API_KEY non définie")
        sys.exit(1)

    client = None if args.dry_run else anthropic.Anthropic()

    # Charger le catalogue
    with open(CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)

    # Collecter les épisodes à scorer
    to_score = []
    for col_key, col in catalog.items():
        if args.collection and col_key != args.collection:
            continue
        slug = col.get("slug", col_key.replace("_", "-"))
        is_text = col.get("content_type") == "text"
        for season in col["seasons"]:
            for ep in season["episodes"]:
                if not args.force and "lite_score" in ep:
                    continue
                if is_text:
                    # Text episode: read from articles/<slug>/<stem>.txt
                    stem = ep.get("stem", "")
                    if not stem:
                        continue
                    article_path = ARTICLES_DIR / slug / f"{stem}.txt"
                    if not article_path.exists():
                        continue
                    to_score.append((col_key, season["number"], ep, article_path))
                else:
                    tp = ep.get("transcript_path", "")
                    srt_path = PROJECT_DIR / tp if tp else None
                    if not srt_path or not srt_path.exists():
                        continue
                    to_score.append((col_key, season["number"], ep, srt_path))

    print(f"Épisodes à scorer : {len(to_score)}")
    if not to_score:
        print("Rien à faire.")
        return

    if args.dry_run:
        for col_key, s_num, ep, srt_path in to_score:
            print(f"  {col_key} S{s_num:02d}E{ep['episode_number']:02d} — {ep['title']}")
        return

    # Scorer
    scored = 0
    errors = 0
    for i, (col_key, s_num, ep, text_path) in enumerate(to_score):
        label = f"[{i+1}/{len(to_score)}] {col_key} S{s_num:02d}E{ep['episode_number']:02d}"
        print(f"{label} — {ep['title']}...", end=" ", flush=True)

        try:
            if text_path.suffix == ".srt":
                text = parse_srt(text_path)
            else:
                text = text_path.read_text(encoding="utf-8", errors="replace")
            if len(text) < 50:
                print("⏭ transcript trop court")
                ep["lite_score"] = 5
                scored += 1
                continue

            text = truncate_text(text)
            result = score_episode(client, ep["title"], text)

            if result:
                ep["lite_score"] = result["score"]
                ep["lite_reason"] = result["reason"]
                print(f"✓ {result['score']}/100 — {result['reason'][:60]}")
                scored += 1
            else:
                print("✗ échec du scoring")
                errors += 1

        except Exception as e:
            print(f"✗ erreur: {e}")
            errors += 1

        # Sauvegarder après chaque épisode (résumable)
        with open(CATALOG_PATH, "w", encoding="utf-8") as f:
            json.dump(catalog, f, indent=2, ensure_ascii=False)

        # Petit délai pour rate limiting
        if i < len(to_score) - 1:
            time.sleep(0.3)

    print(f"\nTerminé : {scored} scorés, {errors} erreurs")
    print(f"Catalogue sauvegardé dans {CATALOG_PATH}")


if __name__ == "__main__":
    main()
