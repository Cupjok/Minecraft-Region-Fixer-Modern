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

from glob import glob
from os.path import join, split, exists, isfile
from os import remove
import os
from shutil import copy
import zlib
import sys

import nbt.region as region
import nbt.nbt as nbt
from nbt.nbt import TAG_List

import regionfixer_core.constants as c
from regionfixer_core.util import (DEFAULT_POSITION_BOUND_XZ,
                                   is_position_sane,
                                   is_vector_finite,
                                   read_nbt_vector)


def _summary_table(headers, rows):
    """Return a simple left-aligned ASCII table suitable for terminals/logs."""
    rows = [list(map(str, row)) for row in rows]
    headers = list(map(str, headers))
    all_rows = [headers] + rows
    widths = [max(len(row[i]) for row in all_rows) for i in range(len(headers))]

    def render(row):
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip()

    text = render(headers) + "\n"
    text += render(["-" * width for width in widths]) + "\n"
    for row in rows:
        text += render(row) + "\n"
    return text.rstrip()


def _summary_kv(rows):
    """Return aligned key/value rows."""
    if not rows:
        return ""
    width = max(len(str(key)) for key, _value in rows)
    return "\n".join("  {0:<{1}}  {2}".format(key, width, value) for key, value in rows)


def _problem_total(counts, problems):
    return sum(counts.get(status, 0) for status in problems)


def _fmt_count(value):
    """Format integer counts for large-world terminal summaries."""
    return "{0:,}".format(int(value))


def _plural(value, singular, plural=None):
    plural = plural if plural is not None else singular + "s"
    return singular if value == 1 else plural


def _fmt_vector(values):
    """Format a Pos/Motion tuple compactly; huge values use exponent form."""
    if values is None:
        return "missing"
    return "[" + ", ".join("{0:.6g}".format(v) for v in values) + "]"

# How many offending entities/players to list per chunk or report section.
_MAX_LISTED = 10



class InvalidFileName(IOError):
    """ Exception raised when a filename is wrong. """
    pass


class ScannedDataFile:
    """ Stores all the information of a scanned data file. 
    
    Inputs:
     - path -- String with the path of the data file. Defaults to None.
    """

    def __init__(self, path=None):
        super().__init__()
        self.path = path
        if self.path and exists(self.path):
            self.filename = split(path)[1]
            self.folder = split(split(path)[0])[1]
        else:
            self.filename = None
        # The status of the region file.
        self.status = None
        # Filled by the player position check: Pos and Motion as tuples and
        # the raw Dimension value (a string today, an integer in old worlds).
        self.position = None
        self.motion = None
        self.dimension = None

    def __str__(self):
        text = "NBT file:" + str(self.filename) + "\n"
        text += "\tStatus:" + c.DATAFILE_STATUS_TEXT[self.status] + "\n"
        return text

    @property
    def uuid(self):
        """ Player UUID taken from the file name, or None. """
        if not self.filename or not self.filename.endswith(".dat"):
            return None
        return self.filename[:-len(".dat")]

    @property
    def oneliner_status(self):
        """ One line describing the status of the file. """
        text = "File: \"" + self.filename + "\"; status: " + c.DATAFILE_STATUS_TEXT[self.status]
        if self.status == c.DATAFILE_INVALID_POSITION:
            text += "; UUID: {0}; Pos: {1}; Motion: {2}; Dimension: {3}".format(
                self.uuid, _fmt_vector(self.position), _fmt_vector(self.motion),
                self.dimension if self.dimension is not None else "unknown")
        return text


class ScannedChunk:
    """ Stores all the information of a scanned chunk.
    
    Not used at the moment, it's nice but takes an huge amount of memory when
    is not strange for chunks to be in the order of millions."""
    # WARNING: This is here so I remember to not use objects as ScannedChunk
    # They take too much memory.


