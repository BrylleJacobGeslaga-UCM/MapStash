#!/usr/bin/env python3
"""
MapStash core: everything that isn't the user interface.
osu!Collector API, mirror downloads, size estimates, map filters and collection.db.
Standard library only.

Unofficial: not affiliated with osu!Collector or ppy.
"""

import json
import random
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

APP_NAME = "MapStash"
APP_TAGLINE = "Free collection downloader for osu!"
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".mapstash.json")

API = "https://osucollector.com/api/collections/{cid}"
API_V2 = "https://osucollector.com/api/collections/{cid}/beatmapsv2?perPage=100{cursor}"

# Free beatmap mirrors, tried in order until one works.
MIRRORS = [
    ("catboy.best", "https://catboy.best/d/{sid}", "https://catboy.best/d/{sid}n"),
    ("nerinyan", "https://api.nerinyan.moe/d/{sid}", "https://api.nerinyan.moe/d/{sid}?nv=1"),
    ("osu.direct", "https://osu.direct/api/d/{sid}", "https://osu.direct/api/d/{sid}?noVideo=1"),
    ("sayobot", "https://dl.sayobot.cn/beatmaps/download/full/{sid}",
     "https://dl.sayobot.cn/beatmaps/download/novideo/{sid}"),
]
UA = "MapStash/2.0"



def http_get(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                  timeout=timeout)


