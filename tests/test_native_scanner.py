"""Parity tests between the native scanner and the pure Python scanner.

Every test builds a small world, scans it twice (once with the native scanner
forced off) and asserts that both scans agree chunk by chunk. The tests are
skipped when the native module has not been built.
"""

import io
import os
import shutil
import struct
import tempfile
import unittest
import zlib

from nbt import nbt
from regionfixer_core import native, scan, world


SECTOR = 4096


def chunk_bytes(x, z, data_version=3465, entities=2, extra=None):
    """Build a modern (1.18+) level chunk."""
    root = nbt.NBTFile()
    root.name = ""
    root.tags.append(nbt.TAG_Int(name="DataVersion", value=data_version))
    root.tags.append(nbt.TAG_Int(name="xPos", value=x))
    root.tags.append(nbt.TAG_Int(name="zPos", value=z))
    root.tags.append(nbt.TAG_List(name="sections", type=nbt.TAG_Compound))
    entity_list = nbt.TAG_List(name="entities", type=nbt.TAG_Compound)
    for _ in range(entities):
        entity = nbt.TAG_Compound()
        entity.tags.append(nbt.TAG_String(name="id", value="minecraft:cow"))
        entity_list.tags.append(entity)
    root.tags.append(entity_list)
    if extra is not None:
        root.tags.append(extra)
    buffer = io.BytesIO()
    root.write_file(buffer=buffer)
    return buffer.getvalue()


def legacy_chunk_bytes(x, z, entities=1, with_entities_tag=True):
    """Build a pre 1.18 chunk, where everything lives under Level."""
    root = nbt.NBTFile()
    root.name = ""
    root.tags.append(nbt.TAG_Int(name="DataVersion", value=1976))
    level = nbt.TAG_Compound(name="Level")
    level.tags.append(nbt.TAG_Int(name="xPos", value=x))
    level.tags.append(nbt.TAG_Int(name="zPos", value=z))
    if with_entities_tag:
        entity_list = nbt.TAG_List(name="Entities", type=nbt.TAG_Compound)
        for _ in range(entities):
            entity = nbt.TAG_Compound()
            entity.tags.append(nbt.TAG_String(name="id", value="minecraft:pig"))
            entity_list.tags.append(entity)
        level.tags.append(entity_list)
    root.tags.append(level)
    buffer = io.BytesIO()
    root.write_file(buffer=buffer)
    return buffer.getvalue()


def write_region(path, rx, rz, payloads):
    """Write a region file from a `(x, z) -> (bytes, compression)` mapping."""
    locations = bytearray(SECTOR)
    timestamps = bytearray(SECTOR)
    body = bytearray()
    sector = 2
    for (x, z), (payload, compression) in sorted(payloads.items()):
        blob = struct.pack(">IB", len(payload) + 1, compression) + payload
        blob += b"\0" * ((-len(blob)) % SECTOR)
        used = len(blob) // SECTOR
        i = 4 * (x + z * 32)
        locations[i:i + 4] = struct.pack(">IB", sector, used)[1:]
        timestamps[i:i + 4] = struct.pack(">I", 1700000000)
        body += blob
        sector += used
    with open(path, "wb") as handle:
        handle.write(locations)
        handle.write(timestamps)
        handle.write(body)


def write_level_dat(world_path):
    root = nbt.NBTFile()
    root.name = ""
    data = nbt.TAG_Compound(name="Data")
    data.tags.append(nbt.TAG_String(name="LevelName", value="native test"))
    root.tags.append(data)
    root.write_file(filename=os.path.join(world_path, "level.dat"))


def scan_results(world_path, use_native, entity_limit=10):
    """Scan a world and return `{filename: (region_status, chunk_dict)}`."""
    previous = os.environ.get("REGIONFIXER_NO_NATIVE")
    os.environ["REGIONFIXER_NO_NATIVE"] = "0" if use_native else "1"
    try:
        worlds, _ = world.parse_paths([world_path])
        regionset = worlds[0].regionsets[0]
        scanner = scan.make_regionset_scanner(regionset, 1, entity_limit, False)
        expected = "NativeRegionsetScanner" if use_native else "AsyncRegionsetScanner"
        assert type(scanner).__name__ == expected, type(scanner).__name__
        scanner.scan()
        results = {}
        while not scanner.finished:
            scanned = scanner.get_last_result()
            if scanned is None:
                scanner.sleep()
                continue
            results[scanned.filename] = (scanned.status, dict(scanned._chunks))
        return results
    finally:
        if previous is None:
            os.environ.pop("REGIONFIXER_NO_NATIVE", None)
        else:
            os.environ["REGIONFIXER_NO_NATIVE"] = previous


@unittest.skipUnless(native.available(),
                     "the native scanner has not been built, run ./build_native.sh")
class NativeParityTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regionfixer-native-")
        self.region_dir = os.path.join(self.tmp, "region")
        os.makedirs(self.region_dir)
        write_level_dat(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_parity(self, entity_limit=10):
        native_results = scan_results(self.tmp, True, entity_limit)
        python_results = scan_results(self.tmp, False, entity_limit)
        self.assertEqual(set(native_results), set(python_results))
        for filename in native_results:
            self.assertEqual(native_results[filename], python_results[filename],
                             "results differ for %s" % filename)
        return native_results

    def test_healthy_modern_chunks(self):
        payloads = {}
        for x in range(4):
            for z in range(4):
                raw = chunk_bytes(x, z)
                payloads[(x, z)] = (zlib.compress(raw), 2)
        write_region(os.path.join(self.region_dir, "r.0.0.mca"), 0, 0, payloads)

        results = self.assert_parity()
        status, chunks = results["r.0.0.mca"]
        self.assertEqual(status, 100)  # REGION_OK
        self.assertEqual(len(chunks), 16)
        self.assertTrue(all(tup[1] == 0 for tup in chunks.values()))

    def test_problem_chunks(self):
        payloads = {
            # Healthy.
            (0, 0): (zlib.compress(chunk_bytes(0, 0)), 2),
            # Stored under the wrong coordinates.
            (1, 0): (zlib.compress(chunk_bytes(9, 9)), 2),
            # More entities than the limit.
            (2, 0): (zlib.compress(chunk_bytes(2, 0, entities=50)), 2),
            # Garbled compressed data.
            (3, 0): (zlib.compress(chunk_bytes(3, 0))[:12] + b"\xff" * 30, 2),
            # Uncompressed payload.
            (4, 0): (chunk_bytes(4, 0), 3),
            # Gzip payload.
            (5, 0): (_gzip(chunk_bytes(5, 0)), 1),
            # Pre 1.18 layout.
            (6, 0): (zlib.compress(legacy_chunk_bytes(6, 0)), 2),
            # Pre 1.18 layout without the mandatory Entities tag.
            (7, 0): (zlib.compress(legacy_chunk_bytes(7, 0, with_entities_tag=False)), 2),
        }
        write_region(os.path.join(self.region_dir, "r.0.0.mca"), 0, 0, payloads)

        results = self.assert_parity()
        _, chunks = results["r.0.0.mca"]
        self.assertEqual(chunks[(0, 0)][1], 0)   # CHUNK_OK
        self.assertEqual(chunks[(1, 0)][1], 2)   # CHUNK_WRONG_LOCATED
        self.assertEqual(chunks[(2, 0)][1], 3)   # CHUNK_TOO_MANY_ENTITIES
        self.assertEqual(chunks[(3, 0)][1], 1)   # CHUNK_CORRUPTED
        self.assertEqual(chunks[(4, 0)][1], 0)
        self.assertEqual(chunks[(5, 0)][1], 0)
        self.assertEqual(chunks[(6, 0)][1], 0)
        self.assertEqual(chunks[(7, 0)][1], 5)   # CHUNK_MISSING_ENTITIES_TAG

    def test_truncated_region_file(self):
        path = os.path.join(self.region_dir, "r.0.0.mca")
        with open(path, "wb") as handle:
            handle.write(b"\0" * 100)

        results = self.assert_parity()
        status, chunks = results["r.0.0.mca"]
        self.assertEqual(status, 101)  # REGION_TOO_SMALL
        self.assertEqual(chunks, {})

    def test_empty_region_file(self):
        open(os.path.join(self.region_dir, "r.0.0.mca"), "wb").close()

        results = self.assert_parity()
        status, chunks = results["r.0.0.mca"]
        self.assertEqual(status, 100)  # REGION_OK
        self.assertEqual(chunks, {})

    def test_lz4_chunk_falls_back_to_python(self):
        before = len(native.FALLBACK_LOG)
        payloads = {
            (0, 0): (lz4_java_stream(chunk_bytes(0, 0)), 4),
            (1, 0): (zlib.compress(chunk_bytes(1, 0)), 2),
        }
        write_region(os.path.join(self.region_dir, "r.0.0.mca"), 0, 0, payloads)

        results = self.assert_parity()
        _, chunks = results["r.0.0.mca"]
        self.assertEqual(chunks[(0, 0)][1], 0)
        self.assertEqual(chunks[(1, 0)][1], 0)
        # The lz4 chunk is only readable by the Python scanner.
        self.assertGreater(len(native.FALLBACK_LOG), before)


def lz4_java_stream(data):
    """Wrap a payload in an uncompressed lz4-java block stream."""
    from nbt import lz4_java

    # 64 KiB block size gives a token of 0x16.
    token = 0x16
    checksum = lz4_java.xxhash32(data, lz4_java.XXHASH_SEED) & lz4_java.CHECKSUM_MASK
    block = (lz4_java.MAGIC + bytes([token]) +
             struct.pack("<III", len(data), len(data), checksum) + data)
    terminal = lz4_java.MAGIC + bytes([token]) + struct.pack("<III", 0, 0, 0)
    return block + terminal


def _gzip(payload):
    import gzip
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as handle:
        handle.write(payload)
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
