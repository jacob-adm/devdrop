# SPDX-License-Identifier: Apache-2.0
"""Decode Android binary XML (the compiled AndroidManifest.xml inside an APK).

Format: a RES_XML_TYPE chunk holding a string pool, an optional resource-id
map, and start/end element chunks whose attributes carry typed values
(frameworks/base/libs/androidfw/include/androidfw/ResourceTypes.h).
"""

import struct

RES_STRING_POOL = 0x0001
RES_XML = 0x0003
RES_XML_START_ELEMENT = 0x0102
RES_XML_END_ELEMENT = 0x0103
RES_XML_RESOURCE_MAP = 0x0180
UTF8_FLAG = 1 << 8

# Res_value data types
TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_BOOLEAN = 0x12

# android: attribute resource ids, used when attribute names are stripped.
ATTR_IDS = {
    0x01010003: "name",
    0x0101000E: "enabled",
    0x0101000F: "debuggable",
    0x0101020C: "minSdkVersion",
    0x01010270: "targetSdkVersion",
    0x0101021B: "versionCode",
    0x0101021C: "versionName",
    0x01010572: "compileSdkVersion",
}


class AxmlError(ValueError):
    pass


class Element:
    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs        # local name -> decoded value
        self.children = []

    def find_all(self, tag):
        for c in self.children:
            if c.tag == tag:
                yield c


def _u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def _string_pool(buf, start):
    header_size = _u16(buf, start + 2)
    size = _u32(buf, start + 4)
    count, _styles, flags, strings_start = struct.unpack_from("<IIII", buf, start + 8)
    end = start + size
    if end > len(buf) or start + header_size + 4 * count > end:
        raise AxmlError("string pool runs past the end of the file")
    utf8 = bool(flags & UTF8_FLAG)
    base = start + strings_start
    out = []
    for i in range(count):
        off = base + _u32(buf, start + header_size + 4 * i)
        if utf8:
            off += 2 if buf[off] & 0x80 else 1           # UTF-16 length, unused
            n = buf[off]
            if n & 0x80:
                n = ((n & 0x7F) << 8) | buf[off + 1]
                off += 2
            else:
                off += 1
            raw = buf[off:off + n]
            text = raw.decode("utf-8", errors="replace")
        else:
            n = _u16(buf, off)
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | _u16(buf, off + 2)
                off += 4
            else:
                off += 2
            raw = buf[off:off + 2 * n]
            text = raw.decode("utf-16-le", errors="replace")
        if off + len(raw) > end or len(raw) < (n if utf8 else 2 * n):
            raise AxmlError("string runs past the end of the string pool")
        out.append(text)
    return out


def _value(strings, raw, dtype, data):
    if dtype == TYPE_STRING:
        return strings[data] if data < len(strings) else ""
    if dtype in (TYPE_INT_DEC, TYPE_INT_HEX):
        return struct.unpack("<i", struct.pack("<I", data))[0]
    if dtype == TYPE_BOOLEAN:
        return data != 0
    if dtype == TYPE_REFERENCE:
        return f"@0x{data:08x}"
    if raw != 0xFFFFFFFF and raw < len(strings):
        return strings[raw]
    return data


def parse(buf):
    """Return the root Element of a binary XML document."""
    if len(buf) < 8 or _u16(buf, 0) != RES_XML:
        raise AxmlError("not an Android binary XML file")
    strings, resource_ids = [], []
    root, stack = None, []
    off = _u16(buf, 2)
    try:
        while off + 8 <= len(buf):
            ctype, header_size, size = struct.unpack_from("<HHI", buf, off)
            if size < 8 or off + size > len(buf):
                raise AxmlError(f"chunk at byte {off} runs past the end of the file")
            if ctype == RES_STRING_POOL:
                strings = _string_pool(buf, off)
            elif ctype == RES_XML_RESOURCE_MAP:
                resource_ids = list(struct.unpack_from(f"<{(size - header_size) // 4}I",
                                                       buf, off + header_size))
            elif ctype == RES_XML_START_ELEMENT:
                ext = off + header_size
                _ns, name, attr_start, attr_size, attr_count = struct.unpack_from("<IIHHH", buf, ext)
                attrs = {}
                for i in range(attr_count):
                    a = ext + attr_start + i * attr_size
                    _ans, aname, raw, _vsize, _res0, dtype, data = struct.unpack_from("<IIIHBBI", buf, a)
                    key = ATTR_IDS.get(resource_ids[aname]) if aname < len(resource_ids) else None
                    if key is None:
                        key = strings[aname] if aname < len(strings) else f"attr{aname}"
                    attrs[key] = _value(strings, raw, dtype, data)
                el = Element(strings[name] if name < len(strings) else "", attrs)
                if stack:
                    stack[-1].children.append(el)
                elif root is None:
                    root = el
                stack.append(el)
            elif ctype == RES_XML_END_ELEMENT:
                if stack:
                    stack.pop()
            off += size
    except (struct.error, IndexError) as e:
        raise AxmlError(f"truncated or malformed binary XML ({e})")
    if root is None:
        raise AxmlError("binary XML has no elements")
    return root
