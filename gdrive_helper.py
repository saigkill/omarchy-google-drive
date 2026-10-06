#!/usr/bin/env python3
"""rclone driver for the Omarchy Google Drive panel.

The panel never talks to Google itself and never sees a credential. The OAuth
token lives in rclone's own config file, which rclone reads and refreshes on
its own; this script only ever asks rclone whether a remote of the expected
name and type exists (`rclone listremotes --long`, which prints names and
types and nothing else) and never runs `rclone config dump` or `config show`.

`rclone config create` prints the finished remote, token included, on stdout.
Its output is therefore swallowed here and never relayed to the panel.

Everything the panel decides on goes through this script as well: the list of
folder pairs, the per-pair bisync working directory, the lock that stops two
syncs of one pair overlapping, and the last result of every pair. All of it
lives in a 0700 state directory.

The one thing that has to cross a process boundary in the clear is the local
folder path, because rclone and inotifywait take it as an argument. A path is
not a credential, but it is the reason the paths a user adds travel *into* this
script through GDRIVE_PAIR_JSON on the environment, not argv.
"""

import ctypes
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid

STATE_VERSION = 1

# A remote name goes into argv and into "<name>:<path>", so it is held to the
# characters rclone itself documents for remote names, minus the space.
REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
PAIR_ID_RE = re.compile(r"^[a-f0-9]{12}$")

# bisync renames the loser of a conflict to "<name>.conflict<N>".
CONFLICT_RE = re.compile(r"\.conflict\d+$")
MAX_CONFLICTS_LISTED = 50
MAX_CONFLICT_SCAN_ENTRIES = 200000

LOGIN_TIMEOUT = 300
SYNC_TIMEOUT = 6 * 60 * 60
# A sync reports its progress at most this often (seconds), see ProgressTracker.
PROGRESS_INTERVAL = 1.0
# How many recently uploaded files a result remembers.
RECENT_UPLOADS = 5

# Events below this many seconds apart are reported as one change. The panel
# debounces again on top; this only keeps a `git checkout` from writing a
# thousand lines into the QML side.
WATCH_COALESCE_SECONDS = 1.0

# Files rclone and editors write while they work. A change to one of them is
# not a change the user made, and syncing it would only upload garbage.
DEFAULT_FILTERS = """\
# Filters for every Google Drive pair, in rclone filter syntax.
# https://rclone.org/filtering/
# bisync remembers a hash of this file; after editing it the next sync of
# every pair reports "needs resync". Press Resync in the panel once per pair.
- *.partial
- .~lock.*#
- ~$*
- *.swp
- *.swx
- .DS_Store
- Thumbs.db
- .Trash-*/**
"""

IGNORED_WATCH_RE = re.compile(r"(\.partial$|/\.~lock\..*#$|/~\$|\.sw[px]$|/\.Trash-)")

EXIT_USAGE = 2
EXIT_STATE = 4
EXIT_REFUSED = 5
EXIT_RCLONE = 6


class HelperError(Exception):
    def __init__(self, message, status=EXIT_REFUSED):
        super().__init__(message)
        self.message = message
        self.status = status


# ------------------------------------------------------------------ state dir


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "omarchy-google-drive")


def pairs_path():
    return os.path.join(state_dir(), "pairs.json")


def results_path():
    return os.path.join(state_dir(), "results.json")


def filters_path():
    return os.path.join(state_dir(), "filters.txt")


def workdir_for(pair_id):
    return os.path.join(state_dir(), "bisync", pair_id)


def lock_path(pair_id):
    return os.path.join(state_dir(), "locks", pair_id + ".lock")


def ensure_private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise HelperError("refusing to use " + path + ": not a plain directory", EXIT_STATE)
    if info.st_uid != os.getuid():
        raise HelperError("refusing to use " + path + ": owned by another user", EXIT_STATE)
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)


