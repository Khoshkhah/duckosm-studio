"""Studio's files in the browser (app/storage.js) in headless Chromium (Playwright): the row "Studio keeps ... in this browser" and Delete all.
A build of Monaco (the page saves the area), then other entries of Studio (a kept region file, the folder of a closed tab, the files of an
old build) and entries of other pages of the same site (other-page/, lv-other-app/). Delete all removes Studio's entries only: the
other pages' entries stay, the folder of this tab stays, and the open area's tables still answer.

    python crates/duckosm-wasm/www/storage_e2e.py [out folder]
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

from serve import WWW, start

ROOT = pathlib.Path(__file__).resolve().parents[3]
out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "out-storage")
out.mkdir(parents=True, exist_ok=True)
server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
NAMES = "navigator.storage.getDirectory().then(async (r) => { const n = []; for await (const [k] of r.entries()) n.push(k); return n.sort(); })"
# the entries in Studio's folders regions/ and areas/ (the page makes the folders again, empty, when it lists them)
INSIDE = """navigator.storage.getDirectory().then(async (r) => { const n = [];
  for (const d of ["regions", "areas"]) { try { for await (const [k] of (await r.getDirectoryHandle(d)).entries()) n.push(d + "/" + k); } catch (e) {} }
  return n; })"""

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_function("window.mapReady && window.timings && window.timings.duckdbStart", timeout=120_000)
    page.evaluate("localStorage.clear()")
    page.set_input_files("#file", str(ROOT / "tests/data/monaco.geojson"))
    page.wait_for_function("window.areas >= 1", timeout=60_000)
    page.click("#tab-region")
    page.set_input_files("#region", str(ROOT / "tests/data/monaco_sample.osm.pbf"))
    page.click("#run")
    page.wait_for_function("/^(Built|Error)/.test(document.getElementById('buildstatus').textContent)", timeout=600_000)
    assert page.inner_text("#buildstatus").startswith("Built"), page.inner_text("#buildstatus")
    # other entries: Studio's (a kept region file, a closed tab, an old build) and other pages' (the same site shares one OPFS)
    page.evaluate("""async () => {
      const root = await navigator.storage.getDirectory(), put = async (dir, file, mb) => {
        let d = root; for (const part of dir) d = await d.getDirectoryHandle(part, { create: true });
        const w = await (await d.getFileHandle(file, { create: true })).createWritable(); await w.write(new Uint8Array(mb << 20)); await w.close(); };
      await put(["regions"], "test_region.osm.pbf", 2);
      await put(["tables-closed01"], "edges_driving.parquet", 1);
      await put(["build-1-old"], "o0", 1);
      await put(["other-page"], "data.bin", 1);
      await put(["lv-other-app"], "pairs.csv", 1);
    }""")
    # the row shows after the page saved the area; Delete all reads the entries again when it is clicked
    page.wait_for_function("!document.getElementById('storage-row').hidden && /Studio keeps/.test(document.getElementById('storage-line').textContent)", timeout=30_000)
    before = page.evaluate(NAMES)
    line = page.inner_text("#storage-line")
    folder = page.evaluate("window.duckApi.FOLDER")
    assert {"regions", "areas", "tables-closed01", "build-1-old", "other-page", "lv-other-app", folder} <= set(before), before
    page.screenshot(path=str(out / "storage-row.png"))
    page.evaluate("rs.confirm = async () => true")          # roadstyle's confirm dialog: the test answers it
    page.click("#storage-clear")
    page.wait_for_function("document.getElementById('storage-row').hidden", timeout=30_000)
    after = set(page.evaluate(NAMES))
    inside = page.evaluate(INSIDE)
    rows = page.evaluate("window.duckApi.query('SELECT count(*) AS n FROM driving.edges').then((r) => Number(r.toArray()[0].n))")
    browser.close()

assert not errors, errors
assert not {"tables-closed01", "build-1-old"} & after, f"Studio's old folders stay: {sorted(after)}"
assert not inside, f"Studio's saved areas or region files stay: {inside}"
assert {"other-page", "lv-other-app", folder} <= after, f"a file of another page or of this tab went: {sorted(after)}"
assert rows > 0, "the open area's tables do not answer"
print(f"storage_e2e: passed ({line!r}; after Delete all: {sorted(after)}; the open area has {rows} driving edges)")
sys.exit(0)
