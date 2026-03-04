# Backend-Konfiguration fur MyWorldMediaCenter

PORT = 7800

# AniWorld Basis-URL
ANIWORLD_BASE = "https://aniworld.to"
# SerienStream Basis-URL
STO_BASE = "https://serienstream.to"
# Stream Kiste (Filme)
STREAMKISTE_BASE = "https://streamkiste.al"
# Filmpalast (Filme)
FILMPALAST_BASE = "https://filmpalast.to"

# Bevorzugte Hoster-Reihenfolge (erster verfügbarer wird genommen)
PREFERRED_HOSTERS = ["Vidmoly", "Filemoon", "Streamtape", "VOE"]

# Cache-Zeiten in Sekunden
LIBRARY_CACHE_TTL = 3600
EPISODE_CACHE_TTL = 1800
STREAM_CACHE_TTL = 600

# TMDB API Key (optional für Metadaten wie Beschreibungen/Rating)
# Hol dir einen kostenlosen Key unter https://www.themoviedb.org/settings/api
# Alternativ: export TMDB_API_KEY=... als Umgebungsvariable setzen
import os
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "16e7288e85832ed2d948c963efd1042a")

# Request Headers (Browser-Simulation)
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://aniworld.to/",
}

# Stream Kiste (Filme)
STREAMKISTE_BASE = "https://streamkiste.tv"
