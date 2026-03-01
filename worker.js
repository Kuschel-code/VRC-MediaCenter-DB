/**
 * VRC Media Center - Cloudflare Worker (Service Worker Format)
 * Reads streams.txt from GitHub and redirects to the current HLS stream URL.
 */

const GITHUB_RAW = "https://raw.githubusercontent.com/Kuschel-code/VRC-MediaCenter-DB/master";
const CACHE_STREAMS = 300;
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

    // /stream?id=<episodeId>
    if (path === "/stream") {
        const episodeId = url.searchParams.get("id");
        if (!episodeId) {
            return new Response("Missing parameter: id", { status: 400, headers: CORS });
        }
        const streamsText = await fetchCached(GITHUB_RAW + "/streams.txt", CACHE_STREAMS, event);
        if (!streamsText) {
            return new Response("streams.txt not reachable", { status: 503, headers: CORS });
        }
        const streamUrl = findInLines(streamsText, episodeId);
        if (!streamUrl) {
            return new Response("No URL for: " + episodeId, {
                status: 404,
                headers: { "Content-Type": "text/plain", ...CORS },
            });
        }
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
