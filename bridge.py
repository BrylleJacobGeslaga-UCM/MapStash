"""
The bridge between the web interface (ui/) and MapStash's Python core.

Every public method of Api can be called from JavaScript as
    await window.pywebview.api.method_name(...)
Long jobs (check, download) run in background threads and report back through
an event queue that the page reads with poll().
"""

import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import core

DEFAULT_SETTINGS = {
    "target": "lazer",
    "lazer_out": core.default_lazer_out(),
    "stable_dir": core.default_stable_dir(),
    "no_video": True,
    "make_collection": True,
    "workers": 3,
    # interface
    "petals": True,
    "typewriter": True,
    "text_speed": 2,  # 1 slow, 2 normal, 3 fast, 4 instant
    "preview_volume": 60,
    # background music
    "bgm": True,
    "bgm_source": "piano",  # piano (built in) | folder (your own files)
    "bgm_folder": "",
    "bgm_volume": 35,
}

AUDIO_EXT = (".mp3", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".flac", ".webm")


def _open_path(path):
    if not path or not os.path.isdir(path):
        return False
    if os.name == "nt":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
    return True


class Api:
    def __init__(self):
        self._window = None
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._worker = None
        self._plan = None
        self._last_result = None
        self._cover_cache = {}  # collection id -> a beatmap set id (for cover art)
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._music_server = None
        self._music_dir = None
        self._music_token = secrets.token_hex(8)

    def _set_window(self, window):
        self._window = window

    # ------------------------------------------------------------ events --
    def _emit(self, kind, *data):
        """Called from worker threads by core.check_job / core.run_download."""
        if kind == "plan":
            self._plan = data[0]
            self._events.put({"type": "plan", "plan": self._summary(self._plan)})
        elif kind == "progress":
            self._events.put({"type": "progress", **data[0]})
        elif kind == "log":
            self._events.put({"type": "log", "text": data[0]})
        elif kind == "finished":
            self._last_result = data[0]
            self._events.put({"type": "finished", "result": data[0]})

    def poll(self):
        """The page calls this a few times per second to receive events."""
        out = []
        try:
            while len(out) < 200:
                out.append(self._events.get_nowait())
        except queue.Empty:
            pass
        return out

    def is_busy(self):
        return bool(self._worker and self._worker.is_alive())

    def _run(self, fn, *args):
        self._stop.clear()

        def work():
            try:
                fn(*args, self._emit, self._stop)
            except Exception as e:
                self._events.put({"type": "error", "text": str(e)})
        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    # ---------------------------------------------------------- settings --
    def get_settings(self):
        s = dict(DEFAULT_SETTINGS)
        try:
            with open(core.SETTINGS_FILE) as f:
                saved = json.load(f)
            s.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        except Exception:
            pass
        return s

    def save_settings(self, settings):
        s = self.get_settings()
        s.update({k: v for k, v in (settings or {}).items() if k in DEFAULT_SETTINGS})
        try:
            s["workers"] = max(1, min(8, int(s["workers"])))
        except (TypeError, ValueError):
            s["workers"] = 3
        try:
            with open(core.SETTINGS_FILE, "w") as f:
                json.dump(s, f, indent=1)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "settings": s}

    def app_info(self):
        return {"name": core.APP_NAME, "tagline": core.APP_TAGLINE, "version": "2.0.0",
                "categories": list(core.CATEGORIES), "ranges": core.POPULAR_RANGES}

    # ------------------------------------------------------------ browse --
    def browse(self, kind="popular", cursor=None, range_="week", query=""):
        try:
            cols, has_more, nxt = core.browse_collections(kind, cursor, range_, query or "")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        out = []
        for c in cols:
            if c.get("id") is None:
                continue
            sets = c.get("beatmapsets") or []
            if sets and sets[0].get("id"):
                self._cover_cache.setdefault(c["id"], sets[0]["id"])
            out.append({
                "id": c["id"], "name": c.get("name") or f"Collection {c['id']}",
                "uploader": (c.get("uploader") or {}).get("username", ""),
                "maps": c.get("beatmapCount", 0), "favs": c.get("favourites", 0),
                "date": core.collection_date(c),
                "description": (c.get("description") or "").strip()[:600],
                "cats": core.collection_categories(c), "stars": core.avg_stars(c),
                "mode": core.main_mode(c), "modes": core.mode_summary(c),
                "spread": core.star_summary(c),
                "cover_sid": self._cover_cache.get(c["id"]),
            })
        return {"ok": True, "collections": out, "has_more": bool(has_more and nxt is not None),
                "cursor": nxt}

    def cover_sids(self, ids):
        """Find a beatmap set for each collection (for cover art). Returns {id: sid}."""
        todo = [i for i in ids if i not in self._cover_cache]

        def find(cid):
            try:
                maps, _, _ = core.fetch_map_page(cid, per_page=10)
                return cid, (maps[0]["sid"] if maps else None)
            except Exception:
                return cid, None
        for cid, sid in self._pool.map(find, todo):
            self._cover_cache[cid] = sid
        return {str(i): self._cover_cache.get(i) for i in ids}

    def map_page(self, cid, cursor=None):
        try:
            maps, has_more, nxt = core.fetch_map_page(int(cid), cursor)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if maps:
            self._cover_cache.setdefault(int(cid), maps[0]["sid"])
        for m in maps:
            m["length_text"] = core.fmt_length(m.get("length"))
        return {"ok": True, "maps": maps, "has_more": has_more, "cursor": nxt}

    # ---------------------------------------------------------- download --
    def _opts(self, cid, s):
        return {"cid": cid, "target": s["target"], "lazer_out": s["lazer_out"],
                "stable_dir": s["stable_dir"], "no_video": bool(s["no_video"]),
                "make_collection": bool(s["make_collection"]),
                "workers": max(1, min(8, int(s["workers"]))), "col_name": None}

    def _validate(self, link):
        cid = core.parse_collection_id(str(link or ""))
        if not cid:
            return None, "Paste an osu!Collector link, like https://osucollector.com/collections/24173/..."
        s = self.get_settings()
        if s["target"] == "stable" and not os.path.isdir(s["stable_dir"]):
            return None, "Choose your osu!stable folder in Config first."
        if s["target"] == "lazer" and not str(s["lazer_out"]).strip():
            return None, "Choose where to save maps in Config first."
        return self._opts(cid, s), None

    def check(self, link):
        if self.is_busy():
            return {"ok": False, "error": "Already working on something."}
        opts, err = self._validate(link)
        if err:
            return {"ok": False, "error": err}
        self._plan = None
        self._last_result = None
        self._run(core.check_job, opts)
        return {"ok": True}

    def set_filter(self, f):
        """f: None, or a filter dict (see core.clean_filter). Returns the updated summary."""
        if not self._plan:
            return {"ok": False, "error": "Check a collection first."}
        core.apply_filter(self._plan, core.clean_filter(f))
        return {"ok": True, "plan": self._summary(self._plan)}

    def _summary(self, p):
        if not p:
            return None
        return {
            "cid": p["cid"], "name": p["name"], "lazer": p["lazer"], "out_dir": p["out_dir"],
            "total_maps": p["total_maps"], "total_sets": p["total_sets"],
            "maps": p["maps"], "sets": len(p["sets"]), "have": p["have"], "todo": len(p["todo"]),
            "est_bytes": p["est_bytes"], "est_rough": p["est_rough"],
            "free": core.free_space(p["out_dir"]), "fields": p["fields"],
            "filter_label": p.get("filter_label", ""),
            "filtered": bool(p.get("filter")),
        }

    def plan(self):
        return self._summary(self._plan)

    def download(self, link, f=None):
        """Start downloading. Re-checks first if the settings changed since the check."""
        if self.is_busy():
            return {"ok": False, "error": "Already working on something."}
        opts, err = self._validate(link)
        if err:
            return {"ok": False, "error": err}
        if not self._plan or self._plan["key"] != core.plan_key(opts):
            return {"ok": False, "error": "recheck"}
        self._plan["have_ids"] = core.existing_set_ids(self._plan["out_dir"])
        self.set_filter(f)
        if not self._plan["todo"]:
            return {"ok": False, "error": "You already have every matching map." if self._plan["sets"]
                    else "No maps match your filter."}
        self._run(core.run_download, self._plan, opts)
        return {"ok": True, "plan": self._summary(self._plan)}

    def stop(self):
        self._stop.set()
        return True

    # ------------------------------------------------------------ system --
    def open_folder(self, which="maps"):
        r = self._last_result or {}
        if which == "collection":
            return _open_path(r.get("col_dir"))
        path = r.get("out_dir") or (self._plan or {}).get("out_dir")
        return _open_path(path)

    def open_url(self, url):
        if str(url).startswith(("https://osucollector.com/", "https://osu.ppy.sh/",
                                "https://github.com/", "https://go.microsoft.com/")):
            webbrowser.open(url)
            return True
        return False

    def pick_folder(self, start=""):
        if not self._window:
            return None
        import webview
        dialog = getattr(webview, "FileDialog", None)
        kind = dialog.FOLDER if dialog else webview.FOLDER_DIALOG
        res = self._window.create_file_dialog(kind, directory=start or "")
        if res:
            return os.path.normpath(res[0] if isinstance(res, (list, tuple)) else res)
        return None

    # ------------------------------------------------------------- music --
    def music_files(self, folder):
        """List audio files in folder and serve them on a private local address.
        Returns {"ok", "base", "files"}; the page plays base + encoded file name."""
        folder = os.path.normpath(str(folder or ""))
        if not folder or not os.path.isdir(folder):
            return {"ok": False, "error": "That music folder doesn't exist."}
        files = sorted(f for f in os.listdir(folder)
                       if f.lower().endswith(AUDIO_EXT) and os.path.isfile(os.path.join(folder, f)))
        if not files:
            return {"ok": False, "error": "No music files (mp3, ogg, m4a, wav, flac) in that folder."}
        if self._music_dir != folder:
            self._stop_music_server()
            token, root = self._music_token, folder

            class Handler(SimpleHTTPRequestHandler):
                def __init__(h, *a, **k):
                    super().__init__(*a, directory=root, **k)

                def log_message(h, *a):
                    pass

                def send_head(h):
                    # only /<token>/<audio file> in the chosen folder, nothing else
                    prefix = f"/{token}/"
                    if not h.path.startswith(prefix):
                        h.send_error(404)
                        return None
                    h.path = "/" + h.path[len(prefix):]
                    name = h.path.split("?", 1)[0].lower()
                    if "/" in name.strip("/") or not name.endswith(AUDIO_EXT):
                        h.send_error(404)
                        return None
                    return super().send_head()

            self._music_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=self._music_server.serve_forever, daemon=True).start()
            self._music_dir = folder
        port = self._music_server.server_address[1]
        return {"ok": True, "base": f"http://127.0.0.1:{port}/{self._music_token}/", "files": files}

    def _stop_music_server(self):
        if self._music_server:
            self._music_server.shutdown()
            self._music_server.server_close()
        self._music_server = None
        self._music_dir = None

    def quit(self):
        self._stop.set()
        self._stop_music_server()
        if self._window:
            self._window.destroy()
