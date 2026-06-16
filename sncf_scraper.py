"""
Scraper SNCF Connect via Playwright — VERSION FONCTIONNELLE (testée 2026-06-16).

Ce qui a été nécessaire pour passer DataDome (anti-bot) :
  - IP RÉSIDENTIELLE/MOBILE (partage de connexion) — une IP datacenter est bloquée d'office.
  - Navigateur HEADED (vrai Chromium, pas headless-shell) via un display (WSLg :0 ou Xvfb).
  - PROFIL PERSISTANT (./browser_profile) : le cookie `datadome` y est conservé -> on ne repasse
    pas le captcha à chaque run. La 1re fois, un slider captcha peut apparaître : le résoudre à la
    main dans la fenêtre Chromium (elle est visible sur le bureau via WSLg), puis tout s'enchaîne.

Données : on capture la réponse de l'API interne `bff/api/v1/itineraries` (JSON propre :
clé longDistance.proposals.proposals) -> heures, durée, correspondances, prix.

Flux UI (homepage sncf-connect.com) :
  onglet "Trains" -> 2 champs "Gare, ville, lieu..." (départ/arrivée) + suggestion
  -> champ texte date #input-date-startDate (JJ/MM/AAAA) -> "Confirmer" -> "Rechercher".
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
RAW_DIR = BASE / "raw"
RAW_DIR.mkdir(exist_ok=True)
PROFILE_DIR = str(BASE / "browser_profile")

# --- auto-config environnement (libs Chromium extraites localement + display WSLg) ---------- #
_libs = [BASE / "syslibs/root/usr/lib/x86_64-linux-gnu", BASE / "syslibs/root/lib/x86_64-linux-gnu"]
_existing = os.environ.get("LD_LIBRARY_PATH", "")
_paths = [str(p) for p in _libs if p.exists()]
if _paths:
    os.environ["LD_LIBRARY_PATH"] = ":".join(_paths + ([_existing] if _existing else []))
os.environ.setdefault("DISPLAY", ":0")          # WSLg fournit un vrai serveur X sur :0

from playwright.async_api import async_playwright, Page, Response, TimeoutError as PWTimeout  # noqa: E402

try:
    from playwright_stealth import Stealth       # playwright-stealth >= 2.0
    _STEALTH = Stealth()
except Exception:
    _STEALTH = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


@dataclass
class Journey:
    date: str            # date recherchée (YYYY-MM-DD)
    origine: str
    destination: str
    depart: str          # "13:42"
    arrivee: str         # "22:09"
    duree: str           # "8h27"
    correspondances: int
    prix_eur: float | None
    reservable: bool = True
    trains: str = ""                 # n° de trains, ex "8608, 9773"
    segments: list = field(default_factory=list)   # détail par tronçon (gares, n°, horaires)

    def as_dict(self):
        return asdict(self)


async def _sleep(a=0.4, b=1.1):
    await asyncio.sleep(random.uniform(a, b))


# --------------------------------------------------------------------------- #
# Parsing de la réponse API `bff/api/v1/itineraries`
# --------------------------------------------------------------------------- #
def _segments_from_timeline(pr: dict) -> list[dict]:
    """Extrait le détail par tronçon (gares, n° de train, horaires, correspondances)."""
    steps = (pr.get("globalTimeline") or {}).get("steps") or []
    out = []
    for s in steps:
        if "train" in s:
            t = s["train"]
            tr = t.get("transporter", {}) or {}
            dep, arr = t.get("departure") or {}, t.get("arrival") or {}
            out.append({
                "kind": "train",
                "train": (tr.get("referenceLabel") or tr.get("number") or "").replace("n° ", "n°"),
                "type": tr.get("description", "") or "",
                "de": dep.get("stationLabel", ""), "depart": dep.get("timeLabel", ""),
                "a": arr.get("stationLabel", ""), "arrivee": arr.get("timeLabel", ""),
                "duree": (t.get("duration") or {}).get("label", ""),
            })
        elif "connection" in s:
            c = s["connection"]
            info = c.get("correspondanceRoutingSuggestion", "")
            if isinstance(info, dict):
                info = info.get("label", "") or ""
            out.append({"kind": "corresp", "duree": c.get("correspondanceTimeLabel", ""),
                        "info": info})
    return out


def parse_itineraries(data: dict, date_iso: str, origine: str, destination: str) -> list[Journey]:
    out: list[Journey] = []
    try:
        props = data["longDistance"]["proposals"]["proposals"]
    except (KeyError, TypeError):
        return out
    for pr in props:
        dep = (pr.get("departure") or {}).get("timeLabel")
        arr = (pr.get("arrival") or {}).get("timeLabel")
        if not dep or not arr:
            continue
        dur = pr.get("durationLabel", "")
        td = pr.get("transporterDescription", "") or ""
        m = re.search(r"(\d+)\s*correspondance", td)
        corr = int(m.group(1)) if m else (0 if "direct" in td.lower() else 0)
        price = None
        label = pr.get("bestPriceLabel")
        if label:
            mm = re.search(r"(\d+(?:[.,]\d+)?)", label.replace(" ", "").replace(" ", ""))
            if mm:
                price = float(mm.group(1).replace(",", "."))
        segs = _segments_from_timeline(pr)
        trains = ", ".join(s["train"].replace("n°", "") for s in segs if s["kind"] == "train")
        out.append(Journey(
            date=date_iso, origine=origine, destination=destination,
            depart=dep, arrivee=arr, duree=dur, correspondances=corr,
            prix_eur=price, reservable=price is not None,
            trains=trains.strip(), segments=segs,
        ))
    return sorted(out, key=lambda j: j.depart)


# --------------------------------------------------------------------------- #
# Pilotage du formulaire
# --------------------------------------------------------------------------- #
async def _accept_cookies(page: Page):
    for sel in ['#didomi-notice-agree-button',
                'button:has-text("Accepter et fermer")',
                'button:has-text("Tout accepter")',
                'button:has-text("Accepter")']:
        try:
            b = page.locator(sel).first
            if await b.is_visible(timeout=2000):
                await b.click()
                return
        except Exception:
            continue


async def _wait_datadome(page: Page, max_s=180):
    """Si un captcha DataDome est présent, attend qu'il disparaisse (résolution manuelle)."""
    for _ in range(max_s):
        body = (await page.inner_text("body"))[:300]
        if "Accès temporairement restreint" in body or "sécuriser votre accès" in body \
                or "s'adresse bien à vous" in body:
            print("   ⏳ Captcha DataDome — résous-le dans la fenêtre Chromium (visible sur le bureau)…")
            await asyncio.sleep(3)
        else:
            return True
    return False


