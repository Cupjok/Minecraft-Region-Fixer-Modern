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

""" Entity id presets for --remove-entity-types.

Keep these lists plain so adding a new mob is a one line change. Anything a
player could care about (villagers, tameable or rideable animals, item
entities, item frames, armor stands, paintings, boats, minecarts) must never
appear here: the presets are the only safety net for those, there is no
separate exception list.
"""


# Vanilla hostile mobs. Neutral mobs (enderman, piglin, zombified piglin,
# wolves, bees...) and the two bosses are left out on purpose: the ender
# dragon is part of the End fight state and a wither is usually deliberate.
HOSTILE = frozenset([
    "minecraft:blaze",
    "minecraft:bogged",
    "minecraft:breeze",
    "minecraft:camel_husk",
    "minecraft:cave_spider",
    "minecraft:creaking",
    "minecraft:creeper",
    "minecraft:drowned",
    "minecraft:elder_guardian",
    "minecraft:endermite",
    "minecraft:evoker",
    "minecraft:ghast",
    "minecraft:giant",
    "minecraft:guardian",
    "minecraft:hoglin",
    "minecraft:husk",
    "minecraft:illusioner",
    "minecraft:magma_cube",
    "minecraft:parched",
    "minecraft:phantom",
    "minecraft:piglin_brute",
    "minecraft:pillager",
    "minecraft:ravager",
    "minecraft:shulker",
    "minecraft:silverfish",
    "minecraft:skeleton",
    "minecraft:slime",
    "minecraft:spider",
    "minecraft:stray",
    "minecraft:vex",
    "minecraft:vindicator",
    "minecraft:warden",
    "minecraft:witch",
    "minecraft:wither_skeleton",
    "minecraft:zoglin",
    "minecraft:zombie",
    "minecraft:zombie_nautilus",
    "minecraft:zombie_villager",
])

PRESETS = {
    "hostile": HOSTILE,
    "monsters": HOSTILE,
}


def normalize_entity_id(entity_id):
    """ Add the minecraft namespace to a bare id, the way the game does. """

    entity_id = entity_id.strip().lower()
    if entity_id and ":" not in entity_id:
        entity_id = "minecraft:" + entity_id
    return entity_id


def resolve_entity_types(spec):
    """ Turn a --remove-entity-types value into a set of entity ids.

    Inputs:
     - spec -- Comma separated preset names and/or entity ids, for example
               "hostile,minecraft:enderman".

    Return:
     - ids -- A set of namespaced entity ids.

    Presets and explicit ids are combined, so an id the presets do not know
    about yet can simply be added on the command line.

    """

    ids = set()
    for token in spec.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token in PRESETS:
            ids.update(PRESETS[token])
        else:
            ids.add(normalize_entity_id(token))
    return ids
