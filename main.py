#!/usr/bin/env python3
# main.py — Forum monitor GTA5RP

import time
import re
import sqlite3
import yaml
import cloudscraper
import os
import sys

from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, UTC

CFG_PATH = "config.yml"
DB_FILE = "seen_threads.db"

if not os.path.exists(CFG_PATH):
    print("[ERROR] Не найден config.yml")
    input("Нажмите Enter...")
    sys.exit(1)

with open(CFG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f) or {}

SITES = config.get("sites", [])
WEBHOOK = config.get("discord_webhook")
INTERVAL = int(config.get("poll_interval", 60))
ROLE_IDS = [str(x) for x in config.get("role_ids", ["1468298087549108296"])]
FILTER_KEYWORDS = [str(x).lower() for x in config.get("filter_keywords", [])]
ALERT_EXISTING_ON_START = bool(config.get("alert_existing_on_start", False))
USER_AGENT = config.get(
    "user_agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
)

if not WEBHOOK or WEBHOOK == "PASTE_YOUR_DISCORD_WEBHOOK_HERE":
    print("[ERROR] В config.yml укажи discord_webhook")
    input("Нажмите Enter...")
    sys.exit(1)

scraper = cloudscraper.create_scraper(
    browser={
        "browser": "chrome",
        "platform": "windows",
        "mobile": False
    }
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://forum.gta5rp.com/"
}

conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS seen (
    site_name TEXT,
    thread_id TEXT,
    title TEXT,
    url TEXT,
    seen_at TEXT,
    PRIMARY KEY (site_name, thread_id)
)
""")
conn.commit()

initialized_sites = set()


def extract_thread_id(href: str) -> str:
    m = re.search(r"\.(\d+)/?", href)
    if m:
        return m.group(1)
    return href


def already_seen(site_name, thread_id):
    cur.execute(
        "SELECT 1 FROM seen WHERE site_name=? AND thread_id=? LIMIT 1",
        (site_name, thread_id)
    )
    return cur.fetchone() is not None


def mark_seen(site_name, thread_id, title, url):
    cur.execute(
        "INSERT OR IGNORE INTO seen(site_name, thread_id, title, url, seen_at) VALUES (?, ?, ?, ?, ?)",
        (site_name, thread_id, title, url, datetime.now(UTC).isoformat())
    )
    conn.commit()


def title_ok(title: str) -> bool:
    if not FILTER_KEYWORDS:
        return True
    t = title.lower()
    return any(k in t for k in FILTER_KEYWORDS)


def send_discord(site_name, title, url, author, date_text):
    mention = " ".join(f"<@&{role_id}>" for role_id in ROLE_IDS)

    payload = {
        "content": f"{mention} новая заявка на **юридическую реабилитацию**!",
        "allowed_mentions": {
            "parse": [],
            "roles": ROLE_IDS
        },
        "embeds": [
            {
                "title": title,
                "url": url,
                "color": 3447003,
                "fields": [
                    {"name": "Раздел", "value": site_name, "inline": True},
                    {"name": "Автор", "value": author or "Неизвестно", "inline": True},
                    {"name": "Дата", "value": date_text or "Неизвестно", "inline": True}
                ],
                "footer": {
                    "text": "Forum Monitor"
                },
                "timestamp": datetime.now(UTC).isoformat()
            }
        ]
    }

    r = scraper.post(WEBHOOK, json=payload, timeout=20)
    r.raise_for_status()
    print(f"[✓] Отправлено в Discord: {title}")


def parse_threads(html, base_url, selector):
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(".structItem--thread")
    result = []

    for item in items:
        if item.select_one(".structItem-status--sticky"):
            continue

        link = item.select_one(selector)
        if not link:
            continue

        title = link.get_text(" ", strip=True)
        href = link.get("href", "")

        if not href or not title_ok(title):
            continue

        full_url = urljoin(base_url, href)
        thread_id = extract_thread_id(href)

        author_tag = item.select_one(".structItem-parts a.username")
        author = author_tag.get_text(strip=True) if author_tag else "Неизвестно"

        time_tag = item.select_one("time.u-dt")
        if time_tag:
            date_text = time_tag.get("title") or time_tag.get_text(strip=True)
        else:
            date_text = "Неизвестно"

        result.append((title, full_url, thread_id, author, date_text))

    return result


def monitor_once():
    for site in SITES:
        name = site.get("name", "Юридическая реабилитация")
        url = site.get("url")
        selector = site.get("thread_link_selector", ".structItem-title a[data-tp-primary='on']")

        if not url:
            print(f"[!] Пропуск {name}: нет url")
            continue

        try:
            print(f"[*] Проверка: {name} — {url}")

            r = scraper.get(url, headers=HEADERS, timeout=30)

            if r.status_code == 503:
                print("[!] Форум отдал 503. Подожду и попробую снова.")
                return

            r.raise_for_status()

            threads = parse_threads(r.text, url, selector)
            print(f"[DEBUG] Найдено тем: {len(threads)}")

            first_scan = name not in initialized_sites
            initialized_sites.add(name)

            for title, full_url, thread_id, author, date_text in threads:
                if already_seen(name, thread_id):
                    continue

                if first_scan and not ALERT_EXISTING_ON_START:
                    print(f"[i] Запомнил без отправки: {title}")
                    mark_seen(name, thread_id, title, full_url)
                    continue

                print(f"[+] Новая тема: {title}")
                send_discord(name, title, full_url, author, date_text)
                mark_seen(name, thread_id, title, full_url)

            print("[+] Проверка завершена\n")

        except Exception as e:
            print(f"[!] Ошибка: {e}")


def main():
    print("[✔] Forum monitor запущен. Ctrl+C для остановки.")

    try:
        while True:
            monitor_once()
            print(f"⏳ Ожидание {INTERVAL} секунд...\n")
            time.sleep(INTERVAL)

    except KeyboardInterrupt:
        print("\n[✋] Остановлено.")

    finally:
        conn.close()
        input("Нажмите Enter...")


if __name__ == "__main__":
    main()