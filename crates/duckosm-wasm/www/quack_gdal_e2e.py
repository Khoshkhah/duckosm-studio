"""quack.html with the output "GDAL layers" in headless Chromium (Playwright), at phone size (390 x 844): GDAL's five
layers (duckOSM `docs/rust/quack.md`, "GDAL layers") of a .osm.pbf file, made tile by tile in the browser. On Monaco, small
tiles (at most 2000 nodes, so ways, routes and multipolygons cross the edges of the tiles) and the rows of each layer of
`ogr2ogr -f GPKG` (3960 points, 3836 lines, 69 multilinestrings, 1793 multipolygons, 129 other_relations). With
--native DIR, each downloaded file must have the rows of the native file DIR/<name>_gdal_<layer>.parquet of
`duckosm-rust quack FILE --gdal -f geoparquet -o DIR --name <name>` (the same rows, in any order). With --format gpkg or
both, the page also makes the GeoPackage: SQLite's integrity_check must be ok, GDAL's ogrinfo (when it is installed) must
open it, and with --native the rows of each layer (geometry as hex, no fid) must equal those of DIR/<name>_gdal.gpkg of
`duckosm quack FILE --gdal -o DIR --name <name>`, as sets. With --fixes FILE, the page gets that OSM fixes file
(Settings, OSM fixes), and the native files must be made with the same --fixes. Build the page first (build.sh).

    python crates/duckosm-wasm/www/quack_gdal_e2e.py [out folder] [--pbf file.osm.pbf] [--zoom Z] [--native DIR] [--name N]
        [--format geoparquet|gpkg|both]
"""
import argparse
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile

from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

from serve import WWW, start

LAYERS = ["points", "lines", "multilinestrings", "multipolygons", "other_relations"]
MONACO = {"points": 3960, "lines": 3836, "multilinestrings": 69, "multipolygons": 1793, "other_relations": 129}
ROOT = pathlib.Path(__file__).resolve().parents[3]
a = argparse.ArgumentParser()
a.add_argument("out", nargs="?", default="out-quack-gdal")
a.add_argument("--pbf", default=str(ROOT / "tests/data/monaco.osm.pbf"))
a.add_argument("--zoom", help="the map tiles of this zoom (default on Monaco: tiles of at most 2000 nodes; else zoom 8)")
a.add_argument("--native", help="the folder of the native GeoParquet files to compare with")
a.add_argument("--name", default="monaco", help="the --name of the native files")
a.add_argument("--keep", action="store_true", help="keep the downloaded files of a large file")
a.add_argument("--no-download", action="store_true", help="check the five downloads but do not save them (a large file: no second copy on the disk)")
a.add_argument("--format", default="geoparquet", choices=["geoparquet", "gpkg", "both"])
a.add_argument("--fixes", help="an OSM fixes file for the page (Settings, OSM fixes)")
args = a.parse_args()
parquet, gpkg = args.format != "gpkg", args.format != "geoparquet"
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
monaco = pathlib.Path(args.pbf).name == "monaco.osm.pbf"

