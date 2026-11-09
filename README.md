# cdb-geoserver

Publish the raster datasets of an OGC CDB 1.3 data store (Elevation, Imagery, ...) to
GeoServer as coverages, served over WMS and WCS. The store is only read.

One layer per dataset, named `D001_Elevation`, `D004_Imagery`, ... A dataset with several
components (for example yearly and monthly imagery) gets one layer per component, such as
`D004_Imagery_S001_T001`. LOD is a layer dimension:

- `DIM_LOD` picks the LOD, from `-10` to `23`. Capabilities list the LODs the store holds.
- Without `DIM_LOD`, you get the finest LOD present.

```
GetMap ...&layers=cdb:D004_Imagery&DIM_LOD=3
GetCoverage ...&coverageId=cdb__D001_Elevation&subset=Lat(47.4,47.5)&subset=Long(8.5,8.6)
```

Elevation gets a color-ramp style that spans the store's height range. Imagery uses the
default RGB style. WCS returns the stored values: Float32 metres above the WGS-84 ellipsoid
for Elevation (CDB 1.3 Vol 1 §10.6.1).

## How it works

For each dataset, the script writes a mosaic folder under `--mosaics` holding:

- one small GDAL VRT per geocell and LOD (`N47E008_LOD+03.vrt`), which places that LOD's
  tiles by their CDB address (Vol 1 §8.6.2, §8.6.3.1), so no pixels are copied. When a
  LOD holds only some of a geocell's tiles, each tile gets its own VRT
  (`N47E008_LOD+03_U5_R2.vrt`), so the missing tiles stay transparent rather than drawn
  as black or 0 m;
- `indexer.properties` and `lodregex.properties`, which make GeoServer index the VRTs with
  an integer `lod` attribute.

It then publishes each folder as an ImageMosaic store through the REST API and turns on the
LOD dimension. Elevation posts sit on the corner grid (Vol 1 §10.6 Req 86), so the Elevation
VRTs reach half a post past each geocell's west and north edges. Imagery fills its cells.

## Needs

- Python 3.8 or later and the GDAL command-line tools (`gdalinfo`) on the machine that runs
  the script.
- GeoServer with the **GDAL extension**, which reads the VRT granules and, through them, the
  JPEG 2000 imagery. Tested with GeoServer 2.27.2 and GDAL 3.8.
- GeoServer must read the CDB store and read and write the mosaic folder. The VRTs point at
  the tiles with relative paths, so keep the store and the mosaic folder in the same layout
  on both sides, for example both under one shared mount. If GeoServer sees that mount
  under another path, map it with `--server-path LOCAL=SERVER`.

## Use

```bash
python3 cdb_geoserver.py /data/sample/CDB --mosaics /data/sample/geoserver \
    --url http://localhost:8080/geoserver --user admin --password geoserver
```

| Option | Default | |
|---|---|---|
| `--workspace` | `cdb` | GeoServer workspace (created if missing) |
| `--datasets` | all | dataset codes, e.g. `1,4` |
| `--min-lod`, `--max-lod` | `-10`, `23` | LODs to publish |
| `--server-path` | | `LOCAL=SERVER` path prefix, repeatable |
| `--elevation-range` | from the data | `MIN,MAX` metres for the Elevation ramp |
| `--replace` | | replace existing stores and mosaic folders of the same name |

GeoServer creates the mosaic index files inside each mosaic folder, so that folder must be
writable by GeoServer. If the script and GeoServer run as different users, run the script
with `umask 000` or give both users a shared group.

## Tests

```bash
python3 test_cdb_geoserver.py
```

The tests need neither GeoServer nor GDAL.

## License

MIT, see `LICENSE`.
