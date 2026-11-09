"""Tests for cdb_geoserver.py: addressing, VRT and mosaic config, REST calls (no server, no GDAL)."""

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdb_geoserver as c  # noqa: E402


def parse(rel):
    parts = rel.split("/")
    return c.parse_tile_path("/cdb/Tiles/" + rel, parts[:-1])


def make_store(root, rels):
    for rel in rels:
        path = os.path.join(root, "Tiles", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()


class Addressing(unittest.TestCase):
    def test_parse(self):
        t = parse("N47/E008/001_Elevation/L01/U1/N47E008_D001_S001_T001_L01_U1_R0.tif")
        self.assertEqual((t.dataset_dir, t.dataset, t.component, t.lod, t.lat, t.lon, t.u, t.r),
                         ("001_Elevation", 1, "S001_T001", 1, 47, 8, 1, 0))
        t = parse("S06/W118/004_Imagery/LC/U0/S06W118_D004_S001_T001_LC10_U0_R0.jp2")
        self.assertEqual((t.dataset, t.lod, t.lat, t.lon), (4, -10, -6, -118))

    def test_rejects_mismatches(self):
        self.assertIsNone(parse("N47/E008/001_Elevation/L01/U1/N47E008_D001_S001_T001_L01_U0_R0.tif"))
        self.assertIsNone(parse("N47/E008/001_Elevation/L00/U0/N47E008_D001_S001_T001_LC01_U0_R0.tif"))
        self.assertIsNone(parse("N47/E008/001_Elevation/L00/U0/N47E008_D001_S001_T001_L00_U0_R0.TIF"))
        self.assertIsNone(parse("N47/E008/001_Elevation/L00/U0/N47E008_D001_S001_T001_L00_U0_R1.tif"))
        # Geocells are 2° wide at 50..70° N, so the west edge must be even (Table 3-29).
        self.assertIsNone(parse("N60/E009/001_Elevation/L00/U0/N60E009_D001_S001_T001_L00_U0_R0.tif"))

    def test_scan_skips_other_files(self):
        with tempfile.TemporaryDirectory() as d:
            make_store(d, ["N47/E008/004_Imagery/L00/U0/N47E008_D004_S001_T001_L00_U0_R0.jp2",
                           "N47/E008/004_Imagery/L00/U0/notes.txt",
                           "N47/E008/004_Imagery/LC/U0/N47E008_D004_S001_T001_LC02_U0_R0.jp2"])
            self.assertEqual([t.lod for t in c.scan_tiles(d)], [-2, 0])
            self.assertEqual([t.lod for t in c.scan_tiles(d, min_lod=0)], [0])
            self.assertEqual(c.scan_tiles(d, datasets=[1]), [])


class Names(unittest.TestCase):
    def test_layer_name(self):
        self.assertEqual(c.layer_name("004_Imagery", "S001_T001", {"S001_T001"}), "D004_Imagery")
        self.assertEqual(c.layer_name("004_Imagery", "S003_T001", {"S001_T001", "S003_T001"}),
                         "D004_Imagery_S003_T001")

    def test_granule_name_and_regex(self):
        self.assertEqual(c.granule_name(47, 8, 3), "N47E008_LOD+03.vrt")
        self.assertEqual(c.granule_name(-6, -118, -10), "S06W118_LOD-10.vrt")
        self.assertEqual(c.lod_regex_properties(), "regex=(?<=_LOD)[+-][0-9]{2}\n")

    def test_map_path(self):
        m = [("/data/cdb", "/srv/cdb")]
        self.assertEqual(c.map_path("/data/cdb/x/D001", m), "/srv/cdb/x/D001")
        self.assertEqual(c.map_path("/data/cdb2/x", m), "/data/cdb2/x")


class Grid(unittest.TestCase):
    def test_center_grid(self):
        size, gt = c.geocell_grid(4, 47, 8, 1)
        self.assertEqual(size, 2048)
        self.assertEqual(gt, (8.0, 1 / 2048, 0.0, 48.0, 0.0, -1 / 2048))

    def test_corner_grid_reaches_half_a_post_west_and_north(self):
        size, gt = c.geocell_grid(1, 47, 8, -2)
        self.assertEqual(size, 256)
        self.assertAlmostEqual(gt[0], 8 - 0.5 / 256)
        self.assertAlmostEqual(gt[3], 48 + 0.5 / 256)

    def test_wide_geocell(self):
        size, gt = c.geocell_grid(4, 60, 8, 0)
        self.assertEqual((size, gt[1], gt[5]), (1024, 2 / 1024, -1 / 1024))


class Vrt(unittest.TestCase):
    def test_tiles_placed_by_address(self):
        tiles = [parse(f"N47/E008/004_Imagery/L01/U{u}/N47E008_D004_S001_T001_L01_U{u}_R{r}.jp2")
                 for u in (0, 1) for r in (0, 1)]
        layout = c.BandLayout("Byte", ("Red", "Green", "Blue"))
        root = ET.fromstring(c.vrt_xml(tiles, layout, "/mosaics/D004_Imagery"))
        self.assertEqual(root.get("rasterXSize"), "2048")
        bands = root.findall("VRTRasterBand")
        self.assertEqual([b.findtext("ColorInterp") for b in bands], ["Red", "Green", "Blue"])
        srcs = bands[0].findall("SimpleSource")
        self.assertEqual(len(srcs), 4)
        placed = {s.findtext("SourceFilename").rsplit("/", 1)[1]:
                  (s.find("DstRect").get("xOff"), s.find("DstRect").get("yOff")) for s in srcs}
        # U0 is the southern row, so it sits at the bottom (rows run north to south).
        self.assertEqual(placed["N47E008_D004_S001_T001_L01_U0_R0.jp2"], ("0", "1024"))
        self.assertEqual(placed["N47E008_D004_S001_T001_L01_U1_R1.jp2"], ("1024", "0"))
        self.assertEqual(srcs[0].findtext("SourceFilename"),
                         "../../cdb/Tiles/N47/E008/004_Imagery/L01/U0/"
                         "N47E008_D004_S001_T001_L01_U0_R0.jp2")
        self.assertIsNone(bands[0].find("NoDataValue"))

    def test_nodata(self):
        t = parse("N47/E008/001_Elevation/LC/U0/N47E008_D001_S001_T001_LC03_U0_R0.tif")
        root = ET.fromstring(c.vrt_xml([t], c.BandLayout("Float32", ("Gray",), -32767.0), "/m"))
        band = root.find("VRTRasterBand")
        self.assertEqual(band.findtext("NoDataValue"), "-32767.0")
        self.assertEqual(band.find("SimpleSource/SrcRect").get("xSize"), "128")


class PartialLod(unittest.TestCase):
    def tiles(self, lod, addresses):
        return [parse(f"N47/E008/004_Imagery/L{lod:02d}/U{u}/"
                      f"N47E008_D004_S001_T001_L{lod:02d}_U{u}_R{r}.jp2") for u, r in addresses]

    def test_full_lod_is_one_granule_partial_lod_one_per_tile(self):
        granules = {(47, 8, 1): self.tiles(1, [(0, 0), (0, 1), (1, 0), (1, 1)]),
                    (47, 8, 2): self.tiles(2, [(3, 1), (0, 2)])}
        files = c.granule_files(granules)
        self.assertEqual([(f, single) for f, _, single in files],
                         [("N47E008_LOD+01.vrt", False),
                          ("N47E008_LOD+02_U3_R1.vrt", True),
                          ("N47E008_LOD+02_U0_R2.vrt", True)])

    def test_single_tile_vrt(self):
        t = self.tiles(2, [(3, 1)])[0]
        root = ET.fromstring(c.vrt_xml([t], c.BandLayout("Byte", ("Red", "Green", "Blue")),
                                       "/m", single_tile=True))
        self.assertEqual(root.get("rasterXSize"), "1024")
        gt = [float(v) for v in root.findtext("GeoTransform").split(",")]
        # U3 R1 at LOD 2: west 8.25, north 48.0 (the top row of the geocell).
        self.assertAlmostEqual(gt[0], 8.25)
        self.assertAlmostEqual(gt[3], 48.0)
        dst = root.find("VRTRasterBand/SimpleSource/DstRect")
        self.assertEqual((dst.get("xOff"), dst.get("yOff")), ("0", "0"))

    def test_single_tile_corner_grid(self):
        t = parse("N47/E008/001_Elevation/L01/U0/N47E008_D001_S001_T001_L01_U0_R1.tif")
        root = ET.fromstring(c.vrt_xml([t], c.BandLayout("Float32", ("Gray",)), "/m", True))
        gt = [float(v) for v in root.findtext("GeoTransform").split(",")]
        self.assertAlmostEqual(gt[0], 8.5 - 0.5 / 2048)
        self.assertAlmostEqual(gt[3], 47.5 + 0.5 / 2048)


class Config(unittest.TestCase):
    def test_indexer(self):
        props = dict(line.split("=", 1) for line in c.indexer_properties("D001_Elevation").split("\n")
                     if line)
        self.assertEqual(props["Name"], "D001_Elevation")
        self.assertEqual(props["AdditionalDomainAttributes"], "LOD(lod)")
        self.assertIn("lod:Integer", props["Schema"])
        self.assertEqual(props["PropertyCollectors"], "IntegerFileNameExtractorSPI[lodregex](lod)")

    def test_indexer_without_dimension(self):
        text = c.indexer_properties("D004_Imagery_all", dimension=False)
        self.assertNotIn("AdditionalDomainAttributes", text)
        self.assertIn("lod:Integer", text)

    def test_coverage_parameters(self):
        for dimension in (True, False):
            cov = ET.fromstring(c.coverage_xml("t", dimension))
            params = {e[0].text: e[1].text for e in cov.findall("parameters/entry")}
            self.assertEqual(params, {"FootprintBehavior": "Cut", "SORTING": "lod D"})
            self.assertEqual(cov.find("metadata") is not None, dimension)

    def test_footprints(self):
        t = parse("N47/E008/004_Imagery/L02/U3/N47E008_D004_S001_T001_L02_U3_R1.jp2")
        self.assertEqual(c.footprint_wkt(t, True),
                         "POLYGON ((8 47, 9 47, 9 48, 8 48, 8 47))")
        self.assertEqual(c.footprint_wkt(t, False),
                         "POLYGON ((8.25 47.75, 8.5 47.75, 8.5 48.0, 8.25 48.0, 8.25 47.75))")
        t = parse("N60/E008/001_Elevation/L01/U0/N60E008_D001_S001_T001_L01_U0_R1.tif")
        self.assertEqual(c.footprint_wkt(t, False),  # 2° wide geocell (Table 3-29)
                         "POLYGON ((9.0 60.0, 10.0 60.0, 10.0 60.5, 9.0 60.5, 9.0 60.0))")

    def test_write_mosaic(self):
        tiles = [parse(f"N47/E008/004_Imagery/L01/U{u}/N47E008_D004_S001_T001_L01_U{u}_R{r}.jp2")
                 for u, r in ((0, 0), (1, 1))]
        with tempfile.TemporaryDirectory() as d:
            c.write_mosaic(d, "D004_Imagery_all", {(47, 8, 1): tiles},
                           c.BandLayout("Byte", ("Red", "Green", "Blue")), dimension=False)
            self.assertEqual(sorted(os.listdir(d)), [
                "N47E008_LOD+01_U0_R0.vrt", "N47E008_LOD+01_U0_R0.wkt",
                "N47E008_LOD+01_U1_R1.vrt", "N47E008_LOD+01_U1_R1.wkt",
                "indexer.properties", "lodregex.properties"])

    def test_coverage_and_style_are_xml(self):
        cov = ET.fromstring(c.coverage_xml("004_Imagery S001_T001"))
        self.assertEqual(cov.find("metadata/entry").get("key"), "custom_dimension_LOD")
        self.assertEqual(cov.findtext("metadata/entry/dimensionInfo/defaultValue/strategy"),
                         "MAXIMUM")
        sld = ET.fromstring(c.elevation_sld("ramp", 300.0, 2700.0))
        q = [float(e.get("quantity")) for e in sld.iter("{http://www.opengis.net/sld}ColorMapEntry")]
        self.assertEqual(q, sorted(q))
        self.assertEqual((q[1], q[-2]), (300.0, 2700.0))


class FakeServer:
    def __init__(self, existing=()):
        self.existing, self.calls = set(existing), []

    def request(self, method, path, body=None, content_type=None, accept=(200, 201)):
        self.calls.append((method, path.split("?")[0], body))
        status = 200 if method != "GET" or path in self.existing else 404
        if status not in accept:
            raise c.RestError(f"{method} {path}: {status}")
        return status


class Rest(unittest.TestCase):
    STORE = "/workspaces/cdb/coveragestores/D001_Elevation"

    def test_new_layer(self):
        gs = FakeServer()
        c.publish_layer(gs, "cdb", "D001_Elevation", "t", "/srv/m/D001_Elevation", False,
                        ("D001_Elevation_ramp", "<sld/>"))
        self.assertEqual([(m, p) for m, p, _ in gs.calls], [
            ("GET", self.STORE + ".json"),
            ("PUT", self.STORE + "/external.imagemosaic"),
            ("PUT", self.STORE + "/coverages/D001_Elevation"),
            ("GET", "/workspaces/cdb/styles/D001_Elevation_ramp.json"),
            ("POST", "/workspaces/cdb/styles"),
            ("PUT", "/layers/cdb:D001_Elevation")])
        self.assertEqual(gs.calls[1][2], "/srv/m/D001_Elevation")  # a plain path, not a URL

    def test_existing_store(self):
        gs = FakeServer({self.STORE + ".json"})
        with self.assertRaises(c.RestError):
            c.publish_layer(gs, "cdb", "D001_Elevation", "t", "/m", False)
        gs = FakeServer({self.STORE + ".json"})
        c.publish_layer(gs, "cdb", "D001_Elevation", "t", "/m", True)
        self.assertEqual(gs.calls[1][:2], ("DELETE", self.STORE))


if __name__ == "__main__":
    unittest.main()
