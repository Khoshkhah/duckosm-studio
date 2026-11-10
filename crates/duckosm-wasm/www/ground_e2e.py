"""The map terrain from the area's own ground in headless Chromium (Playwright; roadstyle docs/design/ground_terrain.md). A build of Monaco
with a DEM makes the terrain file from main.elevation_grid (roadstyle's engine), writes it to the browser disk (OPFS, ground.pmtiles) and
gives it to the map (rsGround): the hillshade and the 3D terrain then make no request to the AWS Terrain Tiles, the attribution names the
ground, and each pixel of the file's tiles equals the grid (the check of the design: at most 0.10 m). A build without a DEM uses the AWS
tiles and says so. The AWS requests are answered with an error here, so the test counts them and needs no AWS.

    python crates/duckosm-wasm/www/ground_e2e.py [out folder]

It uses tests/data/monaco_sample.osm.pbf, monaco.geojson and monaco-copernicus-glo30.tif. Build the page first (docs/development.md,
"WebAssembly"). The page loads duckdb-wasm from cdn.jsdelivr.net, so the computer needs the network.
"""
import math
import pathlib
import sys

from playwright.sync_api import sync_playwright

from serve import WWW, start

ROOT = pathlib.Path(__file__).resolve().parents[3]
DATA = ROOT / "tests" / "data"
out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "out-ground")
out.mkdir(parents=True, exist_ok=True)
server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/index.html?maxroads=0"
CENTER = [7.4246, 43.7384]


def build(page):
    if not page.is_visible("#run"):
        page.evaluate("rs.sections.fold(document.getElementById('step4-card').dataset.rsSection, false)")      # step 4 folds after a build: open it again
    page.evaluate("document.getElementById('buildstatus').textContent = ''")
    page.click("#run")
    page.wait_for_function("/^(Built|Error)/.test(document.getElementById('buildstatus').textContent)", timeout=600_000)
    status = page.inner_text("#buildstatus")
    assert not status.startswith("Error"), status
    page.wait_for_timeout(1500)       # the cards fold and open after a build


