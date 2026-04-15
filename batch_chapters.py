#!/usr/bin/env python3
"""
batch_chapters.py — Lance chapter.py séquentiellement pour 31 thèmes.

Usage :
    conda run -n interview python batch_chapters.py
    conda run -n interview python batch_chapters.py --dry-run
    conda run -n interview python batch_chapters.py --start-from 5
"""

import subprocess
import sys
import time
from pathlib import Path

THEMES = [
    "The Story of Angulimala",
    "Bhante's Own Story",
    "The Spiritual Faculties",
    "Not-Self and the Question of Identity",
    "The Buddha's Awakening",
    "The Spiral Path",
    "Forgiveness",
    "Right Livelihood",
    "Right Relationship",
    "Spiritual Friendship and Community",
    "Equanimity",
    "Samatha and Vipassana",
    "The Contemplation of Death",
    "Is Awareness Enough?",
    "The Pleasure Syndrome",
    "Feeling: The Turning Point",
    "Free Will",
    "Buddhism and Western Thought",
    "The Discourse to the Kalamas",
    "Ritual in Spiritual Practice",
    # batch 2
    "The Four Noble Truths",
    "The Five Hindrances",
    "The Three Characteristics",
    "Dependent Origination",
    "Kamma and Rebirth",
    "Mindfulness of Breathing",
    "Metta — Cultivating Loving-Kindness",
    "The Life of the Buddha",
    "Fear and Difficult Emotions",
    "Faith on the Buddhist Path",
    "Dharma in Daily Life",
    # batch 3
    "Compassion and Empathy",
    "Joy and Appreciative Gladness",
    "Gratitude, Generosity and Renunciation",
    "The Perfections",
    "Desire, Craving and Letting Go",
    "Wisdom and Intuitive Intelligence",
    "Ethics, Virtue and Moral Conduct",
    "Taking Refuge",
    "The Factors of Awakening",
    "The Body in Meditation",
    # batch 4 — golden nuggets
    "Nibbāna and Liberation",
    "The Two Darts",
    "Postmodernism and Buddhism",
    "Dukkha",
    "Why the Buddha Did Not Return to Lay Life",
    "There Is No Reason for Being",
    "The End of Guilt",
    "Love, Desire and Distraction",
    "Happiness",
    # batch 5 — missing doctrinal chapters for Book A
    "The Noble Eightfold Path",
    "The Four Foundations of Mindfulness",
    "The Five Precepts",
    "Nibbana and the Cessation of Suffering",
    "Neoliberalism and the Dharma",
]

CHAPTER_PY = Path(__file__).parent / "chapter.py"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Batch-run chapter.py for 20 themes.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche les commandes sans les exécuter.")
    parser.add_argument("--start-from", type=int, default=1,
                        help="Numéro du thème à partir duquel reprendre (1-indexed).")
    args = parser.parse_args()

    total = len(THEMES)
    start_idx = args.start_from - 1

    if start_idx < 0 or start_idx >= total:
        print(f"❌ --start-from doit être entre 1 et {total}", file=sys.stderr)
        sys.exit(1)

    themes_to_run = THEMES[start_idx:]
    print(f"📚 {len(themes_to_run)} chapitres à générer (sur {total} total)")
    print()

    successes = []
    failures = []

    for i, theme in enumerate(themes_to_run, start=start_idx + 1):
        print(f"{'='*60}")
        print(f"  [{i}/{total}] {theme}")
        print(f"{'='*60}")

        cmd = [
            sys.executable, str(CHAPTER_PY),
            "--theme", theme,
        ]

        if args.dry_run:
            print(f"  DRY RUN: {' '.join(cmd)}")
            print()
            continue

        t0 = time.time()
        result = subprocess.run(cmd)
        elapsed = time.time() - t0

        if result.returncode == 0:
            print(f"  ✅ Terminé en {elapsed/60:.1f} min")
            successes.append(theme)
        else:
            print(f"  ❌ Échec (code {result.returncode}) après {elapsed/60:.1f} min")
            failures.append(theme)

        print()

    # Résumé
    print(f"{'='*60}")
    print(f"  RÉSUMÉ")
    print(f"{'='*60}")
    print(f"  ✅ Réussis : {len(successes)}/{len(themes_to_run)}")
    if failures:
        print(f"  ❌ Échoués : {len(failures)}")
        for t in failures:
            print(f"     - {t}")


if __name__ == "__main__":
    main()
