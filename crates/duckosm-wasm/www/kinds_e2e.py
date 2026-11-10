"""The table views of duckOSM in the real page (roadstyle's data dock option `kinds`, duckOSM's web/ask/views.js), with the real DuckDB-wasm of the page, on a computer:
a row of a table that is not a road is an object on the map with a card. The page has no table code of its own, so this test checks what the two libraries do with the page's tables.

  - `SELECT * FROM driving.edge_graph`: a turn is 3 parts on the map (from, via, to), and its card has the cost and the section "Row";
  - a result without the column `uses` (a file of an older shape) does not break the dock;
  - `driving.turn_restrictions`, `driving.nodes` (a node), `driving.lane_profile` (the card "Row" has width_m), `features.buildings` (a building);
  - a result of H3 cells (a text and a 64-bit number) is hexagons, and the card of a cell has its resolution, its parent and its area;
  - `refreshKinds()` after a build keeps the objects and the card of the same kind;
  - `SELECT use FROM driving.lane_profile` passes the guard of the dock (a column named `use`);
  - Ask turns layers off (a fake AI service answers), and the Reset of the dock shows them again.

    python crates/duckosm-wasm/www/kinds_e2e.py [out folder]

It uses tests/data/monaco_sample.osm.pbf and tests/data/monaco.geojson, and needs the network (h3-js comes from jsdelivr). Build the page first (docs/development.md, "WebAssembly").
"""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

from serve import WWW, start

ROOT = pathlib.Path(__file__).resolve().parents[3]
out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "out-kinds")
out.mkdir(parents=True, exist_ok=True)
server = start(0, quiet=True, directory=WWW)
url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
errors = []
problems = []

# the features that the dock drew for the result (roadstyle's source rs-dock-kinds), and the text of the card
FEATURES = "(() => { const s = window.duckMap.getSource('rs-dock-kinds'); return s ? s.serialize().data.features.map((f) => ({id: f.properties.rs_id, role: f.properties.rs_role ?? null, type: f.geometry.type})) : []; })()"
CARD = "(document.querySelector('.rs-dd-card') || {innerText: ''}).innerText"


def run(page, sql, expect_kind=True):
    """Run a query in the dock and wait for its table. With expect_kind, wait for the objects on the map."""
    page.evaluate("(sql) => window.dataPart.run(sql)", sql)
    page.wait_for_function("(sql) => window.dataPart.result() && window.dataPart.result().sql.trim() === sql.trim()", arg=sql, timeout=120_000)
    if expect_kind:
        try:
            page.wait_for_function(f"{FEATURES}.length > 0", timeout=20_000)
        except Exception:
            problems.append(("no object on the map", sql, page.inner_text("#rs-dock")[:400]))
            return []
    note = page.inner_text("#rs-dock")
    if "the kind" in note.lower() and "failed" in note.lower():      # a kind that fails is a fault of the SQL of a library: the test goes on and fails at the end
        problems.append(("a kind failed", sql, note[note.lower().index("the kind"):][:300]))
    return page.evaluate(FEATURES)


