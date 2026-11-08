"""The cards of "What you can do" in README.md, a light and a dark set (the dark one is for a GitHub page in the dark theme, chosen by <picture> in README.md): a page picture of docs/img/dashboard/ with a title and a line of text under it, in a card with
rounded corners, a thin border and a soft shadow, on a transparent ground. A README cannot set a shadow or a card with CSS, so the card is a picture
(the alt text in README.md has the same words). Playwright draws each card.

    python crates/duckosm-wasm/www/readme_cards.py        (writes docs/img/cards/<name>.png and <name>-dark.png)
"""
import base64
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC, OUT = ROOT / "docs/img/dashboard", ROOT / "docs/img/cards"
CARDS = [
    ("06b-area-on-map", "1. Choose an area", "Search a place, draw a shape, load a file or pick H3 cells."),
    ("12-map-after-build", "2. Build", "Get the driving, walking and cycling networks, with the graph of legal turns. The build runs in your browser."),
    ("20-fix-turn", "3. Edit OSM data", "Fix a road, a building, a node or a turn rule. Build again to see the fix in the tables."),
    ("32-design-dark-map", "4. Edit levels and design", "Say which bridge or road is on top. Choose a theme, or set each color and width."),
    ("42-dock-chart", "5. Ask the data", "Query the tables with SQL, or ask a question in words with your own key of an AI service. The roads of the result light on the map."),
    ("11b-export", "6. Export", "PMTiles, MBTiles, GeoPackage with a QGIS style, GeoJSON, kepler.gl, MATSim and SUMO."),
]
HTML = """<!doctype html><meta charset=utf-8><style>
html,body{margin:0;background:transparent}
body{width:620px;padding:22px;box-sizing:border-box;font-family:Inter,'Segoe UI',Helvetica,Arial,sans-serif}
.card{background:#fff;border:1px solid #d0d7de;border-radius:14px;box-shadow:0 6px 18px rgba(15,23,42,.18);overflow:hidden}
img{display:block;width:100%;border-bottom:1px solid #e6e8ec}
.t{padding:14px 18px 16px;color:#16181d}.t b{display:block;font-size:20px;margin-bottom:6px}.t span{display:block;font-size:15px;line-height:1.45;color:#4b5563;height:66px}
body.dark .card{background:#151b23;border-color:#3d444d;box-shadow:0 0 0 1px rgba(240,246,252,.06),0 8px 24px rgba(240,246,252,.10)}
body.dark img{border-bottom-color:#3d444d}body.dark .t{color:#f0f6fc}body.dark .t span{color:#9198a1}
</style><div class=card id=c><img src="data:image/png;base64,@IMG@"><div class=t><b>@TITLE@</b><span>@TEXT@</span></div></div>"""

OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 620, "height": 600}, device_scale_factor=1.5)
    for name, title, text in CARDS:
        data = base64.b64encode((SRC / f"{name}.png").read_bytes()).decode()
        page.set_content(HTML.replace("@IMG@", data).replace("@TITLE@", title).replace("@TEXT@", text))
        for suffix in ("", "-dark"):
            page.evaluate(f"document.body.classList.toggle('dark', {str(bool(suffix)).lower()})")
            page.wait_for_timeout(150)
            page.locator("body").screenshot(path=str(OUT / f"{name}{suffix}.png"), omit_background=True)
            print("wrote", (OUT / f"{name}{suffix}.png").relative_to(ROOT))
    browser.close()
