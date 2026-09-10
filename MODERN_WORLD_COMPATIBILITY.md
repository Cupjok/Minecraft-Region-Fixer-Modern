# Modern world compatibility

Minecraft Region Fixer Modern extends the original Region Fixer for current
Java Edition world storage while retaining legacy layouts and the original
command-line workflow.

## World layout support

Modern worlds can store dimensions under:

`dimensions/<namespace>/<dimension>/`

This fork discovers `region`, `poi`, and `entities` folders in that layout and
still discovers the older locations.

Canonical vanilla mappings are used when matching backups:

- root `region` / `poi` / `entities` -> `minecraft/overworld`
- `DIM-1` -> `minecraft/the_nether`
- `DIM1` -> `minecraft/the_end`

That allows modern and legacy worlds/backups to be matched across the storage
layout migration.

## Other modern storage changes covered

- UUID player data under `players/data`, with legacy `playerdata` retained.
- Recursively namespaced `.dat` files under the root `data/` folder.
- Dimension-specific `.dat` files under namespaced dimension `data/` folders.
- `last_id.dat` is handled as raw uncompressed NBT alongside legacy
  `idcounts.dat`.

## Region-file support

The bundled region reader supports:

- compression 1: gzip
- compression 2: zlib
- compression 3: uncompressed
- compression 4: lz4-java `LZ4Block` streams
- external oversized `c.<chunkX>.<chunkZ>.mcc` payloads

LZ4 decoding supports both raw and compressed LZ4Block blocks and validates the
full 32-bit XXH32 checksum used by lz4-java.

Oversized repaired chunks are written to `.mcc` sidecars when they exceed the
Anvil location-table sector limit. Region deletion/replacement also removes or
copies the associated sidecars.

## Compatibility fixes included

While adding regression coverage, older repair issues were also corrected:

- gzip chunk writes open the gzip stream in write mode so replacement of older
  gzip-compressed chunks works correctly;
- chunk replacement no longer overwrites the requested problem status after
  processing one chunk;
- cached backup region scans are separated by dimension/type so identically
  numbered regions in different dimensions do not collide.

## Position checks and entity sweeps

The opt-in position checks (`--check-player-position`, `--check-entity-position`)
and the `--remove-entity-types` sweep read entities from every storage place Region
Fixer knows about:

- `Level.Entities` in pre-1.17 level chunks, and in 1.17 chunks that still embed them;
- the root `entities` list of 1.18+ level chunks, when present;
- `Entities` in the 1.17+ `entities/*.mca` files.

Player files are read from `playerdata/`, `players/data/` and the old `players/`
folder. `--fix-player-position` reads the spawn from either `Data.SpawnX/Y/Z` or the
1.21.9+ `Data.spawn` compound, and writes `Dimension` in the form the file already
uses (string ids, or the pre-1.16 integer 0).

## Scan reporting

The terminal scan report now provides a detailed summary of file counts, chunk
health, region health, player/data health, and per-dimension results. The
existing `--log` option remains the source for exact problematic chunk/file
locations, keeping large-world console output readable.

## CLI compatibility

The existing Region Fixer options are intentionally retained. `--version` is
added, but existing options are not renamed or removed.

A normal scan remains:

```bash
python regionfixer.py "/path/to/world"
```

On Windows, `RegionFixer.bat` can be used as a small launcher or as a drag-and-
drop scan target.

## Validation

Run all compatibility tests with:

```bash
python -m unittest discover -s tests -v
```

The suite covers legacy and modern layouts, old and new region compression,
LZ4, `.mcc` chunks, cross-layout backup matching/replacement, map-id NBT files,
scan-summary compatibility, and end-to-end CLI scans.

Always keep a separate untouched backup before using destructive repair/delete
options on a real world.
