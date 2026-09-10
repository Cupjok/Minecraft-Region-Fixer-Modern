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

import math
import numbers
import platform
import sys
import traceback


# Default bounds for is_position_sane(). The horizontal one matches the
# 30,000,000 block hard limit of the vanilla world border.
DEFAULT_POSITION_BOUND_XZ = 30000000
DEFAULT_POSITION_BOUND_Y = 20000000

# Integer.MAX_VALUE // 16. A block coordinate whose chunk coordinate reaches
# this value makes vanilla throw "Trying to create chunk out of reasonable
# bounds", so it is never accepted whatever bound the user configured.
MAX_CHUNK_COORDINATE = 134217727


def is_position_sane(x, y, z, max_abs_xz=DEFAULT_POSITION_BOUND_XZ,
                     max_abs_y=DEFAULT_POSITION_BOUND_Y):
    """ Return True if an entity or player position is believable.

    Inputs:
     - x, y, z -- Numbers, the position to check.
     - max_abs_xz -- Largest accepted absolute value for x and z.
     - max_abs_y -- Largest accepted absolute value for y.

    Return:
     - boolean -- False for None, NaN, infinity, anything beyond the bounds
                  and anything whose chunk coordinate reaches
                  MAX_CHUNK_COORDINATE.

    """

    for v in (x, y, z):
        if v is None or math.isnan(v) or math.isinf(v):
            return False
    if abs(x) > max_abs_xz or abs(z) > max_abs_xz:
        return False
    if abs(y) > max_abs_y:
        return False
    # Checked separately so a very large --position-bound cannot let a
    # position through that vanilla itself would refuse to load.
    if abs(x) // 16 >= MAX_CHUNK_COORDINATE or abs(z) // 16 >= MAX_CHUNK_COORDINATE:
        return False
    return True


def is_vector_finite(values):
    """ Return True if every value of a vector (for example Motion) is finite. """

    return all(v is not None and math.isfinite(v) for v in values)


def read_nbt_vector(tag, length=3):
    """ Read a TAG_List of numbers, such as Pos or Motion, as a tuple.

    Inputs:
     - tag -- An nbt TAG_List, or None.
     - length -- Number of elements the list must have.

    Return:
     - tuple -- The values as floats, or None when the tag is missing or is
                not a list of exactly `length` numbers.

    A malformed tag returns None instead of raising. Vanilla reads such a tag
    as zeros, so it is not a crash risk and not something to report.

    """

    if tag is None:
        return None
    try:
        items = list(tag)
    except TypeError:
        return None
    if len(items) != length:
        return None
    values = []
    for item in items:
        value = getattr(item, 'value', None)
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            return None
        values.append(float(value))
    return tuple(values)


def get_str_from_traceback(ty, value, tb):
    """ Return a string from a traceback plus exception.
    
    Inputs:
     - ty -- Exception type
     - value -- value of the traceback
     - tb -- Traceback
    
    """

    t = traceback.format_exception(ty, value, tb)
    s = str(ty) + "\n"
    for i in t:
        s += i
    return s


# Stolen from:
# http://stackoverflow.com/questions/3041986/python-command-line-yes-no-input
def query_yes_no(question, default="yes"):
    """Ask a yes/no question via raw_input() and return their answer.

    "question" is a string that is presented to the user.
    "default" is the presumed answer if the user just hits <Enter>.
        It must be "yes" (the default), "no" or None (meaning
        an answer is required of the user).

    The "answer" return value is one of "yes" or "no".
    """
    valid = {"yes": True, "y": True, "ye": True,
             "no": False, "n": False
             }
    if default is None:
        prompt = " [y/n] "
    elif default == "yes":
        prompt = " [Y/n] "
    elif default == "no":
        prompt = " [y/N] "
    else:
        raise ValueError("invalid default answer: '%s'" % default)

    while True:
        sys.stdout.write(question + prompt)
        choice = input().lower()
        if default is not None and choice == '':
            return valid[default]
        elif choice in valid:
            return valid[choice]
        else:
            sys.stdout.write("Please respond with 'yes' or 'no' "
                             "(or 'y' or 'n').\n")


# stolen from minecraft overviewer
# https://github.com/overviewer/Minecraft-Overviewer/
def is_bare_console():
    """Returns true if the python script is running in a bare console
    
    In Windows, that is, if the script wasn't started in a cmd.exe
    session.

    """

    if platform.system() == 'Windows':
        try:
            import ctypes
            GetConsoleProcessList = ctypes.windll.kernel32.GetConsoleProcessList
            num = GetConsoleProcessList(ctypes.byref(ctypes.c_int(0)), ctypes.c_int(1))
            if num == 1:
                return True

        except Exception:
            pass
    return False


def entitle(text, level=0):
    """ Put the text in a title with lot's of hashes around it. """

    t = ''
    if level == 0:
        t += "\n"
        t += "{0:#^60}\n".format('')
        t += "{0:#^60}\n".format(' ' + text + ' ')
        t += "{0:#^60}\n".format('')
    return t


def table(columns):
    """ Generates a text containing a pretty table. 
    
    Input:
     - columns -- A list containing lists in which each one of the is a column 
                 of the table.
    
    """

    def get_max_len(l):
        """ Takes a list of strings and returns the length of the biggest string  """
        m = 0
        for e in l:
            if len(str(e)) > m:
                m = len(str(e))
        return m

    text = ""
    # stores the size of the biggest element in that column
    ml = []
    # fill up ml
    for c in columns:
        m = 0
        t = get_max_len(c)
        if t > m:
            m = t
        ml.append(m)
    # get the total width of the table:
    ml_total = 0
    for i in range(len(ml)):
        ml_total += ml[i] + 2  # size of each word + 2 spaces
    ml_total += 1 + 2  # +1 for the separator | and +2 for the borders
    text += "-" * ml_total + "\n"
    # all the columns have the same number of rows
    row = len(columns[0])
    for r in range(row):
        line = "|"
        # put all the elements in this row together with spaces
        for i in range(len(columns)):
            line += "{0: ^{width}}".format(columns[i][r], width=ml[i] + 2)
            # add a separator for the first column
            if i == 0:
                line += "|"

        text += line + "|" + "\n"
        if r == 0:
            text += "-" * ml_total + "\n"
    text += "-" * ml_total
    return text

