#!/usr/bin/env python3
"""
CROUS Watcher — surveille les nouvelles annonces de logement en phase
complémentaire sur trouverunlogement.lescrous.fr pour Paris intramuros,
et envoie un email dès qu'une nouvelle annonce apparaît.

Le site étant une application JavaScript (Remix / turbo-stream), ce script
utilise Playwright (navigateur automatisé headless) pour charger la page
comme le ferait un vrai navigateur, plutôt que de parser une API interne
difficile à décoder.
"""

import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ============================== CONFIG ==================================

PARIS_BOUNDS = "2.224122_48.902156_2.4697602_48.8155755"
SEARCH_URL = (
    f"https://trouverunlogement.lescrous.fr/tools/47/search"
    f"?bounds={PARIS_BOUNDS}&locationName=Paris"
)

CHECK_INTERVAL_SECONDS = 5 * 60

SEEN_FILE = Path(__file__).parent / "logements_vus.json"
SCREENSHOT_FILE = Path(__file__).parent / "debug_screenshot.png"
DEBUG_HTML_FILE = Path(__file__).parent / "dernier_html_debug.html"


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


SMTP_HOST = env_or_default("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(env_or_default("SMTP_PORT", "587"))
SENDER_EMAIL = env_or_default("SENDER_EMAIL", "votre_adresse@gmail.com")
SENDER_PASSWORD = env_or_default("SENDER_PASSWORD", "votre_mot_de_passe_application")
RECEIVER_EMAIL = env_or_default("RECEIVER_EMAIL", "votre_adresse@gmail.com")

# ==========================================================================


def fetch_rendered_html() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="fr-FR",
        )
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(3000)
        html = page.content()
        page.screenshot(path=str(SCREENSHOT_FILE), full_page=True)
        browser.close()
        return html


def parse_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    candidate_selectors = [
        "div.fr-tile",
        "article.fr-tile",
        "div.fr-card",
        "article.fr-card",
        "div[class*='logement']",
        "article[class*='logement']",
        "div[class*='result-item']",
        "li[class*='result']",
        "div[class*='card']",
        "a[href*='/tools/47/']",
    ]

    cards = []
    for selector in candidate_selectors:
        found = soup.select(selector)
        found = [c for c in found if len(c.get_text(strip=True)) > 15]
        if found:
            cards = found
            break

    for card in cards:
        # Liens génériques de navigation à ignorer (pas de vraies fiches logement)
    NAV_PATHS = {"/tools/47/search", "/tools/47/cart", "/tools/47", "search", "cart"}

    for card in cards:
        text = " ".join(card.get_text(" ", strip=True).split())
        if not text:
            continue

        link_tag = card if card.name == "a" else card.find("a", href=True)
        link = link_tag["href"] if link_tag and link_tag.has_attr("href") else None
        if link and link.startswith("/"):
            link = "https://trouverunlogement.lescrous.fr" + link

        # On ignore les liens de nav générale (pas de vraie fiche logement)
        if link and any(link.rstrip("/").endswith(p) for p in NAV_PATHS):
            continue

        price_match = re.search(r"(\d[\d\s]*,?\d*)\s*€", text)
        if not price_match:
            # Pas de prix détecté = très probablement pas une vraie annonce, on ignore
            continue
        price = price_match.group(0)

        uid = link or str(hash(text))

        listings.append({
            "id": uid,
            "text": text[:300],
            "price": price,
            "link": link or SEARCH_URL,
        })

    unique = {item["id"]: item for item in listings}
    return list(unique.values())
    unique = {item["id"]: item for item in listings}
    return list(unique.values())


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def send_email(subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [RECEIVER_EMAIL], msg.as_string())


def notify_new_listings(new_listings: list[dict]) -> None:
    lines = [f"{len(new_listings)} nouvelle(s) annonce(s) CROUS Paris intramuros :\n"]
    for item in new_listings:
        lines.append(f"- {item['price']} — {item['text']}\n  Lien : {item['link']}\n")
    body = "\n".join(lines)
    print(body)
    try:
        send_email("🏠 Nouvelle(s) annonce(s) CROUS Paris", body)
        print("[OK] Email envoyé.")
    except Exception as exc:
        print(f"[ERREUR] Échec d'envoi de l'email : {exc}")


def check_once(seen: set) -> set:
    try:
        html = fetch_rendered_html()
    except Exception as exc:
        print(f"[ERREUR] Impossible de charger la page : {exc}")
        return seen

    DEBUG_HTML_FILE.write_text(html, encoding="utf-8")
    listings = parse_listings(html)

    if not listings:
        print(
            "[ATTENTION] Aucune annonce détectée — soit il n'y en a réellement "
            "aucune, soit les sélecteurs HTML doivent être ajustés. "
            f"Voir la capture d'écran ({SCREENSHOT_FILE.name}) en pièce jointe du run GitHub Actions."
        )
        return seen

    new_ids = {item["id"] for item in listings} - seen
    new_listings = [item for item in listings if item["id"] in new_ids]

    if new_listings:
        notify_new_listings(new_listings)
    else:
        print(f"[{time.strftime('%H:%M:%S')}] Aucune nouvelle annonce ({len(listings)} au total).")

    seen |= {item["id"] for item in listings}
    save_seen(seen)
    return seen


def main() -> None:
    if "--test" in sys.argv:
        print("Envoi d'un email de test...")
        send_email("Test CROUS Watcher", "Ceci est un email de test. La configuration SMTP fonctionne !")
        print("Email de test envoyé avec succès.")
        return

    if "--once" in sys.argv:
        seen = load_seen()
        check_once(seen)
        return

    print(f"Surveillance de : {SEARCH_URL}")
    print(f"Vérification toutes les {CHECK_INTERVAL_SECONDS // 60} minutes. Ctrl+C pour arrêter.\n")

    seen = load_seen()
    while True:
        seen = check_once(seen)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nArrêt du script.")
