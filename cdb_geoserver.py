#!/usr/bin/env python3
"""Publish the raster datasets of an OGC CDB 1.3 data store to GeoServer as coverages.

Read-only on the store. For each dataset (and component, e.g. S001_T001) it writes a small
mosaic folder next to the store, with one GDAL VRT per geocell and LOD that points at the
store's tiles in place, and publishes that folder as an ImageMosaic coverage through the
GeoServer REST API. LOD is a custom dimension of the layer (WMS/WCS parameter DIM_LOD),
and the finest LOD is the default:

    python3 cdb_geoserver.py out/CDB --mosaics out/geoserver \\
        --url http://localhost:8080/geoserver --user admin --password geoserver

GeoServer reads the VRTs and the tiles itself, so it must see both folders: on the same
machine, or through a shared mount (see --server-path). The VRT files point at the tiles
with relative paths, so the store and the mosaic folder must keep their relative layout.
GeoServer needs its GDAL extension for the VRT granules (and so for the JPEG 2000 imagery).

Needs only the Python standard library and the GDAL command-line tools (gdalinfo).
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import NamedTuple, Optional
from xml.sax.saxutils import escape, quoteattr

# ---------------------------------------------------------------------------------------
# CDB tile addressing (pure)

# CDB 1.3 Vol 1 §8.6.2 Table 3-28: <CDB>/Tiles/Lat/Lon/nnn_DatasetName/LOD/UREF/.
LAT_DIR = re.compile(r"^[NS]\d{2}$")
LON_DIR = re.compile(r"^[EW]\d{3}$")
DATASET_DIR = re.compile(r"^(\d{3})_(\w+)$")  # §8.6.2.3: "001_Elevation"
LOD_DIR = re.compile(r"^L(C|\d{2})$")  # §8.6.2.4: "LC" holds every negative LOD
UREF_DIR = re.compile(r"^U(0|[1-9]\d*)$")  # §8.6.2.5
# §8.6.3.1 Requirement 67, Table 3-30: LatLon_Dnnn_Snnn_Tnnn_LOD_Un_Rn.<ext>
TILE_FILE = re.compile(
    r"^(?P<lat>[NS]\d{2})(?P<lon>[EW]\d{3})_D(?P<d>\d{3})_S(?P<s>\d{3})_T(?P<t>\d{3})"
    r"_(?P<lod>L(?:C\d{2}|\d{2}))_U(?P<u>0|[1-9]\d*)_R(?P<r>0|[1-9]\d*)\.(?P<ext>[A-Za-z0-9]+)$")
# Raster tile formats: GeoTIFF and JPEG 2000 (Vol 1 §9); extensions are lowercase (Req 5).
RASTER_EXTENSIONS = ("tif", "jp2")
# Elevation (001) uses the corner grid (Vol 1 §10.6.1 Table 5-11); the other raster
# datasets, Imagery (004, §10.6.2) among them, use the center grid.
CORNER_GRID_DATASETS = {1}


class Tile(NamedTuple):
    path: str
    dataset_dir: str  # "001_Elevation"
    dataset: int
    component: str  # "S001_T001"
    lod: int
    lat: int  # geocell south edge
    lon: int  # geocell west edge
    u: int
    r: int


def parse_lod_name(token):
    """L00..L23, or LC01..LC10 for negative LODs (Vol 1 §8.3.8.5)."""
    return -int(token[2:]) if token.startswith("LC") else int(token[1:])


def geocell_width(lat):
    """Geocell width in degrees for a latitude slice (Vol 1 §8.6.2.2 Table 3-29)."""
    for south, north, width in ((-90, -89, 12), (-89, -80, 6), (-80, -75, 4), (-75, -70, 3),
                                (-70, -50, 2), (-50, 50, 1), (50, 70, 2), (70, 75, 3),
                                (75, 80, 4), (80, 89, 6), (89, 90, 12)):
        if south <= lat < north:
            return width
    raise ValueError(f"latitude {lat} outside -90..89")


def tiles_per_side(lod):
    """One tile per geocell up to LOD 0, 2^LOD x 2^LOD above (Vol 1 §7.1.2.1 Req 14, §7.1.2.3 Req 16)."""
    return 1 << max(lod, 0)


def raster_size(lod):
    """Elements per tile side: 1024 at LOD >= 0, 1024 >> -LOD below (Vol 1 §7.1.2 Table 2-4)."""
    return 1024 >> max(-lod, 0)


def _signed(name, positive):
    return int(name[1:]) * (1 if name[0] == positive else -1)


def parse_tile_path(path, rel_dirs):
    """Tile for `path` whose folders below Tiles/ are `rel_dirs` (Lat, Lon, Dataset, LOD, UREF);
    None when the file is not a raster tile or does not match its folders."""
    m = TILE_FILE.match(os.path.basename(path))
    if not m or m["ext"] not in RASTER_EXTENSIONS or len(rel_dirs) != 5:
        return None
    lat_dir, lon_dir, dataset_dir, lod_dir, u_dir = rel_dirs
    ds = DATASET_DIR.match(dataset_dir)
    lod = parse_lod_name(m["lod"])
    # Lat, Lon and Un in the name equal the folder names, the dataset code matches the
    # dataset folder and LOD < 0 lives in LC (Vol 1 §8.6.2, Table 3-30).
    if (m["lat"], m["lon"], "U" + m["u"]) != (lat_dir, lon_dir, u_dir) or not ds or \
            ds[1] != m["d"] or lod_dir != ("LC" if lod < 0 else m["lod"]):
        return None
    lat, lon = _signed(m["lat"], "N"), _signed(m["lon"], "E")
    u, r = int(m["u"]), int(m["r"])
    # Geocells: -90 <= lat < 90, -180 <= lon < 180, west edges on multiples of the zone
    # width from -180 (Vol 1 §8.6.2.1, §8.6.2.2); LOD -10..23 (§7.1.2 Table 2-4).
    if not (-90 <= lat < 90 and -180 <= lon < 180 and -10 <= lod <= 23):
        return None
    n = tiles_per_side(lod)
    if (lon + 180) % geocell_width(lat) or u >= n or r >= n:
        return None
    return Tile(path, dataset_dir, int(m["d"]), f"S{m['s']}_T{m['t']}", lod, lat, lon, u, r)


def find_tiles_dir(root):
    """The Tiles folder of a CDB, given the CDB root or the Tiles folder itself."""
    root = os.path.abspath(os.path.expanduser(root))
    if os.path.basename(root) == "Tiles" and os.path.isdir(root):
        return root
    tiles = os.path.join(root, "Tiles")
    if not os.path.isdir(tiles):
        raise FileNotFoundError(f"no Tiles folder in {root}")
    return tiles


def _subdirs(path, pattern):
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError:
        return []
    return [e for e in entries if e.is_dir() and pattern.match(e.name)]


def scan_tiles(root, datasets=None, min_lod=-10, max_lod=23):
    """Raster tiles under <root>/Tiles, filtered by dataset code and LOD range; anything that
    is not a CDB tile is skipped."""
    wanted = set(datasets) if datasets else None
    found = []
    for lat in _subdirs(find_tiles_dir(root), LAT_DIR):
        for lon in _subdirs(lat.path, LON_DIR):
            for ds in _subdirs(lon.path, DATASET_DIR):
                if wanted is not None and int(ds.name[:3]) not in wanted:
                    continue
                for lod in _subdirs(ds.path, LOD_DIR):
                    lo, hi = (-10, -1) if lod.name == "LC" else (int(lod.name[1:]),) * 2
                    if hi < min_lod or lo > max_lod:
                        continue
                    for u in _subdirs(lod.path, UREF_DIR):
                        dirs = (lat.name, lon.name, ds.name, lod.name, u.name)
                        for f in sorted(os.scandir(u.path), key=lambda e: e.name):
                            t = parse_tile_path(f.path, dirs) if f.is_file() else None
                            if t and min_lod <= t.lod <= max_lod:
                                found.append(t)
    found.sort(key=lambda t: (t.dataset, t.component, t.lod, t.lat, t.lon, t.u, t.r))
    return found


DIMENSION = "LOD"  # WMS/WCS parameter DIM_LOD
INDEX_ATTRIBUTE = "lod"
LOD_REGEX_FILE = "lodregex"
ELEVATION_DATASET = 1  # Vol 1 §10.6.1.2 Table 5-11


class BandLayout(NamedTuple):
    data_type: str  # GDAL name, e.g. "Float32", "Byte"
    color_interp: tuple  # per band, e.g. ("Red", "Green", "Blue")
    nodata: Optional[float] = None


# ---------------------------------------------------------------------------------------
# Names, VRT and mosaic configuration (pure)

def layer_name(dataset_dir, component, components):
    """Layer, store and mosaic name: "D001_Elevation", plus "_S001_T001" when the dataset has
    more than one component. The "D" keeps the name a valid XML name (WCS coverage ids)."""
    name = "D" + dataset_dir
    return f"{name}_{component}" if len(components) > 1 else name


def geocell_name(lat, lon):
    """Geocell part of a tile name, e.g. N47E008 (Vol 1 §8.6.3.1 Table 3-30)."""
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def granule_name(lat, lon, lod, tile=None):
    """VRT file name of one geocell at one LOD, e.g. N47E008_LOD+03.vrt, N47E008_LOD-10.vrt,
    or of one tile of it, e.g. N47E008_LOD+03_U5_R2.vrt. The signed number is what the
    mosaic indexer reads into the "lod" attribute."""
    name = f"{geocell_name(lat, lon)}_LOD{lod:+03d}"
    return f"{name}_U{tile.u}_R{tile.r}.vrt" if tile else name + ".vrt"


def geocell_grid(dataset, lat, lon, lod):
    """(size, geotransform) of one geocell at one LOD as a single raster, square in elements.

    A geocell holds 2^LOD x 2^LOD tiles at LOD >= 0 and one tile below (Vol 1 §7.1.2.1
    Req 14, §7.1.2.3 Req 16); a tile holds raster_size(lod)^2 elements (Table 2-4).
    Center-grid elements fill their cells; corner-grid elements (Elevation) sit on the grid
    lines, so their pixels reach half an element past the west and north edges (Vol 1 §10.6
    Req 86, 87). Rows run from north to south (Req 85).
    """
    size = tiles_per_side(lod) * raster_size(lod)
    dx, dy = geocell_width(lat) / size, 1.0 / size
    west, north = float(lon), float(lat + 1)
    if dataset in CORNER_GRID_DATASETS:
        west, north = west - dx / 2, north + dy / 2
    return size, (west, dx, 0.0, north, 0.0, -dy)


def vrt_xml(tiles, layout, vrt_dir, single_tile=False):
    """GDAL VRT that places the tiles of one geocell at one LOD by their CDB address, or with
    `single_tile`, a VRT that covers just tiles[0].

    The tiles' own georeferencing is not needed (CDB readers ignore GeoTIFF tags, Vol 1 §9);
    each tile goes to row (n-1-U), column R of the geocell (Vol 1 §8.6.2.5, §8.6.3.1).
    """
    t0 = tiles[0]
    size, gt = geocell_grid(t0.dataset, t0.lat, t0.lon, t0.lod)
    n, ts = tiles_per_side(t0.lod), raster_size(t0.lod)
    origin = (0, 0)  # pixel offset of the VRT's corner in the geocell grid
    if single_tile:
        tiles, size = tiles[:1], ts
        origin = (t0.r * ts, (n - 1 - t0.u) * ts)
        gt = (gt[0] + origin[0] * gt[1], gt[1], 0.0, gt[3] + origin[1] * gt[5], 0.0, gt[5])
    out = [f'<VRTDataset rasterXSize="{size}" rasterYSize="{size}">',
           # WGS-84 geographic, longitude first in the geotransform (Vol 1 §8.6.2).
           '  <SRS dataAxisToSRSAxisMapping="2,1">EPSG:4326</SRS>',
           "  <GeoTransform>" + ", ".join(repr(v) for v in gt) + "</GeoTransform>"]
    for b, interp in enumerate(layout.color_interp, start=1):
        out.append(f'  <VRTRasterBand dataType="{layout.data_type}" band="{b}">')
        out.append(f"    <ColorInterp>{interp}</ColorInterp>")
        if layout.nodata is not None:
            out.append(f"    <NoDataValue>{layout.nodata!r}</NoDataValue>")
        for t in tiles:
            rel = os.path.relpath(t.path, vrt_dir).replace(os.sep, "/")
            out += ["    <SimpleSource>",
                    f'      <SourceFilename relativeToVRT="1">{escape(rel)}</SourceFilename>',
                    f"      <SourceBand>{b}</SourceBand>",
                    f'      <SrcRect xOff="0" yOff="0" xSize="{ts}" ySize="{ts}"/>',
                    f'      <DstRect xOff="{t.r * ts - origin[0]}" yOff="{(n - 1 - t.u) * ts - origin[1]}" '
                    f'xSize="{ts}" ySize="{ts}"/>',
                    "    </SimpleSource>"]
        out.append("  </VRTRasterBand>")
    out.append("</VRTDataset>")
    return "\n".join(out) + "\n"


def indexer_properties(name, dimension=True):
    """ImageMosaic indexer: VRT granules with an integer "lod" attribute read from the file
    name, published as the custom dimension LOD unless `dimension` is False."""
    return "\n".join([
        f"Name={name}",
        f"TypeName={name}",
        "Wildcard=*.vrt",
        "Recursive=false",
        "AbsolutePath=false",
        f"Schema=*the_geom:Polygon,location:String,{INDEX_ATTRIBUTE}:Integer",
        f"PropertyCollectors=IntegerFileNameExtractorSPI[{LOD_REGEX_FILE}]({INDEX_ATTRIBUTE})",
        *([f"AdditionalDomainAttributes={DIMENSION}({INDEX_ATTRIBUTE})"] if dimension else []),
        "Caching=false",
        "",
    ])


def lod_regex_properties():
    # "+03" / "-10" after "_LOD" in granule_name(); Java's Integer parsing accepts the sign.
    return "regex=(?<=_LOD)[+-][0-9]{2}\n"


def group_by_layer(tiles):
    """{(dataset_dir, component): {(lat, lon, lod): [tiles]}}, in scan order."""
    groups = defaultdict(lambda: defaultdict(list))
    for t in tiles:
        groups[(t.dataset_dir, t.component)][(t.lat, t.lon, t.lod)].append(t)
    return groups


def granule_files(granules):
    """[(file name, tiles, single_tile)]: one VRT per geocell and LOD when that LOD holds every
    tile of the geocell, else one VRT per tile, so the mosaic footprint covers only the tiles
    that exist and the rest stays transparent instead of zero."""
    files = []
    for (lat, lon, lod), tiles in sorted(granules.items()):
        if len(tiles) == tiles_per_side(lod) ** 2:
            files.append((granule_name(lat, lon, lod), tiles, False))
        else:
            files += [(granule_name(lat, lon, lod, t), [t], True) for t in tiles]
    return files


def footprint_wkt(tile, whole_geocell):
    """Footprint of a granule: the geocell, or the one tile, in degrees (Vol 1 §8.6.2.2,
    §8.6.2.5). Cutting granules to it trims the half post that corner-grid Elevation pixels
    reach past the west and north edges (Vol 1 §10.6 Req 86), which would otherwise stick
    out of coarse LODs drawn under finer ones."""
    width = geocell_width(tile.lat)
    if whole_geocell:
        w, s, e, n = tile.lon, tile.lat, tile.lon + width, tile.lat + 1
    else:
        k = tiles_per_side(tile.lod)
        w, s = tile.lon + tile.r * width / k, tile.lat + tile.u / k
        e, n = w + width / k, s + 1 / k
    return f"POLYGON (({w!r} {s!r}, {e!r} {s!r}, {e!r} {n!r}, {w!r} {n!r}, {w!r} {s!r}))"


def write_mosaic(folder, name, granules, layout, dimension=True):
    """Write the mosaic folder: indexer files, and the VRTs of granule_files() with a WKT
    footprint file next to each."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "indexer.properties"), "w", encoding="utf-8") as f:
        f.write(indexer_properties(name, dimension))
    with open(os.path.join(folder, LOD_REGEX_FILE + ".properties"), "w", encoding="utf-8") as f:
        f.write(lod_regex_properties())
    for file_name, tiles, single in granule_files(granules):
        with open(os.path.join(folder, file_name), "w", encoding="utf-8") as f:
            f.write(vrt_xml(tiles, layout, folder, single))
        with open(os.path.join(folder, file_name[:-4] + ".wkt"), "w", encoding="utf-8") as f:
            f.write(footprint_wkt(tiles[0], not single))


