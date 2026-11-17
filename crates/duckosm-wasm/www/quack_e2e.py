"""quack.html in headless Chromium (Playwright), at phone size (390 x 844): choose a .osm.pbf file, make the features table
of QuackOSM in the browser (duckOSM's quack, docs/rust/quack.md of duckOSM), and download the GeoParquet file into the
out folder. On Monaco (the default file), the rows of each kind must be QuackOSM's (3957 nodes, 5573 ways, 56
relations), sorted and not sorted. With --tiles, the table is made tile by tile (the mode for a large file); on Monaco
(the test sets tiles of at most 2000 nodes, so many ways cross a tile edge) the rows must be the same. Prints the rows, the
time, the threads and the WebAssembly memory of each run. Build the page first (build.sh).

    python crates/duckosm-wasm/www/quack_e2e.py [out folder] [--pbf file.osm.pbf] [--tiles]
"""
import argparse
import pathlib
import shutil
import tempfile

from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

from serve import WWW, start

ROOT = pathlib.Path(__file__).resolve().parents[3]
a = argparse.ArgumentParser()
a.add_argument("out", nargs="?", default="out-quack")
a.add_argument("--pbf", default=str(ROOT / "tests/data/monaco.osm.pbf"))
a.add_argument("--tiles", action="store_true", help="make the table tile by tile")
a.add_argument("--zoom", help="tile by tile with the map tiles of this zoom (with --tiles; the page's default is zoom 8, Monaco uses small tiles)")
a.add_argument("--threads", help="with --tiles: the threads of the cut, the tile workers and the threads of the join")
a.add_argument("--memory", choices=["0", "1"], help="with --tiles: the parts of the cut in OPFS (0) or in memory (1)")
a.add_argument("--keep", action="store_true", help="keep the GeoParquet file of a large file (to compare it)")
a.add_argument("--duckdb", action="store_true", help="also make the .duckdb file of a large file (always on Monaco)")
a.add_argument("--sort", choices=["both", "yes", "no"], default="both", help="the runs: sorted, not sorted, or both")
args = a.parse_args()
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
monaco = pathlib.Path(args.pbf).name == "monaco.osm.pbf"

server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/quack.html"
if args.memory:
    url += f"?memory={args.memory}"
