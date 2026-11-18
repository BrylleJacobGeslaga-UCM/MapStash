#!/usr/bin/env python3
"""
MapStash - free collection downloader for osu!
Downloads osu!Collector collections from free beatmap mirrors.
Works with osu!lazer (default) and osu!stable. Python 3.8+, standard library only.

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
UA = "MapStash/1.1"


# ======================================================================
#  Core logic (no GUI code here)
# ======================================================================

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


def parse_collection_id(text):
    m = re.search(r"collections/(\d+)", text or "")
    if m:
        return int(m.group(1))
    if text and text.strip().isdigit():
        return int(text.strip())
    return None


def fetch_collection(cid, log):
    """Returns (name, {set_id: [md5, ...]})."""
    data = get_json(API.format(cid=cid), log)
    name = data.get("name") or f"osu!Collector {cid}"
    sets = {}
    for bs in data.get("beatmapsets") or []:
        if bs.get("id") is None:
            continue
        md5s = [b.get("checksum") for b in bs.get("beatmaps") or [] if b.get("checksum")]
        sets.setdefault(int(bs["id"]), []).extend(md5s)

    if not sets or not any(sets.values()):  # fallback: paginated endpoint
        sets, cursor = {}, ""
        while True:
            page = get_json(API_V2.format(cid=cid, cursor=cursor), log)
            for b in page.get("beatmaps") or []:
                sid = b.get("beatmapset_id") or (b.get("beatmapset") or {}).get("id")
                if sid is None:
                    continue
                lst = sets.setdefault(int(sid), [])
                if b.get("checksum"):
                    lst.append(b["checksum"])
            if not page.get("hasMore"):
                break
            cursor = f"&cursor={page.get('nextPageCursor')}"
            log(f"Loading map list... {sum(len(v) for v in sets.values())} maps")
    return name, sets


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


def estimate_size(set_ids, no_video, log, sample=24):
    """Estimate the total download size from a random sample of sets.
    Returns (bytes, rough) where rough=True means no mirror answered."""
    if not set_ids:
        return 0, False
    picks = random.sample(list(set_ids), min(sample, len(set_ids)))
    log(f"Checking download size ({len(picks)} sample maps)...")
    sizes = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for size in pool.map(lambda sid: probe_size(sid, no_video), picks):
            if size:
                sizes.append(size)
    if not sizes:
        return FALLBACK_AVG[no_video] * len(set_ids), True
    return int(sum(sizes) / len(sizes) * len(set_ids)), False


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
    name, sets = fetch_collection(cid, log)
    if lazer:
        out_dir = os.path.join(opts["lazer_out"], re.sub(r'[\\/:*?"<>|]', "_", f"{cid} {name}"))
    else:
        out_dir = os.path.join(opts["stable_dir"], "Songs")

    have = existing_set_ids(out_dir)
    todo = [sid for sid in sets if sid not in have]
    est, rough = (0, False) if stop.is_set() else estimate_size(todo, opts["no_video"], log)
    plan = {
        "key": plan_key(opts), "cid": cid, "name": name, "sets": sets, "lazer": lazer,
        "out_dir": out_dir, "todo": todo, "have": len(sets) - len(todo),
        "maps": sum(len(v) for v in sets.values()),
        "est_bytes": est, "est_rough": rough, "free": free_space(out_dir),
    }
    log(f'"{name}": {plan["maps"]} maps in {len(sets)} sets, {len(todo)} to download, '
        f"~{fmt_bytes(est)}{' (rough guess)' if rough else ''}")
    emit("plan", plan)
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
    if opts["make_collection"]:
        if plan["lazer"]:
            result["col_dir"] = os.path.join(out_dir, "lazer-collection")
            make_lazer_collection(result["col_dir"], col_name, md5s, log)
        elif stable_is_running():
            log("osu! is running - close it, then press Download again to add the collection.")
        else:
            add_to_collection_db(opts["stable_dir"], col_name, md5s, log)
    emit("finished", result)


# ======================================================================
#  GUI
# ======================================================================

def main_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    BG, PANEL, FG, MUTED, PINK = "#1c1719", "#2a2326", "#f2eef0", "#a89ca1", "#ff66aa"
    GOOD, BAD = "#7ee2a8", "#ff7a7a"

    def open_folder(path):
        if not path or not os.path.isdir(path):
            return
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def load_settings():
        try:
            with open(SETTINGS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    class App:
        def __init__(self, root):
            self.root = root
            self.q = queue.Queue()
            self.stop = threading.Event()
            self.worker = None
            self.plan = None              # result of the last check
            self.download_after_check = False
            self.last_result = None
            s = load_settings()

            root.title(f"{APP_NAME} - {APP_TAGLINE}")
            root.geometry("740x700")
            root.minsize(620, 600)
            root.configure(bg=BG)

            st = ttk.Style(root)
            st.theme_use("clam")
            st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                         bordercolor=PANEL, font=("Segoe UI", 10))
            st.configure("TFrame", background=BG)
            st.configure("Card.TFrame", background=PANEL)
            st.configure("Card.TLabel", background=PANEL)
            st.configure("Muted.TLabel", foreground=MUTED, background=PANEL)
            st.configure("Value.TLabel", background=PANEL, font=("Segoe UI", 10, "bold"))
            st.configure("Good.TLabel", background=PANEL, foreground=GOOD,
                         font=("Segoe UI", 10, "bold"))
            st.configure("Bad.TLabel", background=PANEL, foreground=BAD,
                         font=("Segoe UI", 10, "bold"))
            st.configure("Name.TLabel", background=PANEL, foreground=PINK,
                         font=("Segoe UI", 12, "bold"))
            st.configure("Title.TLabel", font=("Segoe UI", 20, "bold"), foreground=PINK)
            st.configure("Sub.TLabel", foreground=MUTED)
            st.configure("Card.TRadiobutton", background=PANEL)
            st.configure("Card.TCheckbutton", background=PANEL)
            st.map("Card.TRadiobutton", background=[("active", PANEL)])
            st.map("Card.TCheckbutton", background=[("active", PANEL)])
            st.configure("TEntry", foreground=FG, insertcolor=FG, padding=6)
            st.configure("TSpinbox", foreground=FG, arrowcolor=FG, padding=4)
            st.configure("TButton", background="#3a3135", padding=(12, 6))
            st.map("TButton", background=[("active", "#4a3f44"), ("disabled", BG)],
                   foreground=[("disabled", MUTED)])
            st.configure("Accent.TButton", background=PINK, foreground="#ffffff",
                         font=("Segoe UI", 10, "bold"))
            st.map("Accent.TButton", background=[("active", "#ff85bb"), ("disabled", "#5a3a48")])
            st.configure("Pink.Horizontal.TProgressbar", troughcolor=PANEL, background=PINK,
                         bordercolor=PANEL, lightcolor=PINK, darkcolor=PINK)

            main = ttk.Frame(root, padding=16)
            main.pack(fill="both", expand=True)

            ttk.Label(main, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
            ttk.Label(main, text=APP_TAGLINE + " · unofficial, uses free mirrors",
                      style="Sub.TLabel").pack(anchor="w", pady=(0, 12))

            # --- collection link
            card = ttk.Frame(main, style="Card.TFrame", padding=12)
            card.pack(fill="x")
            ttk.Label(card, text="osu!Collector link or collection ID", style="Card.TLabel").grid(
                row=0, column=0, sticky="w")
            self.url = tk.StringVar()
            e = ttk.Entry(card, textvariable=self.url)
            e.grid(row=1, column=0, sticky="ew", pady=(4, 0))
            e.bind("<Return>", lambda _: self.check())
            ttk.Button(card, text="Paste", command=self.paste).grid(
                row=1, column=1, padx=(8, 0), pady=(4, 0))
            self.check_btn = ttk.Button(card, text="Check", command=self.check)
            self.check_btn.grid(row=1, column=2, padx=(8, 0), pady=(4, 0))
            card.columnconfigure(0, weight=1)

            # --- info: what will be downloaded + storage
            info = ttk.Frame(main, style="Card.TFrame", padding=12)
            info.pack(fill="x", pady=(10, 0))
            self.name_lbl = ttk.Label(info, text="Paste a link and press Check",
                                      style="Name.TLabel")
            self.name_lbl.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
            self.fields = {}
            rows = [("maps", "Maps"), ("have", "Already downloaded"),
                    ("size", "Download size"), ("free", "Free space")]
            for i, (key, text) in enumerate(rows):
                r, c = divmod(i, 2)
                ttk.Label(info, text=text, style="Muted.TLabel").grid(
                    row=1 + r * 2, column=c * 2, sticky="w", padx=(0, 24))
                lbl = ttk.Label(info, text="-", style="Value.TLabel")
                lbl.grid(row=2 + r * 2, column=c * 2, sticky="w", padx=(0, 24), pady=(0, 6))
                self.fields[key] = lbl
            self.space_note = ttk.Label(info, text="", style="Muted.TLabel")
            self.space_note.grid(row=5, column=0, columnspan=4, sticky="w")
            info.columnconfigure(0, weight=1)
            info.columnconfigure(2, weight=1)

            # --- settings
            sc = ttk.Frame(main, style="Card.TFrame", padding=12)
            sc.pack(fill="x", pady=(10, 0))
            self.target = tk.StringVar(value=s.get("target", "lazer"))
            ttk.Label(sc, text="Game:", style="Card.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Radiobutton(sc, text="osu!lazer", value="lazer", variable=self.target,
                            style="Card.TRadiobutton", command=self.update_target).grid(
                row=0, column=1, sticky="w")
            ttk.Radiobutton(sc, text="osu!stable", value="stable", variable=self.target,
                            style="Card.TRadiobutton", command=self.update_target).grid(
                row=0, column=2, sticky="w")

            self.folder_label = ttk.Label(sc, text="", style="Card.TLabel")
            self.folder_label.grid(row=1, column=0, sticky="w", pady=(8, 0), padx=(0, 8))
            self.lazer_out = tk.StringVar(value=s.get("lazer_out", default_lazer_out()))
            self.stable_dir = tk.StringVar(value=s.get("stable_dir", default_stable_dir()))
            self.folder_entry = ttk.Entry(sc)
            self.folder_entry.grid(row=1, column=1, columnspan=3, sticky="ew",
                                   pady=(8, 0), padx=(0, 8))
            ttk.Button(sc, text="Browse...", command=self.browse).grid(row=1, column=4, pady=(8, 0))

            opts = ttk.Frame(sc, style="Card.TFrame")
            opts.grid(row=2, column=0, columnspan=5, sticky="w", pady=(10, 0))
            self.no_video = tk.BooleanVar(value=s.get("no_video", True))
            ttk.Checkbutton(opts, text="Skip videos (smaller)", variable=self.no_video,
                            style="Card.TCheckbutton", command=self.options_changed).pack(side="left")
            self.make_col = tk.BooleanVar(value=s.get("make_collection", True))
            ttk.Checkbutton(opts, text="Create collection", variable=self.make_col,
                            style="Card.TCheckbutton").pack(side="left", padx=(16, 0))
            ttk.Label(opts, text="Parallel downloads:", style="Card.TLabel").pack(
                side="left", padx=(16, 4))
            self.workers = tk.IntVar(value=s.get("workers", 3))
            ttk.Spinbox(opts, from_=1, to=8, width=4, textvariable=self.workers).pack(side="left")
            for col in (1, 2, 3):
                sc.columnconfigure(col, weight=1)

            # --- buttons + progress
            bar = ttk.Frame(main)
            bar.pack(fill="x", pady=(12, 0))
            self.start_btn = ttk.Button(bar, text="Download", style="Accent.TButton",
                                        command=self.start)
            self.start_btn.pack(side="left")
            self.stop_btn = ttk.Button(bar, text="Stop", command=self.request_stop,
                                       state="disabled")
            self.stop_btn.pack(side="left", padx=(8, 0))
            self.open_btn = ttk.Button(bar, text="Open folder", state="disabled",
                                       command=lambda: open_folder(self.current_out_dir()))
            self.open_btn.pack(side="right")
            self.col_btn = ttk.Button(bar, text="Collection folder", state="disabled",
                                      command=lambda: open_folder(
                                          (self.last_result or {}).get("col_dir")))
            self.col_btn.pack(side="right", padx=(0, 8))

            self.progress = ttk.Progressbar(main, style="Pink.Horizontal.TProgressbar",
                                            mode="determinate")
            self.progress.pack(fill="x", pady=(12, 2))
            self.status = ttk.Label(main, text="Ready.", style="Sub.TLabel")
            self.status.pack(anchor="w")

            # --- log
            logf = ttk.Frame(main, style="Card.TFrame", padding=2)
            logf.pack(fill="both", expand=True, pady=(8, 0))
            self.log = tk.Text(logf, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                               font=("Consolas", 9), wrap="word", state="disabled",
                               highlightthickness=0, height=8)
            sb = ttk.Scrollbar(logf, command=self.log.yview)
            self.log.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self.log.pack(side="left", fill="both", expand=True)
            self.log.tag_configure("ok", foreground=GOOD)
            self.log.tag_configure("bad", foreground=BAD)
            self.log.tag_configure("pink", foreground=PINK)

            self.update_target()
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            root.after(100, self.poll)

        # ---- helpers
        def current_folder_var(self):
            return self.lazer_out if self.target.get() == "lazer" else self.stable_dir

        def current_out_dir(self):
            if self.last_result:
                return self.last_result.get("out_dir")
            return self.plan["out_dir"] if self.plan else None

        def update_target(self):
            lazer = self.target.get() == "lazer"
            self.folder_label.configure(text="Save to:" if lazer else "osu! folder:")
            self.folder_entry.configure(textvariable=self.current_folder_var())
            self.options_changed()

        def options_changed(self):
            # The check result depends on these settings, so it's out of date now.
            if self.plan and not self.busy():
                self.plan = None
                self.fields["have"].configure(text="-")
                self.fields["size"].configure(text="-")
                self.fields["free"].configure(text="-", style="Value.TLabel")
                self.space_note.configure(text="Settings changed - press Check again.")

        def browse(self):
            d = filedialog.askdirectory(initialdir=self.current_folder_var().get() or None)
            if d:
                self.current_folder_var().set(os.path.normpath(d))
                self.options_changed()

        def paste(self):
            try:
                self.url.set(self.root.clipboard_get().strip())
            except tk.TclError:
                return
            if parse_collection_id(self.url.get()):
                self.check()

        def write_log(self, msg, tag=None):
            if tag is None:
                tag = "ok" if msg.startswith("✓") else "bad" if msg.startswith("✗") else None
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n", tag)
            if int(self.log.index("end-1c").split(".")[0]) > 3000:  # keep log small
                self.log.delete("1.0", "500.0")
            self.log.see("end")
            self.log.configure(state="disabled")

        def save_settings(self):
            try:
                with open(SETTINGS_FILE, "w") as f:
                    json.dump({"target": self.target.get(), "lazer_out": self.lazer_out.get(),
                               "stable_dir": self.stable_dir.get(),
                               "no_video": self.no_video.get(),
                               "make_collection": self.make_col.get(),
                               "workers": self.workers.get()}, f)
            except Exception:
                pass

        def busy(self):
            return bool(self.worker and self.worker.is_alive())

        def set_running(self, running):
            self.start_btn.configure(state="disabled" if running else "normal")
            self.check_btn.configure(state="disabled" if running else "normal")
            self.stop_btn.configure(state="normal" if running else "disabled")

        def gather_opts(self):
            cid = parse_collection_id(self.url.get())
            if not cid:
                messagebox.showwarning(APP_NAME, "Paste an osu!Collector collection link, like\n"
                                       "https://osucollector.com/collections/24173/...")
                return None
            target = self.target.get()
            if target == "stable" and not os.path.isdir(self.stable_dir.get()):
                messagebox.showwarning(APP_NAME, "Choose your osu!stable folder first.")
                return None
            if target == "lazer" and not self.lazer_out.get().strip():
                messagebox.showwarning(APP_NAME, "Choose where to save the maps.")
                return None
            try:
                workers = max(1, min(8, int(self.workers.get())))
            except (tk.TclError, ValueError):
                workers = 3
            self.save_settings()
            return {"cid": cid, "target": target, "lazer_out": self.lazer_out.get(),
                    "stable_dir": self.stable_dir.get(), "no_video": self.no_video.get(),
                    "make_collection": self.make_col.get(), "workers": workers,
                    "col_name": None}

        def run_in_thread(self, fn, *args):
            self.stop.clear()
            self.set_running(True)

            def work():
                try:
                    fn(*args, lambda *a: self.q.put(a), self.stop)
                except Exception as e:
                    self.q.put(("error", str(e)))
            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

        # ---- actions
        def check(self, then_download=False):
            if self.busy():
                return
            opts = self.gather_opts()
            if not opts:
                return
            self.download_after_check = then_download
            self.last_result = None
            self.name_lbl.configure(text="Checking...")
            for lbl in self.fields.values():
                lbl.configure(text="...", style="Value.TLabel")
            self.space_note.configure(text="")
            self.status.configure(text="Checking collection and download size...")
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
            self.run_in_thread(check_job, opts)

        def start(self):
            if self.busy():
                return
            opts = self.gather_opts()
            if not opts:
                return
            if not self.plan or self.plan["key"] != plan_key(opts):
                self.check(then_download=True)  # check first, then continue
                return
            self.begin_download(opts)

        def begin_download(self, opts):
            plan = self.plan
            free = free_space(plan["out_dir"])
            if free is not None and plan["est_bytes"] > free:
                if not messagebox.askyesno(
                        APP_NAME,
                        f"This needs about {fmt_bytes(plan['est_bytes'])}, but the drive only "
                        f"has {fmt_bytes(free)} free.\n\nDownload as much as fits anyway?\n"
                        "(Tip: tick 'Skip videos' or choose another drive.)"):
                    return
            # Re-scan in case files were added since the check
            have = existing_set_ids(plan["out_dir"])
            plan["todo"] = [sid for sid in plan["todo"] if sid not in have]
            self.progress.configure(mode="determinate", value=0, maximum=1)
            self.open_btn.configure(state="normal")
            self.col_btn.configure(state="disabled")
            self.run_in_thread(run_download, plan, opts)

        def request_stop(self):
            self.stop.set()
            self.download_after_check = False
            self.stop_btn.configure(state="disabled")
            self.status.configure(text="Stopping after current downloads...")

        def on_close(self):
            if self.busy():
                if not messagebox.askyesno(APP_NAME, "A download is running. Quit anyway?\n"
                                           "(You can continue it later.)"):
                    return
                self.stop.set()
            self.save_settings()
            self.root.destroy()

        # ---- show results
        def show_plan(self, plan):
            self.plan = plan
            self.name_lbl.configure(text=plan["name"])
            self.fields["maps"].configure(text=f"{plan['maps']:,} maps · "
                                               f"{len(plan['sets']):,} sets")
            self.fields["have"].configure(text=f"{plan['have']:,} sets "
                                               f"({len(plan['todo']):,} to go)")
            size = fmt_bytes(plan["est_bytes"])
            self.fields["size"].configure(
                text="Nothing to download" if not plan["todo"] else
                f"~{size}" + (" (rough guess)" if plan["est_rough"] else ""))
            self.show_free(plan["free"], plan["est_bytes"])

        def show_free(self, free, needed):
            drive = os.path.splitdrive(os.path.abspath(
                (self.plan or {}).get("out_dir", ".")))[0]
            if free is None:
                self.fields["free"].configure(text="unknown", style="Value.TLabel")
                return
            enough = free >= needed
            self.fields["free"].configure(
                text=f"{fmt_bytes(free)}" + (f" on {drive}" if drive else ""),
                style="Good.TLabel" if enough else "Bad.TLabel")
            if needed and enough:
                self.space_note.configure(
                    text=f"✓ Enough space - about {fmt_bytes(free - needed)} left afterwards.")
            elif needed:
                self.space_note.configure(
                    text=f"⚠ Not enough space - you need about {fmt_bytes(needed - free)} more. "
                         "Try 'Skip videos' or another drive.")
            else:
                self.space_note.configure(text="")

        # ---- events from the worker thread
        def poll(self):
            try:
                while True:
                    kind, *data = self.q.get_nowait()
                    if kind == "log":
                        self.write_log(data[0])
                    elif kind == "plan":
                        self.on_plan(data[0])
                    elif kind == "progress":
                        self.on_progress(data[0])
                    elif kind == "error":
                        self.on_error(data[0])
                    elif kind == "finished":
                        self.finished(data[0])
            except queue.Empty:
                pass
            self.root.after(100, self.poll)

        def on_plan(self, plan):
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.show_plan(plan)
            self.open_btn.configure(state="normal" if os.path.isdir(plan["out_dir"])
                                    else "disabled")
            self.status.configure(text="Ready to download." if plan["todo"]
                                  else "You already have every map in this collection.")
            if self.download_after_check and not self.stop.is_set():
                self.download_after_check = False
                self.worker.join(timeout=2)
                opts = self.gather_opts()
                if opts:
                    self.begin_download(opts)
                    if not self.busy():
                        self.set_running(False)
                return
            self.set_running(False)

        def on_progress(self, p):
            self.progress.configure(maximum=max(p["total"], 1), value=p["done"])
            if not p["total"]:
                self.status.configure(text="Nothing to download.")
                return
            text = (f"{p['done']:,} / {p['total']:,} maps  ·  {fmt_bytes(p['bytes'])} of "
                    f"~{fmt_bytes(p['est_total'])}")
            if p["speed"] > 0 and p["done"]:
                remaining = max(p["est_total"] - p["bytes"], 0)
                text += (f"  ·  {fmt_bytes(p['speed'])}/s  ·  "
                         f"{fmt_time(remaining / p['speed'])} left")
            self.status.configure(text=text)
            self.fields["size"].configure(text=f"~{fmt_bytes(p['est_total'])}")
            if p["free"] is not None:
                self.show_free(p["free"], max(p["est_total"] - p["bytes"], 0))

        def on_error(self, msg):
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.set_running(False)
            self.download_after_check = False
            if self.name_lbl.cget("text") == "Checking...":
                self.name_lbl.configure(text="Couldn't load that collection")
                for lbl in self.fields.values():
                    lbl.configure(text="-")
            self.status.configure(text="Error.")
            self.write_log("✗ " + msg)
            messagebox.showerror(APP_NAME, f"Something went wrong:\n{msg}")

        def finished(self, r):
            self.last_result = r
            self.set_running(False)
            self.open_btn.configure(state="normal")
            if r.get("col_dir"):
                self.col_btn.configure(state="normal")
            if r["stopped"]:
                self.status.configure(text=f"Stopped - {fmt_bytes(r['bytes'])} downloaded.")
                self.plan = None  # re-check next time so the numbers are fresh
                return
            self.status.configure(text=f"Done - {r['ok']:,} downloaded ({fmt_bytes(r['bytes'])}), "
                                       f"{r['failed']:,} failed.")
            self.plan = None
            if r["lazer"]:
                steps = ("Done! To finish in osu!lazer:\n"
                         "1. Click 'Open folder', select all .osz files (Ctrl+A) and drag them "
                         "onto the lazer window.\n")
                if r.get("col_dir"):
                    steps += ("2. When the import finishes: Settings -> Maintenance -> import "
                              "from stable, choose this folder and tick only Collections:\n"
                              f"   {r['col_dir']}")
                self.write_log("")
                self.write_log(steps, "pink")
                messagebox.showinfo(APP_NAME, steps)
            else:
                self.write_log("Done! Open osu! - it imports the new maps automatically.")

    root = tk.Tk()
    if os.name == "nt":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main_gui()
