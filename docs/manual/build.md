# Steps 3 and 4: Data and build

<p class="lead">In step 3, you choose the OSM data. In step 4, you build the network in your browser.</p>

## Step 3: Choose the data

![Card 3 with the Region file tab and the Geofabrik files that cover the area](../img/dashboard/07-data.png)

| Tab | Use it when |
|---|---|
| **Region file** | You have an `.osm.pbf` file of a country or a region, or you want one from Geofabrik. |
| **Overpass** | The area is small and you have no file. |
| **Cut file** | You have an `.osm.pbf` file that is already cut to the area. Press **Select a cut file (.osm.pbf)**. This file gives no layers. |

### Get a region file

The tab lists the [Geofabrik](https://download.geofabrik.de) files that cover your area.

1. Click a row to see the file on the map.
2. Press **Get** on a row. The page downloads the file and keeps it in your browser. A bar shows the progress. **Cancel** stops it.
3. On the next visit, press **Use** on a row that says **kept**.

The arrow button of a row saves the `.osm.pbf` file on your computer. **Select a region file (.osm.pbf)** at the top of the tab uses a file that you have. Under **Kept in this browser**, each kept file has a **Delete** button. If no Geofabrik file covers the area, use the Overpass tab.

### Use Overpass

![Card 3 with the Overpass tab](../img/dashboard/39-overpass.png)

The page gets the data from the [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API). A large area can fail, because the servers limit the answer.

## Step 4: Build

![Card 4: the networks, the layers, the fixes file and the Build button](../img/dashboard/08-build-options.png)

1. Select the **Networks**: driving, walking, cycling or more than one.
2. To get a base map, select **Layers**. Step 7 also needs the layers for walls and doors.
3. Optional: select a **Ground height (DEM)** file (`.tif`). It is a GeoTIFF in EPSG:4326 with one band, in metres. The build then also makes the height tables ([the tables](tables.md)). The page has no DEM of its own: you supply the file, and you know its vertical datum. With a DEM, the **3D terrain** and the **hillshade** of the map come from it, not from the AWS Terrain Tiles: the map's attribution then names your DEM.
4. Optional: select a **Fixes** file (`.yaml`). [The fixes file](fixes-file.md) gives its format.
5. Press **Build**.

When the build ends, the card shows the time and the memory:

![Card 4 after a build: the time, the memory and the details](../img/dashboard/09-build-done.png)

Open **Build details** or **Time and memory of each step** for more. A large area can fail on a small device. [The limits](../troubleshooting.md) have the numbers.

Each network gets its own `edges`, `nodes` and `edge_graph` tables. [The tables](tables.md) and [the roads](roads.md) explain them.

## Next

[Steps 5 and 6](result.md) show the result.
