#!/usr/bin/env python3
"""
Catalog Check  -  proves which entries of app_catalog.json really work, without downloading anything big.

HOW TO USE
    1. Put this file in the same folder as release_tracker.py (and app_catalog.json).
    2. Double-click it, or run:   python catalog_check.py
       (no window?  python catalog_check.py --cli)
    3. Click "Start". When it is done, click "Save report" and send me catalog_check_summary.txt.

WHAT IT DOES FOR EVERY APP
    It runs your tracker's own "Check Only" code on the entry, exactly as if you had just added the app
    from the catalog, then confirms three more things the tracker does not look at:
      * the file really downloads  (asks the server for the first 4 bytes only - never the whole file)
      * the file is what its name says  (a real .zip / .exe / .7z, not a web page)
      * inside a .zip: there is a program, and it is the one the Run button will start
        (reads only the END of the zip, where its file list is - usually about 128 KB - using HTTP range requests)

    Results:  PASS = works      WARN = works but needs attention (reason given)
              FAIL = does not work (reason given)      LIMITED = the site said "too many requests" - re-run later
    Installer entries (Opera, Bitvise, calibre ...) can only be proven by really installing: they are
    marked "install not tested".

    Nothing is downloaded or installed, and your tracker, repos.json and catalog are not changed -
    unless you click "Apply results to catalog" (a backup is made first).

IF SOMETHING GOES WRONG
    * catalog_check.log (next to your catalog) is written from the first second: every result and every problem.
    * The checking runs in a separate helper process, so even a hard crash cannot close the window; you get a message
      and the results so far are kept. Click Start to continue where it stopped.
    * The report is saved automatically when the check finishes.
"""
import argparse
import concurrent.futures as cf
import csv
import faulthandler
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT_NAMES = ("catalog_check_report.json", "catalog_check_report.csv", "catalog_check_summary.txt")
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MAGIC = ((".paf.exe", b"MZ"), (".exe", b"MZ"), (".zip", b"PK"), (".7z", b"7z\xbc\xaf"), (".msi", b"\xd0\xcf\x11\xe0"),
         (".rar", b"Rar!"), (".tgz", b"\x1f\x8b"), (".tar.gz", b"\x1f\x8b"), (".gz", b"\x1f\x8b"),
         (".xz", b"\xfd7zX"))
NOT_WINDOWS = re.compile(r"linux|macos|darwin|osx|\.dmg|\.deb|\.rpm|appimage|android|freebsd|\.apk", re.I)
ABOUT_ADD = (" Check fields (written by catalog_check.py when you click 'Apply results'): last_check (date), "
             "check_result (ok | warn | fail) and check_note (why). verified = true only when the last check passed.")
UNVERIFIED_PREFIX = re.compile(r"^Unverified entry\s*[—-]\s*run Fetch Assets once( to confirm the file)?\.\s*")


# --------------------------------------------------------------------------- #
# options / small helpers
# --------------------------------------------------------------------------- #
@dataclass
class Options:
    probe: bool = True          # ask the server for the first bytes of every file
    peek: bool = True           # read the file list of .zip files
    workers: int = 6
    retries: int = 2            # extra tries when a site answers "too many requests"
    backoff: tuple = (8.0, 25.0)
    delay: float = 0.3          # pause after each GitHub entry (politeness)
    timeout: float = 30.0


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


_QT_APP = []


def ensure_qt_app():
    """The tracker's worker is a Qt object; like the tracker's own command-line mode, make sure an application exists."""
    try:
        from PySide6.QtCore import QCoreApplication
        if QCoreApplication.instance() is None:
            _QT_APP.append(QCoreApplication(sys.argv[:1]))
    except Exception:
        pass


def load_tracker(tracker_dir=None):
    """Import release_tracker.py (same folder as this file unless told otherwise)."""
    for d in (tracker_dir, os.environ.get("CATALOG_CHECK_TRACKER_DIR"), str(HERE)):
        if d and d not in sys.path:
            sys.path.insert(0, d)
    try:
        import release_tracker as rt
    except ModuleNotFoundError as e:
        if e.name == "release_tracker":
            raise SystemExit("release_tracker.py was not found. Put catalog_check.py in the same folder as "
                             "release_tracker.py and run it again.")
        raise SystemExit(f"release_tracker.py needs the module '{e.name}'. Install it with:  pip install PySide6")
    return rt


def clean(rt, url: str) -> str:
    fn = getattr(rt, "clean_url", None)
    try:
        return fn(url) if fn else urllib.parse.quote(url, safe="%/:?&=#;+,@!$'()*[]~-._")
    except Exception:
        return url


def safe_header(rt, value: str) -> str:
    """HTTP header values must be Latin-1: percent-encode the rest (a Referer with an odd character must not fail the check)."""
    v = clean(rt, value or "")
    try:
        v.encode("latin-1")
        return v
    except UnicodeEncodeError:
        return v.encode("ascii", "ignore").decode("ascii")


def host_of(url: str) -> str:
    return (urllib.parse.urlparse(url or "").hostname or "").lower()


# --------------------------------------------------------------------------- #
# hooks: capture what the tracker found, without changing what it does
# --------------------------------------------------------------------------- #
class _Ctx(threading.local):
    rel = None
    asset = None


CTX = _Ctx()
HOOK = {"installed": False, "inner_release": None}


def install_hooks(rt):
    if HOOK["installed"]:
        return
    HOOK["inner_release"] = rt.get_release_for

    def get_release_for(*a, **k):
        rel = HOOK["inner_release"](*a, **k)
        CTX.rel = rel
        return rel

    rt.get_release_for = get_release_for
    W = rt.UpdateWorker
    if hasattr(W, "_pick_asset"):
        orig = W._pick_asset

        def _pick_asset(self, *a, **k):
            asset = orig(self, *a, **k)
            CTX.asset = asset
            return asset

        W._pick_asset = _pick_asset
    HOOK["installed"] = True


class _Sig:
    """Stands in for a Qt signal: records everything the tracker 'emits'."""

    def __init__(self):
        self.calls = []

    def emit(self, *a):
        self.calls.append(a)

    def connect(self, *_a):
        pass


def tracker_settings(rt, tmpdir: str) -> dict:
    """Your token / pre-release / API choices from repos.json (read only), plus a throw-away download folder."""
    s = {}
    try:
        p = Path(getattr(rt, "CONFIG_FILE", HERE / "repos.json"))
        if p.is_file():
            s = dict((json.loads(p.read_text(encoding="utf-8")).get("settings")) or {})
    except Exception:
        s = {}
    s.update({"download_dir": tmpdir, "global_install_dir": "", "after_download": "never", "window": {}})
    s.setdefault("github_token", "")
    return s


# --------------------------------------------------------------------------- #
# probing a file: first bytes only
# --------------------------------------------------------------------------- #
def fetch_page(rt, url: str, referer: str = "", timeout: float = 30.0, limit: int = 400_000) -> str:
    headers = {"User-Agent": UA_BROWSER, "Accept": "*/*"}
    if referer:
        headers["Referer"] = safe_header(rt, referer)
    with urllib.request.urlopen(urllib.request.Request(clean(rt, url), headers=headers), timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "replace")


def probe_file(rt, url: str, referer: str = "", timeout: float = 30.0, want_name: str = "", _hop: int = 0) -> dict:
    """GET with 'Range: bytes=0-3'.  -> {ok, code, total, magic, ctype, ranged, final, error, network, via}
    A web page where the file should be is followed the way the tracker's downloader follows it
    (meta refresh / script redirect / link to the file), up to two steps."""
    pr = _probe_once(rt, url, referer, timeout)
    if pr["ok"] and ("text/html" in pr["ctype"] or pr["magic"][:1] == b"<") and _hop < 2:
        follow = getattr(rt, "follow_link_in_page", None)
        if follow:
            try:
                nxt = follow(fetch_page(rt, url, referer, timeout), pr.get("final") or url, want_name)
            except Exception:
                nxt = ""
            if nxt and nxt != url:
                sub = probe_file(rt, nxt, url, timeout, want_name, _hop + 1)
                if sub["ok"]:
                    sub["via"] = (sub.get("via") or []) + [url]
                return sub
    return pr


