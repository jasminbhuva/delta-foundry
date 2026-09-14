"""Trusted, bounded parsers for Git pack (v2/v3) and pack index (v2) files.

These parsers never trust the producer: every length is bounds-checked, every
zlib stream is inflated with an output cap equal to the size its header
declares, and the pack and index trailers are recomputed.
"""

import hashlib
import struct
import zlib

OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG, OBJ_OFS_DELTA, OBJ_REF_DELTA = 1, 2, 3, 4, 6, 7
TYPE_NAMES = {1: "commit", 2: "tree", 3: "blob", 4: "tag", 6: "ofs-delta", 7: "ref-delta"}
HASH_LEN = {"sha1": 20, "sha256": 32}
CHUNK = 1 << 16


class PackError(Exception):
    pass


def _hasher(fmt):
    return hashlib.sha1() if fmt == "sha1" else hashlib.sha256()


def parse_pack(data, fmt, max_entries=10_000_000):
    """Parse a whole pack held in `data` (bytes/mmap).

    Returns dict(version, count, entries) where each entry is a tuple
    (offset, end, type, size, base) and base is None, ('ofs', offset) or
    ('ref', raw_oid_bytes).
    """
    hl = HASH_LEN[fmt]
    n_bytes = len(data)
    if n_bytes < 12 + hl:
        raise PackError("pack shorter than header plus trailer (%d bytes)" % n_bytes)
    if bytes(data[:4]) != b"PACK":
        raise PackError("bad pack signature")
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        raise PackError("unsupported pack version %d" % version)
    if count > max_entries:
        raise PackError("implausible object count %d" % count)
    body_end = n_bytes - hl
    h = _hasher(fmt)
    mv = memoryview(data)
    for i in range(0, body_end, 1 << 22):
        h.update(mv[i:min(body_end, i + (1 << 22))])
    if h.digest() != bytes(data[body_end:]):
        raise PackError("pack trailer checksum mismatch (corrupt, truncated or trailing data)")
    pos = 12
    entries = []
    for _ in range(count):
        if pos >= body_end:
            raise PackError("pack truncated: fewer entries than header count %d" % count)
        start = pos
        c = data[pos]
        pos += 1
        typ = (c >> 4) & 7
        size = c & 15
        shift = 4
        while c & 0x80:
            if pos >= body_end or shift > 60:
                raise PackError("bad entry header at %d" % start)
            c = data[pos]
            pos += 1
            size |= (c & 0x7F) << shift
            shift += 7
        base = None
        if typ == OBJ_OFS_DELTA:
            if pos >= body_end:
                raise PackError("truncated ofs-delta header at %d" % start)
            c = data[pos]
            pos += 1
            ofs = c & 0x7F
            n = 0
            while c & 0x80:
                n += 1
                if pos >= body_end or n > 9:
                    raise PackError("bad ofs-delta offset at %d" % start)
                c = data[pos]
                pos += 1
                ofs = ((ofs + 1) << 7) | (c & 0x7F)
            if ofs <= 0 or ofs > start - 12:
                raise PackError("ofs-delta base outside pack at %d" % start)
            base = ("ofs", start - ofs)
        elif typ == OBJ_REF_DELTA:
            if pos + hl > body_end:
                raise PackError("truncated ref-delta base at %d" % start)
            base = ("ref", bytes(data[pos:pos + hl]))
            pos += hl
        elif typ not in (OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG):
            raise PackError("invalid object type %d at %d" % (typ, start))
        d = zlib.decompressobj()
        produced = 0
        fed_to = min(pos + CHUNK, body_end)
        buf = mv[pos:fed_to]
        while True:
            out = d.decompress(buf, size - produced + 1)
            produced += len(out)
            if produced > size:
                raise PackError("entry at %d inflates beyond declared size %d" % (start, size))
            if d.eof:
                end = fed_to - len(d.unused_data) - len(d.unconsumed_tail)
                break
            if d.unconsumed_tail:
                buf = d.unconsumed_tail
                continue
            if fed_to >= body_end:
                raise PackError("zlib stream at %d runs past pack end" % start)
            nxt = min(fed_to + CHUNK, body_end)
            buf = mv[fed_to:nxt]
            fed_to = nxt
        if produced != size:
            raise PackError("entry at %d inflates to %d, header says %d" % (start, produced, size))
        pos = end
        entries.append((start, end, typ, size, base))
    if pos != body_end:
        raise PackError("%d unexplained bytes after the last entry" % (body_end - pos))
    return {"version": version, "count": count, "entries": entries}


