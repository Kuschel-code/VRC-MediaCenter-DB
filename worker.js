/**
 * VRC Media Center - Cloudflare Worker v2
 *
 * Library/Episodes : GitHub static txt files (gecacht)
 * Streams          : Live-Scraping on-demand, gecacht fuer 5 Minuten
 *                    Kein eigener Server noetig!
 */

const GITHUB_RAW       = "https://raw.githubusercontent.com/Kuschel-code/VRC-MediaCenter-DB/master";
const ANIWORLD_BASE    = "https://aniworld.to";
const STO_BASE         = "https://s.to";
const STREAMKISTE_BASE = "https://streamkiste.tv";

const CACHE_STREAMS  = 300;   // 5 Minuten (Stream-URLs laufen ~10min ab)
const CACHE_LIBRARY  = 3600;  // 1 Stunde
const CACHE_EPISODES = 1800;  // 30 Minuten

const PREFERRED_HOSTERS = ["VOE", "VIDMOLY", "FILEMOON", "STREAMTAPE"];
const LANG_NUM = { "ger-dub": "1", "ger-sub": "2", "eng-sub": "3", "eng-dub": "4" };

const FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://aniworld.to/",
};

const CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
};

addEventListener("fetch", event => {
    event.respondWith(handleRequest(event.request, event));
});

async function handleRequest(request, event) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
        return new Response(null, { headers: CORS });
    }

    const path = url.pathname;

    if (path === "/health" || path === "/") {
        return new Response("VRC Media Center Worker v2 OK", {
            headers: { "Content-Type": "text/plain", ...CORS },
        });
    }

    // /stream?id=<episodeId>
    if (path === "/stream") {
        const episodeId = url.searchParams.get("id");
        if (!episodeId) {
            return new Response("Missing parameter: id", { status: 400, headers: CORS });
        }

        // Cloudflare Cache pruefen
        const cacheKey = new Request(
            "https://vrc-stream-cache.internal/" + encodeURIComponent(episodeId),
            { method: "GET" }
        );
        const cached = await caches.default.match(cacheKey);
        if (cached) {
            const streamUrl = await cached.text();
            if (streamUrl && streamUrl.startsWith("http")) {
                return Response.redirect(streamUrl, 302);
            }
        }

        // Live scrapen
        const streamUrl = await scrapeStreamUrl(episodeId);
        if (!streamUrl) {
            return new Response("Kein Stream gefunden fuer: " + episodeId, {
                status: 404,
                headers: { "Content-Type": "text/plain", ...CORS },
            });
        }

        // Ergebnis cachen (5 Minuten)
        event.waitUntil(caches.default.put(
            cacheKey,
            new Response(streamUrl, {
                headers: { "Cache-Control": "public, max-age=" + CACHE_STREAMS },
            })
        ));

        return Response.redirect(streamUrl, 302);
    }

    // /library?type=anime|movies|series
    if (path === "/library") {
        const type = url.searchParams.get("type") || "anime";
        const fileMap = { anime: "anime.txt", movies: "movies.txt", series: "series.txt" };
        const file = fileMap[type] || "anime.txt";
        const content = await fetchCached(GITHUB_RAW + "/" + file, CACHE_LIBRARY, event);
        if (!content) return new Response("", { status: 503, headers: CORS });
        const filtered = content.split("\n")
            .filter(l => l.trim() && !l.trim().startsWith("#")).join("\n");
        return new Response(filtered, {
            headers: { "Content-Type": "text/plain; charset=utf-8", ...CORS },
        });
    }

    // /episodes?id=<contentId>
    if (path === "/episodes") {
        const contentId = url.searchParams.get("id");
        if (!contentId) {
            return new Response("Missing parameter: id", { status: 400, headers: CORS });
        }
        const content = await fetchCached(GITHUB_RAW + "/episodes.txt", CACHE_EPISODES, event);
        if (!content) return new Response("", { status: 503, headers: CORS });
        const lines = content.split("\n").filter(l => {
            const t = l.trim();
            return t && !t.startsWith("#") && t.startsWith(contentId + "|");
        });
        const result = lines.map(l => l.split("|").slice(1).join("|")).join("\n");
        return new Response(result, {
            headers: { "Content-Type": "text/plain; charset=utf-8", ...CORS },
        });
    }

    return new Response("Not Found", { status: 404, headers: CORS });
}

// ===========================================================================
// Stream Scraping
// ===========================================================================

