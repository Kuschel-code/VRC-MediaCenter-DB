#!/usr/bin/env python3
"""
VRC Media Center - GitHub Database Generator v4 (GitHub Actions kompatibel)
=============================================================================
Scraped ALLE Animes von AniWorld.to in ALLEN verfuegbaren Sprachen.

Sprachen pro Episode:
  - Ger-Dub  → episode_id: <slug>-s1-ep1-ger-dub
  - Ger-Sub  → episode_id: <slug>-s1-ep1-ger-sub
  - Eng-Sub  → episode_id: <slug>-s1-ep1-eng-sub

Fortschritt wird in progress.json gesichert → sicheres Fortsetzen nach Abbruch.
GitHub Actions: Laeuft taeglich automatisch, committed die Aenderungen.

Lokale Usage:
    pip install -r requirements.txt
    python update_library.py
"""

import sys
import asyncio
import subprocess
import os
from pathlib import Path

# ── Pfade ───────────────────────────────────────────────────
DB_PATH       = Path(__file__).parent
PROGRESS_FILE = DB_PATH / "progress.json"

# scraper.py liegt im gleichen Ordner (kopiert aus Backend/)
sys.path.insert(0, str(DB_PATH))

# ── Konfiguration ───────────────────────────────────────────
MAX_ANIME  = 0   # 0 = alle (2350+)
MAX_MOVIES = 0   # 0 = alle

LANGUAGES = ["ger-dub", "ger-sub", "eng-sub", "eng-dub"]
LANG_DISPLAY = {"ger-dub": "Ger Dub", "ger-sub": "Ger Sub", "eng-sub": "Eng Sub", "eng-dub": "Eng Dub"}
LANG_NUMBER  = {"ger-dub": "1", "ger-sub": "2", "eng-sub": "3", "eng-dub": "4"}

# Alle N Animes auf GitHub pushen (0 = nur am Ende)
PUSH_INTERVAL = 50

# ── Dependencies sicherstellen ──────────────────────────────
def install_deps():
    import importlib
    deps = {"httpx": "httpx", "bs4": "beautifulsoup4", "lxml": "lxml",
            "cachetools": "cachetools", "cloudscraper": "cloudscraper"}
    for mod, pkg in deps.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            print(f"[*] pip install {pkg}")
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], check=True)

install_deps()

# ── Scraper importieren ─────────────────────────────────────
try:
    import scraper
    from config import ANIWORLD_BASE, PREFERRED_HOSTERS
except ImportError as e:
    print(f"[!] Fehler: {e}")
    print(f"    Stelle sicher dass scraper.py und config.py im selben Ordner liegen.")
    sys.exit(1)

import json
from bs4 import BeautifulSoup
import httpx


# ── Sprachspezifische Stream-URL ─────────────────────────────
async def get_stream_for_language(client: httpx.AsyncClient,
                                   ep_url_path: str, lang: str) -> str | None:
    lang_num = LANG_NUMBER.get(lang, "1")
    url = ANIWORLD_BASE + ep_url_path + f"?lang={lang_num}"

    try:
        resp = await client.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        html = resp.text
        # Pruefe ob Sprache verfuegbar (einfach: Hoster-Links vorhanden?)
        soup = BeautifulSoup(html, "lxml")
        hoster_links = scraper._find_hoster_links(soup)
        if not hoster_links:
            return None

        # Nach Praeferenz sortieren
        hoster_links.sort(key=lambda h: next(
            (i for i, p in enumerate(PREFERRED_HOSTERS) if p.upper() in h["name"].upper()),
            len(PREFERRED_HOSTERS)
        ))

        for hoster in hoster_links:
            rurl = hoster["redirect_url"]
            if not rurl.startswith("http"):
                rurl = ANIWORLD_BASE + rurl
            try:
                rr = await client.get(rurl, timeout=20)
                hsoup = BeautifulSoup(rr.text, "lxml")
                stream = scraper._extract_from_hoster(hoster["name"], str(rr.url), rr.text, hsoup)
                if stream:
                    return stream
            except Exception:
                continue
    except Exception:
        pass
    return None


# ── Fortschritt ─────────────────────────────────────────────
def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done_slugs": [], "anime_lines": [], "episode_lines": [],
            "stream_lines": [], "movie_lines": []}


def save_progress(p: dict):
    PROGRESS_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Dateien schreiben ────────────────────────────────────────
def write_db_files(p: dict):
    (DB_PATH / "anime.txt").write_text("\n".join(p["anime_lines"]), encoding="utf-8")
    (DB_PATH / "movies.txt").write_text("\n".join(p["movie_lines"]), encoding="utf-8")
    (DB_PATH / "episodes.txt").write_text("\n".join(p["episode_lines"]), encoding="utf-8")
    (DB_PATH / "streams.txt").write_text("\n".join(p["stream_lines"]), encoding="utf-8")
    if not (DB_PATH / "series.txt").exists():
        (DB_PATH / "series.txt").write_text("", encoding="utf-8")