class ScannedRegionFile:
    """ Stores all the scan information for a region file.

    Keywords arguments:
     - path -- A string with the path of the region file
     - scanned_time -- Float, time as returned by bult-in time module. The time
               at which the region file has been scanned. None by default.
     - folder -- Used to enhance print()

    """

    def __init__(self, path, scanned_time=None, folder=""):
        # general region file info
        self.path = path
        self.filename = split(path)[1]
        self.folder = folder
        self.x = self.z = None
        self.x, self.z = self.get_coords()
        self.coords = (self.x, self.z)

        # dictionary storing all the state tuples of all the chunks
        # in the region file, keys are the local coords of the chunk
        # sometimes called header coords
        self._chunks = {}

        # Dictionary containing counters to for all the chunks
        self._counts = {}
        for s in c.CHUNK_STATUSES:
            self._counts[s] = 0

        # {local coords: [(entity_id, pos), ...]} for chunks with the status
        # CHUNK_ENTITY_OUT_OF_BOUNDS, so the log can say which entity is bad.
        self.bad_entities = {}

        # time when the scan for this file finished
        self.scan_time = scanned_time

        # The status of the region file.
        self.status = None

        # has the file been scanned yet?
        self.scanned = False

    @property
    def oneliner_status(self):
        """ On line description of the status of the region file. """
        if self.scanned:
            status = self.status
            if status == c.REGION_OK:  # summary with all found in scan
                stats = ""
                for s in c.CHUNK_PROBLEMS:
                    stats += "{0}:{1}, ".format(c.CHUNK_PROBLEMS_ABBR[s], self.count_chunks(s))
                stats += "t:{0}".format(self.count_chunks())
            else:
                stats = c.REGION_STATUS_TEXT[status]
        else:
            stats = "Not scanned"

        return stats

    def __str__(self):
        text = "Path: {0}".format(self.path)
        scanned = False
        if self.scan_time:
            scanned = True
        text += "\nScanned: {0}".format(scanned)

        return text

    def __getitem__(self, key):
        return self._chunks[key]

    def __setitem__(self, key, value):
        self._chunks[key] = value
        self._counts[value[c.TUPLE_STATUS]] += 1

    def get_coords(self):
        """ Returns the region file coordinates as two integers.
        
        Return:
         - coordX, coordZ -- Integers with the x and z coordinates of the 
                             region file.
        
        Either parse the region file name or uses the stored in the object.

        """

        if self.x != None and self.z != None:
            return self.x, self.z
        else:
            splited = split(self.filename)
            filename = splited[1]
            l = filename.split('.')
            try:
                coordX = int(l[1])
                coordZ = int(l[2])
            except ValueError:
                raise InvalidFileName()

            return coordX, coordZ

    def keys(self):
        """Returns a list with all the local coordinates (header coordinates).
        
        Return:
         - list -- A list with all the local chunk coordinates extracted form the 
                    region file header as integer tuples
        """

        return list(self._chunks.keys())

    @property
    def has_problems(self):
        """ Return True if the region file has problem in itself or in its chunks.
        
        Return:
         - boolean -- True f the region has problems or False otherwise.

        """

        if self.status in c.REGION_PROBLEMS:
            return True
        for s in c.CHUNK_PROBLEMS:
            if self.count_chunks(s):
                return True
        return False

    def get_path(self):
        """ Returns the path of the region file.
        
        Return:
         - path -- A string with the path of the region file.

        """

        return self.path

    def count_chunks(self, status=None):
        """ Counts chunks in the region file with the given problem.
        
        Inputs:
         - status -- Integer with the status of the chunk to count for. See
                     CHUNK_PROBLEMS in constants.py.

        Return:
         - counter -- Integer with the number of chunks with that status

        If problem is omitted or None, counts all the chunks. Returns
        an integer with the counter.

        """

        if status == None:
            counter = 0
            for s in c.CHUNK_STATUSES:
                counter += self._counts[s]
        else:
            counter = self._counts[status]

        return counter

    def get_global_chunk_coords(self, chunkX, chunkZ):
        """ Takes the chunk local coordinates and returns its global coordinates.
        
        Inputs:
         - chunkX -- Integer, local X chunk coordinate.
         - chunkZ -- Integer, local Z chunk coordinate.

        Return:
         - chunkX, chunkZ -- Integers with the x and z global chunk coordinates

        """

        regionX, regionZ = self.get_coords() 
        chunkX += regionX * 32
        chunkZ += regionZ * 32

        return chunkX, chunkZ

    def list_chunks(self, status=None):
        """ Returns a list of tuples of chunks for all the chunks with 'status'.
        
        Inputs:
         - status -- Defaults to None. Integer with the status of the chunk to list,
                     see CHUNK_STATUSES in constants.py
        
        Return:
         - list - List with tuples like (global_coordinates, status_tuple) where status 
                 tuple is (number_of_entities, status)
        
        If status is omitted or None, returns all the chunks in the region file

        """

        l = []
        for ck in list(self.keys()):
            t = self[ck]
            if status == t[c.TUPLE_STATUS]:
                l.append((self.get_global_chunk_coords(*ck), t))
            elif status == None:
                l.append((self.get_global_chunk_coords(*ck), t))

        return l

    def summary(self):
        """ Returns a summary of all the problematic chunks.
        
        Return:
         - text -- Human readable string with the summary of the scan.
        
        The summary is a human readable string with region file, global
        coordinates, local coordinates, and status of every problematic
        chunk, in a subtree like format.

        """

        text = ""
        if self.status in c.REGION_PROBLEMS:
            text += " |- This region has status: {0}.\n".format(c.REGION_STATUS_TEXT[self.status])
        else:
            for ck in list(self.keys()):
                if self[ck][c.TUPLE_STATUS] not in c.CHUNK_PROBLEMS:
                    continue
                status = self[ck][c.TUPLE_STATUS]
                h_coords = ck
                g_coords = self.get_global_chunk_coords(*h_coords)
                text += " |-+-Chunk coords: header {0}, global {1}.\n".format(h_coords, g_coords)
                text += " | +-Status: {0}\n".format(c.CHUNK_STATUS_TEXT[status])
                if self[ck][c.TUPLE_STATUS] == c.CHUNK_TOO_MANY_ENTITIES:
                    text += " | +-No. entities: {0}\n".format(self[ck][c.TUPLE_NUM_ENTITIES])
                elif self[ck][c.TUPLE_STATUS] == c.CHUNK_ENTITY_OUT_OF_BOUNDS:
                    # The entity count is shown too, a crowded chunk reports
                    # this status first and would otherwise hide it.
                    text += " | +-No. entities: {0}\n".format(self[ck][c.TUPLE_NUM_ENTITIES])
                    bad = self.bad_entities.get(ck, [])
                    for entity_id, pos in bad[:_MAX_LISTED]:
                        text += " | +-Bad entity: {0} at Pos {1}\n".format(entity_id, _fmt_vector(pos))
                    if len(bad) > _MAX_LISTED:
                        text += " | +-... and {0} more\n".format(len(bad) - _MAX_LISTED)
                text += " |\n"

        return text

    def remove_problematic_chunks(self, status):
        """ Removes all the chunks with the given status
        
        Inputs:
         - status -- Integer with the status of the chunks to remove.
                     See CHUNK_STATUSES in constants.py
        
        Return:
         - counter -- An integer with the amount of removed chunks.

        """

        counter = 0
        bad_chunks = self.list_chunks(status)
        for ck in bad_chunks:
            global_coords = ck[0]
            local_coords = _get_local_chunk_coords(*global_coords)
            region_file = region.RegionFile(self.path)
            region_file.unlink_chunk(*local_coords)
            counter += 1
            # create the new status tuple
            #                    (num_entities, chunk status)
            self[local_coords] = (0, c.CHUNK_NOT_CREATED)

        return counter

    def fix_problematic_chunks(self, status, entity_limit=None, position_bound=None):
        """ This fixes problems in chunks that can be somehow fixed.

        Inputs:
         - status -- Integer with the status of the chunks to fix. See
                    FIXABLE_CHUNK_PROBLEMS in constants.py
         - entity_limit -- Integer or None. After removing out of bounds
                    entities, a chunk still above this limit is marked as
                    CHUNK_TOO_MANY_ENTITIES instead of OK.
         - position_bound -- Largest accepted absolute x/z entity coordinate,
                    None for the default. Only used for
                    CHUNK_ENTITY_OUT_OF_BOUNDS.

        Return:
         - counter -- An integer with the amount of fixed chunks.
        
        Right now it only fixes chunks missing the TAG_List Entities, wrong located chunks and
        in some cases corrupted chunks.
        
        -TAG_List is fixed by adding said tag.
        
        -Wrong located chunks are relocated to the data coordinates stored in the zip stream. 
         We suppose these coordinates are right because the data has checksum.
         
        -Corrupted chunks: tries to read the the compressed stream byte by byte until it raises
         exception. After that compares the size of the compressed chunk stored in the region file
         with the compressed chunk extracted from the strem, if they are the same it's good to go!

        """

        # TODO: it seems having the Entities TAG missing is just a little part. Some of the
        # chunks have like 3 or 4 tag missing from the NBT structure. I don't really know which
        # of them are mandatory.
        
        assert(status in c.FIXABLE_CHUNK_PROBLEMS)
        if status == c.CHUNK_ENTITY_OUT_OF_BOUNDS:
            return self._fix_out_of_bounds_entities(entity_limit, position_bound)
        counter = 0
        bad_chunks = self.list_chunks(status)
        for ck in bad_chunks:
            global_coords = ck[0]
            local_coords = _get_local_chunk_coords(*global_coords)
            region_file = region.RegionFile(self.path)
            # catch the exception of corrupted chunks 
            try:
                chunk = region_file.get_chunk(*local_coords)
            except region.ChunkDataError:
                # if we are here the chunk is corrupted, but still
                if status == c.CHUNK_CORRUPTED:
                    # read the data raw
                    m = region_file.metadata[local_coords[0], local_coords[1]]
                    region_file.file.seek(m.blockstart * region.SECTOR_LENGTH + 5)
                    # these status doesn't provide a good enough data, we could end up reading garbage
                    if m.status not in (region.STATUS_CHUNK_IN_HEADER, region.STATUS_CHUNK_MISMATCHED_LENGTHS, 
                                       region.STATUS_CHUNK_OUT_OF_FILE, region.STATUS_CHUNK_OVERLAPPING,
                                       region.STATUS_CHUNK_ZERO_LENGTH):
                        # get the raw data of the chunk
                        raw_chunk = region_file.file.read(m.length - 1)
                        # decompress byte by byte so we can get as much as we can before the error happens
                        dc = zlib.decompressobj()
                        out = b""

                        list_of_bytes = [i.to_bytes(1, sys.byteorder) for i in raw_chunk]
                        dc_exception = None
                        try:
                            for i in list_of_bytes:
                                out += dc.decompress(i)
                        except Exception as e:
                            dc_exception = e
                            out = b""
                        # compare the sizes of the new compressed strem and the old one to see if we've got something that could be good
                        cdata = zlib.compress(out)
                        if len(cdata) == len(raw_chunk):
                            # the chunk is probably good, write it in the region file
                            region_file.write_blockdata(local_coords[0], local_coords[1], out)
                            # Try to load the chunk to see if we solved anything. Sometimes we can get a good stream of bytes and still not being a chunk.
                            try:
                                __c = region_file.get_chunk(*local_coords)
                                print("The chunk {0},{1} in region file {2} was fixed successfully.".format(local_coords[0], local_coords[1], join(self.folder,self.filename)))
                                counter += 1
                            except Exception as e:
                                print("The chunk {0},{1} in region file {2} couldn't be fixed.".format(local_coords[0], local_coords[1], join(self.folder,self.filename)))
                                pass

                        else:
                            print("The chunk {0},{1} in region file {2} couldn't be fixed.".format(local_coords[0], local_coords[1], join(self.folder,self.filename)))
                        #=======================================================
                        # print("Extracted: " + str(len(out)))
                        # print("Size of the compressed stream: " + str(len(raw_chunk)))
                        #=======================================================
            except (region.ChunkHeaderError, region.RegionHeaderError, UnicodeDecodeError):
                # usually a chunk with zero length in the first two cases, or veeery broken chunk in the third
                print("The chunk {0},{1} in region file {2} couldn't be fixed.".format(local_coords[0], local_coords[1], join(self.folder,self.filename)))

            if status == c.CHUNK_MISSING_ENTITIES_TAG:
                # The arguments to create the empty TAG_List have been somehow extracted by comparing
                # the tag list from a healthy chunk with the one created by nbt
                chunk_type = get_chunk_type(chunk)
                if chunk_type == c.LEVEL_DIR :
                    if "DataVersion" in chunk and chunk["DataVersion"].value >= 2844 : # Snapshot 21w43a (1.18)
                        chunk['entities'] = TAG_List(name='entities', type=nbt._TAG_End)
                    else :
                        chunk['Level']['Entities'] = TAG_List(name='Entities', type=nbt._TAG_End)
                elif chunk_type == c.ENTITIES_DIR :
                    chunk['Entities'] = TAG_List(name='Entities', type=nbt._TAG_End)
                else :
                    raise AssertionError("Unsupported chunk type.")
                region_file.write_chunk(local_coords[0],local_coords[1], chunk)

                # create the new status tuple
                #                    (num_entities, chunk status)
                self[local_coords] = (0           , c.CHUNK_NOT_CREATED)
                counter += 1

            elif status == c.CHUNK_WRONG_LOCATED:
                # TODO: IMPORTANT. Right now the chunk is written in its rightful position without checking if there is another chunk in there.
                # Sometimes regionfixer will overwrite a good chunk or a corrupted chunk (probably the original). This could be greatly improved
                # maybe prompt for user input on what to do.
                data_coords = get_chunk_data_coords(chunk)
                data_l_coords = _get_local_chunk_coords(*data_coords)
                region_file.write_chunk(data_l_coords[0], data_l_coords[1], chunk)
                region_file.unlink_chunk(*local_coords)
                # what to do with the old chunk in the wrong position?
                # remove it or keep it? It's probably the best to remove it.
                # create the new status tuple
                
                # remove the wrong position of the chunk and update the status 
                #                    (num_entities, chunk status)
                self[local_coords] = (0           , c.CHUNK_NOT_CREATED)
                self[data_l_coords]= (0           , c.CHUNK_OK)
                counter += 1

        return counter

    def _fix_out_of_bounds_entities(self, entity_limit, position_bound):
        """ Remove the out of bounds entities of every CHUNK_ENTITY_OUT_OF_BOUNDS chunk.

        Return:
         - counter -- Integer with the number of chunks fixed.

        """

        counter = 0
        bad_chunks = self.list_chunks(c.CHUNK_ENTITY_OUT_OF_BOUNDS)
        if not bad_chunks:
            return counter
        region_file = region.RegionFile(self.path)
        try:
            for global_coords, _status_tuple in bad_chunks:
                local_coords = _get_local_chunk_coords(*global_coords)
                removed, remaining = remove_bad_entities(region_file, local_coords[0],
                                                         local_coords[1], position_bound)
                for entity_id, pos in removed:
                    print("Removed {0} at Pos {1} from chunk {2},{3} in region file {4}.".format(
                        entity_id, _fmt_vector(pos), local_coords[0], local_coords[1],
                        join(self.folder, self.filename)))
                # Do not let the repair hide a crowded chunk.
                if entity_limit is not None and remaining > entity_limit:
                    new_status = c.CHUNK_TOO_MANY_ENTITIES
                else:
                    new_status = c.CHUNK_OK
                self[local_coords] = (remaining, new_status)
                self.bad_entities.pop(local_coords, None)
                counter += 1
        finally:
            region_file.close()
        return counter

    def remove_entities(self):
        """ Removes all the entities in chunks with status c.CHUNK_TOO_MANY_ENTITIES.
        
        Return:
         - counter -- Integer with the number of removed entities.

        """

        status = c.CHUNK_TOO_MANY_ENTITIES
        counter = 0
        bad_chunks = self.list_chunks(status)
        for ck in bad_chunks:
            global_coords = ck[0]
            local_coords = _get_local_chunk_coords(*global_coords)
            counter += self.remove_chunk_entities(*local_coords)
            # create new status tuple:
            #                    (num_entities, chunk status)
            self[local_coords] = (0, c.CHUNK_OK)
        return counter

    def remove_chunk_entities(self, x, z):
        """ Takes a chunk local coordinates and remove its entities.
        
        Inputs:
         - x -- Integer with the X local (header) coordinate of the chunk
         - z -- Integer with the Z local (header) coordinate of the chunk
        
        Return:
         - counter -- An integer with the number of entities removed. 
        
        This will remove all the entities in the chunk, it will not perform any
        kind of check.

        """

        return delete_entities( region.RegionFile(self.path), x, z )

    def rescan_entities(self, options):
        """ Updates the status of all the chunks after changing entity_limit.
        
        Inputs:
         - options -- argparse arguments, the whole argparse.ArgumentParser() object as used
                      by regionfixer.py

        """

        for ck in list(self.keys()):
            # for safety reasons use a temporary list to generate the
            # new tuple
            t = [0, 0]
            if self[ck][c.TUPLE_STATUS] in (c.CHUNK_TOO_MANY_ENTITIES, c.CHUNK_OK):
                # only touch the ok chunks and the too many entities chunk
                if self[ck][c.TUPLE_NUM_ENTITIES] > options.entity_limit:
                    # now it's a too many entities problem
                    t[c.TUPLE_NUM_ENTITIES] = self[ck][c.TUPLE_NUM_ENTITIES]
                    t[c.TUPLE_STATUS] = c.CHUNK_TOO_MANY_ENTITIES

                elif self[c][c.TUPLE_NUM_ENTITIES] <= options.entity_limit:
                    # the new limit says it's a normal chunk
                    t[c.TUPLE_NUM_ENTITIES] = self[ck][c.TUPLE_NUM_ENTITIES]
                    t[c.TUPLE_STATUS] = c.CHUNK_OK

                self[ck] = tuple(t)


