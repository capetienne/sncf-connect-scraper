# 🚆 sncf-connect-scraper

Compare les prix des trains **SNCF Connect** sur plusieurs dates, avec gestion des **cartes de
réduction** (Carte Avantage Jeune), recherche **parallèle**, capture de **toute la journée**
(pagination), et exploration automatique de **trajets fractionnés** via un graphe TGV.

> ⚠️ **Usage personnel / éducatif.** Ce projet pilote un navigateur sur le site public SNCF Connect
> (protégé par l'anti-bot DataDome). Respectez les CGU du site, n'abusez pas du débit, et ne
> l'utilisez pas à des fins commerciales. Aucune API officielle n'est utilisée.

## Fonctionnalités
- 🔎 Recherche de trajets (heures, durée, correspondances, **prix**) par date.
- 🎫 **Carte Avantage Jeune** (plafonne les prix) — extensible aux autres cartes.
- ⚡ **Parallélisation** : plusieurs recherches dans un seul navigateur (~2,7× plus rapide).
- 🕖 **Toute la journée** : heure de départ réglable + pagination « trajets suivants ».
- 🧩 **Trajets fractionnés** : graphe TGV → routes candidates (directe + via Paris/Lyon/Rennes/Dijon…),
  chaînées sous contrainte de correspondance et de durée totale.
- 📊 Notebook prêt à l'emploi (`comparateur_sncf.ipynb`).

## Installation
```bash
python -m venv .venv && . .venv/bin/activate     # ou virtualenv .venv
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium                 # libs système (sudo) — voir note WSL plus bas
```

## Démarrage rapide
```python
import asyncio, sncf_scraper as s

# Un trajet, une date, avec Carte Avantage Jeune, toute la journée
js = asyncio.run(s.search_journeys('Brest', 'Bourg-en-Bresse', '2026-07-11',
                                   carte_jeune=True, heure='06h', max_pages=3))

# Plusieurs dates en parallèle
rows = asyncio.run(s.compare_dates('Brest', 'Bourg-en-Bresse',
                                   ['2026-07-10', '2026-07-11'], carte_jeune=True))

# Trajets fractionnés découverts automatiquement via le graphe TGV
best = asyncio.run(s.auto_split_search('Brest', 'Bourg-en-Bresse', '2026-07-11',
                                       carte_jeune=True, max_total_h=14))
```
Ou ouvrir le notebook : `jupyter lab comparateur_sncf.ipynb`.

## En ligne de commande (CLI)
```bash
# Comparer une plage de dates, avec Carte Avantage Jeune, journée complète
python sncf_scraper.py Brest Bourg-en-Bresse --debut 2026-07-10 --fin 2026-07-12 --carte-jeune --pages 3

# Une date précise, en trajets fractionnés (graphe TGV)
python sncf_scraper.py Brest Bourg-en-Bresse --dates 2026-07-11 --split

# Plage de dates + export CSV
python sncf_scraper.py Paris Lyon --debut 2026-07-11 --fin 2026-07-13 --csv prix.csv
```
| Option | Rôle |
|---|---|
| `origine` `destination` | villes (positionnels) |
| `--debut` / `--fin` | plage de dates (ou `--dates J1 J2 …`) |
| `--carte-jeune` | applique la Carte Avantage Jeune |
| `--heure` | heure de référence (`06h`, `08h`…) |
| `--pages` | pagination : trains/jour (`3` = journée) |
| `--split` / `--max-h` | trajets fractionnés via graphe TGV, durée totale max |
| `--concurrency` | recherches simultanées |
| `--csv` | export CSV |

`python sncf_scraper.py --help` pour le détail.

## Comment ça marche
1. **Chromium furtif** (playwright-stealth + UA réaliste) avec **profil persistant**
   (`./browser_profile`) qui conserve le cookie DataDome.
2. Flux homepage : onglet *Trains* → gares (`fill` + suggestion correspondante) → date `JJ/MM/AAAA`
   → heure de référence → *Confirmer* → (option carte) → lancer.
3. On capture la réponse de l'API interne **`bff/api/v1/itineraries`**
   (`longDistance.proposals.proposals`) → parsing propre dans `parse_itineraries()`.
4. **Pagination** : clics « Afficher les trajets suivants » (lazy-rendered, révélé au scroll) pour
   couvrir la journée.

### API du module
| Fonction | Rôle |
|---|---|
| `search_journeys(o, d, date, carte_jeune, heure, max_pages)` | un trajet, une date |
| `search_many(queries, concurrency, …)` | N recherches en parallèle (onglets) |
| `compare_dates(o, d, dates, parallel=True, …)` | comparaison multi-dates |
| `auto_split_search(o, d, date, …)` | routes via graphe TGV + chaînage |
| `candidate_routes(o, d)` / `TGV_GRAPH` | graphe et génération de routes |

## ⚠️ Passer DataDome (important)
L'anti-bot bloque le scraping « classique ». Il faut réunir :
1. **IP résidentielle/mobile** (box perso, partage de connexion 4G). Une IP datacenter est bloquée
   d'office.
2. **Navigateur visible** (`headless=False`, défaut) sur un vrai affichage. Le mode headless est détecté.
3. **Profil persistant** : à la 1re visite, résoudre une fois le slider captcha à la main dans la
   fenêtre Chromium ; le cookie est ensuite réutilisé.

### Note WSL / sans sudo
Le module auto-règle `DISPLAY=:0` (affichage WSLg) et, si présent, un dossier `syslibs/` de libs
Chromium extraites localement — utile quand `playwright install-deps` (sudo) n'est pas disponible.
Sur une machine Linux standard avec bureau, `playwright install-deps chromium` suffit.

## Limites
- `heure` va par pas de 2 h (`'06h'`, `'08h'`…). La pagination complète la journée.
- Certaines gares (ex. **Saint-Claude**) sont « Non réservable » en billet direct → passer par une
  gare proche (ex. **Bourg-en-Bresse**).
- Prix « dès » (à partir de), 2de classe.

## Pourquoi pas une API ?
Évalué et écarté : `juliuste/sncf` & `lanjelot/sncf` (voyages-sncf.com, mortes),
`maxmouchet/locomotive` (oui.sncf v3, abandonné), `tducret/trainline-python`
(Trainline v5_1 → renvoie « créez un compte / thetrainline.com »). Aucune API publique anonyme ne
fonctionne aujourd'hui.

## Licence
MIT — voir [LICENSE](LICENSE).
