"""The DEM input of step 4 in headless Chromium (Playwright): a build of Monaco with a DEM (a GeoTIFF) writes the table main.elevation_grid
(col, row, lon, lat, z), the heights of the nodes and the metadata, and the downloads list it. A build without a DEM has none of them.

    python crates/duckosm-wasm/www/dem_e2e.py [out folder]

It uses tests/data/monaco_sample.osm.pbf, monaco.geojson and monaco-copernicus-glo30.tif (the Copernicus GLO-30 DEM of Monaco, from SimReady).
Build the page first (docs/development.md, "WebAssembly"). The page loads duckdb-wasm from cdn.jsdelivr.net, so the computer needs the network.
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

from serve import WWW, start

ROOT = pathlib.Path(__file__).resolve().parents[3]
DATA = ROOT / "tests" / "data"
out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "out-dem")
out.mkdir(parents=True, exist_ok=True)
server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/index.html?maxroads=0"


def count(page, table):
    return page.evaluate(f"window.duckApi.query('SELECT count(*) AS n FROM {table}').then((r) => Number(r.toArray()[0].n))")


def build(page):
    if not page.is_visible("#run"):
        page.evaluate("rs.sections.fold(document.getElementById('step4-card').dataset.rsSection, false)")      # step 4 folds after a build: open it again
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

    # with a DEM
    page.set_input_files("#dem", str(DATA / "monaco-copernicus-glo30.tif"))
    assert "monaco-copernicus-glo30.tif" in page.text_content("#dem-name")
    build(page)
    n = count(page, "main.elevation_grid")
    assert n > 0, "main.elevation_grid has no rows"
    cols = page.evaluate("window.duckApi.query('DESCRIBE main.elevation_grid').then((r) => r.toArray().map((x) => x.column_name))")
    assert cols == ["col", "row", "lon", "lat", "z"], cols
    box = page.evaluate("window.duckApi.query('SELECT min(lon) a, max(lon) b, min(lat) c, max(lat) d, count(z) nz FROM main.elevation_grid').then((r) => r.toArray()[0].toJSON())")
    assert 7.3 < box["a"] < box["b"] < 7.6 and 43.6 < box["c"] < box["d"] < 43.9, box      # Monaco
    assert count(page, "driving.elevation_nodes") == count(page, "driving.nodes"), "a height row for each node"
    assert count(page, "main.elevation_metadata") == 1
    uri = page.evaluate("window.duckApi.query('SELECT uri FROM main.elevation_metadata').then((r) => String(r.toArray()[0].uri))")
    assert uri == "monaco-copernicus-glo30.tif", uri
    listed = page.text_content("#files")
    assert "elevation_grid" in listed and f"{n:,}" in listed.replace(" ", ","), listed[:300]
    page.screenshot(path=str(out / "dem.png"))
    print(f"with a DEM: main.elevation_grid has {n} rows, lon {box['a']:.4f} to {box['b']:.4f}, lat {box['c']:.4f} to {box['d']:.4f}")
    assert not errors, errors
    browser.close()
print("ok")
