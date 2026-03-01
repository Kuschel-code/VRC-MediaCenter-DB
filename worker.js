/**
 * VRC Media Center - Cloudflare Worker
 * ======================================
 * Liest streams.txt von GitHub und redirected zu der aktuellen Stream-URL.
 *
 * Routen:
 *   /stream?id=attack-on-titan-s1-ep1-ger-dub   → 302 Redirect zur HLS-URL
 *   /library?type=anime                           → anime.txt Inhalt
 *   /library?type=movies                          → movies.txt Inhalt
 *   /episodes?id=attack-on-titan                  → Eintraege gefiltert aus episodes.txt
 *   /health                                       → OK
 *
 * Deployment:
 *   npx wrangler deploy
 */

const GITHUB_RAW = "https://raw.githubusercontent.com/Kuschel-code/VRC-MediaCenter-DB/master";

// Cache-Zeiten (Cloudflare KV-Cache über fetch cache)
const CACHE_STREAMS = 300;   // 5 Minuten (streams.txt aendert sich taeglich)
const CACHE_LIBRARY = 3600;  // 1 Stunde
const CACHE_EPISODES = 1800;  // 30 Minuten

// CORS-Header fuer VRChat (falls noetig)
const CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
};

export default {
    async fetch(request, env, ctx) {
        const url = new URL(request.url);

        // OPTIONS preflight
        if (request.method === "OPTIONS") {
            return new Response(null, { headers: CORS });
        }

        const path = url.pathname;

        // ── /health ─────────────────────────────────────────────
        if (path === "/health" || path === "/") {
            return new Response("VRC Media Center Worker OK", {
                headers: { "Content-Type": "text/plain", ...CORS },
            });
        }

        // ── /stream?id=<episodeId> ───────────────────────────────
        if (path === "/stream") {
            const episodeId = url.searchParams.get("id");
            if (!episodeId) {
                return new Response("Fehlender Parameter: id", { status: 400, headers: CORS });
            }

            // streams.txt laden (mit Cache)
            const streamsText = await fetchCached(GITHUB_RAW + "/streams.txt", CACHE_STREAMS, ctx);
            if (!streamsText) {
                return new Response("streams.txt nicht erreichbar", { status: 503, headers: CORS });
            }

            // Zeile finden: episodeId|streamUrl
            const streamUrl = findInLines(streamsText, episodeId);
            if (!streamUrl) {
                return new Response(`Keine URL fuer: ${episodeId}`, {
                    status: 404,
                    headers: { "Content-Type": "text/plain", ...CORS },
                });
            }

            // HTTP 302 Redirect → AVPro Player folgt automatisch
            return Response.redirect(streamUrl, 302);
        }

        // ── /library?type=anime|movies|series ───────────────────
        if (path === "/library") {
            const type = url.searchParams.get("type") || "anime";
            const fileMap = {
                anime: "anime.txt",
                movies: "movies.txt",
                series: "series.txt",
            };
            const file = fileMap[type] || "anime.txt";

            const content = await fetchCached(GITHUB_RAW + "/" + file, CACHE_LIBRARY, ctx);
            if (!content) {
                return new Response("", { status: 503, headers: CORS });
            }

            // Kommentarzeilen filtern
            const filtered = content
                .split("\n")
                .filter((l) => l.trim() && !l.trim().startsWith("#"))
                .join("\n");

            return new Response(filtered, {
                headers: { "Content-Type": "text/plain; charset=utf-8", ...CORS },
            });
        }

        // ── /episodes?id=<contentId> ────────────────────────────
        if (path === "/episodes") {
            const contentId = url.searchParams.get("id");
            if (!contentId) {
                return new Response("Fehlender Parameter: id", { status: 400, headers: CORS });
            }

            const content = await fetchCached(GITHUB_RAW + "/episodes.txt", CACHE_EPISODES, ctx);
            if (!content) {
                return new Response("", { status: 503, headers: CORS });
            }

            // Nur Zeilen fuer diesen contentId zurueckgeben
            // Format: contentId|EpisodeTitel|EpisodenID → EpisodeTitel|EpisodenID
            const lines = content.split("\n").filter((l) => {
                const t = l.trim();
                return t && !t.startsWith("#") && t.startsWith(contentId + "|");
            });

            const result = lines
                .map((l) => {
                    const parts = l.split("|");
                    return parts.slice(1).join("|"); // Entferne contentId-Praefix
                })
                .join("\n");

            return new Response(result, {
                headers: { "Content-Type": "text/plain; charset=utf-8", ...CORS },
            });
        }

        return new Response("Not Found", { status: 404, headers: CORS });
    },
};

// ── Helper: GitHub-Datei mit Cloudflare-Cache laden ─────────
async function fetchCached(url, ttl, ctx) {
    const cacheKey = new Request(url, { method: "GET" });
    const cache = caches.default;

    // Cache-Hit?
    let response = await cache.match(cacheKey);
    if (response) {
        return await response.text();
    }

    // Cache-Miss: von GitHub laden
    try {
        const resp = await fetch(url, {
            cf: { cacheTtl: ttl, cacheEverything: true },
        });
        if (!resp.ok) return null;
        const text = await resp.text();

        // In Cloudflare-Cache speichern
        const toCache = new Response(text, {
            headers: {
                "Content-Type": "text/plain",
                "Cache-Control": `public, max-age=${ttl}`,
            },
        });
        ctx.waitUntil(cache.put(cacheKey, toCache));
        return text;
    } catch (e) {
        return null;
    }
}

// ── Helper: Zeile mit Key|Value-Format durchsuchen ──────────
function findInLines(text, key) {
    for (const line of text.split("\n")) {
        const t = line.trim();
        if (!t || t.startsWith("#")) continue;
        const idx = t.indexOf("|");
        if (idx === -1) continue;
        const lineKey = t.substring(0, idx).trim();
        if (lineKey === key) {
            return t.substring(idx + 1).trim();
        }
    }
    return null;
}
