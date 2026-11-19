"""quack.html with the output "Vector tiles (PMTiles, Shortbread)" in headless Chromium (Playwright), at phone size
(390 x 844) and at desktop size: duckOSM's quack --pmtiles (docs/exports/pmtiles.md of duckOSM) in the browser. The
page makes the PMTiles file (tile by tile is on by default), shows its numbers and a map of it with the VersaTiles
style. The downloaded file must equal the native file of `duckosm quack FILE --pmtiles -o DIR --name N` byte for byte
(--native DIR/N.pmtiles; the same run of the engine, with the same --tile-zoom). The map needs the network (MapLibre
and the style come from the internet). Build the page first (build.sh).

    python crates/duckosm-wasm/www/quack_pmtiles_e2e.py [out folder] [--pbf file.osm.pbf] [--zoom Z] [--native FILE]
"""
import argparse
import pathlib
import shutil
import tempfile

from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

from serve import WWW, start

ROOT = pathlib.Path(__file__).resolve().parents[3]
a = argparse.ArgumentParser()
a.add_argument("out", nargs="?", default="out-quack-pmtiles")
a.add_argument("--pbf", default=str(ROOT / "tests/data/monaco.osm.pbf"))
a.add_argument("--zoom", default="8", help="the tile zoom of the run ('' for one piece)")
a.add_argument("--native", help="the native .pmtiles file to compare with")
args = a.parse_args()
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)

server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/quack.html"
with sync_playwright() as p:
    for name, size in [("phone", {"width": 390, "height": 844}), ("desktop", {"width": 1280, "height": 900})]:
        profile = tempfile.mkdtemp(prefix="quack-pmtiles-e2e-")
        phone = name == "phone"
        b = p.chromium.launch_persistent_context(profile, viewport=size, has_touch=phone, is_mobile=phone, accept_downloads=True)
        page = b.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(url)
        assert page.evaluate("crossOriginIsolated"), "the page is not cross-origin isolated (serve.py sends COOP and COEP)"
        page.click("label[for=src-f]")
        page.set_input_files("#file", args.pbf)
        page.select_option("#output", "pmtiles")
        assert page.is_checked("#tiles") and not page.is_checked("#sort") and page.is_disabled("#sort"), "vector tiles: tile by tile, no sort"
        assert page.text_content("#run") == "Make the tiles"
        assert page.is_visible("#zoom") and page.is_hidden("#workers"), "one run: the tile size, no tile workers"
        if args.zoom == "":
            page.uncheck("#tiles")
        else:
            page.select_option("#zoom", args.zoom)
        page.click("#run")
        for _ in range(240):
            try:
                page.wait_for_function("/Done|Error/.test(document.getElementById('status').textContent)", timeout=15000)
                break
            except PlaywrightTimeout:
                print("  ", page.text_content("#clock"), page.text_content("#status"), "|", page.text_content("#mem-text"), flush=True)
        else:
            raise AssertionError("no end after 60 minutes: " + page.text_content("#status"))
        status = page.text_content("#status")
        assert status == "Done.", status
        got = {k: page.text_content(f"#{k}") for k in ("nodes", "ways", "relations", "seconds", "mib", "size")}
        print(name, got)
        steps = page.eval_on_selector_all("#steplist li", "ls => ls.map(l => l.className + ' ' + l.textContent)")
        assert all(s.startswith("done") for s in steps), steps
        print("   " + "\n   ".join(page.eval_on_selector_all("#steps tr", "rs => rs.map(r => [...r.cells].map(c => c.textContent).join(' | '))")))
        layers = page.evaluate("window.lastResult.layers")
        assert {"streets", "buildings", "boundaries", "place_labels", "pois"} <= set(layers), sorted(layers)
        assert page.text_content("#download") == "Download PMTiles" and page.is_hidden("#duckdb")
        with page.expect_download() as d:
            page.click("#download")
        f = out / d.value.suggested_filename
        d.value.save_as(f)
        data = f.read_bytes()
        assert data[:8] == b"PMTiles\x03", data[:8]
        if args.native:
            same = data == pathlib.Path(args.native).read_bytes()
            print(f"   {f.name}: {len(data)} bytes, {'equal to' if same else 'NOT equal to'} the native file")
            assert same, "the file differs from the native file"
        # the map of the file: MapLibre loads its tiles, and the map has features of the file
        page.wait_for_selector("#preview[data-ready]", timeout=60000)
        n = page.evaluate("window.previewMap.queryRenderedFeatures().filter(f => f.source === 'versatiles-shortbread').length")
        assert n > 50, f"the map shows only {n} features of the file"
        print(f"   the map shows {n} features of the file")
        page.wait_for_timeout(1000)
        page.screenshot(path=str(out / f"quack-pmtiles-{name}.png"), full_page=True)
        wide = page.evaluate("document.documentElement.scrollWidth - innerWidth")
        assert wide <= 1, f"the page is {wide} px wider than the screen"
        assert not errs, errs
        b.close()
        shutil.rmtree(profile, ignore_errors=True)
server.shutdown()
print("quack_pmtiles_e2e: passed")
