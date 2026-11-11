"""The Ground step of step 4 in headless Chromium (Playwright): duckOSM's step lists the DEM sources of the area (Monaco: EU-DTM, FABDEM, Copernicus GLO-30, EU-DEM),
a file that is dropped in builds the table main.elevation_grid (col, row, lon, lat, z), the heights of the nodes and the metadata with the product, the vertical datum and the
licence of the chosen source, and the fallback with no file makes a DEM from the AWS Terrain Tiles with the label "Ground: AWS Terrain Tiles" (product DSM). A build without a DEM has
none of the tables. A wrong file (not EPSG:4326) is refused with the gdalwarp command. A phone has no sideways scroll in the step.

    python crates/duckosm-wasm/www/dem_e2e.py [out folder]

It uses tests/data/monaco_sample.osm.pbf, monaco.geojson and monaco-copernicus-glo30.tif (the Copernicus GLO-30 DEM of Monaco, from SimReady).
Build the page first (docs/development.md, "WebAssembly"). The page loads duckdb-wasm from cdn.jsdelivr.net, so the computer needs the network. The AWS tiles are
answered by the test (a flat Terrarium PNG): the test does not need AWS.
"""
import pathlib
import struct
import sys
import zlib

from playwright.sync_api import sync_playwright

from serve import WWW, start

ROOT = pathlib.Path(__file__).resolve().parents[3]
DATA = ROOT / "tests" / "data"
out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "out-dem")
out.mkdir(parents=True, exist_ok=True)
server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/index.html?maxroads=0"