class DataSet:
    """ Stores data items to be scanned by AsyncScanner in scan.py.

    Inputs:
     - typevalue -- The type of the class to store in the set. In initialization it will be
                    asserted if it is of that type

    The data will be stored  in the self._set dictionary.
    
    Implemented private methods are: __getitem__, __setitem__, _get_list, __len__.

    Three methods should be overridden to work with a DataSet, two of the mandatory:
     - _replace_in_data_structure -- (mandatory) Should be created because during the scan the
            different processes create copies of the original data, so replacing it in
            the original data set is mandatory in order to keep everything working.

     - _update_counts -- (mandatory) Makes sure that the DataSet stores all the counts and
            that it is not needed to loop through all of them to know the real count.

     - has_problems -- (optional but used) Should return True only if any element
                         of the set has problems

    """

    def __init__(self, typevalue, *args, **kwargs):
        self._set = {}
        self._typevalue = typevalue

    def _get_list(self):
        """ Returns a list with all the values in the set. """

        return list(self._set.values())

    def __getitem__(self, key):
        return self._set[key]

    def __delitem__(self, key):
        del self._set[key]

    def __setitem__(self, key, value):
        assert self._typevalue == type(value)
        self._set[key] = value
        self._update_counts(value)

    def __len__(self):
        return len(self._set)

    # mandatory implementation methods
    def summary(self):
        """ Return a summary of problems found in this set. """

        raise NotImplementedError

    @property
    def has_problems(self):
        """ Returns True if the scanned set has problems. """

        raise NotImplementedError

    def _replace_in_data_structure(self, data, key):
        """ For multiprocessing. Replaces the data in the set with the new data.

        Inputs:
         - data -- Value of the data to be stored
         - key -- Key in which to store the data

        Child scanning processes make copies of the ScannedRegion/DataFile when they scan them.
        The AsyncScanner will call this function so the ScannedRegion/DataFile is stored
        in the set properly.
        """

        raise NotImplementedError

    def _update_counts(self, s):
        """ This functions is used by __set__ to update the counters. """

        raise NotImplementedError


class DataFileSet(DataSet):
    """ DataSet for Minecraft data files (.dat).
    
    Inputs:
     - path -- Path to the folder containing data files
     - title -- Some user readable string to represent the DataSet
    """

    def __init__(self, path, title, recursive=False, *args, **kwargs):
        DataSet.__init__(self, ScannedDataFile, *args, **kwargs)
        d = self._set

        self.title = title
        self.path = path
        if recursive and exists(path):
            data_files_path = []
            for root, _directories, filenames in os.walk(path):
                for filename in filenames:
                    if filename.endswith(".dat"):
                        data_files_path.append(join(root, filename))
        else:
            data_files_path = glob(join(path, "*.dat"))

        for path in data_files_path:
            d[path] = ScannedDataFile(path)

        # stores the counts of files
        self._counts = {}
        for s in c.DATAFILE_STATUSES:
            self._counts[s] = 0

    @property
    def has_problems(self):
        """ Returns True if the dataset has problems and false otherwise. """

        for d in self._set.values():
            if d.status in c.DATAFILE_PROBLEMS:
                return True
        return False

    def _replace_in_data_structure(self, data):
        self._set[data.path] = data

    def _update_counts(self, s):
        assert isinstance(s, self._typevalue)
        self._counts[s.status] += 1

    def count_datafiles(self, status=None):
        """Count data files, optionally limited to a scan status."""
        if status is None:
            return len(self._set)
        return sum(1 for item in self._set.values() if item.status == status)

    def summary(self):
        """ Return a summary of problems found in this set. """

        text = ""
        bad_data_files = [i for i in list(self._set.values()) if i.status in c.DATAFILE_PROBLEMS]
        for f in bad_data_files:
            text += "\t" + f.oneliner_status
            text += "\n"
        return text


class RegionSet(DataSet):
    """Stores an arbitrary number of region files and their scan results.
    
    Inputs:
     - regionset_path -- Path to the folder containing region files
                         IT MUST NOT END WITH A SLASH ("/")
     - region_list -- List of paths to all the region files
     - overworld -- Tweak to tell it's a dimension and not the overworld
    """

    def __init__(self, regionset_path=None, region_list=[], overworld=True):
        DataSet.__init__(self, ScannedRegionFile)
        # Otherwise, problems in _get_dimension_directory() and _get_region_type_directory()
        if regionset_path != None :
            assert regionset_path[-1] != "/"
        self.overworld = overworld

        if regionset_path:
            self.path = regionset_path
            self.region_list = glob(join(self.path, "r.*.*.mca"))
        else:
            self.path = None
            self.region_list = region_list
        self._set = {}
        for path in self.region_list:
            try:
                r = ScannedRegionFile(path, folder=self._get_dim_type_string())
                self._set[r.get_coords()] = r

            except InvalidFileName:
                try :
                    region_type = c.REGION_TYPES_NAMES[self._get_region_type_directory()][0]
                except:
                    region_type = "region (?)"
                print("Warning: The file {0} is not a valid name for a {1} file. I'll skip it.".format(path, region_type))

        # region and chunk counters with all the data from the scan
        self._region_counters = {}
        for status in c.REGION_STATUSES:
            self._region_counters[status] = 0

        self._chunk_counters = {}
        for status in c.CHUNK_STATUSES:
            self._chunk_counters[status] = 0

        # has this regionset been scanned?
        self.scanned = False

    def get_name(self):
        """ Return a string with a representative name for the regionset

        The order for getting the name is:
         1 - The name derived by the dimension path
         2 - The name of the last directory in the path as returned by _get_dimension_directory
         3 - Empty string ""

        """

        dim_directory = self._get_dimension_directory()
        region_type_directory = self._get_region_type_directory()
        if (dim_directory or self.overworld) and region_type_directory:
            try: dim_directory = c.DIMENSION_NAMES[dim_directory]
            except: dim_directory = "\"" + dim_directory + "\""
            try: region_type_directory = c.REGION_TYPES_NAMES[region_type_directory][1]
            except: region_type_directory = "\"" + region_type_directory + "\""
            return "{0} files for {1}".format(region_type_directory, dim_directory)
        else:
            return ""

    def _get_dimension_directory(self):
        """Return a canonical dimension id for this RegionSet.

        Modern Minecraft stores dimensions under
        ``dimensions/<namespace>/<dimension>/``.  Older worlds instead store
        the Overworld at the world root and the vanilla Nether/End in DIM-1
        and DIM1.  Normalising both layouts lets modern and legacy worlds use backups
        across the storage-layout migration.
        """

        if not self.path:
            return None

        normal_path = os.path.normpath(self.path)
        dimension_root = os.path.dirname(normal_path)
        parts = dimension_root.split(os.sep)

        # Modern namespaced-dimension layout. Use the last "dimensions" component in
        # case a parent directory happens to have the same name.
        dimensions_indexes = [i for i, part in enumerate(parts) if part == "dimensions"]
        if dimensions_indexes:
            index = dimensions_indexes[-1]
            dimension_parts = parts[index + 1:]
            if dimension_parts:
                return "/".join(dimension_parts)

        # Legacy vanilla layout. Canonical names match the modern resource
        # locations so backup replacement works across the storage migration.
        if self.overworld:
            return "minecraft/overworld"

        legacy_name = os.path.basename(dimension_root)
        legacy_dimensions = {
            "DIM-1": "minecraft/the_nether",
            "DIM1": "minecraft/the_end",
        }
        return legacy_dimensions.get(legacy_name, legacy_name)

    def _get_region_type_directory(self):
        """ Returns a string with the directory containing the RegionSet.
        
        If there is no such a directory returns None. If it's composed
        of sparse region files returns 'regionset'.
        """

        if self.path:
            rest, type_dir = split(self.path)
            return type_dir
        else:
            return None

    def _get_dim_type_string(self) :
        dim = self._get_dimension_directory()
        rg_type = self._get_region_type_directory()
        string = ""
        if rg_type != None : string = rg_type
        if dim != None and dim != "" : string = dim + "/" + rg_type
        return string

    def _update_counts(self, scanned_regionfile):
        """ Updates the counters of the regionset with the new regionfile. """

        assert isinstance(scanned_regionfile, ScannedRegionFile)

        self._region_counters[scanned_regionfile.status] += 1

        for status in c.CHUNK_STATUSES:
            self._chunk_counters[status] += scanned_regionfile.count_chunks(status)

    def _replace_in_data_structure(self, data):
        self._set[data.get_coords()] = data

    def __str__(self):
        text = "RegionSet: {0}\n".format(self.get_name())
        if self.path:
            text += "   Regionset path: {0}\n".format(self.path)
        text += "   Region files: {0}\n".format(len(self._set))
        text += "   Scanned: {0}".format(str(self.scanned))
        return text

    @property
    def has_problems(self):
        """ Returns True if the regionset has chunk or region problems and false otherwise. """

        for s in c.REGION_PROBLEMS:
            if self.count_regions(s):
                return True

        for s in c.CHUNK_PROBLEMS:
            if self.count_chunks(s):
                return True

        return False

    def keys(self):
        return list(self._set.keys())

    def list_regions(self, status=None):
        """ Returns a list of all the ScannedRegionFile objects with 'status'.
        
        Inputs:
         - status -- The region file status. See c.REGION_STATUSES
        
        Return:
         - t -- List with all the ScannedRegionFile objects with that status
        
        If status = None it returns all the objects.
        
        """

        if status is None:
            return list(self._set.values())
        t = []
        for coords in list(self._set.keys()):
            r = self._set[coords]
            if r.status == status:
                t.append(r)
        return t

    def count_regions(self, status=None):
        """ Return the number of region files with status.
        
        Inputs:
         - status -- The region file status. See c.REGION_STATUSES
        
        Return:
         - counter -- Integer with the number of regions with that status
        
        If none returns the total number of region files in this regionset.
        
        """

        counter = 0
        if status is None:
            for s in c.REGION_STATUSES:
                counter += self._region_counters[s]
        else:
            counter = self._region_counters[status]

        return counter

    def count_chunks(self, status=None):
        """ Returns the number of chunks with the given status.
        
        Inputs:
         - status -- Integer with the chunk status to count. See 
                     c.CHUNK_STATUSES in constants.py
        
        Return:
         - counter -- Integer with the number of chunks removed

        If status is None returns the number of chunks in this region file.

        """

        counter = 0
        if status is None:
            for s in c.CHUNK_STATUSES:
                counter += self._chunk_counters[s]
        else:
            counter = self._chunk_counters[status]

        return counter

    def list_chunks(self, status=None):
        """ Returns a list of all the chunk tuples with 'status'.
        
        Inputs:
         - status -- The chunk status to list. See c.CHUNK_STATUSES
        
        Return:
         - l -- List with tuples like (global_coordinates, status_tuple) where status 
                 tuple is (number_of_entities, status). For more details see
                 ScannedRegionFile.list_chunks()
        
        If status = None it returns all the chunk tuples.
        
        """

        l = []
        for r in list(self.keys()):
            l.extend(self[r].list_chunks(status))
        return l

    def summary(self):
        """ Returns a string with a summary of the problematic chunks.

        Return:
         - text -- String, human readable text with information about the scan.

        The summary contains global coordinates, local coordinates,
        data coordinates and status.

        """

        text = ""
        for r in list(self.keys()):
            if not self[r].has_problems:
                continue
            text += "Region file: {0}\n".format(join(self._get_dim_type_string(),self[r].filename))

            text += self[r].summary()
            text += " +\n\n"
        return text

    def locate_chunk(self, global_coords):
        """ Takes the global coordinates of a chunk and returns where is it.
        
        Inputs:
         - global_coords -- Tuple of two integers with the global chunk coordinates to locate.
        
        Return:
         - path -- String, with the path of the region file where
                       the chunk is stored
         - local_coords -- Tuple of two integers with local coordinates of the
                           chunk in the region file

        """

        path = join(self.path, get_chunk_region(*global_coords))
        local_coords = _get_local_chunk_coords(*global_coords)

        return path, local_coords

    def locate_region(self, coords):
        """ Returns a string with the path of the region file.
        
        Inputs:
         - coords -- Tuple of two integers with the global region coordinates of the region
                     file to locate in this RegionSet.
        
        Return:
         - region_name -- String containing the path of the region file or None if it
                          doesn't exist

        """

        x, z = coords
        region_name = 'r.' + str(x) + '.' + str(z) + '.mca'

        return region_name

    def remove_problematic_chunks(self, status):
        """ Removes all the chunks with the given status.
        
        Inputs:
         - status -- Integer with the chunk status to remove. See c.CHUNK_STATUSES
                     in constants.py for a list of possible statuses.
        
        Return:
         - counter -- Integer with the number of chunks removed
        """

        counter = 0
        if self.count_chunks():
            dim_name = self.get_name()
            print(' Deleting chunks in regionset \"{0}\":'.format(dim_name if dim_name else "selected region files"))
            for r in list(self._set.keys()):
                counter += self._set[r].remove_problematic_chunks(status)
            print("Removed {0} chunks in this regionset.\n".format(counter))

        return counter

    def fix_problematic_chunks(self, status, entity_limit=None, position_bound=None):
        """ Try to fix all the chunks with the given problem.

        Inputs:
         - status -- Integer with the chunk status to fix. See c.CHUNK_STATUSES in constants.py
                     for a list of possible statuses.
         - entity_limit, position_bound -- See ScannedRegionFile.fix_problematic_chunks().

        Return:
         - counter -- Integer with the number of chunks fixed.
        """

        counter = 0
        if self.count_chunks():
            dim_name = self.get_name()
            print('Repairing chunks in regionset \"{0}\":'.format(dim_name if dim_name else "selected region files"))
            for r in list(self._set.keys()):
                counter += self._set[r].fix_problematic_chunks(status, entity_limit, position_bound)
            print("    Repaired {0} chunks in this regionset.\n".format(counter))

        return counter

    def remove_entities(self):
        """ Removes entities in chunks with the status TOO_MANY_ENTITIES. 

        Return:
         - counter -- Integer with the number of removed entities.
        """

        counter = 0
        for r in list(self._set.keys()):
            counter += self._set[r].remove_entities()
        return counter

    def rescan_entities(self, options):
        """ Updates the c.CHUNK_TOO_MANY_ENTITIES status of all the chunks in the RegionSet.
        
        This should be ran when the option entity limit is changed.
        """

        for r in list(self.keys()):
            self[r].rescan_entities(options)

    def generate_report(self, standalone):
        """Generate scan counts or a detailed human-readable summary.

        The non-standalone return value remains compatible with the original
        Region Fixer API: ``(chunk_counts, region_counts)``.
        """
        chunk_counts = {status: self.count_chunks(status) for status in c.CHUNK_STATUSES}
        chunk_counts['TOTAL'] = self.count_chunks()
        region_counts = {status: self.count_regions(status) for status in c.REGION_STATUSES}
        region_counts['TOTAL'] = self.count_regions()

        if not standalone:
            legacy_chunk_counts = {status: chunk_counts[status] for status in c.CHUNK_PROBLEMS}
            legacy_chunk_counts['TOTAL'] = chunk_counts['TOTAL']
            legacy_region_counts = {status: region_counts[status] for status in c.REGION_PROBLEMS}
            legacy_region_counts['TOTAL'] = region_counts['TOTAL']
            return legacy_chunk_counts, legacy_region_counts

        chunk_problem_total = _problem_total(chunk_counts, c.CHUNK_PROBLEMS)
        region_problem_total = _problem_total(region_counts, c.REGION_PROBLEMS)
        total_problems = chunk_problem_total + region_problem_total

        lines = [
            "=" * 72,
            "SCAN SUMMARY - SELECTED REGION FILES",
            "=" * 72,
            _summary_kv([
                ("Overall result", "CLEAN" if total_problems == 0 else "ISSUES FOUND"),
                ("Region files scanned", _fmt_count(region_counts['TOTAL'])),
                ("Chunks scanned", _fmt_count(chunk_counts['TOTAL'])),
            ]),
            "",
            "CHUNK HEALTH",
        ]
        chunk_rows = [
            ("OK", _fmt_count(chunk_counts.get(c.CHUNK_OK, 0))),
            ("Problems", _fmt_count(chunk_problem_total)),
        ]
        for status in c.CHUNK_PROBLEMS:
            chunk_rows.append((c.CHUNK_STATUS_TEXT[status], _fmt_count(chunk_counts.get(status, 0))))
        lines.append(_summary_kv(chunk_rows))

        lines.extend(["", "REGION FILE HEALTH"] )
        region_rows = [
            ("OK", _fmt_count(region_counts.get(c.REGION_OK, 0))),
            ("Problems", _fmt_count(region_problem_total)),
        ]
        for status in c.REGION_PROBLEMS:
            region_rows.append((c.REGION_STATUS_TEXT[status], _fmt_count(region_counts.get(status, 0))))
        lines.append(_summary_kv(region_rows))

        lines.extend(["", "RESULT"] )
        if total_problems == 0:
            lines.append("  CLEAN - No problems found. No chunk or region problems were detected.")
        else:
            lines.append("  ISSUES FOUND - {0} chunk {1} and {2} region {3}.".format(
                _fmt_count(chunk_problem_total), _plural(chunk_problem_total, "problem"),
                _fmt_count(region_problem_total), _plural(region_problem_total, "problem")))
            lines.append("  Use --log <file> for exact problematic chunk/file locations.")
        lines.append("=" * 72)
        return "\n".join(lines)

    def remove_problematic_regions(self, status):
        """ Removes all the regions files with the given status. See the warning!
        
        Inputs:
         - status -- Integer with the status of the region files to remove.
                     See c.REGION_STATUSES in constants.py for a list.
        
        Return:
         - counter -- An integer with the amount of removed region files.
        
        Warning! This is NOT the same as removing chunks, this WILL DELETE the region files 
        from the hard drive.
        """

        counter = 0
        for r in self.list_regions(status):
            region_path = r.get_path()
            # Oversized chunks may live in sibling c.<x>.<z>.mcc files.
            # Remove those sidecars with the region so they are not orphaned.
            for external_path in _external_chunk_files_for_region(region_path):
                remove(external_path)
            remove(region_path)
            counter += 1
        return counter

