"""Tests for the entity/player position checks and the entity type sweep.

The incident values come from a real Purpur crash ("Trying to create chunk out
of reasonable bounds: [134217727, 134217727]") caused by a player whose Pos was
written during a lag spike, and a Zombie with a similar Pos in the world.
"""

import io
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from nbt import nbt
from nbt import region
from regionfixer_core import constants as c
from regionfixer_core import world as world_module
from regionfixer_core.entity_presets import HOSTILE, resolve_entity_types
from regionfixer_core.scan import scan_data, scan_region_file
from regionfixer_core.util import is_position_sane
from regionfixer_core.world import ScannedDataFile, ScannedRegionFile, World


INCIDENT_UUID = "deb4307a-972a-3f6e-8ded-8c055a6a2bf9"
INCIDENT_POS = (1.8e16, 2.0e10, 1.4e14)
BOUND = 30000000
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def double_list(name, values):
    tag = nbt.TAG_List(name=name, type=nbt.TAG_Double)
    tag.tags.extend(nbt.TAG_Double(v) for v in values)
    return tag


def entity(entity_id, pos, custom_name=None, tags=None, passengers=None):
    compound = nbt.TAG_Compound()
    compound.tags.append(nbt.TAG_String(name="id", value=entity_id))
    compound.tags.append(double_list("Pos", pos))
    compound.tags.append(double_list("Motion", (0.0, 0.0, 0.0)))
    if custom_name is not None:
        compound.tags.append(nbt.TAG_String(name="CustomName", value=custom_name))
    if tags is not None:
        tag_list = nbt.TAG_List(name="Tags", type=nbt.TAG_String)
        tag_list.tags.extend(nbt.TAG_String(t) for t in tags)
        compound.tags.append(tag_list)
    if passengers is not None:
        riders = nbt.TAG_List(name="Passengers", type=nbt.TAG_Compound)
        riders.tags.extend(passengers)
        compound.tags.append(riders)
    return compound


def entity_list(name, entities):
    tag = nbt.TAG_List(name=name, type=nbt.TAG_Compound)
    tag.tags.extend(entities)
    return tag


def to_bytes(root):
    buffer = io.BytesIO()
    root.write_file(buffer=buffer)
    return buffer.getvalue()


def entities_chunk(entities, x=0, z=0):
    """An entities/*.mca chunk (1.17+)."""
    root = nbt.NBTFile()
    root.name = ""
    root.tags.append(nbt.TAG_Int(name="DataVersion", value=3465))
    position = nbt.TAG_Int_Array(name="Position")
    position.value = [x, z]
    root.tags.append(position)
    root.tags.append(entity_list("Entities", entities))
    return to_bytes(root)


def legacy_level_chunk(entities, x=0, z=0):
    """A pre 1.17 chunk with the entities embedded under Level."""
    root = nbt.NBTFile()
    root.name = ""
    root.tags.append(nbt.TAG_Int(name="DataVersion", value=1976))
    level = nbt.TAG_Compound(name="Level")
    level.tags.append(nbt.TAG_Int(name="xPos", value=x))
    level.tags.append(nbt.TAG_Int(name="zPos", value=z))
    level.tags.append(entity_list("Entities", entities))
    root.tags.append(level)
    return to_bytes(root)


def modern_level_chunk(entities, x=0, z=0):
    """A 1.18+ level chunk that still carries an `entities` list."""
    root = nbt.NBTFile()
    root.name = ""
    root.tags.append(nbt.TAG_Int(name="DataVersion", value=3465))
    root.tags.append(nbt.TAG_Int(name="xPos", value=x))
    root.tags.append(nbt.TAG_Int(name="zPos", value=z))
    root.tags.append(nbt.TAG_List(name="sections", type=nbt.TAG_Compound))
    root.tags.append(entity_list("entities", entities))
    return to_bytes(root)