def coverage_xml(title, dimension=True):
    """Coverage settings: WGS-84 geographic; granules cut to their footprints and drawn finest
    LOD on top; with `dimension`, LOD as a list dimension whose default is the largest
    (finest) LOD present."""
    metadata = (f"<metadata><entry key={quoteattr('custom_dimension_' + DIMENSION)}>"
                "<dimensionInfo><enabled>true</enabled><presentation>LIST</presentation>"
                "<defaultValue><strategy>MAXIMUM</strategy></defaultValue>"
                "</dimensionInfo></entry></metadata>") if dimension else ""
    return (f"<coverage><title>{escape(title)}</title><enabled>true</enabled>"
            "<srs>EPSG:4326</srs><projectionPolicy>FORCE_DECLARED</projectionPolicy>"
            "<parameters>"
            "<entry><string>FootprintBehavior</string><string>Cut</string></entry>"
            f"<entry><string>SORTING</string><string>{INDEX_ATTRIBUTE} D</string></entry>"
            f"</parameters>{metadata}</coverage>")


def elevation_sld(name, low, high):
    """Color ramp from `low` to `high` metres (heights above the WGS-84 ellipsoid, Vol 1 §10.6.1)."""
    colors = ("#2b6a3b", "#8fb36b", "#e8d58c", "#b0865a", "#ffffff")
    steps = len(colors) - 1
    # GeoServer leaves values outside the ramp transparent, so the end colors are repeated
    # far below and above it: sampled ranges can miss a few extreme posts.
    stops = [(colors[0], -20000.0)] + \
        [(c, low + (high - low) * i / steps) for i, c in enumerate(colors)] + \
        [(colors[-1], 20000.0)]
    entries = "".join(f'<ColorMapEntry color="{c}" quantity="{q:.1f}"/>' for c, q in stops)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" '
            'xmlns:ogc="http://www.opengis.net/ogc">'
            f"<NamedLayer><Name>{escape(name)}</Name><UserStyle><Title>{escape(name)}</Title>"
            "<FeatureTypeStyle><Rule><RasterSymbolizer><Opacity>1.0</Opacity>"
            f"<ColorMap>{entries}</ColorMap>"
            "</RasterSymbolizer></Rule></FeatureTypeStyle></UserStyle></NamedLayer>"
            "</StyledLayerDescriptor>")


