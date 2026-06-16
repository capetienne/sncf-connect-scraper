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
from dataclasses import dataclass, asdict
from datetime import datetime
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

    def as_dict(self):
        return asdict(self)


async def _sleep(a=0.4, b=1.1):
    await asyncio.sleep(random.uniform(a, b))


# --------------------------------------------------------------------------- #
# Parsing de la réponse API `bff/api/v1/itineraries`
# --------------------------------------------------------------------------- #
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
        out.append(Journey(
            date=date_iso, origine=origine, destination=destination,
            depart=dep, arrivee=arr, duree=dur, correspondances=corr,
            prix_eur=price, reservable=price is not None,
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
    seen = {}
    for j in journeys:
        seen[(j.depart, j.arrivee)] = j
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
                      concurrency=3, timeout_ms=60000, heure="06h", max_pages=2) -> list[dict]:
    """Lance PLUSIEURS recherches en parallèle (onglets) dans un seul navigateur.

    queries : liste de (origine, destination, date). Renvoie une liste de dicts.
    Un seul contexte persistant = un seul cookie DataDome partagé ; `concurrency` onglets max.
    """
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


async def compare_dates(origine, destination, dates, headless=False,
                        carte_jeune=False, parallel=True, concurrency=3, max_pages=2) -> list[dict]:
    """Recherche pour plusieurs dates. `parallel=True` -> onglets simultanés (rapide)."""
    queries = [(origine, destination, d) for d in dates]
    if parallel:
        return await search_many(queries, headless=headless, carte_jeune=carte_jeune,
                                 concurrency=concurrency, max_pages=max_pages)
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
                       concurrency=4, headless=False, max_pages=2) -> list[dict]:
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


if __name__ == "__main__":
    res = asyncio.run(compare_dates(
        "Brest", "Bourg-en-Bresse",
        ["2026-07-10", "2026-07-11", "2026-07-12"],
    ))
    import pandas as pd
    df = pd.DataFrame(res)
    print(df.to_string(index=False) if not df.empty else "Aucun résultat.")