class World:
    """ This class stores information and scan results for a Minecraft world.
    
    Inputs:
     - world_path -- String with the path of the world.
    
    Once scanned, stores all the problems found in it. It also has all the tools
    needed to modify the world.

    """

    def __init__(self, world_path):
        self.path = world_path

        # List with RegionSets. Modern worlds can store dimensions under
        # dimensions/<namespace>/<dimension>/{region,poi,entities}. Discover
        # both that layout and the legacy root/DIM* layout. Only add folders
        # that actually exist so an empty legacy placeholder cannot shadow the
        # matching modern RegionSet when using backups.
        self.regionsets = []
        region_type_directories = (c.LEVEL_DIR, c.POI_DIR, c.ENTITIES_DIR)
        discovered = []

        for region_type in region_type_directories:
            legacy_overworld = join(self.path, region_type)
            if os.path.isdir(legacy_overworld):
                discovered.append((legacy_overworld, True))
            for directory in glob(join(self.path, "DIM*", region_type)):
                if os.path.isdir(directory):
                    discovered.append((directory, False))

        modern_dimensions = join(self.path, "dimensions")
        if os.path.isdir(modern_dimensions):
            for directory, child_directories, _filenames in os.walk(modern_dimensions):
                if os.path.basename(directory) in region_type_directories:
                    discovered.append((directory, False))
                    # A region-data folder should not contain dimension folders.
                    child_directories[:] = []

        seen = set()
        for directory, overworld in discovered:
            normalized = os.path.normcase(os.path.abspath(directory))
            if normalized in seen:
                continue
            seen.add(normalized)
            self.regionsets.append(RegionSet(directory, overworld=overworld))

        # level.dat
        # Let's scan level.dat here so we can extract the world name
        level_dat_path = join(self.path, "level.dat")
        if exists(level_dat_path):
            try:
                self.level_data = nbt.NBTFile(level_dat_path)["Data"]
                self.name = self.level_data["LevelName"].value
                self.scanned_level = ScannedDataFile(level_dat_path)
                self.scanned_level.status = c.DATAFILE_OK
            except Exception:
                self.name = None
                self.scanned_level = ScannedDataFile(level_dat_path)
                self.scanned_level.status = c.DATAFILE_UNREADABLE
        else:
            self.level_file = None
            self.level_data = None
            self.name = None
            self.scanned_level = ScannedDataFile(level_dat_path)
            self.scanned_level.status = c.DATAFILE_UNREADABLE

        # Player/data files. Modern worlds can store UUID player data under
        # playerdata/ to players/data/ and made data/ files namespaced.
        self.datafilesets = []
        modern_players_directory = join(self.path, 'players', 'data')
        legacy_players_directory = join(self.path, 'playerdata')
        players_directory = (modern_players_directory if exists(modern_players_directory)
                             else legacy_players_directory)

        self.players = DataFileSet(players_directory,
                                   "\nPlayer UUID files:\n")
        self.datafilesets.append(self.players)
        # Very old (pre-UUID) player files lived directly in players/. On a
        # modern world this directory may only contain subdirectories, so a
        # non-recursive scan safely finds none here.
        self.old_players = DataFileSet(join(self.path, 'players'),
                                       "\nOld format player files:\n")
        self.datafilesets.append(self.old_players)
        self.data_files = DataFileSet(join(self.path, 'data'),
                                      "\nStructures and map data files:\n",
                                      recursive=True)

        # Modern worlds can also store dimension-specific NBT data in each
        # dimension's data/<namespace>/ tree (for example the End dragon
        # fight). Keep these files in the existing data_files DataFileSet so
        # the scanner/reporting behavior stays exactly the same for callers.
        if os.path.isdir(modern_dimensions):
            for directory, _child_directories, filenames in os.walk(modern_dimensions):
                if os.path.basename(directory) != 'data':
                    continue
                for data_root, _data_directories, data_filenames in os.walk(directory):
                    for filename in data_filenames:
                        if filename.endswith('.dat'):
                            data_path = join(data_root, filename)
                            self.data_files._set[data_path] = ScannedDataFile(data_path)

        self.datafilesets.append(self.data_files)

        # level.dat is sufficient to identify a world even if all region files
        # are currently missing/corrupt. This also avoids rejecting freshly
        # modern worlds during discovery.
        self.isworld = exists(level_dat_path) or any(len(r) for r in self.regionsets)
        # TODO: Make a Exception for this! so we can use try/except

        # Set in scan.py, used in interactive.py
        self.scanned = False

    def __str__(self):
        counters = self.get_number_regions()
        text = "World information:\n"
        text += "   World path: {0}\n".format(self.path)
        text += "   World name: {0}\n".format(self.name)
        if c.LEVEL_DIR in counters :
            text += "   Region/Level files: {0}\n".format(counters[c.LEVEL_DIR])
        if c.POI_DIR in counters :
            text += "   POI files: {0}\n".format(counters[c.POI_DIR])
        if c.ENTITIES_DIR in counters :
            text += "   Entities files: {0}\n".format(counters[c.ENTITIES_DIR])
        text += "   Scanned: {0}".format(str(self.scanned))
        return text

    @property
    def has_problems(self):
        """ Returns True if the regionset has chunk or region problems and false otherwise.

        Return:
         - boolean -- A boolean, True if the world has any problems, false otherwise
        
        """

        if self.scanned_level.status in c.DATAFILE_PROBLEMS:
            return True

        for d in self.datafilesets:
            if d.has_problems:
                return True

        for r in self.regionsets:
            if r.has_problems:
                return True

        return False

    def get_number_regions(self):
        """ Returns a dictionnary with the number of regions files in this world
        
        Return:
         - counters -- An dictionnary with the amount of region files.
                
        """

        counters = {}
        for dim in self.regionsets:
            region_type = dim._get_region_type_directory()
            if not region_type in counters :
                counters[region_type] = 0
            counters[region_type] += len(dim)

        return counters

    def summary(self):
        """ Returns a string with a summary of the problems in this world.

        Return:
         - text -- A String with a human readable summary of all the problems in this world.

        This method calls the other summary() methods in RegionSet and DataSet. See these
        methods for more details.

        """

        final = ""

        # intro with the world name
        final += "{0:#^60}\n".format('')
        final += "{0:#^60}\n".format(" World name: {0} ".format(self.name))
        final += "{0:#^60}\n".format('')

        # leve.dat and data files
        final += "\nlevel.dat:\n"
        if self.scanned_level.status not in c.DATAFILE_PROBLEMS:
            final += "\t\'level.dat\' is readable\n"
        else:
            final += "\t[WARNING]: \'level.dat\' isn't readable, error: {0}\n".format(c.DATAFILE_STATUS_TEXT[self.scanned_level.status])

        sets = [self.players,
                self.old_players,
                self.data_files]

        for s in sets:
            final += s.title
            text = s.summary()
            final += text if text else "All files ok.\n"

        final += "\n"

        # chunk info
        chunk_info = ""
        for regionset in self.regionsets:
            title = regionset.get_name()
            final += "\n" + title + ":\n"

            # don't add text if there aren't broken chunks
            text = regionset.summary()
            chunk_info += text if text else ""
            final += chunk_info if chunk_info else "All the chunks are ok."

        final += "\n\n"

        return final

    def get_name(self):
        """ Returns a string with the name of the world.
        
        Return:
         - name -- String with either the world name as found in level.dat or the last
                   directory in the world path.

        """

        if self.name:
            return self.name
        else:
            n = split(self.path)
            if n[1] == '':
                n = split(n[0])[1]
            return n

    def count_regions(self, status=None):
        """ Returns an integer with the count of region files with status.

        Inputs:
         - status -- An integer from c.REGION_STATUSES to region files with that status.
                     For a list of status see REGION_STATUSES in constants.py

        Return:
         - counter -- An integer with the number of region files with the given status.

        """

        counter = 0
        for r in self.regionsets:
            counter += r.count_regions(status)
        return counter

    def count_chunks(self, status=None):
        """ Returns an integer with the count of chunks with 'status'.

        Inputs:
         - status -- An integer from c.CHUNK_STATUSES to count chunks with that status.
                     For a list of status see c.CHUNK_STATUSES.

        Return:
         - counter -- An integer with the number of chunks with the given status.

        """

        counter = 0
        for r in self.regionsets:
            count = r.count_chunks(status)
            counter += count
        return counter

    def replace_problematic_chunks(self, backup_worlds, status, entity_limit, delete_entities,
                                   entity_position_bound=None):
        """ Replaces problematic chunks using backups.

        Inputs:
         - backup_worlds -- A list of World objects to use as backups. Backup worlds will be used
                            in a ordered way.
         - status -- An integer indicating the status of chunks to be replaced.
                      See CHUNK_STATUSES in constants.py for a complete list.
         - entity_limit -- The threshold to consider a chunk with the status TOO_MANY_ENTITIES.
         - delete_entities -- Boolean indicating if the chunks with too_many_entities should have
                             their entities removed.
         - entity_position_bound -- None, or the x/z bound of the entity position check. When
                             set, backup chunks holding out of bounds entities are not used.

        Return:
         - counter -- An integer with the number of chunks replaced.

        """

        counter = 0
        scanned_regions = {}
        requested_status = status
        for regionset in self.regionsets:
            for backup in backup_worlds:
                # choose the correct regionset based on the canonical dimension
                # id and the type name (region, POI and entities)
                b_regionset = None
                for temp_regionset in backup.regionsets:
                    if ( temp_regionset._get_dimension_directory() == regionset._get_dimension_directory() and
                         temp_regionset._get_region_type_directory() == regionset._get_region_type_directory()):
                        b_regionset = temp_regionset
                        break

                # this doesn't need to be aware of region status, it just
                # iterates the list returned by list_chunks()
                bad_chunks = regionset.list_chunks(requested_status)

                if bad_chunks and b_regionset is None:
                    print("The regionset '{0}' doesn't exist in the backup directory. Skipping this backup directory.".format(regionset._get_dim_type_string()))
                    continue
                else:
                    for ck in bad_chunks:
                        global_coords = ck[0]
                        status_tuple = ck[1]
                        local_coords = _get_local_chunk_coords(*global_coords)
                        print("\n{0:-^60}".format(' New chunk to replace. Coords: x = {0}; z = {1} '.format(*global_coords)))

                        # search for the region file
                        backup_region_path, local_coords = b_regionset.locate_chunk(global_coords)
                        tofix_region_path, _ = regionset.locate_chunk(global_coords)
                        if exists(backup_region_path):
                            print("Backup region file found in:\n  {0}".format(backup_region_path))
                            # Scan the whole region file, pretty slow, but
                            # absolutely needed to detect sharing offset chunks
                            # The backups world doesn't change, check if the
                            # region_file is already scanned:
                            # Region coordinates alone are not unique across
                            # dimensions/types (every dimension can have an
                            # r.0.0.mca). Cache by the actual backup path.
                            cache_key = os.path.normcase(os.path.abspath(backup_region_path))
                            try:
                                r = scanned_regions[cache_key]
                            except KeyError:
                                from .scan import scan_region_file
                                r = scan_region_file(ScannedRegionFile(backup_region_path), entity_limit, delete_entities,
                                                     entity_position_bound)
                                scanned_regions[cache_key] = r
                            try:
                                status_tuple = r[local_coords]
                            except KeyError:
                                status_tuple = None

                            # Retrive the status from status_tuple
                            if status_tuple == None:
                                backup_status = c.CHUNK_NOT_CREATED
                            else:
                                backup_status = status_tuple[c.TUPLE_STATUS]

                            if backup_status == c.CHUNK_OK:
                                backup_region_file = region.RegionFile(backup_region_path)
                                working_chunk = backup_region_file.get_chunk(local_coords[0], local_coords[1])

                                print("Replacing...")
                                # the chunk exists and is healthy, fix it!
                                tofix_region_file = region.RegionFile(tofix_region_path)
                                # first unlink the chunk, second write the chunk.
                                # unlinking the chunk is more secure and the only way to replace chunks with
                                # a shared offset without overwriting the good chunk
                                tofix_region_file.unlink_chunk(*local_coords)
                                tofix_region_file.write_chunk(local_coords[0], local_coords[1], working_chunk)
                                counter += 1
                                print("Chunk replaced using backup dir: {0}".format(backup.path))

                            else:
                                print("Can't use this backup directory, the chunk has the status: {0}".format(c.CHUNK_STATUS_TEXT[backup_status]))
                                continue

                        else:
                            print("The region file doesn't exist in the backup directory: {0}".format(backup_region_path))

        return counter

    def remove_problematic_chunks(self, status):
        """ Removes all the chunks with the given status.
        
        Inputs:
         - status -- Integer with the chunk status to remove. See CHUNK_STATUSES in constants.py 
                     for a list of possible statuses.
        
        Return:
         - counter -- Integer with the number of chunks removed
        
        This method calls remove_problematic_chunks() in the RegionSets.

        """

        counter = 0
        for regionset in self.regionsets:
            counter += regionset.remove_problematic_chunks(status)
        return counter

    def fix_problematic_chunks(self, status, entity_limit=None, position_bound=None):
        """ Try to fix all the chunks with the given status.

        Inputs:
         - status -- Integer with the chunk status to remove. See CHUNK_STATUSES in constants.py
                     for a list of possible statuses.
         - entity_limit, position_bound -- See ScannedRegionFile.fix_problematic_chunks().

        Return:
         - counter -- Integer with the number of chunks fixed.

        This method calls remove_problematic_chunks() in the RegionSets.

        """

        counter = 0
        for regionset in self.regionsets:
            counter += regionset.fix_problematic_chunks(status, entity_limit, position_bound)
        return counter

    def get_spawn(self):
        """ Return the world spawn from level.dat as ((x, y, z), dimension).

        Return:
         - pos -- Tuple of three floats. (0.0, 64.0, 0.0) when level.dat has
                  no usable spawn.
         - dimension -- The spawn dimension id, or None if level.dat does not
                        say (before 1.21.9 the spawn was always the overworld).

        Since 1.21.9 the spawn is a `spawn` compound holding `pos` and
        `dimension`; older worlds use SpawnX/SpawnY/SpawnZ.

        """

        data = self.level_data
        try:
            if data is not None and "spawn" in data:
                spawn = data["spawn"]
                pos = [float(v) for v in spawn["pos"].value]
                dimension = spawn["dimension"].value if "dimension" in spawn else None
                if len(pos) == 3:
                    return tuple(pos), dimension
            if data is not None and all(k in data for k in ("SpawnX", "SpawnY", "SpawnZ")):
                return (float(data["SpawnX"].value), float(data["SpawnY"].value),
                        float(data["SpawnZ"].value)), None
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        return (0.0, 64.0, 0.0), None

    def list_invalid_player_files(self):
        """ Return every scanned player file with the status DATAFILE_INVALID_POSITION. """

        return [f for s in (self.players, self.old_players) for f in s._get_list()
                if f.status == c.DATAFILE_INVALID_POSITION]

    def fix_player_positions(self):
        """ Move every player with an invalid position to the world spawn.

        Return:
         - counter -- Integer with the number of player files fixed.

        For each file a copy of the original is kept next to it as
        `<file>.bak` (or `<file>.bak.N` if that name is taken) before
        anything is written. Pos becomes the spawn from level.dat, Motion and
        the fall distance become zero and, when the spawn dimension is known
        or the file uses the string form, Dimension is set to the spawn
        dimension so the player is not dropped at overworld coordinates in
        another dimension.

        """

        spawn, spawn_dimension = self.get_spawn()
        counter = 0
        for scanned in self.list_invalid_player_files():
            backup_path = _free_backup_path(scanned.path)
            copy(scanned.path, backup_path)
            player = nbt.NBTFile(filename=scanned.path)
            pos = nbt.TAG_List(name="Pos", type=nbt.TAG_Double)
            pos.tags.extend(nbt.TAG_Double(v) for v in spawn)
            player["Pos"] = pos
            motion = nbt.TAG_List(name="Motion", type=nbt.TAG_Double)
            motion.tags.extend(nbt.TAG_Double(0.0) for _ in range(3))
            player["Motion"] = motion
            # Falling through the broken position may have built up a lethal
            # fall distance. The tag was renamed in 1.21.5.
            if "FallDistance" in player:
                player["FallDistance"] = nbt.TAG_Float(0.0)
            if "fall_distance" in player:
                player["fall_distance"] = nbt.TAG_Double(0.0)
            if "Dimension" in player:
                current = player["Dimension"]
                if isinstance(current, nbt.TAG_String):
                    player["Dimension"] = nbt.TAG_String(spawn_dimension or "minecraft:overworld")
                elif spawn_dimension is None:
                    # Integer ids in pre-1.16 files; 0 is the overworld.
                    player["Dimension"] = nbt.TAG_Int(0)
            player.write_file(filename=scanned.path)

            print("Reset player {0}: Pos {1} -> {2} (original saved as {3})".format(
                scanned.uuid, _fmt_vector(scanned.position), _fmt_vector(spawn),
                split(backup_path)[1]))
            scanned.status = c.DATAFILE_OK
            scanned.position = spawn
            scanned.motion = (0.0, 0.0, 0.0)
            counter += 1
        return counter

    def replace_problematic_regions(self, backup_worlds, status, entity_limit, delete_entities):
        """ Replaces problematic region files using backups.
        
        Inputs:
         - backup_worlds -- A list of World objects to use as backups. Backup worlds will be used
                            in a ordered way.
         - status -- An integer indicating the status of region files to be replaced.
                      See c.REGION_STATUSES for a complete list.
         - entity_limit -- The threshold to consider a chunk with the status TOO_MANY_ENTITIES.
                           (variable not used, just for inputs to be homogeneous)
         - delete_entities -- Boolean indicating if the chunks with too_many_entities should have
                             their entities removed. (variable not used, just for inputs to be homogeneous)
        Return:
         - counter -- An integer with the number of chunks replaced.

        Note: entity_limit and delete_entities are not really used here. They are just there to make all
        the methods homogeneous.

        """

        counter = 0
        for regionset in self.regionsets:
            for backup in backup_worlds:
                # choose the correct regionset based on the canonical dimension
                # id and the type name (region, POI and entities)
                b_regionset = None
                for temp_regionset in backup.regionsets:
                    if ( temp_regionset._get_dimension_directory() == regionset._get_dimension_directory() and
                         temp_regionset._get_region_type_directory() == regionset._get_region_type_directory()):
                        b_regionset = temp_regionset
                        break

                bad_regions = regionset.list_regions(status)
                if bad_regions and b_regionset is None:
                    print("The regionset '{0}' doesn't exist in the backup directory. Skipping this backup directory.".format(regionset._get_dim_type_string()))
                    continue
                else:
                    for r in bad_regions:
                        print("\n{0:-^60}".format(' New region file to replace! Coords {0} '.format(r.get_coords())))

                        # search for the region file

                        try:
                            backup_region_path = b_regionset[r.get_coords()].get_path()
                        except:
                            backup_region_path = None
                        tofix_region_path = r.get_path()

                        if backup_region_path != None and exists(backup_region_path):
                            print("Backup region file found in:\n  {0}".format(backup_region_path))
                            # check the region file, just open it.
                            try:
                                backup_region_file = region.RegionFile(backup_region_path)
                            except region.NoRegionHeader as e:
                                print("Can't use this backup directory, the error while opening the region file: {0}".format(e))
                                continue
                            except Exception as e:
                                print("Can't use this backup directory, unknown error: {0}".format(e))
                                continue
                            copy(backup_region_path, tofix_region_path)
                            _sync_external_chunk_files(backup_region_path, tofix_region_path)
                            print("Region file replaced!")
                            counter += 1
                        else:
                            print("The region file doesn't exist in the backup directory: {0}".format(backup_region_path))

        return counter

    def remove_problematic_regions(self, status):
        """ Removes all the regions files with the given status. See the warning!
        
        Inputs:
         - status -- Integer with the status of the region files to remove.
                     See REGION_STATUSES in constants. py for a list.
        
        Return:
         - counter -- An integer with the amount of removed region files.
        
        Warning! This is NOT the same as removing chunks, this WILL DELETE the region files 
        from the hard drive.

        """

        counter = 0
        for regionset in self.regionsets:
            counter += regionset.remove_problematic_regions(status)
        return counter

    def remove_entities(self):
        """ Removes entities in chunks with the status TOO_MANY_ENTITIES. 

        Return:
         - counter -- Integer with the number of removed entities.

        """

        counter = 0
        for regionset in self.regionsets:
            counter += regionset.remove_entities()
        return counter

    def rescan_entities(self, options):
        """ Updates the CHUNK_TOO_MANY_ENTITIES status of all the chunks in the RegionSet.
        
        This should be ran when the option entity limit is changed.

        """

        for regionset in self.regionsets:
            regionset.rescan_entities(options)

    def generate_report(self, standalone):
        """Generate scan counts or a detailed human-readable world summary.

        ``standalone=False`` keeps the original ``(chunk_counts,
        region_counts)`` interface. ``standalone=True`` provides a detailed
        terminal summary without replacing the exact-location output available
        through ``--log``.
        """
        chunk_counts = {status: self.count_chunks(status) for status in c.CHUNK_STATUSES}
        chunk_counts['TOTAL'] = self.count_chunks()
        region_counts = {status: self.count_regions(status) for status in c.REGION_STATUSES}
        region_counts['TOTAL'] = self.count_regions()

        if not standalone:
            legacy_chunk_counts = {status: chunk_counts[status] for status in c.CHUNK_PROBLEMS}
            legacy_chunk_counts['TOTAL'] = chunk_counts['TOTAL']
            legacy_region_counts = {status: region_counts[status] for status in c.REGION_PROBLEMS}
            legacy_region_counts['TOTAL'] = region_counts['TOTAL']
            return legacy_chunk_counts, legacy_region_counts

        chunk_problem_total = _problem_total(chunk_counts, c.CHUNK_PROBLEMS)
        region_problem_total = _problem_total(region_counts, c.REGION_PROBLEMS)

        # "bad" here means unreadable; invalid player positions are counted
        # separately so the "unreadable" labels below stay accurate.
        uuid_total = len(self.players)
        uuid_bad = sum(1 for item in self.players._get_list() if item.status == c.DATAFILE_UNREADABLE)
        old_player_total = len(self.old_players)
        old_player_bad = sum(1 for item in self.old_players._get_list() if item.status == c.DATAFILE_UNREADABLE)
        data_total = len(self.data_files)
        data_bad = sum(1 for item in self.data_files._get_list() if item.status in c.DATAFILE_PROBLEMS)
        level_bad = 1 if self.scanned_level.status in c.DATAFILE_PROBLEMS else 0
        invalid_players = self.list_invalid_player_files()
        data_problem_total = uuid_bad + old_player_bad + data_bad + level_bad + len(invalid_players)
        total_problems = chunk_problem_total + region_problem_total + data_problem_total

        modern_layout = False
        legacy_layout = False
        world_root = os.path.normcase(os.path.abspath(self.path))
        for regionset in self.regionsets:
            if not regionset.path:
                continue
            region_path = os.path.normcase(os.path.abspath(regionset.path))
            try:
                relative = os.path.relpath(region_path, world_root)
            except ValueError:
                relative = region_path
            if relative == 'dimensions' or relative.startswith('dimensions' + os.sep):
                modern_layout = True
            else:
                legacy_layout = True
        if modern_layout and legacy_layout:
            storage_layout = "Mixed legacy and namespaced-dimension layout"
        elif modern_layout:
            storage_layout = "Namespaced-dimension layout"
        elif legacy_layout:
            storage_layout = "Legacy dimension layout"
        else:
            storage_layout = "No region directories found"

        data_version = None
        try:
            data_version = self.level_data['DataVersion'].value
        except (AttributeError, KeyError, TypeError):
            pass

        region_type_counts = self.get_number_regions()
        level_status = "OK" if not level_bad else c.DATAFILE_STATUS_TEXT.get(
            self.scanned_level.status, "Unreadable")

        lines = [
            "=" * 72,
            "SCAN SUMMARY",
            "=" * 72,
        ]
        overview = [
            ("World", self.get_name()),
            ("Path", self.path),
            ("Overall result", "CLEAN" if total_problems == 0 else "ISSUES FOUND"),
            ("Storage layout", storage_layout),
        ]
        if data_version is not None:
            overview.append(("DataVersion", data_version))
        lines.append(_summary_kv(overview))

        lines.extend(["", "FILES SCANNED"] )
        lines.append(_summary_kv([
            ("level.dat", level_status),
            ("Region/Level files", _fmt_count(region_type_counts.get(c.LEVEL_DIR, 0))),
            ("POI files", _fmt_count(region_type_counts.get(c.POI_DIR, 0))),
            ("Entities files", _fmt_count(region_type_counts.get(c.ENTITIES_DIR, 0))),
            ("Total region files", _fmt_count(region_counts['TOTAL'])),
            ("UUID player files", "{0} total, {1} unreadable".format(_fmt_count(uuid_total), _fmt_count(uuid_bad))),
            ("Old player files", "{0} total, {1} unreadable".format(_fmt_count(old_player_total), _fmt_count(old_player_bad))),
            ("World/dimension data files", "{0} total, {1} unreadable".format(_fmt_count(data_total), _fmt_count(data_bad))),
        ]))

        lines.extend(["", "CHUNK HEALTH"] )
        chunk_rows = [
            ("Total scanned", _fmt_count(chunk_counts['TOTAL'])),
            ("OK", _fmt_count(chunk_counts.get(c.CHUNK_OK, 0))),
            ("Problems", _fmt_count(chunk_problem_total)),
        ]
        for status in c.CHUNK_PROBLEMS:
            chunk_rows.append((c.CHUNK_STATUS_TEXT[status], _fmt_count(chunk_counts.get(status, 0))))
        lines.append(_summary_kv(chunk_rows))

        lines.extend(["", "REGION FILE HEALTH"] )
        region_rows = [
            ("Total scanned", _fmt_count(region_counts['TOTAL'])),
            ("OK", _fmt_count(region_counts.get(c.REGION_OK, 0))),
            ("Problems", _fmt_count(region_problem_total)),
        ]
        for status in c.REGION_PROBLEMS:
            region_rows.append((c.REGION_STATUS_TEXT[status], _fmt_count(region_counts.get(status, 0))))
        lines.append(_summary_kv(region_rows))

        lines.extend(["", "PLAYER / DATA HEALTH"] )
        lines.append(_summary_kv([
            ("level.dat problems", _fmt_count(level_bad)),
            ("Unreadable UUID player files", _fmt_count(uuid_bad)),
            ("Unreadable old player files", _fmt_count(old_player_bad)),
            ("Unreadable world/data files", _fmt_count(data_bad)),
            ("Invalid player positions", _fmt_count(len(invalid_players))),
            ("Total data-file problems", _fmt_count(data_problem_total)),
        ]))
        for scanned in invalid_players[:_MAX_LISTED]:
            lines.append("  ! {0}  Pos {1}  Dimension {2}".format(
                scanned.uuid, _fmt_vector(scanned.position),
                scanned.dimension if scanned.dimension is not None else "unknown"))
        if len(invalid_players) > _MAX_LISTED:
            lines.append("  ! ... and {0} more, see --log".format(len(invalid_players) - _MAX_LISTED))

        lines.extend(["", "DIMENSION / REGION-TYPE BREAKDOWN"] )
        breakdown = []
        for regionset in sorted(
                self.regionsets,
                key=lambda item: (item._get_dimension_directory() or '',
                                  item._get_region_type_directory() or '')):
            dim_id = regionset._get_dimension_directory() or "Unknown"
            dim_name = c.DIMENSION_NAMES.get(dim_id, dim_id)
            type_id = regionset._get_region_type_directory() or "Unknown"
            type_name = c.REGION_TYPES_NAMES.get(type_id, (type_id, type_id))[1]
            rs_chunk_problems = sum(regionset.count_chunks(status) for status in c.CHUNK_PROBLEMS)
            rs_region_problems = sum(regionset.count_regions(status) for status in c.REGION_PROBLEMS)
            breakdown.append([
                dim_name,
                type_name,
                _fmt_count(regionset.count_regions()),
                _fmt_count(regionset.count_chunks()),
                _fmt_count(rs_chunk_problems + rs_region_problems),
            ])
        if breakdown:
            lines.append(_summary_table(
                ["Dimension", "Type", "Regions", "Chunks", "Issues"],
                breakdown))
        else:
            lines.append("  No region sets were discovered.")

        lines.extend(["", "RESULT"] )
        if total_problems == 0:
            lines.append("  CLEAN - No problems found. No chunk, region, player, or data-file problems were detected.")
        else:
            lines.append(
                "  ISSUES FOUND - {0} chunk {1}, {2} region {3}, and {4} data-file {5}.".format(
                    _fmt_count(chunk_problem_total), _plural(chunk_problem_total, "problem"),
                    _fmt_count(region_problem_total), _plural(region_problem_total, "problem"),
                    _fmt_count(data_problem_total), _plural(data_problem_total, "problem")))
            lines.append("  Use --log <file> for exact problematic chunk/file locations.")
        lines.append("=" * 72)
        return "\n".join(lines)