async function scrapeStreamUrl(episodeId) {
    // Anime-Format: <slug>-s<season>-ep<episode>[-<lang>]
    const animeMatch = episodeId.match(/^(.+)-s(\d+)-ep(\d+)(?:-(ger-dub|ger-sub|eng-sub|eng-dub))?$/);
    if (animeMatch) {
        return await scrapeAnimeStream(
            animeMatch[1], animeMatch[2], animeMatch[3], animeMatch[4] || "ger-dub"
        );
    }
    // Film-Format: einfacher Slug (Streamkiste)
    return await scrapeMovieStream(episodeId);
}

async function scrapeAnimeStream(slug, season, episode, lang) {
    const langNum = LANG_NUM[lang] || "1";
    const paths = [
        `/anime/stream/${slug}/staffel-${season}/episode-${episode}`,
        `/serien/stream/${slug}/staffel-${season}/episode-${episode}`,
    ];
    for (const base of [ANIWORLD_BASE, STO_BASE]) {
        for (const path of paths) {
            const epUrl = `${base}${path}?lang=${langNum}`;
            const html = await fetchPage(epUrl);
            if (!html) continue;
            if (!html.includes("data-link-target") && !html.includes("hosterSiteVideo")) continue;
            const streamUrl = await extractStreamFromPage(html, base);
            if (streamUrl) return streamUrl;
        }
    }
    return null;
}

async function scrapeMovieStream(slug) {
    const html = await fetchPage(`${STREAMKISTE_BASE}/stream/${slug}/`);
    if (!html) return null;
    return await extractStreamFromPage(html, STREAMKISTE_BASE);
}

async function extractStreamFromPage(html, baseUrl) {
    const hosterLinks = findHosterLinks(html);
    if (!hosterLinks.length) return null;

    // Nach Praeferenz sortieren
    hosterLinks.sort((a, b) => {
        const ai = PREFERRED_HOSTERS.findIndex(p => a.name.toUpperCase().includes(p));
        const bi = PREFERRED_HOSTERS.findIndex(p => b.name.toUpperCase().includes(p));
        return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });

    for (const hoster of hosterLinks) {
        let redirectUrl = hoster.redirect_url;
        if (!redirectUrl.startsWith("http")) redirectUrl = baseUrl + redirectUrl;
        const rHtml = await fetchPage(redirectUrl);
        if (!rHtml) continue;
        const streamUrl = extractStream(hoster.name, rHtml);
        if (streamUrl) return streamUrl;
    }
    return null;
}

async function fetchPage(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
        const resp = await fetch(url, {
            headers: FETCH_HEADERS,
            redirect: "follow",
            signal: controller.signal,
        });
        if (!resp.ok) return null;
        return await resp.text();
    } catch {
        return null;
    } finally {
        clearTimeout(timer);
    }
}

// --- Hoster-Links aus HTML extrahieren --------------------------------------

function findHosterLinks(html) {
    const links = [];
    const seen = new Set();

    // AniWorld: data-link-target mit Hoster-Name in h4
    const regex = /data-link-target="([^"]+)"([\s\S]{1,600}?)(?=data-link-target=|<\/ul>|<\/div>\s*<\/div>)/g;
    let m;
    while ((m = regex.exec(html)) !== null) {
        const redirectUrl = m[1];
        if (!redirectUrl || seen.has(redirectUrl)) continue;

        let name = "";
        const h4m = m[2].match(/<h4[^>]*>([\s\S]*?)<\/h4>/);
        if (h4m) {
            name = h4m[1].replace(/<[^>]+>/g, "").trim();
        } else {
            const im = m[2].match(/title="([^"]+)"/);
            if (im) name = im[1].trim();
        }

        if (name && redirectUrl) {
            seen.add(redirectUrl);
            links.push({ name, redirect_url: redirectUrl });
        }
    }

    // Fallback fuer andere Layouts
    if (!links.length) {
        const fbRegex = /<a[^>]+href="([^"]*(?:redirect|hoster)[^"]*)"[^>]*>[\s\S]{0,300}?<h4[^>]*>([\s\S]*?)<\/h4>/g;
        while ((m = fbRegex.exec(html)) !== null) {
            const redirectUrl = m[1];
            const name = m[2].replace(/<[^>]+>/g, "").trim();
            if (redirectUrl && name && !seen.has(redirectUrl)) {
                seen.add(redirectUrl);
                links.push({ name, redirect_url: redirectUrl });
            }
        }
    }

    return links;
}

// --- Hoster-spezifische Extraktoren -----------------------------------------