async def _set_carte_jeune(page: Page, age: int = 25):
    """Ajoute la Carte Avantage Jeune au voyageur 1 (âge 4-29 ans requis pour qu'elle apparaisse)."""
    await page.locator('button:has-text("voyageur")').first.click()
    await _sleep(0.9, 1.5)
    await page.get_by_text(re.compile(r"Voyageur 1")).first.click()
    await _sleep(0.9, 1.5)
    # régler la tranche d'âge sur 4 - 29 ans (sinon la carte Jeune n'est pas proposée)
    await page.get_by_text("30 - 59 ans", exact=True).first.click()
    await _sleep(0.5, 0.9)
    await page.get_by_text("4 - 29 ans", exact=True).first.click()
    await _sleep(0.5, 0.9)
    # cette tranche rend obligatoire le champ "Âge (le jour du voyage)"
    await page.get_by_label(re.compile(r"Âge", re.I)).first.fill(str(age))
    await _sleep(0.4, 0.8)
    # ouvrir les cartes de réduction et cocher la Jeune
    await page.get_by_text(re.compile(r"Cartes de réduction")).first.click()
    await _sleep(0.8, 1.3)
    # cliquer la 1re occurrence VISIBLE (un doublon caché existe dans le DOM)
    loc = page.get_by_text("Carte Avantage Jeune", exact=True)
    for i in range(await loc.count()):
        el = loc.nth(i)
        if await el.is_visible():
            await el.click()
            break
    await _sleep(0.5, 0.9)
    await page.locator('button:has-text("Valider")').first.click()
    await _sleep(0.6, 1.1)
    await page.locator('button:has-text("Appliquer ce profil")').first.click()
    await _sleep(0.8, 1.3)


async def _fill_station(page: Page, idx: int, value: str):
    inp = page.locator('input[placeholder="Gare, ville, lieu..."]').nth(idx)
    await inp.click()
    await inp.fill(value)                        # saisie directe (rapide)
    # cliquer l'option qui CORRESPOND à la saisie (sinon on tombe sur une recherche récente)
    options = page.locator('li[role="option"], [role="option"]')
    match = options.filter(has_text=value)
    try:
        await match.first.wait_for(state="visible", timeout=6000)
        await match.first.click()
    except Exception:                            # repli : laisse l'autocomplétion se rafraîchir
        await _sleep(1.0, 1.6)
        await options.first.click()


async def _set_heure(page: Page, heure: str):
    """Règle l'heure de départ (pas de 2h : 00h,02h,…,22h) pour capter toute la journée."""
    await page.locator("#startDateTime").click()
    await _sleep(0.3, 0.7)
    await page.locator(f'li[role="option"]:has-text("{heure}")').first.click()
    await _sleep(0.3, 0.6)


async def _run_flow(page: Page, origine: str, destination: str, date: str,
                    carte_jeune: bool, timeout_ms: int, heure: str | None = "06h",
                    max_pages: int = 2) -> list[Journey]:
    """Exécute le flux de recherche sur une page DÉJÀ ouverte. Renvoie les Journey.

    max_pages : nombre de clics « Afficher les trajets suivants » (pagination) pour capter
    plus de trains dans la journée (0 = page initiale seulement).
    """
    date_fr = datetime.strptime(date, "%Y-%m-%d").strftime("%d/%m/%Y")
    captured: list = []

    async def on_response(resp: Response):
        if "itineraries" in resp.url and "json" in resp.headers.get("content-type", ""):
            try:
                captured.append(await resp.json())
            except Exception:
                pass
    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    await page.goto("https://www.sncf-connect.com/", timeout=timeout_ms, wait_until="domcontentloaded")
    await _sleep(1.2, 2.0)
    await _wait_datadome(page)
    await _accept_cookies(page)

    tab = page.locator("#home-search-tab-train")
    await tab.wait_for(state="visible", timeout=timeout_ms)
    await tab.click()
    await _fill_station(page, 0, origine)
    await _fill_station(page, 1, destination)

    # date : champ texte JJ/MM/AAAA (AVANT la carte, car le panneau Voyageurs reste ouvert)
    dfield = page.locator("#input-date-startDate")
    await dfield.click()
    await dfield.fill(date_fr)                   # saisie directe
    await _sleep(0.2, 0.4)
    if heure:
        try:
            await _set_heure(page, heure)
        except Exception:
            pass
    await page.locator('button:has-text("Confirmer")').first.click()
    await _sleep(0.6, 1.1)

    # carte de réduction (laisse le panneau Voyageurs ouvert -> on lance via son bouton)
    if carte_jeune:
        await _set_carte_jeune(page)
        await _sleep(0.3, 0.7)
        await page.locator('button:has-text("Lancer la recherche")').first.click()
    else:
        await page.locator('button:has-text("Rechercher")').last.click()

    try:
        await page.wait_for_url("**/shop/results/**", timeout=timeout_ms)
    except PWTimeout:
        pass
    for _ in range(25):
        if captured:
            break
        await asyncio.sleep(0.6)

    # pagination : « Afficher les trajets suivants » (lazy-rendered en bas -> scroller d'abord)
    for _ in range(max_pages):
        await page.mouse.wheel(0, 6000)
        await _sleep(0.4, 0.8)
        btn = page.locator('button:has-text("Afficher les trajets suivants")').first
        try:
            if not await btn.is_visible(timeout=1500):   # routes peu desservies : absent -> on sort
                break
            before = len(captured)
            await btn.scroll_into_view_if_needed()
            await btn.click()
            for _ in range(20):                  # attend une nouvelle réponse itineraries
                if len(captured) > before:
                    break
                await asyncio.sleep(0.4)
            await _sleep(0.4, 0.8)
        except Exception:
            break

    stamp = f"{origine}_{destination}_{date}".replace(" ", "")
    if captured:
        (RAW_DIR / f"{stamp}.json").write_text(json.dumps(captured[-1], ensure_ascii=False)[:5_000_000])

    journeys: list[Journey] = []
    for data in captured:
        journeys.extend(parse_itineraries(data, date, origine, destination))
    # dédoublonnage : garder la version la PLUS RICHE (certaines réponses n'ont ni prix ni détail)
    seen: dict = {}
    for j in journeys:
        key = (j.depart, j.arrivee)
        prev = seen.get(key)
        if prev is None or (not prev.segments and j.segments) \
                or (prev.prix_eur is None and j.prix_eur is not None):
            seen[key] = j
    return sorted(seen.values(), key=lambda x: x.depart)