def parse_paths(args):
    """ Parse a list of paths to and returns World and a RegionSet objects.
    
    Keywords arguments:
    args -- arguments as argparse got them

    Return:
    world_list -- A list of World objects
    RegionSet -- A RegionSet object with all the regionfiles found in args
    """

    # windows shell doesn't parse wildcards, parse them here using glob
    expanded_args = []
    for arg in args:
        earg = glob(arg)
        # glob eats away any argument that doesn't match a file, keep those, they will be world folders
        if earg: expanded_args.extend(earg)
        else: expanded_args.append(arg)
    args = expanded_args

    # parese the list of region files and worlds paths
    world_list = []
    region_list = []
    warning = False
    for arg in args:
        if arg[-4:] == ".mca":
            region_list.append(arg)
        elif arg[-4:] == ".mcr": # ignore pre-anvil region files
            if not warning:
                print("Warning: Region-Fixer only works with anvil format region files. Ignoring *.mcr files")
                warning = True
        else:
            world_list.append(arg)

    # check if they exist
    region_list_tmp = []
    for f in region_list:
        if exists(f):
            if isfile(f):
                region_list_tmp.append(f)
            else:
                print("Warning: \"{0}\" is not a file. Skipping it and scanning the rest.".format(f))
        else:
            print("Warning: The region file {0} doesn't exists. Skipping it and scanning the rest.".format(f))
    region_list = region_list_tmp

    # init the world objects
    world_list = parse_world_list(world_list)

    return world_list, RegionSet(region_list = region_list)