def map_path(path, mappings):
    """`path` as GeoServer sees it: the first LOCAL=SERVER prefix that matches is replaced."""
    for local, server in mappings:
        local = local.rstrip("/")
        if path == local or path.startswith(local + "/"):
            return server.rstrip("/") + path[len(local):]
    return path


# ---------------------------------------------------------------------------------------
# GDAL command-line tools

def _gdalinfo(path, *args):
    env = dict(os.environ, GDAL_PAM_ENABLED="NO")  # no .aux.xml files in the store
    out = subprocess.run(["gdalinfo", "-json", *args, path], capture_output=True, text=True,
                         env=env, check=True).stdout
    return json.loads(out)


def read_layout(path):
    """Band layout of a tile: data type, color interpretation and nodata of its bands."""
    bands = _gdalinfo(path)["bands"]
    return BandLayout(bands[0]["type"], tuple(b.get("colorInterpretation", "Undefined")
                                              for b in bands), bands[0].get("noDataValue"))


def value_range(paths):
    """(min, max) of band 1 over the given tiles."""
    lo, hi = float("inf"), float("-inf")
    for p in paths:
        b = _gdalinfo(p, "-mm")["bands"][0]
        lo, hi = min(lo, b["computedMin"]), max(hi, b["computedMax"])
    return lo, hi