def terrarium_png(h):
    """A 256 x 256 Terrarium tile with the height h (m) everywhere."""
    v = h + 32768
    row = b"\x00" + bytes([int(v // 256), int(v % 256), int((v - int(v)) * 256)]) * 256
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(row * 256)) + chunk(b"IEND", b"")


def count(page, table):
    return page.evaluate(f"window.duckApi.query('SELECT count(*) AS n FROM {table}').then((r) => Number(r.toArray()[0].n))")


def open_step4(page):
    """Step 4 folds after a build: open it again."""
    if not page.is_visible("#run"):
        page.evaluate("rs.sections.fold(document.getElementById('step4-card').dataset.rsSection, false)")


def open_ground(page):
    open_step4(page)
    page.evaluate("document.getElementById('ground-row').open = true")


def build(page):
    open_step4(page)
    page.evaluate("document.getElementById('buildstatus').textContent = ''")      # the line of the last build goes away
    page.click("#run")
    page.wait_for_function("/^(Built|Error)/.test(document.getElementById('buildstatus').textContent)", timeout=600_000)
    status = page.inner_text("#buildstatus")
    assert not status.startswith("Error"), status
    page.wait_for_timeout(1500)       # the cards fold and open after a build


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_function("window.mapReady && window.timings && window.timings.duckdbStart", timeout=120_000)
    page.set_input_files("#file", str(DATA / "monaco.geojson"))
    page.wait_for_function("window.areas >= 1", timeout=60_000)
    page.click("#tab-region")
    page.set_input_files("#region", str(DATA / "monaco_sample.osm.pbf"))
    page.set_checked("#features", False)
    for box in page.query_selector_all("input[name=mode]"):
        box.set_checked(box.get_attribute("value") == "driving")

    # no DEM: none of the elevation tables
    build(page)
    tables = page.evaluate("window.duckApi.query(\"SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'elevation%'\").then((r) => r.toArray().map((x) => x.table_name))")
    assert tables == [], tables
    assert "elevation_grid" not in page.inner_text("#files")
    print("no DEM: no elevation tables")

    # Monaco: the sources in order, the best one chosen; with no file there is no DEM (so the build above had none)
    open_ground(page)
    names = page.eval_on_selector_all("#ground [data-gs=list] input", "els => els.map((e) => e.value)")
    assert names == ["eudtm", "fabdem", "copernicus-glo30", "eu-dem"], names
    assert page.eval_on_selector("#ground input[name=gs-src]:checked", "e => e.value") == "eudtm"
    assert "Ground: AWS Terrain Tiles" in page.text_content("#ground"), page.inner_html("#ground")[:600]
    print("the sources of Monaco:", ", ".join(names))

    # (the refusal of a file in another CRS is checked in duckOSM's tests/test_ground_step.py, with a real EPSG:3006 file)

    # a Copernicus GLO-30 file: the product, the datum and the licence are the catalog's
    page.click("#ground label:has(input[value=copernicus-glo30])")
    page.set_input_files("#ground [data-gs=file]", str(DATA / "monaco-copernicus-glo30.tif"))
    page.wait_for_selector("#ground [data-gs=pick] span:text('monaco-copernicus-glo30.tif')")
    assert page.query_selector("#ground .gs-bad") is None, page.inner_text("#ground")
    build(page)
    n = count(page, "main.elevation_grid")
    assert n > 0, "main.elevation_grid has no rows"
    cols = page.evaluate("window.duckApi.query('DESCRIBE main.elevation_grid').then((r) => r.toArray().map((x) => x.column_name))")
    assert cols == ["col", "row", "lon", "lat", "z"], cols
    box = page.evaluate("window.duckApi.query('SELECT min(lon) a, max(lon) b, min(lat) c, max(lat) d, count(z) nz FROM main.elevation_grid').then((r) => r.toArray()[0].toJSON())")
    assert 7.3 < box["a"] < box["b"] < 7.6 and 43.6 < box["c"] < box["d"] < 43.9, box      # Monaco
    assert count(page, "driving.elevation_nodes") == count(page, "driving.nodes"), "a height row for each node"
    assert count(page, "main.elevation_metadata") == 1
    meta = page.evaluate("window.duckApi.query('SELECT * FROM main.elevation_metadata').then((r) => r.toArray()[0].toJSON())")
    assert meta["uri"] == "monaco-copernicus-glo30.tif", meta
    assert (meta["product"], meta["vertical_datum"], meta["license"]) == ("DSM", "EGM2008", "Copernicus open (attribution)"), meta
    assert meta["sha256"] and len(meta["sha256"]) == 64, meta
    listed = page.text_content("#files")
    assert "elevation_grid" in listed and f"{n:,}" in listed.replace(" ", ","), listed[:300]
    page.screenshot(path=str(out / "dem.png"))
    print(f"a Copernicus file: main.elevation_grid has {n} rows, lon {box['a']:.4f} to {box['b']:.4f}, lat {box['c']:.4f} to {box['d']:.4f}; {meta['product']}, {meta['vertical_datum']}, {meta['license']}")

    # FABDEM: a DTM with its licence (the same file stands for it; the catalog gives the metadata)
    open_ground(page)
    page.click("#ground label:has(input[value=fabdem])")
    page.set_input_files("#ground [data-gs=file]", str(DATA / "monaco-copernicus-glo30.tif"))
    page.wait_for_selector("#ground [data-gs=pick] span:text('monaco-copernicus-glo30.tif')")
    build(page)
    meta = page.evaluate("window.duckApi.query('SELECT * FROM main.elevation_metadata').then((r) => r.toArray()[0].toJSON())")
    assert (meta["product"], meta["vertical_datum"], meta["license"]) == ("DTM", "EGM2008", "CC-BY-NC-SA-4.0"), meta
    print("a FABDEM file:", meta["product"], meta["vertical_datum"], meta["license"])

    # the fallback with no file: a grid from the AWS Terrain Tiles, labelled, never a reference
    page.route("https://s3.amazonaws.com/elevation-tiles-prod/**", lambda r: r.fulfill(status=200, body=terrarium_png(100), content_type="image/png", headers={"Access-Control-Allow-Origin": "*"}))
    open_ground(page)
    page.click("#ground label:has(input[value=aws-terrain-tiles])")
    build(page)
    meta = page.evaluate("window.duckApi.query('SELECT * FROM main.elevation_metadata').then((r) => r.toArray()[0].toJSON())")
    assert meta["source"] == "Ground: AWS Terrain Tiles" and meta["product"] == "DSM" and meta["uri"] == "Ground: AWS Terrain Tiles", meta
    n2 = count(page, "main.elevation_grid")
    z = page.evaluate("window.duckApi.query('SELECT min(z) a, max(z) b FROM main.elevation_grid').then((r) => r.toArray()[0].toJSON())")
    assert n2 > 0 and abs(z["a"] - 100) < 0.01 and abs(z["b"] - 100) < 0.01, (n2, z)
    assert count(page, "driving.elevation_nodes") == count(page, "driving.nodes")
    print(f"AWS Terrain Tiles: main.elevation_grid has {n2} rows, z {z['a']:.2f}; source {meta['source']}, product {meta['product']}")
    assert not errors, errors

    # a phone: the step has no sideways scroll, and its buttons are a finger's size
    phone = browser.new_context(**p.devices["iPhone 13"]).new_page()
    phone.on("pageerror", lambda e: errors.append(str(e)))
    phone.goto(url)
    phone.wait_for_function("window.mapReady && window.timings && window.timings.duckdbStart", timeout=120_000)
    phone.set_input_files("#file", str(DATA / "monaco.geojson"))
    phone.wait_for_function("window.areas >= 1", timeout=60_000)
    phone.click("#tab-region")
    phone.set_input_files("#region", str(DATA / "monaco_sample.osm.pbf"))
    phone.evaluate("rs.sections.fold(document.getElementById('step4-card').dataset.rsSection, false); document.getElementById('ground-row').open = true; document.getElementById('ground-row').scrollIntoView()")
    phone.wait_for_selector("#ground [data-gs=list] label", state="visible")
    assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth && document.getElementById('sb').scrollWidth <= document.getElementById('sb').clientWidth + 1"), "a sideways scroll"
    over = phone.evaluate("""() => { const sb = document.getElementById('sb').getBoundingClientRect(); return [...document.querySelectorAll('#ground *')].filter((e) => e.getBoundingClientRect().width && e.getBoundingClientRect().right > sb.right + 1).map((e) => e.tagName + '.' + e.className); }""")
    assert not over, over
    small = phone.evaluate("[...document.querySelectorAll('#ground button, #ground summary')].filter((e) => e.getBoundingClientRect().height && e.getBoundingClientRect().height < 40).length")
    assert small == 0, small
    phone.screenshot(path=str(out / "phone-ground.png"))
    print("phone: the step fits at 390 px")
    assert not errors, errors
    browser.close()