def get_json(url, log):
    for attempt in range(4):
        try:
            with http_get(url, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if attempt == 3:
                raise
            log(f"Retrying osu!Collector ({e})...")
            time.sleep(2 * (attempt + 1))


# ---- browsing osu!Collector ---------------------------------------

BROWSE_PAGE = 30
POPULAR_RANGES = {"Today": "today", "This week": "week", "This month": "month",
                  "All time": "alltime"}


def browse_collections(kind, cursor=None, range_="week", query=""):
    """kind: 'popular' | 'recent' | 'search'.
    Returns (collections, has_more, next_cursor)."""
    from urllib.parse import urlencode
    params = {"perPage": BROWSE_PAGE}
    if cursor is not None:
        params["cursor"] = cursor
    if kind == "popular":
        path = "popularv2"
        params["range"] = range_
    elif kind == "recent":
        path = "recent"
    else:
        path = "search"
        params["search"] = query
    url = f"https://osucollector.com/api/collections/{path}?{urlencode(params)}"
    data = get_json(url, lambda _msg: None)
    if isinstance(data, list):  # some endpoints may return a bare list
        return data, False, None
    cols = data.get("collections") or data.get("results") or data.get("hits") or []
    return cols, bool(data.get("hasMore")), data.get("nextPageCursor")


def collection_date(col):
    """dateUploaded is a Firestore timestamp ({_seconds}) or an ISO string."""
    d = col.get("dateUploaded")
    try:
        if isinstance(d, dict):
            return time.strftime("%Y-%m-%d", time.localtime(d.get("_seconds") or d.get("seconds")))
        if isinstance(d, str):
            return d[:10]
    except Exception:
        pass
    return ""


def star_summary(col):
    """Short star-rating breakdown from difficultySpread, e.g. '5★ 40% · 6★ 35%'."""
    spread = col.get("difficultySpread") or {}
    try:
        items = sorted(((int(float(k)), int(v)) for k, v in spread.items() if int(v) > 0))
    except (ValueError, TypeError):
        return ""
    total = sum(v for _, v in items)
    if not total:
        return ""
    top = sorted(items, key=lambda kv: -kv[1])[:4]
    return " · ".join(f"{k}★ {v * 100 // total}%" for k, v in sorted(top))


def mode_summary(col):
    names = {"osu": "osu!", "taiko": "taiko", "fruits": "catch", "mania": "mania"}
    modes = col.get("modes") or {}
    parts = [f"{names.get(m, m)} {n:,}" for m, n in modes.items() if isinstance(n, int) and n]
    return " · ".join(parts)


# Categories are guessed from a collection's name + description.
CATEGORIES = {
    "Aim": [r"\baim", r"\bjumps?\b", r"\bspaced\b", r"cross[- ]?screen", r"\bflow\b",
            r"\bsnap"],
    "Stream": [r"\bstreams?\b", r"\bstreaming\b", r"\bbursts?\b", r"death ?streams?"],
    "Speed": [r"\bspeed\b", r"\bhigh bpm\b", r"\b2[0-9]0 ?bpm\b", r"\bdt\b", r"double ?time"],
    "Tech": [r"\btech", r"\bsliders?\b", r"\balt\b", r"\balternat", r"finger control",
             r"\brhythm\b"],
    "Reading": [r"\breading\b", r"\blow ar\b", r"\bhigh ar\b", r"\bar ?(?:1[01]|[0-8])\b",
                r"\bhd\b", r"\bhidden\b", r"\bfl\b", r"\bflashlight\b"],
    "Farm": [r"\bfarm", r"\bpp\b", r"\bconsistency\b", r"\bfc\b"],
    "Stamina": [r"\bstamina\b", r"\bmarathon", r"\blong maps?\b", r"\bendurance\b"],
    "Tourney": [r"\btourn", r"\bmappools?\b", r"\bpools?\b", r"\bowc\b", r"\b(?:nm|hd|hr|dt|fm|tb)\d\b"],
}
_CAT_RE = {k: re.compile("|".join(v), re.I) for k, v in CATEGORIES.items()}


def collection_categories(col):
    text = f"{col.get('name', '')} {col.get('description', '')}"
    return [k for k, rx in _CAT_RE.items() if rx.search(text)]


def avg_stars(col):
    """Weighted average star rating from difficultySpread ({"5": 120, ...}),
    treating bucket k as k.5 stars. None if unknown."""
    spread = col.get("difficultySpread") or {}
    total = weighted = 0
    for k, v in spread.items():
        try:
            k, v = float(k), int(v)
        except (TypeError, ValueError):
            continue
        total += v
        weighted += (k + 0.5) * v
    return round(weighted / total, 1) if total else None


def main_mode(col):
    modes = {m: n for m, n in (col.get("modes") or {}).items() if isinstance(n, int)}
    return max(modes, key=modes.get) if modes and max(modes.values()) > 0 else None


def parse_collection_id(text):
    m = re.search(r"collections/(\d+)", text or "")
    if m:
        return int(m.group(1))
    if text and text.strip().isdigit():
        return int(text.strip())
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


COVER_URL = "https://assets.ppy.sh/beatmaps/{sid}/covers/{kind}.jpg"  # list | card | cover
PREVIEW_URL = "https://b.ppy.sh/preview/{sid}.mp3"


def _parse_v2_map(b):
    """One beatmap from osu!Collector's paginated list -> our map dict (or None)."""
    bset = b.get("beatmapset") or {}
    sid = b.get("beatmapset_id") or bset.get("id")
    if sid is None or not b.get("checksum"):
        return None
    return {
        "sid": int(sid), "md5": b["checksum"],
        "stars": _num(b.get("difficulty_rating")),
        "bpm": _num(b.get("bpm") if b.get("bpm") is not None else bset.get("bpm")),
        "length": _num(b.get("hit_length") or b.get("total_length")),
        "mode": b.get("mode"), "version": b.get("version", ""),
        "artist": bset.get("artist", ""), "title": bset.get("title", ""),
        "creator": bset.get("creator", ""), "status": b.get("status") or bset.get("status", ""),
        "ar": _num(b.get("ar")), "cs": _num(b.get("cs")),
        "od": _num(b.get("accuracy")), "hp": _num(b.get("drain")),
    }


def fetch_map_page(cid, cursor=None, per_page=50):
    """One page of a collection's maps. Returns (maps, has_more, next_cursor)."""
    url = (f"https://osucollector.com/api/collections/{cid}/beatmapsv2?perPage={per_page}"
           + (f"&cursor={cursor}" if cursor is not None else ""))
    page = get_json(url, lambda _m: None)
    maps = [m for m in (_parse_v2_map(b) for b in page.get("beatmaps") or []) if m]
    has_more = bool(page.get("hasMore")) and page.get("nextPageCursor") is not None
    return maps, has_more, page.get("nextPageCursor")


def fmt_length(sec):
    if sec is None:
        return ""
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def fetch_collection(cid, log):
    """Returns (name, maps). Each map is a dict:
    {sid, md5, stars, bpm, length, mode, version}  (stars/bpm may be None)."""
    data = get_json(API.format(cid=cid), log)
    name = data.get("name") or f"osu!Collector {cid}"
    basic = []
    for bs in data.get("beatmapsets") or []:
        if bs.get("id") is None:
            continue
        for b in bs.get("beatmaps") or []:
            if b.get("checksum"):
                basic.append({"sid": int(bs["id"]), "md5": b["checksum"], "stars": None,
                              "bpm": None, "length": None, "mode": None, "version": "",
                              "status": "", "ar": None, "cs": None, "od": None, "hp": None})

    # Paginated list has star rating / BPM for every difficulty.
    detailed, cursor = [], ""
    try:
        while True:
            page = get_json(API_V2.format(cid=cid, cursor=cursor), log)
            for b in page.get("beatmaps") or []:
                m = _parse_v2_map(b)
                if m:
                    detailed.append(m)
            if not page.get("hasMore") or page.get("nextPageCursor") is None:
                break
            cursor = f"&cursor={page.get('nextPageCursor')}"
            log(f"Loading map details... {len(detailed):,} maps")
    except Exception as e:
        log(f"Couldn't load star ratings ({e}) - map filter won't be available.")
        detailed = []

    if not detailed:
        if not basic:
            raise RuntimeError("This collection has no maps (or osu!Collector didn't answer).")
        return name, basic
    seen = {m["md5"] for m in detailed}
    detailed += [m for m in basic if m["md5"] not in seen]  # keep anything v2 missed
    return name, detailed


# Map filter: {"stars": [lo, hi], "bpm": [lo, hi], "length": [lo, hi] (seconds),
#              "ar"/"cs"/"od"/"hp": [lo, hi], "modes": [...], "statuses": [...]}
# Leave a key out (or None / empty) to not filter on it.
RANGE_KEYS = ("stars", "bpm", "length", "ar", "cs", "od", "hp")
MODE_NAMES = {"osu": "osu!", "taiko": "taiko", "fruits": "catch", "mania": "mania"}


def clean_filter(f):
    """Validate a filter from the interface. Returns a tidy dict, or None if it filters nothing."""
    if not f:
        return None
    out = {}
    for k in RANGE_KEYS:
        r = f.get(k)
        if not r:
            continue
        lo, hi = _num(r[0]), _num(r[1])
        if lo is None and hi is None:
            continue
        lo = 0.0 if lo is None else lo
        hi = float("inf") if hi is None else hi
        out[k] = sorted((lo, hi))
    for k in ("modes", "statuses"):
        vals = [str(v).lower() for v in (f.get(k) or []) if v]
        if vals:
            out[k] = vals
    return out or None


def map_matches(m, f):
    if not f:
        return True
    for k in RANGE_KEYS:
        if k in f:
            v = m.get(k)
            if v is None or not f[k][0] <= v <= f[k][1]:
                return False
    if "modes" in f and (m.get("mode") or "") not in f["modes"]:
        return False
    if "statuses" in f and (m.get("status") or "").lower() not in f["statuses"]:
        return False
    return True


def group_sets(maps, f=None):
    """{set_id: [md5 of each matching difficulty]}"""
    sets = {}
    for m in maps:
        if map_matches(m, f):
            sets.setdefault(m["sid"], []).append(m["md5"])
    return sets


def filter_label(f):
    """Short text for a filter, used in the collection's name, e.g. "6-7★, ≤3:00, ranked"."""
    if not f:
        return ""
    def rng(k, fmt=lambda v: f"{v:g}"):
        lo, hi = f[k]
        if hi == float("inf"):
            return f"{fmt(lo)}+"
        if lo <= 0:
            return f"≤{fmt(hi)}"
        return f"{fmt(lo)}-{fmt(hi)}"
    parts = []
    if "stars" in f:
        parts.append(rng("stars") + "★")
    if "bpm" in f:
        parts.append(rng("bpm") + " BPM")
    if "length" in f:
        parts.append(rng("length", fmt_length))
    for k in ("ar", "cs", "od", "hp"):
        if k in f:
            parts.append(k.upper() + rng(k))
    if "modes" in f:
        parts.append("/".join(MODE_NAMES.get(x, x) for x in f["modes"]))
    if "statuses" in f:
        parts.append("/".join(f["statuses"]))
    return ", ".join(parts)


def filter_fields(maps):
    """Which filters this collection's data supports."""
    out = {k: any(m.get(k) is not None for m in maps) for k in RANGE_KEYS}
    out["modes"] = sorted({m["mode"] for m in maps if m.get("mode")})
    out["statuses"] = sorted({m["status"].lower() for m in maps if m.get("status")})
    return out


def existing_set_ids(folder):
    ids = set()
    if os.path.isdir(folder):
        for entry in os.listdir(folder):
            m = re.match(r"(\d+)", entry)
            if m:
                ids.add(int(m.group(1)))
    return ids


def download_set(sid, dest_dir, no_video, stop):
    final = os.path.join(dest_dir, f"{sid}.osz")
    tmp = final + ".part"
    errors = []
    for name, url, url_nv in MIRRORS:
        if stop.is_set():
            return False, "stopped"
        url = (url_nv if no_video else url).format(sid=sid)
        try:
            with http_get(url, timeout=120) as r:
                ctype = r.headers.get("Content-Type", "")
                if "json" in ctype or "html" in ctype:
                    raise ValueError("mirror returned an error page")
                with open(tmp, "wb") as f:
                    while True:
                        if stop.is_set():
                            raise InterruptedError("stopped")
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
            with open(tmp, "rb") as f:
                if f.read(2) != b"PK":  # .osz files are zip archives
                    raise ValueError("not a valid .osz")
            size = os.path.getsize(tmp)
            os.replace(tmp, final)
            return True, (name, size)
        except Exception as e:
            errors.append(f"{name}: {e}")
            if os.path.exists(tmp):
                os.remove(tmp)
            if isinstance(e, InterruptedError):
                return False, "stopped"
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                time.sleep(10)
    return False, "; ".join(errors)


# ---- storage size ---------------------------------------------------

# Used only if no mirror reports a size: typical .osz sizes.
FALLBACK_AVG = {True: 9 * 1024 ** 2, False: 16 * 1024 ** 2}  # no_video -> bytes


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def fmt_time(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


def free_space(path):
    """Free bytes on the drive that holds path (path may not exist yet)."""
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def probe_size(sid, no_video):
    """Ask mirrors how big a set is without downloading it. Returns bytes or None."""
    for _name, url, url_nv in MIRRORS:
        url = (url_nv if no_video else url).format(sid=sid)
        for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
            try:
                req = urllib.request.Request(url, method=method,
                                             headers={"User-Agent": UA, **headers})
                with urllib.request.urlopen(req, timeout=15) as r:
                    ctype = r.headers.get("Content-Type", "")
                    if "json" in ctype or "html" in ctype:
                        break  # error page from this mirror, try the next one
                    rng = r.headers.get("Content-Range", "")  # "bytes 0-0/12345"
                    if "/" in rng and rng.rsplit("/", 1)[1].isdigit():
                        return int(rng.rsplit("/", 1)[1])
                    length = r.headers.get("Content-Length")
                    if method == "HEAD" and length and int(length) > 1000:
                        return int(length)
            except Exception:
                continue
    return None


def estimate_avg_set(set_ids, no_video, log, sample=24):
    """Average .osz size from a random sample of sets.
    Returns (bytes_per_set, rough) where rough=True means no mirror answered."""
    if not set_ids:
        return FALLBACK_AVG[no_video], True
    picks = random.sample(list(set_ids), min(sample, len(set_ids)))
    log(f"Checking download size ({len(picks)} sample maps)...")
    sizes = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for size in pool.map(lambda sid: probe_size(sid, no_video), picks):
            if size:
                sizes.append(size)
    if not sizes:
        return FALLBACK_AVG[no_video], True
    return int(sum(sizes) / len(sizes)), False


# ---- collection.db --------------------------------------------------

def _read_uleb(f):
    result = shift = 0
    while True:
        b = f.read(1)[0]
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result
        shift += 7


def _read_str(f):
    return "" if f.read(1)[0] == 0 else f.read(_read_uleb(f)).decode("utf-8")


def _uleb(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _str(s):
    if not s:
        return b"\x00"
    d = s.encode("utf-8")
    return b"\x0b" + _uleb(len(d)) + d


def read_collection_db(path):
    if not os.path.exists(path):
        return 20240820, []
    with open(path, "rb") as f:
        version, count = struct.unpack("<ii", f.read(8))
        cols = []
        for _ in range(count):
            name = _read_str(f)
            (n,) = struct.unpack("<i", f.read(4))
            cols.append([name, [_read_str(f) for _ in range(n)]])
    return version, cols


def write_collection_db(path, version, cols):
    out = bytearray(struct.pack("<ii", version, len(cols)))
    for name, hashes in cols:
        out += _str(name) + struct.pack("<i", len(hashes))
        for h in hashes:
            out += _str(h)
    with open(path, "wb") as f:
        f.write(out)


def add_to_collection_db(folder, col_name, md5s, log):
    path = os.path.join(folder, "collection.db")
    version, cols = read_collection_db(path)
    if os.path.exists(path):
        backup = path + time.strftime(".backup-%Y%m%d-%H%M%S")
        shutil.copy2(path, backup)
        log(f"Backed up collection.db -> {os.path.basename(backup)}")
    for col in cols:
        if col[0] == col_name:
            seen = set(col[1])
            col[1] += [h for h in dict.fromkeys(md5s) if h not in seen]
            target = col
            break
    else:
        target = [col_name, list(dict.fromkeys(md5s))]
        cols.append(target)
    write_collection_db(path, version, cols)
    log(f'Collection "{col_name}" saved with {len(target[1])} maps.')


def make_lazer_collection(folder, col_name, md5s, log):
    """lazer imports collections from a 'stable install' folder; a folder counts as
    one if it holds an osu!.*.cfg file, so build a minimal one."""
    os.makedirs(os.path.join(folder, "Songs"), exist_ok=True)
    cfg = os.path.join(folder, "osu!.import.cfg")
    if not os.path.exists(cfg):
        open(cfg, "w").close()
    add_to_collection_db(folder, col_name, md5s, log)


def default_stable_dir():
    local = os.environ.get("LOCALAPPDATA")
    if local and os.path.isdir(os.path.join(local, "osu!")):
        return os.path.join(local, "osu!")
    return ""


def default_lazer_out():
    return os.path.join(os.path.expanduser("~"), "Downloads", "MapStash")


def stable_is_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq osu!.exe"], capture_output=True,
                             text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "osu!.exe" in out.stdout
    except Exception:
        return False


def plan_key(opts):
    """Settings that change what a check finds (re-check if any of these change)."""
    folder = opts["lazer_out"] if opts["target"] == "lazer" else opts["stable_dir"]
    return (opts["cid"], opts["target"], os.path.normcase(os.path.abspath(folder)),
            opts["no_video"])


def check_job(opts, emit, stop):
    """Fetch the collection, see what's already downloaded, estimate the size.
    Emits ("plan", plan)."""
    log = lambda msg: emit("log", msg)
    cid = opts["cid"]
    lazer = opts["target"] == "lazer"

    log(f"Fetching collection {cid}...")
    name, maps = fetch_collection(cid, log)
    if lazer:
        out_dir = os.path.join(opts["lazer_out"], re.sub(r'[\\/:*?"<>|]', "_", f"{cid} {name}"))
    else:
        out_dir = os.path.join(opts["stable_dir"], "Songs")

    all_sets = group_sets(maps)
    have = existing_set_ids(out_dir)
    missing = [sid for sid in all_sets if sid not in have]
    avg, rough = ((FALLBACK_AVG[opts["no_video"]], True) if stop.is_set()
                  else estimate_avg_set(missing, opts["no_video"], log))
    plan = {
        "key": plan_key(opts), "cid": cid, "name": name, "lazer": lazer,
        "out_dir": out_dir, "all_maps": maps, "have_ids": have,
        "fields": filter_fields(maps),
        "total_maps": len(maps), "total_sets": len(all_sets),
        "avg_set": avg, "est_rough": rough, "free": free_space(out_dir),
    }
    apply_filter(plan, None)
    log(f'"{name}": {len(maps)} maps in {len(all_sets)} sets, {len(plan["todo"])} to download, '
        f"~{fmt_bytes(plan['est_bytes'])}{' (rough guess)' if rough else ''}")
    emit("plan", plan)
    return plan


def apply_filter(plan, f):
    """Fill plan's sets/todo/size for the given map filter (None = everything)."""
    sets = group_sets(plan["all_maps"], f)
    todo = [sid for sid in sets if sid not in plan["have_ids"]]
    plan.update({
        "filter": f, "filter_label": filter_label(f), "sets": sets, "todo": todo,
        "have": len(sets) - len(todo), "maps": sum(len(v) for v in sets.values()),
        "est_bytes": plan["avg_set"] * len(todo),
    })
    return plan


def run_download(plan, opts, emit, stop):
    """Downloads everything in plan["todo"]. Emits progress/log/finished events."""
    log = lambda msg: emit("log", msg)
    out_dir, todo, cid = plan["out_dir"], plan["todo"], plan["cid"]
    os.makedirs(out_dir, exist_ok=True)

    failed, done, ok_count, bytes_done = [], 0, 0, 0
    started = time.time()

    def progress():
        # Refine the size estimate with real sizes once a few maps are in.
        if ok_count >= 5:
            est_total = bytes_done + bytes_done / ok_count * (len(todo) - done)
        else:
            est_total = max(plan["est_bytes"], bytes_done)
        elapsed = max(time.time() - started, 0.001)
        emit("progress", {"done": done, "total": len(todo), "bytes": bytes_done,
                          "est_total": est_total, "speed": bytes_done / elapsed,
                          "free": free_space(out_dir)})

    progress()
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, opts["workers"])) as pool:
            futures = {pool.submit(download_set, s, out_dir, opts["no_video"], stop): s
                       for s in todo}
            for fut in as_completed(futures):
                sid = futures[fut]
                try:
                    ok, info = fut.result()
                except Exception as e:  # never let one map kill the job
                    ok, info = False, str(e)
                if info == "stopped":
                    continue
                done += 1
                if ok:
                    mirror, size = info
                    ok_count += 1
                    bytes_done += size
                    log(f"✓ {sid}  {fmt_bytes(size)}  ({mirror})")
                else:
                    failed.append(sid)
                    log(f"✗ {sid}  {info}")
                    if "No space left" in str(info) or "Errno 28" in str(info):
                        log("✗ The drive is full - stopping.")
                        stop.set()
                progress()
            if stop.is_set():
                for f in futures:
                    f.cancel()

    if failed:
        with open(os.path.join(out_dir, f"failed_{cid}.txt"), "w") as f:
            f.write("\n".join(f"https://osu.ppy.sh/beatmapsets/{s}" for s in failed))
        log(f"{len(failed)} failed - press Download again to retry them.")

    result = {"stopped": stop.is_set(), "out_dir": out_dir, "lazer": plan["lazer"],
              "col_dir": None, "ok": ok_count, "failed": len(failed), "bytes": bytes_done}
    if stop.is_set():
        log("Stopped. Press Download again to continue where you left off.")
        emit("finished", result)
        return

    md5s = [h for hashes in plan["sets"].values() for h in hashes]
    col_name = opts.get("col_name") or plan["name"]
    if plan.get("filter_label"):
        col_name += f" [{plan['filter_label']}]"
    if opts["make_collection"]:
        if plan["lazer"]:
            result["col_dir"] = os.path.join(out_dir, "lazer-collection")
            make_lazer_collection(result["col_dir"], col_name, md5s, log)
        elif stable_is_running():
            log("osu! is running - close it, then press Download again to add the collection.")
        else:
            add_to_collection_db(opts["stable_dir"], col_name, md5s, log)
    emit("finished", result)
