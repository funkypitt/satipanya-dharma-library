#!/usr/bin/env python3
"""
garde_maj.py — Garde-fou de update-all.py : rien de ce qui existait ne doit se perdre.

    python garde_maj.py avant    # état des lieux (update-all.py le lance en premier)
    python garde_maj.py apres    # comparaison (update-all.py le lance en dernier)

Pourquoi : chaque mise à jour ou presque a défait en silence quelque chose de déjà
produit, et on ne le découvrait qu'après coup, à la main :
  - notes littéraires effacées par le re-scraping, toute la bibliothèque renotée
    (juillet et octobre 2026) ;
  - sept causeries portant le transcript d'une autre (mars à octobre 2026) ;
  - notes des 40 causeries de retraite recalculées à chaque passage, six titres
    soignés remplacés par ceux de la page source (18 novembre 2026).
À savoir aussi : build_site.py VIDE site/ (sauf books/) avant de le reconstruire ;
un fichier posé là à la main, ou qu'il ne régénère plus, disparaît.

« apres » sort en erreur (code 1) s'il y a une perte : un fichier disparu, un
document source réécrit, un épisode disparu du catalogue, ou un champ acquis
(titre, transcript, descriptions, note) perdu ou changé sur un épisode existant.
Les valeurs d'avant sont dans .garde-maj/ et dans git.
"""

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
ETAT = PROJECT_DIR / ".garde-maj"
DOSSIERS = ("site", "articles", "transcripts", "metadata", "chapters", "feeds", "covers")
SOURCES = ("articles", "transcripts", "metadata", "chapters")     # jamais réécrits
CATALOGUES = ("catalog.json", "text_catalog.json", "retreat_catalog.json", "rankings_scores.json")
CHAMPS_FIGES = ("title", "transcript_path", "description_short", "description_long",
                "lite_score", "lite_reason")
VIDE = (None, "", [], {})


def inventaire():
    inv = {}
    for racine in DOSSIERS:
        for dossier, _, fichiers in os.walk(PROJECT_DIR / racine):
            for f in fichiers:
                p = Path(dossier) / f
                inv[str(p.relative_to(PROJECT_DIR))] = hashlib.md5(p.read_bytes()).hexdigest()
    return inv


def episodes(catalogue):
    return {(cle, ep.get("url") or ep.get("title")): ep
            for cle, feed in catalogue.items()
            for saison in feed.get("seasons", [])
            for ep in saison.get("episodes", [])}


def par_dossier(chemins):
    compte = {}
    for p in chemins:
        morceaux = p.split("/")
        cle = "/".join(morceaux[:2]) if morceaux[0] == "site" and len(morceaux) > 2 else morceaux[0]
        compte[cle] = compte.get(cle, 0) + 1
    return dict(sorted(compte.items()))


def avant():
    ETAT.mkdir(exist_ok=True)
    inv = inventaire()
    (ETAT / "inventaire.json").write_text(json.dumps(inv), encoding="utf-8")
    for nom in CATALOGUES:
        if (PROJECT_DIR / nom).exists():
            shutil.copy2(PROJECT_DIR / nom, ETAT / nom)
    # Une copie datée du catalogue, à côté de lui, comme les sauvegardes faites à la main.
    shutil.copy2(PROJECT_DIR / "catalog.json",
                 PROJECT_DIR / f"catalog.json.bak-avant-maj-{time.strftime('%Y%m%d-%H%M%S')}")
    n = len(episodes(json.loads((PROJECT_DIR / "catalog.json").read_text(encoding="utf-8"))))
    print(f"  État des lieux : {len(inv)} fichiers, {n} épisodes → {ETAT.name}/")


def apres():
    if not (ETAT / "inventaire.json").exists():
        print("  ⚠ Pas d'état des lieux (garde_maj.py avant) : rien à comparer.")
        return 0
    inv_avant = json.loads((ETAT / "inventaire.json").read_text(encoding="utf-8"))
    inv_apres = inventaire()
    disparus = sorted(set(inv_avant) - set(inv_apres))
    nouveaux = sorted(set(inv_apres) - set(inv_avant))
    changes = sorted(p for p in inv_avant if p in inv_apres and inv_avant[p] != inv_apres[p])
    sources_reecrites = [p for p in changes if p.split("/")[0] in SOURCES]
    pages_changees = [p for p in changes if p.endswith((".html", ".xml", ".json"))]

    ancien = episodes(json.loads((ETAT / "catalog.json").read_text(encoding="utf-8")))
    actuel = episodes(json.loads((PROJECT_DIR / "catalog.json").read_text(encoding="utf-8")))
    ep_disparus = sorted(set(ancien) - set(actuel), key=str)
    ep_nouveaux = sorted(set(actuel) - set(ancien), key=str)
    champs = []
    for cle in set(ancien) & set(actuel):
        for champ, valeur in ancien[cle].items():
            maintenant = actuel[cle].get(champ)
            if valeur in VIDE or maintenant == valeur:
                continue
            if maintenant in VIDE or champ in CHAMPS_FIGES:
                champs.append(f"{cle[0]} | {ancien[cle].get('title')} | {champ} : "
                              f"{str(valeur)[:45]!r} → {str(maintenant)[:45]!r}")

    print(f"  Fichiers : {len(nouveaux)} nouveaux {par_dossier(nouveaux)}")
    print(f"             {len(changes)} régénérés, dont {len(pages_changees)} pages ou flux "
          f"au contenu changé")
    print(f"  Catalogue : {len(ancien)} → {len(actuel)} épisodes, {len(ep_nouveaux)} nouveaux")
    for cle in ep_nouveaux[:40]:
        print(f"     + {cle[0]} | {actuel[cle].get('title')}")

    pertes = [
        ("FICHIERS DISPARUS", disparus),
        ("DOCUMENTS SOURCES RÉÉCRITS (articles, transcripts, metadata, chapters)", sources_reecrites),
        ("ÉPISODES DISPARUS DU CATALOGUE",
         [f"{c[0]} | {ancien[c].get('title')}" for c in ep_disparus]),
        ("CHAMPS ACQUIS PERDUS OU CHANGÉS", sorted(champs)),
    ]
    total = sum(len(liste) for _, liste in pertes)
    if not total:
        print("  ✓ Rien de ce qui existait n'a été perdu ni réécrit.")
        if not ep_nouveaux and not nouveaux and not pages_changees:
            print("  Aucun contenu nouveau : le site est le même, inutile de l'envoyer "
                  "(git checkout -- site feeds efface les PDF/EPUB simplement redatés).")
        return 0
    print("\n  ✗✗✗ PERTES — NE PAS ENVOYER LE SITE AVANT D'AVOIR COMPRIS ✗✗✗")
    for titre, liste in pertes:
        if liste:
            print(f"\n  {titre} : {len(liste)}")
            for ligne in liste[:60]:
                print(f"     - {ligne}")
            if len(liste) > 60:
                print(f"     … et {len(liste) - 60} autres")
    print(f"\n  Les valeurs d'avant sont dans {ETAT.name}/ et dans git (git status, git diff).")
    return 1


if __name__ == "__main__":
    commande = sys.argv[1] if len(sys.argv) > 1 else ""
    if commande == "avant":
        avant()
    elif commande == "apres":
        sys.exit(apres())
    else:
        print(__doc__)
        sys.exit(2)
