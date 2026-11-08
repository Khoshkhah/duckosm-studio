# To do: suggested work

Work that is suggested but not started. Each item says why, and where its notes are.

## The time zone in the browser

The page finds the time zone of an area with a table of the Geofabrik regions (`www/timezones.json`, 447 KB with
gzip; [tables.md](tables.md)). Kaveh chose it on 2026-10-23. Two other ways, for later:

- **A small time zone service of our own on Cloudflare** (`…/tz?lat=…&lon=…`, beside the relay). The page sends
  the middle point of the area and gets the zone name. The page downloads nothing, and the name is exact (the
  world map of `tzf-rs`, 2.7 MB with gzip, fits in a Worker of the free plan, which allows 3 MB). The cost: the
  build needs the network for this request.
- **The JavaScript package `@photostructure/tz-lookup`** (73 KB, CC0). Small and with no service, but by its own
  README about 30 % of random points get another zone name than the exact library, and about 5 % another clock.
  Good only if the name may differ from duckOSM's.

## The page and its map

- **A "Style" choice in step 5**: roadstyle's and mapstyle's 64 looks (mode, colors, paths) for the roads and the
  base map, as in the GeoLibre plugin.
- **Faster maps for a large area**: done in part (2026-10-23). The roads go to MapLibre as vector tiles that Rust
  makes when the map asks for them: Tartu 15.7 s to 7.4 s, and the page's memory 803 to 104 MiB
  ([browser.md](browser.md)). Since roadstyle-rs `900b43e` the sources `roads` and `simple` are typed pieces
  (Tartu: the module's memory 1,387 to 878 MiB). Still to do: the limit of 100,000 roads stays. Making the tiles' features without the trees
  would lift it. The work is in geolibre-roadstyle (`wasm/`).
- **The sea (`features.ocean`)**: not in the page for now (Kaveh, 2026-10-23). duckdb-wasm cannot read Overture's
  sea files in parts (27 GB in total). Two ways for later: read only the needed part of Overture's files in the page
  (about 20 MB the first time, the same sea as duckOSM), or make the sea from the coastline ways of the region file
  (no download, but no sea for Monaco, whose file has no coastline) ([features.md](features.md)).

- **The level editor (card 8)**: done. Since 2026-11 it is roadstyle's level editor, and the page keeps only the data adapter ([app.md](app.md), "Step 8: edit the levels"). These notes are from 2026-10-24 to 2026-10-25 and are history:
  - The editor's place in the page's side panel (now on the right of the map). Done at roadstyle `8208486`: the
    duckOSM base-map layers stay while the editor is open, and the panel follows the page's theme.
  - `makeArea` runs in the main thread: a large area (Tartu) can stop the page while the level area is made. The
    editor's own engine is in a Web Worker since roadstyle `7d4534f`.
  - The page does not know the version of roadstyle's module, so `edge_levels_meta.roadstyle_version` keeps the build's.
  - `duckosm-core` takes its levels from roadstyle `aba5884`; the map and the editor from `083224c` ([levels.md](levels.md)).

## The README and the manual

- **Pictures and the live page in the light and the dark theme** (Kaveh, 2026-11-08). Today the README and the manual show
  one picture for each page, in one theme (the six README cards and the banner have a light and a dark file; the 45 pictures
  of the manual do not). To do:
  - Make each picture of the manual twice, in the light and in the dark theme (`docs_shots.py` and `cards_shots` make
    the pictures; set the page's theme before each shot).
  - Show the picture that fits the reader's theme: `<picture>` with `prefers-color-scheme` in the README, and the
    `#only-light` and `#only-dark` marks of mkdocs-material in the manual.
  - The live page (the links and any frame of it in the README and the manual): check if a link can open the page in a
    chosen theme. If it cannot, ask for a link parameter in the page (roadstyle's `rsSetTheme`), and use it in the links.
  - Check the new pictures with `mkdocs build --strict`, and on github.com in both themes.

## The tables

- **Group 2** ([tables.md](tables.md)) is done: `driving.lane_profile` ([lane_profile.md](lane_profile.md)) and the
  bus routes ([bus.md](bus.md)).

## Publishing

- Done: the page is on GitHub Pages at <https://khoshkhah.github.io/duckosm-studio/>, from this repository
  (`.github/workflows/pages.yml`, on each push to `main`). A page repository `duckosm-web` is not necessary: the
  repository is public since 2026-10-24. The relay allows `https://khoshkhah.github.io` ([relay.md](relay.md)).
- The repository secret `GOOGLE_MAPS_KEY` (Kaveh adds it): without it, the page asks the user for a key.
