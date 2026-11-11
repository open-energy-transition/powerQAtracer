# powerQAtracer

Exports global OpenStreetmap's [Osmose](https://osmose.openstreetmap.fr/) power-line issues (item 7040) at or above a voltage threshold as GeoJSON, ready to open in JOSM.

## Usage

```sh
pip install -r requirements.txt

# Unfinished power lines ≥ 300 kV (default)
./powerQAtracer.py

# Lines with a voltage that doesn't match their substation, ≥ 380 kV
./powerQAtracer.py --issue unmatched-voltage --min-voltage-kv 380

# Lines with different voltages that share a node (own check on live OSM data, see below)
./powerQAtracer.py --issue different-voltages --bbox 5.8,47.2,15.1,55.1
```

Output: `osmose_<issue>_<kV>kv_and_above.geojson`. Downloads are cached in a `.state.sqlite` file, so interrupted runs resume and other thresholds re-filter without re-downloading (`--reset-state` fetches fresh data). See `./powerQAtracer.py --help` for all options.

## Different voltages

Osmose's "Connection between different voltages" issues contain no voltages and skip many cases, so `--issue different-voltages` runs its own check via Overpass instead. It reports **every** node where power lines with different `voltage` tags meet, without Osmose's exceptions, and labels each one so you can filter in JOSM or QGIS:

- `kind`: `voltage_ends` (a voltage appears on only one line — most likely an error), `missing_voltage` (a line has no voltage tag) or `voltage_split` (e.g. a 380;110 kV line splitting into 380 and 110 kV — usually fine).
- `line_voltages`, `unmatched_voltages_v`, and the node's `power`, `line_management`, `transformer` and `location:transition` tags.

The output is `powerQAtracer_different_voltages_<kV>kv_and_above.geojson`. `--country` is not available for this check; use `--bbox`. It is slow on the public Overpass server (a 1°×1° tile in Germany took 1–2 minutes), so start with a region.

## Working through the issues

- **Region:** `--country germany,austria` (Osmose region prefix) or `--bbox 5.8,47.2,15.1,55.1` (W,S,E,N). `--bbox` also limits the download.
- **Priority:** issues are sorted by voltage (then voltage gap) and numbered in the `priority` property.
- **JOSM:** each issue has a `josm_url` that loads the surrounding area in JOSM and selects the affected elements (enable *Remote control* in JOSM).
- **MapRoulette:** `--maproulette tasks.geojson` writes one task per line for a MapRoulette challenge.
- **Progress:** `--compare-to old.geojson` marks issues as `new` or `open`, prints a per-country summary and writes fixed issues to `<output>_resolved.geojson`:

```sh
cp osmose_unfinished_lines_300kv_and_above.geojson last_week.geojson
./powerQAtracer.py --reset-state --compare-to last_week.geojson
```

## License

MIT © Open Energy Transition. See [LICENSE](LICENSE).
