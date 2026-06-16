"""
Revalidation DataDome : ouvre sncf-connect.com dans le profil persistant (fenêtre visible).
Si un captcha/slider apparaît, résous-le À LA MAIN dans la fenêtre -> le cookie est rafraîchi
et réutilisé par les recherches suivantes.

Lancer :  python warmup.py
Astuce anti-blocage : être sur une IP résidentielle/mobile, et éviter --browsers >1 (plusieurs
navigateurs avec le même cookie + même IP = DataDome se braque).
"""
import asyncio

from playwright.async_api import async_playwright

import sncf_scraper as s

try:
    from playwright_stealth import Stealth
    _CM = lambda: Stealth().use_async(async_playwright())
except Exception:
    _CM = async_playwright


BLOCK = ("Accès temporairement restreint", "sécuriser votre accès", "s'adresse bien à vous")


async def main():
    async with _CM() as p:
        ctx = await p.chromium.launch_persistent_context(
            s.PROFILE_DIR, headless=False, user_agent=s.UA, locale="fr-FR",
            timezone_id="Europe/Paris", viewport={"width": 1366, "height": 900},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto("https://www.sncf-connect.com/", wait_until="domcontentloaded", timeout=60000)
        print("Fenêtre ouverte. Si un captcha/slider s'affiche, résous-le à la main…")
        ok = False
        for i in range(72):                      # ~6 min max
            await asyncio.sleep(5)
            body = (await pg.inner_text("body"))[:300]
            if any(b in body for b in BLOCK):
                print(f"[{i * 5:>3}s] toujours bloqué / captcha à résoudre…")
            else:
                print("✅ Accès OK — cookie DataDome rafraîchi. Tu peux relancer une recherche.")
                ok = True
                await asyncio.sleep(2)
                break
        await ctx.close()
        if not ok:
            print("❌ Toujours bloqué. Change d'IP (mode avion 10 s sur le tél, ou re-partage de "
                  "connexion) puis relance warmup.py.")


if __name__ == "__main__":
    asyncio.run(main())
