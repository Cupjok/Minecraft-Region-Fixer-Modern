=============================
Minecraft Region Fixer Modern
=============================

An unofficial maintained compatibility fork of Alejandro Aguilera's
``Minecraft Region Fixer``. It keeps the original command-line behavior and
repair workflow while extending world discovery and region-file handling for
current Minecraft Java Edition worlds.

The original project is:
https://github.com/Fenixin/Minecraft-Region-Fixer

What this fork is trying to preserve
====================================

Compatibility comes first. Existing Region Fixer commands and repair options
remain available, and legacy world layouts continue to be supported. Modern
support is added underneath the existing scanner rather than replacing it with
a new workflow.

The compatibility test suite covers both old-style worlds and modern worlds.
Before a release, both sets of tests should pass.

Modern Minecraft support
========================

This fork currently adds support for:

* Namespaced default and custom dimension storage under
  ``dimensions/<namespace>/<dimension>``.
* Cross-layout backup matching between legacy ``region``/``DIM-1``/``DIM1``
  folders and the namespaced Overworld/Nether/End locations.
* Player UUID data under ``players/data`` while retaining ``playerdata``.
* Namespaced root and dimension-specific ``data`` folders.
* Region compression IDs 1 (gzip), 2 (zlib), 3 (uncompressed), and 4
  (Minecraft's LZ4 block stream).
* External oversized chunk payloads stored as ``c.<x>.<z>.mcc`` files.
* Reading, deleting, replacing, and writing external oversized chunks.

Older Minecraft support
=======================

The original layouts remain supported, including:

* Overworld regions in ``region``.
* Nether regions in ``DIM-1``.
* End regions in ``DIM1``.
* Legacy ``playerdata`` and old ``players`` data files.
* Traditional gzip and zlib Anvil chunk compression.
* Direct scanning of individual ``.mca`` region files.
* Existing backup replacement, deletion, fixing, logging, verbose scanning,
  entity limits, and multiprocessing options.

Native scanner
==============

Scanning is the slow part of Region Fixer, and this fork ships an optional
scanning core written in Rust. It reads the same bytes as the Python scanner
but skips over the NBT tags it does not need instead of building a full tag
tree for every chunk, and it scans region files on a thread pool inside a
single process.

Building it needs a Rust toolchain (https://rustup.rs)::

    ./build_native.sh

The script compiles the crate in ``native/`` and copies the extension module
next to ``regionfixer.py``. Region Fixer picks it up automatically; if it is
not there, the pure Python scanner is used and nothing else changes.

On a 702 MB world of 16 region files (14,000 chunks) on a 14-core machine, a
full scan takes 0.21 s with the native scanner against 9.08 s for the previous
single-process default, and 2.89 s for the Python scanner using every core.

The native scanner is conservative. Whenever a region file holds something it
cannot reproduce exactly, such as an lz4 chunk or a tag of an unexpected type,
it hands that one file back to the Python scanner, so results are identical
either way. The test suite scans the same worlds with both and compares them
chunk by chunk.

To turn it off, pass ``--no-native`` or set ``REGIONFIXER_NO_NATIVE=1``.

Position sanity checks
======================

A player or mob whose stored ``Pos`` is NaN, infinite or absurdly large can
crash a server every time it is loaded, typically with ``Trying to create
chunk out of reasonable bounds``. Two opt-in checks find them::

    python regionfixer.py --check-player-position --check-entity-position "/path/to/world"

* ``--check-player-position`` (``--cpp``) marks player files whose ``Pos``
  fails the check or whose ``Motion`` is not finite as ``Invalid player
  position``. The summary and ``--log`` show the UUID, ``Pos`` and
  ``Dimension``.
* ``--check-entity-position`` (``--cep``) marks chunks in ``region`` and
  ``entities`` files that hold such an entity as ``Entity out of bounds``.
  ``--log`` lists each offending entity. Region files with entities are
  scanned by the Python scanner while this is on.
* ``--position-bound`` (``--pb``) sets the largest accepted absolute X/Z
  (default 30,000,000). Y is limited to 20,000,000, and a chunk coordinate of
  134,217,727 or more is always rejected.

Repairs:

* ``--fix-entity-position`` (``--fep``) removes only the offending entities
  and leaves the rest of the chunk alone.
* ``--replace-entity-position`` (``--reob``) replaces those chunks from
  ``--backups`` instead.
* ``--fix-player-position`` (``--fpp``) moves the player to the world spawn
  from ``level.dat`` and zeros their ``Motion``. The original file is kept as
  ``<uuid>.dat.bak``.

The fix and replace options turn on the matching check by themselves.

Removing entity types
=====================

``/kill`` only reaches loaded chunks. ``--remove-entity-types`` reads every
chunk of every region and entities file, loaded or not, and removes entities
by id. It takes comma separated ids and/or the ``hostile`` preset (alias
``monsters``), and the two combine::

    python regionfixer.py --remove-entity-types hostile "/path/to/world"
    python regionfixer.py --remove-entity-types hostile,minecraft:enderman --apply-entity-removal "/path/to/world"

Without ``--apply-entity-removal`` nothing is written; the run prints what
would be removed, per entity id and per region file. Entities with a
``CustomName`` or ``Tags`` are kept unless ``--include-named`` or
``--include-tagged`` is given. The ``hostile`` preset never contains
villagers, pets, mounts, item entities, item frames, armor stands, paintings,
boats or minecarts; it is a plain list in
``regionfixer_core/entity_presets.py``.

Stop the server before using any of these repairs.

Worker count
============

``--processes``/``-p`` now defaults to ``0``, which means one worker per
logical CPU core. Pass ``-p 1`` for the old single-worker behavior.

Detailed scan summary
=====================

A normal world scan finishes with a structured summary showing:

* Overall ``CLEAN`` or ``ISSUES FOUND`` result.
* World path and detected storage layout.
* ``DataVersion`` when it is present in ``level.dat``.
* Region/Level, POI, Entities, player, and world-data file counts.
* Total and healthy chunks plus every chunk problem category.
* Total and healthy region files plus every region problem category.
* Player/data-file health.
* A per-dimension and per-region-type breakdown with region, chunk, and issue
  counts.

The summary is intentionally bounded and readable even for very large worlds.
Exact problematic chunk/file locations remain available through the original
``--log`` option::

    python regionfixer.py --log problems.txt "/path/to/world"

Requirements
============

Python 3 is required. No third-party Python packages are required for the
command-line scanner included in this repository.

Windows quick start
===================

The easiest Windows method is to drag a Minecraft world folder onto
``RegionFixer.bat``. The launcher finds Python 3 and forwards the world path and
any additional arguments to the original ``regionfixer.py`` command. After the
scan finishes, the launcher waits for a keypress so the detailed summary remains
visible instead of the window closing immediately.

You can also open Terminal/PowerShell in this folder and run::

    python .\regionfixer.py "C:\path\to\world"

or, on systems with the Python Launcher::

    py -3 .\regionfixer.py "C:\path\to\world"

Command-line usage
==================

The command-line interface intentionally remains compatible with Region Fixer.
Read the complete built-in help with::

    python regionfixer.py --help

Show the fork version with::

    python regionfixer.py --version

A normal scan is non-destructive::

    python regionfixer.py "/path/to/world"

Existing repair/delete/backup options continue to work. Review the scan output
before using options that modify a world.

Testing
=======

Run the complete compatibility suite with::

    python -m unittest discover -s tests -v

The GitHub Actions workflow runs the same test suite on Windows and Linux using
multiple Python versions.

Release policy
==============

Changes to region parsing, chunk classification, world discovery, replacement,
or deletion should include a regression test. New-format support should not
remove an older format merely because Minecraft no longer writes it.

See ``CHANGELOG.md``, ``MODERN_WORLD_COMPATIBILITY.md``, and
``RELEASE_CHECKLIST.md`` for the current fork changes and release checks.

License and attribution
=======================

Minecraft Region Fixer is free software distributed under the GNU General
Public License, version 3 or later. See ``COPYING.txt``.

The original Region Fixer code and copyright notices remain attributed to
Alejandro Aguilera (Fenixin) and the original contributors. This repository is
an unofficial compatibility fork and is not affiliated with Mojang or
Microsoft. See ``FORK_NOTICE.md`` for details.

Warning
=======

Region Fixer can delete or replace chunks and region files when destructive
options are explicitly selected. Always keep a separate untouched backup of a
world before attempting repairs.
