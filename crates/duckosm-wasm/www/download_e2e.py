"""The download of a region file through the relay (app/regions.js) in headless Chromium (Playwright), with no network: the relay is a
route of the browser that answers HEAD and Range requests from 96 MB of random bytes (parts of 12 MB: a part is split when it has 8 MB left), as the relay does (the dated file in
Content-Location, an ETag). The first request of one part waits 4 s: the page sends that request again after 2 s, and a part that is
done takes the second half of the part with the most bytes left. The stored file must equal the bytes (SHA-256).

    python crates/duckosm-wasm/www/download_e2e.py
"""
import hashlib
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from serve import WWW, start

RELAY = "https://relay.test/"
DATA = os.urandom(96 << 20)
SMALL = os.urandom(16 << 20)     # the cached file: one answer of 96 MB closes the headless page
SMALL_SHA = hashlib.sha256(SMALL).hexdigest()
SHA = hashlib.sha256(DATA).hexdigest()
server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/index.html?relay={RELAY}"
ranges = []
HIT = {"on": False}     # the relay has the file in its cache: the page takes it in one connection


def relay(route):
    req = route.request
    target = parse_qs(urlparse(req.url).query)["url"][0]
    DATA_ = SMALL if "/small-" in target else DATA
    if not re.search(r"-\d{6}\.osm\.pbf$", target):        # only the dated files are there, as on Geofabrik after a day
        return route.fulfill(status=404, body="not there")
    head = {"Content-Length": str(len(DATA_)), "Accept-Ranges": "bytes", "Content-Location": target, "ETag": '"e1"',
            "Last-Modified": "Wed, 11 Nov 2026 00:00:00 GMT", "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "Content-Length, Accept-Ranges, Content-Location, ETag, Last-Modified, X-Relay-Cache",
            "X-Relay-Cache": "HIT age=1" if HIT["on"] else "MISS-direct"}
    if req.method == "HEAD":
        return route.fulfill(status=200, headers=head, body=b"")
    if "range" not in req.headers:                          # one stream: the whole file
        ranges.append(("all",))
        return route.fulfill(status=200, headers=head, body=DATA_)
    m = re.match(r"bytes=(\d+)-(\d+)", req.headers["range"])
    assert m, req.headers["range"]
    a, z = int(m[1]), int(m[2])
    ranges.append((a, z))
    route.fulfill(status=206, headers={**head, "Content-Length": str(z - a + 1), "Content-Range": f"bytes {a}-{z}/{len(DATA_)}"},
                  body=DATA_[a:z + 1])


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route(RELAY + "**", relay)
    # the first request of the third part (of 8) waits 4 s before it goes, as a slow path of the relay
    third = f"bytes={2 * -(-len(DATA) // 8)}-"
    page.add_init_script("""(() => { const third = "%s", f = window.fetch; window.thirdAsks = 0;
      window.fetch = async (u, init) => {
        if (init && init.headers && String(init.headers.Range || "").startsWith(third) && window.thirdAsks++ === 0) await new Promise((ok) => setTimeout(ok, 4000));
        return f(u, init); }; })()""" % third)
    page.goto(url)
    page.wait_for_load_state("load")
    res = page.evaluate("""async () => {
      const regions = await import("./app/regions.js");
      const x = { id: "test/region", name: "test/region", pbf: "https://download.geofabrik.de/test/region-latest.osm.pbf" };
      const t0 = performance.now(), got = await regions.download(x, () => {}), ms = performance.now() - t0;
      const sha = [...new Uint8Array(await crypto.subtle.digest("SHA-256", await got.file.arrayBuffer()))].map((b) => b.toString(16).padStart(2, "0")).join("");
      await regions.remove(x.id);
      return { size: got.size, sha, ms, thirdAsks: window.thirdAsks };
    }""")
    parts_ranges = list(ranges)
    # a file in the cache of the relay: one connection; Download: the file goes to the file of the save dialog, not into the storage
    HIT["on"] = True
    ranges.clear()
    res2 = page.evaluate("""async () => {
      const regions = await import("./app/regions.js");
      const x = { id: "test/small", name: "test/small", pbf: "https://download.geofabrik.de/test/small-latest.osm.pbf" };
      const to = await (await navigator.storage.getDirectory()).getFileHandle("saved.osm.pbf", { create: true });
      await regions.download(x, () => {}, null, to);
      const f = await to.getFile();
      const sha = [...new Uint8Array(await crypto.subtle.digest("SHA-256", await f.arrayBuffer()))].map((b) => b.toString(16).padStart(2, "0")).join("");
      return { size: f.size, sha, kept: (await regions.kept()).length };
    }""")
    browser.close()

assert not errors, errors
assert res["size"] == len(DATA), res
assert res["sha"] == SHA, "the stored file is not the file of the relay"
assert res["thirdAsks"] >= 2, "the slow request did not go again"
assert len(parts_ranges) > 8, f"no part took over bytes of another part: {len(parts_ranges)} requests"
assert res2["sha"] == SMALL_SHA and res2["size"] == len(SMALL), "the saved file is not the file of the relay"
assert res2["kept"] == 0, "Download kept a file in the browser"
assert ("all",) in ranges and len(ranges) == 2, f"a cached file did not come in one connection: {ranges}"
print(f"download_e2e: passed ({len(parts_ranges)} range requests, {res['ms'] / 1000:.1f} s, the file equals the relay's; "
      f"cached file and Download in one connection: equal)")
sys.exit(0)