function extractStream(hosterName, html) {
    const name = hosterName.toUpperCase();
    if (name.includes("VOE"))        return extractVOE(html);
    if (name.includes("VIDOZA"))     return extractVidoza(html);
    if (name.includes("VIDMOLY") || name.includes("FILEMOON")) return extractVidmoly(html);
    if (name.includes("STREAMTAPE")) return extractStreamtape(html);
    return extractGeneric(html);
}

function extractVOE(html) {
    // HLS URL (direkt oder Base64)
    let m = html.match(/'hls'\s*:\s*'([^']+)'/);
    if (!m) m = html.match(/"hls"\s*:\s*"([^"]+)"/);
    if (m) {
        try { const d = atob(m[1]); if (d.startsWith("http")) return d; } catch {}
        if (m[1].startsWith("http")) return m[1];
    }
    // MP4
    m = html.match(/'mp4'\s*:\s*'([^']+)'/);
    if (!m) m = html.match(/"mp4"\s*:\s*"([^"]+)"/);
    if (m && m[1].startsWith("http")) return m[1];
    // VOE neue Obfuscation (ROT13 + Base64 + Char-Shift + Reverse + Base64)
    for (const p of [/var\s+\w+\s*=\s*'([A-Za-z0-9+\/=]{50,})'/, /let\s+\w+\s*=\s*"([A-Za-z0-9+\/=]{50,})"/]) {
        m = html.match(p);
        if (m) { const d = decodeVOEObfuscated(m[1]); if (d) return d; }
    }
    m = html.match(/source\s*=\s*['"]([^'"]+\.m3u8[^'"]*)["']/);
    if (m) return m[1];
    m = html.match(/(https?:\/\/[^\s"'<>]+\.(?:mp4|m3u8)\?[^\s"'<>]*)/);
    if (m) return m[1];
    return null;
}

function decodeVOEObfuscated(encoded) {
    try {
        const s1 = rot13(encoded);
        const s2 = atob(s1);
        let s3 = "";
        for (const c of s2) s3 += String.fromCharCode(c.charCodeAt(0) - 3);
        const s4 = s3.split("").reverse().join("");
        const r = atob(s4);
        if (r.startsWith("http")) return r;
    } catch {}
    try { const r = atob(encoded); if (r.startsWith("http")) return r; } catch {}
    return null;
}

function rot13(str) {
    return str.replace(/[a-zA-Z]/g, c => {
        const b = c <= "Z" ? 65 : 97;
        return String.fromCharCode(((c.charCodeAt(0) - b + 13) % 26) + b);
    });
}

function extractVidmoly(html) {
    for (const p of [
        /file:\s*"(https?:\/\/[^"]+)"/,
        /file:\s*'(https?:\/\/[^']+)'/,
        /(https?:\/\/[^\s"']+\.m3u8[^\s"']*)/,
    ]) {
        const m = html.match(p);
        if (m && m[1].startsWith("http")) return m[1];
    }
    return null;
}

function extractVidoza(html) {
    let m = html.match(/src:\s*"(https?:\/\/[^"]+)"/);
    if (m) return m[1];
    m = html.match(/<source\s+src="(https?:\/\/[^"]+)"/);
    if (m) return m[1];
    return null;
}

function extractStreamtape(html) {
    const m = html.match(/document\.getElementById\('robotlink'\)\.innerHTML\s*=\s*'([^']*)'/);
    if (m) {
        const rest = html.slice(html.indexOf(m[0]) + m[0].length);
        const m2 = rest.match(/\+\s*\('([^']+)'\)/);
        if (m2) return "https:" + m[1] + m2[1];
    }
    return null;
}

function extractGeneric(html) {
    let m = html.match(/<source\s+src="(https?:\/\/[^"]+)"/);
    if (m) return m[1];
    m = html.match(/file:\s*"(https?:\/\/[^"]+\.(?:mp4|m3u8)[^"]*)"/);
    if (m) return m[1];
    m = html.match(/(https?:\/\/[^\s"'<>]+\.(?:mp4|m3u8)\?[^\s"'<>]+)/);
    if (m) return m[1];
    return null;
}

// --- Cached Fetch fuer Library/Episodes -------------------------------------

async function fetchCached(url, ttl, event) {
    const cacheKey = new Request(url, { method: "GET" });
    const cache = caches.default;
    let response = await cache.match(cacheKey);
    if (response) return await response.text();
    try {
        const resp = await fetch(url);
        if (!resp.ok) return null;
        const text = await resp.text();
        const toCache = new Response(text, {
            headers: {
                "Content-Type": "text/plain",
                "Cache-Control": "public, max-age=" + ttl,
            },
        });
        event.waitUntil(cache.put(cacheKey, toCache));
        return text;
    } catch {
        return null;
    }
}