def parse_world_list(world_path_list):
    """ Parses a world path list. Returns a list of World objects.

    Keywords arguments:
    world_path_list -- A list of string with paths where minecraft worlds are supposed to be

    Return:
    world_list -- A list of World objects using the paths from the input
 
    Parses a world path list checking if they exists and are a minecraft
    world folders. Returns a list of World objects. Prints errors for the 
    paths that are not minecraft worlds.
    """
    
    world_list = []
    for d in world_path_list:
        if exists(d):
            w = World(d)
            if w.isworld:
                world_list.append(w)
            else:
                print("Warning: The folder {0} doesn't look like a minecraft world. I'll skip it.".format(d))
        else:
            print("Warning: The folder {0} doesn't exist. I'll skip it.".format(d))
    return world_list


def parse_backup_list(world_backup_dirs):
    """ Generates a list with the input of backup dirs containing the
    world objects of valid world directories."""

    directories = world_backup_dirs.split(',')
    backup_worlds = parse_world_list(directories)
    return backup_worlds


def delete_entities(region_file, x, z):
    """ Removes entities in chunks with the status TOO_MANY_ENTITIES. 

    Keyword entities:
     - x -- Integer, X local coordinate of the chunk in the region files
     - z -- Integer, Z local coordinate of the chunk in the region files
     - region_file -- RegionFile object where the chunk is stored

    Return:
     - counter -- Integer with the number of removed entities.

    This function is used in scan.py.

    """

    chunk = region_file.get_chunk(x, z)
    chunk_type = get_chunk_type(chunk)
    empty_tag_list = nbt.TAG_List(nbt.TAG_Byte, '', 'Entities')

    if chunk_type == c.LEVEL_DIR : # Region file
        if "DataVersion" in chunk and chunk["DataVersion"].value >= 2844 : # Snapshot 21w43a (1.18)
            counter = len(chunk['entities'])
            chunk['entities'] = empty_tag_list
        else :
            counter = len(chunk['Level']['Entities'])
            chunk['Level']['Entities'] = empty_tag_list

    elif chunk_type == c.ENTITIES_DIR : # Entities file (>=1.17)
        counter = len(chunk['Entities'])
        chunk['Entities'] = empty_tag_list

    else :
        raise AssertionError("Unsupported chunk type in delete_entities().")

    region_file.write_chunk(x, z, chunk)

    return counter