async def _with_context(headless: bool):
    """Ouvre un contexte persistant unique (réutilise le cookie DataDome)."""
    pw_cm = _STEALTH.use_async(async_playwright()) if _STEALTH else async_playwright()
    p = await pw_cm.__aenter__()
    ctx = await p.chromium.launch_persistent_context(
        PROFILE_DIR, headless=headless, user_agent=UA, locale="fr-FR",
        timezone_id="Europe/Paris", viewport={"width": 1366, "height": 900},
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    return pw_cm, p, ctx


async def search_journeys(origine, destination, date, headless=False,
                          timeout_ms=60000, carte_jeune=False, heure="06h",
                          max_pages=2) -> list[Journey]:
    """Recherche les trajets pour une date (aller simple). Renvoie une liste de Journey."""
    pw_cm, _p, ctx = await _with_context(headless)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return await _run_flow(page, origine, destination, date, carte_jeune, timeout_ms, heure, max_pages)
    finally:
        await ctx.close()
        await pw_cm.__aexit__(None, None, None)


async def search_many(queries, headless=False, carte_jeune=False,
                      concurrency=5, timeout_ms=60000, heure="06h", max_pages=2,
                      n_browsers=1) -> list[dict]:
    """Lance PLUSIEURS recherches en parallèle (onglets) dans un seul navigateur.

    queries : liste de (origine, destination, date). Renvoie une liste de dicts.
    Un seul contexte persistant = un seul cookie DataDome partagé ; `concurrency` onglets max.
    `n_browsers>1` -> délègue à search_many_multi (N navigateurs × `concurrency` onglets).
    """
    if n_browsers > 1:
        return await search_many_multi(queries, n_browsers=n_browsers, concurrency_per=concurrency,
                                       headless=headless, carte_jeune=carte_jeune,
                                       heure=heure, max_pages=max_pages, timeout_ms=timeout_ms)
    pw_cm, _p, ctx = await _with_context(headless)
    sem = asyncio.Semaphore(concurrency)
    rows: list[dict] = []

    async def one(q):
        origine, destination, date = q
        async with sem:
            page = await ctx.new_page()
            try:
                js = await _run_flow(page, origine, destination, date, carte_jeune, timeout_ms, heure, max_pages)
                n = sum(1 for j in js if j.prix_eur is not None)
                print(f"✓ {origine}→{destination} {date} : {len(js)} trajet(s), {n} prix")
                return [j.as_dict() for j in js]
            except Exception as e:
                print(f"✗ {origine}→{destination} {date} : {type(e).__name__}: {e}")
                return []
            finally:
                await page.close()

    try:
        results = await asyncio.gather(*[one(q) for q in queries])
        for r in results:
            rows.extend(r)
    finally:
        await ctx.close()
        await pw_cm.__aexit__(None, None, None)
    return rows


async def _run_queries_on_context(ctx, chunk, sem, carte_jeune, heure, max_pages, timeout_ms):
    async def one(q):
        origine, destination, date = q
        async with sem:
            page = await ctx.new_page()
            try:
                js = await _run_flow(page, origine, destination, date,
                                     carte_jeune, timeout_ms, heure, max_pages)
                n = sum(1 for j in js if j.prix_eur is not None)
                print(f"✓ {origine}→{destination} {date} : {len(js)} trajet(s), {n} prix")
                return [j.as_dict() for j in js]
            except Exception as e:
                print(f"✗ {origine}→{destination} {date} : {type(e).__name__}: {e}")
                return []
            finally:
                await page.close()
    res = await asyncio.gather(*[one(q) for q in chunk])
    return [r for sub in res for r in sub]


async def search_many_multi(queries, n_browsers=2, concurrency_per=3, headless=False,
                            carte_jeune=False, heure="06h", max_pages=2, timeout_ms=60000):
    """Parallélisme maximal : N NAVIGATEURS (copies du profil) × `concurrency_per` onglets.

    Dépasse la limite d'un seul navigateur en copiant `browser_profile` (cookie DataDome inclus)
    en N exemplaires. ⚠️ même cookie + même IP en parallèle : DataDome peut réagir -> tester.
    """
    n_browsers = max(1, min(n_browsers, len(queries)))
    chunks = [queries[i::n_browsers] for i in range(n_browsers)]   # répartition round-robin
    pw_cm = _STEALTH.use_async(async_playwright()) if _STEALTH else async_playwright()
    rows: list[dict] = []

    async with pw_cm as p:
        async def run_browser(chunk, idx):
            if not chunk:
                return []
            dst = f"{PROFILE_DIR}_w{idx}"
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(PROFILE_DIR, dst, symlinks=True,           # copie le cookie DataDome
                            ignore=shutil.ignore_patterns("Singleton*", "*lock*", "*.lock"))
            ctx = await p.chromium.launch_persistent_context(
                dst, headless=headless, user_agent=UA, locale="fr-FR",
                timezone_id="Europe/Paris", viewport={"width": 1366, "height": 900},
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            try:
                sem = asyncio.Semaphore(concurrency_per)
                return await _run_queries_on_context(ctx, chunk, sem, carte_jeune,
                                                     heure, max_pages, timeout_ms)
            finally:
                await ctx.close()
                shutil.rmtree(dst, ignore_errors=True)

        print(f"{n_browsers} navigateur(s) × {concurrency_per} onglet(s) = "
              f"{n_browsers * concurrency_per} recherches simultanées")
        results = await asyncio.gather(*[run_browser(c, i) for i, c in enumerate(chunks)])
    for r in results:
        rows.extend(r)
    return rows


async def compare_dates(origine, destination, dates, headless=False, carte_jeune=False,
                        parallel=True, concurrency=5, max_pages=2, n_browsers=1) -> list[dict]:
    """Recherche pour plusieurs dates. `parallel=True` -> onglets simultanés (rapide)."""
    queries = [(origine, destination, d) for d in dates]
    if parallel:
        return await search_many(queries, headless=headless, carte_jeune=carte_jeune,
                                 concurrency=concurrency, max_pages=max_pages, n_browsers=n_browsers)
    rows = []
    for q in queries:
        rows.extend(await search_many([q], headless=headless, carte_jeune=carte_jeune,
                                      concurrency=1, max_pages=max_pages))
    return rows


# --------------------------------------------------------------------------- #
# Trajets FRACTIONNÉS (billets séparés via villes intermédiaires)
# --------------------------------------------------------------------------- #
def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _fmt_h(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}"


def _chain_route(legs, by_leg, min_lay, max_lay, max_total_min):
    """Renvoie toutes les combinaisons valides (1 option par segment) d'une route."""
    options = []
    for leg in legs:
        opts = [r for r in by_leg.get(leg, []) if r.get("prix_eur") is not None]
        opts.sort(key=lambda r: _to_min(r["depart"]))
        options.append(opts)
    if any(not o for o in options):
        return []                          # un segment sans prix -> route impossible

    chains = []

    def build(idx, cur):
        if idx == len(legs):
            first = _to_min(cur[0]["depart"])
            last = _to_min(cur[-1]["arrivee"])
            if 0 < last - first <= max_total_min:
                chains.append(list(cur))
            return
        for opt in options[idx]:
            if cur:
                gap = _to_min(opt["depart"]) - _to_min(cur[-1]["arrivee"])
                if not (min_lay <= gap <= max_lay):
                    continue
            build(idx + 1, cur + [opt])

    build(0, [])
    return chains


def combine_splits(rows, routes, min_layover_min=30, max_layover_min=300, max_total_h=14):
    """À partir des résultats par segment, construit le meilleur trajet fractionné par route."""
    by_leg = {}
    for r in rows:
        by_leg.setdefault((r["origine"], r["destination"]), []).append(r)

    out = []
    for legs in routes:
        chains = _chain_route(legs, by_leg, min_layover_min, max_layover_min, max_total_h * 60)
        for ch in chains:
            first, last = _to_min(ch[0]["depart"]), _to_min(ch[-1]["arrivee"])
            corr = sum(c["correspondances"] for c in ch) + (len(ch) - 1)
            out.append({
                "route": " → ".join([ch[0]["origine"]] + [c["destination"] for c in ch]),
                "n_segments": len(ch),
                "depart": ch[0]["depart"],
                "arrivee": ch[-1]["arrivee"],
                "duree_totale": _fmt_h(last - first),
                "correspondances": corr,
                "prix_total": round(sum(c["prix_eur"] for c in ch), 2),
                "detail": " | ".join(f'{c["origine"]} {c["depart"]}→{c["arrivee"]} {c["prix_eur"]}€'
                                     for c in ch),
            })
    # garder la combinaison la moins chère par route
    best = {}
    for o in out:
        k = o["route"]
        if k not in best or o["prix_total"] < best[k]["prix_total"]:
            best[k] = o
    return sorted(best.values(), key=lambda o: o["prix_total"])


# Graphe des grandes villes reliées en TGV (liens ~directs). Non exhaustif mais couvre les hubs.
TGV_GRAPH = {
    "Brest": ["Rennes", "Paris"],
    "Rennes": ["Brest", "Paris", "Lyon", "Marseille", "Strasbourg", "Lille", "Nantes", "Bordeaux"],
    "Nantes": ["Rennes", "Paris", "Lyon", "Marseille", "Bordeaux", "Strasbourg", "Lille"],
    "Paris": ["Brest", "Rennes", "Nantes", "Lyon", "Lille", "Strasbourg", "Bordeaux",
              "Marseille", "Dijon", "Bourg-en-Bresse", "Montpellier"],
    "Lyon": ["Paris", "Rennes", "Nantes", "Marseille", "Montpellier", "Strasbourg", "Dijon",
             "Bourg-en-Bresse", "Lille", "Grenoble"],
    "Bourg-en-Bresse": ["Lyon", "Paris", "Dijon"],
    "Dijon": ["Paris", "Lyon", "Bourg-en-Bresse", "Strasbourg"],
    "Marseille": ["Paris", "Lyon", "Rennes", "Nantes", "Lille", "Strasbourg", "Montpellier"],
    "Lille": ["Paris", "Lyon", "Marseille", "Rennes", "Nantes", "Strasbourg"],
    "Strasbourg": ["Paris", "Lyon", "Dijon", "Marseille", "Lille", "Rennes", "Nantes"],
    "Bordeaux": ["Paris", "Rennes", "Nantes", "Marseille", "Lyon"],
    "Montpellier": ["Lyon", "Marseille", "Paris"],
    "Grenoble": ["Lyon", "Paris"],
}

# Coordonnées (lat, lon) pour la carte de France
CITY_COORDS = {
    "Brest": (48.39, -4.49), "Quimper": (47.99, -4.10), "Rennes": (48.11, -1.68),
    "Nantes": (47.22, -1.55), "Angers": (47.47, -0.55), "Le Mans": (48.00, 0.20),
    "Tours": (47.39, 0.69), "Paris": (48.85, 2.35), "Lille": (50.63, 3.06),
    "Strasbourg": (48.58, 7.75), "Dijon": (47.32, 5.04), "Lyon": (45.76, 4.84),
    "Bourg-en-Bresse": (46.20, 5.23), "Grenoble": (45.19, 5.72), "Marseille": (43.30, 5.37),
    "Montpellier": (43.61, 3.88), "Bordeaux": (44.84, -0.58), "Toulouse": (43.60, 1.44),
    "Nice": (43.70, 7.27), "Genève": (46.20, 6.14),
}

# Contour simplifié de la France métropolitaine (lat, lon), projeté comme les villes
FRANCE_BORDER = [
    (51.03, 2.37), (50.95, 1.85), (50.12, 1.55), (49.70, 0.20), (49.43, -0.40),
    (49.40, -1.00), (49.72, -1.55), (49.30, -1.62), (48.65, -1.52), (48.72, -2.80),
    (48.70, -3.95), (48.40, -4.78), (48.03, -4.72), (47.80, -4.05), (47.52, -3.10),
    (47.28, -2.50), (46.50, -1.80), (45.75, -1.20), (44.65, -1.25), (43.55, -1.55),
    (43.30, -1.45), (42.80, -0.70), (42.70, 0.65), (42.48, 1.45), (42.45, 2.02),
    (42.72, 3.03), (43.20, 3.05), (43.45, 3.95), (43.38, 4.85), (43.28, 5.38),
    (43.10, 6.00), (43.55, 6.95), (43.75, 7.52), (44.15, 7.00), (44.85, 6.90),
    (45.13, 6.63), (45.90, 6.80), (46.25, 6.30), (46.40, 6.10), (47.40, 7.00),
    (47.62, 7.55), (48.32, 7.60), (48.97, 8.22), (49.50, 6.60), (49.80, 4.85),
    (50.35, 4.20), (50.50, 3.60), (51.00, 2.55),
]


def candidate_routes(origin, dest, max_legs=3, max_routes=8, graph=TGV_GRAPH):
    """Trouve les routes (chemins simples) de origin à dest dans le graphe TGV.

    Renvoie une liste de routes ; chaque route = liste de segments (A, B).
    La route directe [(origin, dest)] est toujours incluse en premier.
    """
    paths = []

    def dfs(node, path):
        if len(path) - 1 > max_legs:
            return
        if node == dest and len(path) >= 2:
            paths.append(path[:])
            return
        for nxt in graph.get(node, []):
            if nxt not in path:
                dfs(nxt, path + [nxt])

    dfs(origin, [origin])
    paths.sort(key=len)                          # routes les plus directes d'abord
    routes = [[(origin, dest)]]                  # toujours tester le trajet direct
    for p in paths:
        r = [(p[i], p[i + 1]) for i in range(len(p) - 1)]
        if r not in routes:
            routes.append(r)
        if len(routes) >= max_routes:
            break
    return routes


async def auto_split_search(origin, dest, date, max_legs=3, max_routes=8, **kwargs):
    """Découvre seul les routes candidates via le graphe TGV, puis lance split_search."""
    routes = candidate_routes(origin, dest, max_legs=max_legs, max_routes=max_routes)
    print("Routes candidates :")
    for r in routes:
        print("  •", " → ".join([r[0][0]] + [b for _, b in r]))
    return await split_search(routes, date, **kwargs)


async def split_search(routes, date, carte_jeune=True, heure="06h",
                       min_layover_min=30, max_layover_min=300, max_total_h=14,
                       concurrency=5, headless=False, max_pages=2) -> list[dict]:
    """Cherche le meilleur trajet fractionné parmi plusieurs routes pour une date.

    routes : liste de routes ; chaque route = liste de segments (A,B), ex.
        [[('Brest','Bourg-en-Bresse')],
         [('Brest','Lyon'),('Lyon','Bourg-en-Bresse')],
         [('Brest','Rennes'),('Rennes','Lyon'),('Lyon','Bourg-en-Bresse')]]
    Tous les segments uniques sont cherchés en parallèle, puis chaînés.
    """
    legs = {leg for r in routes for leg in r}
    queries = [(a, b, date) for (a, b) in legs]
    print(f"Recherche de {len(queries)} segments en parallèle…")
    rows = await search_many(queries, headless=headless, carte_jeune=carte_jeune,
                             concurrency=concurrency, heure=heure, max_pages=max_pages)
    return combine_splits(rows, routes, min_layover_min, max_layover_min, max_total_h)


async def auto_split_dates(origin, dest, dates, max_legs=3, max_routes=8,
                           carte_jeune=True, heure="06h", max_pages=2,
                           min_layover_min=30, max_layover_min=300, max_total_h=14,
                           concurrency=5, headless=False, n_browsers=1) -> list[dict]:
    """Split-ticketing sur PLUSIEURS dates en un seul lot parallèle (segments × dates aplatis)."""
    routes = candidate_routes(origin, dest, max_legs=max_legs, max_routes=max_routes)
    print("Routes candidates :")
    for r in routes:
        print("  •", " → ".join([r[0][0]] + [b for _, b in r]))
    legs = {leg for r in routes for leg in r}
    queries = [(a, b, d) for d in dates for (a, b) in legs]
    print(f"{len(queries)} recherches (segments × dates) en parallèle…")
    rows = await search_many(queries, headless=headless, carte_jeune=carte_jeune,
                             concurrency=concurrency, heure=heure, max_pages=max_pages,
                             n_browsers=n_browsers)
    out = []
    for d in dates:
        drows = [r for r in rows if r["date"] == d]
        for combo in combine_splits(drows, routes, min_layover_min, max_layover_min, max_total_h):
            combo["date"] = d
            out.append(combo)
    return out


# --------------------------------------------------------------------------- #
# Visualisation HTML
# --------------------------------------------------------------------------- #
def _price_of(r, pcol):
    v = r.get(pcol)
    return None if v is None or (isinstance(v, float) and v != v) else float(v)


def _esc(v):
    import html as _html
    return _html.escape("" if v is None or (isinstance(v, float) and v != v) else str(v))


def _detail_html(r):
    """Détail dépliable d'un trajet : trains (n°, type), gares, horaires, correspondances."""
    segs = r.get("segments") or []
    if segs:
        parts = []
        for s in segs:
            if s.get("kind") == "train":
                parts.append(
                    f'<div class="seg"><span class="tno">{_esc(s.get("train"))}</span>'
                    f'<span class="ttype">{_esc(s.get("type"))}</span><br>'
                    f'<b>{_esc(s.get("depart"))}</b> {_esc(s.get("de"))} → '
                    f'<b>{_esc(s.get("arrivee"))}</b> {_esc(s.get("a"))} '
                    f'<span class="muted">({_esc(s.get("duree"))})</span></div>')
            else:
                info = s.get("info")
                parts.append(f'<div class="corresp">↔ correspondance {_esc(s.get("duree"))}'
                             + (f' · {_esc(info)}' if info else "") + "</div>")
        return "".join(parts)
    if r.get("detail"):                                   # combos fractionnés
        return "".join(f'<div class="seg">{_esc(p.strip())}</div>'
                       for p in str(r["detail"]).split("|"))
    return '<div class="seg muted">Détail indisponible</div>'


def _cheapest(rows):
    if not rows:
        return None, None
    pcol = "prix_total" if "route" in rows[0] else "prix_eur"
    best = None
    for r in rows:
        p = _price_of(r, pcol)
        if p is not None and (best is None or p < _price_of(best, pcol)):
            best = r
    return best, pcol


def _table_html(rows, tab_id):
    if not rows:
        return '<p class="muted" style="padding:16px">Aucun trajet.</p>'
    split = "route" in rows[0]
    pcol = "prix_total" if split else "prix_eur"
    rows = sorted(rows, key=lambda r: (r.get("date", ""),
                                       _price_of(r, pcol) if _price_of(r, pcol) is not None else 9e9))
    prices = [p for p in (_price_of(r, pcol) for r in rows) if p is not None]
    pmin, pmax = (min(prices), max(prices)) if prices else (0, 1)
    mins = {}
    for r in rows:
        p = _price_of(r, pcol)
        if p is not None:
            mins[r.get("date")] = min(mins.get(r.get("date"), 9e9), p)

    head = ["Route" if split else "Trajet", "Départ", "Arrivée", "Durée", "Corresp.", "Trains", "Prix"]
    th = "".join(f"<th>{h}</th>" for h in head)
    body, cur, rid = "", None, 0
    for r in rows:
        d = r.get("date")
        if d != cur:
            cur = d
            body += f'<tr class="date-row"><td colspan="7">📅 {_esc(d)}</td></tr>'
        lbl = r["route"] if split else f'{r.get("origine")} → {r.get("destination")}'
        dur = r.get("duree_totale") if split else r.get("duree")
        p = _price_of(r, pcol)
        if p is not None:
            frac = 0 if pmax == pmin else (p - pmin) / (pmax - pmin)
            pcell = (f'<div class="bar" style="width:{int(100 * (1 - frac))}%"></div>'
                     f'<span class="ptxt">{p:.2f} €</span>')
        else:
            pcell = '<span class="na">non réservable</span>'
        best = " best" if (p is not None and abs(p - mins.get(d, -1)) < 1e-6) else ""
        rid += 1
        did = f"{tab_id}-{rid}"
        body += (f'<tr class="j{best}" onclick="tog(\'{did}\')">'
                 f'<td class="route">▸ {_esc(lbl)}</td><td>{_esc(r.get("depart"))}</td>'
                 f'<td>{_esc(r.get("arrivee"))}</td><td>{_esc(dur)}</td>'
                 f'<td class="c">{_esc(r.get("correspondances"))}</td>'
                 f'<td class="tr">{_esc(r.get("trains") or "—")}</td>'
                 f'<td class="price">{pcell}</td></tr>'
                 f'<tr id="{did}" class="detail"><td colspan="7">{_detail_html(r)}</td></tr>')
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _city_from_station(label):
    """Mappe un libellé de gare ('Paris - Gare De Lyon') vers une ville connue de CITY_COORDS."""
    if not label:
        return None
    base = str(label).split(" - ")[0].split("(")[0].strip()
    for k in CITY_COORDS:
        if k.lower() == base.lower():
            return k
    for k in CITY_COORDS:
        if k.lower() in base.lower():
            return k
    return None


def _journey_cities(row):
    """Suite des villes traversées par un trajet (pour la carte)."""
    if row.get("route"):
        cities = [c.strip() for c in str(row["route"]).split("→")]
    else:
        segs = [s for s in (row.get("segments") or []) if s.get("kind") == "train"]
        if segs:
            cities = [_city_from_station(segs[0]["de"])] + [_city_from_station(s["a"]) for s in segs]
        else:
            cities = [row.get("origine"), row.get("destination")]
    out = []
    for c in cities:                                     # canonise + retire les doublons consécutifs
        c = c if c in CITY_COORDS else _city_from_station(c)
        if c and (not out or out[-1] != c):
            out.append(c)
    return out


def _france_map_svg(routes):
    """SVG d'une carte de France : réseau TGV en fond + trajets animés (train en mouvement)."""
    LON, LAT, W, H = (-5.2, 8.7), (41.3, 51.2), 560, 600

    def proj(lat, lon):
        return ((lon - LON[0]) / (LON[1] - LON[0]) * W,
                (LAT[1] - lat) / (LAT[1] - LAT[0]) * H)

    bpts = [proj(lat, lon) for lat, lon in FRANCE_BORDER]
    border = ('<path class="border" d="M ' + " L ".join(f"{x:.0f} {y:.0f}" for x, y in bpts)
              + ' Z"/>')

    edges, seen = "", set()
    for a, nbrs in TGV_GRAPH.items():
        if a not in CITY_COORDS:
            continue
        ax, ay = proj(*CITY_COORDS[a])
        for b in nbrs:
            if b not in CITY_COORDS or tuple(sorted((a, b))) in seen:
                continue
            seen.add(tuple(sorted((a, b))))
            bx, by = proj(*CITY_COORDS[b])
            edges += f'<line x1="{ax:.0f}" y1="{ay:.0f}" x2="{bx:.0f}" y2="{by:.0f}" class="edge"/>'

    on = set()
    for cities, _c, _lbl in routes:
        on |= set(cities)
    dots = ""
    for c, (lat, lon) in CITY_COORDS.items():
        x, y = proj(lat, lon)
        if c in on:
            dots += (f'<circle cx="{x:.0f}" cy="{y:.0f}" r="5" class="city on"/>'
                     f'<text x="{x + 9:.0f}" y="{y + 4:.0f}" class="lbl">{_esc(c)}</text>')
        else:
            dots += f'<circle cx="{x:.0f}" cy="{y:.0f}" r="2.5" class="city"/>'

    paths = ""
    for i, (cities, color, _lbl) in enumerate(routes):
        pts = [proj(*CITY_COORDS[c]) for c in cities if c in CITY_COORDS]
        if len(pts) < 2:
            continue
        d = "M " + " L ".join(f"{x:.0f} {y:.0f}" for x, y in pts)
        paths += (f'<path id="route{i}" d="{d}" class="route" style="stroke:{color}"/>'
                  f'<circle r="5.5" fill="{color}" class="train">'
                  f'<animateMotion dur="3.6s" repeatCount="indefinite" path="{d}"/></circle>')

    leg = "".join(f'<span><i style="background:{c}"></i>{_esc(lbl)}</span>'
                  for _cities, c, lbl in routes)
    return (f'<div class="map"><svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet">'
            f'{border}{edges}{paths}{dots}</svg><div class="legend">{leg}</div></div>')


def to_html_report(aller_rows, path="resultats.html", title="Trajets SNCF Connect", retour_rows=None):
    """Rapport HTML autonome : onglets Aller/Retour, prix A/R, lignes dépliables (n° trains…)."""
    from datetime import datetime as _dt

    has_retour = retour_rows is not None
    ba, pca = _cheapest(aller_rows or [])
    br, pcb = _cheapest(retour_rows or [])

    # bandeau prix
    if has_retour and ba and br:
        ta, tb_ = _price_of(ba, pca), _price_of(br, pcb)
        hero = (f'<div class="hero"><div class="hero-price">{ta + tb_:.2f} €</div>'
                f'<div class="hero-info">🏆 Meilleur aller-retour'
                f'<br>🛫 Aller {_esc(ba["date"])} · {_esc(ba["depart"])}→{_esc(ba["arrivee"])} '
                f'· <b>{ta:.2f} €</b>'
                f'<br>🛬 Retour {_esc(br["date"])} · {_esc(br["depart"])}→{_esc(br["arrivee"])} '
                f'· <b>{tb_:.2f} €</b></div></div>')
    elif ba:
        lbl = ba["route"] if "route" in ba else f'{ba.get("origine")} → {ba.get("destination")}'
        hero = (f'<div class="hero"><div class="hero-price">{_price_of(ba, pca):.2f} €</div>'
                f'<div class="hero-info">🏆 Moins cher — {_esc(ba["date"])}<br>'
                f'<b>{_esc(lbl)}</b> · {_esc(ba["depart"])}→{_esc(ba["arrivee"])}</div></div>')
    else:
        hero = ""

    # carte de France : trajet le moins cher (aller en vert, retour en orange)
    map_routes = []
    if ba:
        ca = _journey_cities(ba)
        if len(ca) >= 2:
            map_routes.append((ca, "#37d4a7", "🛫 Aller"))
    if has_retour and br:
        cr = _journey_cities(br)
        if len(cr) >= 2:
            map_routes.append((cr, "#f4a13c", "🛬 Retour"))
    map_html = _france_map_svg(map_routes) if map_routes else ""

    if has_retour:
        tabs = ('<div class="tabs"><button class="tab active" id="tab-aller" '
                'onclick="showTab(\'aller\')">🛫 Aller</button>'
                '<button class="tab" id="tab-retour" onclick="showTab(\'retour\')">🛬 Retour</button></div>'
                f'<div id="aller" class="pane active">{_table_html(aller_rows or [], "a")}</div>'
                f'<div id="retour" class="pane">{_table_html(retour_rows or [], "r")}</div>')
        n = len(aller_rows or []) + len(retour_rows or [])
    else:
        tabs = f'<div class="pane active">{_table_html(aller_rows or [], "a")}</div>'
        n = len(aller_rows or [])

    generated = _dt.now().strftime("%d/%m/%Y %H:%M")
    doc = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>
:root{{--card:#1a1f3a;--ink:#e8ebff;--mut:#9aa3c7;--acc:#37d4a7;--best:#23314a}}
*{{box-sizing:border-box}}body{{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;
background:linear-gradient(160deg,#0f1226,#161a33);color:var(--ink);padding:24px}}
.wrap{{max-width:1040px;margin:0 auto}}h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:18px}}
.hero{{display:flex;align-items:center;gap:18px;background:var(--card);border:1px solid #2a3160;
border-radius:14px;padding:18px 22px;margin-bottom:18px}}
.hero-price{{font-size:34px;font-weight:800;color:var(--acc);white-space:nowrap}}
.hero-info{{font-size:14px;line-height:1.6}}
.tabs{{display:flex;gap:8px;margin-bottom:12px}}
.tab{{background:var(--card);color:var(--mut);border:1px solid #2a3160;border-radius:10px;
padding:9px 18px;font-size:14px;font-weight:600;cursor:pointer}}
.tab.active{{color:var(--ink);border-color:var(--acc);background:#202a4d}}
.pane{{display:none}}.pane.active{{display:block}}
table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:14px;overflow:hidden}}
th,td{{padding:10px 12px;text-align:left;font-size:13.5px;border-bottom:1px solid #232a52}}
th{{background:#222a52;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.date-row td{{background:#11152e;color:var(--mut);font-weight:700;font-size:12.5px}}
td.c{{text-align:center}}.route{{font-weight:600}}.tr{{color:var(--mut);font-size:12px}}
tr.j{{cursor:pointer}}tr.j:hover td{{background:#202750}}
tr.best td{{background:var(--best)}}
td.price{{position:relative;min-width:150px}}
.bar{{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,#1f6f57,#37d4a7);opacity:.30}}
.ptxt{{position:relative;font-weight:700}}.na{{color:#ff8da1;font-style:italic}}
tr.detail{{display:none}}tr.detail.open{{display:table-row}}
tr.detail td{{background:#0d1124;padding:12px 18px}}
.seg{{padding:6px 0;border-left:3px solid var(--acc);padding-left:12px;margin:4px 0;font-size:13px}}
.tno{{font-weight:700;color:var(--acc)}}.ttype{{color:var(--mut);margin-left:8px;font-size:12px}}
.corresp{{color:var(--mut);font-size:12.5px;font-style:italic;padding:3px 0 3px 12px}}
.muted{{color:var(--mut)}}
.map{{background:radial-gradient(120% 120% at 50% 10%,#171d3c,#0e1228);border:1px solid #2a3160;
border-radius:14px;padding:10px;margin-bottom:18px;position:relative}}
.map svg{{width:100%;height:auto;max-height:560px;display:block}}
.border{{fill:rgba(70,95,165,.10);stroke:#4a5a93;stroke-width:1.5;stroke-linejoin:round}}
.edge{{stroke:#2b3361;stroke-width:1}}
.city{{fill:#56619c}}.city.on{{fill:var(--acc);filter:drop-shadow(0 0 6px var(--acc))}}
.lbl{{fill:#dfe4ff;font-size:12px;font-weight:600;paint-order:stroke;stroke:#0e1228;stroke-width:3px}}
.route{{fill:none;stroke-width:3.5;stroke-linecap:round;stroke-linejoin:round;
stroke-dasharray:9 9;animation:flow 1s linear infinite;filter:drop-shadow(0 0 4px rgba(55,212,167,.5))}}
@keyframes flow{{to{{stroke-dashoffset:-36}}}}
.train{{filter:drop-shadow(0 0 6px #fff)}}
.legend{{position:absolute;top:14px;left:14px;font-size:12px;color:var(--mut);display:flex;gap:14px}}
.legend span{{display:flex;align-items:center;gap:6px}}
.legend i{{width:14px;height:4px;border-radius:2px;display:inline-block}}
.foot{{color:var(--mut);font-size:12px;margin-top:14px;text-align:center}}
</style></head><body><div class="wrap">
<h1>🚆 {_esc(title)}</h1>
<div class="sub">{n} trajets · généré le {generated} · clique une ligne pour le détail</div>
{hero}{map_html}{tabs}
<div class="foot">Prix « dès », 2de classe · source SNCF Connect (non officiel)</div>
</div><script>
function tog(id){{document.getElementById(id).classList.toggle('open');}}
function showTab(id){{
document.querySelectorAll('.pane').forEach(p=>p.classList.remove('active'));
document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
document.getElementById(id).classList.add('active');
document.getElementById('tab-'+id).classList.add('active');}}
</script></body></html>"""
    Path(path).write_text(doc, encoding="utf-8")
    return path


def _date_range(debut, fin):
    d0 = datetime.strptime(debut, "%Y-%m-%d")
    d1 = datetime.strptime(fin or debut, "%Y-%m-%d")
    out = []
    while d0 <= d1:
        out.append(d0.strftime("%Y-%m-%d"))
        d0 += timedelta(days=1)
    return out


def _cli():
    import argparse
    import pandas as pd

    ap = argparse.ArgumentParser(
        description="Comparateur de prix SNCF Connect (parallèle, carte, pagination, split TGV).",
        epilog="Exemples :\n"
               "  python sncf_scraper.py Brest Bourg-en-Bresse --debut 2026-07-10 --fin 2026-07-12 --carte-jeune\n"
               "  python sncf_scraper.py Brest Bourg-en-Bresse --dates 2026-07-11 --split\n"
               "  python sncf_scraper.py Paris Lyon --debut 2026-07-11 --pages 3 --csv prix.csv",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("origine", help="ville de départ (ex. Brest)")
    ap.add_argument("destination", help="ville d'arrivée (ex. Bourg-en-Bresse)")
    ap.add_argument("--debut", help="date de début AAAA-MM-JJ")
    ap.add_argument("--fin", help="date de fin AAAA-MM-JJ (défaut = début)")
    ap.add_argument("--dates", nargs="+", help="dates explicites AAAA-MM-JJ (au lieu de --debut/--fin)")
    ap.add_argument("--carte-jeune", action="store_true", help="appliquer la Carte Avantage Jeune")
    ap.add_argument("--heure", default="06h", help="heure de départ de référence (06h, 08h… défaut 06h)")
    ap.add_argument("--pages", type=int, default=2, help="pagination : trains/jour (défaut 2 ; 3 = journée)")
    ap.add_argument("--concurrency", type=int, default=5, help="onglets simultanés par navigateur (défaut 5)")
    ap.add_argument("--browsers", type=int, default=1, help="nb de navigateurs parallèles (copies du profil)")
    ap.add_argument("--split", action="store_true", help="trajets fractionnés via graphe TGV")
    ap.add_argument("--max-h", type=float, default=14.0, help="durée totale max en heures (split, défaut 14)")
    ap.add_argument("--retour", action="store_true", help="chercher aussi le retour (sens inverse)")
    ap.add_argument("--retour-dates", nargs="+", help="dates du retour (défaut = mêmes dates)")
    ap.add_argument("--retour-debut", help="date de début du retour AAAA-MM-JJ")
    ap.add_argument("--retour-fin", help="date de fin du retour AAAA-MM-JJ")
    ap.add_argument("--csv", help="exporter le résultat dans ce fichier CSV")
    ap.add_argument("--html", nargs="?", const="resultats.html",
                    help="générer un rapport HTML (défaut resultats.html)")
    args = ap.parse_args()

    if args.dates:
        dates = args.dates
    elif args.debut:
        dates = _date_range(args.debut, args.fin)
    else:
        ap.error("préciser --dates ou --debut [--fin]")

    want_retour = args.retour or args.retour_dates or args.retour_debut
    if args.retour_dates:
        rdates = args.retour_dates
    elif args.retour_debut:
        rdates = _date_range(args.retour_debut, args.retour_fin)
    else:
        rdates = dates

    async def do(o, d, dts):
        if args.split:
            return await auto_split_dates(o, d, dts, carte_jeune=args.carte_jeune,
                heure=args.heure, max_pages=args.pages, max_total_h=args.max_h,
                concurrency=args.concurrency, n_browsers=args.browsers)
        return await compare_dates(o, d, dts, carte_jeune=args.carte_jeune,
            concurrency=args.concurrency, max_pages=args.pages, n_browsers=args.browsers)

    async def run():
        aller = await do(args.origine, args.destination, dates)
        retour = await do(args.destination, args.origine, rdates) if want_retour else []
        return aller, retour

    aller, retour = asyncio.run(run())
    pcol = "prix_total" if args.split else "prix_eur"
    cols = (["date", "route", "prix_total", "depart", "arrivee", "duree_totale", "correspondances"]
            if args.split else
            ["date", "origine", "destination", "depart", "arrivee", "duree", "correspondances",
             "trains", "prix_eur"])
    pd.set_option("display.width", 240)
    pd.set_option("display.max_colwidth", 120)

    def show(rows, label):
        if not rows:
            print(f"\n{label} : aucun résultat."); return None
        df = pd.DataFrame(rows)
        c = [x for x in cols if x in df.columns]
        df = df.sort_values(["date", pcol]).reset_index(drop=True)
        print(f"\n===== {label} =====")
        print(df[c].to_string(index=False))
        cheap = df.loc[df[pcol].notna()].nsmallest(1, pcol)
        return cheap.iloc[0] if not cheap.empty else None

    ba = show(aller, "ALLER")
    br = show(retour, "RETOUR") if want_retour else None
    if ba is not None:
        lbl = ba["route"] if args.split else f'{ba["depart"]}→{ba["arrivee"]}'
        print(f"\n💸 Aller le moins cher : {ba[pcol]} € — {ba['date']} — {lbl}")
    if br is not None:
        lbl = br["route"] if args.split else f'{br["depart"]}→{br["arrivee"]}'
        print(f"💸 Retour le moins cher : {br[pcol]} € — {br['date']} — {lbl}")
    if ba is not None and br is not None:
        print(f"🎫 Meilleur aller-retour : {ba[pcol] + br[pcol]:.2f} €")

    if args.csv:
        rows = [{**r, "sens": "aller"} for r in aller] + [{**r, "sens": "retour"} for r in retour]
        out = pd.DataFrame(rows)
        if "segments" in out.columns:
            out = out.drop(columns=["segments"])
        out.to_csv(args.csv, index=False)
        print(f"\nCSV : {args.csv}")
    if args.html:
        to_html_report(aller, path=args.html, title=f"{args.origine} ⇄ {args.destination}",
                       retour_rows=(retour if want_retour else None))
        print(f"HTML : {args.html}")


if __name__ == "__main__":
    _cli()
