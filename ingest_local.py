#!/usr/bin/env python3
"""
ingest_local.py — Ingère les enregistrements MP3 locaux de retraite dans la Dharma Library.

Scanne local_mp3_files/, probe les fichiers, transcrit via WhisperX, et fusionne
dans catalog.json comme collection text-only (pas de distribution audio).

Pass 1: catalog    — Scan local_mp3_files/, parse filenames, build retreat_catalog.json
Pass 2: probe      — ffprobe local pour durée/taille
Pass 3: transcribe — WhisperX sur les fichiers locaux → SRT + TXT
Pass 4: merge      — Fusionne retreat_catalog.json dans catalog.json

Usage:
    python ingest_local.py catalog
    python ingest_local.py probe
    python ingest_local.py transcribe
    python ingest_local.py merge
    python ingest_local.py all
"""

import json, os, re, subprocess, sys, time
from pathlib import Path

# ============================================================
# Constants
# ============================================================

PROJECT_DIR = Path(__file__).parent
CATALOG_PATH = PROJECT_DIR / "catalog.json"
RETREAT_CATALOG_PATH = PROJECT_DIR / "retreat_catalog.json"
LOCAL_DIR = PROJECT_DIR / "local_mp3_files"
TRANSCRIPTS_DIR = PROJECT_DIR / "transcripts" / "retreat-talks"

WHISPER_MODEL = "large-v3"
WORDS_PER_MINUTE = 250

COLLECTION_KEY = "retreat_talks"
COLLECTION_META = {
    "name": "Satipanya — Retreat Talks",
    "slug": "retreat-talks",
    "author": "Bhante Bodhidhamma",
    "description": (
        "Retreat instruction recordings by Bhante Bodhidhamma. Guided meditations, "
        "evening dharma talks, and special topic lectures from residential vipassanā "
        "retreats at Satipanya Buddhist Retreat."
    ),
    "content_type": "text",
    "source_type": "local_audio",
    "language": "en",
    "category": "Religion & Spirituality",
    "subcategory": "Buddhism",
}

# Season assignments by track number prefix
# (track_prefix, season_number, season_name)
SEASON_RULES = [
    # S01: Guided Meditations — tracks 01-07
    (range(1, 8), 1, "Guided Meditations"),
    # S02: Daily Practice — tracks 10-11
    (range(10, 12), 2, "Daily Practice"),
    # S03: Evening Retreat Talks — tracks 12-36
    (range(12, 37), 3, "Evening Retreat Talks"),
    # S04: Special Topics — tracks 40-99 + non-numbered
    (range(40, 100), 4, "Special Topics"),
]


# ============================================================
# Filename parsing
# ============================================================

def parse_filename(filename):
    """Parse un nom de fichier MP3 de retraite en (track_number, title).

    Exemples :
        '12 The Technique 45 min 2 evening.mp3' → (12, 'The Technique')
        '01a-Starting-a-Retreat.mp3'             → (1, 'Starting a Retreat')
        '11ii Evening Metta Chant.mp3'           → (11, 'Evening Metta Chant')
        'Chi Gong min guidance oct 22.mp3'       → (None, 'Chi Gong')
    """
    stem = Path(filename).stem

    # Extraire le préfixe numérique (avec suffixe optionnel comme 'a', 'i', 'ii')
    # NB: pas de IGNORECASE — le suffixe doit être en minuscule (a, i, ii)
    m = re.match(r'^(\d+)([a-z]{0,3})[\s\-]+(.+)$', stem)
    if not m:
        # Pas de numéro de piste (ex: 'Chi Gong min guidance oct 22')
        title = stem
        track = None
    else:
        track = int(m.group(1))
        title = m.group(3)

    # Nettoyer le titre : retirer durée ('45 min'), infos session ('2 evening', 'FINISHING DAY')
    # et autres suffixes informationnels
    title = re.sub(r'\s+\d+\s*min\b', '', title)                    # '45 min'
    title = re.sub(r'\s+\d+\s+(?:evening|morning)\b', '', title, flags=re.IGNORECASE)  # '2 evening'
    title = re.sub(r'\s+FINISHING\s+DAY\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s+\w+\s+\d{2,4}\b$', '', title)              # 'oct 22' trailing date
    title = re.sub(r'\s+\d+\s*$', '', title)                        # trailing numbers

    # Remplacer tirets par espaces, nettoyer espaces multiples
    title = title.replace('-', ' ')
    title = re.sub(r'\s{2,}', ' ', title).strip()

    # Supprimer le point final s'il reste
    title = title.rstrip('.')

    return track, title


