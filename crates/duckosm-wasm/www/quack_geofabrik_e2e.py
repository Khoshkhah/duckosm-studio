"""quack.html with the output "Geofabrik layers" in headless Chromium (Playwright), at phone size (390 x 844): the 20
layers of Geofabrik's free files (duckOSM `docs/concepts/geofabrik.md`) of a .osm.pbf file, made tile by tile in the
browser, as a GeoPackage, a zip file of shapefiles and GeoParquet files (one run for each format). Each download must
have the rows of the native files in DIR of `duckosm quack FILE --geofabrik -f gpkg,shp,geoparquet -o DIR --name <name>`
(the same rows, in any order): the GeoPackage rows (geometry as hex, no fid), the shapefile records (the .dbf record and
the .shp shape of each row) and the GeoParquet rows. SQLite's integrity_check must be ok. On Monaco the page uses small
tiles (at most 2000 nodes). Build the page first (build.sh).

    python crates/duckosm-wasm/www/quack_geofabrik_e2e.py --native DIR [out folder] [--pbf file.osm.pbf] [--name N]
"""
import argparse
import collections
import io
import pathlib
import shutil
import sqlite3
import struct
import tempfile
import zipfile

from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

from serve import WWW, start

ROOT = pathlib.Path(__file__).resolve().parents[3]
a = argparse.ArgumentParser()
a.add_argument("out", nargs="?", default="out-quack-geofabrik")
a.add_argument("--pbf", default=str(ROOT / "tests/data/monaco.osm.pbf"))
a.add_argument("--native", required=True, help="the folder of the native files to compare with")
a.add_argument("--name", default="monaco", help="the --name of the native files")
args = a.parse_args()
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
native = pathlib.Path(args.native)
monaco = pathlib.Path(args.pbf).name == "monaco.osm.pbf"
base = pathlib.Path(args.pbf).name.removesuffix(".osm.pbf")


def gpkg_rows(path):
    """The rows of each table of a GeoPackage as a sorted list (geometry as hex, no fid)."""
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", path
    rows = {}
    for (t,) in con.execute("SELECT table_name FROM gpkg_contents ORDER BY rowid"):
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")') if c[1] not in ("fid", "geom")]
        q = f'SELECT hex(geom), {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{t}"'
        rows[t] = (cols, sorted(con.execute(q), key=repr))
    con.close()
    return rows


def shp_rows(data):
    """The records of each shapefile of a zip file: the counter of (the .dbf record, the .shp shape with no record number)."""
    z = zipfile.ZipFile(io.BytesIO(data))
    rows = {}
    for name in z.namelist():
        if not name.endswith(".shp"):
            continue
        stem = name[:-4]
        shp, dbf = z.read(name), z.read(stem + ".dbf")
        assert z.read(stem + ".cpg") == b"UTF-8\n" and z.read(stem + ".prj").startswith(b"GEOGCS"), stem
        n, head, size = struct.unpack_from("<IHH", dbf, 4)
        records = [dbf[head + i * size:head + (i + 1) * size] for i in range(n)]
        shapes, at = [], 100
        while at < len(shp):
            length = struct.unpack_from(">i", shp, at + 4)[0] * 2
            shapes.append(shp[at + 8:at + 8 + length])
            at += 8 + length
        assert len(shapes) == n, (stem, len(shapes), n)
        rows[stem] = (dbf[32:head], collections.Counter(zip(records, shapes)))
    return rows


