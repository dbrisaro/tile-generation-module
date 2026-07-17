from tilegen.grid import TileGrid


def test_tile_ids():
    g = TileGrid(10)
    t = g.tile_at(-77.03, -12.05)  # Lima
    assert t.id == "s20w080"
    assert t.bounds == (-80, -20, -70, -10)
    assert g.tile_at(0.1, 0.1).id == "n00e000"
    assert g.tile_at(-0.1, -0.1).id == "s10w010"


def test_bbox_and_snap():
    g = TileGrid(10)
    tiles = g.tiles_for_bbox(-82, -19, -68, 1)  # Peru-ish
    assert len(tiles) == 9
    assert g.snap_bbox((-82, -19, -68, 1)) == (-90, -20, -60, 10)
    assert len(g.all_tiles()) == 36 * 18


def test_parse_roundtrip():
    g = TileGrid(10)
    for t in g.all_tiles():
        assert g.parse_id(t.id) == t


if __name__ == "__main__":
    test_tile_ids()
    test_bbox_and_snap()
    test_parse_roundtrip()
    print("all grid tests passed")