def assign_season(track, filename):
    """Assigne un numéro de saison basé sur le track number."""
    if track is None:
        return 4, "Special Topics"  # Non-numéroté → Special Topics
    for track_range, season_num, season_name in SEASON_RULES:
        if track in track_range:
            return season_num, season_name
    return 4, "Special Topics"


def make_stem(season_num, episode_num, title):
    """Crée un stem de fichier standardisé: S01E01_Title_Words."""
    slug = re.sub(r'[^A-Za-z0-9]+', '_', title)
    slug = slug.strip('_')[:60]
    return f"S{season_num:02d}E{episode_num:02d}_{slug}"


# ============================================================
# Pass 1: Catalog
# ============================================================

def pass_catalog():
    """Scan local_mp3_files/ et construit retreat_catalog.json."""
    print("=" * 60)
    print("PASS 1: Scanning local MP3 files")
    print("=" * 60)

    if not LOCAL_DIR.exists():
        print(f"ERROR: {LOCAL_DIR} not found")
        sys.exit(1)

    # Charger le catalogue existant pour préserver les données
    existing_by_url = {}
    if RETREAT_CATALOG_PATH.exists():
        with open(RETREAT_CATALOG_PATH, encoding="utf-8") as f:
            old = json.load(f)
        for season in old.get(COLLECTION_KEY, {}).get("seasons", []):
            for ep in season.get("episodes", []):
                existing_by_url[ep["url"]] = ep

    # Scanner les fichiers MP3
    mp3_files = sorted([
        f.name for f in LOCAL_DIR.iterdir()
        if f.suffix.lower() == '.mp3'
    ])

    if not mp3_files:
        print("  No MP3 files found")
        return

    print(f"  Found {len(mp3_files)} MP3 files")

    # Parser et assigner les saisons
    parsed = []
    for filename in mp3_files:
        track, title = parse_filename(filename)
        season_num, season_name = assign_season(track, filename)
        parsed.append({
            "filename": filename,
            "track": track,
            "title": title,
            "season_num": season_num,
            "season_name": season_name,
        })

    # Grouper par saison et numéroter les épisodes
    seasons_dict = {}
    for p in parsed:
        sn = p["season_num"]
        if sn not in seasons_dict:
            seasons_dict[sn] = {
                "name": p["season_name"],
                "number": sn,
                "episodes": [],
            }
        seasons_dict[sn]["episodes"].append(p)

    # Construire les saisons avec épisodes numérotés
    seasons = []
    total = 0
    for sn in sorted(seasons_dict.keys()):
        sdata = seasons_dict[sn]
        episodes = []
        for e_idx, p in enumerate(sdata["episodes"], start=1):
            url = f"local://local_mp3_files/{p['filename']}"
            stem = make_stem(sn, e_idx, p["title"])

            # Base episode
            ep = {
                "url": url,
                "local_path": f"local_mp3_files/{p['filename']}",
                "title": p["title"],
                "speaker": "Bhante Bodhidhamma",
                "language": "en",
                "file_format": "mp3",
                "content_type": "text",
                "source_type": "local_audio",
                "season_number": sn,
                "episode_number": e_idx,
                "stem": stem,
            }

            # Préserver les données existantes
            old_ep = existing_by_url.get(url, {})
            for field in [
                "duration_seconds", "file_size_bytes", "word_count",
                "reading_minutes", "transcript_path",
                "description_short", "description_long",
                "lite_score", "lite_reason",
            ]:
                if old_ep.get(field):
                    ep[field] = old_ep[field]

            episodes.append(ep)
            total += 1

        seasons.append({
            "name": sdata["name"],
            "number": sn,
            "episode_count": len(episodes),
            "episodes": episodes,
        })

    catalog = {
        COLLECTION_KEY: {
            **COLLECTION_META,
            "season_count": len(seasons),
            "episode_count": total,
            "seasons": seasons,
        }
    }

    with open(RETREAT_CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    # Résumé
    print(f"\n  {COLLECTION_META['name']}")
    for s in seasons:
        print(f"    S{s['number']:02d}: {s['name']} ({s['episode_count']} episodes)")
    print(f"\n  TOTAL: {total} episodes")
    print(f"  Saved to: {RETREAT_CATALOG_PATH}")


# ============================================================
# Pass 2: Probe
# ============================================================

def pass_probe():
    """Probe local des fichiers pour durée et taille."""
    print("=" * 60)
    print("PASS 2: Probing local files for duration & size")
    print("=" * 60)

    if not RETREAT_CATALOG_PATH.exists():
        print("ERROR: retreat_catalog.json not found. Run 'catalog' first.")
        return

    with open(RETREAT_CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)

    fdata = catalog.get(COLLECTION_KEY, {})
    probed = 0
    skipped = 0

    for season in fdata.get("seasons", []):
        for ep in season.get("episodes", []):
            if ep.get("duration_seconds", 0) > 0:
                skipped += 1
                continue

            local_path = PROJECT_DIR / ep["local_path"]
            if not local_path.exists():
                print(f"  SKIP (missing): {ep['local_path']}")
                continue

            # ffprobe pour la durée
            try:
                result = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(local_path)],
                    capture_output=True, text=True, timeout=30,
                )
                duration = float(result.stdout.strip())
                ep["duration_seconds"] = round(duration, 3)
            except (ValueError, subprocess.TimeoutExpired) as e:
                print(f"  ERROR probing {ep['local_path']}: {e}")
                continue

            # Taille du fichier
            ep["file_size_bytes"] = local_path.stat().st_size

            probed += 1
            dur_str = f"{int(duration // 60)}:{int(duration % 60):02d}"
            size_mb = ep["file_size_bytes"] / (1024 * 1024)
            print(f"  ✓ {ep['title']}: {dur_str} ({size_mb:.1f} MB)")

    with open(RETREAT_CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    print(f"\n  Probed: {probed}, Skipped (already done): {skipped}")


# ============================================================
# Pass 3: Transcribe
# ============================================================

def pass_transcribe():
    """Transcription WhisperX des fichiers locaux → SRT + TXT."""
    print("=" * 60)
    print("PASS 3: Transcribing local files (WhisperX)")
    print("=" * 60)

    # Vérifier que WhisperX est disponible
    try:
        import whisperx
        import torch
    except ImportError:
        print("ERROR: whisperx not installed. Install with: pip install whisperx")
        return

    if not RETREAT_CATALOG_PATH.exists():
        print("ERROR: retreat_catalog.json not found. Run 'catalog' first.")
        return

    with open(RETREAT_CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    fdata = catalog.get(COLLECTION_KEY, {})
    transcribed = 0
    skipped = 0

    # Charger le modèle WhisperX une seule fois
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    print(f"  Loading WhisperX model ({WHISPER_MODEL}) on {device}...")
    model = whisperx.load_model(WHISPER_MODEL, device, compute_type=compute_type,
                                language="en")

    for season in fdata.get("seasons", []):
        for ep in season.get("episodes", []):
            stem = ep.get("stem", "")
            srt_path = TRANSCRIPTS_DIR / f"{stem}.srt"
            txt_path = TRANSCRIPTS_DIR / f"{stem}.txt"

            if srt_path.exists():
                skipped += 1
                # Mettre à jour word_count si manquant
                if not ep.get("word_count") and txt_path.exists():
                    text = txt_path.read_text(encoding="utf-8")
                    words = len(text.split())
                    ep["word_count"] = words
                    ep["reading_minutes"] = max(1, round(words / WORDS_PER_MINUTE))
                if not ep.get("transcript_path"):
                    ep["transcript_path"] = str(srt_path.relative_to(PROJECT_DIR))
                continue

            local_path = PROJECT_DIR / ep["local_path"]
            if not local_path.exists():
                print(f"  SKIP (missing): {ep['local_path']}")
                continue

            print(f"  Transcribing: {ep['title']}...")
            t0 = time.time()

            try:
                # Charger et transcrire
                audio = whisperx.load_audio(str(local_path))
                result = model.transcribe(audio, batch_size=16, language="en")

                # Alignement
                align_model, align_meta = whisperx.load_align_model(
                    language_code="en", device=device
                )
                result = whisperx.align(
                    result["segments"], align_model, align_meta,
                    audio, device, return_char_alignments=False
                )

                # Écrire le SRT
                segments = result["segments"]
                write_srt(segments, srt_path)

                # Écrire le TXT (texte brut concaténé)
                full_text = " ".join(s["text"].strip() for s in segments if s.get("text"))
                txt_path.write_text(full_text, encoding="utf-8")

                # Statistiques
                words = len(full_text.split())
                ep["word_count"] = words
                ep["reading_minutes"] = max(1, round(words / WORDS_PER_MINUTE))
                ep["transcript_path"] = str(srt_path.relative_to(PROJECT_DIR))

                elapsed = time.time() - t0
                print(f"    ✓ {words} words ({elapsed:.0f}s)")
                transcribed += 1

            except Exception as e:
                print(f"    ERROR: {e}")
                continue

    with open(RETREAT_CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    print(f"\n  Transcribed: {transcribed}, Skipped (existing): {skipped}")


def write_srt(segments, srt_path):
    """Écrit les segments en format SRT."""
    def fmt_time(seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, seg in enumerate(segments, 1):
        start = seg.get("start", 0)
        end = seg.get("end", start + 1)
        text = seg.get("text", "").strip()
        if not text:
            continue
        lines.append(f"{i}")
        lines.append(f"{fmt_time(start)} --> {fmt_time(end)}")
        lines.append(text)
        lines.append("")

    srt_path.write_text("\n".join(lines), encoding="utf-8")


# ============================================================
# Pass 4: Merge
# ============================================================

def pass_merge():
    """Fusionne retreat_catalog.json dans catalog.json."""
    print("=" * 60)
    print("PASS 4: Merging into catalog.json")
    print("=" * 60)

    if not RETREAT_CATALOG_PATH.exists():
        print("ERROR: retreat_catalog.json not found. Run 'catalog' first.")
        return

    with open(CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)

    with open(RETREAT_CATALOG_PATH, encoding="utf-8") as f:
        retreat_catalog = json.load(f)

    for feed_key, fdata in retreat_catalog.items():
        ep_count = sum(
            len(s["episodes"]) for s in fdata.get("seasons", [])
        )
        if ep_count == 0:
            print(f"  SKIP {feed_key}: no episodes")
            continue

        if feed_key in catalog:
            # Mise à jour : remplacer entièrement (la source de vérité est retreat_catalog)
            catalog[feed_key] = fdata
            print(f"  ✓ Updated {feed_key}: {ep_count} episodes")
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

    if command == "catalog":
        pass_catalog()
    elif command == "probe":
        pass_probe()
    elif command == "transcribe":
        pass_transcribe()
    elif command == "merge":
        pass_merge()
    elif command == "all":
        pass_catalog()
        pass_probe()
        pass_transcribe()
        pass_merge()
    else:
        print(f"Unknown command: {command}")
        print("Usage: catalog | probe | transcribe | merge | all")
        sys.exit(1)


if __name__ == "__main__":
    main()
