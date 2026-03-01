#!/usr/bin/env python3
"""
VRC Media Center - GitHub Database Generator
=============================================
Importiert Backend/scraper.py und schreibt die Ergebnisse
als GitHub-Textdateien (anime.txt, episodes.txt, streams.txt).

Usage:
    python update_library.py

Ergebnis wird automatisch auf GitHub gepusht.
"""

import sys
import asyncio
import subprocess
from pathlib import Path

# ── Pfade einrichten ────────────────────────────────────────
DB_PATH      = Path(__file__).parent
BACKEND_PATH = Path(r"k:\Unity\Avis\My World\Assets\MyWorldMediaCenter\Backend")

# Backend-Ordner in sys.path aufnehmen, damit scraper.py importierbar ist
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

# ── Konfiguration ───────────────────────────────────────────
MAX_ANIME  = 10   # Wieviele Animes scrapen? (0 = alle)
MAX_MOVIES = 10   # Wieviele Filme?

# ── Dependencies prüfen ─────────────────────────────────────
def install_deps():
    deps = ["httpx", "beautifulsoup4", "lxml", "cachetools", "cloudscraper"]
    for dep in deps:
        try:
            __import__(dep.replace("-", "_").replace("beautifulsoup4", "bs4"))
        except ImportError:
            print(f"[*] Installiere {dep}...")
            subprocess.run([sys.executable, "-m", "pip", "install", dep, "--quiet"], check=True)

install_deps()

# ── Scraper importieren ─────────────────────────────────────
try:
    import scraper
except ImportError as e:
    print(f"[!] Konnte scraper.py nicht importieren von: {BACKEND_PATH}")
    print(f"    Fehler: {e}")
    sys.exit(1)


# ── Git Push ─────────────────────────────────────────────────
def git_push():
    try:
        subprocess.run(["git", "add", "."], cwd=DB_PATH, check=True)
        subprocess.run(["git", "commit", "-m", "Update library data"], cwd=DB_PATH, check=True)
        subprocess.run(["git", "push"], cwd=DB_PATH, check=True)
        print("[+] GitHub Push erfolgreich!")
    except subprocess.CalledProcessError as e:
        print(f"[!] Git-Fehler: {e}")


# ── Main ──────────────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("  VRC Media Center - GitHub Database Generator")
    print("=" * 60)
    print(f"  Backend-Pfad: {BACKEND_PATH}")
    print()

    anime_lines   = []
    episode_lines = []
    stream_lines  = []

    # ── Anime-Liste laden ────────────────────────────────────
    print("[1/3] Lade Anime-Liste...")
    animes = await scraper.fetch_library("anime")
    print(f"      -> {len(animes)} Animes gefunden")
    if MAX_ANIME > 0:
        animes = animes[:MAX_ANIME]
    print(f"      -> Verarbeite {len(animes)} Animes")

    for item in animes:
        title      = item.get("title", "")
        thumb      = item.get("thumb", "")
        content_id = item.get("content_id", "")
        genre      = item.get("genre", "")
        year       = item.get("year", "")
        rating     = item.get("rating", "")
        if content_id:
            anime_lines.append(f"{title}|{thumb}|{content_id}|{genre}|{year}|{rating}")

    # ── Film-Liste laden ──────────────────────────────────────
    print("[2/3] Lade Film-Liste...")
    movies = await scraper.fetch_library("movies")
    print(f"      -> {len(movies)} Filme gefunden")
    if MAX_MOVIES > 0:
        movies = movies[:MAX_MOVIES]

    movie_lines = []
    for item in movies:
        content_id = item.get("content_id", "")
        if content_id:
            movie_lines.append(
                f"{item.get('title','')}|{item.get('thumb','')}|{content_id}|"
                f"{item.get('genre','')}|{item.get('year','')}|{item.get('rating','')}"
            )

    # ── Episoden + Streams laden ──────────────────────────────
    print("[3/3] Lade Episodenlisten und Stream-URLs...")
    all_items = [(a, "anime") for a in animes] + [(m, "movie") for m in movies]

    for item, itype in all_items:
        content_id = item.get("content_id", "")
        if not content_id:
            continue

        print(f"  -> {content_id} ({itype})")
        episodes = await scraper.fetch_episodes(content_id)
        print(f"     {len(episodes)} Episoden")

        for ep in episodes:
            ep_title = ep.get("title", "")
            ep_id    = ep.get("episode_id", "")
            if not ep_id:
                continue

            episode_lines.append(f"{content_id}|{ep_title}|{ep_id}")

            # Stream-URL holen
            stream_url = await scraper.get_stream_url(ep_id)
            if stream_url:
                stream_lines.append(f"{ep_id}|{stream_url}")
                print(f"     [OK] {ep_id}: {stream_url[:60]}...")
            else:
                print(f"     [!] Kein Stream: {ep_id}")

    # ── Dateien schreiben ─────────────────────────────────────
    (DB_PATH / "anime.txt").write_text("\n".join(anime_lines), encoding="utf-8")
    (DB_PATH / "movies.txt").write_text("\n".join(movie_lines), encoding="utf-8")
    (DB_PATH / "episodes.txt").write_text("\n".join(episode_lines), encoding="utf-8")
    (DB_PATH / "streams.txt").write_text("\n".join(stream_lines), encoding="utf-8")

    if not (DB_PATH / "series.txt").exists():
        (DB_PATH / "series.txt").write_text("", encoding="utf-8")

    print()
    print(f"[+] anime.txt:    {len(anime_lines)} Eintraege")
    print(f"[+] movies.txt:   {len(movie_lines)} Eintraege")
    print(f"[+] episodes.txt: {len(episode_lines)} Eintraege")
    print(f"[+] streams.txt:  {len(stream_lines)} Eintraege")

    # ── GitHub Push ───────────────────────────────────────────
    print()
    print("[*] Pushe auf GitHub...")
    git_push()

    print()
    print("=" * 60)
    print("  Fertig! In Unity: My World > Bake URLs von GitHub")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