server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/quack.html"
with sync_playwright() as p:
    profile = tempfile.mkdtemp(prefix="quack-gdal-e2e-")
    b = p.chromium.launch_persistent_context(profile, viewport={"width": 390, "height": 844},
                                             has_touch=True, is_mobile=True, accept_downloads=True)
    page = b.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(url)
    assert page.evaluate("crossOriginIsolated"), "the page is not cross-origin isolated (serve.py sends COOP and COEP)"
    if args.fixes:
        page.select_option("#fixes-src", "file")
        page.set_input_files("#fixes-file", args.fixes)
        got = page.eval_on_selector("#fixes-src", "s => [s.value, s.selectedOptions[0].text]")
        assert got == ["file", "File: " + pathlib.Path(args.fixes).name], got
    page.click("label[for=src-f]")
    page.set_input_files("#file", args.pbf)
    page.select_option("#output", "gdal")
    page.select_option("#format", args.format)
    assert page.is_checked("#tiles") and page.is_disabled("#tiles") and not page.is_checked("#sort"), "GDAL layers: tile by tile, no sort"
    assert page.text_content("#run") == "Make the layers"
    if args.zoom:
        page.select_option("#zoom", args.zoom)
    elif monaco:
        page.select_option("#zoom", "")
        page.evaluate("document.getElementById('run').dataset.tileNodes = 2000")
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
    got = {k: page.text_content(f"#{k}") for k in ("nodes", "ways", "relations", "seconds", "threads", "mib", "size")}
    print(got)
    print("  ", page.text_content("#timeline-text"))
    steps = page.eval_on_selector_all("#steps tr", "rs => rs.map(r => [...r.cells].map(c => c.textContent).join(' | '))")
    print("   " + "\n   ".join(steps))
    layers = page.evaluate("(window.lastResult || {}).layers")
    print("   layers:", layers, "| most browser storage:", page.evaluate("(window.lastResult || {}).storageMib"), "MB | join memory:", page.evaluate("(window.lastResult || {}).joinMib"), "MiB")
    if monaco and not args.fixes:
        assert layers == MONACO, layers
    links = page.eval_on_selector_all("#layers a", "as => as.map(a => a.download)")
    base = pathlib.Path(args.pbf).name.removesuffix('.osm.pbf')
    want = [f"{base}_gdal_{l}.parquet" for l in LAYERS] if parquet else []
    assert links == want + ([f"{base}_gdal.gpkg"] if gpkg else []), links
    assert page.is_hidden("#duckdb") and page.is_hidden("#download")
    bad = 0
    if not args.no_download:
        import pyarrow.parquet as pq  # only to read the downloads (a computer with no pyarrow can run --no-download)
    for i, layer in enumerate(LAYERS if parquet and not args.no_download else []):
        with page.expect_download() as d:
            page.click(f"#layers a >> nth={i}")
        f = out / d.value.suggested_filename
        d.value.save_as(f)
        t = pq.read_table(f)
        assert t.num_rows == layers[layer], (layer, t.num_rows)
        if args.native:
            n = pq.read_table(pathlib.Path(args.native) / f"{args.name}_gdal_{layer}.parquet")
            assert t.column_names == n.column_names, (layer, t.column_names, n.column_names)
            rows = lambda x: sorted(map(tuple, zip(*[c.to_pylist() for c in x.columns])), key=repr)
            ours, native = rows(t), rows(n)
            same = ours == native
            print(f"   {layer}: {t.num_rows} rows, {'equal to' if same else 'NOT equal to'} the native file")
            bad += not same
        if not monaco and not args.keep:
            f.unlink()
    if gpkg and not args.no_download:
        with page.expect_download() as d:
            page.click("#layers a >> nth=-1")
        f = out / d.value.suggested_filename
        d.value.save_as(f)
        con = sqlite3.connect(f)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        assert ok == "ok", ok
        print(f"   GeoPackage: {f.stat().st_size} bytes, integrity_check {ok}")
        if shutil.which("ogrinfo"):
            info = subprocess.run(["ogrinfo", "-ro", "-so", "-al", str(f)], capture_output=True, text=True)
            assert info.returncode == 0, info.stderr
            print("   ogrinfo opens it:", ", ".join(l.split(": ")[1] for l in info.stdout.splitlines() if l.startswith("Feature Count")))
        nat = sqlite3.connect(pathlib.Path(args.native) / f"{args.name}_gdal.gpkg") if args.native else None
        for layer in LAYERS:
            n = con.execute(f'SELECT count(*) FROM "{layer}"').fetchone()[0]
            assert n == layers[layer] == con.execute(f'SELECT count(*) FROM "rtree_{layer}_geom"').fetchone()[0], (layer, n)
            if nat:
                cols = [c[1] for c in con.execute(f'PRAGMA table_info("{layer}")') if c[1] not in ("fid", "geom")]
                assert cols == [c[1] for c in nat.execute(f'PRAGMA table_info("{layer}")') if c[1] not in ("fid", "geom")], layer
                q = f'SELECT hex(geom), {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{layer}"'
                same = sorted(con.execute(q), key=repr) == sorted(nat.execute(q), key=repr)
                print(f"   gpkg {layer}: {n} rows, {'equal to' if same else 'NOT equal to'} the native GeoPackage")
                bad += not same
        con.close()
        if not monaco and not args.keep:
            f.unlink()
    assert bad == 0, f"{bad} layers differ from the native files"
    wide = page.evaluate("document.documentElement.scrollWidth - innerWidth")
    page.screenshot(path=str(out / "quack-gdal-phone.png"), full_page=True)
    assert wide <= 1, f"the page is {wide} px wider than the screen"
    assert not errs, errs
    b.close()
    server.shutdown()
    shutil.rmtree(profile, ignore_errors=True)
print("quack_gdal_e2e: passed")
