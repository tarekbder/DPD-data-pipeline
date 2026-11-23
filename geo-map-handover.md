# Explore API: map points (`geo`), UI handover

**Applies to:** `POST /v1/explore` (v1). New optional field, no breaking change. Everything else in `pipelines-api-contract.md` is unchanged.

## What's new

The explore response has a new top-level key, `geo`. When the dataset has GPS coordinates, it holds a ready-to-plot list of points. When it doesn't, it is `null`.

The pipeline does all the detection, cleaning and sampling. The UI only renders what it receives.

## Response shape

```json
{
  "meta": { "...": "..." },
  "head": [ "..." ],
  "geo": {
    "source": "columns",
    "lat_column": "latitude",
    "lon_column": "longitude",
    "points": [
      [27.7240967, 85.3390033, 0, 412],
      [27.7238100, 85.3401200, 67, 3],
      [27.7102550, 85.3189870, 134, 58]
    ],
    "point_count": 5000,
    "total_valid": 337020,
    "sampled": true,
    "cell_deg": 0.0021,
    "regions": {
      "items": [
        { "city": "Kathmandu", "admin1": "Central Region", "country_code": "NP", "count": 276356, "pct": 82.0 },
        { "city": "Patan", "admin1": "Central Region", "country_code": "NP", "count": 60664, "pct": 18.0 }
      ],
      "other_count": 0
    },
    "bounds": {
      "min_lat": 27.6057067,
      "max_lat": 27.7497467,
      "min_lon": 85.2212283,
      "max_lon": 85.4287483
    }
  }
}
```

## Fields

| Field | Type | Meaning |
|---|---|---|
| `geo` | object or `null` | `null` = no coordinates found. Hide the map |
| `source` | `"columns"` or `"geojson"` | Where the points came from: two lat/lon columns, or GeoJSON `Point` features |
| `lat_column`, `lon_column` | string or `null` | Column names used. `null` when `source` is `"geojson"` |
| `points` | array of `[lat, lon, row, count]` | WGS84 decimal degrees, max 7 decimals. `row` = 0-based row index in the full dataset. `count` = how many rows this point stands for (1 when not thinned) |
| `point_count` | int | Length of `points` (max 5,000 by default) |
| `total_valid` | int | Rows with usable coordinates before sampling |
| `sampled` | bool | `true` when `total_valid > point_count` (points were grid-thinned) |
| `cell_deg` | number or `null` | Grid cell size in degrees used for thinning. `null` when not thinned |
| `regions` | object or `null` | Where the rows are. `items[]`: `city`, `admin1` (state/region, may be `null`), `country_code` (ISO alpha-2), `count` (rows), `pct` (of `total_valid`), largest first, max 10. `other_count`: rows in all other places. `null` if the lookup is unavailable |
| `bounds` | object | `min_lat`, `max_lat`, `min_lon`, `max_lon` of the sent points |

## What the UI needs to do

1. **Show the map only when `geo` is not `null`.** A tab, toggle or panel in the data preview window. No map, no empty state needed.
2. **Plot `points`.** One marker per entry. Use a canvas renderer for 5,000 points (Leaflet `preferCanvas: true`, or a MapLibre circle layer); DOM markers will be slow.
3. **Fit the view to `bounds`** on load.
4. **Show a sampling note when `sampled` is `true`**, e.g. "Showing 5,000 of 337,020 points".
5. **Optional: use `count` for density.** Scale marker size or colour by `count`, or feed it as the weight to a heatmap layer. Without it, a depot with 400 rows looks the same as a road passed once.
6. **List the regions above the map** when `regions` is not `null`: "City, Country · pct%", plus "Other places" when `other_count` > 0. Turn `country_code` into a name with `new Intl.DisplayNames(["en"], { type: "region" }).of(code)`.
7. **Label the source columns** (`lat_column` / `lon_column`) so users know what's plotted. Skip when `null`.
8. **Add map tile attribution** for whichever tile provider is used (OpenStreetMap requires it).

## Watch out for

- **Coordinate order is `[lat, lon]`.** Leaflet takes `[lat, lng]`, so pass as is. MapLibre, Mapbox and GeoJSON take `[lng, lat]`: swap them, or every point lands in the wrong place.
- **`row` is not a key into `head`/`tail`.** It's the row's position in the whole dataset, but the payload only carries the first and last 50 rows. Fine for showing "Row 12,345" in a tooltip; don't build click-to-open-row on it.
- **Each point is one cell, not one row** when `sampled` is `true`. Its coordinates are the first real row in that cell, so they're exact, but `count` other rows sit nearby.
- **Points are cleaned already.** Rows with missing, out-of-range or `0,0` coordinates are dropped server-side. Don't re-filter.
- **Not in `raw`.** `geo` is computed once, on the normalized data. There is no `raw.geo`.
- **Explore only.** `/v1/download` is unchanged; exported files keep their original columns.

## How detection works (for reference)

- Columns are matched by name, case-insensitive: `latitude`/`lat` and `longitude`/`lon`/`lng`/`long`, or names ending in those (`gps_lat`, `position.lng`).
- Both columns must be numeric, with at least 90% of values in range (lat ±90, lon ±180).
- If no columns match, GeoJSON `Point` geometry is used.
- Regions: each point gets the nearest city with 1,000+ inhabitants from the offline GeoNames dataset, weighted by `count`. A point far from any town (sea, countryside) still gets its nearest town, so treat names as approximate.
- Not mapped: lines, polygons, WKT/WKB strings, projected coordinates (e.g. UTM). These datasets get `geo: null`.
- Thinning: above `MAP_MAX_POINTS` (default 5,000), a grid of square cells is laid over the points, sized so at most that many cells are occupied. Each occupied cell keeps its first row and a `count`. This guarantees every area with data appears, even sparse ones.

## Demo

`docs/map-preview-demo.html` in the DPD folder: a working Leaflet map built from the Mahanagar bus CSV. Note: it was made before the switch to grid thinning, so its points have no `count`. Open it in a browser as a reference implementation.