def pick(page, index=0):
    page.locator(".rs-dock-id").nth(index).click()
    try:
        page.wait_for_selector(".rs-dd-rc-hd", timeout=20_000)
    except Exception:
        raise AssertionError(("no card", page.evaluate("[...document.querySelectorAll('[class*=rs-dd-]')].map((e) => e.className).filter((c, i, a) => a.indexOf(c) === i).join(' ; ')"), errors))
    page.wait_for_function("!/Loading/.test(document.querySelector('.rs-dd-card').innerText)", timeout=30_000)
    return page.evaluate(CARD)


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
    page.on("response", lambda r: errors.append(f"{r.status} {r.url}") if r.status >= 400 and "favicon" not in r.url and "local_keys" not in r.url else None)
    page.goto(url)
    page.wait_for_function("window.mapReady", timeout=120_000)
    page.set_input_files("#file", str(ROOT / "tests/data/monaco.geojson"))
    page.wait_for_function("window.areas >= 1", timeout=60_000)
    page.set_input_files("#region", str(ROOT / "tests/data/monaco_sample.osm.pbf"))
    page.click("#run")
    page.wait_for_function("!document.getElementById('step5-card').classList.contains('off') && !document.getElementById('run').disabled", timeout=900_000)
    page.wait_for_function("/drawn|failed/.test(document.getElementById('mapnote').textContent) && !window.mapDrawing", timeout=600_000)
    page.evaluate("rs.sections.fold('step5', false)")
    page.click("#query-tables")
    try:
        page.wait_for_function("document.documentElement.classList.contains('rs-dock-has')", timeout=20_000)
    except Exception:
        raise AssertionError(("the dock did not open", errors))
    names = page.evaluate("window.dataPart.schema().then((s) => s.tables.map((t) => t.name))")
    print("tables:", len(names), [n for n in names if "turn" in n or "lane" in n or "boundary" in n or "building" in n])
    about = page.evaluate("window.askAbout")      # the text for the agent (schema() made it): it must tell that results of ids are drawn
    assert "node_id" in about and "h3_cell" in about and "building_faces" in about and "highway" in about and "only show roads" in about, about[-600:]
    assert page.evaluate("window.RS_DOCK && !!window.RS_DOCK.result && !!window.RS_DOCK.roles.from"), "the options of the dashboard carry the look's dock section"

    # a turn: 3 parts on the map, and the card has the cost and the section "Row"
    feats = run(page, "SELECT * FROM driving.edge_graph LIMIT 20")
    roles = sorted({f["role"] for f in feats})
    ids = {f["id"] for f in feats}
    assert roles == ["from", "to", "via"] or roles == ["from", "to", "via"][::-1] or set(roles) == {"from", "via", "to"}, ("a turn has the parts from, via and to", roles)
    assert len(feats) == 3 * len(ids), ("3 parts for each turn", len(feats), len(ids))
    assert "Shown as Turn" in page.inner_text("#rs-dock"), "the note names the kind"
    card = pick(page)
    assert "cost" in card.lower() and "row" in card.lower() and "from" in card.lower(), ("the card of a turn", card)
    cols = page.evaluate("window.dataPart.result().columns.map((c) => c.name)")
    row = page.evaluate("window.dataPart.result().rows[0]")
    # the facts of the card are those of the clicked row (the same pair is in the driving, walking and cycling networks with other costs)
    assert f"{row[cols.index('cost')]:.1f}" in card and str(row[cols.index("uses")]) in card and "walk" not in card, (row, card)
    # the kinds are a function (roadstyle PR 134): after a build the page calls refreshKinds(), and when the same kind still wins nothing is drawn again, so the objects and the card stay
    before = page.evaluate(FEATURES)
    assert page.evaluate("window.dataPart.refreshKinds().then(() => true)") is True
    after, card_after = page.evaluate(FEATURES), page.evaluate(CARD)
    assert after == before and card_after == card, ("refreshKinds with the same kinds redraws nothing", len(before), len(after), card[:200], card_after[:200])
    page.screenshot(path=str(out / "1-turn.png"))
    print("turn:", len(ids), "turns,", len(feats), "parts; the card has", card.replace("\n", " | ")[:200])

    # a file of an older shape: the result has no `uses` column
    feats = run(page, "SELECT from_edge, to_edge, cost FROM driving.edge_graph LIMIT 5")
    assert len(feats) >= 3, "a result without the column uses still gives turns"
    card = pick(page)
    assert "cost" in card.lower(), card

    # the turn restrictions (the table can be empty in a small file)
    n = page.evaluate("window.duckApi.query('SELECT count(*) AS n FROM driving.turn_restrictions').then((r) => Number(r.toArray()[0].n))")
    if n:
        feats = run(page, "SELECT * FROM driving.turn_restrictions LIMIT 10")
        assert {"from", "to"} <= {f["role"] for f in feats}, ("a restriction is a turn", feats[:3])
        print("turn_restrictions:", n, "rows;", len(feats), "parts")
    else:
        print("turn_restrictions: no rows in this file; not checked")

    # a node: a point, with its card. The browser build keeps no h3_cell in the nodes (the Rust engine makes no H3 columns), so the card has no "Show H3 cell" here.
    feats = run(page, "SELECT node_id FROM driving.nodes LIMIT 10")
    if feats:
        assert all(f["type"] == "Point" for f in feats) and len(feats) >= 5, ("nodes are points", feats[:3])
        card = pick(page)
        assert "node" in card.lower() and "row" in card.lower(), ("the card of a node", card)
        assert page.locator(".rs-dd-card button", has_text="Show H3 cell").count() == 0, "no h3_cell in the nodes of this build, so no button"
        print("node:", len(feats), "points; the card has", card.replace("\n", " | ")[:120])

    # the nodes of the ways: a node of a way is mostly not a junction, so the point comes from raw.nodes when the build keeps it (it does not): no fault, whatever the objects
    if "main.way_nodes" in names or any(n.endswith(".way_nodes") for n in names):
        n = next(n for n in names if n.endswith(".way_nodes"))
        run(page, f"SELECT * FROM {n} LIMIT 10", expect_kind=False)
        print("way_nodes: ran with no kind fault")

    # H3 cells of a result, as a hex text and as a BIGINT (the page has no H3 table and DuckDB no H3 function, so the cells come from h3-js here): hexagons, and a card with
    # the resolution, the parent, the area (the count of nodes inside needs nodes.h3_cell, so it is not there in this build)
    cells = page.evaluate("import('h3-js').then((h) => [h.latLngToCell(43.7384, 7.4246, 8), h.latLngToCell(43.7384, 7.4246, 9), h.latLngToCell(43.74, 7.43, 8)])")
    values = ", ".join(f"('{c}')" for c in cells[:2]) + f", ('{cells[2]}')"
    feats = run(page, f"SELECT * FROM (VALUES {values}) AS t(h3_cell)")
    assert len(feats) == len(set(cells)) and all(f["type"] == "Polygon" for f in feats), ("H3 cells are hexagons", feats)
    card = pick(page)
    for word in ("resolution", "parent cell", "area", "row"):
        assert word in card.lower(), (word, card)
    big = int(cells[0], 16)
    feats = run(page, f"SELECT {big}::UBIGINT AS h3_cell")
    assert len(feats) == 1 and feats[0]["type"] == "Polygon", ("a BIGINT cell keeps all its 64 bits", feats)
    page.screenshot(path=str(out / "2-h3.png"))
    print("h3: a text and a BIGINT cell are hexagons; the card has", card.replace("\n", " | ")[:200])

    # lane_profile: a road row; the card "This row" has width_m. A column named `use` passes the guard of the dock.
    if "driving.lane_profile" in names:
        page.evaluate("window.dataPart.run('SELECT * FROM driving.lane_profile LIMIT 5')")
        page.wait_for_function("window.dataPart.result() && window.dataPart.result().sql.includes('lane_profile')", timeout=60_000)
        page.wait_for_function("document.querySelector('.rs-dock-id')", timeout=30_000)
        card = pick(page)
        assert "width" in card.lower(), ("the card of a lane_profile row has width_m", card)
        page.evaluate("window.dataPart.run('SELECT use FROM driving.lane_profile LIMIT 5')")
        page.wait_for_function("window.dataPart.result() && window.dataPart.result().columns[0].name === 'use'", timeout=60_000)
        print("lane_profile: the card has the row (width); a column named use passes the guard")
    else:
        print("lane_profile: not in this file; not checked")

    # a building
    if "features.buildings" in names:
        feats = run(page, "SELECT osm_id, name FROM features.buildings LIMIT 10")
        assert all(f["type"] in ("Polygon", "MultiPolygon") for f in feats), ("buildings are areas", feats[:3])
        print("buildings:", len(feats), "shapes")

    # Ask turns layers off (a fake AI service), and Reset of the dock shows them again
    layers = page.evaluate("Object.keys(rs.getState().overlays || {})")
    print("layers:", layers)
    if len(layers) >= 2:
        two = layers[:2]
        calls = []

        def groq(route):
            if route.request.url.endswith("/models"):
                return route.fulfill(status=200, content_type="application/json", body=json.dumps({"data": [{"id": "llama-3.3-70b-versatile"}]}))
            calls.append(1)
            if len(calls) == 1:
                body = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "set_map", "arguments": json.dumps({"overlays": {two[0]: False, two[1]: False}})}}]}}]}
            else:
                body = {"choices": [{"message": {"role": "assistant", "content": "Done."}}]}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        page.route("https://api.groq.com/**", groq)
        page.evaluate("localStorage.setItem('rs-key:ask:groq', 'test-key'); localStorage.setItem('rs-settings:ask', JSON.stringify({provider: 'groq', model: {groq: 'llama-3.3-70b-versatile'}}))")
        page.evaluate("window.askPart.open()")
        page.evaluate("window.askPart.ask('Turn off the water and the waterways layers')")
        page.wait_for_function(f"rs.getState().overlays[{json.dumps(two[0])}].visible === false", timeout=60_000)
        assert page.evaluate(f"rs.getState().overlays[{json.dumps(two[1])}].visible") is False
        page.click('.rs-dd-ib[title^="Reset"]')
        page.wait_for_function(f"rs.getState().overlays[{json.dumps(two[0])}].visible !== false && rs.getState().overlays[{json.dumps(two[1])}].visible !== false", timeout=30_000)
        print(f"Ask turned {two} off, and Reset of the dock shows them again")

    # the phone: a turn with its card does not make the page wider than 390 px
    page.set_viewport_size({"width": 390, "height": 844})
    run(page, "SELECT * FROM driving.edge_graph LIMIT 5")
    pick(page)
    assert page.evaluate("document.documentElement.scrollWidth") <= 390, "the page with a card of a turn is wider than the phone"
    page.screenshot(path=str(out / "3-phone-turn.png"))

    browser.close()

assert not errors, errors
assert not problems, problems
print("kinds: ok")
