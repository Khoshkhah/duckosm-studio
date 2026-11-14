"""quack.html in headless Chromium (Playwright), at phone size (390 x 844): choose a .osm.pbf file, make the features table
of QuackOSM in the browser (duckOSM's quack, docs/rust/quack.md of duckOSM), and download the GeoParquet file into the
out folder. On Monaco (the default file), the rows of each kind must be QuackOSM's (3957 nodes, 5573 ways, 56
relations), sorted and not sorted. Prints the rows, the time and the threads of each run. Build the page first
(build.sh).

    python crates/duckosm-wasm/www/quack_e2e.py [out folder] [--pbf file.osm.pbf]
"""
import argparse
import pathlib

from playwright.sync_api import sync_playwright

from serve import WWW, start

ROOT = pathlib.Path(__file__).resolve().parents[3]
a = argparse.ArgumentParser()
a.add_argument("out", nargs="?", default="out-quack")
a.add_argument("--pbf", default=str(ROOT / "tests/data/monaco.osm.pbf"))
args = a.parse_args()
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
monaco = pathlib.Path(args.pbf).name == "monaco.osm.pbf"

server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/quack.html"
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True, accept_downloads=True)
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(url)
    assert page.evaluate("crossOriginIsolated"), "the page is not cross-origin isolated (serve.py sends COOP and COEP)"
    page.set_input_files("#file", args.pbf)
    for sort in (True, False):
        page.set_checked("#sort", sort)
        page.click("#run")
        page.wait_for_function("/Done|Error/.test(document.getElementById('status').textContent)", timeout=600000)
        status = page.text_content("#status")
        assert status == "Done.", status
        got = {k: page.text_content(f"#{k}") for k in ("nodes", "ways", "relations", "seconds", "threads", "mib", "size")}
        print("sorted" if sort else "not sorted", got)
        if monaco:
            assert (got["nodes"], got["ways"], got["relations"]) == ("3,957", "5,573", "56"), got
        with page.expect_download() as d:
            page.click("#download")
        f = out / (("sorted_" if sort else "") + d.value.suggested_filename)
        d.value.save_as(f)
        data = f.read_bytes()
        assert data[:4] == b"PAR1" and data[-4:] == b"PAR1", f"{f} is not a Parquet file"
    # phone size: nothing wider than the screen
    wide = page.evaluate("document.documentElement.scrollWidth - innerWidth")
    page.screenshot(path=str(out / "quack-phone.png"), full_page=True)
    assert wide <= 1, f"the page is {wide} px wider than the screen"
    assert not errs, errs
    b.close()
    server.shutdown()
print("quack_e2e: passed")
