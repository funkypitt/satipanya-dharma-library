#!/usr/bin/env python3
"""
refine_articles.py — Raffine les articles du livre qui n'ont pas encore de
chapitre Opus (refined_heavier.json).

Pour chaque stem du book_selection qui manque de chapitre Opus-raffiné :
  1. Crée le dossier chapters/<slug>/ avec base.json
  2. Fabrique composition.json à partir de l'article existant (articles/)
  3. Lance les passes 4c (prune), 6 (polish léger), 7 (polish éditorial)

Usage :
    conda run -n interview python refine_articles.py
    conda run -n interview python refine_articles.py --dry-run
    conda run -n interview python refine_articles.py --start-from 5
    conda run -n interview python refine_articles.py --selection book_selection_v3.json
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# On importe les fonctions de chapter.py
sys.path.insert(0, str(Path(__file__).parent))
from chapter import (
    pass4c_prune, pass6_refine, pass7_heavier_refine,
    pass5_render_docx, pass5b_render_preprint,
    pass6b_render_refined, pass7b_render_heavier,
    lookup_source_url, slugify,
    CHAPTERS_DIR, ARTICLES_DIR,
)

try:
    import anthropic
except ImportError:
    print("❌ anthropic non installé.", file=sys.stderr)
    sys.exit(1)


PROJECT_DIR = Path(__file__).parent
DEFAULT_SELECTION = PROJECT_DIR / "book_selection_v3.json"


def load_article(feed_slug: str, stem: str) -> str | None:
    """Charge l'article brut depuis articles/."""
    # feed_slug dans la sélection est "dharma-talks" etc.
    # mais les articles sont rangés par collection du catalogue
    # Essayer plusieurs variantes
    for slug in [feed_slug]:
        path = ARTICLES_DIR / slug / f"{stem}.txt"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    # Chercher dans tous les sous-dossiers
    for path in ARTICLES_DIR.glob(f"*/{stem}.txt"):
        return path.read_text(encoding="utf-8").strip()
    return None


def article_to_composition(article_text: str, title: str, theme: str,
                           source_id: str) -> dict:
    """Convertit un article texte brut en format composition.json."""
    paragraphs = [p.strip() for p in article_text.split("\n\n") if p.strip()]
    segments = [{"source": "base", "text": p} for p in paragraphs]
    total_chars = sum(len(p) for p in paragraphs)
    return {
        "theme": theme,
        "title": title,
        "subtitle": "",
        "epigraph": "",
        "base_source_id": source_id,
        "segments": segments,
        "stats": {"total_chars": total_chars},
    }


def build_chapter_index() -> dict[str, Path]:
    """Construit un index stem → dossier chapitre ayant refined_heavier.json."""
    index = {}
    if not CHAPTERS_DIR.exists():
        return index
    for base_json in CHAPTERS_DIR.glob("*/base.json"):
        rh = base_json.parent / "refined_heavier.json"
        if not rh.exists():
            continue
        with open(base_json) as f:
            data = json.load(f)
        source_id = data.get("source_id", "")
        if "/" in source_id:
            stem = source_id.split("/", 1)[1]
            index[stem] = base_json.parent
    return index


