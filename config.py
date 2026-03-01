# Backend-Konfiguration für MyWorldMediaCenter
# Passe diese Werte an dein Setup an.

# Server-Port
PORT = 7800

# AniWorld Basis-URL
ANIWORLD_BASE = "https://aniworld.to"

# Bevorzugte Hoster-Reihenfolge (erster verfügbarer wird genommen)
# VOE liefert meist HLS/MP4. Vidoza wurde von AniWorld offiziell eingestellt (2025).
PREFERRED_HOSTERS = ["VOE", "Vidmoly", "Streamtape"]

# Cache-Zeiten in Sekunden
LIBRARY_CACHE_TTL = 3600      # 1 Stunde für Inhaltslisten
EPISODE_CACHE_TTL = 1800      # 30 Minuten für Episodenlisten
STREAM_CACHE_TTL = 600        # 10 Minuten für Stream-URLs (können ablaufen)

# Request Headers (Browser-Simulation)
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://aniworld.to/",
}

# Manuell konfigurierte Inhalte (falls AniWorld-Scraping nicht gewünscht)
# Format: Liste von Dicts mit title, thumb, content_id, genre, year, rating, url_path
# url_path = AniWorld-Pfad z.B. "/anime/stream/attack-on-titan"
MANUAL_ANIME = []
MANUAL_MOVIES = []
MANUAL_SERIES = []
