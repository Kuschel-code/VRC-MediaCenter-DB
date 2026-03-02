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
import urllib.parse
from pathlib import Path

# ── Pfade ───────────────────────────────────────────────────
DB_PATH       = Path(__file__).parent
PROGRESS_FILE = DB_PATH / "progress.json"

# ── TMDB (Poster/Metadaten) ──────────────────────────────────
TMDB_API_KEY  = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE     = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w500"

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

# Zeitlimit & Stream-Scraping
START_TIME = time.time()
MAX_RUNTIME_SECONDS = 160 * 60  # 2h40min
SCRAPE_STREAMS = False  # Streams laufen nach ~10min ab - on-demand holen ist sinnvoller

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
    from config import ANIWORLD_BASE, PREFERRED_HOSTERS, STREAMKISTE_BASE
except ImportError as e:
    print(f"[!] Fehler: {e}")
    print(f"    Stelle sicher dass scraper.py und config.py im selben Ordner liegen.")
    sys.exit(1)

import json
import re
from bs4 import BeautifulSoup
import httpx
import time
import random


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


# ── TMDB Poster + Metadaten ─────────────────────────────────
async def get_tmdb_info(client: httpx.AsyncClient,
                        title: str,
                        media_type: str = "tv") -> dict:
    """Holt Poster-URL, Genre, Jahr und Rating von TMDB.
    media_type: 'tv' fuer Anime/Serien, 'movie' fuer Filme.
    Gibt leeres dict zurueck wenn TMDB-Key fehlt oder nichts gefunden."""
    if not TMDB_API_KEY:
        return {}
    query = urllib.parse.quote(title)
    url = f"{TMDB_BASE}/search/{media_type}?api_key={TMDB_API_KEY}&query={query}&language=de-DE&page=1"
    try:
        resp = await client.get(url, timeout=10)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        results = data.get("results", [])
        if not results:
            # Fallback: englische Suche
            url_en = f"{TMDB_BASE}/search/{media_type}?api_key={TMDB_API_KEY}&query={query}&language=en-US&page=1"
            resp2 = await client.get(url_en, timeout=10)
            if resp2.status_code == 200:
                results = resp2.json().get("results", [])
        if not results:
            return {}
        r = results[0]
        poster_path = r.get("poster_path", "")
        thumb = (TMDB_IMG_BASE + poster_path) if poster_path else ""
        # Jahr
        date_str = r.get("release_date", "") or r.get("first_air_date", "")
        year = date_str[:4] if date_str else ""
        # Rating
        rating = str(round(r.get("vote_average", 0), 1)) if r.get("vote_average") else ""
        # Genre-IDs → Genre-Namen (TMDB gibt nur IDs zurueck bei Search)
        # Wir nehmen nur die erste Genre-ID und mappen sie grob
        genre_ids = r.get("genre_ids", [])
        genre = _tmdb_genre_name(genre_ids[0]) if genre_ids else ""
        return {"thumb": thumb, "year": year, "rating": rating, "genre": genre}
    except Exception:
        return {}


_TMDB_GENRES = {
    28: "Action", 12: "Abenteuer", 16: "Animation", 35: "Komoedie",
    80: "Krimi", 99: "Dokumentation", 18: "Drama", 10751: "Familie",
    14: "Fantasy", 36: "Geschichte", 27: "Horror", 10402: "Musik",
    9648: "Mystery", 10749: "Romanze", 878: "Sci-Fi", 10770: "TV-Film",
    53: "Thriller", 10752: "Krieg", 37: "Western",
    # TV
    10759: "Action & Abenteuer", 10762: "Kids", 10763: "News",
    10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap",
    10767: "Talk", 10768: "Krieg & Politik",
}

def _tmdb_genre_name(genre_id: int) -> str:
    return _TMDB_GENRES.get(genre_id, "")


# ── Fortschritt ─────────────────────────────────────────────
def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done_slugs": [], "done_movie_slugs": [], "anime_lines": [],
            "episode_lines": [], "stream_lines": [], "movie_lines": []}


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


# ── Streamkiste Filme ────────────────────────────────────────
async def fetch_streamkiste_movie_list(client: httpx.AsyncClient) -> list:
    """Holt Filmliste von streamkiste.tv (bis zu 20 Seiten)."""
    movies = []
    seen = set()
    for page_num in range(1, 21):
        url = (f"{STREAMKISTE_BASE}/filme/page/{page_num}/"
               if page_num > 1 else f"{STREAMKISTE_BASE}/filme/")
        try:
            resp = await client.get(url, timeout=20)
            if resp.status_code == 404:
                break
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            articles = soup.find_all("article")
            if not articles:
                break
            for article in articles:
                link = article.find("a", href=True)
                if not link:
                    continue
                href = link.get("href", "")
                if not href:
                    continue
                # Slug aus URL extrahieren
                slug = href.rstrip("/").split("/")[-1]
                slug = re.sub(r"-stream(-deutsch)?$", "", slug)
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                title_tag = article.find(["h2", "h3", "h1"])
                title = (title_tag.get_text(strip=True)
                         if title_tag else slug.replace("-", " ").title())
                img_tag = article.find("img")
                thumb = ""
                if img_tag:
                    thumb = img_tag.get("data-src", img_tag.get("src", ""))
                movies.append({"title": title, "thumb": thumb,
                               "content_id": slug, "genre": "", "year": "", "rating": ""})
        except Exception as e:
            print(f"  [!] Streamkiste Seite {page_num}: {e}")
            await asyncio.sleep(5)
            continue
    return movies