# ---------------------------------------------------------------------------------------
# GeoServer REST API

class RestError(RuntimeError):
    pass


class GeoServer:
    def __init__(self, url, user, password):
        self.base = url.rstrip("/") + "/rest"
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Authorization": "Basic " + token}

    def request(self, method, path, body=None, content_type=None, accept=(200, 201)):
        """Status code; raises RestError unless it is in `accept`."""
        data = body.encode("utf-8") if isinstance(body, str) else body
        headers = dict(self.headers)
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                status, text = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            raise RestError(f"{method} {self.base}{path}: {e.reason}") from None
        if status not in accept:
            raise RestError(f"{method} {self.base}{path}: HTTP {status} {text.strip()[:300]}")
        return status


def publish_layer(gs, workspace, name, title, mosaic_dir, replace, style=None, dimension=True):
    q = urllib.parse.quote
    store = f"/workspaces/{q(workspace)}/coveragestores/{q(name)}"
    if gs.request("GET", store + ".json", accept=(200, 404)) == 200:
        if not replace:
            raise RestError(f"store {workspace}:{name} exists; use --replace")
        # purge=none: GeoServer leaves the files alone; this script rewrites the folder.
        gs.request("DELETE", store + "?recurse=true&purge=none")
    gs.request("PUT", store + "/external.imagemosaic?configure=all", mosaic_dir,
               "text/plain")
    gs.request("PUT", f"{store}/coverages/{q(name)}", coverage_xml(title, dimension),
               "application/xml")
    if style:
        sname, sld = style
        styles = f"/workspaces/{q(workspace)}/styles"
        if gs.request("GET", f"{styles}/{q(sname)}.json", accept=(200, 404)) == 404:
            gs.request("POST", f"{styles}?name={q(sname)}", sld, "application/vnd.ogc.sld+xml")
        else:
            gs.request("PUT", f"{styles}/{q(sname)}", sld, "application/vnd.ogc.sld+xml")
        body = json.dumps({"layer": {"defaultStyle": {"name": f"{workspace}:{sname}"}}})
        gs.request("PUT", f"/layers/{q(workspace)}:{q(name)}", body, "application/json")


