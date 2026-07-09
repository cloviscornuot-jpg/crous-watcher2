#!/usr/bin/env python3
"""
CROUS Watcher — surveille les nouvelles annonces de logement en phase
complémentaire sur trouverunlogement.lescrous.fr pour Paris intramuros,
et envoie un email dès qu'une nouvelle annonce apparaît.

Installation :
    pip install requests beautifulsoup4

Configuration :
    Remplissez la section CONFIG ci-dessous, puis lancez :
        python crous_watcher.py --test     # envoie un email de test
        python crous_watcher.py            # lance la surveillance en continu

Astuce Gmail : utilisez un "mot de passe d'application" (pas votre mot de
passe normal) -> https://myaccount.google.com/apppasswords
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

# Bounding box Paris intramuros (format lon_lat_lon_lat : coin NO puis coin SE)
# Vous pouvez recalculer ce paramètre vous-même en allant sur le site,
# en filtrant la carte sur la zone souhaitée, et en copiant le "bounds=..."
# présent dans l'URL de la page.
PARIS_BOUNDS = "2.2241_48.9021_2.4699_48.8156"

# tools/45 = logements pour l'année universitaire 2026-2027 (à vérifier :
# ouvrez le site, faites une recherche, et copiez le bon numéro de "tools/XX"
# si jamais le CROUS a changé l'identifiant entre-temps).
SEARCH_URL = f"https://trouverunlogement.lescrous.fr/tools/45/search?bounds={PARIS_BOUNDS}"

CHECK_INTERVAL_SECONDS = 5 * 60  # 5 minutes

SEEN_FILE = Path(__file__).parent / "logements_vus.json"
DEBUG_HTML_FILE = Path(__file__).parent / "dernier_html_debug.html"

# --- Email (SMTP) ---
# Ces valeurs peuvent être surchargées par des variables d'environnement
# (utilisé automatiquement par le workflow GitHub Actions via les Secrets).
# Pour un usage 100% local, vous pouvez aussi remplir directement les
# valeurs par défaut ci-dessous.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "votre_adresse@gmail.com")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "votre_mot_de_passe_application")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "votre_adresse@gmail.com")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# ==========================================================================


def fetch_page(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.text


def parse_listings(html: str) -> list[dict]:
    """
    Extrait les annonces de logement de la page.

    Le site utilise vraisemblablement le Système de Design de l'État (DSFR),
    on essaie donc plusieurs sélecteurs courants. Si aucun ne fonctionne,
    on sauvegarde le HTML brut dans DEBUG_HTML_FILE pour pouvoir ajuster
    facilement les sélecteurs (cherchez la carte d'une annonce avec les
    outils de développement du navigateur -> clic droit -> Inspecter).
    """
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
    ]

    cards = []
    for selector in candidate_selectors:
        found = soup.select(selector)
        if found:
            cards = found
            break

    if not cards:
        # Dernier recours : on part des liens qui pointent vers une fiche de logement
        links = soup.find_all("a", href=re.compile(r"/tools/\d+/(description|logement)"))
        cards = links

    for card in cards:
        text = " ".join(card.get_text(" ", strip=True).split())
        if not text:
            continue

        link_tag = card if card.name == "a" else card.find("a", href=True)
        link = link_tag["href"] if link_tag and link_tag.has_attr("href") else None
        if link and link.startswith("/"):
            link = "https://trouverunlogement.lescrous.fr" + link

        # Identifiant unique : on préfère le lien, sinon on hash le texte
        uid = link or str(hash(text))

        price_match = re.search(r"(\d[\d\s]*,?\d*)\s*€", text)
        price = price_match.group(0) if price_match else "prix non trouvé"

        listings.append({
            "id": uid,
            "text": text[:300],
            "price": price,
            "link": link or SEARCH_URL,
        })

    # Déduplique par id
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
        html = fetch_page(SEARCH_URL)
    except requests.RequestException as exc:
        print(f"[ERREUR] Impossible de récupérer la page : {exc}")
        return seen

    listings = parse_listings(html)

    if not listings:
        print(
            "[ATTENTION] Aucune annonce détectée — soit il n'y en a réellement "
            "aucune, soit les sélecteurs HTML doivent être ajustés."
        )
        DEBUG_HTML_FILE.write_text(html, encoding="utf-8")
        print(f"HTML brut sauvegardé dans {DEBUG_HTML_FILE} pour inspection.")
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
        # Mode utilisé par GitHub Actions : une seule vérification puis on quitte.
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