def terrain_on(page, want):
    """The 3D terrain on over Monaco, tilted, until the map has loaded every tile; the DEM source's tiles and attribution."""
    page.evaluate("""c => { const m = window.duckMap; m.jumpTo({center: c, zoom: 15, pitch: 60}); window.rsTerrain(false); window.rsTerrain(true); }""", CENTER)
    page.wait_for_function("""w => { const m = window.duckMap, s = m.getSource('rs-dem');
        return m.getTerrain() && s && s.tiles && s.tiles[0].startsWith(w) && m.areTilesLoaded(); }""", arg=want, timeout=60_000)
    return page.evaluate("(() => { const s = window.duckMap.getSource('rs-dem'); return {tiles: s.tiles, maxzoom: s.maxzoom, attr: s.attribution}; })()")


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    errors, aws = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/elevation-tiles-prod/**", lambda r: (aws.append(r.request.url), r.abort()))
    page.goto(url)
    page.wait_for_function("window.mapReady && window.timings && window.timings.duckdbStart", timeout=120_000)
    page.set_input_files("#file", str(DATA / "monaco.geojson"))
    page.wait_for_function("window.areas >= 1", timeout=60_000)
    page.click("#tab-region")
    page.set_input_files("#region", str(DATA / "monaco_sample.osm.pbf"))
    page.set_checked("#features", False)
    for box in page.query_selector_all("input[name=mode]"):
        box.set_checked(box.get_attribute("value") == "driving")

    # no DEM: the AWS tiles, and the map says so
    build(page)
    src = terrain_on(page, "https://s3.amazonaws.com/elevation-tiles-prod/")
    assert "Ground: AWS Terrain Tiles" in src["attr"], src
    assert aws, "the AWS tiles were not asked for"
    print(f"no DEM: the AWS Terrain Tiles ({len(aws)} requests)")

    # with a DEM: the area's ground, from the browser disk, and no request to AWS
    page.set_input_files("#dem", str(DATA / "monaco-copernicus-glo30.tif"))
    build(page)
    page.wait_for_function("window.rsGround() && window.rsGround().terrain instanceof File", timeout=60_000)      # the page gave the map its ground
    aws.clear()
    src = terrain_on(page, "rsground://")
    page.evaluate("window.rsHillshade(true)")
    page.wait_for_function("window.duckMap.areTilesLoaded()", timeout=60_000)
    page.wait_for_timeout(1000)
    assert not aws, f"{len(aws)} requests to AWS with the area's ground: {aws[:3]}"
    assert src["attr"] == "Ground: monaco-copernicus-glo30.tif (product unknown, datum unknown)", src      # the page knows no datum of a DEM file
    assert src["maxzoom"] == 13, src                    # 1 arc-second: the last zoom 13
    size = page.evaluate("navigator.storage.getDirectory().then(d => d.getFileHandle('ground.pmtiles')).then(h => h.getFile()).then(f => f.size)")
    print(f"with a DEM: the terrain from ground.pmtiles in OPFS ({size} bytes), {src['attr']!r}, no request to AWS")

    # the check of the design: each pixel of the file's z13 tiles over the grid (decoded by the browser) minus the grid's bilinear value at
    # the pixel's point (its north-west corner, where MapLibre reads it), at most 0.10 m
    grid = page.evaluate("""window.duckApi.query('SELECT col, row, lon, lat, z FROM main.elevation_grid').then(r => r.toArray().map(x => [x.col, x.row, x.lon, x.lat, x.z]))""")
    nc, nr = max(g[0] for g in grid) + 1, max(g[1] for g in grid) + 1
    z = {(g[0], g[1]): g[4] for g in grid}
    lon0, lat0 = min(g[2] for g in grid), max(g[3] for g in grid)
    dx, dy = (max(g[2] for g in grid) - lon0) / (nc - 1), (lat0 - min(g[3] for g in grid)) / (nr - 1)
    tiles = page.evaluate("""async () => {
        const f = await (await (await navigator.storage.getDirectory()).getFileHandle('ground.pmtiles')).getFile();
        const a = new pmtiles.PMTiles({getKey: () => 'g', getBytes: (o, l) => f.slice(o, o + l).arrayBuffer().then(data => ({data}))});
        const h = await a.getHeader(), n = 2 ** 13, out = [];
        const tx = lon => Math.floor((lon + 180) / 360 * n), ty = lat => Math.floor((1 - Math.log(Math.tan(lat * Math.PI / 180) + 1 / Math.cos(lat * Math.PI / 180)) / Math.PI) / 2 * n);
        for(let x = tx(h.minLon); x <= tx(h.maxLon); x++) for(let y = ty(h.maxLat); y <= ty(h.minLat); y++){
          const r = await a.getZxy(13, x, y), img = await createImageBitmap(new Blob([r.data]), {colorSpaceConversion: 'none'});
          const c = document.createElement('canvas'); c.width = c.height = 256; const g = c.getContext('2d'); g.drawImage(img, 0, 0);
          const px = g.getImageData(0, 0, 256, 256).data, hs = [];
          for(let i = 0; i < 65536; i++) hs.push(px[i * 4] * 256 + px[i * 4 + 1] + px[i * 4 + 2] / 256 - 32768);
          out.push([x, y, hs]);
        }
        return out; }""")
    worst, n = 0.0, 0
    for x, y, hs in tiles:
        for i, h in enumerate(hs):
            gx, gy = (x * 256 + i % 256) / (256 * 2**13), (y * 256 + i // 256) / (256 * 2**13)
            lon, lat = gx * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * gy))))
            fc, fr = (lon - lon0) / dx, (lat0 - lat) / dy
            c, r = math.floor(fc), math.floor(fr)
            if not (0 <= c < nc - 1 and 0 <= r < nr - 1):
                continue
            cells = [z[(c, r)], z[(c + 1, r)], z[(c, r + 1)], z[(c + 1, r + 1)]]
            if any(v is None for v in cells):
                continue
            a, b = fc - c, fr - r
            want = (cells[0] * (1 - a) + cells[1] * a) * (1 - b) + (cells[2] * (1 - a) + cells[3] * a) * b
            worst, n = max(worst, abs(h - want)), n + 1
    assert n > 1000 and worst <= 0.10, (n, worst)
    print(f"the file equals the grid: {n} pixels of {len(tiles)} z13 tiles, the largest difference {worst:.4f} m (limit 0.10 m)")
    page.screenshot(path=str(out / "ground.png"))
    assert not errors, errors
    browser.close()
print("ok")
