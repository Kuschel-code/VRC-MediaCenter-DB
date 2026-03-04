#!/usr/bin/env python3
"""
VRC Media Center - GitHub Database Generator v5
=============================================================================
Scraped Animes, Filme und Serien von AniWorld, Filmpalast und SerienStream.

Sprachen pro Episode:
  - Ger-Dub  → episode_id: <slug>-s1-ep1-ger-dub
  - Ger-Sub  → episode_id: <slug>-s1-ep1-ger-sub
  - Eng-Sub  → episode_id: <slug>-s1-ep1-eng-sub
  - Eng-Dub  → episode_id: <slug>-s1-ep1-eng-dub

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
MAX_SERIES = 0   # 0 = alle

LANGUAGES = ["ger-dub", "ger-sub", "eng-sub", "eng-dub"]
LANG_DISPLAY = {"ger-dub": "Ger Dub", "ger-sub": "Ger Sub", "eng-sub": "Eng Sub", "eng-dub": "Eng Dub"}
LANG_NUMBER  = {"ger-dub": "1", "ger-sub": "2", "eng-sub": "3", "eng-dub": "4"}

# Alle N Eintraege auf GitHub pushen (0 = nur am Ende)
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
    from config import ANIWORLD_BASE, STO_BASE, FILMPALAST_BASE, PREFERRED_HOSTERS
except ImportError as e:
    print(f"[!] Fehler: {e}")
    print(f"    Stelle sicher dass scraper.py und config.py im selben Ordner liegen.")
    sys.exit(1)

import json
from bs4 import BeautifulSoup
import httpx


# ── Sprachspezifische Stream-URL ─────────────────────────────
async def get_stream_for_language(client: httpx.AsyncClient,
                                   ep_url_path: str, lang: str,
                                   base_url: str = None) -> str | None:
    if base_url is None:
        base_url = ANIWORLD_BASE
    lang_num = LANG_NUMBER.get(lang, "1")
    url = base_url + ep_url_path + f"?lang={lang_num}"

    try:
        # Nutze _fetch_page (geht über Cloudflare Worker Proxy bei CUII-Domains)
        html = await scraper._fetch_page(client, url)
        if not html:
            return None
        
        # SerienStream: Spezial-Resolver für iframe → redirect → VOE → delivery → HLS
        if any(d in base_url for d in ["serienstream.to", "s.to", "bs.to"]):
            stream = await asyncio.to_thread(scraper._resolve_serienstream_hoster, html, base_url)
            if stream:
                return stream
            return None  # Kein Fallback nötig, STO nutzt nur diesen Weg
        
        # AniWorld/Standard: Pruefe ob Sprache verfuegbar (Hoster-Links vorhanden?)
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
                rurl = base_url + rurl
            # Nur die Embed-URL speichern (Worker löst live auf)
            if rurl.startswith("http"):
                return rurl
    except Exception:
        pass
    return None


# ── Filmpalast Stream-URL (keine Sprachvarianten) ───────────
async def get_filmpalast_stream(client: httpx.AsyncClient,
                                 url_path: str) -> str | None:
    # url_path kommt als //filmpalast.to/stream/... (vollständig)
    if url_path.startswith("//"):
        url = "https:" + url_path
    elif url_path.startswith("http"):
        url = url_path
    else:
        url = FILMPALAST_BASE + url_path
    try:
        html = await scraper._fetch_page(client, url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")

        # Filmpalast: ul.currentStreamLinks mit .hostName und a.iconPlay
        hoster_links = []
        for ul in soup.select('ul.currentStreamLinks'):
            name_tag = ul.select_one('.hostName')
            name = name_tag.get_text(strip=True) if name_tag else "Unknown"
            # VOE HD links haben href, veev.to hat data-player-url
            btn = ul.select_one('li.streamPlayBtn a.iconPlay[href]')
            if btn and btn.get("href") and btn["href"].startswith("http"):
                hoster_links.append({
                    "name": name,
                    "redirect_url": btn.get("href"),
                })
            # Fallback: data-player-url (für veev.to etc.)
            embed_btn = ul.select_one('a.iconPlay[data-player-url]')
            if embed_btn and embed_btn.get("data-player-url"):
                hoster_links.append({
                    "name": name + " (embed)",
                    "redirect_url": embed_btn.get("data-player-url"),
                })

        if not hoster_links:
            return None

        # Nach Praeferenz sortieren (VOE zuerst, da Worker es auflösen kann)
        hoster_links.sort(key=lambda h: next(
            (i for i, p in enumerate(PREFERRED_HOSTERS) if p.upper() in h["name"].upper()),
            len(PREFERRED_HOSTERS)
        ))

        # Erste verfügbare Hoster-Embed-URL zurückgeben (Worker löst live auf)
        for hoster in hoster_links:
            href = hoster["redirect_url"]
            if href and href.startswith("http"):
                return href

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
            "stream_lines": [], "movie_lines": [], "series_lines": []}


def save_progress(p: dict):
    PROGRESS_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Dateien schreiben ────────────────────────────────────────
def write_db_files(p: dict):
    # 1. Hauptdateien
    (DB_PATH / "anime.txt").write_text("\n".join(p["anime_lines"]), encoding="utf-8")
    (DB_PATH / "movies.txt").write_text("\n".join(p["movie_lines"]), encoding="utf-8")
    (DB_PATH / "series.txt").write_text("\n".join(p.get("series_lines", [])), encoding="utf-8")
    (DB_PATH / "episodes.txt").write_text("\n".join(p["episode_lines"]), encoding="utf-8")
    (DB_PATH / "streams.txt").write_text("\n".join(p["stream_lines"]), encoding="utf-8")

    # 2. Anime Splitting (A-Z) für VRChat 100KB Limit
    chunks = {}
    for line in p["anime_lines"]:
        if not line.strip(): continue
        title = line.split("|")[0].upper()
        first_char = title[0] if title else "#"
        if not first_char.isalpha():
            key = "#"
        else:
            key = first_char
        
        if key not in chunks: chunks[key] = []
        chunks[key].append(line)
    
    # Schreib Chunks
    for key, lines in chunks.items():
        fname = f"anime_{key}.txt"
        (DB_PATH / fname).write_text("\n".join(lines), encoding="utf-8")


# ── Git Push (fuer Zwischen-Pushes) ─────────────────────────
def git_push(msg: str = "Auto-update library data"):
    is_ci = os.environ.get("CI", "false") == "true"
    try:
        if is_ci:
            subprocess.run(["git", "config", "user.name", "GitHub Actions Bot"],
                           cwd=DB_PATH, check=True)
            subprocess.run(["git", "config", "user.email", "actions@github.com"],
                           cwd=DB_PATH, check=True)
        subprocess.run(["git", "add", "anime.txt", "episodes.txt", "streams.txt",
                        "movies.txt", "series.txt", "progress.json"], cwd=DB_PATH, check=True)
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
    print("  VRC Media Center - Database Generator v5")
    print(f"  Sprachen: {', '.join(LANGUAGES)}")
    print(f"  Animes:   {'alle' if MAX_ANIME == 0 else MAX_ANIME}")
    print(f"  Filme:    {'alle' if MAX_MOVIES == 0 else MAX_MOVIES}")
    print(f"  Serien:   {'alle' if MAX_SERIES == 0 else MAX_SERIES}")
    print("=" * 60)

    p = load_progress()
    # Ensure series_lines exists (backwards compat)
    if "series_lines" not in p:
        p["series_lines"] = []
    done = set(p["done_slugs"])
    if done:
        print(f"[*] Fortschritt: {len(done)} Eintraege bereits fertig")

    # ═══════════════════════════════════════════════════════
    # [1] Filme von Filmpalast
    # ═══════════════════════════════════════════════════════
    print("\n[1] Lade Filme von Filmpalast...")
    try:
        movies = await scraper.fetch_library("movies")
        if MAX_MOVIES > 0:
            movies = movies[:MAX_MOVIES]
        
        async with scraper._client() as client:
            for mi, m in enumerate(movies):
                mc = m.get("content_id", "")
                m_title = m.get("title", "")
                m_thumb = m.get("thumb", "")
                m_url   = m.get("url_path", "")
                
                if not mc: continue
                
                # Film-Eintrag für Library
                e = f"{m_title}|{m_thumb}|{mc}|{m.get('genre','')}|{m.get('year','')}|{m.get('rating','')}"
                if e not in p["movie_lines"]:
                    p["movie_lines"].append(e)

                # Filmpalast hat keine Sprachvarianten – nur einen Stream
                lid = f"{mc}-s1-ep1"
                ep_line = f"{mc}|{m_title}|{lid}"
                if ep_line not in p["episode_lines"]:
                    p["episode_lines"].append(ep_line)

                stream = await get_filmpalast_stream(client, m_url)
                if stream:
                    s = f"{lid}|{stream}"
                    if s not in p["stream_lines"]:
                        p["stream_lines"].append(s)
                    print(f"    [{mi+1}/{len(movies)}] [OK] {mc}: {stream[:55]}...")
                else:
                    print(f"    [{mi+1}/{len(movies)}] [--] {mc}: kein Stream")
                
                await asyncio.sleep(0.5)

        print(f"    -> {len(p['movie_lines'])} Filme verarbeitet")
    except Exception as e:
        print(f"  [!] Filme Fehler: {e}")

    write_db_files(p)
    save_progress(p)
    git_push("Update: Filme von Filmpalast")

    # ═══════════════════════════════════════════════════════
    # [2] Serien von SerienStream (Optimiert: Parallel)
    # ═══════════════════════════════════════════════════════
    print("\n[2] Lade Serien von SerienStream...")
    try:
        series = await scraper.fetch_library("series")
        if MAX_SERIES > 0:
            series = series[:MAX_SERIES]
        
        # Semaphore für Rate-Limiting (15 gleichzeitige Requests)
        sem = asyncio.Semaphore(15)
        
        async def fetch_lang_with_limit(client, ep_urlpath, lang, base):
            """Fetch einer Sprache mit Semaphore Rate-Limiting."""
            async with sem:
                return await get_stream_for_language(client, ep_urlpath, lang, base)
        
        processed_since_save = 0
        
        for si, s_item in enumerate(series):
            sc = s_item.get("content_id", "")
            s_title = s_item.get("title", "")
            s_thumb = s_item.get("thumb", "")
            
            if not sc or sc in done: continue
            
            # TMDB Poster-Fallback wenn kein Thumbnail vorhanden
            if not s_thumb:
                try:
                    tmdb = await scraper.fetch_tmdb_metadata(s_title, "series")
                    if tmdb and tmdb.get("poster"):
                        s_thumb = tmdb["poster"]
                except Exception:
                    pass
            
            # Serien-Eintrag
            s_entry = f"{s_title}|{s_thumb}|{sc}|{s_item.get('genre','')}|{s_item.get('year','')}|{s_item.get('rating','')}"
            if s_entry not in p["series_lines"]:
                p["series_lines"].append(s_entry)

            # Episoden laden
            episodes = await scraper.fetch_episodes(sc)
            ep_count = len(episodes)
            print(f"  [{si+1}/{len(series)}] {sc}: {ep_count} Episoden")
            
            if ep_count == 0:
                p["done_slugs"].append(sc)
                done.add(sc)
                continue

            async with scraper._client() as client:
                # Alle Episoden × Sprachen parallel abrufen (in Batches von 10 Episoden)
                for batch_start in range(0, ep_count, 10):
                    batch = episodes[batch_start:batch_start + 10]
                    
                    async def process_episode(ep):
                        """Alle 4 Sprachen einer Episode parallel abrufen."""
                        ep_id      = ep.get("episode_id", "")
                        ep_title   = ep.get("title", "")
                        ep_urlpath = ep.get("url_path", "")
                        if not ep_id:
                            return
                        
                        # Alle Sprachen parallel starten
                        tasks = {}
                        for lang in LANGUAGES:
                            if ep_urlpath:
                                tasks[lang] = fetch_lang_with_limit(client, ep_urlpath, lang, STO_BASE)
                            else:
                                async def _noop():
                                    return None
                                tasks[lang] = _noop()
                        
                        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
                        
                        for lang, result in zip(tasks.keys(), results):
                            lid   = f"{ep_id}-{lang}"
                            ltitle = f"{ep_title} ({LANG_DISPLAY[lang]})"
                            
                            ep_line = f"{sc}|{ltitle}|{lid}"
                            if ep_line not in p["episode_lines"]:
                                p["episode_lines"].append(ep_line)
                            
                            stream = result if isinstance(result, str) else None
                            if stream:
                                s_line = f"{lid}|{stream}"
                                if s_line not in p["stream_lines"]:
                                    p["stream_lines"].append(s_line)
                                print(f"    [OK] {lang}: {stream[:55]}...")
                            # Keine Ausgabe für nicht verfügbare Streams (reduziert Spam)
                    
                    # 10 Episoden gleichzeitig verarbeiten
                    await asyncio.gather(*[process_episode(ep) for ep in batch])

            p["done_slugs"].append(sc)
            done.add(sc)
            processed_since_save += 1
            
            # Nur alle 50 Serien speichern + pushen (statt jede einzelne)
            if processed_since_save >= PUSH_INTERVAL:
                save_progress(p)
                write_db_files(p)
                git_push(f"Update: {processed_since_save} Serien")
                processed_since_save = 0

        print(f"    -> {len(p['series_lines'])} Serien verarbeitet")
    except Exception as e:
        print(f"  [!] Serien Fehler: {e}")
        import traceback
        traceback.print_exc()

    write_db_files(p)
    save_progress(p)
    git_push("Update: Serien von SerienStream")

    # ═══════════════════════════════════════════════════════
    # [3] Anime von AniWorld
    # ═══════════════════════════════════════════════════════
    print("\n[3] Lade Anime-Liste von AniWorld...")
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
                        stream = await get_stream_for_language(client, ep_urlpath, lang, ANIWORLD_BASE)

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
                git_push(f"Update: {len(done)} Eintraege verarbeitet")
                pushed_count = 0

    write_db_files(p)
    save_progress(p)

    print(f"\n[+] anime.txt:    {len(p['anime_lines'])}")
    print(f"[+] movies.txt:   {len(p['movie_lines'])}")
    print(f"[+] series.txt:   {len(p['series_lines'])}")
    print(f"[+] episodes.txt: {len(p['episode_lines'])}")
    print(f"[+] streams.txt:  {len(p['stream_lines'])}")

    git_push("Final update: alle Inhalte verarbeitet")

    # Fortschritt loeschen nach Abschluss
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()

    print("\n" + "=" * 60)
    print("  Fertig! In Unity: My World > Setup Cloudflare URLs")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