def find_collection_for_stem(stem: str, catalog_path: Path) -> str | None:
    """Trouve la collection (feed_slug) pour un stem donné."""
    with open(catalog_path) as f:
        catalog = json.load(f)
    for slug, feed_data in catalog.items():
        for season in feed_data.get("seasons", []):
            for ep in season.get("episodes", []):
                ep_stem = ep.get("stem") or Path(ep.get("audio_file", "")).stem
                if ep_stem == stem:
                    return slug
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Raffine les articles manquants via Opus (passes 4c/6/7).")
    parser.add_argument("--selection", default=str(DEFAULT_SELECTION),
                        help=f"Fichier de sélection (def: {DEFAULT_SELECTION.name})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche les articles à traiter sans lancer Opus.")
    parser.add_argument("--start-from", type=int, default=1,
                        help="Numéro (1-indexed) à partir duquel reprendre.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Nombre max d'articles à traiter (0 = tous).")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY") and not args.dry_run:
        print("❌ ANTHROPIC_API_KEY non défini", file=sys.stderr)
        sys.exit(1)

    # Charger la sélection
    sel_path = Path(args.selection)
    with open(sel_path) as f:
        selection = json.load(f)
    all_stems = selection["selections"]
    print(f"📚 {len(all_stems)} sélections dans {sel_path.name}")

    # Charger l'index des chapitres existants
    existing = build_chapter_index()
    print(f"📖 {len(existing)} chapitres Opus-raffinés existants")

    # Identifier les manquants
    missing = []
    for sel in all_stems:
        stem = sel["stem"]
        if stem not in existing:
            missing.append(sel)

    print(f"🔧 {len(missing)} articles à raffiner\n")

    if not missing:
        print("✅ Tous les articles ont déjà un chapitre Opus-raffiné !")
        return

    # Appliquer start-from et limit
    start_idx = args.start_from - 1
    to_process = missing[start_idx:]
    if args.limit > 0:
        to_process = to_process[:args.limit]

    if args.dry_run:
        print(f"DRY RUN — {len(to_process)} articles à traiter :\n")
        for i, sel in enumerate(to_process, start=start_idx + 1):
            print(f"  [{i}/{len(missing)}] {sel['title']}  ({sel['stem']})")
        return

    catalog_path = PROJECT_DIR / "catalog.json"
    client = anthropic.Anthropic()

    successes = []
    failures = []

    for i, sel in enumerate(to_process, start=start_idx + 1):
        stem = sel["stem"]
        title = sel["title"]
        theme = sel.get("theme", title)
        feed_slug = sel.get("feed_slug", "")
        source_id = f"{feed_slug}/{stem}" if feed_slug else stem

        print(f"\n{'='*68}")
        print(f"  [{i}/{len(missing)}] {title}")
        print(f"  stem: {stem}")
        print(f"{'='*68}")

        t0 = time.time()
        try:
            # 1. Charger l'article
            # Trouver la vraie collection dans le catalogue
            real_collection = find_collection_for_stem(stem, catalog_path)
            if not real_collection:
                real_collection = feed_slug
            article = load_article(real_collection, stem)
            if not article:
                print(f"  ⚠ Article introuvable pour {stem} — skip")
                failures.append((title, "article introuvable"))
                continue

            # 2. Créer le dossier chapitre
            slug = slugify(title)
            out_dir = CHAPTERS_DIR / slug
            out_dir.mkdir(parents=True, exist_ok=True)

            # 3. base.json
            base_path = out_dir / "base.json"
            if not base_path.exists():
                # Construire le source_id correct
                full_source_id = f"{real_collection}/{stem}"
                base = {
                    "source_id": full_source_id,
                    "title": title,
                    "collection": real_collection,
                    "content_type": "audio",
                    "justification": f"Selected for Book v3 — theme: {theme}",
                    "summary": sel.get("summary", ""),
                }
                with open(base_path, "w") as f:
                    json.dump(base, f, indent=2, ensure_ascii=False)
                print(f"  ✓ base.json créé")
            else:
                with open(base_path) as f:
                    base = json.load(f)
                full_source_id = base["source_id"]
                print(f"  ✓ base.json existant")

            # 4. composition.json (à partir de l'article)
            comp_path = out_dir / "composition.json"
            if not comp_path.exists():
                comp = article_to_composition(article, title, theme,
                                              full_source_id)
                with open(comp_path, "w") as f:
                    json.dump(comp, f, indent=2, ensure_ascii=False)
                n_segs = len(comp["segments"])
                n_chars = comp["stats"]["total_chars"]
                print(f"  ✓ composition.json créé ({n_segs} segments, {n_chars} chars)")
            else:
                with open(comp_path) as f:
                    comp = json.load(f)
                print(f"  ✓ composition.json existant")

            # 5. Passes Opus
            pruned_path = out_dir / "pruned.json"
            refined_path = out_dir / "refined.json"
            heavier_path = out_dir / "refined_heavier.json"

            source_url = lookup_source_url(full_source_id)

            pruned = pass4c_prune(client, comp, theme, pruned_path)
            refined = pass6_refine(client, pruned, theme, refined_path)
            heavier = pass7_heavier_refine(client, refined, theme, heavier_path)

            # 6. Render DOCX (optionnel mais utile)
            docx_path = out_dir / "chapter.docx"
            preprint_path = out_dir / "chapter-preprint.docx"
            refined_docx_path = out_dir / "chapter-preprint-refined.docx"
            heavier_docx_path = out_dir / "chapter-preprint-refined-heavier.docx"

            pass5_render_docx(pruned, base, theme, docx_path,
                              source_url=source_url)
            pass5b_render_preprint(pruned, base, theme, preprint_path,
                                   source_url=source_url)
            pass6b_render_refined(refined, theme, refined_docx_path,
                                  source_title=title, source_url=source_url)
            pass7b_render_heavier(heavier, theme, heavier_docx_path,
                                  source_title=title, source_url=source_url)

            elapsed = time.time() - t0
            print(f"  ✅ Terminé en {elapsed/60:.1f} min")
            successes.append(title)

        except Exception as e:
            elapsed = time.time() - t0
            print(f"  ❌ Échec après {elapsed/60:.1f} min : {e}")
            failures.append((title, str(e)))

    # Résumé
    print(f"\n{'='*68}")
    print(f"  RÉSUMÉ")
    print(f"{'='*68}")
    print(f"  ✅ Réussis : {len(successes)}/{len(to_process)}")
    if failures:
        print(f"  ❌ Échoués : {len(failures)}")
        for title, reason in failures:
            print(f"     - {title}: {reason}")
    print(f"\n  Total chapitres Opus : {len(existing) + len(successes)}")


if __name__ == "__main__":
    main()
