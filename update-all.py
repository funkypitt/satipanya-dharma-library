#!/usr/bin/env python3
"""
update-all.py — Met à jour la bibliothèque Satipanya avec les nouveaux enregistrements.

Lance le pipeline complet en séquence :
  1. catalog   — re-scrape le site, fusionne les épisodes existants
  2. probe     — récupère durée/taille des nouveaux épisodes
  3. transcribe — WhisperX sur les nouveaux épisodes (GPU)
  4. describe  — descriptions Claude pour les nouveaux épisodes
  5. feeds     — régénère les 6 flux RSS
  6. beautify  — embellissement des transcripts (nouveaux seulement)
  7. site      — régénère le site statique + PDFs

Chaque passe est incrémentale : elle saute les épisodes déjà traités.

Usage:
    python update-all.py              # tout
    python update-all.py --no-gpu     # saute transcription (pas de GPU)
    python update-all.py --quick      # catalog + probe + feeds + site seulement
"""

import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
PYTHON = sys.executable


def run(label, cmd):
    """Lance une commande et affiche le résultat."""
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}\n")
    t0 = time.time()
    result = subprocess.run(cmd, cwd=str(PROJECT_DIR))
    elapsed = time.time() - t0
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    if result.returncode != 0:
        print(f"\n  ✗ {label} failed (exit code {result.returncode})")
        sys.exit(result.returncode)
    print(f"\n  ✓ {label} done ({minutes}m{seconds:02d}s)")


def main():
    args = set(sys.argv[1:])
    quick = "--quick" in args
    no_gpu = "--no-gpu" in args

    print("╔══════════════════════════════════════════════════════════╗")
    print("║       Satipanya Dharma Library — Full Update            ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # Passes toujours exécutées
    run("Pass 1: Scraping catalog", [PYTHON, "podcastify.py", "catalog"])
    run("Pass 2: Probing new files", [PYTHON, "podcastify.py", "probe"])

    if not quick:
        if not no_gpu:
            run("Pass 3: Transcribing (WhisperX)", [PYTHON, "podcastify.py", "transcribe"])
        else:
            print("\n  ⏭ Skipping transcription (--no-gpu)")

        run("Pass 4: Generating descriptions", [PYTHON, "podcastify.py", "describe"])

    run("Pass 5: Generating feeds", [PYTHON, "podcastify.py", "feeds"])

    if not quick:
        run("Pass 6: Beautifying transcripts", [PYTHON, "podcastify.py", "beautify"])

    run("Pass 7: Building website + PDFs", [PYTHON, "build_site.py"])

    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║                    Update complete                      ║")
    print("╚══════════════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