def ensure_state_dir():
    ensure_private_dir(state_dir())
    ensure_private_dir(os.path.join(state_dir(), "bisync"))
    ensure_private_dir(os.path.join(state_dir(), "locks"))
    if not os.path.lexists(filters_path()):
        write_private(filters_path(), DEFAULT_FILTERS)


def read_json(path, fallback):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return fallback
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HelperError("refusing to read " + path + ": not a regular file", EXIT_STATE)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return fallback
    return data if isinstance(data, type(fallback)) else fallback


def write_private(path, text):
    """Atomic write with 0600 from the start: the temporary file is created
    with O_EXCL and the right mode, so there is no moment at which another
    user could open it, and a symlink planted at the target is replaced
    rather than followed."""
    directory = os.path.dirname(path)
    temp = os.path.join(directory, ".%s.%s.tmp" % (os.path.basename(path), uuid.uuid4().hex))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        with _suppress():
            os.unlink(temp)
        raise


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return exc[0] is not None and issubclass(exc[0], OSError)


def load_pairs():
    data = read_json(pairs_path(), {})
    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not isinstance(pairs, list):
        return []
    clean = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        pair_id = str(pair.get("id", ""))
        if not PAIR_ID_RE.match(pair_id):
            continue
        clean.append({
            "id": pair_id,
            "local": str(pair.get("local", "")),
            "remote": str(pair.get("remote", "")),
            "enabled": pair.get("enabled") is not False,
        })
    return clean


def save_pairs(pairs):
    ensure_state_dir()
    write_private(pairs_path(), json.dumps({"version": STATE_VERSION, "pairs": pairs}, indent=2) + "\n")


def load_results():
    data = read_json(results_path(), {})
    return data if isinstance(data, dict) else {}


def save_result(pair_id, result):
    results = load_results()
    results[pair_id] = result
    write_private(results_path(), json.dumps(results, indent=2) + "\n")


def find_pair(pair_id):
    if not PAIR_ID_RE.match(str(pair_id)):
        raise HelperError("invalid pair id", EXIT_USAGE)
    for pair in load_pairs():
        if pair["id"] == pair_id:
            return pair
    raise HelperError("unknown pair", EXIT_REFUSED)


# ---------------------------------------------------------------- validation


def check_remote_name(name):
    if not REMOTE_NAME_RE.match(str(name)):
        raise HelperError("invalid remote name", EXIT_USAGE)
    return name


