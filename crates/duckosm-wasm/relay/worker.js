// The relay of duckOSM in the browser (docs/relay.md): a Cloudflare Worker that passes a Geofabrik extract through and adds the CORS
// header, so the page can download the region file itself. Geofabrik sends no CORS header, so a page cannot read its files directly.
//
// GET or HEAD /?url=https://download.geofabrik.de/<region>-latest.osm.pbf
// A GET can have a Range header (bytes=<from>-<to>): the answer is 206. Every answer has Content-Location: the dated file that
// "latest" is today (<region>-<yymmdd>.osm.pbf), which the page reads with Range, so that all the parts are of one version.
//
// It is not an open proxy: it accepts only Geofabrik's .osm.pbf files, and only from the pages in ALLOWED_ORIGINS (wrangler.toml) and
// from http://localhost or 127.0.0.1 (a development server). The body is streamed: the Worker uses no CPU time for it. Small and medium files stay in the edge cache of Cloudflare.

const GEOFABRIK = /^https:\/\/download\.geofabrik\.de\/[a-z0-9/_-]+-(latest|\d{6})\.osm\.pbf$/;
const LOCAL = /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;
const EDGE = { cacheEverything: true, cacheTtl: 604800 };
// Cloudflare keeps an object of up to 512 MB. Files up to 214 MB (the largest that was tested) have a first download as fast
// as with no cache; a file of 300 MB was 5 times slower (cause not known), so a file above this size is not kept.
const FILL_MAX = 220 << 20;
const PASS = ["Content-Type", "Content-Length", "Last-Modified", "ETag", "Content-Range", "Accept-Ranges"];

export default {
  async fetch(req, env, ctx) {
    const origin = req.headers.get("Origin") || "";
    const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
    if (!allowed.includes(origin) && !LOCAL.test(origin)) return new Response("This page may not use the relay.\n", { status: 403 });
    const cors = {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Expose-Headers": [...PASS, "Content-Location", "X-Relay-Cache"].join(", "),
      // the page is cross-origin isolated (COEP require-corp, for the WebAssembly threads)
      "Cross-Origin-Resource-Policy": "cross-origin",
      Vary: "Origin",
    };
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: { ...cors, "Access-Control-Allow-Methods": "GET, HEAD", "Access-Control-Allow-Headers": "Range" } });
    if (req.method !== "GET" && req.method !== "HEAD") return new Response("Only GET and HEAD.\n", { status: 405, headers: cors });
    const url = new URL(req.url).searchParams.get("url") || "";
    if (!GEOFABRIK.test(url)) return new Response("Only the .osm.pbf files of download.geofabrik.de.\n", { status: 400, headers: cors });
    const range = req.headers.get("Range") || "";
    if (range && !/^bytes=\d+-\d*$/.test(range)) return new Response("Only one range, bytes=<from>-<to>.\n", { status: 416, headers: cors });
    const send = { "User-Agent": "duckosm-relay (+https://github.com/Khoshkhah/duckOSM)" };
    if (range) send.Range = range;
    // The edge cache of Cloudflare keeps the files of up to FILL_MAX bytes (docs/relay.md, "The cache"). A file in the cache comes
    // from it (a Range answer too). A file that is not goes to Geofabrik at once, as with no cache, and the part that starts at
    // byte 0 (or a request with no Range) fills the cache in the background. CACHE = "off" in wrangler.toml turns the cache off.
    let r, how, age;
    if (env.CACHE !== "off" && req.method === "GET") {
      // the answer of Geofabrik comes first (as with no cache); its Content-Range or Content-Length gives the size of the file
      const direct = await fetch(url, { method: "GET", headers: send });
      const total = Number(/\/(\d+)$/.exec(direct.headers.get("Content-Range") || "")?.[1] || direct.headers.get("Content-Length")) || 0;
      if (direct.ok && total > 0 && total <= FILL_MAX) {
        const cached = fetch(url, { method: "GET", headers: send, cf: EDGE });
        // a file in the cache answers in some ms; a file that is not (Cloudflare reads all of it from Geofabrik first) does not
        const c = await Promise.race([cached, new Promise((ok) => setTimeout(ok, 300))]);
        // an entry that is younger than its own fill time (1 s and the size at 2 MB/s) can still be in its fill: a request that
        // joins it runs at the speed of the one read of Geofabrik. So only an older entry is used.
        const old = c && Number(c.headers.get("age")) >= 1 + total / (2 << 20);
        if (c && c.headers.get("cf-cache-status") === "HIT" && old) {
          direct.body?.cancel();
          r = c;
          how = "HIT";
          age = c.headers.get("age") || "none";
        } else {
          r = direct;
          how = "MISS-direct";
          // the part that starts at byte 0 (or a request with no Range) reads the cached request to its end: the file is then
          // in the cache. The other parts stop theirs (Cloudflare joins the requests for one file: one read of Geofabrik).
          const fills = !range || /^bytes=0-/.test(range);
          ctx?.waitUntil(cached.then((x) => (fills ? x.body?.pipeTo(new WritableStream()) : x.body?.cancel()), () => {}));
        }
      } else r = direct;
    } else r = await fetch(url, { method: req.method, headers: send });
    const headers = new Headers(cors);
    headers.set("Content-Location", r.url);
    if (how) headers.set("X-Relay-Cache", how + (age ? " age=" + age : ""));
    for (const k of PASS) if (r.headers.has(k)) headers.set(k, r.headers.get(k));
    return new Response(r.body, { status: r.status, headers });
  },
};