def parse_idx(data, fmt):
    """Parse a v2 pack index. Returns (list of (oid_hex, offset), pack_checksum)."""
    hl = HASH_LEN[fmt]
    if len(data) < 8 + 1024 + 2 * hl or bytes(data[:4]) != b"\xfftOc":
        raise PackError("not a v2 pack index")
    if struct.unpack(">I", data[4:8])[0] != 2:
        raise PackError("unsupported index version")
    h = _hasher(fmt)
    h.update(data[:len(data) - hl])
    if h.digest() != bytes(data[len(data) - hl:]):
        raise PackError("index checksum mismatch")
    fan = struct.unpack(">256I", data[8:8 + 1024])
    n = fan[255]
    p = 8 + 1024
    need = p + n * (hl + 8) + 2 * hl
    if len(data) < need:
        raise PackError("index truncated")
    oids = [bytes(data[p + i * hl:p + (i + 1) * hl]).hex() for i in range(n)]
    p += n * hl + 4 * n
    offs32 = struct.unpack(">%dI" % n, data[p:p + 4 * n])
    p += 4 * n
    large = []
    n_large = sum(1 for o in offs32 if o & 0x80000000)
    if n_large:
        if len(data) < p + 8 * n_large + 2 * hl:
            raise PackError("index large-offset table truncated")
        large = struct.unpack(">%dQ" % n_large, data[p:p + 8 * n_large])
        p += 8 * n_large
    out = []
    for oid, o in zip(oids, offs32):
        if o & 0x80000000:
            k = o & 0x7FFFFFFF
            if k >= len(large):
                raise PackError("bad large offset reference")
            o = large[k]
        out.append((oid, o))
    if p + 2 * hl != len(data):
        raise PackError("unexplained bytes in index")
    pack_sum = bytes(data[p:p + hl])
    return out, pack_sum


def chain_depths(entries, oid_to_offset):
    """Return {offset: depth} (number of deltas to apply) for every entry.

    Raises PackError on a missing/external base or a cycle.
    """
    by_off = {e[0]: e for e in entries}
    depth = {}
    for e in entries:
        if e[0] in depth:
            continue
        chain = []
        cur = e
        seen = set()
        while True:
            off = cur[0]
            if off in depth:
                d = depth[off]
                break
            if off in seen:
                raise PackError("delta cycle through offset %d" % off)
            seen.add(off)
            base = cur[4]
            if base is None:
                d = 0
                depth[off] = 0
                break
            chain.append(off)
            if base[0] == "ofs":
                boff = base[1]
            else:
                boff = oid_to_offset.get(base[1].hex())
                if boff is None:
                    raise PackError("ref-delta at %d names a base outside the pack" % off)
            nxt = by_off.get(boff)
            if nxt is None:
                raise PackError("delta at %d has no entry at base offset %d" % (off, boff))
            cur = nxt
        for off in reversed(chain):
            d += 1
            depth[off] = d
    return depth


def pack_stats(entries, depths):
    """Diagnostics: counts and compressed bytes per stored representation."""
    st = {"entries": len(entries), "by_type": {}, "max_depth": 0, "depth_hist": {}}
    for off, end, typ, size, base in entries:
        name = TYPE_NAMES[typ]
        t = st["by_type"].setdefault(name, {"count": 0, "stored_bytes": 0, "inflated_bytes": 0})
        t["count"] += 1
        t["stored_bytes"] += end - off
        t["inflated_bytes"] += size
        d = depths.get(off, 0)
        st["max_depth"] = max(st["max_depth"], d)
        st["depth_hist"][d] = st["depth_hist"].get(d, 0) + 1
    return st
