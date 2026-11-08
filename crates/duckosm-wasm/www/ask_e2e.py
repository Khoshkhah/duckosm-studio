"""Ask and the data dock (roadstyle's dataDock and askPanel) in headless Chromium (Playwright), on a computer and on a phone (390 x 844, touch):
"Query the tables" (card 5) opens the dock with the start query, and Run gives its rows; a query of road ids gives roads on the map; the card "Ask the data"
opens Ask with the key note and the example questions, and the head row and the body of Ask start at the same left edge (Kaveh: they did not).
Nothing is wider than the phone. No AI service and no key: the test does not ask a question.

    python crates/duckosm-wasm/www/ask_e2e.py [out folder]

It uses tests/data/monaco_sample.osm.pbf and tests/data/monaco.geojson. Build the page first (docs/development.md, "WebAssembly").
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

from serve import WWW, start

ROOT = pathlib.Path(__file__).resolve().parents[3]
out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "out-ask")
out.mkdir(parents=True, exist_ok=True)
server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
errors = []
EDGE = """(sel) => { const e = [...document.querySelectorAll(sel)].find((x) => x.offsetParent); if (!e) return null; const r = e.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right)]; }"""


def run(browser, name, **context):
    phone = "has_touch" in context
    page = browser.new_context(**context).new_page()
    page.on("pageerror", lambda e: errors.append(f"{name}: {e}"))
    page.goto(url)
    page.wait_for_function("window.mapReady", timeout=120_000)
    tap = page.tap if phone else page.click
    page.set_input_files("#file", str(ROOT / "tests/data/monaco.geojson"))
    page.wait_for_function("window.areas >= 1", timeout=60_000)
    page.set_input_files("#region", str(ROOT / "tests/data/monaco_sample.osm.pbf"))
    tap("#run")
    page.wait_for_function("!document.getElementById('step5-card').classList.contains('off') && !document.getElementById('run').disabled", timeout=900_000)

    page.wait_for_function("/drawn|failed/.test(document.getElementById('mapnote').textContent) && !window.mapDrawing", timeout=600_000)   # the roads on the map
    # the dock: the start query runs on the tables of the build
    # no dock until Ask or Query the tables opens it (Kaveh, 2026-11-06: no SQL panel outside Ask)
    assert not page.evaluate("!!document.getElementById('rs-dock') || document.documentElement.classList.contains('rs-dock-has')"), f"{name}: a dock before its first use"
    page.evaluate("rs.sections.fold('step5', false)")
    tap("#query-tables")
    page.wait_for_function("document.documentElement.classList.contains('rs-dock-has')", timeout=10_000)
    page.evaluate("window.dataPart.run()")                             # the start text of the SQL line; the person presses Run
    page.wait_for_function("window.dataPart.result() && window.dataPart.result().rows.length > 0", timeout=120_000)
    r = page.evaluate("(() => { const r = window.dataPart.result(); return {cols: r.columns.map((c) => c.name), rows: r.rows.length}; })()")
    assert r["cols"][:3] == ["highway", "edges", "km"] and r["rows"] > 5, ("the start query", r)
    # a DECIMAL column (a sum or a count cast to DECIMAL) is a number in the table and for a chart, not a quoted text ("\"919\"", Kaveh 2026-11-08)
    page.evaluate("window.dataPart.run(\"SELECT 919::DECIMAL(38,0) AS a, 3.5::DECIMAL(10,2) AS b, (-3.5)::DECIMAL(10,2) AS c, 7::BIGINT AS d\")")
    page.wait_for_function("window.dataPart.result() && window.dataPart.result().columns[0].name === 'a'", timeout=30_000)
    cells = page.evaluate("window.dataPart.result().rows[0]")
    assert cells[:3] == [919, 3.5, -3.5] and int(cells[3]) == 7, ("DECIMAL cells are numbers", cells)
    page.evaluate("window.dataPart.run(\"SELECT highway, count(*) AS edges, round(sum(length_m) / 1000, 1) AS km FROM driving.edges GROUP BY highway ORDER BY edges DESC\")")   # the start query again: run() with no text runs the SQL in the box
    page.wait_for_function("window.dataPart.result() && window.dataPart.result().rows.length > 1", timeout=60_000)
    ids = page.evaluate("window.dataPart.roads(\"SELECT edge_id FROM driving.edges WHERE highway = 'primary'\").then((x) => x.length)")
    assert ids > 0, ("a query of road ids gives roads on the map", ids)
    # Ask and the dock see the base-map tables (duckOSM's askTables: every table of the file but raw.*): the traffic signals are in features.traffic. The main table is the driving edges,
    # with its columns (the agent reads them first), and a table has the note of duckOSM (TABLE_NOTES)
    sch = page.evaluate("window.dataPart.schema().then((s) => s.tables.map((t) => ({name: t.name, main: !!t.main, cols: (t.columns || []).map((c) => c.name), about: t.about || ''})))")
    names = [t["name"] for t in sch]
    assert "features.traffic" in names and "driving.edges" in names and not any(n.startswith("raw.") for n in names), ("Ask sees the base-map tables, not raw.*", names)
    main = [t for t in sch if t["main"]]
    assert len(main) == 1 and main[0]["name"] == "driving.edges" and "highway" in main[0]["cols"], ("the main table is the driving edges, with its columns", main)
    assert next(t for t in sch if t["name"] == "driving.edges")["about"], "a table has the note of duckOSM"
    n_sig = page.evaluate("window.duckApi.query(\"SELECT count(*) AS n FROM features.traffic WHERE kind = 'traffic_signals'\").then((r) => Number(r.toArray()[0].n))")
    assert n_sig >= 0, n_sig
    print(f"ask: the schema has {len(names)} tables, features.traffic included ({n_sig} traffic signals); the main table is {main[0]['name']}")
    page.screenshot(path=str(out / f"{name}-1-dock.png"))
    if phone:
        assert page.evaluate("document.documentElement.scrollWidth") <= 390, "the page with the dock is wider than the phone"

    # Ask: the key note and the examples, and one left edge for the head row and the body
    page.evaluate("window.askPart.open()")
    page.wait_for_function("window.askPart.isOpen()", timeout=10_000)
    page.locator(".rs-ask .rs-ask-q").first.wait_for(state="visible")
    assert "key" in page.inner_text(".rs-ask-need").lower(), "Ask says that it needs a key"
    # the example questions: roadstyle's six general groups first, then the groups of duckOSM's file (web/ask/questions.js, copied by build.sh); the page has no copy of its own
    want = page.evaluate("import('./duckosm/ask/questions.js').then((m) => m.QUESTIONS.map((g) => [g.title, g.items.length]))")
    heads = page.evaluate("[...document.querySelectorAll('.rs-ask-ex .rs-ask-grp')].map((e) => e.textContent)")
    assert len(heads) == len(want) + 6 and heads[6:] == [t for t, _ in want], ("the groups of duckOSM's file come after roadstyle's six general groups", heads)
    assert page.locator(".rs-ask-ex .rs-ask-q").count() >= sum(n for _, n in want), "every question of the file is a chip"
    assert any("chart" in t.lower() for t in page.locator(".rs-ask-ex .rs-ask-q").all_inner_texts()), "the chart questions show"
    # a chart in the Ask card is a chart: the page's icon rule (once `#sb svg`) made every svg of the panel 18 px wide and outlined the bars of a chart (width of the chart svg)
    w = page.evaluate("""() => { const host = document.querySelector('#sb .rs-ask .rs-ask-ex') || document.querySelector('#sb .rs-ask'); const box = document.createElement('div'); box.className = 'rs-ask-chart'; host.parentElement.insertBefore(box, host);
        window.rs.chart(box, {kind: 'bar', x: 'highway', y: 'km', columns: [{name: 'highway', type: 'VARCHAR'}, {name: 'km', type: 'DOUBLE'}], rows: [['residential', 30.5], ['primary', 12.5], ['service', 8.1]], small: true, onPick: null});
        const svg = box.querySelector('svg'), r = svg.getBoundingClientRect(); box.remove(); return Math.round(r.width); }""")
    assert w > 100, ("the chart svg in the Ask card is an 18 px icon", w)
    head, body = page.evaluate(EDGE, ".rs-ask-who"), page.evaluate(EDGE, ".rs-ask-hint, .rs-ask .rs-ask-q")
    assert head and body and abs(head[0] - body[0]) <= 1, ("the head row and the body of Ask start at the same left edge", head, body)
    page.screenshot(path=str(out / f"{name}-2-ask.png"))
    if phone:
        assert page.evaluate("document.documentElement.scrollWidth") <= 390, "the page with Ask is wider than the phone"
    page.context.close()
    print(f"ask ({name}): the dock runs the start query ({r['rows']} rows) and finds {ids} roads; Ask has the key note and the examples, one left edge")


with sync_playwright() as p:
    browser = p.chromium.launch()
    run(browser, "desktop", viewport={"width": 1400, "height": 900})
    run(browser, "phone", viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True, device_scale_factor=2)
    browser.close()
server.shutdown()
assert not errors, errors
print("ask: the data dock and Ask on a computer and on a phone; no page errors")
