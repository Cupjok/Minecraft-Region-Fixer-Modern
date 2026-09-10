#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
#   Region Fixer.
#   Fix your region files with a backup copy of your Minecraft world.
#   Copyright (C) 2020  Alejandro Aguilera (Fenixin)
#   https://github.com/Fenixin/Minecraft-Region-Fixer
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

""" Bridge to the native (Rust) region scanner.

The pure Python scanner builds a complete NBT tree for every chunk of every
region file, which is by far the slowest part of a scan. The native scanner in
``native/`` reads the same bytes but only extracts the handful of tags Region
Fixer actually looks at, and it spreads region files over a thread pool inside
a single process.

The native scanner is deliberately conservative: whenever a region file holds
something it cannot reproduce exactly (an lz4 chunk, a tag of an unexpected
type, an unrecognised chunk layout) it reports a fallback and this module
rescans that one file with the original Python scanner. The results of a
native scan are therefore identical to the results of a Python scan.

Set ``REGIONFIXER_NO_NATIVE=1`` in the environment to disable it entirely.
"""


import logging
import os
from time import sleep, time

import regionfixer_core.constants as c


try:
    import regionfixer_native as _native
except ImportError:  # pragma: no cover - depends on the build
    _native = None


#: Region files handed back to the Python scanner, for reporting.
FALLBACK_LOG = []


def disabled_by_environment():
    """ True when the user asked for the pure Python scanner. """

    value = os.environ.get('REGIONFIXER_NO_NATIVE', '')
    return value.strip().lower() not in ('', '0', 'false', 'no')


def available():
    """ True when the native scanner can be used. """

    return _native is not None and not disabled_by_environment()


def version():
    """ Version string of the native module, or None when it is missing. """

    return getattr(_native, '__version__', None) if _native else None


def default_threads():
    """ Number of worker threads to use when the user did not choose one. """

    if _native is not None:
        return _native.available_threads()
    return os.cpu_count() or 1


def resolve_workers(processes):
    """ Turn the --processes value into a concrete worker count.

    Inputs:
     - processes -- Integer from the command line. Zero or less means
                    "one worker per logical core".

    Return:
     - workers -- Integer, at least 1.

    """

    if processes is None or processes <= 0:
        return default_threads()
    return processes


