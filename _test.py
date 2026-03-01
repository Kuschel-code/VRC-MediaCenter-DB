#!/usr/bin/env python3
"""Quick test: cloudscraper + AniWorld redirect extraction."""
import re, time, base64
try:
    import cloudscraper
except ImportError:
    print("FEHLER: pip install cloudscraper")
    exit(1)

scraper = cloudscraper.create_scraper()

def fetch(url):
    try:
        r = scraper.get(url, timeout=20, allow_redirects=True)
        return r.text, r.url
    except Exception as e:
        print(f"ERR: {e}")
        return None, None

# Step 1: Episode page
print("[1] Lade Episode-Seite mit cloudscraper...")
html, furl = fetch("https://aniworld.to/anime/stream/a-returners-magic-should-be-special/staffel-1/episode-1")
if not html:
    print("FEHLER: Seite konnte nicht geladen werden.")
    exit(1)

print(f"    Status: erhalten ({len(html)} Zeichen)")
redirects = re.findall(r'href="(https://aniworld\.to/redirect/\d+)"', html)
print(f"    Redirect-Links: {redirects[:5]}")

if not redirects:
    # Show snippet for debugging
    print("    HTML-Snippet:", html[:1000])
    exit(1)

# Step 2: Follow first redirect
print(f"[2] Follow redirect: {redirects[0]}")
_, hoster_url = fetch(redirects[0])
print(f"    Hoster URL: {hoster_url}")

if not hoster_url or "aniworld" in hoster_url:
    print("FEHLER: Redirect wurde nicht zur Hoster-URL geleitet!")
    exit(1)

# Step 3: Extract stream from hoster
print(f"[3] Extrahiere Stream von Hoster...")
hhtml, _ = fetch(hoster_url)
if not hhtml:
    exit(1)

# Try HLS / m3u8
m = re.search(r"['\"]([^'\"]{20,}\.m3u8[^'\"]*)['\"]", hhtml)
if m:
    val = m.group(1)
    try:
        decoded = base64.b64decode(val + "==").decode()
        if decoded.startswith("http"):
            val = decoded
    except Exception:
        pass
    print(f"    [HLS] {val[:80]}")
else:
    m2 = re.search(r"['\"]([^'\"]{20,}\.mp4[^'\"]*)['\"]", hhtml)
    if m2:
        print(f"    [MP4] {m2.group(1)[:80]}")
    else:
        print("    Kein Stream gefunden. HTML-Snippet:", hhtml[:500])

print("\nTEST FERTIG!")
