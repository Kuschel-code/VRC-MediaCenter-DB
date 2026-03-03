"""
AniWorld Scraper – Holt Inhaltslisten, Episodenlisten und Stream-URLs.

Basiert auf den Patterns von:
- AniWorld-Downloader (phoenixthrush) – VOE/Vidoza/Vidmoly Extraktoren
- aniworld_scraper (wolfswolke) – Staffel/Episoden-Crawling + VOE Deobfuscation

Verwendet httpx (async) + BeautifulSoup für HTML-Parsing.
"""

import re
import codecs
import base64
import logging
import asyncio
import random
import httpx
from bs4 import BeautifulSoup
import sqlite3
import json
import time
from cachetools import TTLCache
from config import (
    ANIWORLD_BASE,
    STO_BASE,
    STREAMKISTE_BASE,
    FILMPALAST_BASE,
    PREFERRED_HOSTERS,
    REQUEST_HEADERS,
    LIBRARY_CACHE_TTL,
    EPISODE_CACHE_TTL,
    STREAM_CACHE_TTL,
    TMDB_API_KEY,
)

log = logging.getLogger("scraper")

# ─── SQLite Cache ─────────────────────────────────────────
DB_PATH = "cache.db"

def init_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    expires REAL
                )
            """)
    except Exception as e:
        log.error(f"[Cache] Fehler bei Initialisierung: {e}")

init_db()

def get_cache(key: str):
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            cur = conn.execute("SELECT value, expires FROM cache WHERE key = ?", (key,))
            row = cur.fetchone()
            if row:
                value, expires = row
                if expires > time.time():
                    return json.loads(value)
                else:
                    conn.execute("DELETE FROM cache WHERE key = ?", (key,))
    except Exception as e:
        log.error(f"[Cache] Fehler beim Lesen: {e}")
    return None

def set_cache(key: str, value, ttl: int):
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, expires) VALUES (?, ?, ?)",
                (key, json.dumps(value), time.time() + ttl)
            )
    except Exception as e:
        log.error(f"[Cache] Fehler beim Schreiben: {e}")

# SSL-Warnungen unterdrücken (manche Hoster haben ungültige Zertifikate)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# DNS-Bypass# ── PATCH FÜR DNS-SPERREN (UMGEHUNG VON ISP-BLOCKS Z.B. CUII) ──
import socket
_orig_getaddrinfo = socket.getaddrinfo

def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == "filmpalast.to":
        return _orig_getaddrinfo("172.67.158.161", port, family, type, proto, flags)
    elif host in ("serien.sx", "s.to", "serienstream.to", "bs.to"):
        return _orig_getaddrinfo("3.122.9.223", port, family, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = _patched_getaddrinfo

# cloudscraper fuer Cloudflare-geschuetzte Seiten (Fallback)
try:
    import cloudscraper
    _has_cloudscraper = True
except ImportError:
    _has_cloudscraper = False
    log.warning("[Scraper] cloudscraper nicht installiert – Cloudflare-Bypass nicht verfuegbar")

# ─── Caches ─────────────────────────────────────────
_library_cache = TTLCache(maxsize=64, ttl=LIBRARY_CACHE_TTL)
_episode_cache = TTLCache(maxsize=256, ttl=EPISODE_CACHE_TTL)
_stream_cache = TTLCache(maxsize=512, ttl=STREAM_CACHE_TTL)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=REQUEST_HEADERS,
        timeout=httpx.Timeout(15.0, connect=5.0),
        verify=False,  # Manche Hoster haben ungültige Zertifikate
        follow_redirects=True
    )


def _cloudscraper_get(url: str) -> str | None:
    """Synchroner Fallback mit cloudscraper fuer Cloudflare-geschuetzte Seiten."""
    if not _has_cloudscraper:
        return None
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'desktop': True
            },
            ssl_context=ctx
        )
        resp = scraper.get(url, headers=REQUEST_HEADERS, timeout=20, verify=False)
        if resp.status_code == 200:
            return resp.text
        log.info(f"[Scraper] cloudscraper bekam Status {resp.status_code} fuer {url}")
        
    except Exception as e:
        log.info(f"[Scraper] cloudscraper fehlgeschlagen fuer {url}: {e}")
        
    # Letztes Fallback: Nativer cURL über Subprocess (umgeht manche Python-spezifischen Firewalls)
    log.info(f"[Scraper] Versuche nativen cURL Fallback fuer {url}...")
    try:
        import subprocess
        result = subprocess.run([
            "curl", "-s", "-L", "-k",
            "-H", f"User-Agent: {REQUEST_HEADERS['User-Agent']}",
            "-m", "15",
            url
        ], capture_output=True, text=True, check=True)
        
        if result.stdout and len(result.stdout) > 500:
            log.info(f"[Scraper] cURL war erfolgreich fuer {url}")
            return result.stdout
    except Exception as curl_e:
        log.info(f"[Scraper] Nativer cURL fehlgeschlagen: {curl_e}")
        
    return None


async def _fetch_page(client: httpx.AsyncClient, url: str, max_retries: int = 3) -> str | None:
    """Holt eine Seite – nutzt für Filmpalast/Serienstream direkt cloudscraper, ansonsten httpx."""
    if any(domain in url for domain in ["filmpalast.to", "s.to", "serienstream.to", "bs.to"]) and _has_cloudscraper:
        log.info(f"[Scraper] Direkte Route via cloudscraper ({url})")
        return await asyncio.to_thread(_cloudscraper_get, url)

    for attempt in range(max_retries):
        try:
            resp = await client.get(url)

            if resp.status_code == 429:
                # Rate-Limit: Retry-After Header beachten, sonst exponential warten
                wait = float(resp.headers.get("Retry-After", 30 * (attempt + 1)))
                wait += random.uniform(1, 5)  # Jitter
                log.info(f"[Scraper] Rate-Limit fuer {url} – warte {wait:.0f}s (Versuch {attempt+1}/{max_retries})")
                await asyncio.sleep(wait)
                continue

            if resp.status_code in (403, 503) and _has_cloudscraper:
                log.info(f"[Scraper] {resp.status_code} von {url} – versuche cloudscraper...")
                result = await asyncio.to_thread(_cloudscraper_get, url)
                if result:
                    return result
            
            if resp.status_code == 200:
                html = resp.text
                if "cuii.info" in html or "urheberrechtlichen Gr" in html:
                    log.warning(f"[Scraper] CUII Sperre! Nutze cloudscraper Fallback mit Socket Patch...")
                    if _has_cloudscraper:
                        return await asyncio.to_thread(_cloudscraper_get, url)
                    return None
                return html

            log.info(f"[Scraper] HTTP {resp.status_code} fuer {url}")
            # Bei Server-Fehlern (5xx) nochmal versuchen
            if resp.status_code >= 500 and attempt < max_retries - 1:
                await asyncio.sleep(10 * (attempt + 1) + random.uniform(0, 5))
                continue
            return None
        except httpx.TimeoutException:
            wait = 10 * (attempt + 1) + random.uniform(0, 5)
            log.info(f"[Scraper] Timeout fuer {url} – retry {attempt+1}/{max_retries} in {wait:.0f}s")
            await asyncio.sleep(wait)
        except httpx.HTTPError as e:
            log.info(f"[Scraper] HTTP-Fehler fuer {url}: {e}")
            if _has_cloudscraper:
                return await asyncio.to_thread(_cloudscraper_get, url)
            return None
    log.info(f"[Scraper] Alle {max_retries} Versuche fehlgeschlagen fuer {url}")
    return None


async def fetch_tmdb_metadata(title: str, content_type: str = "tv") -> dict:
    """Holt Metadaten von TMDB (Beschreibung, Rating, Poster)."""
    if not TMDB_API_KEY:
        return {}

    cache_key = f"tmdb_{title.lower()}_{content_type}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    # Map content_type
    tmdb_type = "tv" if content_type in ["anime", "series", "serien"] else "movie"
    
    url = f"https://api.themoviedb.org/3/search/{tmdb_type}?api_key={TMDB_API_KEY}&query={title}&language=de-DE"
    
    async with _client() as client:
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("results"):
                    best_match = data["results"][0]
                    metadata = {
                        "description": best_match.get("overview", ""),
                        "rating": str(best_match.get("vote_average", "")),
                        "poster": f"https://image.tmdb.org/t/p/w500{best_match.get('poster_path')}" if best_match.get("poster_path") else "",
                        "year": best_match.get("release_date", best_match.get("first_air_date", ""))[:4]
                    }
                    set_cache(cache_key, metadata, 86400 * 7) # Cache 1 week
                    return metadata
        except Exception as e:
            log.error(f"[TMDB] Fehler: {e}")
    
    return {}


# ═══════════════════════════════════════════════════════
# Inhaltsliste (Anime / Serien / Filme)
# ═══════════════════════════════════════════════════════

async def fetch_library(content_type: str) -> list[dict]:
    cache_key = f"library_{content_type}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    # AniWorld-Pfade / Filmpalast
    is_filmpalast = content_type.lower() in ("movies", "filme")
    type_map = {
        "anime":   (ANIWORLD_BASE,    "/animes"),
        "filme":   (FILMPALAST_BASE,  "/movies/new"),
        "movies":  (FILMPALAST_BASE,  "/movies/new"),
        "serien":  (STO_BASE,         "/serien"),
        "series":  (STO_BASE,         "/serien"),
    }

    base_url, path = type_map.get(content_type.lower(), (ANIWORLD_BASE, "/animes"))

    items = []

    # ── Filmpalast: mehrere Seiten laden ───────────────────
    if is_filmpalast:
        async with _client() as client:
            seen_ids: set = set()
            for page_num in range(1, 10):  # bis zu ~200 Filme (10 Seiten à ~20)
                page_url = f"{base_url}{path}/page/{page_num}" if page_num > 1 else base_url + path
                html = await _fetch_page(client, page_url)
                if not html:
                    break

                soup = BeautifulSoup(html, "lxml")
                found_on_page = 0

                for article in soup.find_all("article", class_="liste"):
                    link = article.find("a", href=True)
                    if not link:
                        continue
                    href = link.get("href", "")
                    if not href:
                        continue

                    # Slug extrahieren (z.B. ".../stream/movie-slug")
                    slug = href.rstrip("/").split("/")[-1]
                    if not slug or slug in seen_ids or len(slug) < 2:
                        continue
                    seen_ids.add(slug)

                    title_tag = article.find(["h2", "h3", "h1"])
                    title = title_tag.get_text(strip=True) if title_tag else slug.replace("-", " ").title()

                    img_tag = article.find("img")
                    thumb = ""
                    if img_tag:
                        thumb = img_tag.get("data-src", img_tag.get("src", ""))
                        if thumb and not thumb.startswith("http"):
                            thumb = base_url + thumb

                    items.append({
                        "title": title,
                        "thumb": thumb,
                        "content_id": slug,
                        "genre": "",
                        "year": "",
                        "rating": "",
                        "url_path": href,
                    })
                    found_on_page += 1

                if found_on_page == 0:
                    break  # Keine weiteren Seiten

        if items:
            set_cache(cache_key, items, LIBRARY_CACHE_TTL)
            log.info(f"[Scraper] {len(items)} Filmpalast-Filme geladen")
        else:
            log.info(f"[Scraper] Keine Filmpalast-Filme gefunden")
        return items

    # ── AniWorld / S.to: einzelne Seite ─────────────────────
    url = base_url + path
    async with _client() as client:
        html = await _fetch_page(client, url)
        if not html:
            log.info(f"[Scraper] Seite nicht ladbar: {url}")
            return items

        soup = BeautifulSoup(html, "lxml")

        # AniWorld Hauptseite: <div class="genre"> enthält <a>-Tags
        # Jeder Link hat href="/anime/stream/<slug>" und den Titel als Text
        # + data-alternative-title für alternative Titel
        seen_ids = set()

        # Methode 1: Genre-Divs (Standard AniWorld Layout)
        for genre_div in soup.find_all("div", class_="genre"):
            for link in genre_div.find_all("a"):
                href = link.get("href", "")
                if not href:
                    continue

                # Slug aus dem Pfad extrahieren
                match = re.search(r"/stream/([^/]+)", href)
                if not match:
                    continue

                slug = match.group(1)
                if slug in seen_ids:
                    continue
                seen_ids.add(slug)

                title = link.get_text(strip=True)
                if not title:
                    title = slug.replace("-", " ").title()

                # Thumbnail versuchen zu finden
                img = link.find("img")
                thumb = ""
                if img:
                    thumb = img.get("data-src", img.get("src", ""))
                    if thumb and not thumb.startswith("http"):
                        thumb = base_url + thumb

                content_id = slug
                
                # Serienstream liefert noch alte "s.to" absolute Links aus.
                if href.startswith("http"):
                    # Extrahiere relativen Pfad aus /serie/stream...
                    rel_match = re.search(r"/(anime|serien|serie|filme|stream)/(.+)", href)
                    if rel_match:
                        url_path = "/" + rel_match.group(1) + "/" + rel_match.group(2)
                    else:
                        url_path = href
                else:
                    url_path = href if href.startswith("/") else "/" + href

                items.append({
                    "title": title,
                    "thumb": thumb,
                    "content_id": content_id,
                    "genre": "",
                    "year": "",
                    "rating": "",
                    "url_path": url_path,
                })

        # Methode 2: Falls Genre-Divs leer, generische Link-Suche
        if not items:
            link_pattern = re.compile(r"/(anime|serien|filme)/stream/([^/]+)|/serie/([^/]+)")
            for link in soup.find_all("a", href=link_pattern):
                href = link.get("href", "")
                match = re.search(link_pattern, href)
                if not match:
                    continue

                slug = match.group(2) or match.group(3)
                if slug in seen_ids or slug == "stream":
                    continue
                seen_ids.add(slug)

                title = link.get_text(strip=True)
                if not title or len(title) < 2:
                    title = slug.replace("-", " ").title()

                items.append({
                    "title": title,
                    "thumb": "",
                    "content_id": slug,
                    "genre": "",
                    "year": "",
                    "rating": "",
                    "url_path": href,
                })

    if items:
        set_cache(cache_key, items, LIBRARY_CACHE_TTL)
        log.info(f"[Scraper] {len(items)} Einträge für '{content_type}' geladen")
    else:
        log.info(f"[Scraper] Keine Einträge für '{content_type}' gefunden")

    return items


async def fetch_search_results(query: str) -> list[dict]:
    """
    Sucht nach Inhalten auf AniWorld.
    query: Suchbegriff
    Returns: Liste von {title, thumb, content_id, genre, year, rating, url_path}
    """
    if not query or len(query) < 2:
        return []

    cache_key = f"search_{query.lower()}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    items = []
    seen_ids = set()

    async with _client() as client:
        for base_url in [ANIWORLD_BASE, STO_BASE]:
            url = f"{base_url}/suche?q={query}"
            html = await _fetch_page(client, url)
            if not html:
                continue

            soup = BeautifulSoup(html, "lxml")

            # AniWorld Suche: <div class="seriesListContainer">
            for link in soup.select(".seriesListContainer a"):
                href = link.get("href", "")
                if "/stream/" not in href:
                    continue

                match = re.search(r"/stream/([^/]+)", href)
                if not match:
                    continue

                slug = match.group(1)
                if slug in seen_ids:
                    continue
                seen_ids.add(slug)

                title = link.get_text(strip=True)
                if not title:
                    h3 = link.find("h3")
                    if h3: title = h3.get_text(strip=True)
                    else: title = slug.replace("-", " ").title()

                img = link.find("img")
                thumb = ""
                if img:
                    thumb = img.get("data-src", img.get("src", ""))
                    if thumb and not thumb.startswith("http"):
                        thumb = base_url + thumb

                items.append({
                    "title": title,
                    "thumb": thumb,
                    "content_id": slug,
                    "genre": "",
                    "year": "",
                    "rating": "",
                    "url_path": href,
                })

    if items:
        set_cache(cache_key, items, 3600)  # Search results cache for 1h
        log.info(f"[Scraper] {len(items)} Suchergebnisse für '{query}'")

    return items


# ═══════════════════════════════════════════════════════
# Episodenliste
# ═══════════════════════════════════════════════════════

async def fetch_episodes(content_id: str) -> list[dict]:
    cache_key = f"episodes_{content_id}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    episodes = []

    async with _client() as client:
        # Optimiere: Wenn der Slug in den Filmen ist, nur Filmpalast durchsuchen
        bases = [ANIWORLD_BASE, STO_BASE, FILMPALAST_BASE]
        cached_movies = get_cache("library_filme") or get_cache("library_movies") or []
        if any(m.get("content_id") == content_id for m in cached_movies):
            bases = [FILMPALAST_BASE]
        
        series_html = None
        used_path = ""
        used_base = ""

        for base in bases:
            base_paths = [
                f"/anime/stream/{content_id}",
                f"/serien/stream/{content_id}",
                f"/serie/stream/{content_id}",
                f"/serie/{content_id}",
                f"/filme/stream/{content_id}",
                f"/stream/{content_id}",
            ]

            for path in base_paths:
                page = await _fetch_page(client, base + path)
                if page and ("/staffel-" in page or "/episode-" in page or "seasonEpisodesList" in page):
                    series_html = page
                    used_path = path
                    used_base = base
                    break
            if series_html:
                break

        if not series_html:
            # Fallback für Filme
            for base in bases:
                for path in [
                    f"/anime/stream/{content_id}", 
                    f"/serien/stream/{content_id}", 
                    f"/serie/stream/{content_id}",
                    f"/serie/{content_id}",
                    f"/filme/stream/{content_id}", 
                    f"/stream/{content_id}"
                ]:
                    page = await _fetch_page(client, base + path)
                    if page:
                        episodes.append({
                            "title": content_id.replace("-", " ").title(),
                            "episode_id": f"{content_id}-s1-ep1",
                            "number": 1,
                            "season": 1,
                            "ep_in_season": 1,
                            "url_path": path,
                        })
                        set_cache(cache_key, episodes, EPISODE_CACHE_TTL)
                        return episodes
            log.info(f"[Scraper] Inhalt '{content_id}' nicht gefunden")
            return episodes

        # Staffel-Anzahl bestimmen (Pattern von aniworld_scraper)
        season_count = _count_seasons(series_html, used_path)
        log.info(f"[Scraper] {content_id}: {season_count} Staffel(n) gefunden")

        ep_counter = 0

        for season_num in range(1, season_count + 1):
            season_url = used_base + used_path + f"/staffel-{season_num}"

            season_html = await _fetch_page(client, season_url)
            if not season_html:
                continue

            # Episode-Anzahl bestimmen (Pattern von aniworld_scraper)
            ep_count = _count_episodes(season_html, used_path, season_num)
            log.info(f"[Scraper] {content_id} Staffel {season_num}: {ep_count} Episode(n)")

            # Episodentitel aus der Seite extrahieren
            ep_titles = _extract_episode_titles(season_html)

            for ep_num in range(1, ep_count + 1):
                ep_counter += 1
                episode_id = f"{content_id}-s{season_num}-ep{ep_num}"
                ep_url_path = f"{used_path}/staffel-{season_num}/episode-{ep_num}"

                # Titel aus extrahierten Titeln oder Fallback
                ep_title = ep_titles.get(ep_num, f"Staffel {season_num} Episode {ep_num}")

                episodes.append({
                    "title": ep_title,
                    "episode_id": episode_id,
                    "number": ep_counter,
                    "season": season_num,
                    "ep_in_season": ep_num,
                    "url_path": ep_url_path,
                })

    if episodes:
        set_cache(cache_key, episodes, EPISODE_CACHE_TTL)

    return episodes


def _count_seasons(html: str, base_path: str) -> int:
    """Zählt die Staffeln einer Serie (Pattern von aniworld_scraper)."""
    count = 0
    while True:
        search = f"/staffel-{count + 1}"
        if search in html:
            count += 1
        else:
            break
    return max(count, 1)  # Mindestens 1 Staffel


def _count_episodes(html: str, base_path: str, season: int) -> int:
    """Zählt die Episoden einer Staffel (Pattern von aniworld_scraper)."""
    count = 0
    while True:
        search = f"/staffel-{season}/episode-{count + 1}"
        if search in html:
            count += 1
        else:
            break
    return count


def _extract_episode_titles(html: str) -> dict[int, str]:
    """Extrahiert Episodentitel aus einer Staffel-Seite."""
    titles = {}
    soup = BeautifulSoup(html, "lxml")

    # AniWorld zeigt Episoden in einer Tabelle oder Liste
    # Versuche verschiedene Selektoren
    rows = soup.select("table.seasonEpisodesList tbody tr")
    if not rows:
        rows = soup.select("tr")

    for row in rows:
        # Episoden-Link finden
        link = row.find("a", href=re.compile(r"/episode-(\d+)"))
        if not link:
            continue

        ep_match = re.search(r"/episode-(\d+)", link.get("href", ""))
        if not ep_match:
            continue

        ep_num = int(ep_match.group(1))

        # Titel aus der zweiten Spalte oder dem Link-Text
        title_td = row.select_one("td.seasonEpisodeTitle a, td:nth-of-type(2) a, td:nth-of-type(2)")
        if title_td:
            title = title_td.get_text(strip=True)
            # Bereinige typische Prefixe
            title = re.sub(r"^Episode\s+\d+\s*[-:]\s*", "", title).strip()
            if title and len(title) > 1:
                titles[ep_num] = title

    return titles


# ═══════════════════════════════════════════════════════
# Stream-URL Extraktion
# ═══════════════════════════════════════════════════════

async def get_stream_url(episode_id: str) -> str | None:
    cache_key = f"stream_{episode_id}"
    cached = get_cache(cache_key)
    if cached:
        return cached

    # episode_id Format: <slug>-s<season>-ep<episode>
    match = re.match(r"^(.+)-s(\d+)-ep(\d+)$", episode_id)
    if not match:
        log.info(f"[Scraper] Ungültige Episode-ID: {episode_id}")
        return None

    slug = match.group(1)
    season = match.group(2)
    episode = match.group(3)

    # Versuche verschiedene Pfade
    paths = [
        f"/anime/stream/{slug}/staffel-{season}/episode-{episode}",
        f"/serien/stream/{slug}/staffel-{season}/episode-{episode}",
        f"/serie/stream/{slug}/staffel-{season}/episode-{episode}",
        f"/serie/{slug}/staffel-{season}/episode-{episode}",
        f"/filme/stream/{slug}/staffel-{season}/episode-{episode}",
        f"/filme/stream/{slug}",
        f"/stream/{slug}",
    ]

    async with _client() as client:
        episode_html = None
        used_base = ""

        cached_movies = get_cache("library_filme") or get_cache("library_movies") or []
        cached_series = get_cache("library_serien") or get_cache("library_series") or []
        cached_anime = get_cache("library_anime") or []
        
        if any(m.get("content_id") == slug for m in cached_movies):
            bases = [FILMPALAST_BASE]
        elif any(s.get("content_id") == slug for s in cached_series):
            bases = [STO_BASE]
        elif any(a.get("content_id") == slug for a in cached_anime):
            bases = [ANIWORLD_BASE]
        else:
            bases = [ANIWORLD_BASE, STO_BASE, FILMPALAST_BASE]
            
        for base in bases:
            for path in paths:
                # Skip anime paths for serienstream to avoid timeouts
                if base == STO_BASE and "anime" in path:
                    continue
                # Skip series paths for aniworld to avoid timeouts
                if base == ANIWORLD_BASE and ("serie" in path or "filme" in path):
                    continue
                # Skip everything but filme paths for filmpalast
                if base == FILMPALAST_BASE and "filme" not in path:
                    continue
                    
                page = await _fetch_page(client, base + path)
                if page and (
                    "hosterSiteVideo" in page
                    or "watchEpisode" in page
                    or "data-link-target" in page
                    or "changemark" in page
                    or "currentStreamLinks" in page
                ):
                    episode_html = page
                    used_base = base
                    break
            if episode_html:
                break

        if not episode_html:
            log.info(f"[Scraper] Episode nicht gefunden: {episode_id}")
            return None

        soup = BeautifulSoup(episode_html, "lxml")

        # ── Hoster-Links extrahieren ──
        if used_base == FILMPALAST_BASE:
            hoster_links = []
            for ul in soup.select('ul.currentStreamLinks'):
                name_tag = ul.select_one('.hostName')
                name = name_tag.get_text(strip=True) if name_tag else "Unknown"
                btn = ul.select_one('li.streamPlayBtn a.iconPlay')
                if btn and btn.get("href"):
                    hr = btn.get("href")
                    hoster_links.append({
                        "name": name,
                        "redirect_url": hr,
                        "is_direct": True
                    })
        else:
            hoster_links = _find_hoster_links(soup)

        if not hoster_links:
            log.info(f"[Scraper] Keine Hoster gefunden für: {episode_id}")
            return None

        # Nach Präferenz sortieren
        def hoster_priority(h):
            name = h["name"]
            for i, pref in enumerate(PREFERRED_HOSTERS):
                if pref.upper() in name.upper():
                    return i
            return len(PREFERRED_HOSTERS)

        hoster_links.sort(key=hoster_priority)

        # Versuche jeden Hoster
        for hoster in hoster_links:
            log.info(f"[Scraper] Versuche Hoster: {hoster['name']} -> {hoster['redirect_url']}")

            try:
                # Portal-Redirection folgen oder direkten Hosterlink laden
                redirect_url = hoster["redirect_url"]
                
                if hoster.get("is_direct"):
                    hoster_url = redirect_url
                    try:
                        hoster_resp = await client.get(hoster_url)
                        hoster_html = hoster_resp.text
                    except Exception as he:
                        log.info(f"[Scraper] httpx Fehler bei direct Hoster ({he}), probiere cloudscraper...")
                        hoster_html = await asyncio.to_thread(_cloudscraper_get, hoster_url)
                        if not hoster_html:
                            continue
                else:
                    if not redirect_url.startswith("http"):
                        redirect_url = used_base + redirect_url
                    try:
                        redirect_resp = await client.get(redirect_url)
                        hoster_url = str(redirect_resp.url)
                        hoster_html = redirect_resp.text
                    except Exception as he:
                        log.info(f"[Scraper] httpx Fehler bei redirect Hoster ({he}), probiere cloudscraper...")
                        hoster_html = await asyncio.to_thread(_cloudscraper_get, redirect_url)
                        hoster_url = redirect_url
                        if not hoster_html:
                            continue

                # JS Redirect (VOE nutzt oft window.location.href statt echten HTTP Redirects)
                js_redirect = re.search(r"window\.location\.href\s*=\s*['\"](https?://[^'\"]+)['\"]", hoster_html)
                if js_redirect:
                    redir_url = js_redirect.group(1)
                    log.info(f"[Scraper] JS-Redirect bei Hoster gefunden: {redir_url}")
                    try:
                        hoster_html = await asyncio.to_thread(_cloudscraper_get, redir_url)
                        if not hoster_html:
                            continue
                        hoster_url = redir_url
                    except Exception as e:
                        log.info(f"[Scraper] JS-Redirect fehlgeschlagen: {e}")
                        continue

                # BeautifulSoup für den Hoster parsen
                hoster_soup = BeautifulSoup(hoster_html, "lxml")

                stream_url = _extract_from_hoster(hoster["name"], hoster_url, hoster_html, hoster_soup)
                if stream_url:
                    log.info(f"[Scraper] Stream-URL gefunden: {stream_url[:80]}...")
                    set_cache(cache_key, stream_url, STREAM_CACHE_TTL)
                    return stream_url

            except Exception as e:
                log.info(f"[Scraper] Hoster {hoster['name']} fehlgeschlagen (Fehlerklasse: {type(e).__name__}): {e}")
                continue

    log.info(f"[Scraper] Kein Stream für {episode_id} gefunden")
    return None


def _find_hoster_links(soup: BeautifulSoup) -> list[dict]:
    """Findet alle verfügbaren Hoster-Links auf einer AniWorld Episodenseite."""
    hoster_links = []
    seen = set()

    # Methode 1: data-link-target Attribute (Hauptmethode)
    for elem in soup.select("[data-link-target]"):
        link = elem.get("data-link-target", "")
        if not link or link in seen:
            continue
        seen.add(link)

        # Hoster-Name extrahieren
        hoster_name = ""
        h4 = elem.find("h4")
        if h4:
            hoster_name = h4.get_text(strip=True)
        else:
            i_tag = elem.find("i")
            if i_tag and i_tag.get("title"):
                hoster_name = i_tag.get("title")
            else:
                hoster_name = elem.get_text(strip=True)

        if hoster_name:
            hoster_links.append({
                "name": hoster_name.strip(),
                "redirect_url": link,
            })

    # Methode 2: Hoster-Links in der Video-Sektion
    if not hoster_links:
        for elem in soup.select("li a[href*='redirect'], div.hosterSiteVideo a"):
            href = elem.get("href", "")
            if not href or href in seen:
                continue
            seen.add(href)

            hoster_name = ""
            h4 = elem.find("h4")
            if h4:
                hoster_name = h4.get_text(strip=True)
            else:
                hoster_name = elem.get_text(strip=True)

            if hoster_name:
                hoster_links.append({
                    "name": hoster_name.strip(),
                    "redirect_url": href,
                })

    return hoster_links


# ═══════════════════════════════════════════════════════
# Hoster-spezifische Extraktoren
# (basierend auf AniWorld-Downloader Quellcode)
# ═══════════════════════════════════════════════════════

def _extract_from_hoster(hoster_name: str, url: str, html: str, soup: BeautifulSoup) -> str | None:
    """Dispatcht zum richtigen Hoster-Extraktor."""
    name = hoster_name.upper()

    if "VOE" in name:
        return _extract_voe(html, soup)
    elif "VIDOZA" in name:
        return _extract_vidoza(html, soup)
    elif "VIDMOLY" in name or "FILEMOON" in name:
        return _extract_vidmoly(html, soup)
    elif "STREAMTAPE" in name:
        return _extract_streamtape(html)
    else:
        return _extract_generic(html, soup)


def _extract_voe(html: str, soup: BeautifulSoup) -> str | None:
    """
    VOE Stream-URL extrahieren.

    VOE nutzt mehrere Obfuscation-Layer:
    1. Redirect via window.location.href zum echten Embed
    2. HLS-URL ist Base64-encoded in 'hls': '<base64>'
    3. Alternativ: ROT13 + Base64 + char-shift Obfuscation (neuere Version)

    Basiert auf: AniWorld-Downloader/extractors/provider/voe.py
    """

    # ── Schritt 1: Redirect-URL finden (VOE redirected oft nochmal) ──
    redirect_match = re.search(
        r"window\.location\.href\s*=\s*'(https://[^/]+/e/\w+)'\s*;",
        html
    )
    if redirect_match:
        # Wir haben nur das HTML, können nicht nochmal fetchen
        # Aber der redirect wurde bereits von httpx gefolgt
        pass

    # ── Schritt 2: HLS-URL aus Base64 extrahieren (Standard-Pattern) ──
    hls_match = re.search(r"'hls'\s*:\s*'([^']+)'", html)
    if not hls_match:
        hls_match = re.search(r'"hls"\s*:\s*"([^"]+)"', html)

    if hls_match:
        hls_value = hls_match.group(1)
        # Prüfe ob es Base64 ist
        try:
            decoded = base64.b64decode(hls_value).decode("utf-8")
            if decoded.startswith("http"):
                return decoded
        except Exception:
            # Kein Base64, direkte URL
            if hls_value.startswith("http"):
                return hls_value

    # ── Schritt 3: VOE Neue Obfuscation (JSON + ROT13 + Base64 + Shift) ──
    # Suche nach dem JSON-Array mit dem obfuscated String
    json_match = re.search(r'type="application/json">\["(.*?)"\]</script>', html)
    if json_match:
        encoded = json_match.group(1)
        decoded_url = _decode_voe_obfuscated(encoded)
        if decoded_url and decoded_url.startswith("http"):
            return decoded_url

    # Fallback: Suche nach obfuscated Variablen (ältere Versionen)
    obf_patterns = [
        r"var\s+\w+\s*=\s*'([A-Za-z0-9+/=]{50,})'",
        r'let\s+\w+\s*=\s*"([A-Za-z0-9+/=]{50,})"',
    ]
    for pattern in obf_patterns:
        match = re.search(pattern, html)
        if match:
            encoded = match.group(1)
            decoded_url = _decode_voe_obfuscated(encoded)
            if decoded_url and decoded_url.startswith("http"):
                return decoded_url

    # ── Schritt 4: Direkte MP4/M3U8 URL-Suche ──
    mp4_match = re.search(r"'mp4'\s*:\s*'([^']+)'", html)
    if not mp4_match:
        mp4_match = re.search(r'"mp4"\s*:\s*"([^"]+)"', html)
    if mp4_match:
        url = mp4_match.group(1)
        if url.startswith("http"):
            return url

    # ── Schritt 5: source/src Attribute ──
    for pattern in [
        r"source\s*=\s*['\"]([^'\"]+\.m3u8[^'\"]*)['\"]",
        r"'src'\s*:\s*'(https?://[^']+)'",
        r'"src"\s*:\s*"(https?://[^"]+)"',
    ]:
        match = re.search(pattern, html)
        if match and match.group(1).startswith("http"):
            return match.group(1)

    # ── Schritt 6: Generische Video-URL im HTML ──
    video_url = re.search(r'(https?://[^\s"\'<>]+\.(?:mp4|m3u8)\?[^\s"\'<>]*)', html)
    if video_url:
        return video_url.group(1)

    return None


def _decode_voe_obfuscated(encoded: str) -> str | None:
    """
    Decodiert VOE's neue Obfuscation.
    Pipeline: ROT13 → Base64-Decode → Char-Shift(-3) → Reverse → Base64-Decode
    (Pattern von aniworld_scraper/search_for_links.py)
    """
    try:
        # Schritt 1: ROT13
        step1 = codecs.decode(encoded, "rot_13")

        # Schritt 2: Separatoren entfernen
        # VOE nutzt verschiedene Separatoren wie !! ^^ ~@ etc.
        separators = ['@$', r'\^\^', '~@', r'%\?', r'\*~', '!!', '#&']
        step2 = step1
        for sep in separators:
            step2 = re.sub(sep, '', step2)

        # Schritt 3: Base64 Decode
        # Wir müssen padding ggf. korrigieren falls durch das Entfernen Zeichen fehlen
        missing_padding = len(step2) % 4
        if missing_padding:
            step2 += '=' * (4 - missing_padding)
        
        step3_bytes = base64.b64decode(step2)
        step3 = step3_bytes.decode("utf-8", errors='ignore')

        # Schritt 4: Char-Shift (jedes Zeichen um 3 nach links verschieben)
        step4 = "".join(chr(ord(char) - 3) for char in step3)

        # Schritt 5: Reverse
        step5 = step4[::-1]

        # Schritt 6: Base64 Decode (finales Ergebnis)
        # Auch hier padding prüfen
        missing_padding_final = len(step5) % 4
        if missing_padding_final:
            step5 += '=' * (4 - missing_padding_final)
            
        final_bytes = base64.b64decode(step5)
        result = final_bytes.decode("utf-8")

        # Falls es ein JSON ist, 'source' extrahieren
        if result.startswith('{'):
            try:
                res_json = json.loads(result)
                if 'source' in res_json:
                    return res_json['source']
            except:
                pass
        
        if result.startswith("http"):
            return result
    except Exception as e:
        log.debug(f"[VOE] Decode Fehler: {e}")

    # Fallback: Nur Base64
    try:
        result = base64.b64decode(encoded).decode("utf-8")
        if result.startswith("http"):
            return result
    except Exception:
        pass

    return None


def _extract_vidoza(html: str, soup: BeautifulSoup) -> str | None:
    """
    Vidoza Stream-URL extrahieren.
    Vidoza hat die Source direkt in <source> Tags oder in sourcesCode JS.

    Basiert auf: AniWorld-Downloader/extractors/provider/vidoza.py
    """

    # Methode 1: sourcesCode im Script (AniWorld-Downloader Pattern)
    for tag in soup.find_all("script"):
        if tag.string and "sourcesCode" in tag.string:
            match = re.search(r'src:\s*"([^"]+)"', tag.string)
            if match:
                return match.group(1)

    # Methode 2: <source> Tag
    source_tag = soup.find("source", src=True)
    if source_tag:
        src = source_tag.get("src", "")
        if src.startswith("http"):
            return src

    # Methode 3: Regex-Fallbacks
    for pattern in [
        r'<source\s+src="(https?://[^"]+)"',
        r'src:\s*"(https?://[^"]+\.mp4[^"]*)"',
        r'(https?://[^\s"\']+\.vidoza\.[^\s"\']+/[^\s"\']+\.mp4[^\s"\']*)',
    ]:
        match = re.search(pattern, html)
        if match:
            return match.group(1)

    return None


def _extract_vidmoly(html: str, soup: BeautifulSoup) -> str | None:
    """
    Vidmoly Stream-URL extrahieren.
    Vidmoly speichert die URL im file: Parameter in einem Script.

    Basiert auf: AniWorld-Downloader/extractors/provider/vidmoly.py
    """

    # Methode 1: file: "URL" im Script (AniWorld-Downloader Pattern)
    for script in soup.find_all("script"):
        if script.string:
            match = re.search(r'file:\s*"(https?://[^"]+)"', script.string)
            if match:
                return match.group(1)

    # Methode 2: Regex über das gesamte HTML
    for pattern in [
        r'file:\s*"(https?://[^"]+)"',
        r"file:\s*'(https?://[^']+)'",
        r'sources:\s*\[\{[^}]*file:\s*"(https?://[^"]+)"',
        r'(https?://[^\s"\']+\.m3u8[^\s"\']*)',
    ]:
        match = re.search(pattern, html)
        if match and match.group(1).startswith("http"):
            return match.group(1)

    # Methode 3: Packed JS entpacken
    packed = re.search(r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('([^']+)'", html, re.DOTALL)
    if packed:
        try:
            unpacked = _unpack_js(packed.group(0))
            if unpacked:
                match = re.search(r'file:\s*"(https?://[^"]+)"', unpacked)
                if match:
                    return match.group(1)
        except Exception:
            pass

    return None


def _extract_streamtape(html: str) -> str | None:
    """Streamtape Stream-URL extrahieren."""
    # Streamtape baut die URL aus mehreren Teilen im JS zusammen
    match = re.search(
        r"document\.getElementById\('robotlink'\)\.innerHTML\s*=\s*'([^']*)'",
        html,
    )
    if match:
        partial = match.group(1)
        match2 = re.search(r"\+\s*\('([^']+)'\)", html[match.end():])
        if match2:
            return "https:" + partial + match2.group(1)
    return None


def _extract_generic(html: str, soup: BeautifulSoup) -> str | None:
    """Generischer Extraktor als letzter Fallback."""

    # <source src="...">
    source_tag = soup.find("source", src=True)
    if source_tag:
        src = source_tag.get("src", "")
        if src.startswith("http"):
            return src

    # file: "..." in Scripts
    for script in soup.find_all("script"):
        if script.string:
            match = re.search(r'file:\s*"(https?://[^"]+\.(?:mp4|m3u8)[^"]*)"', script.string)
            if match:
                return match.group(1)

    # Letzte Hoffnung: Direkte URL im HTML
    for pattern in [
        r'<source\s+src="(https?://[^"]+)"',
        r'(https?://[^\s"\'<>]+\.(?:mp4|m3u8)\?[^\s"\'<>]+)',
    ]:
        match = re.search(pattern, html)
        if match:
            return match.group(1)

    return None


def _unpack_js(packed_code: str) -> str:
    """Dean Edwards JS Unpacker (für Vidmoly/Filemoon)."""
    try:
        match = re.search(
            r"eval\(function\(p,a,c,k,e,[dr]\)\{.*?\}\('(.+)',(\d+),(\d+),'([^']+)'",
            packed_code,
            re.DOTALL,
        )
        if not match:
            return ""

        payload = match.group(1)
        a = int(match.group(2))
        c = int(match.group(3))
        keywords = match.group(4).split("|")

        def base_n(num, base):
            chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if num < base:
                return chars[num]
            return base_n(num // base, base) + chars[num % base]

        for i in range(c - 1, -1, -1):
            if i < len(keywords) and keywords[i]:
                word = base_n(i, a)
                payload = re.sub(r"\b" + word + r"\b", keywords[i], payload)

        return payload
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════
# HLS Qualitäts-Auswahl (für m3u8 Master-Playlists)
# ═══════════════════════════════════════════════════════

async def resolve_best_quality(m3u8_url: str) -> str:
    """
    Falls die URL eine Master-Playlist (.m3u8) ist,
    wähle den Stream mit der höchsten Auflösung.
    Falls es direkt ein Stream ist, gib die URL zurück.
    """
    if not m3u8_url.endswith(".m3u8") and ".m3u8" not in m3u8_url:
        return m3u8_url  # Kein m3u8, direkte URL

    async with _client() as client:
        try:
            resp = await client.get(m3u8_url)
            content = resp.text

            if "#EXT-X-STREAM-INF" not in content:
                return m3u8_url  # Bereits ein konkreter Stream

            # Master Playlist: Finde den Stream mit der höchsten Bandbreite
            best_url = None
            best_bandwidth = 0

            lines = content.strip().split("\n")
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    bw_match = re.search(r"BANDWIDTH=(\d+)", line)
                    if bw_match:
                        bandwidth = int(bw_match.group(1))
                        if bandwidth > best_bandwidth and i + 1 < len(lines):
                            best_bandwidth = bandwidth
                            stream_url = lines[i + 1].strip()
                            if not stream_url.startswith("http"):
                                # Relative URL → absolut machen
                                base = m3u8_url.rsplit("/", 1)[0]
                                stream_url = base + "/" + stream_url
                            best_url = stream_url

            return best_url if best_url else m3u8_url

        except Exception:
            return m3u8_url