class NativeRegionsetScanner:
    """ Scan a RegionSet using the native scanner.

    This class exposes the same interface as
    :class:`regionfixer_core.scan.AsyncRegionsetScanner` so the console and
    GUI scan loops can use either one.

    Inputs:
     - regionset -- A RegionSet from world.py containing the files to scan
     - processes -- An integer with the number of worker threads to use,
                    zero or less means one per logical core
     - entity_limit -- An integer, threshold of entities for a chunk to be
                       considered with too many entities
     - remove_entities -- A boolean, remove the entities of chunks that hold
                          too many of them
     - entity_position_bound -- None to skip the entity position check,
                          otherwise the largest accepted absolute x/z
                          coordinate. The native scanner does not read
                          entity positions, so with the check on every
                          region file that holds entities is handed back to
                          the Python scanner.

    """

    def __init__(self, regionset, processes, entity_limit,
                 remove_entities=False, entity_position_bound=None):
        self.data_structure = regionset
        self.regionset = regionset
        self.list_files_to_scan = regionset._get_list()
        self.processes = resolve_workers(processes)
        self.entity_limit = entity_limit
        self.remove_entities = remove_entities
        self.entity_position_bound = entity_position_bound

        self._scanner = None
        self._by_path = {}
        self._pending = len(self.list_files_to_scan)
        self._str_last_scanned = None

        # Same self tuning sleep as the multiprocessing scanner.
        self.SCAN_START_SLEEP_TIME = 0.001
        self.SCAN_MIN_SLEEP_TIME = 1e-6
        self.SCAN_MAX_SLEEP_TIME = 0.1
        self.scan_sleep_time = self.SCAN_START_SLEEP_TIME
        self.queries_without_results = 0
        self.MIN_QUERY_NUM = 1
        self.MAX_QUERY_NUM = 5
        self.scan_wait_time = 0.001

    def scan(self):
        """ Start the native scan. """

        self._by_path = {r.path: r for r in self.list_files_to_scan}
        paths = [r.path for r in self.list_files_to_scan]
        self._pending = len(paths)
        self._str_last_scanned = ""
        if not paths:
            self._scanner = None
            return
        logging.debug("Starting native scan of %d region files on %d threads",
                      len(paths), self.processes)
        self._scanner = _native.RegionScanner(paths,
                                              self.entity_limit,
                                              self.processes)

    def get_last_result(self):
        """ Return the next scanned region file, or None if none is ready. """

        if self._scanner is None:
            return None

        result = self._scanner.next_result()
        if result is None:
            self.queries_without_results += 1
            return None

        path, region_status, chunks, fallback = result
        scanned = self._by_path[path]

        if (fallback is None and self.entity_position_bound is not None and
                any(num_entities for _x, _z, num_entities, _status in chunks)):
            # Entity positions are not skimmed yet. Handing the file back is
            # the same contract as any other shape the skimmer cannot model.
            fallback = "entity position check needs the Python scanner"

        if fallback is not None:
            scanned = self._python_rescan(scanned, path, fallback)
        else:
            self._fill(scanned, region_status, chunks)

        self._pending -= 1
        self.queries_without_results = 0

        ds = self.data_structure
        ds._replace_in_data_structure(scanned)
        ds._update_counts(scanned)
        self.update_str_last_scanned(scanned)

        return scanned

    def _fill(self, scanned, region_status, chunks):
        """ Copy one native result into a ScannedRegionFile. """

        scanned._chunks = {}
        for status in c.CHUNK_STATUSES:
            scanned._counts[status] = 0

        for x, z, num_entities, status in chunks:
            scanned[(x, z)] = (num_entities, status)

        if self.remove_entities:
            self._remove_entities(scanned)

        scanned.status = region_status
        scanned.scan_time = time()
        scanned.scanned = True

    def _remove_entities(self, scanned):
        """ Delete the entities of every chunk that holds too many. """

        # Imported here because scan.py imports this module.
        import nbt.region as region
        from regionfixer_core import world

        crowded = [coords for coords, tup in scanned._chunks.items()
                   if tup[c.TUPLE_STATUS] == c.CHUNK_TOO_MANY_ENTITIES]
        if not crowded:
            return

        region_file = region.RegionFile(scanned.path)
        for x, z in crowded:
            num_entities = scanned[(x, z)][c.TUPLE_NUM_ENTITIES]
            world.delete_entities(region_file, x, z)
            print(("Deleted {0} entities in chunk"
                   " ({1},{2}) of the region file: {3}").format(num_entities,
                                                                x, z,
                                                                scanned.filename))
            # The entities are gone, so the chunk is fine now.
            scanned._counts[c.CHUNK_TOO_MANY_ENTITIES] -= 1
            scanned._chunks[(x, z)] = (0, c.CHUNK_OK)
            scanned._counts[c.CHUNK_OK] += 1

    def _python_rescan(self, scanned, path, reason):
        """ Rescan one region file with the pure Python scanner. """

        # Imported here because scan.py imports this module.
        from regionfixer_core import scan

        logging.debug("Native scanner handed back %s: %s", path, reason)
        FALLBACK_LOG.append((path, reason))
        result = scan.scan_region_file(scanned,
                                       self.entity_limit,
                                       self.remove_entities,
                                       self.entity_position_bound)
        if isinstance(result, tuple):
            # The Python scanner packs child process exceptions in a tuple.
            raise scan.ChildProcessException(result[0], result[1][0],
                                             result[1][1], result[1][2])
        return result

    def update_str_last_scanned(self, r):
        self._str_last_scanned = self.data_structure.get_name() + ": " + r.filename

    def sleep(self):
        """ Sleep waiting for results, adjusting the delay to the result rate. """

        if not ((self.queries_without_results < self.MAX_QUERY_NUM) &
                (self.queries_without_results > self.MIN_QUERY_NUM)):
            if self.queries_without_results < self.MIN_QUERY_NUM:
                self.scan_sleep_time *= 0.5
            elif self.queries_without_results > self.MAX_QUERY_NUM:
                self.scan_sleep_time *= 2.0
            self.scan_sleep_time = min(max(self.scan_sleep_time,
                                           self.SCAN_MIN_SLEEP_TIME),
                                       self.SCAN_MAX_SLEEP_TIME)

        sleep(self.scan_sleep_time)

    def terminate(self):
        """ Stop the worker threads. """

        if self._scanner is not None:
            self._scanner.cancel()
            self._scanner = None
        self._pending = 0

    @property
    def str_last_scanned(self):
        """ A friendly string with the last scanned result. """

        return self._str_last_scanned if self._str_last_scanned \
            else "Scanning..."

    @property
    def finished(self):
        """ True once every region file has been returned. """

        return self._pending <= 0

    @property
    def results(self):
        """ Yield all the results of the scan. """

        while not self.finished:
            result = self.get_last_result()
            if result is None:
                self.sleep()
            else:
                yield result

    def __len__(self):
        return len(self.data_structure)
