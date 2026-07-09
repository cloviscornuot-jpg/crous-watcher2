#!/usr/bin/env python3
"""
CROUS Watcher — surveille les nouvelles annonces de logement en phase
complémentaire sur trouverunlogement.lescrous.fr pour Paris intramuros,
et envoie un email dès qu'une nouvelle annonce apparaît.
"""

import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ============================== CONFIG ==================================

SEARCH_URL = "https://trouverunlogement.lescrous.fr/tools/47/search"

# Ne garde que les logements dont l'adresse contient un code postal
# parisien (75001 à 75020). Passez à False pour surveiller toute la France.
FILTER_PARIS_ONLY = True
PARIS_POSTCODE_REGEX = re.compile(r"\b75(0[1-9]|1[0-9]|20)\b")

CHECK_INTERVAL_SECONDS = 5 * 60

SEEN_FILE = Path(__file__).parent / "logements_vus.json"
DEBUG_HTML_FILE = Path(__file__).parent / "dernier_html_debug.html"


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


SMTP_HOST = env_or_default("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(env_or_default("SMTP_PORT", "587"))
SENDER_EMAIL = env_or_default("SENDER_EMAIL", "votre_adresse@gmail.com")
SENDER_PASSWORD = env_or_default("SENDER_PASSWORD", "votre_mot_de_passe_application")
RECEIVER_EMAIL = env_or_default("RECEIVER_EMAIL", "votre_adresse@gmail.com")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# ==========================================================================


def fetch_page(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.text


def get_total_pages(html: str) -> int:
    match = re.search(r"page\s+\d+\s+sur\s+(\d+)", html, re.IGNORECASE)
    return int(match.group(1)) if match else 1


def fetch_all_pages_html() -> list[str]:
    first_html = fetch_page(SEARCH_URL)
    total_pages = max(1, get_total_pages(first_html))
    pages_html = [first_html]

    for page_num in range(2, total_pages + 1):
        time.sleep(1)
        try:
            pages_html.append(fetch_page(f"{SEARCH_URL}?page={page_num}"))
        except requests.RequestException as exc:
            print(f"[ATTENTION] Échec récupération page {page_num} : {exc}")
            break

    return pages_html


def parse_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    accommodation_links = soup.find_all("a", href=re.compile(r"/tools/\d+/accommodations/\d+"))

    for link_tag in accommodation_links:
        href = link_tag["href"]
        if href.startswith("/"):
            href = "https://trouverunlogement.lescrous.fr" + href

        container = link_tag
        text = ""
        for _ in range(8):
            if container.parent is None:
                break
            container = container.parent
            candidate_text = " ".join(container.get_text(" ", strip=True).split())
            if "€" in candidate_text:
                text = candidate_text
                break

        if not text:
            text = " ".join(link_tag.get_text(" ", strip=True).split())

        price_match = re.search(r"(\d[\d\s]*,?\d*)\s*€", text)
        price = price_match.group(0) if price_match else "prix non trouvé"

        listings.append({
            "id": href,
            "text": text[:300],
            "price": price,
            "link": href,
        })

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
    lines = [f"{len(new_listings)} nouvelle(s) annonce(s) CROUS à Paris :\n"]
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
        pages_html = fetch_all_pages_html()
    except requests.RequestException as exc:
        print(f"[ERREUR] Impossible de récupérer la page : {exc}")
        return seen

    DEBUG_HTML_FILE.write_text(pages_html[0], encoding="utf-8")

    listings = []
    for page_html in pages_html:
        listings.extend(parse_listings(page_html))
    listings = list({item["id"]: item for item in listings}.values())

    if FILTER_PARIS_ONLY:
        listings = [item for item in listings if PARIS_POSTCODE_REGEX.search(item["text"])]

    if not listings:
        print(
            "[ATTENTION] Aucune annonce détectée pour Paris — c'est peut-être "
            "simplement qu'il n'y en a réellement aucune en ce moment."
        )
        return seen

    new_ids = {item["id"] for item in listings} - seen
    new_listings = [item for item in listings if item["id"] in new_ids]

    if new_listings:
        notify_new_listings(new_listings)
    else:
        print(f"[{time.strftime('%H:%M:%S')}] Aucune nouvelle annonce ({len(listings)} au total à Paris).")

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