def get_chunk_entity_list(chunk):
    """ Return the TAG_List holding the entities of a chunk.

    Inputs:
     - chunk -- A level or entities chunk, from the NBT module.

    Return:
     - entities -- The TAG_List, or None when the chunk stores no entities
                   (a 1.17+ level chunk whose entities live in entities/).

    The storage place follows scan_chunk(): `entities` at the root since
    21w43a (1.18), `Level.Entities` before that, and `Entities` at the root of
    the chunks in entities/*.mca.

    """

    chunk_type = get_chunk_type(chunk)
    if chunk_type == c.LEVEL_DIR:
        if "DataVersion" in chunk and chunk["DataVersion"].value >= 2844:
            return chunk["entities"] if "entities" in chunk else None
        level = chunk["Level"]
        return level["Entities"] if "Entities" in level else None
    elif chunk_type == c.ENTITIES_DIR:
        return chunk["Entities"]
    return None


def _entity_id(entity):
    """ Return the id string of an entity compound, or None. """

    try:
        return entity["id"].value if "id" in entity else None
    except (AttributeError, TypeError):
        return None


def _entity_position_problem(entity, position_bound):
    """ Return the bad Pos of an entity or of one of its passengers.

    Inputs:
     - entity -- A TAG_Compound from an entity list.
     - position_bound -- Largest accepted absolute x/z coordinate.

    Return:
     - pos -- The offending (x, y, z) tuple, or None if every position is sane.

    Passengers are saved inside the entity they ride, so a broken passenger is
    reported against (and removed together with) its root vehicle.

    """

    if not isinstance(entity, nbt.TAG_Compound):
        return None
    pos = read_nbt_vector(entity["Pos"]) if "Pos" in entity else None
    if pos is not None and not is_position_sane(*pos, max_abs_xz=position_bound):
        return pos
    if "Passengers" in entity:
        for passenger in entity["Passengers"]:
            bad = _entity_position_problem(passenger, position_bound)
            if bad is not None:
                return bad
    return None


def find_bad_entities(entity_list, position_bound=None):
    """ Find the entities of a chunk whose position fails is_position_sane().

    Inputs:
     - entity_list -- A TAG_List of entity compounds, or None.
     - position_bound -- Largest accepted absolute x/z coordinate, None for
                         the default.

    Return:
     - bad -- A list of (index, entity_id, pos) tuples, in list order.

    """

    if position_bound is None:
        position_bound = DEFAULT_POSITION_BOUND_XZ
    bad = []
    if entity_list is None:
        return bad
    for index, entity in enumerate(entity_list):
        pos = _entity_position_problem(entity, position_bound)
        if pos is not None:
            bad.append((index, _entity_id(entity), pos))
    return bad


def remove_bad_entities(region_file, x, z, position_bound=None):
    """ Remove only the entities of a chunk that fail the position check.

    Inputs:
     - region_file -- RegionFile object where the chunk is stored
     - x -- Integer, X local coordinate of the chunk in the region file
     - z -- Integer, Z local coordinate of the chunk in the region file
     - position_bound -- Largest accepted absolute x/z coordinate

    Return:
     - removed -- A list of (entity_id, pos) tuples that were removed.
     - remaining -- Integer, entities left in the chunk.

    The chunk is only rewritten when something was removed.

    """

    chunk = region_file.get_chunk(x, z)
    entity_list = get_chunk_entity_list(chunk)
    bad = find_bad_entities(entity_list, position_bound)
    # Delete from the end so the earlier indexes stay valid.
    for index, _entity_id_value, _pos in reversed(bad):
        del entity_list[index]
    if bad:
        region_file.write_chunk(x, z, chunk)
    remaining = len(entity_list) if entity_list is not None else 0
    return [(entity_id, pos) for _index, entity_id, pos in bad], remaining


def _has_content(entity, tag_name):
    """ True when an entity has a non-empty tag, like CustomName or Tags. """

    if tag_name not in entity:
        return False
    tag = entity[tag_name]
    value = getattr(tag, 'value', None)
    if isinstance(value, str):
        return value.strip() != ""
    try:
        return len(tag) > 0
    except TypeError:
        return value is not None


