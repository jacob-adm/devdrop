# SPDX-License-Identifier: Apache-2.0
"""Decode the protobuf XML inside an Android App Bundle.

An .aab stores base/manifest/AndroidManifest.xml as an aapt2 `XmlNode`
message (frameworks/base/tools/aapt2/Resources.proto). Only the fields
needed to rebuild the element tree are read; the result uses the same
Element class as the binary XML decoder, so APK and AAB share one reader.
"""

import struct

from devdrop.axml import ATTR_IDS, AxmlError, Element

# XmlNode
NODE_ELEMENT = 1
# XmlElement
EL_NAME, EL_ATTRIBUTE, EL_CHILD = 3, 4, 5
# XmlAttribute
AT_NAME, AT_VALUE, AT_RESOURCE_ID, AT_COMPILED = 2, 3, 5, 6
# Item
ITEM_REF, ITEM_PRIM = 1, 7
# Reference
REF_ID = 2
# Primitive
PRIM_INT_DEC, PRIM_INT_HEX, PRIM_BOOL = 6, 7, 8

MAX_DEPTH = 64


def _varint(buf, i):
    result = shift = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise AxmlError("truncated protobuf varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def fields(buf):
    """Yield (field number, wire type, value) for one message."""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            if i + 8 > len(buf):
                raise AxmlError("truncated protobuf fixed64")
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:
            n, i = _varint(buf, i)
            if i + n > len(buf):
                raise AxmlError("protobuf field runs past the end")
            value, i = buf[i:i + n], i + n
        elif wire == 5:
            if i + 4 > len(buf):
                raise AxmlError("truncated protobuf fixed32")
            value, i = buf[i:i + 4], i + 4
        else:
            raise AxmlError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def _compiled(item):
    """Typed value of an Item, or None when it carries none we use."""
    for number, wire, value in fields(item):
        if wire != 2:                    # nested messages only; anything else is malformed or unused
            continue
        if number == ITEM_PRIM:
            for pnum, pwire, pval in fields(value):
                if pnum == PRIM_BOOL and pwire == 0:
                    return bool(pval)
                if pnum in (PRIM_INT_DEC, PRIM_INT_HEX) and pwire == 0:
                    return struct.unpack("<i", struct.pack("<I", pval & 0xFFFFFFFF))[0]
        if number == ITEM_REF:
            for rnum, rwire, rval in fields(value):
                if rnum == REF_ID and rwire == 0:
                    return f"@0x{rval:08x}"
    return None


def _attribute(buf):
    name = text = ""
    resource_id = None
    typed = None
    for number, wire, value in fields(buf):
        if number == AT_NAME and wire == 2:
            name = value.decode("utf-8", "replace")
        elif number == AT_VALUE and wire == 2:
            text = value.decode("utf-8", "replace")
        elif number == AT_RESOURCE_ID and wire == 0:
            resource_id = value
        elif number == AT_COMPILED and wire == 2:
            typed = _compiled(value)
    key = ATTR_IDS.get(resource_id) or name
    return key, (typed if typed is not None else text)


def _element(buf, depth):
    if depth > MAX_DEPTH:
        raise AxmlError("protobuf XML nested too deeply")
    name, attrs, children = "", {}, []
    for number, wire, value in fields(buf):
        if wire != 2:
            continue
        if number == EL_NAME:
            name = value.decode("utf-8", "replace")
        elif number == EL_ATTRIBUTE:
            key, val = _attribute(value)
            attrs[key] = val
        elif number == EL_CHILD:
            child = _node(value, depth + 1)
            if child is not None:
                children.append(child)
    el = Element(name, attrs)
    el.children = children
    return el


def _node(buf, depth=0):
    for number, wire, value in fields(buf):
        if number == NODE_ELEMENT and wire == 2:
            return _element(value, depth)
    return None              # a text node


def parse(buf):
    """Root Element of a protobuf XmlNode document."""
    root = _node(buf)
    if root is None:
        raise AxmlError("protobuf XML has no root element")
    return root