def is_within(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def normalize_local(raw):
    text = str(raw or "").strip()
    if text.startswith("file://"):
        text = text[len("file://"):]
    if text == "~" or text.startswith("~/"):
        text = os.path.expanduser(text)
    if not os.path.isabs(text):
        raise HelperError("The local folder must be an absolute path")
    real = os.path.realpath(text)
    if not os.path.isdir(real):
        raise HelperError("The local folder does not exist")
    home = os.path.realpath(os.path.expanduser("~"))
    if real == "/" or real == home:
        raise HelperError("Refusing to sync / or the whole home directory")
    if not is_within(real, home):
        raise HelperError("The local folder must be inside your home directory")
    # Quickshell watches the plugin directory and reloads on every write, and
    # the state directory holds the bisync listings themselves. Syncing either
    # would feed the sync its own output.
    forbidden = [
        os.path.realpath(os.path.join(home, ".config", "omarchy")),
        os.path.realpath(state_dir()),
        os.path.realpath(os.path.join(home, ".config", "rclone")),
        os.path.realpath(os.path.join(home, ".cache", "rclone")),
    ]
    for path in forbidden:
        if is_within(real, path) or is_within(path, real):
            raise HelperError("Refusing to sync a folder that contains plugin, rclone or sync state")
    return real


def normalize_remote_path(raw):
    """The Drive side is a path *inside* the configured remote. A colon would
    let it name a different remote (or a local path, via ":local:"), and ".."
    would climb out of the folder the user picked."""
    text = str(raw or "").strip().replace("\\", "/")
    if ":" in text:
        raise HelperError("The Drive folder must not contain ':'")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise HelperError("The Drive folder must not contain '..'")
    if any(ord(ch) < 32 for ch in text):
        raise HelperError("The Drive folder contains control characters")
    return "/".join(parts)


def overlaps(a, b):
    return is_within(a, b) or is_within(b, a)


def remote_overlaps(a, b):
    if a == "" or b == "":
        return True
    return overlaps("/" + a, "/" + b)


# -------------------------------------------------------------------- rclone


def rclone_binary():
    return shutil.which("rclone")


def rclone_version():
    binary = rclone_binary()
    if not binary:
        return ""
    try:
        out = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    first = out.splitlines()[0] if out else ""
    return first.replace("rclone", "").strip()


def remote_type(name):
    """Returns the backend type of a configured remote, "" if it does not
    exist. `listremotes --long` prints "<name>: <type>" and nothing secret."""
    binary = rclone_binary()
    if not binary:
        return ""
    try:
        out = subprocess.run([binary, "listremotes", "--long"], capture_output=True, text=True,
                             timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        head, sep, kind = line.partition(":")
        if sep and head.strip() == name:
            return kind.strip()
    return ""


def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(text))


def parse_bisync_log(text):
    """Reads rclone's --use-json-log stream. Lines that are not JSON (rclone
    prints a few before the logger is up) are kept as plain messages."""
    errors = []
    notices = []
    stats = {}
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = {"level": "error" if "ERROR" in line else "notice", "msg": line}
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("stats"), dict):
            stats = entry["stats"]
            continue
        message = strip_ansi(entry.get("msg", "")).strip()
        if not message:
            continue
        if entry.get("level") in ("error", "critical", "alert", "emergency"):
            errors.append(message)
        else:
            notices.append(message)
    everything = " ".join(errors + notices).lower()
    return {
        "errors": errors,
        "stats": stats,
        # Not just "--resync": with --resilient, bisync also says an error "is
        # retryable without --resync", which is the opposite.
        "needsResync": "must run --resync" in everything,
        "safetyAbort": "safety abort" in everything or "too many deletes" in everything,
        # bisync refuses to mirror a side that has become completely empty,
        # since that is what an unmounted disk or an emptied Drive looks like.
        "emptySide": "empty current path" in everything,
        "locked": "prior lock file found" in everything,
    }


def classify(exit_code, parsed, conflicts):
    if exit_code == 0:
        return "conflict" if conflicts else "ok"
    if parsed["locked"]:
        return "busy"
    if parsed["needsResync"]:
        return "needs-resync"
    if parsed["safetyAbort"]:
        return "safety-abort"
    return "error"


def summary_message(state, parsed, conflicts):
    if state == "ok":
        return "Up to date"
    if state == "conflict":
        return "%d conflict file%s" % (len(conflicts), "" if len(conflicts) == 1 else "s")
    if state == "busy":
        return "Another sync of this folder is still running"
    if state == "needs-resync":
        return "bisync lost track of this folder. Press Resync to merge both sides again"
    if state == "safety-abort":
        return "Stopped: too many files would be deleted. Check both sides, then Sync anyway"
    if parsed.get("emptySide"):
        return ("Stopped: one side is completely empty. If that is intended, empty the other side by hand;"
                " otherwise check that the folder is mounted and Drive is reachable")
    for message in reversed(parsed["errors"]):
        if message.startswith("Bisync aborted") or message.startswith("Failed to bisync"):
            continue
        return message[:300]
    return "Sync failed"