server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/quack.html"
bad = 0
with sync_playwright() as p:
    profile = tempfile.mkdtemp(prefix="quack-geofabrik-e2e-")
    b = p.chromium.launch_persistent_context(profile, viewport={"width": 390, "height": 844},
                                             has_touch=True, is_mobile=True, accept_downloads=True)
    page = b.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(url)
    assert page.evaluate("crossOriginIsolated"), "the page is not cross-origin isolated (serve.py sends COOP and COEP)"
    page.click("label[for=src-f]")
    page.set_input_files("#file", args.pbf)
    page.select_option("#output", "geofabrik")
    options = page.eval_on_selector_all("#format option", "os => os.map(o => o.value)")
    assert options == ["gpkg", "shp", "geoparquet"], options
    assert page.is_checked("#tiles") and page.is_disabled("#tiles") and not page.is_checked("#sort"), "layers: tile by tile, no sort"
    assert page.text_content("#run") == "Make the layers"
    if monaco:
        page.select_option("#zoom", "")
        page.evaluate("document.getElementById('run').dataset.tileNodes = 2000")
    for fmt in ["gpkg", "shp", "geoparquet"]:
        page.select_option("#format", fmt)
        page.click("#run")
        for _ in range(240):
            try:
                page.wait_for_function("/Done|Error/.test(document.getElementById('status').textContent)", timeout=15000)
                break
            except PlaywrightTimeout:
                print("  ", page.text_content("#clock"), page.text_content("#status"), flush=True)
        else:
            raise AssertionError("no end after 60 minutes: " + page.text_content("#status"))
        status = page.text_content("#status")
        assert status == "Done.", status
        print(fmt, {k: page.text_content(f"#{k}") for k in ("nodes", "ways", "relations", "seconds", "mib", "size")})
        assert page.text_content("#command").startswith(f"duckosm quack {pathlib.Path(args.pbf).name} --geofabrik -o out -f {fmt}")
        layers = page.evaluate("(window.lastResult || {}).layers")
        assert len(layers) == 20, layers
        links = page.eval_on_selector_all("#layers a", "as => as.map(a => a.download)")
        want = {"gpkg": [f"{base}_geofabrik.gpkg"], "shp": [f"{base}_geofabrik.shp.zip"],
                "geoparquet": [f"{base}_geofabrik_{l}.parquet" for l in layers]}[fmt]
        assert links == want, links
        if fmt == "geoparquet":
            try:
                import pyarrow.parquet as pq
            except ImportError:
                pq = None
                print("   no pyarrow: the GeoParquet files are saved in", out, "but not compared")
            # the bytes of each link (20 downloads in a row: Chromium stops the downloads after some)
            get = "async (i) => { const a = document.querySelectorAll('#layers a')[i]; return [a.download, Array.from(new Uint8Array(await (await fetch(a.href)).arrayBuffer()))]; }"
            for i, layer in enumerate(layers):
                name, data = page.evaluate(get, i)
                f = out / name
                f.write_bytes(bytes(data))
                if pq is None:
                    continue
                t, n = pq.read_table(f), pq.read_table(native / f"{args.name}_geofabrik_{layer}.parquet")
                rows = lambda x: sorted(map(tuple, zip(*[c.to_pylist() for c in x.columns])), key=repr)
                same = t.num_rows == layers[layer] and t.column_names == n.column_names and rows(t) == rows(n)
                bad += not same
                if not same:
                    print(f"   {layer}: NOT equal to the native file")
            if pq:
                print(f"   GeoParquet: 20 files, {'equal' if not bad else 'NOT equal'} to the native files")
            continue
        with page.expect_download() as d:
            page.click("#layers a >> nth=0")
        f = out / d.value.suggested_filename
        d.value.save_as(f)
        if fmt == "gpkg":
            ours, nat = gpkg_rows(f), gpkg_rows(native / f"{args.name}_geofabrik.gpkg")
            assert {t: len(r[1]) for t, r in ours.items()} == layers, "the counts of the page"
        else:
            ours, nat = shp_rows(f.read_bytes()), shp_rows((native / f"{args.name}_geofabrik.shp.zip").read_bytes())
            assert sum(sum(r[1].values()) for r in ours.values()) == sum(layers.values()), "the counts of the page"
        diff = [t for t in nat if ours.get(t) != nat[t]]
        print(f"   {fmt}: {f.stat().st_size} bytes, {len(ours)} layers, {'equal to' if not diff else 'NOT equal to'} the native file {diff}")
        bad += len(diff) + (set(ours) != set(nat))
    wide = page.evaluate("document.documentElement.scrollWidth - innerWidth")
    page.screenshot(path=str(out / "quack-geofabrik-phone.png"), full_page=True)
    assert wide <= 1, f"the page is {wide} px wider than the screen"
    assert not errs, errs
    b.close()
    server.shutdown()
    shutil.rmtree(profile, ignore_errors=True)
assert bad == 0, f"{bad} outputs differ from the native files"
print("quack_geofabrik_e2e: passed")