def ensure_workspace(gs, workspace):
    if gs.request("GET", f"/workspaces/{urllib.parse.quote(workspace)}.json",
                  accept=(200, 404)) == 404:
        gs.request("POST", "/workspaces", json.dumps({"workspace": {"name": workspace}}),
                   "application/json")


# ---------------------------------------------------------------------------------------

def publish(root, mosaics, gs, workspace="cdb", datasets=None, min_lod=-10, max_lod=23,
            server_paths=(), replace=False, elevation_range=None, log=print):
    """Write the mosaic folders under `mosaics` and publish two layers per dataset/component:
    `name`, one LOD at a time (DIM_LOD), and `name`_all, every LOD with the finest on top.
    Returns the layer names."""
    tiles_dir = find_tiles_dir(root)
    mosaics = os.path.abspath(mosaics)
    if os.path.commonpath([mosaics, tiles_dir]) == tiles_dir:
        raise ValueError("the mosaic folder must be outside the CDB Tiles folder")
    groups = group_by_layer(scan_tiles(root, datasets, min_lod, max_lod))
    if not groups:
        raise ValueError(f"no raster tiles in {tiles_dir}")
    components = defaultdict(set)
    for dataset_dir, component in groups:
        components[dataset_dir].add(component)
    ensure_workspace(gs, workspace)
    names = []
    for (dataset_dir, component), granules in groups.items():
        name = layer_name(dataset_dir, component, components[dataset_dir])
        first = next(iter(granules.values()))[0]
        layout = read_layout(first.path)
        lods = sorted({lod for _, _, lod in granules})
        style = None
        if first.dataset == ELEVATION_DATASET:
            if elevation_range:
                lo, hi = elevation_range
            else:  # a coarse LOD that still holds 256 x 256 posts per geocell (Table 2-4)
                pick = min((lod for lod in lods if lod >= -2), default=lods[-1])
                lo, hi = value_range([t.path for (_, _, lod), ts in granules.items()
                                      if lod == pick for t in ts])
            style = (name + "_ramp", elevation_sld(name + "_ramp", lo, hi))
        for layer, dimension in ((name, True), (name + "_all", False)):
            folder = os.path.join(mosaics, layer)
            if os.path.exists(folder):
                if not replace:
                    raise ValueError(f"{folder} exists; use --replace")
                shutil.rmtree(folder)
            write_mosaic(folder, layer, granules, layout, dimension)
            title = f"{dataset_dir} {component}" + ("" if dimension else ", all LODs")
            publish_layer(gs, workspace, layer, title, map_path(folder, server_paths), replace,
                          style, dimension)
            names.append(layer)
        partial = sorted({lod for (_, _, lod), ts in granules.items()
                          if len(ts) < tiles_per_side(lod) ** 2})
        log(f"{workspace}:{name}: {len(granule_files(granules))} granules, "
            f"LOD {lods[0]}..{lods[-1]} (default {lods[-1]})"
            + (f"; LOD {', '.join(map(str, partial))} cover part of a geocell" if partial else "")
            + f"; {workspace}:{name}_all: every LOD, finest on top")
    return names