def _probe_once(rt, url: str, referer: str = "", timeout: float = 30.0) -> dict:
    headers = {"User-Agent": UA_BROWSER, "Accept": "*/*", "Range": "bytes=0-3"}
    if referer:
        headers["Referer"] = safe_header(rt, referer)
    req = urllib.request.Request(clean(rt, url), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.status
            ctype = (r.headers.get("Content-Type") or "").lower()
            cr = r.headers.get("Content-Range") or ""
            m = re.match(r"bytes\s+\d+-\d+/(\d+)", cr)
            if code == 206 and m:
                total = int(m.group(1))
            elif code == 200:
                total = int(r.headers.get("Content-Length") or 0)
            else:
                total = 0
            magic = r.read(4)
            return {"ok": True, "code": code, "total": total, "magic": magic, "ctype": ctype,
                    "ranged": code == 206 and bool(m), "final": r.geturl()}
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": e.code, "error": f"HTTP error {e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "code": 0, "error": f"network error: {e.reason}", "network": True}
    except Exception as e:                      # timeouts, resets
        return {"ok": False, "code": 0, "error": f"{type(e).__name__}: {e}", "network": True}


class RangeFile(io.RawIOBase):
    """A read-only file made of HTTP range requests (just enough for zipfile to read a zip's index)."""
    BLOCK = 128 * 1024
    MAX_REQUESTS = 24

    def __init__(self, rt, url, size, referer="", timeout=30.0):
        self.rt, self.url, self.size, self.referer, self.timeout = rt, url, size, referer, timeout
        self.pos = 0
        self.cache = {}
        self.requests = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        self.pos = max(0, min(self.pos, self.size))
        return self.pos

    def _block(self, i):
        if i in self.cache:
            return self.cache[i]
        if self.requests >= self.MAX_REQUESTS:
            raise OSError("zip index is too scattered to read cheaply")
        a = i * self.BLOCK
        b = min(a + self.BLOCK, self.size) - 1
        headers = {"User-Agent": UA_BROWSER, "Accept": "*/*", "Range": f"bytes={a}-{b}"}
        if self.referer:
            headers["Referer"] = safe_header(self.rt, self.referer)
        self.requests += 1
        with urllib.request.urlopen(urllib.request.Request(clean(self.rt, self.url), headers=headers),
                                    timeout=self.timeout) as r:
            if r.status != 206:
                raise OSError("server does not support range requests")
            data = r.read()
        if len(data) != b - a + 1:
            raise OSError("short range answer")
        self.cache[i] = data
        return data

    def readinto(self, buf):
        want = min(len(buf), self.size - self.pos)
        got = 0
        while got < want:
            i, off = divmod(self.pos, self.BLOCK)
            chunk = self._block(i)[off:off + (want - got)]
            buf[got:got + len(chunk)] = chunk
            got += len(chunk)
            self.pos += len(chunk)
        return got


def peek_zip(rt, url, size, referer="", timeout=30.0):
    """-> (entries [(name, size)], requests_used).  Raises zipfile.BadZipFile / OSError."""
    rf = RangeFile(rt, url, size, referer, timeout)
    with zipfile.ZipFile(rf) as z:
        entries = [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]
    return entries, rf.requests


def best_exe(rt, entries, app_name, exe_name="", hint=""):
    fn = getattr(rt, "pick_best_exe", None)
    if fn:
        try:
            return fn(entries, app_name, exe_name, hint)
        except Exception:
            pass
    exes = [n for n, _ in entries if n.lower().endswith(".exe")]
    if hint:
        for n in exes:
            if fnmatch.fnmatch(n.replace("\\", "/").rsplit("/", 1)[-1].lower(), hint.lower()):
                return n
    return exes[0] if exes else None


# --------------------------------------------------------------------------- #
# checking one app
# --------------------------------------------------------------------------- #
_GATES = {}
_GATES_LOCK = threading.Lock()


def gate_for(host: str) -> threading.Semaphore:
    with _GATES_LOCK:
        if host not in _GATES:
            _GATES[host] = threading.Semaphore(2 if host.endswith("github.com") else 3)
        return _GATES[host]


def is_limited(text: str, host: str) -> bool:
    """'Too many requests' answers only (a file called tool-1429.zip must not count)."""
    t = (text or "").lower()
    if re.search(r"error\s*429\b|\b429\s+too many|rate.?limit|too many requests", t):
        return True
    return host.endswith("github.com") and bool(re.search(r"error\s*403\b", t))


def run_tracker_check(rt, app: dict, settings: dict):
    """The tracker's own Check Only on this one app.  -> (repo, status_text, latest, errors, logs, rel, asset)"""
    repo = rt.catalog_entry_to_repo(app, "")
    CTX.rel = CTX.asset = None
    w = rt.UpdateWorker([repo], dict(settings), False)
    sigs = {n: _Sig() for n in ("log", "row_status", "row_latest", "row_progress", "repo_updated", "overall", "finished")}
    for n, s in sigs.items():
        try:
            setattr(w, n, s)
        except Exception:
            pass
    w.run()
    logs = [str(c[0]) for c in sigs["log"].calls if c]
    errors = [re.sub(r"^\[[^\]]*\]\s*ERROR:\s*", "", l).strip() for l in logs if "ERROR:" in l]
    status = ""
    for c in sigs["row_status"].calls:
        if len(c) > 1:
            status = str(c[1])
    latest = ""
    for c in sigs["row_latest"].calls:
        if len(c) > 1:
            latest = str(c[1]).strip()
    latest = latest or str(repo.get("latest_seen") or repo.get("tag") or "")
    return repo, status, latest, errors, logs, CTX.rel, CTX.asset


def check_app(rt, app: dict, opts: Options, settings: dict, stop=None) -> dict:
    t0 = time.time()
    name = app.get("name", "?")
    res = {"name": name, "category": app.get("category", ""), "source": app.get("source_mode", "auto") or "auto",
           "status": "FAIL", "latest": "", "file": "", "size": 0, "url": "", "run_target": "", "note": "",
           "warnings": [], "info": [], "pattern": app.get("asset_pattern", ""), "pattern_suggested": "", "manual": False,
           "needs_7zip": False, "seconds": 0.0, "checked_at": now_iso()}
    host = host_of(app.get("url", ""))
    try:
        for attempt in range(opts.retries + 1):
            if stop is not None and stop.is_set():
                res.update(status="SKIP", note="stopped before this app was checked")
                return res
            with gate_for(host):
                try:
                    repo, status, latest, errors, logs, rel, asset = run_tracker_check(rt, app, settings)
                except Exception as e:                       # bad URL etc. raised before the worker started
                    repo, status, latest, errors, logs, rel, asset = {}, "", "", [f"{type(e).__name__}: {e}"], [], None, None
                if host.endswith("github.com"):
                    time.sleep(opts.delay)
            if errors and is_limited(errors[0], host) and attempt < opts.retries:
                time.sleep(opts.backoff[min(attempt, len(opts.backoff) - 1)])
                continue
            break
        res["latest"] = latest
        if errors or status.startswith("❌") or not status:
            msg = errors[0] if errors else (status or "the tracker produced no result")
            if is_limited(msg, host):
                res.update(status="LIMITED", note=f"the site refused too many requests ({msg[:120]}). "
                                                  "Re-run later, or put a GitHub token in the tracker's Settings.")
            else:
                res.update(status="FAIL", note=msg)
            return res
        if asset is None and rel:
            asset = _asset_from_logs(rel, logs)
        if asset is None:
            res.update(status="FAIL", note="the tracker found a release but could not say which file it would download")
            return res
        warns = res["warnings"]
        fname = asset.get("name", "")
        res.update(file=fname, size=int(asset.get("size") or 0), url=asset.get("browser_download_url", ""))
        sugg = (repo or {}).get("asset_pattern", "")
        if sugg and sugg != res["pattern"]:
            res["pattern_suggested"] = sugg
            warns.append(f"the catalog pattern '{res['pattern']}' no longer matches; the tracker only found "
                         f"'{fname}' by auto-pick. Suggested pattern: '{sugg}'")
        if not re.search(r"\d", latest or ""):
            warns.append("no version number could be read, so the tracker cannot tell a new version from an old one")
        low = fname.lower()
        if NOT_WINDOWS.search(low) and "win" not in low:
            warns.append("the file name looks like it is not for Windows")
        if re.search(r"arm64|aarch64", low) and not re.search(r"x64|x86_64|amd64", low):
            warns.append("the file looks like an ARM build, not x64")
        if app.get("install_type") == "installer":
            res["manual"] = True
            warns_manual = "installer entry: the download is checked, but its silent switches can only be proven by really installing"
        else:
            warns_manual = ""
        res["needs_7zip"] = low.endswith((".7z", ".paf.exe", ".rar")) or app.get("install_type") == "sfx"
        if opts.probe and res["url"]:
            _probe_and_peek(rt, app, res, opts)
        if res["status"] == "FAIL" and res["note"]:
            return res
        res["status"] = "WARN" if warns else "PASS"
        notes = list(warns) + list(res["info"])
        if warns_manual:
            notes.append(warns_manual)
        if res["needs_7zip"]:
            notes.append("needs 7-Zip set in the tracker's Options")
        if "administrator" in (app.get("notes") or "").lower():
            notes.append("needs administrator rights")
        res["note"] = "; ".join(notes)
        return res
    except Exception as e:
        res.update(status="FAIL", note=f"checker error: {type(e).__name__}: {e}")
        return res
    finally:
        res["seconds"] = round(time.time() - t0, 1)


def _asset_from_logs(rel, logs):
    for l in logs:
        m = re.search(r"available \((.+), [^,()]*\)\s*$", l)
        if m:
            for a in rel.get("assets", []):
                if a.get("name") == m.group(1):
                    return a
    return None


def _probe_and_peek(rt, app, res, opts):
    """Fills res (size / status / warnings) from the first bytes and, for zips, the file list."""
    warns = res["warnings"]
    referer = app.get("url", "")
    pr = probe_file(rt, res["url"], referer, opts.timeout, res["file"])
    if pr.get("via"):
        res["info"].append("the link opens a download page that the tracker follows to the real file")
        res["url_final"] = pr.get("final", "")
    if not pr["ok"]:
        if pr.get("network"):
            res.update(status="FAIL", note=f"the download address could not be reached ({pr['error']})")
        else:
            res.update(status="FAIL", note=f"the download address answers {pr['error']} — the file is not there")
        return
    low = res["file"].lower()
    magic = pr["magic"]
    if "text/html" in pr["ctype"] or magic[:1] == b"<":
        res.update(status="FAIL", note="the download address returns a web page, not the file "
                                       "(a login / 'start download' page, or the link needs a browser)")
        return
    for ext, want in MAGIC:
        if low.endswith(ext):
            if not magic.startswith(want):
                res.update(status="FAIL", note=f"the file does not look like a real {ext} "
                                               f"(it starts with {magic[:4]!r}) — probably an error page")
                return
            break
    total = pr["total"] or res["size"]
    if total:
        res["size"] = total
        if total < 50 * 1024:
            warns.append(f"the file is only {human(total)} — probably not the program itself")
    if not (opts.peek and low.endswith(".zip") and pr["ranged"] and total):
        if opts.peek and low.endswith(".zip") and not pr["ranged"]:
            res["info"].append("zip contents not checked (the server does not allow reading inside the zip cheaply)")
        return
    try:
        entries, _req = peek_zip(rt, pr.get("final") or res["url"], total, referer, opts.timeout)
    except zipfile.BadZipFile:
        res.update(status="FAIL", note="the file is not a valid zip archive (corrupt, or not a zip)")
        return
    except Exception as e:
        res["info"].append(f"zip contents not checked ({e})")
        return
    exes = [n for n, _ in entries if n.lower().endswith(".exe")]
    hint = (app.get("exe_hint") or "").strip()
    exe_name = (app.get("exe_name") or "").strip()
    pick = best_exe(rt, entries, app.get("name", ""), exe_name, hint)
    res["run_target"] = pick or ""
    res["inside_count"] = len(entries)
    if not exes:
        warns.append("there is no .exe inside the zip — check the install type (is it really a ready-to-run build?)")
    elif hint and not any(fnmatch.fnmatch(n.replace("\\", "/").rsplit("/", 1)[-1].lower(), hint.lower()) for n in exes):
        shown = ", ".join(sorted({n.replace("\\", "/").rsplit("/", 1)[-1] for n in exes})[:6])
        warns.append(f"exe_hint '{hint}' is not inside the zip (programs found: {shown})")
    elif pick is None:
        warns.append("only helper programs (uninstall / updater) were found inside the zip")


# --------------------------------------------------------------------------- #
# running many
# --------------------------------------------------------------------------- #
def run_checks(rt, apps, opts: Options, settings: dict, stop=None, on_result=None, on_progress=None):
    results = []
    lock = threading.Lock()
    done = [0]
    install_hooks(rt)

    def task(app):
        if stop is not None and stop.is_set():
            r = {"name": app.get("name", "?"), "category": app.get("category", ""), "status": "SKIP",
                 "note": "stopped before this app was checked", "latest": "", "file": "", "size": 0, "url": "",
                 "run_target": "", "warnings": [], "info": [], "source": app.get("source_mode", "auto"), "pattern": "",
                 "pattern_suggested": "", "manual": False, "needs_7zip": False, "seconds": 0.0, "checked_at": now_iso()}
        else:
            r = check_app(rt, app, opts, settings, stop)
        with lock:
            done[0] += 1
            results.append(r)
            n = done[0]
        if on_result:
            on_result(r)
        if on_progress:
            on_progress(n, len(apps))
        return r

    with cf.ThreadPoolExecutor(max_workers=max(1, int(opts.workers))) as ex:
        list(ex.map(task, apps))
    order = {a.get("name"): i for i, a in enumerate(apps)}
    results.sort(key=lambda r: order.get(r["name"], 0))
    return results


# --------------------------------------------------------------------------- #
# self-test: does this checker fit YOUR release_tracker.py?
# --------------------------------------------------------------------------- #
def selftest(rt):
    install_hooks(rt)
    app = {"name": "__SelfTest", "category": "x", "description": "", "url": "https://github.com/selftest/selftest",
           "source_mode": "auto", "asset_pattern": "selftest-{version}-win64.zip", "link_text": "", "link_context": "",
           "install_type": "archive", "install_mode": "merge", "strip_mode": "auto", "protect": "", "exe_name": "",
           "installer_args": "", "folder": "SelfTest", "auto_pick": False, "file_hint": "", "pick": "",
           "version_regex": "", "rev": 1, "verified": False, "notes": ""}
    tmp = tempfile.mkdtemp(prefix="rtselftest_")
    opts = Options(probe=False, peek=False, retries=0, delay=0)
    old = HOOK["inner_release"]
    try:
        settings = tracker_settings(rt, tmp)
        HOOK["inner_release"] = lambda *a, **k: {"tag_name": "v1.2.3", "assets": [
            {"name": "selftest-1.2.3-win64.zip", "size": 123456, "browser_download_url": "https://example.invalid/s.zip"}]}
        r1 = check_app(rt, app, opts, settings)
        ok1 = r1["status"] == "PASS" and "1.2.3" in r1["latest"] and r1["file"] == "selftest-1.2.3-win64.zip"

        def boom(*a, **k):
            raise RuntimeError("HTTP error 404 for selftest")

        HOOK["inner_release"] = boom
        r2 = check_app(rt, app, opts, settings)
        ok2 = r2["status"] == "FAIL" and "404" in r2["note"]
        if ok1 and ok2:
            return True, "ok"
        return False, f"good case -> {r1['status']} ({r1['note']}); error case -> {r2['status']} ({r2['note']})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        HOOK["inner_release"] = old
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# catalog / scope / reports
# --------------------------------------------------------------------------- #
def catalog_path(rt, arg=None) -> Path:
    if arg:
        return Path(arg)
    p = getattr(rt, "CATALOG_FILE", None)
    return Path(p) if p else HERE / "app_catalog.json"


def load_apps(path: Path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data, list(data.get("apps", []))


def select_apps(apps, names=None, text="", category="", not_passed=False):
    out = []
    names_l = {n.casefold() for n in (names or [])}
    for a in apps:
        if names_l and a.get("name", "").casefold() not in names_l:
            continue
        if text and text.casefold() not in a.get("name", "").casefold():
            continue
        if category and a.get("category", "") != category:
            continue
        if not_passed and a.get("check_result") == "ok":
            continue
        out.append(a)
    return out


def summarize(results):
    c = {k: 0 for k in ("PASS", "WARN", "FAIL", "LIMITED", "SKIP")}
    for r in results:
        c[r["status"]] = c.get(r["status"], 0) + 1
    return c


def failures_text(results) -> str:
    c = summarize(results)
    lines = [f"Catalog check {datetime.now():%Y-%m-%d %H:%M} — {len(results)} apps: "
             f"{c['PASS']} passed, {c['WARN']} warnings, {c['FAIL']} failed, {c['LIMITED']} limited, {c['SKIP']} skipped", ""]
    for title, key in (("FAILED", "FAIL"), ("WARNINGS", "WARN"), ("LIMITED (re-run later)", "LIMITED")):
        rows = [r for r in results if r["status"] == key]
        if rows:
            lines.append(f"== {title} ({len(rows)}) ==")
            for r in rows:
                extra = f"  [latest {r['latest']}, file {r['file']}]" if key == "WARN" and r.get("file") else ""
                lines.append(f"- {r['name']}: {r['note']}{extra}")
            lines.append("")
    manual = [r["name"] for r in results if r.get("manual") and r["status"] in ("PASS", "WARN")]
    if manual:
        lines.append("Installer entries (install not tested): " + ", ".join(manual))
    passed = [r["name"] for r in results if r["status"] == "PASS"]
    lines.append("")
    lines.append(f"== PASSED ({len(passed)}) ==")
    lines.append(", ".join(passed))
    return "\n".join(lines)


def save_reports(results, outdir: Path):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / REPORT_NAMES[0]).write_text(json.dumps(
        {"checked_at": now_iso(), "summary": summarize(results), "results": results}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    with open(outdir / REPORT_NAMES[1], "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["app", "category", "result", "latest", "file", "size_mb", "run_target", "source", "note"])
        for r in results:
            w.writerow([r["name"], r["category"], r["status"], r["latest"], r["file"],
                        round((r.get("size") or 0) / 1048576, 1), r.get("run_target", ""), r.get("source", ""), r["note"]])
    (outdir / REPORT_NAMES[2]).write_text(failures_text(results), encoding="utf-8")
    return [outdir / n for n in REPORT_NAMES]


def apply_results(path: Path, results) -> dict:
    """Write verified / last_check / check_result / check_note into the catalog (backup first)."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    by = {a.get("name"): a for a in data.get("apps", [])}
    changed = skipped = 0
    for r in results:
        a = by.get(r["name"])
        if a is None or r["status"] in ("LIMITED", "SKIP"):
            skipped += 1
            continue
        a["verified"] = r["status"] == "PASS"
        a["check_result"] = {"PASS": "ok", "WARN": "warn", "FAIL": "fail"}[r["status"]]
        a["last_check"] = r["checked_at"][:10]
        a["check_note"] = (r["note"] or "")[:400]
        if r["status"] == "PASS" and a.get("notes"):
            a["notes"] = UNVERIFIED_PREFIX.sub("", a["notes"]).strip()
        changed += 1
    if "check_result" not in data.get("about", "") and "last_check" not in data.get("about", ""):
        data["about"] = (data.get("about", "") + ABOUT_ADD).strip()
    backup = path.with_name(path.name + ".bak")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak{n}")
        n += 1
    shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return {"changed": changed, "skipped": skipped, "backup": str(backup)}


# --------------------------------------------------------------------------- #
# logging + crash diagnostics: a silent close must always leave a trail
# --------------------------------------------------------------------------- #
LOG_NAME = "catalog_check.log"
PROGRESS_NAME = "catalog_check_progress.jsonl"
_LOG = {"path": None, "crash_fh": None, "crash_path": None, "lock": threading.Lock(), "errors": 0}


def writable_dir(preferred=None) -> Path:
    """The first folder we can really write into (the tracker's folder may be read-only)."""
    for d in (Path(preferred) if preferred else None, Path.home() / "Documents", Path(tempfile.gettempdir())):
        if d is None:
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".catalog_check_write_test"
            probe.write_text("x")
            probe.unlink()
            return d
        except OSError:
            continue
    return Path(tempfile.gettempdir())


def log(msg: str):
    p = _LOG["path"]
    if not p:
        return
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n"
    with _LOG["lock"]:
        for _ in range(3):
            try:
                with open(p, "a", encoding="utf-8", errors="replace") as f:
                    f.write(line)
                return
            except OSError:
                time.sleep(0.05)


def init_logging(folder, tag: str) -> Path:
    """Creates catalog_check.log, turns on crash dumps (faulthandler) and logs every uncaught exception."""
    d = writable_dir(folder)
    p = d / LOG_NAME
    try:
        if p.exists() and p.stat().st_size > 2_000_000:
            p.replace(p.with_name(p.name + ".old"))
    except OSError:
        pass
    _LOG["path"] = p
    try:
        cp = d / f"catalog_check_crash_{tag}.log"
        fh = open(cp, "a", encoding="utf-8")
        faulthandler.enable(file=fh, all_threads=True)
        _LOG["crash_fh"], _LOG["crash_path"] = fh, cp
    except Exception:
        pass

    def hook(et, ev, tb):
        _LOG["errors"] += 1
        log(f"UNCAUGHT {et.__name__}: {ev}\n" + "".join(traceback.format_tb(tb)))
        try:
            sys.__excepthook__(et, ev, tb)
        except Exception:
            pass

    def thook(a):
        _LOG["errors"] += 1
        name = a.thread.name if a.thread else "?"
        log(f"UNCAUGHT in thread {name}: {a.exc_type.__name__}: {a.exc_value}\n" + "".join(traceback.format_tb(a.exc_traceback)))

    sys.excepthook = hook
    threading.excepthook = thook
    return d


def finish_logging():
    fh, cp = _LOG["crash_fh"], _LOG["crash_path"]
    try:
        if fh:
            faulthandler.disable()
            fh.close()
        if cp and Path(cp).exists() and Path(cp).stat().st_size == 0:
            Path(cp).unlink()                    # no crash happened: leave no empty file behind
    except Exception:
        pass
    _LOG["crash_fh"] = None


def open_path(path):
    """Open a file / folder with the system's default program."""
    p = str(path)
    if sys.platform.startswith("win"):
        os.startfile(p)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def py_executable() -> str:
    """python.exe for the helper process (pythonw.exe has no console streams to talk through)."""
    exe = sys.executable or "python"
    if sys.platform.startswith("win") and exe.lower().endswith("pythonw.exe"):
        cand = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.isfile(cand):
            return cand
    return exe


def last_crash_dump(path: Path, max_lines: int = 40) -> str:
    """The newest crash dump (Python's own 'Fatal Python error …' stack) from the helper's crash file."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    i = text.rfind("Fatal Python error")
    if i < 0:
        i = text.rfind("Windows fatal exception")
    if i < 0:
        return ""
    return "\n".join(text[i:].splitlines()[:max_lines])


def load_progress(path: Path) -> dict:
    """{app name: result} from an interrupted run (one JSON result per line)."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("name") and r.get("status") in ("PASS", "WARN", "FAIL", "LIMITED"):
                    out[r["name"]] = r
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #
def run_cli(args) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    rt = load_tracker(args.tracker_dir)
    ensure_qt_app()
    if not args.skip_selftest:
        ok, why = selftest(rt)
        if not ok:
            print("SELF-TEST FAILED: this catalog_check.py does not fit your release_tracker.py.\n  " + why +
                  "\nSend me both files and I will adjust it.")
            return 2
    cpath = catalog_path(rt, args.catalog)
    out = Path(args.out) if args.out else cpath.parent
    out = init_logging(out, "cli")
    _data, apps = load_apps(cpath)
    todo = select_apps(apps, args.only, args.text, args.category, args.not_passed)
    if not todo:
        print("No apps match.")
        finish_logging()
        return 0
    opts = Options(probe=not args.no_probe, peek=not args.no_peek, workers=args.workers)
    if args.backoff is not None:
        opts.backoff = (args.backoff, args.backoff * 3)
    tmp = tempfile.mkdtemp(prefix="rtcheck_")
    tag = {"PASS": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]", "LIMITED": "[LIMIT]", "SKIP": "[SKIP]"}
    print(f"Checking {len(todo)} app(s) from {cpath.name} with {opts.workers} workers ...\n")
    log(f"CLI run: {len(todo)} apps, {opts.workers} workers, catalog {cpath}")

    def show(r):
        extra = f"{r['latest']}  {r['file']}  ({human(r['size'])})" if r["status"] in ("PASS", "WARN") else r["note"][:110]
        print(f"{tag[r['status']]:7} {r['name'][:34]:34} {extra}", flush=True)
        log(f"{r['status']:7} {r['name']}: {r['note'][:200] or r['latest']}")

    try:
        results = run_checks(rt, todo, opts, tracker_settings(rt, tmp), None, show)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    files = save_reports(results, out)
    c = summarize(results)
    print(f"\n{c['PASS']} passed, {c['WARN']} warnings, {c['FAIL']} failed, {c['LIMITED']} limited, {c['SKIP']} skipped")
    print("Reports: " + ", ".join(str(f) for f in files))
    log(f"CLI run finished: {c}")
    if args.apply:
        info = apply_results(cpath, results)
        print(f"Catalog updated: {info['changed']} entries (backup: {info['backup']})")
    finish_logging()
    return 1 if c["FAIL"] else 0


# --------------------------------------------------------------------------- #
# helper process: does the checking for the window (so a crash can never close the window)
# --------------------------------------------------------------------------- #
def worker_main(args) -> int:
    """Talks to the window through JSON lines on stdout; stdin 'stop' (or the window closing) stops it."""
    proto = sys.stdout
    sys.stdout = sys.stderr                        # anything the tracker prints must not disturb the protocol
    lock = threading.Lock()

    def send(obj):
        line = json.dumps(obj) + "\n"              # ASCII-only JSON: no console-encoding trouble on Windows
        with lock:
            try:
                proto.write(line)
                proto.flush()
            except Exception:
                pass

    logdir = Path(args.log_dir) if args.log_dir else Path(tempfile.gettempdir())
    try:
        cp = logdir / "catalog_check_crash_worker.log"
        fh = open(cp, "a", encoding="utf-8")
        faulthandler.enable(file=fh, all_threads=True)
    except Exception:
        fh, cp = None, None
    tmp = None
    code = 0
    try:
        send({"t": "hello", "pid": os.getpid(), "python": sys.version.split()[0]})
        rt = load_tracker(args.tracker_dir)
        ensure_qt_app()
        if not args.skip_selftest:
            ok, why = selftest(rt)
            send({"t": "selftest", "ok": ok, "why": why})
            if not ok:
                return 2
        apps = json.loads(Path(args.apps_file).read_text(encoding="utf-8"))
        stop = threading.Event()

        def watch_stdin():
            if sys.stdin is None:
                return                              # no control channel at all: just run to the end
            try:
                for line in sys.stdin:
                    if line.strip().lower() == "stop":
                        stop.set()
            except Exception:
                return                              # stdin unreadable: not the same as "window gone" - keep running
            stop.set()                              # clean EOF = the window is gone: stop, never run on as an orphan

        threading.Thread(target=watch_stdin, daemon=True).start()
        opts = Options(probe=not args.no_probe, peek=not args.no_peek, workers=args.workers)
        if args.backoff is not None:
            opts.backoff = (args.backoff, args.backoff * 3)
        tmp = tempfile.mkdtemp(prefix="rtcheck_")
        send({"t": "start", "total": len(apps)})
        results = run_checks(rt, apps, opts, tracker_settings(rt, tmp), stop,
                             lambda r: send({"t": "result", "r": r}),
                             lambda n, t: send({"t": "progress", "n": n, "total": t}))
        send({"t": "done", "summary": summarize(results), "stopped": stop.is_set()})
    except SystemExit as e:
        send({"t": "fatal", "error": str(e.code or e)})
        code = 3
    except BaseException as e:
        send({"t": "fatal", "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})
        code = 3
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        try:
            if fh:
                faulthandler.disable()
                fh.close()
                if cp and Path(cp).exists() and Path(cp).stat().st_size == 0:
                    Path(cp).unlink()
        except Exception:
            pass
    return code


# --------------------------------------------------------------------------- #
# window
# --------------------------------------------------------------------------- #
def run_gui(args) -> int:
    from PySide6.QtCore import QObject, Qt, QTimer, Signal
    from PySide6.QtGui import QBrush, QColor, QGuiApplication
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                                   QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                                   QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView)
    rt = load_tracker(args.tracker_dir)
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        theme = "system"
        p = Path(getattr(rt, "CONFIG_FILE", HERE / "repos.json"))
        if p.is_file():
            theme = (json.loads(p.read_text(encoding="utf-8")).get("settings") or {}).get("theme", "system")
        rt.apply_theme(app, theme)
    except Exception:
        pass
    colors = getattr(rt, "THEME", None) or {}
    ORDER = {"FAIL": 0, "WARN": 1, "LIMITED": 2, "PASS": 3, "SKIP": 4}
    COL = {"PASS": colors.get("ok", "#2e9d4f"), "WARN": colors.get("warn", "#d0902c"),
           "FAIL": colors.get("err", "#d04a4a"), "LIMITED": colors.get("info", "#4a9eff"),
           "SKIP": colors.get("muted", "#888888")}
    LABEL = {"PASS": "✅ PASS", "WARN": "⚠ WARN", "FAIL": "❌ FAIL", "LIMITED": "⏳ LIMITED", "SKIP": "— skipped"}
    cpath = catalog_path(rt, args.catalog)
    out_dir = init_logging(Path(args.out) if args.out else cpath.parent, "gui")
    progress_path = out_dir / PROGRESS_NAME
    _data, apps = load_apps(cpath)
    try:
        import PySide6
        qt_version = PySide6.__version__
    except Exception:
        qt_version = "?"
    log(f"Catalog Check window started | python {sys.version.split()[0]} | PySide6 {qt_version} | {sys.platform} | "
        f"catalog {cpath} ({len(apps)} apps) | folder for reports and log: {out_dir}")

    class Bridge(QObject):
        msg = Signal(dict)

    class Win(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Catalog Check — which catalog entries really work?")
            self.resize(1280, 760)
            self.results = {}
            self.rows = {}
            self.app_by_name = {}
            self.proc = None
            self.running = False
            self.done_flag = False
            self.stopping = False
            self.sort_col, self.sort_asc = 2, True
            self.errlines = []
            self.bridge = Bridge()
            self.bridge.msg.connect(self.on_msg)
            self.kill_timer = QTimer(self)
            self.kill_timer.setSingleShot(True)
            self.kill_timer.timeout.connect(self.kill_worker)
            w = QWidget()
            lay = QVBoxLayout(w)
            top = QHBoxLayout()
            self.scope = QComboBox()
            self.scope.addItem(f"All apps ({len(apps)})", "all")
            self.scope.addItem("Only apps that have not passed yet", "notpassed")
            for c in sorted({a.get("category", "") for a in apps}):
                self.scope.addItem(f"Category: {c}", "cat:" + c)
            self.text = QLineEdit()
            self.text.setPlaceholderText("name contains…")
            self.chk_probe = QCheckBox("Confirm each file downloads")
            self.chk_probe.setChecked(True)
            self.chk_probe.setToolTip("Asks the server for the first 4 bytes of each file. Nothing is saved.")
            self.chk_peek = QCheckBox("Look inside zip files")
            self.chk_peek.setChecked(True)
            self.chk_peek.setToolTip("Reads only the end of each zip (about 128 KB, where its file list is) to confirm a program is inside.")
            self.workers = QSpinBox()
            self.workers.setRange(1, 12)
            self.workers.setValue(6)
            self.workers.setPrefix("workers: ")
            self.workers.setToolTip("How many apps are checked at the same time. 1 = slowest but gentlest.")
            for wdg in (self.scope, self.text, self.chk_probe, self.chk_peek, self.workers):
                top.addWidget(wdg, 1 if wdg is self.text else 0)
            lay.addLayout(top)
            self.table = QTableWidget(0, 7)
            self.table.setHorizontalHeaderLabels(["App", "Category", "Result", "Latest", "File", "Size", "Note"])
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.table.setSortingEnabled(False)           # sorting is done here, in Python (no Qt sort callbacks)
            self.table.verticalHeader().setVisible(False)
            self.table.setAlternatingRowColors(True)
            hh = self.table.horizontalHeader()
            hh.setSectionResizeMode(QHeaderView.Interactive)
            hh.setSectionsClickable(True)
            hh.setSortIndicatorShown(True)
            hh.sectionClicked.connect(self.on_header)
            for i, wd in enumerate((200, 130, 90, 100, 260, 80, 520)):
                self.table.setColumnWidth(i, wd)
            self.table.itemSelectionChanged.connect(self.show_detail)
            lay.addWidget(self.table, 1)
            self.detail = QPlainTextEdit()
            self.detail.setReadOnly(True)
            self.detail.setMaximumHeight(96)
            lay.addWidget(self.detail)
            bar = QHBoxLayout()
            self.progress = QProgressBar()
            self.progress.setFormat("%v / %m")
            self.count = QLabel("")
            self.show_combo = QComboBox()
            for k, t in (("all", "Show: all"), ("FAIL", "Show: ❌ failed"), ("WARN", "Show: ⚠ warnings"),
                         ("PASS", "Show: ✅ passed"), ("LIMITED", "Show: ⏳ limited")):
                self.show_combo.addItem(t, k)
            self.show_combo.currentIndexChanged.connect(self.apply_filter)
            bar.addWidget(self.progress, 1)
            bar.addWidget(self.count)
            bar.addWidget(self.show_combo)
            lay.addLayout(bar)
            btns = QHBoxLayout()
            self.b_start = QPushButton("▶  Start")
            self.b_stop = QPushButton("⏹  Stop")
            self.b_save = QPushButton("💾  Save report…")
            self.b_copy = QPushButton("📋  Copy failures")
            self.b_log = QPushButton("📄  Open log")
            self.b_log.setToolTip(f"{out_dir / LOG_NAME}")
            self.b_apply = QPushButton("✔  Apply results to catalog")
            self.b_apply.setToolTip("Writes verified / last_check / check_result into app_catalog.json (a backup is made).")
            self.b_start.clicked.connect(self.start)
            self.b_stop.clicked.connect(self.stop_run)
            self.b_save.clicked.connect(self.save)
            self.b_copy.clicked.connect(self.copy)
            self.b_log.clicked.connect(self.open_log)
            self.b_apply.clicked.connect(self.apply)
            for b in (self.b_start, self.b_stop):
                btns.addWidget(b)
            btns.addStretch()
            for b in (self.b_log, self.b_save, self.b_copy, self.b_apply):
                btns.addWidget(b)
            lay.addLayout(btns)
            self.setCentralWidget(w)
            self.note = QLabel("")
            self.statusBar().addWidget(self.note, 1)
            self.set_idle(True)
            prev = load_progress(progress_path)
            if prev:
                self.note.setText(f"A previous check was interrupted ({len(prev)} apps have results). "
                                  "Click Start to continue where it stopped.")

        # ---- state
        def set_idle(self, idle):
            self.running = not idle
            self.b_start.setEnabled(idle)
            self.b_stop.setEnabled(not idle)
            for wdg in (self.scope, self.text, self.chk_probe, self.chk_peek, self.workers):
                wdg.setEnabled(idle)
            has = bool(self.results)
            for b in (self.b_save, self.b_copy, self.b_apply):
                b.setEnabled(idle and has)

        def reload_apps(self):
            try:
                apps[:] = load_apps(cpath)[1]
            except Exception as e:
                log(f"could not re-read the catalog: {e}")

        def chosen(self):
            s = self.scope.currentData()
            return select_apps(apps, None, self.text.text().strip(), s[4:] if s.startswith("cat:") else "",
                               s == "notpassed")

        # ---- table
        def hidden(self, name):
            k = self.show_combo.currentData()
            r = self.results.get(name)
            return k != "all" and (r is None or r["status"] != k)

        def fill_row(self, row, name):
            a = self.app_by_name.get(name, {})
            r = self.results.get(name)
            vals = [name, a.get("category", ""), LABEL[r["status"]] if r else "…", r["latest"] if r else "",
                    r["file"] if r else "", human(r["size"]) if r and r.get("size") else "", r["note"] if r else ""]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 6 and r:
                    it.setToolTip(r["note"])
                if c == 2 and r:
                    it.setForeground(QBrush(QColor(COL[r["status"]])))
                self.table.setItem(row, c, it)
            self.table.setRowHidden(row, self.hidden(name))

        def rebuild(self, names):
            self.table.setUpdatesEnabled(False)
            self.table.setRowCount(len(names))
            self.rows = {}
            for r, n in enumerate(names):
                self.rows[n] = r
                self.fill_row(r, n)
            self.table.setUpdatesEnabled(True)

        def sorted_names(self):
            col, asc = self.sort_col, self.sort_asc

            def key(n):
                r = self.results.get(n)
                a = self.app_by_name.get(n, {})
                nm = n.casefold()
                if col == 0:
                    return (nm,)
                if col == 1:
                    return (a.get("category", "").casefold(), nm)
                if r is None:
                    return (1, nm)
                if col == 2:
                    return (0, ORDER[r["status"]], nm)
                if col == 3:
                    return (0, r["latest"].casefold(), nm)
                if col == 4:
                    return (0, r["file"].casefold(), nm)
                if col == 5:
                    return (0, int(r.get("size") or 0), nm)
                return (0, r["note"].casefold(), nm)

            return sorted(self.rows, key=key, reverse=not asc)

        def resort(self):
            self.table.horizontalHeader().setSortIndicator(self.sort_col, Qt.AscendingOrder if self.sort_asc else Qt.DescendingOrder)
            self.rebuild(self.sorted_names())

        def on_header(self, col):
            if self.running or not self.rows:
                return
            self.sort_asc = (not self.sort_asc) if col == self.sort_col else True
            self.sort_col = col
            self.resort()

        def apply_filter(self):
            for name, row in self.rows.items():
                self.table.setRowHidden(row, self.hidden(name))

        # ---- run
        def start(self):
            if self.running:
                return
            self.reload_apps()
            todo = self.chosen()
            if not todo:
                QMessageBox.information(self, "Catalog Check", "No apps match.")
                return
            names = {a["name"] for a in todo}
            prev = {n: r for n, r in load_progress(progress_path).items() if n in names}
            if prev and len(prev) < len(todo):
                ans = QMessageBox.question(
                    self, "Continue the interrupted check?",
                    f"A previous check was interrupted: {len(prev)} of these {len(todo)} apps already have a result.\n\n"
                    "Yes = continue with the remaining apps\nNo = start over\nCancel = do nothing",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                if ans == QMessageBox.Cancel:
                    return
                if ans == QMessageBox.No:
                    prev = {}
            elif prev:
                prev = {}
            if not prev:
                try:
                    progress_path.unlink()
                except OSError:
                    pass
            crash_file = out_dir / "catalog_check_crash_worker.log"
            if crash_file.exists():                          # a file here must always belong to the latest run
                try:
                    crash_file.replace(crash_file.with_name(crash_file.name + ".prev"))
                except OSError:
                    pass
            self.results = dict(prev)
            self.app_by_name = {a["name"]: a for a in todo}
            self.rows = {}
            self.rebuild([a["name"] for a in todo])
            self.rebuild(self.sorted_names() if prev else [a["name"] for a in todo])
            remaining = [a for a in todo if a["name"] not in prev]
            self.progress.setRange(0, len(todo))
            self.progress.setValue(len(prev))
            self.update_counts()
            self.done_flag = False
            self.stopping = False
            self.helper_stopped = False
            self.errlines = []
            opts = dict(workers=self.workers.value(), probe=self.chk_probe.isChecked(), peek=self.chk_peek.isChecked())
            log(f"START: {len(remaining)} apps to check ({len(prev)} resumed), {opts['workers']} workers, "
                f"probe={opts['probe']} peek={opts['peek']}")
            try:
                self.spawn(remaining, opts)
            except Exception as e:
                log(f"could not start the helper process: {e}\n{traceback.format_exc()}")
                QMessageBox.critical(self, "Catalog Check", f"Could not start the checking process:\n\n{e}\n\nDetails: {out_dir / LOG_NAME}")
                return
            self.set_idle(False)
            self.note.setText("Checking … (no file is downloaded: only the first bytes of each file, and the end of each zip, are requested)")

        def spawn(self, todo, o):
            fd, apps_file = tempfile.mkstemp(prefix="cc_apps_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(todo, f)
            self.apps_file = apps_file
            cmd = [py_executable(), str(Path(__file__).resolve()), "--worker", "--apps-file", apps_file,
                   "--log-dir", str(out_dir), "--workers", str(o["workers"])]
            if args.tracker_dir:
                cmd += ["--tracker-dir", str(args.tracker_dir)]
            if not o["probe"]:
                cmd.append("--no-probe")
            if not o["peek"]:
                cmd.append("--no-peek")
            if args.backoff is not None:
                cmd += ["--backoff", str(args.backoff)]
            if args.skip_selftest:
                cmd.append("--skip-selftest")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            flags = 0x08000000 if sys.platform.startswith("win") else 0           # CREATE_NO_WINDOW
            log("helper process: " + " ".join(cmd))
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, creationflags=flags, cwd=str(HERE))
            self.proc = proc
            errlines = self.errlines

            def read_out():
                try:
                    for raw in iter(proc.stdout.readline, b""):
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except ValueError:
                            errlines.append("(stdout) " + line)
                            log("helper stdout (not JSON): " + line[:300])
                            continue
                        if isinstance(d, dict):
                            self.bridge.msg.emit(d)
                finally:
                    code = proc.wait()
                    self.bridge.msg.emit({"t": "exit", "code": code})

            def read_err():
                for raw in iter(proc.stderr.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line:
                        errlines.append(line)
                        log("helper: " + line[:400])

            threading.Thread(target=read_out, daemon=True, name="cc-read-out").start()
            threading.Thread(target=read_err, daemon=True, name="cc-read-err").start()

        def stop_run(self):
            if not self.running:
                return
            self.stopping = True
            self.note.setText("Stopping … finishing the checks already running")
            log("STOP requested")
            try:
                self.proc.stdin.write(b"stop\n")
                self.proc.stdin.flush()
            except Exception:
                pass
            self.kill_timer.start(30000)

        def kill_worker(self):
            if self.proc and self.proc.poll() is None:
                log("helper did not stop in 30 s: killed")
                try:
                    self.proc.kill()
                except Exception:
                    pass

        # ---- messages from the helper process
        def on_msg(self, d):
            try:
                self.handle_msg(d)
            except Exception:
                _LOG["errors"] += 1
                log(f"internal error while handling a message from the helper ({d.get('t')}):\n{traceback.format_exc()}")
                self.note.setText("An internal error was logged (catalog_check.log). The check continues.")

        def handle_msg(self, d):
            t = d.get("t")
            if t == "result":
                self.on_result(d["r"])
            elif t == "progress":
                self.progress.setValue(max(self.progress.value(), len(self.results)))
            elif t == "selftest":
                log(f"self-test: {d.get('ok')} {d.get('why', '')}")
                if not d.get("ok"):
                    self.fail_selftest(d.get("why", ""))
            elif t == "done":
                self.done_flag = True
                self.helper_stopped = bool(d.get("stopped"))
                log(f"helper finished: {d.get('summary')} stopped={d.get('stopped')}")
            elif t == "fatal":
                self.errlines.append("FATAL: " + str(d.get("error")) + ("\n" + d["trace"] if d.get("trace") else ""))
                log("helper FATAL: " + str(d.get("error")) + "\n" + str(d.get("trace", "")))
            elif t == "exit":
                self.on_exit(d.get("code"))

        def fail_selftest(self, why):
            self.selftest_failed = True
            QMessageBox.critical(
                self, "Doesn't fit your tracker",
                "This catalog_check.py does not fit your release_tracker.py:\n\n" + why +
                "\n\nSend me both files and I will adjust it.")

        def on_result(self, r):
            name = r["name"]
            self.results[name] = r
            try:
                with open(progress_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(r) + "\n")
            except OSError:
                pass
            log(f"{r['status']:7} {name}: {(r['note'] or r['latest'])[:200]}")
            row = self.rows.get(name)
            if row is not None:
                self.fill_row(row, name)
            self.progress.setValue(len(self.results))
            self.update_counts()

        def on_exit(self, code):
            self.kill_timer.stop()
            try:
                os.unlink(self.apps_file)
            except Exception:
                pass
            log(f"helper process ended with exit code {code} (finished normally: {self.done_flag})")
            if getattr(self, "selftest_failed", False):
                self.selftest_failed = False
                self.set_idle(True)
                self.note.setText("SELF-TEST FAILED — see the message box")
                return
            if self.done_flag:
                self.finish_ok()
            else:
                self.finish_crashed(code)

        def finish_ok(self):
            self.set_idle(True)
            self.sort_col, self.sort_asc = 2, True
            self.resort()
            self.update_counts()
            c = summarize(list(self.results.values()))
            files = []
            try:
                files = save_reports(self.all_results(), out_dir)
                if not self.helper_stopped:                      # a stopped run keeps its progress: Start can continue it
                    try:
                        progress_path.unlink()
                    except OSError:
                        pass
            except Exception as e:
                log(f"could not save the reports: {e}\n{traceback.format_exc()}")
            summary = (f"{c['PASS']} passed, {c['WARN']} warnings, {c['FAIL']} failed, {c['LIMITED']} limited"
                       + (f", {c['SKIP']} skipped" if c["SKIP"] else ""))
            log(f"FINISHED: {summary}; reports: {', '.join(str(f) for f in files)}")
            self.note.setText("Done. Problems are listed first. The report was saved automatically.")
            if files:
                text = (f"{summary}.\n\nThe report was saved in:\n{out_dir}\n\nSend me catalog_check_summary.txt "
                        "(or click 'Copy failures' and paste it).")
            else:
                text = f"{summary}.\n\nThe report could not be saved automatically — click 'Save report…'."
            if self.helper_stopped:
                text = ("Stopped.  " + text + "\n\nClick Start later to continue with the apps that were not checked yet.")
            QMessageBox.information(self, "Catalog Check finished", text)

        def finish_crashed(self, code):
            self.set_idle(True)
            self.resort()
            self.update_counts()
            if self.stopping:                              # we killed it ourselves after a Stop
                self.note.setText("Stopped. Results so far are kept.")
                return
            dump = last_crash_dump(out_dir / "catalog_check_crash_worker.log")
            tail = "\n".join(self.errlines[-12:])
            if dump:
                tail = (tail + "\n\n" if tail else "") + "Python's crash report:\n" + dump
            log(f"HELPER PROCESS DIED (exit code {code}) after {len(self.results)} results.\n{tail}")
            self.note.setText(f"The checking process stopped unexpectedly (exit code {code}) — results so far are kept. Click Start to continue.")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("The check stopped unexpectedly")
            box.setText(f"The checking process stopped unexpectedly (exit code {code}) after {len(self.results)} apps.\n\n"
                        "Nothing is lost: the results so far are shown and saved. Click Start to continue where it stopped. "
                        "If it happens again, set 'workers' to 1.\n\n"
                        f"Details were written to:\n{out_dir / LOG_NAME}")
            if tail:
                box.setDetailedText(tail)
            box.exec()

        def update_counts(self):
            c = summarize(list(self.results.values()))
            self.count.setText(f"✅ {c['PASS']}   ⚠ {c['WARN']}   ❌ {c['FAIL']}   ⏳ {c['LIMITED']}")
            self.b_save.setEnabled(not self.running and bool(self.results))
            self.b_copy.setEnabled(not self.running and bool(self.results))
            self.b_apply.setEnabled(not self.running and bool(self.results))

        def show_detail(self):
            rows = {i.row() for i in self.table.selectedIndexes()}
            if not rows:
                return
            it = self.table.item(min(rows), 0)
            name = it.text() if it else ""
            r = self.results.get(name)
            if not r:
                self.detail.setPlainText(name)
                return
            lines = [f"{r['name']}   —   {LABEL[r['status']]}   (source: {r['source']}, {r['seconds']} s)",
                     f"latest: {r['latest'] or '-'}    file: {r['file'] or '-'}    size: {human(r['size'])}"]
            if r.get("run_target"):
                lines.append(f"Run button would start: {r['run_target']}")
            if r.get("pattern_suggested"):
                lines.append(f"suggested pattern: {r['pattern_suggested']}")
            lines.append(r["note"] or "nothing to report")
            self.detail.setPlainText("\n".join(lines))

        # ---- output
        def all_results(self):
            order = {a["name"]: i for i, a in enumerate(apps)}
            return sorted(self.results.values(), key=lambda r: order.get(r["name"], 0))

        def save(self):
            d = QFileDialog.getExistingDirectory(self, "Save the report in …", str(out_dir))
            if d:
                files = save_reports(self.all_results(), Path(d))
                QMessageBox.information(self, "Saved", "Saved:\n" + "\n".join(str(f) for f in files) +
                                        "\n\nSend me catalog_check_summary.txt.")

        def copy(self):
            QGuiApplication.clipboard().setText(failures_text(self.all_results()))
            self.note.setText("Copied — paste it into the chat.")

        def open_log(self):
            try:
                open_path(out_dir / LOG_NAME if (out_dir / LOG_NAME).exists() else out_dir)
            except Exception as e:
                QMessageBox.information(self, "Log", f"The log is here:\n{out_dir / LOG_NAME}\n\n({e})")

        def apply(self):
            res = self.all_results()
            n = sum(1 for r in res if r["status"] in ("PASS", "WARN", "FAIL"))
            if QMessageBox.question(self, "Apply results",
                                    f"Write the results of {n} apps into\n{cpath}\n\n"
                                    "(verified, last_check, check_result, check_note). A backup is made first.") != QMessageBox.Yes:
                return
            info = apply_results(cpath, res)
            self.reload_apps()
            log(f"results applied to the catalog: {info}")
            QMessageBox.information(self, "Catalog updated",
                                    f"{info['changed']} entries updated.\nBackup: {info['backup']}\n\n"
                                    "In the tracker, click 'Reload catalog' on the App Catalog tab.")

        def closeEvent(self, e):
            if self.running:
                if QMessageBox.question(self, "A check is running",
                                        "A check is still running. Stop it and close the window?\n\n"
                                        "(Results so far are saved; the next start can continue.)") != QMessageBox.Yes:
                    e.ignore()
                    return
                self.stopping = True
                try:
                    self.proc.stdin.write(b"stop\n")
                    self.proc.stdin.flush()
                except Exception:
                    pass
                try:
                    self.proc.kill()
                except Exception:
                    pass
            log("window closed")
            e.accept()

    w = Win()
    w.show()
    if args.autostart:
        QTimer.singleShot(200, w.start)
    if args.no_exec:
        args.window = w                    # (used by the tests)
        return 0
    code = app.exec()
    log(f"event loop ended with exit code {code}")
    finish_logging()
    return code


def build_parser():
    p = argparse.ArgumentParser(description="Checks which entries of app_catalog.json really work (nothing is downloaded).")
    p.add_argument("--cli", action="store_true", help="no window: print the results")
    p.add_argument("--only", nargs="+", metavar="NAME", help="check only these apps")
    p.add_argument("--text", default="", help="only apps whose name contains this")
    p.add_argument("--category", default="", help="only this category")
    p.add_argument("--not-passed", action="store_true", help="only apps that have not passed a previous check")
    p.add_argument("--no-probe", action="store_true", help="don't ask the server for the first bytes of each file")
    p.add_argument("--no-peek", action="store_true", help="don't read the file list of zip files")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--apply", action="store_true", help="write the results into the catalog (backup first)")
    p.add_argument("--catalog", help="path of app_catalog.json (default: the tracker's)")
    p.add_argument("--out", help="folder for the report files and the log (default: next to the catalog)")
    p.add_argument("--tracker-dir", help="folder that contains release_tracker.py (default: this folder)")
    p.add_argument("--selftest", action="store_true", help="only check that this tool fits your release_tracker.py")
    p.add_argument("--skip-selftest", action="store_true")
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--apps-file", help=argparse.SUPPRESS)
    p.add_argument("--log-dir", help=argparse.SUPPRESS)
    p.add_argument("--backoff", type=float, default=None, help=argparse.SUPPRESS)
    p.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-exec", action="store_true", help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.worker:
        code = worker_main(args)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(code)                      # leave without interpreter / Qt tear-down (nothing left to save)
    if args.selftest:
        rt = load_tracker(args.tracker_dir)
        ensure_qt_app()
        ok, why = selftest(rt)
        print("self-test: OK — this tool fits your release_tracker.py" if ok else "self-test FAILED:\n  " + why)
        return 0 if ok else 2
    if args.cli:
        return run_cli(args)
    try:
        return run_gui(args)
    except ImportError as e:
        print(f"The window needs PySide6 ({e}). Use  python catalog_check.py --cli  instead.")
        return 3


if __name__ == "__main__":
    sys.exit(main())