with sync_playwright() as p:
    # a profile on disk, as a person's browser: a new context of Playwright is like an incognito window, whose OPFS
    # quota (about 1 GB) is less than the temporary files of Kentucky
    profile = tempfile.mkdtemp(prefix="quack-e2e-")
    b = p.chromium.launch_persistent_context(profile, viewport={"width": 390, "height": 844},
                                             has_touch=True, is_mobile=True, accept_downloads=True)
    page = b.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(url)
    assert page.evaluate("crossOriginIsolated"), "the page is not cross-origin isolated (serve.py sends COOP and COEP)"
    page.click("label[for=src-f]")  # the input card: Local file
    page.set_input_files("#file", args.pbf)
    page.set_checked("#tiles", args.tiles)
    if args.tiles and args.zoom:
        page.select_option("#zoom", args.zoom)
    elif args.tiles and monaco:
        page.select_option("#zoom", "")  # the small tiles of at most 2000 nodes below (zoom 8 is one tile for Monaco)
    if args.tiles and args.threads:
        for i in ("#cut-threads", "#workers", "#join-threads"):
            page.fill(i, args.threads)
    if args.tiles and monaco:
        # small tiles, so that ways and multipolygons cross the edges of the tiles
        page.evaluate("document.getElementById('run').dataset.tileNodes = 2000")
    for sort in {"both": (True, False), "yes": (True,), "no": (False,)}[args.sort]:
        page.set_checked("#sort", sort)
        # each status with its time, so that the sub-steps of the cut show their times
        page.evaluate("""() => { const t0 = performance.now(); window.statusLog = [];
            new MutationObserver(() => statusLog.push(((performance.now() - t0) / 1000).toFixed(1) + " s " +
              document.getElementById("status").textContent.slice(0, 140))).observe(document.getElementById("status"),
              { childList: true, characterData: true, subtree: true }); }""")
        page.click("#run")
        # the status and the memory each 15 s, so a long run shows where it is
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
        got = {k: page.text_content(f"#{k}") for k in ("nodes", "ways", "relations", "seconds", "threads", "mib", "size")}
        steps = page.eval_on_selector_all("#steps tr", "rs => rs.map(r => [...r.cells].map(c => c.textContent).join(' | '))")
        last = "4." if args.tiles else ("5." if not sort else "6.")
        assert steps and steps[-1].startswith(last), steps
        done = page.eval_on_selector_all("#progress li.done", "ls => ls.length")
        assert done == len(steps), (done, steps)
        print("sorted" if sort else "not sorted", got)
        print("   " + "\n   ".join(steps))
        if args.tiles:
            print("  ", page.text_content("#timeline-text"))
            print("   the WebAssembly memory of the join worker:", page.evaluate("(window.lastResult || {}).joinMib"), "MiB")
            # the passes of the longest tile (window.lastResult: the result of the run)
            print("   " + "\n   ".join(page.evaluate("""() => { const r = window.lastResult || {}; const tl = r.timeline || [];
                if (!tl.length) return [];
                const long = tl.reduce((a, x) => (x.end - x.start > a.end - a.start ? x : a), tl[0]).tile;
                const io = tl.find((x) => x.tile === long).io;
                return (r.tileSteps || []).filter((s) => s.tile === long).map((s) => `${long} ${(s.ms / 1000).toFixed(1)} s ${s.step}`)
                  .concat([`${long}: ${((io || 0) / 1000).toFixed(1)} s waiting for the file and the storage`]); }""")))
        print("   " + "\n   ".join(l for l in page.evaluate("statusLog") if "tile cut" in l or "tiles 2" in l))
        if args.tiles and sort:
            # the merge of the sorted parts tells the page its progress
            merging = [l for l in page.evaluate("statusLog") if "merging the sorted parts" in l]
            assert merging and merging[-1].endswith("100 %"), merging[-3:]
            print("  ", merging[-1])
        if monaco:
            assert (got["nodes"], got["ways"], got["relations"]) == ("3,957", "5,573", "56"), got
        with page.expect_download() as d:
            page.click("#download")
        f = out / (("sorted_" if sort else "") + d.value.suggested_filename)
        d.value.save_as(f)
        with open(f, "rb") as h:
            head = h.read(4)
            h.seek(-4, 2)
            assert head == b"PAR1" and h.read(4) == b"PAR1", f"{f} is not a Parquet file"
        if not monaco and not args.keep:
            f.unlink()  # a large file: the check is enough
        # the .duckdb file, written by DuckDB in the browser into OPFS
        if not monaco and not args.duckdb:
            continue
        page.click("#duckdb")
        page.wait_for_function("/MB in|Error/.test(document.getElementById('duckdb-status').textContent)", timeout=3600000)
        print("   " + page.text_content("#duckdb-status"))
        assert "Error" not in page.text_content("#duckdb-status")
        if not monaco:
            assert got["nodes"] and page.text_content("#duckdb-status").count("rows"), "no rows"
            continue
        with page.expect_download() as d:
            page.click("#duckdb-download")
        f = out / (("sorted_" if sort else "") + d.value.suggested_filename)
        d.value.save_as(f)
        assert f.read_bytes()[8:12] == b"DUCK", f"{f} is not a DuckDB file"
    # phone size: nothing wider than the screen
    wide = page.evaluate("document.documentElement.scrollWidth - innerWidth")
    page.screenshot(path=str(out / "quack-phone.png"), full_page=True)
    assert wide <= 1, f"the page is {wide} px wider than the screen"
    assert not errs, errs
    b.close()
    server.shutdown()
    # the profile holds the files of the runs in OPFS (Kentucky: 2 GB)
    shutil.rmtree(profile, ignore_errors=True)
print("quack_e2e: passed")
