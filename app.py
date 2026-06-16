"""
Petite appli web locale : un formulaire (villes, dates, options) -> rapport HTML.

Lancer :  python app.py   puis ouvrir http://127.0.0.1:5000
⚠️ La recherche ouvre une fenêtre Chromium VISIBLE (DataDome) et prend ~30-90 s.
Prérequis identiques au scraper : IP résidentielle/mobile + profil DataDome déjà validé.
"""
import asyncio

from flask import Flask, request

import sncf_scraper as s

app = Flask(__name__)

FORM = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SNCF — Recherche</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;
background:linear-gradient(160deg,#0f1226,#161a33);color:#e8ebff;padding:32px;min-height:100vh}
.card{max-width:560px;margin:0 auto;background:#1a1f3a;border:1px solid #2a3160;border-radius:16px;padding:26px}
h1{font-size:22px;margin:0 0 4px}.sub{color:#9aa3c7;font-size:13px;margin-bottom:20px}
label{display:block;font-size:13px;color:#9aa3c7;margin:14px 0 5px}
input[type=text],input[type=date],input[type=number]{width:100%;padding:10px 12px;border-radius:9px;
border:1px solid #2a3160;background:#11152e;color:#e8ebff;font-size:14px}
.row{display:flex;gap:12px}.row>div{flex:1}
.chk{display:flex;align-items:center;gap:8px;margin-top:14px;font-size:14px;color:#e8ebff}
.chk input{width:auto}
button{margin-top:22px;width:100%;padding:13px;border:0;border-radius:10px;cursor:pointer;
background:linear-gradient(90deg,#1f6f57,#37d4a7);color:#06231b;font-weight:800;font-size:15px}
.hint{color:#9aa3c7;font-size:12px;margin-top:14px;line-height:1.5}
#load{display:none;position:fixed;inset:0;background:#0b0e1fd0;align-items:center;justify-content:center;
flex-direction:column;gap:14px;font-size:16px}
.sp{width:42px;height:42px;border:4px solid #2a3160;border-top-color:#37d4a7;border-radius:50%;
animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
fieldset{border:1px solid #2a3160;border-radius:10px;margin-top:16px;padding:8px 14px 14px}
legend{color:#9aa3c7;font-size:12px;padding:0 6px}
</style></head><body>
<div id="load"><div class="sp"></div><div>Recherche en cours… une fenêtre Chrome va s'ouvrir.<br>
<span style="color:#9aa3c7;font-size:13px">~30 à 90 s selon le nombre de dates.</span></div></div>
<div class="card">
<h1>🚆 Recherche SNCF Connect</h1>
<div class="sub">Compare les prix, avec carte, retour, et trajets fractionnés.</div>
<form method="post" action="/search" onsubmit="document.getElementById('load').style.display='flex'">
  <div class="row">
    <div><label>Ville de départ</label><input type="text" name="origine" value="Brest" required></div>
    <div><label>Ville d'arrivée</label><input type="text" name="destination" value="Bourg-en-Bresse" required></div>
  </div>
  <div class="row">
    <div><label>Aller — du</label><input type="date" name="debut" required></div>
    <div><label>au (optionnel)</label><input type="date" name="fin"></div>
  </div>
  <label class="chk"><input type="checkbox" name="carte" checked> Carte Avantage Jeune</label>
  <fieldset><legend>Retour (optionnel)</legend>
    <label class="chk"><input type="checkbox" name="retour"> Chercher aussi le retour</label>
    <div class="row" style="margin-top:8px">
      <div><label>Retour — du</label><input type="date" name="retour_debut"></div>
      <div><label>au</label><input type="date" name="retour_fin"></div>
    </div>
  </fieldset>
  <label class="chk"><input type="checkbox" name="split"> Trajets fractionnés (graphe TGV)</label>
  <div class="row" style="margin-top:6px">
    <div><label>Trains/jour (pagination)</label><input type="number" name="pages" value="2" min="0" max="6"></div>
    <div><label>Navigateurs parallèles</label><input type="number" name="browsers" value="1" min="1" max="4"></div>
  </div>
  <button type="submit">Rechercher</button>
</form>
<div class="hint">⚠️ Une fenêtre Chrome visible s'ouvre (anti-bot DataDome). Nécessite une IP
résidentielle/mobile et le profil DataDome déjà validé une fois.</div>
</div></body></html>"""


def _dates(debut, fin, listed):
    if listed:
        return [d for d in listed.replace(",", " ").split() if d]
    if debut:
        return s._date_range(debut, fin or debut)
    return []


@app.route("/")
def index():
    return FORM


@app.route("/search", methods=["POST"])
def search():
    f = request.form
    origine = (f.get("origine") or "").strip()
    destination = (f.get("destination") or "").strip()
    dates = _dates(f.get("debut"), f.get("fin"), f.get("dates"))
    if not origine or not destination or not dates:
        return "<p>Champs manquants. <a href='/'>Retour</a></p>", 400

    carte = "carte" in f
    split = "split" in f
    retour = "retour" in f
    pages = int(f.get("pages") or 2)
    browsers = int(f.get("browsers") or 1)
    maxh = float(f.get("maxh") or 14)
    rdates = _dates(f.get("retour_debut"), f.get("retour_fin"), f.get("retour_dates")) or dates

    async def do(o, d, dts):
        if split:
            return await s.auto_split_dates(o, d, dts, carte_jeune=carte, max_pages=pages,
                                            concurrency=5, n_browsers=browsers, max_total_h=maxh)
        return await s.compare_dates(o, d, dts, carte_jeune=carte, max_pages=pages,
                                     concurrency=5, n_browsers=browsers)

    async def run():
        aller = await do(origine, destination, dates)
        ret = await do(destination, origine, rdates) if retour else []
        return aller, ret

    aller, ret = asyncio.run(run())
    s.to_html_report(aller, path="resultats.html", title=f"{origine} ⇄ {destination}",
                     retour_rows=(ret if retour else None))
    html = open("resultats.html", encoding="utf-8").read()
    # injecte un lien "nouvelle recherche"
    html = html.replace('<div class="wrap">',
                        '<div class="wrap"><a href="/" style="color:#37d4a7;text-decoration:none;'
                        'font-size:13px">↩ Nouvelle recherche</a>', 1)
    return html


if __name__ == "__main__":
    # threaded=False : une recherche à la fois (le profil navigateur ne supporte qu'un accès)
    app.run(host="127.0.0.1", port=5000, threaded=False)
