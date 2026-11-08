<p align="center">
  <img src="docs/img/banner.png" alt="duckOSM Studio: build the road network of any area from OpenStreetMap in your browser. Built with duckOSM and roadstyle." width="100%">
</p>

<p align="center">
  <b>Build the road network of any area from OpenStreetMap in your browser.</b><br>
  Fix the OSM data, style the map and export the result. You install nothing.
</p>

<p align="center">
  <a href="https://github.com/Khoshkhah/duckosm-studio/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Khoshkhah/duckosm-studio/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/Khoshkhah/duckosm-studio/actions/workflows/pages.yml"><img alt="Pages" src="https://github.com/Khoshkhah/duckosm-studio/actions/workflows/pages.yml/badge.svg?branch=main"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/github/license/Khoshkhah/duckosm-studio"></a>
</p>

<p align="center">
  <a href="https://khoshkhah.github.io/duckosm-studio/"><b>Open duckOSM Studio</b></a> ·
  <a href="https://khoshkhah.github.io/duckosm-studio/manual/"><b>Read the manual</b></a> ·
  <a href="https://khoshkhah.github.io/duckosm-studio/manual/first_map/">Make your first map</a>
</p>

### What you can do

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/cards/06b-area-on-map-dark.png">
    <img src="docs/img/cards/06b-area-on-map.png" alt="1. Choose an area. Search a place, draw a shape, load a file or pick H3 cells." width="49%">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/cards/12-map-after-build-dark.png">
    <img src="docs/img/cards/12-map-after-build.png" alt="2. Build. Get the driving, walking and cycling networks, with the graph of legal turns. The build runs in your browser." width="49%">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/cards/20-fix-turn-dark.png">
    <img src="docs/img/cards/20-fix-turn.png" alt="3. Edit OSM data. Fix a road, a building, a node or a turn rule. Build again to see the fix in the tables." width="49%">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/cards/32-design-dark-map-dark.png">
    <img src="docs/img/cards/32-design-dark-map.png" alt="4. Edit levels and design. Say which bridge or road is on top. Choose a theme, or set each color and width." width="49%">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/cards/42-dock-chart-dark.png">
    <img src="docs/img/cards/42-dock-chart.png" alt="5. Ask the data. Query the tables with SQL, or ask a question in words with your own key of an AI service. The roads of the result light on the map." width="49%">
  </picture>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/cards/11b-export-dark.png">
    <img src="docs/img/cards/11b-export.png" alt="6. Export. PMTiles, MBTiles, GeoPackage with a QGIS style, GeoJSON, kepler.gl, MATSim and SUMO." width="49%">
  </picture>
</p>

The result has the same tables as [duckOSM](https://github.com/Khoshkhah/duckOSM). Each road has a stable `edge_id`: a hash of the OSM way and its two end nodes, so the same road has the same `edge_id` in each build.

### Built with

<table>
  <tr>
    <td width="72" valign="middle"><a href="https://github.com/Khoshkhah/duckOSM"><img src="docs/img/logos/duckosm-mark.svg" alt="duckOSM logo" width="64"></a></td>
    <td width="42%" valign="middle"><b><a href="https://github.com/Khoshkhah/duckOSM">duckOSM</a></b><br>The Rust engine that builds the networks from OpenStreetMap data, and the fixes editor of card 7.</td>
    <td width="72" valign="middle"><a href="https://github.com/Khoshkhah/roadstyle"><img src="docs/img/logos/roadstyle-logo.svg" alt="roadstyle logo" width="64"></a></td>
    <td width="42%" valign="middle"><b><a href="https://github.com/Khoshkhah/roadstyle">roadstyle</a></b><br>The map and the dashboard: the panel, the bar of tools, the level editor, the design panel, the data dock and Ask.</td>
  </tr>
</table>

Studio has no engine and no map code of its own. It uses the two projects as libraries, each at one pinned version: the duckOSM `rev` in `crates/duckosm-wasm/Cargo.toml` and `ROADSTYLE_COMMIT` in `crates/duckosm-wasm/build.sh`. To get a change from one of them, merge it there and move the pin here.

Other parts: [MapLibre GL JS](https://maplibre.org/) draws the map, [DuckDB](https://duckdb.org/) (duckdb-wasm) holds the tables in the browser, and [OpenFreeMap](https://openfreemap.org/) gives the base map. The data is © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) (ODbL).

### Run it yourself

You need Rust, `wasm-bindgen-cli` and the nightly toolchain that [docs/development.md](docs/development.md) names.

```bash
CARGO_BUILD_JOBS=4 ./crates/duckosm-wasm/build.sh
python3 crates/duckosm-wasm/www/serve.py 8000
```

Open <http://localhost:8000/index.html>. `serve.py` sends the headers that WebAssembly threads need.

### How it is built

- The page is static: `index.html` and ES modules in `crates/duckosm-wasm/www/`. It needs no server.
- The Rust engine of duckOSM is compiled to WebAssembly (`crates/duckosm-wasm`). Each row of each table is equal to the duckOSM output, and a test checks it.
- DuckDB in the browser holds the tables. The page saves each build in the browser, so **Your areas** opens it again.
- roadstyle makes the vector tiles in a worker, and MapLibre draws them.
- GitHub Pages deploys the page and the [manual](https://khoshkhah.github.io/duckosm-studio/manual/) from `main`.

[docs/development.md](docs/development.md) tells how to build and test it.

### Documents

- [The manual](https://khoshkhah.github.io/duckosm-studio/manual/): every step, every editor, every export and every table.
- [Development](docs/development.md)
- [Changelog](CHANGELOG.md)

### License

[MIT](LICENSE)