# ── Git Push (fuer Zwischen-Pushes) ─────────────────────────
def git_push(msg: str = "Auto-update library data"):
    # In GitHub Actions: git config wird ueber env gesetzt
    is_ci = os.environ.get("CI", "false") == "true"
    try:
        if is_ci:
            subprocess.run(["git", "config", "user.name", "GitHub Actions Bot"],
                           cwd=DB_PATH, check=True)
            subprocess.run(["git", "config", "user.email", "actions@github.com"],
                           cwd=DB_PATH, check=True)
        subprocess.run(["git", "add", "anime.txt", "episodes.txt", "streams.txt",
                        "movies.txt", "progress.json"], cwd=DB_PATH, check=True)
        r = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=DB_PATH)
        if r.returncode != 0:  # Aenderungen vorhanden
            subprocess.run(["git", "commit", "-m", msg], cwd=DB_PATH, check=True)
            subprocess.run(["git", "push"], cwd=DB_PATH, check=True)
            print(f"[+] Git Push: {msg}")
    except subprocess.CalledProcessError as e:
        print(f"[!] Git-Fehler: {e}")


# ── Main ─────────────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("  VRC Media Center - Database Generator v4")
    print(f"  Sprachen: {', '.join(LANGUAGES)}")
    print(f"  Animes:   {'alle' if MAX_ANIME == 0 else MAX_ANIME}")
    print("=" * 60)

    p = load_progress()
    done = set(p["done_slugs"])
    if done:
        print(f"[*] Fortschritt: {len(done)} Animes bereits fertig")

    # Anime-Liste laden
    print("\n[1] Lade Anime-Liste von AniWorld...")
    animes = await scraper.fetch_library("anime")
    print(f"    -> {len(animes)} Animes gefunden")
    if MAX_ANIME > 0:
        animes = animes[:MAX_ANIME]

    pushed_count = 0
    async with scraper._client() as client:
        for ai, item in enumerate(animes):
            cid = item.get("content_id", "")
            if not cid or cid in done:
                if cid in done:
                    print(f"  [{ai+1}/{len(animes)}] {cid} (fertig, ueberspringe)")
                continue

            print(f"\n  [{ai+1}/{len(animes)}] {cid}")

            # Anime-Eintrag
            entry = (f"{item.get('title','')}|{item.get('thumb','')}|{cid}|"
                     f"{item.get('genre','')}|{item.get('year','')}|{item.get('rating','')}")
            if entry not in p["anime_lines"]:
                p["anime_lines"].append(entry)

            # Episoden laden
            episodes = await scraper.fetch_episodes(cid)
            print(f"    {len(episodes)} Episoden × {len(LANGUAGES)} Sprachen")

            for ep in episodes:
                ep_id      = ep.get("episode_id", "")
                ep_title   = ep.get("title", "")
                ep_urlpath = ep.get("url_path", "")
                if not ep_id:
                    continue

                for lang in LANGUAGES:
                    lid   = f"{ep_id}-{lang}"
                    ltitle = f"{ep_title} ({LANG_DISPLAY[lang]})"

                    ep_line = f"{cid}|{ltitle}|{lid}"
                    if ep_line not in p["episode_lines"]:
                        p["episode_lines"].append(ep_line)

                    stream = None
                    if ep_urlpath:
                        stream = await get_stream_for_language(client, ep_urlpath, lang)

                    if stream:
                        s = f"{lid}|{stream}"
                        if s not in p["stream_lines"]:
                            p["stream_lines"].append(s)
                        print(f"    [OK] {lang}: {stream[:55]}...")
                    else:
                        print(f"    [--] {lang}: nicht verfuegbar")

                    await asyncio.sleep(0.3)

            p["done_slugs"].append(cid)
            done.add(cid)
            pushed_count += 1

            save_progress(p)
            write_db_files(p)

            if PUSH_INTERVAL > 0 and pushed_count >= PUSH_INTERVAL:
                git_push(f"Update: {len(done)} Animes verarbeitet")
                pushed_count = 0

    # Filme
    print("\n[2] Lade Filme...")
    try:
        movies = await scraper.fetch_library("movies")
        if MAX_MOVIES > 0:
            movies = movies[:MAX_MOVIES]
        for m in movies:
            mc = m.get("content_id", "")
            if mc:
                e = (f"{m.get('title','')}|{m.get('thumb','')}|{mc}|"
                     f"{m.get('genre','')}|{m.get('year','')}|{m.get('rating','')}")
                if e not in p["movie_lines"]:
                    p["movie_lines"].append(e)
        print(f"    -> {len(p['movie_lines'])} Filme")
    except Exception as e:
        print(f"  [!] Filme: {e}")

    write_db_files(p)
    save_progress(p)

    print(f"\n[+] anime.txt:    {len(p['anime_lines'])}")
    print(f"[+] movies.txt:   {len(p['movie_lines'])}")
    print(f"[+] episodes.txt: {len(p['episode_lines'])}")
    print(f"[+] streams.txt:  {len(p['stream_lines'])}")

    git_push("Final update: alle Animes und Sprachen verarbeitet")

    # Fortschritt loeschen nach Abschluss
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()

    print("\n" + "=" * 60)
    print("  Fertig! In Unity: My World > Setup Cloudflare URLs")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