class ProgressTracker:
    """Follows bisync's log while it runs. Path1 is the local folder and Path2
    Drive, so copies in the "to Path2" phase are uploads. A normal sync
    announces every upload first ("Queue copy to Path2"); a resync does not,
    so its upload total stays unknown."""

    QUEUED_UPLOAD = re.compile(r"^- Path1\s+Queue copy to Path2\s+- ")
    PHASE = re.compile(r"^- Path\d\s+(Do queued copies to|Resync is copying files to)\s+- Path(\d)$")

    def __init__(self):
        self.phase = "checking"
        self.queued_uploads = 0
        self.uploaded = 0
        self.recent = []
        self.eta = None
        # The "to Path1" phase starts long before the first download: bisync
        # first compares the listings, and on a big folder with a rate-limited
        # Drive client that alone takes hours. Only report "downloading" once
        # something is actually in flight or done.
        self.downloaded = 0
        self.transferring = False

    def feed(self, line):
        """Returns True when something the user sees has changed."""
        try:
            entry = json.loads(line)
        except ValueError:
            return False
        if not isinstance(entry, dict):
            return False
        stats = entry.get("stats")
        if isinstance(stats, dict):
            eta = stats.get("eta")
            eta = int(eta) if isinstance(eta, (int, float)) and eta >= 0 else None
            transferring = bool(stats.get("transferring"))
            changed = eta != self.eta or transferring != self.transferring
            self.eta = eta
            self.transferring = transferring
            return changed
        message = strip_ansi(entry.get("msg", "")).strip()
        if self.QUEUED_UPLOAD.match(message):
            self.queued_uploads += 1
            return True
        phase = self.PHASE.match(message)
        if phase:
            self.phase = "upload" if phase.group(2) == "2" else "download"
            return True
        if message.startswith("Copied") and self.phase == "upload" and entry.get("object"):
            self.uploaded += 1
            self.recent = ([str(entry["object"])] + self.recent)[:RECENT_UPLOADS]
            return True
        if message.startswith("Copied") and self.phase == "download" and entry.get("object"):
            self.downloaded += 1
            return True
        return False

    def snapshot(self):
        return {"progress": {
            "phase": self.phase,
            "uploaded": self.uploaded,
            "uploadsTotal": self.queued_uploads if self.queued_uploads > 0 else None,
            "eta": self.eta,
            "downloaded": self.downloaded,
            "transferring": self.transferring,
            "recentUploads": self.recent,
        }}


def merge_recent(new, previous):
    """Newest first; a sync that uploaded nothing keeps the earlier list."""
    merged = []
    for name in list(new) + list(previous or []):
        if isinstance(name, str) and name not in merged:
            merged.append(name)
    return merged[:RECENT_UPLOADS]


def scan_conflicts(root):
    found = []
    seen = 0
    for directory, dirs, files in os.walk(root):
        for name in files:
            seen += 1
            if CONFLICT_RE.search(name):
                found.append(os.path.relpath(os.path.join(directory, name), root))
        if seen > MAX_CONFLICT_SCAN_ENTRIES:
            break
    found.sort()
    return found


def bisync_command(remote, pair, resync=False, force=False):
    command = [
        rclone_binary() or "rclone", "bisync",
        pair["local"], "%s:%s" % (remote, pair["remote"]),
        "--workdir", workdir_for(pair["id"]),
        "--filters-file", filters_path(),
        "--create-empty-src-dirs",
        "--resilient", "--recover",
        "--max-lock", "2m",
        "--conflict-resolve", "none",
        "--conflict-loser", "num",
        "--conflict-suffix", "conflict",
        # Google Docs, Sheets and Slides have no file behind them. rclone can
        # export them, but bisync would then upload the export back as a new
        # .docx next to the original, so they stay on Drive only.
        "--drive-skip-gdocs",
        # No --fast-list: it lists the whole Drive tree and only then applies
        # the filters, so excluded build output on Drive (often the bulk of
        # the files) is fetched on every sync. Per-directory listing skips
        # excluded directories. With rclone's shared client id that runs into
        # Drive's per-minute quota; the README tells users to bring their own.
        "--use-json-log", "-v",
        # Stats every second carry the ETA for the progress lines.
        "--stats", "1s", "--stats-log-level", "NOTICE",
    ]
    if resync:
        # Union of both sides; for a file present on both, the newer copy
        # wins. Nothing is deleted on a resync, which is why it is the safe
        # answer to every "lost track" situation.
        command += ["--resync", "--resync-mode", "newer"]
    if force:
        command.append("--force")
    return command