async def get_streamkiste_stream(client: httpx.AsyncClient, slug: str) -> str | None:
    """Holt Stream-URL fuer einen Streamkiste-Film per Slug."""
    movie_url = f"{STREAMKISTE_BASE}/stream/{slug}/"
    try:
        resp = await client.get(movie_url, timeout=20)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        hoster_links = scraper._find_hoster_links(soup)
        if not hoster_links:
            return None
        hoster_links.sort(key=lambda h: next(
            (i for i, p in enumerate(PREFERRED_HOSTERS) if p.upper() in h["name"].upper()),
            len(PREFERRED_HOSTERS)
        ))
        for hoster in hoster_links:
            rurl = hoster.get("redirect_url", "")
            if not rurl:
                continue
            if not rurl.startswith("http"):
                rurl = STREAMKISTE_BASE + rurl
            try:
                rr = await client.get(rurl, timeout=20)
                hsoup = BeautifulSoup(rr.text, "lxml")
                stream = scraper._extract_from_hoster(
                    hoster["name"], str(rr.url), rr.text, hsoup)
                if stream:
                    return stream
            except Exception:
                continue
    except Exception as e:
        print(f"  [!] Streamkiste stream {slug}: {e}")
    return None


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

            # Anime-Eintrag (mit TMDB-Poster falls verfuegbar)
            title = item.get("title", "")
            thumb = item.get("thumb", "")
            genre = item.get("genre", "")
            year  = item.get("year", "")
            rating = item.get("rating", "")
            if not thumb or not year:
                tmdb = await get_tmdb_info(client, title, "tv")
                if tmdb:
                    thumb  = tmdb.get("thumb", thumb)
                    year   = tmdb.get("year", year)
                    rating = tmdb.get("rating", rating)
                    genre  = tmdb.get("genre", genre)
                    await asyncio.sleep(0.15)
            entry = f"{title}|{thumb}|{cid}|{genre}|{year}|{rating}"
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

                    if SCRAPE_STREAMS and ep_urlpath:
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

            if time.time() - START_TIME > MAX_RUNTIME_SECONDS:
                print(f"[!] Zeitlimit - beende sicher nach {len(done)} Animes...")
                write_db_files(p)
                git_push(f"Zeitlimit-Update: {len(done)} Animes verarbeitet")
                return

            if PUSH_INTERVAL > 0 and pushed_count >= PUSH_INTERVAL:
                git_push(f"Update: {len(done)} Animes verarbeitet")
                pushed_count = 0

    # Filme von Stream Kiste
    print("\n[2] Lade Filme von Stream Kiste (streamkiste.tv)...")
    done_movies = set(p.get("done_movie_slugs", []))
    try:
        async with scraper._client() as mclient:
            movies = await fetch_streamkiste_movie_list(mclient)
            if MAX_MOVIES > 0:
                movies = movies[:MAX_MOVIES]
            print(f"    -> {len(movies)} Filme gefunden, scrape Streams...")
            for mi, m in enumerate(movies):
                mc = m.get("content_id", "")
                if not mc:
                    continue
                # Filmtitel: TMDB fuer fehlende Poster/Metadaten
                mtitle  = m.get("title", "")
                mthumb  = m.get("thumb", "")
                mgenre  = m.get("genre", "")
                myear   = m.get("year", "")
                mrating = m.get("rating", "")
                if not mthumb or not myear:
                    tmdb = await get_tmdb_info(mclient, mtitle, "movie")
                    if tmdb:
                        mthumb  = tmdb.get("thumb", mthumb)
                        myear   = tmdb.get("year", myear)
                        mrating = tmdb.get("rating", mrating)
                        mgenre  = tmdb.get("genre", mgenre)
                        await asyncio.sleep(0.15)
                entry = f"{mtitle}|{mthumb}|{mc}|{mgenre}|{myear}|{mrating}"
                if entry not in p["movie_lines"]:
                    p["movie_lines"].append(entry)
                if mc not in done_movies:
                    if SCRAPE_STREAMS:
                        stream = await get_streamkiste_stream(mclient, mc)
                        if stream:
                            s = f"{mc}|{stream}"
                            if s not in p["stream_lines"]:
                                p["stream_lines"].append(s)
                            print(f"  [{mi+1}/{len(movies)}] [OK] {mc}: {stream[:55]}...")
                        else:
                            print(f"  [{mi+1}/{len(movies)}] [--] {mc}: kein Stream")
                        await asyncio.sleep(0.3)
                    else:
                        print(f"  [{mi+1}/{len(movies)}] {mc}")
                    done_movies.add(mc)
                    p["done_movie_slugs"] = list(done_movies)
        print(f"    -> {len(p['movie_lines'])} Filme eingetragen")
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