class EntitySweepReport:
    """ Result of a --remove-entity-types sweep.

    Attributes:
     - removed -- {entity_id: count} of matched entities.
     - by_region -- {region path: {entity_id: count}}.
     - skipped_named -- Matched entities kept because they have a CustomName.
     - skipped_tagged -- Matched entities kept because they have Tags.
     - unreadable_chunks -- Chunks that could not be read and were left alone.
     - chunks_changed -- Chunks that were (or, in a dry run, would be) rewritten.

    """

    def __init__(self):
        self.removed = {}
        self.by_region = {}
        self.skipped_named = 0
        self.skipped_tagged = 0
        self.unreadable_chunks = 0
        self.chunks_changed = 0

    @property
    def total_removed(self):
        return sum(self.removed.values())

    def add(self, region_path, entity_id):
        self.removed[entity_id] = self.removed.get(entity_id, 0) + 1
        per_region = self.by_region.setdefault(region_path, {})
        per_region[entity_id] = per_region.get(entity_id, 0) + 1

    def summary(self, apply, world_root=None):
        """ Human readable end-of-run summary. """

        verb = "Removed" if apply else "Would remove"
        lines = []
        if not apply:
            lines.append("DRY RUN - nothing was written. Add --apply-entity-removal "
                         "to remove these entities.")
        lines.append("{0} {1} {2} in {3} {4}.".format(
            verb, _fmt_count(self.total_removed),
            _plural(self.total_removed, "entity", "entities"),
            _fmt_count(self.chunks_changed), _plural(self.chunks_changed, "chunk")))
        if self.removed:
            lines.append("")
            lines.append("By entity id:")
            lines.append(_summary_kv([(entity_id, _fmt_count(count))
                                      for entity_id, count in sorted(self.removed.items())]))
            lines.append("")
            lines.append("By region file:")
            for path in sorted(self.by_region):
                shown = path
                if world_root:
                    try:
                        shown = os.path.relpath(path, world_root)
                    except ValueError:
                        pass
                counts = ", ".join("{0} x{1}".format(entity_id, count)
                                   for entity_id, count in sorted(self.by_region[path].items()))
                lines.append("  {0}: {1}".format(shown, counts))
        if self.skipped_named or self.skipped_tagged:
            lines.append("")
            lines.append("Kept {0} matching {1} with a CustomName (use --include-named) and "
                         "{2} with Tags (use --include-tagged).".format(
                             _fmt_count(self.skipped_named),
                             _plural(self.skipped_named, "entity", "entities"),
                             _fmt_count(self.skipped_tagged)))
        if self.unreadable_chunks:
            lines.append("Skipped {0} unreadable {1}; scan the world to see why.".format(
                _fmt_count(self.unreadable_chunks), _plural(self.unreadable_chunks, "chunk")))
        return "\n".join(lines)


def _sweep_entity_list(entity_list, type_ids, include_named, include_tagged,
                       report, region_path):
    """ Remove matching entities from one entity list, recursing into passengers.

    Return:
     - changed -- True if anything was (or would be) removed.

    """

    changed = False
    keep = []
    for entity in entity_list:
        if not isinstance(entity, nbt.TAG_Compound):
            keep.append(entity)
            continue
        entity_id = _entity_id(entity)
        if entity_id is not None and entity_id.lower() in type_ids:
            if not include_named and _has_content(entity, "CustomName"):
                report.skipped_named += 1
            elif not include_tagged and _has_content(entity, "Tags"):
                report.skipped_tagged += 1
            else:
                # Anything riding a removed entity goes with it.
                report.add(region_path, entity_id)
                changed = True
                continue
        # A zombie riding a chicken is stored inside the chicken.
        if "Passengers" in entity:
            if _sweep_entity_list(entity["Passengers"], type_ids, include_named,
                                  include_tagged, report, region_path):
                changed = True
        keep.append(entity)
    if changed:
        entity_list.tags[:] = keep
    return changed


def sweep_entity_types(regionsets, type_ids, include_named=False,
                       include_tagged=False, apply=False, report=None):
    """ Remove every entity whose id is in type_ids from a set of region files.

    Inputs:
     - regionsets -- Iterable of RegionSet objects. POI region sets are skipped.
     - type_ids -- Set of namespaced entity ids to remove.
     - include_named -- Also remove matching entities that have a CustomName.
     - include_tagged -- Also remove matching entities that have Tags.
     - apply -- Write the changes. When False nothing is written and the
                report says what would have been removed.
     - report -- An EntitySweepReport to add to, a new one by default.

    Return:
     - report -- The EntitySweepReport.

    This reads every chunk directly and does not depend on a previous scan,
    so it also reaches chunks the server never loads.

    """

    if report is None:
        report = EntitySweepReport()
    type_ids = {i.lower() for i in type_ids}
    for regionset in regionsets:
        if regionset._get_region_type_directory() == c.POI_DIR:
            continue
        for scanned in regionset._get_list():
            try:
                region_file = region.RegionFile(scanned.path)
            except Exception:
                # Too small or unreadable; the normal scan reports it.
                continue
            try:
                for m in region_file.get_metadata():
                    try:
                        chunk = region_file.get_chunk(m.x, m.z)
                        entity_list = get_chunk_entity_list(chunk)
                    except Exception:
                        report.unreadable_chunks += 1
                        continue
                    if entity_list is None:
                        continue
                    if _sweep_entity_list(entity_list, type_ids, include_named,
                                          include_tagged, report, scanned.path):
                        report.chunks_changed += 1
                        if apply:
                            region_file.write_chunk(m.x, m.z, chunk)
            finally:
                region_file.close()
    return report


def _get_local_chunk_coords(chunkx, chunkz):
    """ Gives the chunk local coordinates from the global coordinates.
    
    Inputs:
     - chunkx -- Integer, X chunk global coordinate in the world.
     - chunkz -- Integer, Z chunk global coordinate in the world.
    
    Return:
     - x, z -- X and Z local coordinates of the chunk in the region file.

    """

    return chunkx % 32, chunkz % 32


def get_chunk_region(chunkX, chunkZ):
    """ Returns the name of the region file given global chunk coordinates.
    
    Inputs:
     - chunkx -- Integer, X chunk global coordinate in the world.
     - chunkz -- Integer, Z chunk global coordinate in the world.
    
    Return:
     - region_name -- A string with the name of the region file where the chunk
                     should be.

    """

    regionX = chunkX // 32
    regionZ = chunkZ // 32

    region_name = 'r.' + str(regionX) + '.' + str(regionZ) + '.mca'

    return region_name


def get_chunk_type(chunk):
    """Get the type of the chunk (Region/level, POIs or entities)
    
    Input:
     - chunk -- A chunk, from the NBT module
    
    Return:
     - type -- The chunk type (LEVEL_DIR, POI_DIR or ENTITIES_DIR)
    """
    
    # DataVersion was introduced in snapshot 15w32a (1.9)
    # https://minecraft.wiki/w/Data_version
    data_version = 0
    if "DataVersion" in chunk:
        data_version = chunk["DataVersion"].value
    
    # Region/level < 21w43a (1.17)
    if data_version < 2844 and "Level" in chunk:
        return c.LEVEL_DIR
    
    # Region/level >= 21w43a (1.18)
    # The "or" is important, because some tags doesn't seem to be mandatory
    if data_version >= 2844 and ("structures" in chunk or "sections" in chunk):
        return c.LEVEL_DIR
    
    # POIs >= 1.14 (Which snapshot ?)
    # I couldn't find when POI files were added
    # But it's certainly a snapshot after 18w43a (DataVersion = 1901)
    if data_version >= 1901 and "Sections" in chunk:
        return c.POI_DIR
    
    # Entities >= 20w45a (1.17)
    if data_version >= 2681 and "Entities" in chunk:
        return c.ENTITIES_DIR
    
    raise AssertionError("Unrecognized chunk type in get_chunk_type().")


def get_chunk_data_coords(nbt_file):
    """ Gets and returns the coordinates stored in the NBT structure of the chunk.
    
    Inputs:
     - nbt_file -- An NBT file. From the nbt module.
     
    Return:
     - coordX, coordZ -- Integers with the X and Z global coordinates of the chunk.

    Do not confuse with the coordinates returned by get_global_coords, which could be different,
    marking this chunk as wrong_located.

    """

    chunk_type = get_chunk_type(nbt_file)

    # Region file
    if chunk_type == c.LEVEL_DIR :
        # Since snapshot 21w43a (1.18), "Level" tag doesn't exist anymore
        if "DataVersion" in nbt_file and nbt_file["DataVersion"].value >= 2844 :
            level = nbt_file
        else :
            level = nbt_file.__getitem__('Level')

        coordX = level.__getitem__('xPos').value
        coordZ = level.__getitem__('zPos').value

    # Entities file :
    elif chunk_type == c.ENTITIES_DIR :
        coordX, coordZ = nbt_file.__getitem__('Position').value

    else :
        raise AssertionError("Unrecognized chunk in get_chunk_data_coords().")

    return coordX, coordZ


def _free_backup_path(path):
    """ Return `<path>.bak`, or `<path>.bak.N` if earlier backups exist. """

    candidate = path + ".bak"
    n = 1
    while exists(candidate):
        candidate = "{0}.bak.{1}".format(path, n)
        n += 1
    return candidate


def _external_chunk_files_for_region(region_path):
    """Return external .mcc payloads belonging to a region file."""
    try:
        region_x, region_z = get_region_coords(os.path.basename(region_path))
    except (ValueError, IndexError):
        return []

    result = []
    directory = os.path.dirname(region_path)
    for external_path in glob(join(directory, "c.*.*.mcc")):
        parts = os.path.basename(external_path).split('.')
        if len(parts) != 4 or parts[0] != 'c' or parts[3] != 'mcc':
            continue
        try:
            chunk_x = int(parts[1])
            chunk_z = int(parts[2])
        except ValueError:
            continue
        if chunk_x // 32 == region_x and chunk_z // 32 == region_z:
            result.append(external_path)
    return result


def _sync_external_chunk_files(source_region_path, destination_region_path):
    """Mirror external chunk sidecars when replacing a complete region."""
    if os.path.abspath(source_region_path) == os.path.abspath(destination_region_path):
        return

    for external_path in _external_chunk_files_for_region(destination_region_path):
        remove(external_path)

    destination_directory = os.path.dirname(destination_region_path)
    for external_path in _external_chunk_files_for_region(source_region_path):
        copy(external_path, join(destination_directory, os.path.basename(external_path)))


def get_region_coords(filename):
    """ Get and return a region file coordinates from path.
    
    Inputs:
     - filename -- Filename or path of the region file.
     
    Return:
     - coordX, coordZ -- X and z coordinates of the region file.

    """

    l = filename.split('.')
    coordX = int(l[1])
    coordZ = int(l[2])

    return coordX, coordZ


def get_global_chunk_coords(region_name, chunkX, chunkZ):
    """ Get and return a region file coordinates from path.
    
    Inputs:
     - region_name -- String with filename or path of the region file.
     - chunkX -- Integer, X local coordinate of the chunk
     - chunkZ -- Integer, Z local coordinate of the chunk

    Return:
     - coordX, coordZ -- X and z global coordinates of the
                         chunk in that region file.

    """

    regionX, regionZ = get_region_coords(region_name)
    chunkX += regionX * 32
    chunkZ += regionZ * 32

    return chunkX, chunkZ