def is_initialized(pair_id):
    try:
        return any(name.endswith(".lst") for name in os.listdir(workdir_for(pair_id)))
    except OSError:
        return False


# ------------------------------------------------------------ child handling


_children = []


def _set_pdeathsig():
    """The panel stops a helper with SIGTERM, but a shell reload can SIGKILL
    it, and then nothing would ever stop rclone or inotifywait. PR_SET_PDEATHSIG
    makes the kernel send the child SIGTERM when this process dies, however
    it dies."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def _terminate_children(signum, frame):
    for child in _children:
        with _suppress():
            child.terminate()
    sys.exit(128 + signum)


def spawn(command, **kwargs):
    child = subprocess.Popen(command, preexec_fn=_set_pdeathsig, **kwargs)
    _children.append(child)
    return child


# ------------------------------------------------------------------ commands


def emit(data):
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def cmd_status(args):
    if len(args) != 1:
        raise HelperError("usage: status <remote>", EXIT_USAGE)
    remote = check_remote_name(args[0])
    ensure_state_dir()
    version = rclone_version()
    kind = remote_type(remote) if version else ""
    results = load_results()
    pairs = []
    for pair in load_pairs():
        entry = dict(pair)
        entry["initialized"] = is_initialized(pair["id"])
        entry["exists"] = os.path.isdir(pair["local"])
        entry["result"] = results.get(pair["id"]) if isinstance(results.get(pair["id"]), dict) else None
        pairs.append(entry)
    emit({
        "rclone": {"installed": bool(version), "version": version},
        "remote": {"name": remote, "type": kind, "configured": kind == "drive"},
        "filtersFile": filters_path(),
        "pairs": pairs,
    })


def cmd_add_pair(args):
    if args:
        raise HelperError("usage: add-pair (reads GDRIVE_PAIR_JSON)", EXIT_USAGE)
    try:
        request = json.loads(os.environ.get("GDRIVE_PAIR_JSON", ""))
    except ValueError:
        raise HelperError("GDRIVE_PAIR_JSON is not valid JSON", EXIT_USAGE)
    if not isinstance(request, dict):
        raise HelperError("GDRIVE_PAIR_JSON must be an object", EXIT_USAGE)
    local = normalize_local(request.get("local"))
    remote_path = normalize_remote_path(request.get("remote"))
    pairs = load_pairs()
    for pair in pairs:
        if overlaps(local, pair["local"]):
            raise HelperError("This local folder overlaps " + pair["local"])
        if remote_overlaps(remote_path, pair["remote"]):
            raise HelperError("This Drive folder overlaps " + (pair["remote"] or "the Drive root"))
    pair = {"id": uuid.uuid4().hex[:12], "local": local, "remote": remote_path, "enabled": True}
    pairs.append(pair)
    save_pairs(pairs)
    emit(pair)


def cmd_remove_pair(args):
    if len(args) != 1:
        raise HelperError("usage: remove-pair <id>", EXIT_USAGE)
    pair = find_pair(args[0])
    ensure_state_dir()
    # Removing a pair never touches a single synced file, on either side. Only
    # the listings go, so adding the same folders again starts with a resync.
    shutil.rmtree(workdir_for(pair["id"]), ignore_errors=True)
    save_pairs([p for p in load_pairs() if p["id"] != pair["id"]])
    results = load_results()
    if pair["id"] in results:
        del results[pair["id"]]
        write_private(results_path(), json.dumps(results, indent=2) + "\n")
    emit({"ok": True})


def cmd_set_enabled(args):
    if len(args) != 2 or args[1] not in ("0", "1"):
        raise HelperError("usage: set-enabled <id> <0|1>", EXIT_USAGE)
    pair = find_pair(args[0])
    pairs = load_pairs()
    for entry in pairs:
        if entry["id"] == pair["id"]:
            entry["enabled"] = args[1] == "1"
    save_pairs(pairs)
    emit({"ok": True})


def cmd_sync(args):
    flags = [a for a in args if a.startswith("--")]
    rest = [a for a in args if not a.startswith("--")]
    if len(rest) != 2 or any(f not in ("--resync", "--force") for f in flags):
        raise HelperError("usage: sync <remote> <id> [--resync] [--force]", EXIT_USAGE)
    remote = check_remote_name(rest[0])
    pair = find_pair(rest[1])
    ensure_state_dir()
    ensure_private_dir(workdir_for(pair["id"]))
    started = time.time()

    if not rclone_binary():
        raise HelperError("rclone is not installed", EXIT_RCLONE)
    if not os.path.isdir(pair["local"]):
        result = {"state": "error", "message": "The local folder is missing", "at": int(started)}
        save_result(pair["id"], result)
        emit(result)
        return

    lock = open(lock_path(pair["id"]), "a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Not saved: the sync that holds the lock writes the real result.
        emit({"state": "busy", "message": "Another sync of this folder is still running", "at": int(started)})
        return

    resync = "--resync" in flags or not is_initialized(pair["id"])
    command = bisync_command(remote, pair, resync=resync, force="--force" in flags)
    child = spawn(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  text=True)
    timed_out = threading.Event()

    def stop():
        timed_out.set()
        child.kill()

    timer = threading.Timer(SYNC_TIMEOUT, stop)
    timer.daemon = True
    timer.start()
    # Progress goes out as JSON lines while bisync runs; the result is the last
    # line, which is the one the plugin reads as the outcome.
    tracker = ProgressTracker()
    lines = []
    last_emit = 0.0
    try:
        for line in child.stdout:
            lines.append(line)
            if tracker.feed(line) and time.time() - last_emit >= PROGRESS_INTERVAL:
                emit(tracker.snapshot())
                last_emit = time.time()
        child.wait()
    finally:
        timer.cancel()
        child.stdout.close()
    code = -1 if timed_out.is_set() else child.returncode
    output = "".join(lines)

    parsed = parse_bisync_log(output)
    conflicts = scan_conflicts(pair["local"]) if code == 0 else []
    state = classify(code, parsed, conflicts)
    if code == -1:
        state = "error"
    stats = parsed["stats"]
    previous = load_results().get(pair["id"])
    result = {
        "state": state,
        "message": "Timed out" if code == -1 else summary_message(state, parsed, conflicts),
        "at": int(time.time()),
        "seconds": round(time.time() - started, 1),
        "resync": resync,
        "exitCode": code,
        "transfers": int(stats.get("transfers") or 0),
        "deletes": int(stats.get("deletes") or 0),
        "bytes": int(stats.get("bytes") or 0),
        "conflictCount": len(conflicts),
        "conflicts": conflicts[:MAX_CONFLICTS_LISTED],
        "uploaded": tracker.uploaded,
        "recentUploads": merge_recent(tracker.recent, previous.get("recentUploads") if isinstance(previous, dict) else None),
    }
    save_result(pair["id"], result)
    emit(result)


def cmd_login(args):
    if len(args) != 1:
        raise HelperError("usage: login <remote>", EXIT_USAGE)
    remote = check_remote_name(args[0])
    binary = rclone_binary()
    if not binary:
        raise HelperError("rclone is not installed", EXIT_RCLONE)
    kind = remote_type(remote)
    if kind == "drive":
        emit({"ok": True, "existing": True})
        return
    if kind:
        raise HelperError("A remote named '%s' already exists and is of type %s" % (remote, kind))
    # rclone opens the browser itself and waits for Google's redirect on
    # 127.0.0.1:53682. Its stdout carries the new token and is discarded.
    child = spawn([binary, "config", "create", remote, "drive", "scope=drive"],
                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, err = child.communicate(timeout=LOGIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        # A half-written remote would make the next attempt fail with "already
        # exists", so it is removed again.
        subprocess.run([binary, "config", "delete", remote], capture_output=True, timeout=15)
        raise HelperError("Login timed out", EXIT_RCLONE)
    if child.returncode != 0 or remote_type(remote) != "drive":
        subprocess.run([binary, "config", "delete", remote], capture_output=True, timeout=15)
        lines = [strip_ansi(l) for l in (err or "").splitlines() if "ERROR" in l or "Failed" in l]
        raise HelperError(lines[-1][:300] if lines else "Login failed", EXIT_RCLONE)
    emit({"ok": True, "existing": False})


def cmd_logout(args):
    if len(args) != 1:
        raise HelperError("usage: logout <remote>", EXIT_USAGE)
    remote = check_remote_name(args[0])
    binary = rclone_binary()
    if not binary:
        raise HelperError("rclone is not installed", EXIT_RCLONE)
    if remote_type(remote) not in ("", "drive"):
        raise HelperError("Refusing to delete '%s': it is not a Google Drive remote" % remote)
    ensure_state_dir()
    # The listings describe the account that is going away. Kept, they would
    # tell the first sync against another account that every file had been
    # deleted on Drive, and bisync would delete them locally to match. Without
    # them, the next sync is a resync, which only ever adds files.
    shutil.rmtree(os.path.join(state_dir(), "bisync"), ignore_errors=True)
    ensure_private_dir(os.path.join(state_dir(), "bisync"))
    with _suppress():
        os.unlink(results_path())
    if remote_type(remote) == "drive":
        subprocess.run([binary, "config", "delete", remote], capture_output=True, timeout=15)
    emit({"ok": True})


def cmd_watch(args):
    if len(args) != 1:
        raise HelperError("usage: watch <id>", EXIT_USAGE)
    pair = find_pair(args[0])
    binary = shutil.which("inotifywait")
    if not binary:
        raise HelperError("inotifywait is not installed (package inotify-tools)", EXIT_RCLONE)
    if not os.path.isdir(pair["local"]):
        raise HelperError("The local folder is missing")
    child = spawn([binary, "-m", "-r", "-q",
                   "-e", "close_write", "-e", "create", "-e", "delete", "-e", "move",
                   "--format", "%w%f", pair["local"]],
                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    last = 0.0
    for line in child.stdout:
        path = line.rstrip("\n")
        if IGNORED_WATCH_RE.search(path):
            continue
        now = time.monotonic()
        if now - last < WATCH_COALESCE_SECONDS:
            continue
        last = now
        # Only the fact that something changed goes to the panel, never the
        # name: it would end up in the shell's log on any QML warning.
        sys.stdout.write("change\n")
        sys.stdout.flush()
    child.wait()
    if child.returncode not in (0, None):
        message = strip_ansi(child.stderr.read() or "").strip().splitlines()
        raise HelperError(message[-1][:300] if message else "inotifywait stopped", EXIT_RCLONE)


COMMANDS = {
    "status": cmd_status,
    "add-pair": cmd_add_pair,
    "remove-pair": cmd_remove_pair,
    "set-enabled": cmd_set_enabled,
    "sync": cmd_sync,
    "login": cmd_login,
    "logout": cmd_logout,
    "watch": cmd_watch,
}


def main(argv):
    signal.signal(signal.SIGTERM, _terminate_children)
    signal.signal(signal.SIGINT, _terminate_children)
    if len(argv) < 2 or argv[1] not in COMMANDS:
        sys.stderr.write("usage: gdrive_helper.py {%s} ...\n" % ",".join(sorted(COMMANDS)))
        return EXIT_USAGE
    # No umask here: rclone inherits it, and every file it downloads would
    # come out 0600. The state files get their mode from write_private().
    try:
        COMMANDS[argv[1]](argv[2:])
    except HelperError as error:
        # A refused request exits non-zero with an empty stdout, so the panel
        # never mistakes an error for a result.
        sys.stderr.write(error.message + "\n")
        return error.status
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
