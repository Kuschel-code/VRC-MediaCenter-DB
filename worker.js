/**
 * VRC Media Center - Cloudflare Worker (Service Worker Format)
 * Reads streams.txt from GitHub and redirects to the current HLS stream URL.
 */

const GITHUB_RAW = "https://raw.githubusercontent.com/Kuschel-code/VRC-MediaCenter-DB/master";
const CACHE_STREAMS = 60;
const CACHE_LIBRARY = 3600;
const CACHE_EPISODES = 1800;
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

    // /health or /
    if (path === "/health" || path === "/") {
        return new Response("VRC Media Center Worker OK", {
            headers: { "Content-Type": "text/plain", ...CORS },
        });
    }

    // /stream?id=<episodeId> — Live-Aufloesung: Holt die aktuelle Stream-URL
    if (path === "/stream") {
        const episodeId = url.searchParams.get("id");
        if (!episodeId) {
            return new Response("Missing parameter: id", { status: 400, headers: CORS });
        }
        const streamsText = await fetchCached(GITHUB_RAW + "/streams.txt", CACHE_STREAMS, event);
        if (!streamsText) {
            return new Response("streams.txt not reachable", { status: 503, headers: CORS });
        }
        let embedUrl = findInLines(streamsText, episodeId);
        if (!embedUrl) {
            // Film-Fallback: Versuche mit -s1-ep1 Suffix
            embedUrl = findInLines(streamsText, episodeId + "-s1-ep1");
        }
        if (!embedUrl) {
            return new Response("No URL for: " + episodeId, {
                status: 404,
                headers: { "Content-Type": "text/plain", ...CORS },
            });
        }

        // Wenn die URL bereits ein direktes MP4/m3u8 ist (z.B. vidnest), direkt weiterleiten
        if (embedUrl.includes(".mp4") || embedUrl.includes(".m3u8")) {
            return Response.redirect(embedUrl, 302);
        }

        // Live-Aufloesung: Hoster-Embed-Seite abrufen -> Stream extrahieren
        try {
            const streamUrl = await resolveStreamUrl(embedUrl);
            if (streamUrl) {
                return Response.redirect(streamUrl, 302);
            }
            return new Response("Could not resolve stream from: " + embedUrl, {
                status: 502, headers: CORS
            });
        } catch (e) {
            return new Response("Stream resolution error: " + e.message, {
                status: 502, headers: CORS
            });
        }
    }

    // /library?type=anime|movies|series&letter=A&q=query
    if (path === "/library") {
        const type = url.searchParams.get("type") || "anime";
        const letter = url.searchParams.get("letter")?.toUpperCase();
        const query = url.searchParams.get("q")?.toLowerCase();

        const fileMap = { anime: "anime.txt", movies: "movies.txt", series: "series.txt" };
        let file = fileMap[type] || "anime.txt";

        // Wenn ein Buchstabe angegeben ist, nutze den entsprechenden Chunk
        if (letter) {
            file = `${type}_${letter}.txt`;
        }

        const content = await fetchCached(GITHUB_RAW + "/" + file, CACHE_LIBRARY, event);
        if (!content) return new Response("", { status: 503, headers: CORS });

        let lines = content.split("\n")
            .filter(l => l.trim() && !l.trim().startsWith("#"));

        // Server-seitiger Suchfilter (sehr wichtig wegen 100KB Limit!)
        if (query) {
            lines = lines.filter(l => l.toLowerCase().includes(query));
        }

        return new Response(lines.join("\n"), {
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

    // /proxy?url=<encoded_url> — Cloudflare-Edge-Proxy zum Umgehen von ISP-Sperren (CUII)
    if (path === "/proxy") {
        const targetUrl = url.searchParams.get("url");
        if (!targetUrl) {
            return new Response("Missing parameter: url", { status: 400, headers: CORS });
        }

        // Whitelist: Nur erlaubte Domains (kein offener Proxy!)
        const allowed = ["serienstream.to", "s.to", "bs.to", "serien.sx", "aniworld.to", "filmpalast.to",
            "voe.sx", "vidoza.net", "streamtape.com", "doodstream.com", "filemoon.sx"];
        let targetHost;
        try {
            targetHost = new URL(targetUrl).hostname;
        } catch {
            return new Response("Invalid URL", { status: 400, headers: CORS });
        }
        if (!allowed.some(d => targetHost === d || targetHost.endsWith("." + d))) {
            return new Response("Domain not allowed: " + targetHost, { status: 403, headers: CORS });
        }

        const noRedirect = url.searchParams.get("noredirect") === "1";

        try {
            const resp = await fetch(targetUrl, {
                headers: {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
                },
                redirect: noRedirect ? "manual" : "follow",
            });

            if (noRedirect && (resp.status >= 300 && resp.status < 400)) {
                const location = resp.headers.get("Location") || "";
                return new Response(JSON.stringify({ redirect: location, status: resp.status }), {
                    status: 200,
                    headers: { "Content-Type": "application/json", ...CORS },
                });
            }

            const body = await resp.text();
            return new Response(body, {
                status: resp.status,
                headers: {
                    "Content-Type": resp.headers.get("Content-Type") || "text/html; charset=utf-8",
                    "X-Final-Url": resp.url || targetUrl,
                    ...CORS,
                },
            });
        } catch (e) {
            return new Response("Proxy fetch error: " + e.message, { status: 502, headers: CORS });
        }
    }

    // /youtube?url=<youtubeUrl> or /youtube?v=<videoId> — YouTube URL resolver for AVPro
    if (path === "/youtube") {
        const videoUrl = url.searchParams.get("url");
        const videoIdParam = url.searchParams.get("v");

        let videoId = videoIdParam;
        if (!videoId && videoUrl) {
            videoId = extractYouTubeId(videoUrl);
        }
        if (!videoId) {
            return new Response("Missing parameter: url or v. Example: /youtube?v=dQw4w9WgXcQ", {
                status: 400, headers: { "Content-Type": "text/plain", ...CORS },
            });
        }

        const result = await resolveYouTubeStream(videoId);
        if (result) {
            return Response.redirect(result, 302);
        }

        return new Response("Could not resolve YouTube video: " + videoId, {
            status: 502, headers: { "Content-Type": "text/plain", ...CORS },
        });
    }

    return new Response("Not Found", { status: 404, headers: CORS });
}

async function fetchCached(url, ttl, event) {
    // Version-basierter Cache-Key: Aendert sich bei jedem Deploy
    const CACHE_VERSION = "v9";
    const cacheKey = new Request(url + "?_cv=" + CACHE_VERSION, { method: "GET" });
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
    } catch (e) {
        return null;
    }
}

function findInLines(text, key) {
    for (const line of text.split("\n")) {
        const t = line.trim();
        if (!t || t.startsWith("#")) continue;
        const idx = t.indexOf("|");
        if (idx === -1) continue;
        if (t.substring(0, idx).trim() === key) {
            return t.substring(idx + 1).trim();
        }
    }
    return null;
}

/**
 * Loest eine gespeicherte URL zu einer frischen Stream-URL auf.
 * Unterstuetzt:
 * - Filmpalast-Seiten-URLs (z.B. filmpalast.to/stream/xxx) -> parst Hoster-Links -> VOE-Kette
 * - VOE/Hoster-Embed-URLs -> JS Fallback -> Delivery -> HLS/MP4
 */
async function resolveStreamUrl(storedUrl) {
    const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36";
    const HEADERS = { "User-Agent": UA, "Accept": "text/html,*/*" };

    let embedUrl = storedUrl;

    // Step 1: Wenn Filmpalast-URL -> Seite laden und Hoster-Link extrahieren
    if (storedUrl.includes("filmpalast.to")) {
        try {
            const resp = await fetch(storedUrl, { headers: HEADERS, redirect: "follow" });
            const pageHtml = await resp.text();

            const hosterPatterns = [
                /data-player-url="(https?:\/\/voe\.sx\/[^"]+)"/,
                /href="(https?:\/\/voe\.sx\/[^"]+)"/,
                /data-player-url="(https?:\/\/veev\.to\/[^"]+)"/,
                /href="(https?:\/\/veev\.to\/[^"]+)"/,
                /data-player-url="(https?:\/\/vidsonic\.net\/[^"]+)"/,
                /href="(https?:\/\/vidsonic\.net\/[^"]+)"/,
            ];

            for (const pattern of hosterPatterns) {
                const match = pageHtml.match(pattern);
                if (match) {
                    embedUrl = match[1];
                    break;
                }
            }

            if (embedUrl === storedUrl) return null;
        } catch (e) {
            return null;
        }
    }

    // Step 2: Embed-Seite abrufen (z.B. voe.sx/xxx)
    let html = "";
    try {
        const resp = await fetch(embedUrl, { headers: HEADERS, redirect: "follow" });
        html = await resp.text();
    } catch (e) {
        return null;
    }

    // Step 3: VOE JS-Fallback: window.location.href = 'https://delivery.com/xxx'
    if (html.length < 2000) {
        const fallback = html.match(/window\.location\.href\s*=\s*'(https?:\/\/[^']+)'/);
        if (fallback) {
            try {
                const resp2 = await fetch(fallback[1], { headers: HEADERS, redirect: "follow" });
                html = await resp2.text();
            } catch (e) {
                return null;
            }
        }
    }

    // Step 4: HLS m3u8 URL extrahieren
    const hlsMatch = html.match(/https?:\/\/[^"'\s]+\.m3u8[^"'\s]*/);
    if (hlsMatch) return hlsMatch[0];

    // Step 5: MP4 URL extrahieren
    const mp4Match = html.match(/https?:\/\/[^"'\s]+\.mp4[^"'\s]*/);
    if (mp4Match) return mp4Match[0];

    // Step 6: sources Array (VOE)
    const sourcesMatch = html.match(/sources\s*:\s*\[\s*\{\s*src\s*:\s*["']([^"']+)/);
    if (sourcesMatch) return sourcesMatch[1];

    // Step 7: Packed JS (Vidmoly / Filemoon)
    const packedMatch = html.match(/eval\(function\(p,a,c,k,e,[dr]\)\{.*?\}\('.+?',\d+,\d+,'[^']+'\.split\('\|'\)/);
    if (packedMatch) {
        try {
            const packed = packedMatch[0];
            const pm = packed.match(/eval\(function\(p,a,c,k,e,[dr]\)\{.*?\}\('(.+?)',(\d+),(\d+),'([^']+)'\.split\('\|'\)/);
            if (pm) {
                let payload = pm[1];
                const a = parseInt(pm[2]);
                const c = parseInt(pm[3]);
                const keywords = pm[4].split("|");

                function baseN(num, base) {
                    const chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
                    if (num < base) return chars[num];
                    return baseN(Math.floor(num / base), base) + chars[num % base];
                }

                for (let i = c - 1; i >= 0; i--) {
                    if (i < keywords.length && keywords[i]) {
                        const word = baseN(i, a);
                        const reg = new RegExp("\\b" + word + "\\b", "g");
                        payload = payload.replace(reg, keywords[i]);
                    }
                }

                const unpackedHls = payload.match(/https?:\/\/[^"'\s]+\.m3u8[^"'\s]*/);
                if (unpackedHls) return unpackedHls[0];

                const unpackedMp4 = payload.match(/https?:\/\/[^"'\s]+\.mp4[^"'\s]*/);
                if (unpackedMp4) return unpackedMp4[0];
            }
        } catch (e) {
            // Unpacking failed
        }
    }

    return null;
}

/**
 * Extract a YouTube video ID from various URL formats.
 */
function extractYouTubeId(urlStr) {
    if (!urlStr) return null;
    // Already just an ID (11 chars, alphanumeric + dash/underscore)
    if (/^[A-Za-z0-9_-]{11}$/.test(urlStr)) return urlStr;
    try {
        const u = new URL(urlStr);
        // youtube.com/watch?v=xxx
        if (u.searchParams.has("v")) return u.searchParams.get("v");
        // youtu.be/xxx
        if (u.hostname === "youtu.be") return u.pathname.slice(1).split("/")[0];
        // youtube.com/embed/xxx or youtube.com/v/xxx or youtube.com/shorts/xxx
        const embedMatch = u.pathname.match(/\/(embed|v|shorts)\/([A-Za-z0-9_-]{11})/);
        if (embedMatch) return embedMatch[2];
    } catch {
        // Not a valid URL, try regex fallback
        const m = urlStr.match(/(?:v=|youtu\.be\/|\/(?:embed|v|shorts)\/)([A-Za-z0-9_-]{11})/);
        if (m) return m[1];
    }
    return null;
}

/**
 * Resolve a YouTube video ID to a direct progressive (audio+video) MP4 stream URL
 * using public Invidious and Piped API instances.
 * AVPro needs a single URL with both audio and video (not DASH separate streams).
 * Prefers 720p, falls back to 360p or any available progressive stream.
 */
async function resolveYouTubeStream(videoId) {
    // Invidious instances — formatStreams contains progressive (combined) streams
    const invidiousInstances = [
        "https://vid.puffyan.us",
        "https://y.com.sb",
        "https://invidious.kavin.rocks",
        "https://inv.nadeko.net",
        "https://invidious.nerdvpn.de",
    ];

    // Piped instances — streams where videoOnly === false are combined
    const pipedInstances = [
        "https://pipedapi.kavin.rocks",
        "https://piped-api.privacy.com.de",
        "https://pipedapi.adminforge.de",
    ];

    // Try Invidious first (most reliable for progressive streams)
    for (const instance of invidiousInstances) {
        try {
            const resp = await fetch(`${instance}/api/v1/videos/${videoId}`, {
                headers: { "Accept": "application/json" },
                signal: AbortSignal.timeout(5000),
            });
            if (!resp.ok) continue;
            const data = await resp.json();

            // formatStreams = progressive streams (audio+video combined)
            const streams = data.formatStreams;
            if (!streams || streams.length === 0) continue;

            // Prefer 720p MP4, then 360p, then any
            let best = streams.find(s => s.qualityLabel === "720p" && s.type && s.type.includes("video/mp4"));
            if (!best) best = streams.find(s => s.qualityLabel === "720p");
            if (!best) best = streams.find(s => s.qualityLabel === "360p" && s.type && s.type.includes("video/mp4"));
            if (!best) best = streams.find(s => s.qualityLabel === "360p");
            if (!best) best = streams.find(s => s.type && s.type.includes("video/mp4"));
            if (!best) best = streams[0];

            if (best && best.url) return best.url;
        } catch {
            continue;
        }
    }

    // Fallback: Try Piped instances
    for (const instance of pipedInstances) {
        try {
            const resp = await fetch(`${instance}/streams/${videoId}`, {
                headers: { "Accept": "application/json" },
                signal: AbortSignal.timeout(5000),
            });
            if (!resp.ok) continue;
            const data = await resp.json();

            // videoStreams where videoOnly === false are progressive (combined audio+video)
            const combined = (data.videoStreams || []).filter(s => s.videoOnly === false);
            if (combined.length === 0) continue;

            // Prefer 720p MP4, then 360p, then any
            let best = combined.find(s => s.quality === "720p" && s.mimeType && s.mimeType.includes("video/mp4"));
            if (!best) best = combined.find(s => s.quality === "720p");
            if (!best) best = combined.find(s => s.quality === "360p" && s.mimeType && s.mimeType.includes("video/mp4"));
            if (!best) best = combined.find(s => s.quality === "360p");
            if (!best) best = combined.find(s => s.mimeType && s.mimeType.includes("video/mp4"));
            if (!best) best = combined[0];

            if (best && best.url) return best.url;
        } catch {
            continue;
        }
    }

    return null;
}