def write_region(path, chunks):
    """Write `{(x, z): raw nbt bytes}` into a new region file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()
    rf = region.RegionFile(path)
    try:
        for (x, z), raw in chunks.items():
            rf.write_blockdata(x, z, raw, compression=region.COMPRESSION_ZLIB)
    finally:
        rf.close()


def chunk_entities(path, x=0, z=0):
    rf = region.RegionFile(path)
    try:
        chunk = rf.get_chunk(x, z)
        return [(e["id"].value, tuple(p.value for p in e["Pos"]))
                for e in world_module.get_chunk_entity_list(chunk)]
    finally:
        rf.close()


def write_level_dat(world_path, spawn=(100, 70, -50)):
    root = nbt.NBTFile()
    root.name = ""
    data = nbt.TAG_Compound(name="Data")
    data.tags.append(nbt.TAG_String(name="LevelName", value="Position Test World"))
    if spawn is not None:
        for axis, value in zip("XYZ", spawn):
            data.tags.append(nbt.TAG_Int(name="Spawn" + axis, value=value))
    root.tags.append(data)
    os.makedirs(world_path, exist_ok=True)
    root.write_file(filename=os.path.join(world_path, "level.dat"))


def write_player(world_path, uuid, pos, motion=(0.0, 0.0, 0.0),
                 dimension="minecraft:overworld", folder=("playerdata",)):
    root = nbt.NBTFile()
    root.name = ""
    root.tags.append(double_list("Pos", pos))
    root.tags.append(double_list("Motion", motion))
    root.tags.append(nbt.TAG_String(name="Dimension", value=dimension))
    root.tags.append(nbt.TAG_Float(name="FallDistance", value=1.0e9))
    directory = os.path.join(world_path, *folder)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, uuid + ".dat")
    root.write_file(filename=path)
    return path


def run_cli(*args):
    return subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "regionfixer.py")] + list(args),
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
        # The native scanner hands these files back anyway; keep the CLI
        # tests independent of whether it has been built.
        env=dict(os.environ, REGIONFIXER_NO_NATIVE="1"),
    )


class PositionSanityTests(unittest.TestCase):

    def test_normal_positions_are_sane(self):
        self.assertTrue(is_position_sane(0.5, 64.0, -0.5))
        self.assertTrue(is_position_sane(29999999.0, -64.0, -29999999.0))

    def test_incident_position_is_not_sane(self):
        self.assertFalse(is_position_sane(*INCIDENT_POS))

    def test_nan_and_infinity_are_rejected_even_with_a_huge_bound(self):
        # A huge bound makes "merely very large" values pass, so these only
        # fail because of the NaN/infinity check itself.
        huge = 10 ** 30
        self.assertTrue(is_position_sane(1.0e9, 1.0e9, 0.0, max_abs_xz=huge, max_abs_y=huge))
        self.assertFalse(is_position_sane(math.nan, 64.0, 0.0, max_abs_xz=huge, max_abs_y=huge))
        self.assertFalse(is_position_sane(0.0, math.inf, 0.0, max_abs_xz=huge, max_abs_y=huge))
        self.assertFalse(is_position_sane(0.0, 64.0, -math.inf, max_abs_xz=huge, max_abs_y=huge))
        self.assertFalse(is_position_sane(None, 64.0, 0.0))

    def test_bounds(self):
        self.assertFalse(is_position_sane(30000001.0, 64.0, 0.0))
        self.assertFalse(is_position_sane(0.0, 20000001.0, 0.0))
        self.assertTrue(is_position_sane(30000001.0, 64.0, 0.0, max_abs_xz=40000000))

    def test_vanilla_chunk_limit_ignores_the_configured_bound(self):
        edge = 134217727 * 16
        self.assertTrue(is_position_sane(edge - 16.0, 0.0, 0.0, max_abs_xz=10 ** 12))
        self.assertFalse(is_position_sane(float(edge), 0.0, 0.0, max_abs_xz=10 ** 12))
        self.assertFalse(is_position_sane(0.0, 0.0, -float(edge), max_abs_xz=10 ** 12))


class PlayerPositionTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="regionfixer-player-pos-")

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def test_incident_player_is_reported(self):
        path = write_player(self.tempdir, INCIDENT_UUID, INCIDENT_POS)
        result = scan_data(ScannedDataFile(path), BOUND)
        self.assertEqual(result.status, c.DATAFILE_INVALID_POSITION)
        self.assertEqual(result.uuid, INCIDENT_UUID)
        self.assertEqual(result.position, INCIDENT_POS)
        self.assertEqual(result.dimension, "minecraft:overworld")
        self.assertIn(INCIDENT_UUID, result.oneliner_status)
        self.assertIn("1.8e+16", result.oneliner_status)

    def test_check_is_off_without_a_bound(self):
        path = write_player(self.tempdir, INCIDENT_UUID, INCIDENT_POS)
        self.assertEqual(scan_data(ScannedDataFile(path)).status, c.DATAFILE_OK)

    def test_healthy_player_is_ok(self):
        path = write_player(self.tempdir, "healthy", (10.5, 70.0, -3.2))
        self.assertEqual(scan_data(ScannedDataFile(path), BOUND).status, c.DATAFILE_OK)

    def test_nan_and_infinite_values(self):
        cases = {
            "nan-pos": dict(pos=(math.nan, 64.0, 0.0)),
            "inf-pos": dict(pos=(0.0, 64.0, math.inf)),
            "nan-motion": dict(pos=(0.0, 64.0, 0.0), motion=(0.0, math.nan, 0.0)),
            "inf-motion": dict(pos=(0.0, 64.0, 0.0), motion=(-math.inf, 0.0, 0.0)),
        }
        for name, kwargs in cases.items():
            path = write_player(self.tempdir, name, **kwargs)
            self.assertEqual(scan_data(ScannedDataFile(path), BOUND).status,
                             c.DATAFILE_INVALID_POSITION, name)

    def test_large_but_finite_motion_is_not_reported(self):
        # Vanilla already discards an oversized Motion on load.
        path = write_player(self.tempdir, "fast", (0.0, 64.0, 0.0), motion=(1.0e6, 0.0, 0.0))
        self.assertEqual(scan_data(ScannedDataFile(path), BOUND).status, c.DATAFILE_OK)

    def _scan_players(self, w):
        for dataset in (w.players, w.old_players):
            for path, scanned in list(dataset._set.items()):
                dataset._set[path] = scan_data(scanned, BOUND)

    def test_fix_resets_incident_player_to_spawn(self):
        write_level_dat(self.tempdir, spawn=(100, 70, -50))
        path = write_player(self.tempdir, INCIDENT_UUID, INCIDENT_POS,
                            motion=(3.0e8, math.nan, 1.0), dimension="minecraft:the_nether")
        healthy = write_player(self.tempdir, "healthy", (1.0, 2.0, 3.0))
        with open(healthy, "rb") as handle:
            healthy_bytes = handle.read()

        w = World(self.tempdir)
        self._scan_players(w)
        self.assertEqual(len(w.list_invalid_player_files()), 1)
        self.assertEqual(w.fix_player_positions(), 1)

        fixed = nbt.NBTFile(filename=path)
        self.assertEqual([t.value for t in fixed["Pos"]], [100.0, 70.0, -50.0])
        self.assertEqual([t.value for t in fixed["Motion"]], [0.0, 0.0, 0.0])
        self.assertEqual(fixed["Dimension"].value, "minecraft:overworld")
        self.assertEqual(fixed["FallDistance"].value, 0.0)

        backup = nbt.NBTFile(filename=path + ".bak")
        self.assertEqual(tuple(t.value for t in backup["Pos"]), INCIDENT_POS)

        # Untouched players stay byte for byte identical and get no backup.
        with open(healthy, "rb") as handle:
            self.assertEqual(handle.read(), healthy_bytes)
        self.assertFalse(os.path.exists(healthy + ".bak"))

        # A rescan now finds nothing.
        w = World(self.tempdir)
        self._scan_players(w)
        self.assertEqual(w.list_invalid_player_files(), [])

    def test_fix_uses_origin_when_level_dat_has_no_spawn_and_keeps_old_backups(self):
        write_level_dat(self.tempdir, spawn=None)
        path = write_player(self.tempdir, "nan", (math.nan, 64.0, 0.0),
                            folder=("players", "data"))
        open(path + ".bak", "wb").close()

        w = World(self.tempdir)
        self._scan_players(w)
        self.assertEqual(w.fix_player_positions(), 1)
        fixed = nbt.NBTFile(filename=path)
        self.assertEqual([t.value for t in fixed["Pos"]], [0.0, 64.0, 0.0])
        self.assertTrue(os.path.exists(path + ".bak.1"))
        self.assertEqual(os.path.getsize(path + ".bak"), 0)

    def test_fix_reads_the_modern_spawn_compound(self):
        root = nbt.NBTFile()
        root.name = ""
        data = nbt.TAG_Compound(name="Data")
        data.tags.append(nbt.TAG_String(name="LevelName", value="Modern spawn"))
        spawn = nbt.TAG_Compound(name="spawn")
        spawn_pos = nbt.TAG_Int_Array(name="pos")
        spawn_pos.value = [7, 80, 9]
        spawn.tags.append(spawn_pos)
        spawn.tags.append(nbt.TAG_String(name="dimension", value="minecraft:overworld"))
        data.tags.append(spawn)
        root.tags.append(data)
        root.write_file(filename=os.path.join(self.tempdir, "level.dat"))

        self.assertEqual(World(self.tempdir).get_spawn(),
                         ((7.0, 80.0, 9.0), "minecraft:overworld"))


class EntityPositionTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="regionfixer-entity-pos-")
        self.path = os.path.join(self.tempdir, "entities", "r.0.0.mca")

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def scan(self, bound=BOUND, entity_limit=300):
        return scan_region_file(ScannedRegionFile(self.path, folder="entities"),
                                entity_limit, False, bound)

    def test_entities_file_zombie_is_reported_and_fixed_surgically(self):
        write_region(self.path, {(0, 0): entities_chunk([
            entity("minecraft:cow", (3.5, 64.0, 7.5)),
            entity("minecraft:zombie", INCIDENT_POS),
        ])})

        scanned = self.scan()
        self.assertEqual(scanned[(0, 0)], (2, c.CHUNK_ENTITY_OUT_OF_BOUNDS))
        self.assertEqual(scanned.bad_entities[(0, 0)], [("minecraft:zombie", INCIDENT_POS)])
        self.assertIn("Bad entity: minecraft:zombie", scanned.summary())

        self.assertEqual(scanned.fix_problematic_chunks(c.CHUNK_ENTITY_OUT_OF_BOUNDS, 300, BOUND), 1)
        self.assertEqual(chunk_entities(self.path), [("minecraft:cow", (3.5, 64.0, 7.5))])
        self.assertEqual(self.scan()[(0, 0)], (1, c.CHUNK_OK))

    def test_check_is_off_without_a_bound(self):
        write_region(self.path, {(0, 0): entities_chunk([entity("minecraft:zombie", INCIDENT_POS)])})
        self.assertEqual(self.scan(bound=None)[(0, 0)], (1, c.CHUNK_OK))

    def test_legacy_embedded_entities_nan_and_infinity(self):
        self.path = os.path.join(self.tempdir, "region", "r.0.0.mca")
        write_region(self.path, {
            (0, 0): legacy_level_chunk([entity("minecraft:pig", (1.0, 64.0, 1.0)),
                                        entity("minecraft:zombie", (math.nan, 64.0, 1.0))], 0, 0),
            (1, 0): legacy_level_chunk([entity("minecraft:skeleton", (math.inf, 64.0, 1.0))], 1, 0),
            (2, 0): legacy_level_chunk([entity("minecraft:pig", (40.0, 64.0, 1.0))], 2, 0),
        })
        scanned = self.scan()
        self.assertEqual(scanned[(0, 0)][c.TUPLE_STATUS], c.CHUNK_ENTITY_OUT_OF_BOUNDS)
        self.assertEqual(scanned[(1, 0)][c.TUPLE_STATUS], c.CHUNK_ENTITY_OUT_OF_BOUNDS)
        self.assertEqual(scanned[(2, 0)][c.TUPLE_STATUS], c.CHUNK_OK)

        scanned.fix_problematic_chunks(c.CHUNK_ENTITY_OUT_OF_BOUNDS, 300, BOUND)
        self.assertEqual(chunk_entities(self.path, 0, 0), [("minecraft:pig", (1.0, 64.0, 1.0))])
        self.assertEqual(chunk_entities(self.path, 1, 0), [])

    def test_modern_level_chunk_entities_list(self):
        self.path = os.path.join(self.tempdir, "region", "r.0.0.mca")
        write_region(self.path, {(0, 0): modern_level_chunk([entity("minecraft:zombie", INCIDENT_POS)])})
        self.assertEqual(self.scan()[(0, 0)][c.TUPLE_STATUS], c.CHUNK_ENTITY_OUT_OF_BOUNDS)

    def test_wrong_located_is_not_hidden(self):
        write_region(self.path, {(0, 0): entities_chunk([entity("minecraft:zombie", INCIDENT_POS)], x=5)})
        self.assertEqual(self.scan()[(0, 0)][c.TUPLE_STATUS], c.CHUNK_WRONG_LOCATED)

    def test_fix_reports_a_chunk_that_is_still_crowded(self):
        write_region(self.path, {(0, 0): entities_chunk([
            entity("minecraft:cow", (1.0, 64.0, 1.0)),
            entity("minecraft:cow", (2.0, 64.0, 1.0)),
            entity("minecraft:zombie", INCIDENT_POS),
        ])})
        scanned = self.scan(entity_limit=1)
        # Out of bounds wins over too many entities, the count is kept.
        self.assertEqual(scanned[(0, 0)], (3, c.CHUNK_ENTITY_OUT_OF_BOUNDS))
        scanned.fix_problematic_chunks(c.CHUNK_ENTITY_OUT_OF_BOUNDS, 1, BOUND)
        self.assertEqual(scanned[(0, 0)], (2, c.CHUNK_TOO_MANY_ENTITIES))

    def test_bad_passenger_flags_its_vehicle(self):
        jockey = entity("minecraft:chicken", (1.0, 64.0, 1.0),
                        passengers=[entity("minecraft:zombie", (1.0, 64.0, math.nan))])
        write_region(self.path, {(0, 0): entities_chunk([jockey])})
        scanned = self.scan()
        self.assertEqual(scanned.bad_entities[(0, 0)][0][0], "minecraft:chicken")


class EntityTypeSweepTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="regionfixer-sweep-")
        write_level_dat(self.tempdir)
        self.entities_path = os.path.join(self.tempdir, "entities", "r.0.0.mca")
        self.region_path = os.path.join(self.tempdir, "region", "r.0.0.mca")
        write_region(self.entities_path, {(0, 0): entities_chunk([
            entity("minecraft:zombie", (1.0, 64.0, 1.0)),
            entity("minecraft:wolf", (2.0, 64.0, 1.0), custom_name='"Rex"'),
            entity("minecraft:villager", (3.0, 64.0, 1.0)),
            entity("minecraft:zombie", (4.0, 64.0, 1.0), custom_name='"Bob"'),
        ])})
        write_region(self.region_path, {(0, 0): legacy_level_chunk([
            entity("minecraft:skeleton", (5.0, 64.0, 1.0), tags=["keep"]),
            entity("minecraft:chicken", (6.0, 64.0, 1.0),
                   passengers=[entity("minecraft:zombie", (6.0, 64.0, 1.0))]),
        ])})

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def sweep(self, **kwargs):
        return world_module.sweep_entity_types(World(self.tempdir).regionsets,
                                               resolve_entity_types("hostile"), **kwargs)

    def ids(self, path):
        return [entity_id for entity_id, _pos in chunk_entities(path)]

    def test_dry_run_writes_nothing(self):
        with open(self.entities_path, "rb") as handle:
            before = handle.read()
        report = self.sweep()
        self.assertEqual(report.removed, {"minecraft:zombie": 2})
        with open(self.entities_path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertIn("DRY RUN", report.summary(False))

    def test_default_run_removes_only_the_plain_zombies(self):
        report = self.sweep(apply=True)
        self.assertEqual(self.ids(self.entities_path),
                         ["minecraft:wolf", "minecraft:villager", "minecraft:zombie"])
        self.assertEqual(report.skipped_named, 1)
        self.assertEqual(report.skipped_tagged, 1)
        self.assertEqual(report.chunks_changed, 2)

        # The chicken jockey loses its rider; the tagged skeleton stays.
        rf = region.RegionFile(self.region_path)
        try:
            chunk = rf.get_chunk(0, 0)
            entities = world_module.get_chunk_entity_list(chunk)
            self.assertEqual([e["id"].value for e in entities],
                             ["minecraft:skeleton", "minecraft:chicken"])
            self.assertEqual(len(entities[1]["Passengers"]), 0)
        finally:
            rf.close()

        summary = report.summary(True, self.tempdir)
        self.assertIn("minecraft:zombie", summary)
        self.assertIn(os.path.join("entities", "r.0.0.mca"), summary)

    def test_include_named_and_tagged(self):
        self.sweep(apply=True, include_named=True, include_tagged=True)
        self.assertEqual(self.ids(self.entities_path),
                         ["minecraft:wolf", "minecraft:villager"])
        self.assertEqual(self.ids(self.region_path), ["minecraft:chicken"])

    def test_presets(self):
        ids = resolve_entity_types("hostile, Enderman ,minecraft:custom_mob")
        self.assertTrue(HOSTILE <= ids)
        self.assertIn("minecraft:enderman", ids)
        self.assertIn("minecraft:custom_mob", ids)
        self.assertEqual(resolve_entity_types("zombie"), {"minecraft:zombie"})
        for valuable in ("minecraft:villager", "minecraft:wolf", "minecraft:cat",
                         "minecraft:horse", "minecraft:item", "minecraft:item_frame",
                         "minecraft:armor_stand", "minecraft:painting", "minecraft:boat",
                         "minecraft:chest_minecart", "minecraft:ender_dragon",
                         "minecraft:iron_golem", "minecraft:zombified_piglin"):
            self.assertNotIn(valuable, HOSTILE)


class PositionCliTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="regionfixer-pos-cli-")
        write_level_dat(self.tempdir, spawn=(0, 80, 0))
        self.player = write_player(self.tempdir, INCIDENT_UUID, INCIDENT_POS)
        self.entities_path = os.path.join(self.tempdir, "entities", "r.0.0.mca")
        write_region(self.entities_path, {(0, 0): entities_chunk([
            entity("minecraft:cow", (1.0, 64.0, 1.0)),
            entity("minecraft:zombie", INCIDENT_POS),
        ])})

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def test_default_scan_is_unchanged(self):
        result = run_cli(self.tempdir)
        self.assertEqual(result.returncode, c.RV_OK, msg=result.stdout)

    def test_checks_report_and_log(self):
        result = run_cli("--check-player-position", "--check-entity-position",
                         "--log", "-", self.tempdir)
        self.assertEqual(result.returncode, c.RV_BAD_WORLD, msg=result.stdout)
        self.assertIn("Invalid player positions      1", result.stdout)
        self.assertIn("Entity out of bounds  1", result.stdout)
        self.assertIn(INCIDENT_UUID, result.stdout)
        self.assertIn("Bad entity: minecraft:zombie", result.stdout)

    def test_fix_options_repair_both(self):
        result = run_cli("--fix-player-position", "--fix-entity-position", self.tempdir)
        self.assertIn("Repaired 1 player files", result.stdout)
        self.assertEqual(chunk_entities(self.entities_path), [("minecraft:cow", (1.0, 64.0, 1.0))])
        self.assertEqual([t.value for t in nbt.NBTFile(filename=self.player)["Pos"]],
                         [0.0, 80.0, 0.0])
        rescan = run_cli("--check-player-position", "--check-entity-position", self.tempdir)
        self.assertEqual(rescan.returncode, c.RV_OK, msg=rescan.stdout)

    def test_sweep_cli_dry_run_then_apply(self):
        dry = run_cli("--remove-entity-types", "hostile", self.tempdir)
        self.assertIn("DRY RUN", dry.stdout)
        self.assertIn("Would remove 1 entity in 1 chunk.", dry.stdout)
        self.assertEqual(len(chunk_entities(self.entities_path)), 2)

        run_cli("--remove-entity-types", "hostile", "--apply-entity-removal", self.tempdir)
        self.assertEqual(chunk_entities(self.entities_path), [("minecraft:cow", (1.0, 64.0, 1.0))])

    def test_apply_needs_types(self):
        result = run_cli("--apply-entity-removal", self.tempdir)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
