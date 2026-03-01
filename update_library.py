#!/usr/bin/env python3
"""
VRC Media Center - Database Updater
====================================
Dieses Script scraped AniWorld.to und generiert die Textdateien
fuer das GitHub-Repository, von dem deine VRChat-Welt die Daten bezieht.

Usage:
    python update_library.py

Ergebnis: anime.txt, movies.txt, series.txt, episodes.txt, streams.txt
          werden aktualisiert und koennen dann mit 'git push' auf GitHub hochgeladen werden.
"""

import re
import sys
import json
import time
import base64
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# ============================================================
# KONFIGURATION - Hier anpassen!
# ============================================================
ANIWORLD_BASE    = "https://aniworld.to"
# Hoster-Prioritaet (erste = beste Qualitaet / Stabilitaet)
PREFERRED_HOSTERS = ["VOE", "Vidoza", "Vidmoly", "Streamtape"]
# Wie viele Animes werden maximal gescraped? (0 = alle)
MAX_ANIME = 50
# Wie viele Filme werden maximal gescraped? (0 = alle)
MAX_MOVIES = 30
# Timeout fuer HTTP-Anfragen in Sekunden
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
}

# ============================================================
# HTTP UTILS
# ============================================================

def fetch(url: str, retries: int = 3) -> str | None:
    """Fetch a URL and return the UTF-8 text body, or None on failure."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            print(f"  [HTTP {e.code}] {url}")
            if e.code in (403, 404):
                return None
        except Exception as e:
            print(f"  [Error attempt {attempt+1}] {url}: {e}")
            time.sleep(2)
    return None

# ============================================================
# ANIWORLD SCRAPER
# ============================================================

def scrape_anime_list() -> list[dict]:
    """Scrapes the AniWorld.to anime list page and returns a list of dicts."""
    print("[*] Lade Anime-Liste von AniWorld.to...")
    html = fetch(f"{ANIWORLD_BASE}/animes")
    if not html:
        print("[!] Konnte AniWorld-Anime-Liste nicht laden.")
        return []

    # Match all anime links: /anime/stream/<slug>
    slugs = re.findall(r'href="/anime/stream/([a-z0-9\-]+)"', html)
    seen = []
    for s in slugs:
        if s not in seen:
            seen.append(s)

    if MAX_ANIME > 0:
        seen = seen[:MAX_ANIME]

    print(f"  -> {len(seen)} Animes gefunden.")
    results = []
    for i, slug in enumerate(seen):
        print(f"  [{i+1}/{len(seen)}] {slug}")
        info = scrape_anime_info(slug)
        if info:
            results.append(info)
        time.sleep(0.5)
    return results


def scrape_anime_info(slug: str) -> dict | None:
    """Scrape title, genre, year, rating and season/episode list for one anime."""
    url  = f"{ANIWORLD_BASE}/anime/stream/{slug}"
    html = fetch(url)
    if not html:
        return None

    title  = re.search(r'<h1[^>]*itemprop="name"[^>]*>(.*?)</h1>', html)
    title  = title.group(1).strip() if title else slug.replace("-", " ").title()
    genre  = re.search(r'Genre.*?<a[^>]*>(.*?)</a>', html, re.DOTALL)
    genre  = genre.group(1).strip() if genre else ""
    year   = re.search(r'(\d{4})', title)
    year   = year.group(1) if year else ""
    rating = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', html)
    rating = rating.group(1) if rating else ""

    # Season slugs
    seasons = re.findall(r'href="/anime/stream/' + slug + r'/staffel-(\d+)"', html)

    return {
        "slug":    slug,
        "title":   title,
        "genre":   genre,
        "year":    year,
        "rating":  rating,
        "seasons": list(set(seasons)),
    }


def scrape_episodes(anime_slug: str, season_num: str) -> list[dict]:
    """Returns list of {slug, title} for episodes of a season."""
    url  = f"{ANIWORLD_BASE}/anime/stream/{anime_slug}/staffel-{season_num}"
    html = fetch(url)
    if not html:
        return []

    # Match episode slugs: /anime/stream/<anime>/staffel-<n>/episode-<m>
    pattern = rf'/anime/stream/{anime_slug}/staffel-{season_num}/episode-(\d+)'
    ep_nums = sorted(set(re.findall(pattern, html)))
    results = []
    for ep_num in ep_nums:
        ep_slug = f"{anime_slug}-s{season_num.zfill(2)}e{ep_num.zfill(2)}"
        results.append({"slug": ep_slug, "number": ep_num})
    return results


def scrape_stream_url(anime_slug: str, season: str, episode: str, hoster: str = None) -> str | None:
    """
    Fetches the stream URL for a given episode from AniWorld.
    Returns a direct mp4 or m3u8 URL, or None.
    """
    url  = f"{ANIWORLD_BASE}/anime/stream/{anime_slug}/staffel-{season}/episode-{episode}"
    html = fetch(url)
    if not html:
        return None

    # Find available hosters
    hosters_found = re.findall(r'data-link-target="([^"]+)"[^>]*>.*?([A-Za-z]+)</a>', html, re.DOTALL)
    redirect_url  = None

    for h_url, h_name in hosters_found:
        if hoster and hoster.lower() not in h_name.lower():
            continue
        for preferred in PREFERRED_HOSTERS:
            if preferred.lower() in h_name.lower():
                redirect_url = h_url
                break
        if redirect_url:
            break

    if not redirect_url:
        # Fallback: try first link found
        first = re.search(r'data-link-target="(https?://[^"]+)"', html)
        if first:
            redirect_url = first.group(1)

    if not redirect_url:
        return None

    return extract_stream_url(redirect_url)


def extract_stream_url(hoster_url: str) -> str | None:
    """
    Extracts direct mp4/m3u8 URL from a hoster page (VOE, Vidoza, Vidmoly, Streamtape).
    """
    html = fetch(hoster_url)
    if not html:
        return None

    domain = hoster_url.lower()

    # VOE.sx – Base64 encoded HLS or mp4
    if "voe.sx" in domain:
        # Try HLS link
        m = re.search(r"'hls'\s*:\s*'([^']+\.m3u8[^']*)'", html)
        if not m:
            m = re.search(r'"hls"\s*:\s*"([^"]+\.m3u8[^"]*)"', html)
        if m:
            url = m.group(1)
            # Sometimes base64 encoded
            try:
                url = base64.b64decode(url + "==").decode()
            except Exception:
                pass
            return url
        # Fallback mp4
        m = re.search(r"'mp4'\s*:\s*'([^']+\.mp4[^']*)'", html)
        if m:
            return m.group(1)

    # Vidoza – sourcesCode / <source src="...">
    if "vidoza" in domain:
        m = re.search(r'sourcesCode\s*:\s*\[\s*\{.*?src\s*:\s*"([^"]+)"', html, re.DOTALL)
        if not m:
            m = re.search(r'<source\s+src="([^"]+\.mp4[^"]*)"', html)
        if m:
            return m.group(1)

    # Vidmoly – file: '...'
    if "vidmoly" in domain:
        m = re.search(r"file\s*:\s*'([^']+\.(?:m3u8|mp4)[^']*)'", html)
        if m:
            return m.group(1)

    # Streamtape – /get_video?... concatenation
    if "streamtape" in domain:
        m = re.search(r"robotlink.*?(/get_video\?[^'\"]+)", html, re.DOTALL)
        if m:
            return "https://streamtape.com" + m.group(1)

    # Generic fallback
    m = re.search(r'(?:file|src)\s*[=:]\s*[\'"]([^\'"]+\.(?:m3u8|mp4)[^\'"]*)[\'"]', html)
    if m:
        return m.group(1)

    return None


# ============================================================
# WRITE OUTPUT FILES
# ============================================================

def write_files(animes: list[dict]):
    db_path = Path(__file__).parent

    anime_lines   = []
    episode_lines = []
    stream_lines  = []

    for anime in animes:
        slug   = anime["slug"]
        title  = anime["title"]
        genre  = anime["genre"]
        year   = anime["year"]
        rating = anime["rating"]

        # --- anime.txt line ---
        content_id = slug
        thumb_url  = ""  # No thumbnails without login
        anime_lines.append(f"{title}|{thumb_url}|{content_id}|{genre}|{year}|{rating}")

        # --- episodes per season ---
        for season_num in sorted(anime.get("seasons", ["1"])):
            episodes = scrape_episodes(slug, season_num)
            for ep in episodes:
                ep_slug  = ep["slug"]
                ep_title = f"Episode {ep['number']}"

                # episode_lines: contentId|Title|EpisodeID
                episode_lines.append(f"{content_id}|{ep_title}|{ep_slug}")

                # Try to get stream URL
                stream_url = scrape_stream_url(slug, season_num, ep["number"])
                if stream_url:
                    stream_lines.append(f"{ep_slug}|{stream_url}")
                else:
                    print(f"    [!] Keine Stream-URL fuer {ep_slug}")

                time.sleep(0.3)

    # Write anime.txt
    (db_path / "anime.txt").write_text("\n".join(anime_lines), encoding="utf-8")
    print(f"[+] anime.txt: {len(anime_lines)} Eintraege")

    # Parse episode_lines to per-series episode files format
    # Format fuer VRChat: EpisodeTitel|EpisodenID (grouped per series, written in episodes.txt)
    # We store all of them as: contentId|EpisodeTitel|EpisodenID
    (db_path / "episodes.txt").write_text("\n".join(episode_lines), encoding="utf-8")
    print(f"[+] episodes.txt: {len(episode_lines)} Eintraege")

    # streams.txt: EpisodenID|DirectURL
    (db_path / "streams.txt").write_text("\n".join(stream_lines), encoding="utf-8")
    print(f"[+] streams.txt: {len(stream_lines)} Eintraege")

    # Empty placeholders for movies.txt and series.txt (can be filled later)
    if not (db_path / "movies.txt").exists():
        (db_path / "movies.txt").write_text("", encoding="utf-8")
        print("[+] movies.txt: angelegt (leer, manuell befuellen)")
    if not (db_path / "series.txt").exists():
        (db_path / "series.txt").write_text("", encoding="utf-8")
        print("[+] series.txt: angelegt (leer, manuell befuellen)")


# ============================================================
# GIT AUTO-PUSH
# ============================================================

def git_push():
    db_path = Path(__file__).parent
    try:
        subprocess.run(["git", "add", "."], cwd=db_path, check=True)
        subprocess.run(["git", "commit", "-m", "Update library data"], cwd=db_path, check=True)
        subprocess.run(["git", "push"], cwd=db_path, check=True)
        print("[+] Dateien erfolgreich auf GitHub gepusht!")
    except subprocess.CalledProcessError as e:
        print(f"[!] Git-Fehler: {e}. Bitte manuell pushen: cd '{db_path}' && git push")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  VRC Media Center - Database Updater")
    print("=" * 60)
    print()

    animes = scrape_anime_list()
    if not animes:
        print("[!] Keine Animes gefunden. Abbruch.")
        sys.exit(1)

    write_files(animes)

    print()
    print("[*] Lade Aenderungen auf GitHub hoch...")
    git_push()

    print()
    print("=" * 60)
    print("  Fertig! Gehe jetzt in Unity und druecke:")
    print("  My World > Bake URLs (Datenbank updaten)")
    print("=" * 60)