def _range(text):
    lo, hi = (float(v) for v in text.split(","))
    return lo, hi


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("cdb", help="CDB folder (the one holding Tiles/)")
    p.add_argument("--mosaics", required=True,
                   help="folder for the mosaic configuration (VRTs); GeoServer must read and "
                        "write it")
    p.add_argument("--url", default="http://localhost:8080/geoserver")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default=os.environ.get("GEOSERVER_PASSWORD", "geoserver"),
                   help="default: $GEOSERVER_PASSWORD, else geoserver")
    p.add_argument("--workspace", default="cdb")
    p.add_argument("--datasets", help="dataset codes, e.g. 1,4 (default: all raster datasets)")
    # LOD −10..23 (CDB 1.3 Vol 1 §7.1.2 Table 2-4).
    p.add_argument("--min-lod", type=int, default=-10)
    p.add_argument("--max-lod", type=int, default=23)
    p.add_argument("--server-path", action="append", default=[], metavar="LOCAL=SERVER",
                   help="path prefix as GeoServer sees it, e.g. /data/cdb=/srv/cdb "
                        "(repeatable)")
    p.add_argument("--elevation-range", type=_range, metavar="MIN,MAX",
                   help="metres for the Elevation color ramp (default: min/max of the "
                        "coarsest LOD)")
    p.add_argument("--replace", action="store_true",
                   help="replace existing stores and mosaic folders of the same name")
    a = p.parse_args(argv)
    try:
        datasets = [int(c) for c in a.datasets.split(",")] if a.datasets else None
        mappings = [tuple(m.split("=", 1)) for m in a.server_path]
        if any(len(m) != 2 for m in mappings):
            raise ValueError("--server-path takes LOCAL=SERVER")
        names = publish(a.cdb, a.mosaics, GeoServer(a.url, a.user, a.password), a.workspace,
                        datasets, a.min_lod, a.max_lod, mappings, a.replace, a.elevation_range)
    except (OSError, ValueError, RestError, subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    wms = a.url.rstrip("/") + f"/{a.workspace}/wms"
    print(f"WMS: {wms}?service=WMS&request=GetCapabilities "
          f"(layers {', '.join(a.workspace + ':' + n for n in names)}; pick a LOD with DIM_LOD)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
