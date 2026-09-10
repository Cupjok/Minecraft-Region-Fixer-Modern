# Changelog

## 0.5.0 - Native scanner

### Added

- Optional native (Rust) scanning core in `native/`, built with `./build_native.sh`,
  that memory maps region files, decompresses chunks with libdeflate, skims the NBT
  instead of building a tag tree, and scans region files on a thread pool.
- `--no-native` and `REGIONFIXER_NO_NATIVE=1` to force the pure Python scanner.
- Parity tests in `tests/test_native_scanner.py` that scan the same world with both
  scanners and compare every chunk result.

### Changed

- `--processes`/`-p` now defaults to `0`, meaning one worker per logical CPU core.
  Pass `-p 1` for the previous single-worker behavior. The GUI field defaults to `0`
  as well.
- The startup banner reports which scanner and how many workers are in use.

### Fixed

- When `--delete-entities` empties a crowded chunk, the "too many entities" counter is
  no longer left incremented for the repaired chunk. This applies to the native
  scanner path.

## 0.4.1 - Windows launcher visibility fix

### Fixed

- `RegionFixer.bat` now keeps its window open after a drag-and-drop or batch-file scan so the detailed scan summary can be read.
- The launcher preserves and displays Region Fixer's exit code before waiting for a keypress.
- Direct `python regionfixer.py ...` command-line behavior is unchanged.

## 0.4.0 - Modern world compatibility

First release of the modern compatibility fork. The goal of this release is to
extend the existing Region Fixer without changing its normal repair workflow.

### Added

- Namespaced dimension discovery under `dimensions/<namespace>/<dimension>/`.
- Canonical dimension matching so modern worlds can use backups made with the
  legacy Overworld, Nether, and End directory layout, and vice versa.
- `players/data` discovery while retaining legacy player-data paths.
- Recursive namespaced `.dat` discovery in the root `data` tree.
- Dimension-specific `.dat` discovery under namespaced dimension `data` trees.
- Region compression ID 3 (uncompressed) support.
- Region compression ID 4 (Minecraft LZ4 block stream) support.
- External oversized `.mcc` chunk support for reads, writes, deletes, and full
  region replacements.
- `last_id.dat` raw-NBT handling alongside legacy `idcounts.dat`.
- Detailed terminal scan summary with file, chunk, region, player/data, and
  per-dimension health information.
- `--version` command-line option.
- Windows `RegionFixer.bat` launcher with drag-and-drop world support.
- Regression tests for legacy world layouts and traditional gzip/zlib region
  compression.
- Tests for modern dimension layouts, LZ4, uncompressed chunks, `.mcc`
  sidecars, and summary/API compatibility.
- GitHub Actions compatibility test workflow.

### Fixed

- Modern worlds no longer appear to have no region data solely because their
  dimensions are stored outside the legacy world-root locations.
- Backup replacement no longer reuses a stale RegionSet when a backup lacks the
  requested matching dimension/type.
- Cross-dimension chunk replacement no longer overwrites the requested problem
  status or collides cached region scans with another dimension at the same
  region coordinates.
- Legacy gzip chunk writes now use a writable gzip stream.
- Full region deletion/replacement now handles matching external `.mcc`
  sidecars to avoid orphaning or losing oversized chunk payloads.

### Compatibility promise

The original command-line repair options remain in place. Legacy
`region`/`DIM-1`/`DIM1` worlds and gzip/zlib chunks remain supported and are
covered by regression tests. Human-readable reporting is improved while the
programmatic `generate_report(False)` return shape remains compatible.
