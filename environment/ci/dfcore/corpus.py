"""Deterministic Delta Foundry case generation.

A case is described by a JSON-able spec (family, generator, params, seed,
object format, profile). `materialize()` turns a spec into:

  <out>/repo.git        read-only input repository (loose and/or packed objects)
  <out>/stdin.txt       exact `git pack-objects` standard input
  <out>/manifest.tsv    expected object set: "<oid> <type> <size>" per object
  <out>/schedules/*.txt read schedules (large cases only)
  <out>/case.json       spec, counts, byte totals, generator revision

Every byte depends only on the spec, the pinned Git binary used for
construction and the vendored excerpt bundles.
"""

import hashlib
import json
import os
import random
import re
import shutil
import struct
import subprocess
import zlib

from . import histbuild

GENERATOR_REVISION = "df-corpus-4"


def rng_for(*parts):
    h = hashlib.sha256("\x00".join(str(p) for p in parts).encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


# ---------------------------------------------------------------- excerpts

class Excerpt:
    """Linear upstream excerpt loaded from a vendored bundle."""

    def __init__(self, git, bundle, workdir):
        self.git = git
        self.repo = os.path.join(workdir, "excerpt-" + os.path.basename(bundle) + ".git")
        if not os.path.isdir(self.repo):
            histbuild.init_bare(git, self.repo, "sha1")
            histbuild.run_git(git, ["--git-dir=" + self.repo, "fetch", "-q", bundle,
                                    "refs/heads/main:refs/heads/main"])
        log = histbuild.run_git(git, ["--git-dir=" + self.repo, "log", "--reverse",
                                      "--format=%H%x00%ct%x00%B%x01", "refs/heads/main"])
        self.commits = []
        for rec in log.split(b"\x01"):
            rec = rec.strip(b"\n")
            if not rec:
                continue
            h, ct, body = rec.split(b"\x00", 2)
            self.commits.append((h.decode(), int(ct), body))
        self._cat = subprocess.Popen([git, "--git-dir=" + self.repo, "cat-file", "--batch"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     env=histbuild.git_env())
        self._blobs = {}
        self._trees = {}

    def __len__(self):
        return len(self.commits)

    def _read(self, oid):
        self._cat.stdin.write(oid.encode() + b"\n")
        self._cat.stdin.flush()
        hdr = self._cat.stdout.readline().split()
        size = int(hdr[2])
        data = self._cat.stdout.read(size)
        self._cat.stdout.read(1)
        return data

    def snapshot(self, i):
        h = self.commits[i][0]
        if h not in self._trees:
            ls = histbuild.run_git(self.git, ["--git-dir=" + self.repo, "ls-tree", "-r", "-z", h])
            ent = {}
            for e in ls.split(b"\x00"):
                if not e:
                    continue
                meta, path = e.split(b"\t", 1)
                mode, typ, oid = meta.split()
                ent[path.decode("utf-8", "surrogateescape")] = (mode, oid.decode())
            self._trees[h] = ent
        out, modes = {}, {}
        for p, (mode, oid) in self._trees[h].items():
            if oid not in self._blobs:
                self._blobs[oid] = self._read(oid)
            out[p] = self._blobs[oid]
            modes[p] = mode
        return out, modes

    def meta(self, i):
        h, ct, body = self.commits[i]
        return ct - histbuild.EPOCH, body

    def close(self):
        try:
            self._cat.stdin.close()
            self._cat.wait()
        except OSError:
            pass


# ------------------------------------------------------ content mutations

def _lines(b):
    return b.split(b"\n")


def text_edit(rng, data, donors, n_ops=1):
    """Apply seeded source-like edits: insert/delete/move/duplicate/rename."""
    lines = _lines(data)
    for _ in range(n_ops):
        if not lines:
            lines = [b""]
        op = rng.random()
        if op < 0.30 and donors:
            d = _lines(rng.choice(donors))
            a = rng.randrange(len(d))
            blk = d[a:a + rng.randint(2, 40)]
            at = rng.randrange(len(lines) + 1)
            lines[at:at] = blk
        elif op < 0.50 and len(lines) > 4:
            a = rng.randrange(len(lines))
            del lines[a:a + rng.randint(1, 25)]
        elif op < 0.70 and len(lines) > 10:
            a = rng.randrange(len(lines))
            blk = lines[a:a + rng.randint(3, 60)]
            del lines[a:a + len(blk)]
            at = rng.randrange(len(lines) + 1)
            lines[at:at] = blk
        elif op < 0.85:
            words = re.findall(rb"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", b"\n".join(lines[:400]))
            if words:
                w = rng.choice(words)
                new = w + b"_%d" % rng.randrange(100)
                pat = re.compile(rb"\b" + re.escape(w) + rb"\b")
                lines = [pat.sub(new, l) for l in lines]
        else:
            a = rng.randrange(len(lines))
            for k in range(a, min(len(lines), a + rng.randint(1, 30))):
                lines[k] = lines[k].replace(b"\t", b"    ") if rng.random() < 0.5 else b"\t" + lines[k]
    return b"\n".join(lines)


# ----------------------------------------------------- synthetic content

WORDS = (b"alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho "
         b"sigma tau upsilon phi chi psi omega foundry pack index chain window depth cache stream "
         b"object tree blob commit tag window buffer vector matrix region sample token record").split()


def gen_json_catalog(rng, n):
    items = []
    for i in range(n):
        items.append({"id": i, "name": "%s-%s-%d" % (rng.choice(WORDS).decode(), rng.choice(WORDS).decode(), i),
                      "version": "%d.%d.%d" % (rng.randrange(5), rng.randrange(30), rng.randrange(100)),
                      "tags": sorted({rng.choice(WORDS).decode() for _ in range(rng.randint(1, 4))}),
                      "weight": round(rng.random() * 1000, 3), "enabled": rng.random() < 0.8})
    return items


def mutate_catalog(rng, items):
    items = [dict(x) for x in items]
    for _ in range(rng.randint(1, max(2, len(items) // 15))):
        k = rng.randrange(len(items))
        f = rng.choice(["version", "weight", "enabled", "tags"])
        if f == "version":
            items[k]["version"] = "%d.%d.%d" % (rng.randrange(5), rng.randrange(30), rng.randrange(100))
        elif f == "weight":
            items[k]["weight"] = round(rng.random() * 1000, 3)
        elif f == "enabled":
            items[k]["enabled"] = not items[k]["enabled"]
        else:
            items[k]["tags"] = sorted({rng.choice(WORDS).decode() for _ in range(rng.randint(1, 4))})
    if rng.random() < 0.5:
        base = max(x["id"] for x in items) + 1
        items[rng.randrange(len(items) + 1):0] = gen_json_catalog(rng, rng.randint(1, 5))
        for j, x in enumerate(items):
            if x["id"] < 0:
                x["id"] = base + j
    if rng.random() < 0.3 and len(items) > 10:
        del items[rng.randrange(len(items))]
    return items


def render_catalog(items, style):
    if style == 0:
        return json.dumps(items, indent=2, sort_keys=True).encode()
    if style == 1:
        return b"\n".join(json.dumps(x, sort_keys=True).encode() for x in items) + b"\n"
    return json.dumps(items, separators=(",", ":"), sort_keys=True).encode()


def gen_lockfile(rng, n):
    return [[("pkg-%s-%d" % (rng.choice(WORDS).decode(), i)), "%d.%d.%d" % (rng.randrange(9), rng.randrange(40), rng.randrange(60)),
             rng.randbytes(48)] for i in range(n)]


def render_lockfile(entries):
    import base64
    out = []
    for name, ver, integ in entries:
        out.append(b'"%s@^%s":\n  version "%s"\n  resolved "https://registry.example.invalid/%s/-/%s-%s.tgz"\n  integrity sha512-%s\n'
                   % (name.encode(), ver.encode(), ver.encode(), name.encode(), name.encode(), ver.encode(),
                      base64.b64encode(integ)))
    return b"\n".join(out)


def gen_c_table(rng, n, seed_vals=None):
    vals = seed_vals or [rng.randrange(1 << 16) for _ in range(n)]
    rows = [b"/* generated table - do not edit */", b"static const unsigned short df_table[%d] = {" % len(vals)]
    for i in range(0, len(vals), 12):
        rows.append(b"  " + b", ".join(b"0x%04x" % v for v in vals[i:i + 12]) + b",")
    rows.append(b"};")
    return b"\n".join(rows) + b"\n", vals


def gen_log_lines(rng, t0, n):
    lvl = [b"INFO", b"INFO", b"INFO", b"WARN", b"DEBUG", b"ERROR"]
    out = []
    for i in range(n):
        t0 += rng.randint(1, 900)
        out.append(b"2026-%02d-%02dT%02d:%02d:%02dZ %s [%s] %s request=%08x bytes=%d latency_ms=%d"
                   % (1 + (t0 // 2592000) % 12, 1 + (t0 // 86400) % 28, (t0 // 3600) % 24, (t0 // 60) % 60, t0 % 60,
                      rng.choice(lvl), rng.choice(WORDS), b" ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 7))),
                      rng.getrandbits(32), rng.randrange(1 << 20), rng.randrange(5000)))
    return out, t0


def gen_markdown(rng, sections):
    out = []
    for title, paras in sections:
        out.append(b"## " + title)
        for p in paras:
            out.append(p)
            out.append(b"")
    return b"\n".join(out) + b"\n"


def rand_para(rng):
    return b" ".join(rng.choice(WORDS) for _ in range(rng.randint(20, 80))) + b"."


def minified_js(rng, n_funcs):
    parts = []
    for i in range(n_funcs):
        parts.append(b"function f%d(a,b){var c=a*%d+b;return c^%d}" % (i, rng.randrange(1000), rng.randrange(1 << 20)))
    return b";".join(parts)


# binary content

def gen_image(rng, w, h):
    px = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            o = (y * w + x) * 4
            px[o] = (x * 255 // w) & 255
            px[o + 1] = (y * 255 // h) & 255
            px[o + 2] = ((x ^ y) * 3) & 255
            px[o + 3] = 255
    for _ in range(w * h // 64):
        o = rng.randrange(w * h) * 4
        px[o + rng.randrange(3)] = rng.randrange(256)
    return px


def edit_image(rng, px, w, h):
    px = bytearray(px)
    for _ in range(rng.randint(1, 4)):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        rw, rh = rng.randint(4, w // 3), rng.randint(4, h // 3)
        col = bytes([rng.randrange(256) for _ in range(3)]) + b"\xff"
        for y in range(y0, min(h, y0 + rh)):
            o = (y * w + x0) * 4
            n = min(w - x0, rw)
            px[o:o + n * 4] = col * n
    if rng.random() < 0.3:
        s = rng.randint(1, 8) * 4
        px = px[s:] + px[:s]
    return px


def bmp_wrap(px, w, h):
    hdr = b"BM" + struct.pack("<IHHI", 54 + len(px), 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, len(px), 2835, 2835, 0, 0)
    return hdr + dib + bytes(px)


def gen_pages(rng, n_pages, page=4096):
    pages = []
    for i in range(n_pages):
        recs = bytearray()
        while len(recs) < page - 64:
            k = rng.randrange(1 << 24)
            recs += struct.pack("<IIH", k, rng.getrandbits(32), 12) + rng.choice(WORDS)[:8].ljust(8, b".")
        pages.append(struct.pack("<IIQ", 0xDF0CA7E5, i, rng.getrandbits(64)) + bytes(recs[:page - 16]))
    return pages


def edit_pages(rng, pages):
    pages = list(pages)
    for _ in range(rng.randint(1, 5)):
        i = rng.randrange(len(pages))
        p = bytearray(pages[i])
        o = 16 + rng.randrange(len(p) - 40)
        p[o:o + 22] = struct.pack("<IIH", rng.randrange(1 << 24), rng.getrandbits(32), 12) + b"updated."[:8].ljust(12, b"!")
        pages[i] = bytes(p)
    if rng.random() < 0.4:
        pages.insert(rng.randrange(len(pages) + 1), gen_pages(rng, 1)[0])
    if rng.random() < 0.2 and len(pages) > 4:
        del pages[rng.randrange(len(pages))]
    return pages


def gen_exe(rng, n_funcs):
    """Pseudo executable: code blocks (small opcode alphabet) plus an absolute pointer table."""
    funcs = []
    for _ in range(n_funcs):
        body = bytes(rng.choice(b"\x48\x89\xe5\x8b\x45\xfc\x01\xc0\x5d\xc3\x90\x0f\x1f\x44\xe8\xff")
                     for _ in range(rng.randint(32, 600)))
        funcs.append(body)
    return funcs


def link_exe(funcs, base=0x400000):
    offs, pos = [], 64
    for f in funcs:
        offs.append(pos)
        pos += len(f)
    code = b"".join(funcs)
    table = b"".join(struct.pack("<Q", base + o) for o in offs)
    # call sites inside code reference absolute targets -> relocations shift on edits
    code = bytearray(code)
    for i, o in enumerate(offs[:-1]):
        at = o - 64 + 8
        if at + 8 <= len(code):
            code[at:at + 8] = struct.pack("<Q", base + offs[(i * 7 + 3) % len(offs)])
    hdr = b"\x7fDFX" + struct.pack("<IQQ", len(funcs), len(code), len(table)).ljust(60, b"\0")
    return hdr + bytes(code) + table


def edit_exe(rng, funcs):
    funcs = list(funcs)
    for _ in range(rng.randint(1, 3)):
        i = rng.randrange(len(funcs))
        f = bytearray(funcs[i])
        at = rng.randrange(len(f))
        ins = bytes(rng.choice(b"\x48\x89\xe5\x8b\x45\xfc\x01\xc0\x90") for _ in range(rng.randint(1, 40)))
        f[at:at] = ins
        funcs[i] = bytes(f)
    if rng.random() < 0.2:
        funcs.insert(rng.randrange(len(funcs) + 1), gen_exe(rng, 1)[0])
    return funcs


def gen_floats(rng, n):
    return [rng.gauss(0, 1) for _ in range(n)]


def pack_floats(v):
    return struct.pack("<%df" % len(v), *v)


def gen_container(rng, members):
    out = bytearray(b"DFAR")
    for name, data, level in members:
        z = zlib.compress(data, level)
        out += struct.pack("<HI", len(name), len(z)) + name + z
    return bytes(out)


# ------------------------------------------------------------- families

def fam_real_linear(w, ex, rng, p):
    start, count = p["start"], p["count"]
    renames = p.get("renames", [])
    tag_every = p.get("tag_every", 0)
    for k, i in enumerate(range(start, min(len(ex), start + count))):
        snap, modes = ex.snapshot(i)
        for at, a, b in renames:
            if k >= at:
                snap = {(b + q[len(a):] if q.startswith(a) else q): v for q, v in snap.items()}
                modes = {(b + q[len(a):] if q.startswith(a) else q): v for q, v in modes.items()}
        when, msg = ex.meta(i)
        m = w.commit("refs/heads/main", snap, when, msg, author=b"Upstream Contributor <upstream@example.invalid>", modes=modes)
        if tag_every and k % tag_every == tag_every - 1:
            w.tag("v%d.%d" % (k // tag_every, k), m, when + 60, "release %d" % k)
    return {}


def fam_real_diverge(w, ex, rng, p):
    base, main_n, nb, per = p["base"], p["mainline"], p["branches"], p["per_branch"]
    main_marks = []
    keep = None
    for i in range(base, min(len(ex), base + main_n)):
        snap, modes = ex.snapshot(i)
        if p.get("subset"):
            if keep is None:
                keep = set(rng.sample(sorted(snap), min(len(snap), p["subset"])))
            snap = {q: v for q, v in snap.items() if q in keep}
            modes = {q: v for q, v in modes.items() if q in keep}
            if not snap:
                snap = {"placeholder": b"%d" % i}
        when, msg = ex.meta(i)
        main_marks.append((w.commit("refs/heads/main", snap, when, msg,
                                    author=b"Upstream Contributor <upstream@example.invalid>", modes=modes), snap, when))
    for b in range(nb):
        fork = rng.randrange(len(main_marks))
        mark, snap, when = main_marks[fork]
        snap = dict(snap)
        ref = "refs/heads/topic-%02d" % b
        parent = [mark]
        for c in range(per):
            files = sorted(snap)
            donors = [snap[f] for f in rng.sample(files, min(4, len(files)))]
            for _ in range(rng.randint(1, 3)):
                f = rng.choice(files)
                r = rng.random()
                if r < 0.08:
                    nf = "moved/%02d/%s" % (b, f.split("/")[-1])
                    if nf not in snap:
                        snap[nf] = snap.pop(f)
                        files = sorted(snap)
                        continue
                if r < 0.14:
                    nf = "copies/%02d-%d/%s" % (b, c, f.split("/")[-1])
                    snap[nf] = text_edit(rng, snap[f], donors, 1)
                    files = sorted(snap)
                    continue
                snap[f] = text_edit(rng, snap[f], donors, rng.randint(1, 3))
            when += rng.randint(600, 86400)
            m = w.commit(ref, snap, when, "topic %d change %d" % (b, c), parents=parent)
            parent = [m]
        w.tag("topic-%02d-done" % b, parent[0], when + 30, "topic %d complete" % b)
        if rng.random() < 0.5:
            tip_mark, tip_snap, tip_when = main_marks[-1]
            merged = dict(tip_snap)
            for f, v in snap.items():
                if f.startswith("moved/") or f.startswith("copies/") or rng.random() < 0.5:
                    merged[f] = v
            m = w.commit("refs/heads/main", merged, max(tip_when, when) + 120,
                         "Merge topic %d" % b, parents=[tip_mark, parent[0]])
            main_marks.append((m, merged, max(tip_when, when) + 120))
    return {}


def fam_gen_text(w, rng, p):
    n_commits = p["commits"]
    scale = p.get("scale", 1.0)
    state = {
        "catalog": gen_json_catalog(rng, int(300 * scale) + 5),
        "catalog_style": rng.randrange(3),
        "lock": gen_lockfile(rng, int(250 * scale) + 5),
        "table": gen_c_table(rng, int(3000 * scale) + 12)[1],
        "log": [], "t": 0,
        "doc": [(b"Section %d %s" % (i, rng.choice(WORDS)), [rand_para(rng) for _ in range(rng.randint(1, 4))])
                for i in range(int(40 * scale) + 3)],
        "js": int(400 * scale) + 5,
        "jsseed": rng.getrandbits(32),
        "csv": [[i, rng.randrange(10000), rng.choice(WORDS).decode(), round(rng.random(), 4)] for i in range(int(800 * scale) + 5)],
    }
    t = 0
    for c in range(n_commits):
        which = rng.sample(["catalog", "lock", "table", "log", "doc", "js", "csv"], rng.randint(1, 3))
        for k in which:
            if k == "catalog":
                state["catalog"] = mutate_catalog(rng, state["catalog"])
            elif k == "lock":
                for _ in range(rng.randint(1, 6)):
                    e = state["lock"][rng.randrange(len(state["lock"]))]
                    e[1] = "%d.%d.%d" % (rng.randrange(9), rng.randrange(40), rng.randrange(60))
                    e[2] = rng.randbytes(48)
                if rng.random() < 0.3:
                    state["lock"].insert(rng.randrange(len(state["lock"]) + 1), gen_lockfile(rng, 1)[0])
            elif k == "table":
                v = state["table"]
                for _ in range(rng.randint(1, 40)):
                    v[rng.randrange(len(v))] = rng.randrange(1 << 16)
                if rng.random() < 0.3:
                    at = rng.randrange(len(v))
                    v[at:at] = [rng.randrange(1 << 16) for _ in range(rng.randint(1, 30))]
            elif k == "log":
                lines, state["t"] = gen_log_lines(rng, state["t"], int(rng.randint(20, 120) * scale) + 1)
                state["log"].extend(lines)
                if len(state["log"]) > int(6000 * scale) + 50:
                    state["log"] = state["log"][len(state["log"]) // 3:]
            elif k == "doc":
                d = state["doc"]
                i = rng.randrange(len(d))
                paras = list(d[i][1])
                paras[rng.randrange(len(paras))] = rand_para(rng)
                d[i] = (d[i][0], paras)
                if rng.random() < 0.2:
                    d.insert(rng.randrange(len(d) + 1), (b"Section new %s" % rng.choice(WORDS), [rand_para(rng)]))
            elif k == "js":
                state["js"] += rng.randint(-3, 8)
                state["jsseed"] = rng.getrandbits(32) if rng.random() < 0.2 else state["jsseed"]
            elif k == "csv":
                rows = state["csv"]
                for _ in range(rng.randint(1, 30)):
                    r = rows[rng.randrange(len(rows))]
                    r[1] = rng.randrange(10000)
                    r[3] = round(rng.random(), 4)
                rows.extend([[len(rows) + j, rng.randrange(10000), rng.choice(WORDS).decode(), round(rng.random(), 4)]
                             for j in range(rng.randint(0, 20))])
        snap = {
            "data/catalog.json": render_catalog(state["catalog"], state["catalog_style"]),
            "deps/yarn.lock": render_lockfile(state["lock"]),
            "gen/table.c": gen_c_table(rng, 0, state["table"])[0],
            "logs/service.log": b"\n".join(state["log"]) + b"\n",
            "docs/guide.md": gen_markdown(rng, state["doc"]),
            "web/app.min.js": minified_js(random.Random(state["jsseed"]), max(5, state["js"])),
            "data/metrics.csv": b"id,value,label,ratio\n" + b"\n".join(
                b"%d,%d,%s,%s" % (r[0], r[1], r[2].encode(), repr(r[3]).encode()) for r in state["csv"]) + b"\n",
        }
        if p.get("copies"):
            snap["mirror/catalog.json"] = snap["data/catalog.json"]
            snap["mirror/guide-v%d.md" % (c // 25)] = snap["docs/guide.md"]
        t += rng.randint(300, 20000)
        m = w.commit("refs/heads/main", snap, t, "regenerate %d" % c)
        if c % 40 == 39:
            w.tag("gen-%d" % c, m, t + 5, "snapshot %d" % c)
    return {}


def fam_gen_binary(w, rng, p):
    n_commits = p["commits"]
    scale = p.get("scale", 1.0)
    iw = ih = max(16, int(256 * scale ** 0.5))
    img = gen_image(rng, iw, ih)
    pages = gen_pages(rng, max(4, int(160 * scale)))
    funcs = gen_exe(rng, max(8, int(400 * scale)))
    floats = gen_floats(rng, max(64, int(60000 * scale)))
    members = [(b"m%d.txt" % i, b"\n".join(rand_para(rng) for _ in range(rng.randint(2, 20))), 6) for i in range(max(2, int(12 * scale)))]
    t = 0
    for c in range(n_commits):
        which = rng.sample(["img", "pages", "exe", "floats", "arch"], rng.randint(1, 3))
        for k in which:
            if k == "img":
                img = edit_image(rng, img, iw, ih)
            elif k == "pages":
                pages = edit_pages(rng, pages)
            elif k == "exe":
                funcs = edit_exe(rng, funcs)
            elif k == "floats":
                for _ in range(rng.randint(1, 200)):
                    i = rng.randrange(len(floats))
                    floats[i] += rng.gauss(0, 0.01)
                if rng.random() < 0.2:
                    at = rng.randrange(len(floats))
                    floats[at:at] = gen_floats(rng, rng.randint(1, 500))
            elif k == "arch":
                i = rng.randrange(len(members))
                members[i] = (members[i][0], members[i][1] + b"\n" + rand_para(rng), members[i][2])
        snap = {
            "assets/sprite.bmp": bmp_wrap(img, iw, ih),
            "db/table.pages": b"".join(pages),
            "bin/tool.dfx": link_exe(funcs),
            "data/weights.f32": pack_floats(floats),
            "pkg/bundle.dfar": gen_container(rng, members),
        }
        t += rng.randint(300, 20000)
        m = w.commit("refs/heads/main", snap, t, "assets %d" % c)
        if c % 30 == 29:
            w.tag("assets-%d" % c, m, t + 5, "asset release %d" % c)
    return {}


def fam_controls(w, rng, p):
    n_commits = p["commits"]
    scale = p.get("scale", 1.0)
    rnd = {"noise/r%02d.bin" % i: rng.randbytes(rng.randint(64, int(200000 * scale) + 64)) for i in range(max(2, int(10 * scale)))}
    tiny = {"tiny/t%03d.txt" % i: rng.choice(WORDS) + b"%d\n" % i for i in range(max(3, int(80 * scale)))}
    comp = {"packed/c%02d.z" % i: zlib.compress(b" ".join(rng.choice(WORDS) for _ in range(rng.randint(100, int(20000 * scale) + 100))), 9)
            for i in range(max(2, int(8 * scale)))}
    t = 0
    for c in range(n_commits):
        r = rng.random()
        if r < 0.35:
            k = rng.choice(sorted(rnd))
            rnd[k] = rng.randbytes(len(rnd[k]))
        elif r < 0.55:
            k = rng.choice(sorted(rnd))
            rnd[k] = rnd[k] + rng.randbytes(rng.randint(16, 4096))
        elif r < 0.75:
            for _ in range(rng.randint(1, 10)):
                k = rng.choice(sorted(tiny))
                tiny[k] = rng.choice(WORDS) + b" %d %d\n" % (c, rng.randrange(1000))
        else:
            k = rng.choice(sorted(comp))
            comp[k] = zlib.compress(b" ".join(rng.choice(WORDS) for _ in range(rng.randint(100, int(20000 * scale) + 100))), 9)
        snap = {}
        snap.update(rnd)
        snap.update(tiny)
        snap.update(comp)
        if c % 7 == 0:
            snap["dup/copy-%d.bin" % (c % 3)] = rnd[sorted(rnd)[0]]
        t += rng.randint(300, 20000)
        w.commit("refs/heads/main", snap, t, "controls %d" % c)
    return {}


# small-case edge generators -------------------------------------------

def fam_edge(w, rng, p, excerpts):
    kind = p["kind"]
    t = 0
    if kind == "empty_and_tiny":
        m = w.commit("refs/heads/main", {}, 1, "empty tree")
        m = w.commit("refs/heads/main", {"empty": b"", "one": b"1", "nl": b"\n"}, 2, "tiny files")
        w.tag("t-empty", m, 3, "tag")
        w.commit("refs/heads/main", {"empty": b"", "one": b"1"}, 4, "remove nl")
    elif kind == "same_content_many_paths":
        blob = b"".join(rng.choice(WORDS) + b"\n" for _ in range(rng.randint(10, 500)))
        for c in range(rng.randint(2, 6)):
            snap = {"d%d/f%d.txt" % (i % 5, i): blob for i in range(rng.randint(3, 30))}
            snap["variant"] = blob + b"%d" % c
            w.commit("refs/heads/main", snap, c + 1, "dup %d" % c)
    elif kind == "deep_chain":
        data = b"".join(rng.choice(WORDS) + b" " for _ in range(rng.randint(200, 3000)))
        for c in range(rng.randint(20, 90)):
            data = text_edit(rng, data, [], 1) if rng.random() < 0.5 else data + b" v%d" % c
            w.commit("refs/heads/main", {"chain.txt": data}, c + 1, "rev %d" % c)
    elif kind == "large_object":
        big = rng.randbytes(rng.randint(200000, 900000)) + b"".join(rng.choice(WORDS) for _ in range(200000))
        for c in range(rng.randint(2, 4)):
            b2 = bytearray(big)
            at = rng.randrange(len(b2))
            b2[at:at] = rng.randbytes(rng.randint(1, 5000))
            big = bytes(b2)
            w.commit("refs/heads/main", {"big.bin": big, "small.txt": b"%d\n" % c}, c + 1, "big %d" % c)
    elif kind == "copy_boundaries":
        shared = rng.randbytes(rng.choice([65535, 65536, 65537, 131072, 200003]))
        for c in range(3):
            pre = rng.randbytes(rng.randint(0, 100))
            w.commit("refs/heads/main", {"a.bin": pre + shared + rng.randbytes(c * 7), "b.bin": shared[::-1][:70000]},
                     c + 1, "boundary %d" % c)
    elif kind == "binary_nul":
        data = bytearray(rng.randint(100, 20000))
        for c in range(rng.randint(3, 12)):
            for _ in range(rng.randint(1, 20)):
                data[rng.randrange(len(data))] = rng.choice([0, 0, 255, rng.randrange(256)])
            w.commit("refs/heads/main", {"nul.bin": bytes(data), "x\x01ctl name": b"\0\0\0%d" % c}, c + 1, "nul %d" % c)
    elif kind == "tags_everywhere":
        m1 = w.commit("refs/heads/main", {"a": b"alpha\n", "dir/b": b"beta\n"}, 1, "one")
        t1 = w.tag("annotated-on-commit", m1, 2, "on commit")
        w.tag("nested", t1, 3, "tag of a tag")
        m2 = w.commit("refs/heads/main", {"a": b"alpha2\n", "dir/b": b"beta\n", "c": b"gamma" * 50}, 4, "two")
        w.reset("refs/heads/side", m1)
        w.commit("refs/heads/side", {"a": b"side\n"}, 5, "side")
        w.tag("final", m2, 6, "final")
    elif kind == "modes_and_symlinks":
        for c in range(rng.randint(2, 6)):
            snap = {"run.sh": b"#!/bin/sh\necho %d\n" % c, "link": b"run.sh", "lib/x.c": b"int x = %d;\n" % c}
            w.commit("refs/heads/main", snap, c + 1, "modes %d" % c,
                     modes={"run.sh": b"100755", "link": b"120000", "lib/x.c": b"100644"})
    elif kind == "merges":
        m0 = w.commit("refs/heads/main", {"f": b"base\n" * 50}, 1, "base")
        tips = []
        for b in range(rng.randint(2, 4)):
            w.reset("refs/heads/b%d" % b, m0)
            tips.append(w.commit("refs/heads/b%d" % b, {"f": (b"base\n" * 50) + b"branch %d\n" % b, "g%d" % b: b"x" * (b + 1)},
                                 2 + b, "branch %d" % b))
        w.commit("refs/heads/main", {"f": b"merged\n" * 50}, 20, "octopus", parents=[m0] + tips)
    elif kind == "incompressible":
        for c in range(rng.randint(2, 8)):
            w.commit("refs/heads/main", {"r%d" % i: rng.randbytes(rng.randint(1, 30000)) for i in range(rng.randint(1, 6))},
                     c + 1, "noise %d" % c)
    elif kind == "renames":
        ex = excerpts[p["excerpt"]]
        i = p["start"] % max(1, len(ex) - 6)
        for k in range(rng.randint(3, 6)):
            snap, modes = ex.snapshot(min(len(ex) - 1, i + k))
            keys = sorted(snap)[:rng.randint(3, 12)]
            pref = "v%d/" % (k % 2)
            w.commit("refs/heads/main", {pref + q.replace("/", "_"): snap[q] for q in keys}, k + 1, "renamed %d" % k)
    elif kind == "excerpt_slice":
        ex = excerpts[p["excerpt"]]
        i = p["start"] % max(1, len(ex) - 8)
        keep = None
        for k in range(rng.randint(2, 8)):
            snap, modes = ex.snapshot(min(len(ex) - 1, i + k))
            if keep is None:
                keep = sorted(rng.sample(sorted(snap), min(len(snap), rng.randint(2, 10))))
            sub = {q: snap[q] for q in keep if q in snap}
            if not sub:
                sub = {"placeholder": b"%d" % k}
            when, msg = ex.meta(min(len(ex) - 1, i + k))
            w.commit("refs/heads/main", sub, when, msg, modes={q: modes[q] for q in sub if q in modes})
    elif kind == "gen_text_small":
        return fam_gen_text(w, rng, {"commits": rng.randint(3, 12), "scale": 0.02})
    elif kind == "gen_binary_small":
        return fam_gen_binary(w, rng, {"commits": rng.randint(3, 10), "scale": 0.02})
    elif kind == "diverge_small":
        return fam_real_diverge(w, excerpts[p["excerpt"]], rng,
                                {"base": p["start"] % max(1, len(excerpts[p["excerpt"]]) - 6), "mainline": 3,
                                 "branches": 2, "per_branch": 2, "subset": 6})
    else:
        raise ValueError("unknown edge kind " + kind)
    return {}


# --------------------------------------------------------- materialize

def _write_stream(spec, path, excerpts):
    rng = rng_for(GENERATOR_REVISION, spec["seed"])
    p = spec["params"]
    with open(path, "wb") as out:
        w = histbuild.FastImportWriter(out)
        g = spec["generator"]
        if g == "real_linear":
            fam_real_linear(w, excerpts[p["excerpt"]], rng, p)
        elif g == "real_diverge":
            fam_real_diverge(w, excerpts[p["excerpt"]], rng, p)
        elif g == "gen_text":
            fam_gen_text(w, rng, p)
        elif g == "gen_binary":
            fam_gen_binary(w, rng, p)
        elif g == "controls":
            fam_controls(w, rng, p)
        elif g == "edge":
            fam_edge(w, rng, p, excerpts)
        else:
            raise ValueError("unknown generator " + g)
        w.done()


def _refs(git, repo):
    out = histbuild.run_git(git, ["--git-dir=" + repo, "for-each-ref", "--format=%(objectname) %(refname)"])
    return [l.split(b" ", 1) for l in out.splitlines() if l]


def materialize(spec, git, out, excerpts, workdir):
    """Create the case directory `out` from `spec`."""
    fmt = spec["object_format"]
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    tmp = os.path.join(workdir, "mat-" + spec["id"])
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    stream = os.path.join(tmp, "stream")
    _write_stream(spec, stream, excerpts)
    staging = os.path.join(tmp, "staging.git")
    histbuild.init_bare(git, staging, fmt)
    histbuild.fast_import(git, staging, stream)
    repo = os.path.join(out, "repo.git")
    histbuild.init_bare(git, repo, fmt)
    for pk in sorted(os.listdir(os.path.join(staging, "objects", "pack"))):
        if pk.endswith(".pack"):
            with open(os.path.join(staging, "objects", "pack", pk), "rb") as f:
                histbuild.run_git(git, ["--git-dir=" + repo, "unpack-objects", "-q"], stdin=f.read())
    upd = b"".join(b"create %s %s\n" % (ref, oid) for oid, ref in _refs(git, staging))
    histbuild.run_git(git, ["--git-dir=" + repo, "update-ref", "--stdin"], stdin=upd)
    histbuild.run_git(git, ["--git-dir=" + repo, "symbolic-ref", "HEAD", "refs/heads/main"])
    layout = spec.get("layout")
    if layout:
        # Pre-pack older history with stock settings; newer objects stay loose.
        commits = histbuild.run_git(git, ["--git-dir=" + repo, "rev-list", "--reverse", "--first-parent",
                                          "refs/heads/main"]).split()
        done = []
        for frac, args in layout:
            cut = commits[max(0, int(len(commits) * frac) - 1)].decode()
            revs = cut + "\n" + "".join("^%s\n" % d for d in done)
            lst = histbuild.run_git(git, ["--git-dir=" + repo, "rev-list", "--objects", "--stdin"], stdin=revs.encode())
            histbuild.run_git(git, ["--git-dir=" + repo, "pack-objects", "-q"] + args +
                              [os.path.join(repo, "objects", "pack", "pack")], stdin=lst)
            done.append(cut)
        histbuild.run_git(git, ["--git-dir=" + repo, "prune-packed"])
        for f in os.listdir(os.path.join(repo, "objects", "pack")):
            if f.endswith(".rev") or f.endswith(".bitmap"):
                os.unlink(os.path.join(repo, "objects", "pack", f))
    histbuild.run_git(git, ["--git-dir=" + repo, "fsck", "--strict", "--no-dangling", "--no-progress"])
    lst = histbuild.run_git(git, ["--git-dir=" + repo, "rev-list", "--objects", "--all"])
    with open(os.path.join(out, "stdin.txt"), "wb") as f:
        f.write(lst)
    oids = [l.split(b" ", 1)[0] for l in lst.splitlines()]
    check = histbuild.run_git(git, ["--git-dir=" + repo, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                              stdin=b"\n".join(oids) + b"\n")
    allobj = histbuild.run_git(git, ["--git-dir=" + repo, "cat-file", "--batch-all-objects",
                                     "--batch-check=%(objectname)"]).split()
    if sorted(set(oids)) != sorted(allobj) or len(set(oids)) != len(oids):
        raise RuntimeError("case %s: reachable set differs from stored objects" % spec["id"])
    with open(os.path.join(out, "manifest.tsv"), "wb") as f:
        f.write(check)
    rows = [l.split() for l in check.splitlines()]
    info = {"spec": spec, "generator_revision": GENERATOR_REVISION, "objects": len(rows),
            "by_type": {}, "raw_bytes": 0, "max_object": 0}
    for oid, typ, size in rows:
        t = info["by_type"].setdefault(typ.decode(), {"count": 0, "bytes": 0})
        t["count"] += 1
        t["bytes"] += int(size)
        info["raw_bytes"] += int(size)
        info["max_object"] = max(info["max_object"], int(size))
    if spec["kind"] == "large":
        info["schedules"] = _schedules(git, repo, out, rows, spec)
    with open(os.path.join(out, "case.json"), "w") as f:
        json.dump(info, f, indent=1, sort_keys=True)
    shutil.rmtree(tmp)
    return info


def _schedules(git, repo, out, rows, spec):
    """Uniform-random, current-tip and ancestor-heavy read schedules."""
    rng = rng_for(GENERATOR_REVISION, spec["seed"], "schedules")
    os.makedirs(os.path.join(out, "schedules"))
    blobs = [r[0] for r in rows if r[1] == b"blob"]
    heads = histbuild.run_git(git, ["--git-dir=" + repo, "for-each-ref", "--format=%(objectname)", "refs/heads/"]).split()
    commits = histbuild.run_git(git, ["--git-dir=" + repo, "rev-list", "--reverse", "--all"]).split()
    # "tip": every tree and blob of each branch head plus the most recent 5% of
    # commits (recency-heavy reads such as checkout, log -p and blame).
    recent = list(heads) + commits[-max(1, len(commits) // 20):][::-1]
    tip = []
    seen = set()
    for h in recent:
        for l in histbuild.run_git(git, ["--git-dir=" + repo, "ls-tree", "-r", "-t", h.decode()]).splitlines():
            oid = l.split()[2]
            if oid not in seen:
                seen.add(oid)
                tip.append(oid)
    old = commits[:max(1, len(commits) // 4)]
    anc = []
    aseen = set()
    for c in old:
        for l in histbuild.run_git(git, ["--git-dir=" + repo, "ls-tree", "-r", c.decode()]).splitlines():
            oid = l.split()[2]
            if oid not in seen and oid not in aseen:
                aseen.add(oid)
                anc.append(oid)
    if not anc:
        anc = blobs[:]
    reps = spec.get("read_repeat", {"uniform": 2, "tip": 1, "ancestor": 1})
    uni = [rng.choice(blobs) for _ in range(int(len(blobs) * reps["uniform"]))]
    sched = {"uniform": uni, "tip": tip * reps["tip"], "ancestor": anc * reps["ancestor"]}
    counts = {}
    for k, v in sched.items():
        with open(os.path.join(out, "schedules", k + ".txt"), "wb") as f:
            f.write(b"".join(o + b"\n" for o in v))
        counts[k] = len(v)
    return counts


def load_excerpts(git, bundle_dir, names, workdir):
    return {n: Excerpt(git, os.path.join(bundle_dir, n + ".bundle"), workdir) for n in names}
