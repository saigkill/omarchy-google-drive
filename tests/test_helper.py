#!/usr/bin/env python3
"""Tests for gdrive_helper.py.

Three things matter most here: that a token never reaches the panel (rclone
prints it on `config create`), that a pair can only ever point at a folder the
user meant (no "/", no home, no plugin or rclone state, no second remote),
and that bisync's state is thrown away whenever it could make a sync delete
files: on logout and on removing a pair.

The end-to-end tests run the real rclone against a remote of type "local"
defined through RCLONE_CONFIG_* environment variables, so bisync itself is
exercised without a Google account.

Run with: python3 -m unittest discover -s tests -v
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HELPER = os.path.join(ROOT, "gdrive_helper.py")

sys.path.insert(0, ROOT)
import gdrive_helper as helper  # noqa: E402

TOKEN = "ya29.test-token-do-not-leak"
REAL_RCLONE = shutil.which("rclone")


class Sandbox(unittest.TestCase):
    """A throwaway HOME and XDG_STATE_HOME for every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.home)
        self.env = dict(os.environ)
        self.env.update({
            "HOME": self.home,
            "XDG_STATE_HOME": self.state,
            "RCLONE_CONFIG": os.path.join(self.home, ".config", "rclone", "rclone.conf"),
        })
        self._saved = {k: os.environ.get(k) for k in ("HOME", "XDG_STATE_HOME")}
        os.environ["HOME"] = self.home
        os.environ["XDG_STATE_HOME"] = self.state

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_helper(self, *args, env=None, timeout=120):
        merged = dict(self.env)
        merged.update(env or {})
        return subprocess.run([sys.executable, HELPER] + list(args), env=merged, capture_output=True,
                              text=True, timeout=timeout)

    def folder(self, *parts):
        path = os.path.join(self.home, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def add_pair(self, local, remote=""):
        return self.run_helper("add-pair", env={"GDRIVE_PAIR_JSON": json.dumps({"local": local, "remote": remote})})

    def fake_rclone(self, script):
        """Puts a fake `rclone` first on PATH. Used where the real one would
        need a network or a browser."""
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        path = os.path.join(bindir, "rclone")
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n" + textwrap.dedent(script))
        os.chmod(path, 0o755)
        self.env["PATH"] = bindir + os.pathsep + self.env.get("PATH", "")


# --------------------------------------------------------------- validation


class LocalPathValidation(Sandbox):
    def test_accepts_a_folder_in_home(self):
        path = self.folder("Drive")
        self.assertEqual(helper.normalize_local(path), os.path.realpath(path))

    def test_expands_tilde_and_file_urls(self):
        path = self.folder("Drive")
        self.assertEqual(helper.normalize_local("~/Drive"), os.path.realpath(path))
        self.assertEqual(helper.normalize_local("file://" + path), os.path.realpath(path))

    def test_refuses_dangerous_folders(self):
        self.folder(".config", "omarchy", "plugins")
        self.folder(".config", "rclone")
        os.makedirs(os.path.join(self.state, "omarchy-google-drive"), exist_ok=True)
        for bad in ["relative/path", "/", self.home, self.home + "/", os.path.join(self.home, "missing"),
                    tempfile.gettempdir(), os.path.join(self.home, ".config", "omarchy", "plugins"),
                    os.path.join(self.home, ".config"), os.path.join(self.home, ".config", "rclone")]:
            with self.assertRaises(helper.HelperError, msg=bad):
                helper.normalize_local(bad)

    def test_refuses_the_state_dir_when_it_lives_in_home(self):
        os.environ["XDG_STATE_HOME"] = os.path.join(self.home, ".local", "state")
        os.makedirs(helper.state_dir())
        with self.assertRaises(helper.HelperError):
            helper.normalize_local(os.path.join(self.home, ".local"))

    def test_symlink_out_of_home_is_resolved_and_refused(self):
        outside = tempfile.mkdtemp()
        try:
            os.symlink(outside, os.path.join(self.home, "link"))
            with self.assertRaises(helper.HelperError):
                helper.normalize_local(os.path.join(self.home, "link"))
        finally:
            shutil.rmtree(outside)


class RemotePathValidation(unittest.TestCase):
    def test_normalizes(self):
        self.assertEqual(helper.normalize_remote_path(" /Docs//Work/ "), "Docs/Work")
        self.assertEqual(helper.normalize_remote_path(""), "")
        self.assertEqual(helper.normalize_remote_path("./A/./B"), "A/B")

    def test_refuses_other_remotes_and_climbing(self):
        for bad in ["other:Docs", ":local:/etc", "Docs/../../x", "..", "a\nb"]:
            with self.assertRaises(helper.HelperError, msg=bad):
                helper.normalize_remote_path(bad)

    def test_overlap(self):
        self.assertTrue(helper.remote_overlaps("A", "A/B"))
        self.assertTrue(helper.remote_overlaps("", "A"))
        self.assertFalse(helper.remote_overlaps("A", "AB"))


class RemoteNames(unittest.TestCase):
    def test_names(self):
        self.assertEqual(helper.check_remote_name("gdrive"), "gdrive")
        for bad in ["", "-x", "a:b", "a b", "../x", "x" * 65, "--config=/tmp/x"]:
            with self.assertRaises(helper.HelperError, msg=bad):
                helper.check_remote_name(bad)


# ----------------------------------------------------------------- the pairs


class Pairs(Sandbox):
    def test_add_list_remove(self):
        local = self.folder("Drive")
        out = self.add_pair(local, "Docs")
        self.assertEqual(out.returncode, 0, out.stderr)
        pair = json.loads(out.stdout)
        self.assertRegex(pair["id"], r"^[a-f0-9]{12}$")
        self.assertEqual(pair["local"], os.path.realpath(local))
        self.assertEqual(pair["remote"], "Docs")

        self.fake_rclone('[ "$1" = version ] && echo "rclone v9.9.9"; [ "$1" = listremotes ] && echo "gdrive: drive"; exit 0\n')
        status = json.loads(self.run_helper("status", "gdrive").stdout)
        self.assertTrue(status["remote"]["configured"])
        self.assertEqual([p["id"] for p in status["pairs"]], [pair["id"]])
        self.assertFalse(status["pairs"][0]["initialized"])

        # Removing stops syncing; it never deletes a synced file.
        open(os.path.join(local, "keep.txt"), "w").close()
        os.makedirs(helper.workdir_for(pair["id"]), exist_ok=True)
        self.assertEqual(self.run_helper("remove-pair", pair["id"]).returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(local, "keep.txt")))
        self.assertFalse(os.path.exists(helper.workdir_for(pair["id"])))
        self.assertEqual(helper.load_pairs(), [])

    def test_overlapping_pairs_are_refused(self):
        self.assertEqual(self.add_pair(self.folder("Drive"), "A").returncode, 0)
        for local, remote in [(self.folder("Drive", "Sub"), "B"), (self.folder("Other"), "A/B"),
                              (self.folder("Other2"), "")]:
            out = self.add_pair(local, remote)
            self.assertNotEqual(out.returncode, 0, (local, remote))
            self.assertEqual(out.stdout, "")
            self.assertIn("overlaps", out.stderr)
        self.assertEqual(self.add_pair(self.folder("Other3"), "C").returncode, 0)

    def test_add_pair_takes_no_arguments(self):
        out = self.run_helper("add-pair", self.folder("Drive"))
        self.assertEqual(out.returncode, helper.EXIT_USAGE)
        self.assertEqual(out.stdout, "")

    def test_refusals_have_an_empty_stdout(self):
        for args in (["remove-pair", "../../etc"], ["sync", "gdrive", "0123456789ab"],
                     ["set-enabled", "0123456789ab", "2"], ["status", "a:b"], ["watch", "x"], ["nope"]):
            out = self.run_helper(*args)
            self.assertNotEqual(out.returncode, 0, args)
            self.assertEqual(out.stdout, "", args)

    def test_set_enabled(self):
        pair = json.loads(self.add_pair(self.folder("Drive")).stdout)
        self.assertEqual(self.run_helper("set-enabled", pair["id"], "0").returncode, 0)
        self.assertFalse(helper.load_pairs()[0]["enabled"])
        self.assertEqual(self.run_helper("set-enabled", pair["id"], "1").returncode, 0)
        self.assertTrue(helper.load_pairs()[0]["enabled"])


class StateFiles(Sandbox):
    def test_modes(self):
        self.add_pair(self.folder("Drive"))
        directory = helper.state_dir()
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        for name in ("pairs.json", "filters.txt"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode), 0o600, name)

    def test_symlinked_pairs_file_is_refused(self):
        helper.ensure_state_dir()
        target = os.path.join(self.tmp, "elsewhere.json")
        with open(target, "w") as handle:
            json.dump({"pairs": []}, handle)
        os.symlink(target, helper.pairs_path())
        out = self.add_pair(self.folder("Drive"))
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "")

    def test_symlinked_state_dir_is_refused(self):
        os.makedirs(self.state)
        os.symlink(self.tmp, helper.state_dir())
        with self.assertRaises(helper.HelperError):
            helper.ensure_state_dir()

    def test_write_replaces_a_planted_symlink(self):
        helper.ensure_state_dir()
        victim = os.path.join(self.tmp, "victim")
        with open(victim, "w") as handle:
            handle.write("untouched")
        os.symlink(victim, helper.results_path())
        with self.assertRaises(helper.HelperError):
            helper.load_results()
        helper.write_private(helper.results_path(), "{}")
        with open(victim) as handle:
            self.assertEqual(handle.read(), "untouched")


# ------------------------------------------------------------------ the token


class Login(Sandbox):
    def test_token_printed_by_rclone_never_reaches_stdout(self):
        marker = os.path.join(self.tmp, "created")
        self.fake_rclone(f"""
            case "$1" in
              version) echo "rclone v9.9.9" ;;
              listremotes) [ -e "{marker}" ] && echo "gdrive: drive" ;;
              config)
                touch "{marker}"
                echo '[gdrive]'
                echo 'type = drive'
                echo 'token = {{"access_token":"{TOKEN}"}}'
                echo "{TOKEN}" >&2
                ;;
            esac
            exit 0
        """)
        out = self.run_helper("login", "gdrive")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn(TOKEN, out.stdout)
        self.assertEqual(json.loads(out.stdout), {"ok": True, "existing": False})

    def test_failed_login_does_not_relay_rclone_output(self):
        self.fake_rclone(f"""
            case "$1" in
              version) echo "rclone v9.9.9" ;;
              config) [ "$2" = create ] && echo 'token = {TOKEN}' && echo "noise {TOKEN}" >&2 && exit 1 ;;
            esac
            exit 0
        """)
        out = self.run_helper("login", "gdrive")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "")
        self.assertNotIn(TOKEN, out.stderr)

    def test_a_remote_of_another_type_is_not_touched(self):
        log = os.path.join(self.tmp, "calls")
        self.fake_rclone(f"""
            echo "$@" >> "{log}"
            [ "$1" = listremotes ] && echo "gdrive: s3"
            exit 0
        """)
        for command in ("login", "logout"):
            out = self.run_helper(command, "gdrive")
            self.assertNotEqual(out.returncode, 0, command)
        with open(log) as handle:
            calls = handle.read()
        self.assertNotIn("config create", calls)
        self.assertNotIn("config delete", calls)

    def test_status_never_dumps_the_config(self):
        log = os.path.join(self.tmp, "calls")
        self.fake_rclone(f"""
            echo "$@" >> "{log}"
            [ "$1" = version ] && echo "rclone v9.9.9"
            [ "$1" = listremotes ] && echo "gdrive: drive"
            exit 0
        """)
        self.run_helper("status", "gdrive")
        with open(log) as handle:
            calls = handle.read()
        self.assertNotIn("dump", calls)
        self.assertNotIn("show", calls)

    def test_logout_forgets_every_listing(self):
        pair = json.loads(self.add_pair(self.folder("Drive")).stdout)
        workdir = helper.workdir_for(pair["id"])
        os.makedirs(workdir, exist_ok=True)
        open(os.path.join(workdir, "x.path1.lst"), "w").close()
        self.fake_rclone('[ "$1" = listremotes ] && echo "gdrive: drive"; exit 0\n')
        out = self.run_helper("logout", "gdrive")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(helper.is_initialized(pair["id"]))
        self.assertEqual(len(helper.load_pairs()), 1, "the pairs themselves stay")


# ------------------------------------------------------------- log parsing


def log_line(msg, **extra):
    entry = {"level": "info", "msg": msg}
    entry.update(extra)
    return json.dumps(entry)


class Progress(unittest.TestCase):
    """Lines as rclone 1.75 writes them with --use-json-log, colours included."""

    QUEUE_UP = "- \x1b[36mPath1\x1b[0m    \x1b[35m\x1b[32mQueue copy to\x1b[0m Path2\x1b[0m       - \x1b[36m/d/Docs/%s\x1b[0m"
    QUEUE_DOWN = "- \x1b[34mPath2\x1b[0m    \x1b[35m\x1b[32mQueue copy to\x1b[0m Path1\x1b[0m       - \x1b[36m/l/%s\x1b[0m"
    DO_DOWN = "- \x1b[34mPath2\x1b[0m    \x1b[35mDo queued copies to\x1b[0m                - \x1b[36mPath1\x1b[0m"
    DO_UP = "- \x1b[36mPath1\x1b[0m    \x1b[35mDo queued copies to\x1b[0m                - \x1b[36mPath2\x1b[0m"
    RESYNC_UP = "- \x1b[36mPath1\x1b[0m    \x1b[35mResync is copying files to\x1b[0m         - \x1b[36mPath2\x1b[0m"

    def feed(self, tracker, lines):
        for line in lines:
            tracker.feed(line)
        return tracker.snapshot()["progress"]

    def test_uploads_are_counted_against_the_announced_total(self):
        tracker = helper.ProgressTracker()
        progress = self.feed(tracker, [
            log_line(self.QUEUE_UP % "a.bin"), log_line(self.QUEUE_UP % "b.bin"), log_line(self.QUEUE_DOWN % "r.txt"),
            log_line(self.DO_DOWN), log_line("Copied (new)", object="r.txt"),
            log_line(self.DO_UP), log_line("Copied (new)", object="a.bin"),
            json.dumps({"level": "notice", "msg": "stats", "stats": {"eta": 42}}),
        ])
        self.assertEqual(progress["uploadsTotal"], 2, "only uploads count")
        self.assertEqual(progress["uploaded"], 1, "the download is not an upload")
        self.assertEqual(progress["recentUploads"], ["a.bin"])
        self.assertEqual(progress["phase"], "upload")
        self.assertEqual(progress["eta"], 42)

    def test_a_resync_has_no_upload_total(self):
        tracker = helper.ProgressTracker()
        progress = self.feed(tracker, [log_line(self.RESYNC_UP), log_line("Copied (new)", object="x")])
        self.assertIsNone(progress["uploadsTotal"])
        self.assertEqual(progress["uploaded"], 1)

    def test_downloads_count_only_once_something_moves(self):
        tracker = helper.ProgressTracker()
        progress = self.feed(tracker, [log_line(self.DO_DOWN), json.dumps({"stats": {"eta": 5, "transferring": []}})])
        self.assertEqual(progress["phase"], "download")
        self.assertEqual(progress["downloaded"], 0)
        self.assertFalse(progress["transferring"])
        progress = self.feed(tracker, [json.dumps({"stats": {"eta": 5, "transferring": [{"name": "r.txt"}]}})])
        self.assertTrue(progress["transferring"])
        progress = self.feed(tracker, [log_line("Copied (new)", object="r.txt")])
        self.assertEqual(progress["downloaded"], 1)
        self.assertEqual(progress["uploaded"], 0)

    def test_recent_uploads_are_newest_first_and_capped(self):
        tracker = helper.ProgressTracker()
        names = ["f%d" % i for i in range(7)]
        progress = self.feed(tracker, [log_line(self.DO_UP)] + [log_line("Copied (replaced existing)", object=n) for n in names])
        self.assertEqual(progress["recentUploads"], ["f6", "f5", "f4", "f3", "f2"])

    def test_merge_recent_keeps_older_names_and_drops_duplicates(self):
        self.assertEqual(helper.merge_recent([], ["a", "b"]), ["a", "b"])
        self.assertEqual(helper.merge_recent(["b", "c"], ["a", "b"]), ["b", "c", "a"])
        self.assertEqual(len(helper.merge_recent(list("abcdefg"), None)), 5)

    def test_garbage_and_missing_eta_are_ignored(self):
        tracker = helper.ProgressTracker()
        progress = self.feed(tracker, ["not json", "[]", json.dumps({"stats": {"eta": None}})])
        self.assertIsNone(progress["eta"])
        self.assertEqual(progress["phase"], "checking")


class LogParsing(unittest.TestCase):
    def test_safety_abort(self):
        log = "\n".join(json.dumps(x) for x in [
            {"level": "error", "msg": "Safety abort: all files were changed on Path1"},
            {"level": "notice", "msg": "\u001b[31mBisync aborted. Please try again.\u001b[0m"},
            {"level": "notice", "msg": "stats", "stats": {"transfers": 0, "errors": 1}},
        ])
        parsed = helper.parse_bisync_log(log)
        self.assertTrue(parsed["safetyAbort"])
        self.assertFalse(parsed["needsResync"])
        self.assertEqual(helper.classify(1, parsed, []), "safety-abort")
        self.assertEqual(parsed["stats"]["errors"], 1)

    def test_needs_resync_from_plain_lines(self):
        log = "2026/10/02 14:14:14 ERROR : \x1b[31mBisync aborted. Must run --resync to recover.\x1b[0m"
        parsed = helper.parse_bisync_log(log)
        self.assertTrue(parsed["needsResync"])
        self.assertEqual(helper.classify(7, parsed, []), "needs-resync")
        self.assertNotIn("\x1b", parsed["errors"][0])

    def test_retryable_is_not_needs_resync(self):
        parsed = helper.parse_bisync_log(
            "ERROR : Bisync aborted. Error is retryable without --resync due to --resilient mode.")
        self.assertFalse(parsed["needsResync"])
        self.assertEqual(helper.classify(1, parsed, []), "error")

    def test_error_message_skips_the_generic_lines(self):
        parsed = helper.parse_bisync_log("\n".join(json.dumps(x) for x in [
            {"level": "error", "msg": "couldn't connect: dial tcp: no such host"},
            {"level": "error", "msg": "Bisync aborted. Please try again."},
        ]))
        self.assertEqual(helper.summary_message("error", parsed, []), "couldn't connect: dial tcp: no such host")

    def test_ok_and_conflict(self):
        parsed = helper.parse_bisync_log("")
        self.assertEqual(helper.classify(0, parsed, []), "ok")
        self.assertEqual(helper.classify(0, parsed, ["a.conflict1"]), "conflict")

    def test_bisync_command_shape(self):
        pair = {"id": "0123456789ab", "local": "/home/x/Drive", "remote": "Docs"}
        cmd = helper.bisync_command("gdrive", pair)
        self.assertIn("gdrive:Docs", cmd)
        self.assertIn("--drive-skip-gdocs", cmd)
        self.assertNotIn("--fast-list", cmd, "it defeats the directory filters")
        self.assertNotIn("--resync", cmd)
        self.assertNotIn("--force", cmd)
        cmd = helper.bisync_command("gdrive", pair, resync=True, force=True)
        self.assertEqual(cmd[cmd.index("--resync-mode") + 1], "newer")
        self.assertIn("--force", cmd)


# ------------------------------------------------------------------ end to end


@unittest.skipUnless(REAL_RCLONE, "rclone is not installed")
class EndToEnd(Sandbox):
    """Runs the real bisync against a "local" remote called testdrive."""

    def setUp(self):
        super().setUp()
        self.drive = os.path.join(self.tmp, "drive")
        os.makedirs(os.path.join(self.drive, "Docs"))
        self.env["RCLONE_CONFIG_TESTDRIVE_TYPE"] = "local"
        # Paths in a local remote are relative to the working directory of
        # rclone; the helper is started from the fake Drive root.
        self.local = self.folder("Drive")

    def run_helper(self, *args, env=None, timeout=120):
        merged = dict(self.env)
        merged.update(env or {})
        return subprocess.run([sys.executable, HELPER] + list(args), env=merged, capture_output=True,
                              text=True, timeout=timeout, cwd=self.drive)

    def sync(self, pair_id, *flags):
        out = self.run_helper("sync", "testdrive", pair_id, *flags)
        self.assertEqual(out.returncode, 0, out.stderr)
        # Progress lines while bisync runs, then the result as the last line.
        lines = out.stdout.strip().split("\n")
        for line in lines[:-1]:
            self.assertIn("progress", json.loads(line), line)
        return json.loads(lines[-1])

    def write(self, path, text):
        with open(path, "w") as handle:
            handle.write(text)

    def read(self, path):
        with open(path) as handle:
            return handle.read()

    def test_first_sync_merges_then_syncs_both_ways(self):
        self.write(os.path.join(self.local, "local.txt"), "from laptop")
        self.write(os.path.join(self.drive, "Docs", "remote.txt"), "from drive")
        pair = json.loads(self.add_pair(self.local, "Docs").stdout)

        first = self.sync(pair["id"])
        self.assertEqual(first["state"], "ok", first)
        self.assertTrue(first["resync"], "the first sync must be a resync")
        self.assertEqual(self.read(os.path.join(self.drive, "Docs", "local.txt")), "from laptop")
        self.assertEqual(self.read(os.path.join(self.local, "remote.txt")), "from drive")
        self.assertTrue(helper.is_initialized(pair["id"]))

        # Downloaded files get the user's normal umask, not 0600.
        mode = stat.S_IMODE(os.stat(os.path.join(self.local, "remote.txt")).st_mode)
        self.assertEqual(mode & 0o044, 0o044 & ~os.umask(os.umask(0)), oct(mode))

        self.write(os.path.join(self.local, "new.txt"), "new")
        second = self.sync(pair["id"])
        self.assertEqual(second["state"], "ok", second)
        self.assertFalse(second["resync"])
        self.assertTrue(os.path.exists(os.path.join(self.drive, "Docs", "new.txt")))

        stored = helper.load_results()[pair["id"]]
        self.assertEqual(stored["state"], "ok")

    def test_conflict_keeps_both_versions(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            self.write(os.path.join(self.local, name), name)
        pair = json.loads(self.add_pair(self.local, "Docs").stdout)
        self.sync(pair["id"])
        self.write(os.path.join(self.local, "a.txt"), "laptop edit")
        time.sleep(1)
        self.write(os.path.join(self.drive, "Docs", "a.txt"), "drive edit")

        result = self.sync(pair["id"])
        self.assertEqual(result["state"], "conflict", result)
        self.assertEqual(result["conflictCount"], 2)
        contents = sorted(self.read(os.path.join(self.local, name)) for name in result["conflicts"])
        self.assertEqual(contents, ["drive edit", "laptop edit"])

    def test_lost_listings_become_a_resync_not_a_deletion(self):
        self.write(os.path.join(self.local, "a.txt"), "a")
        pair = json.loads(self.add_pair(self.local, "Docs").stdout)
        self.sync(pair["id"])
        shutil.rmtree(helper.workdir_for(pair["id"]))
        os.unlink(os.path.join(self.drive, "Docs", "a.txt"))

        result = self.sync(pair["id"])
        self.assertTrue(result["resync"])
        self.assertTrue(os.path.exists(os.path.join(self.local, "a.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.drive, "Docs", "a.txt")))

    def test_mass_deletion_stops_until_forced(self):
        for i in range(4):
            self.write(os.path.join(self.local, "f%d.txt" % i), str(i))
        pair = json.loads(self.add_pair(self.local, "Docs").stdout)
        self.sync(pair["id"])
        for i in range(3):
            os.unlink(os.path.join(self.local, "f%d.txt" % i))

        result = self.sync(pair["id"])
        self.assertEqual(result["state"], "safety-abort", result)
        self.assertEqual(len(os.listdir(os.path.join(self.drive, "Docs"))), 4)

        forced = self.sync(pair["id"], "--force")
        self.assertEqual(forced["state"], "ok", forced)
        self.assertEqual(os.listdir(os.path.join(self.drive, "Docs")), ["f3.txt"])

    def test_an_emptied_side_is_never_mirrored(self):
        for i in range(2):
            self.write(os.path.join(self.local, "f%d.txt" % i), str(i))
        pair = json.loads(self.add_pair(self.local, "Docs").stdout)
        self.sync(pair["id"])
        for i in range(2):
            os.unlink(os.path.join(self.local, "f%d.txt" % i))

        result = self.sync(pair["id"])
        self.assertEqual(result["state"], "error", result)
        self.assertIn("completely empty", result["message"])
        self.assertEqual(len(os.listdir(os.path.join(self.drive, "Docs"))), 2)

    def test_uploads_are_counted_and_remembered(self):
        self.write(os.path.join(self.local, "first.txt"), "1")
        self.write(os.path.join(self.drive, "Docs", "from-drive.txt"), "d")
        pair = json.loads(self.add_pair(self.local, "Docs").stdout)
        first = self.sync(pair["id"])
        self.assertEqual(first["uploaded"], 1, "downloads are not uploads")
        self.assertEqual(first["recentUploads"], ["first.txt"])

        self.write(os.path.join(self.local, "a.txt"), "a")
        self.write(os.path.join(self.local, "b.txt"), "b")
        second = self.sync(pair["id"])
        self.assertEqual(second["uploaded"], 2)
        self.assertEqual(sorted(second["recentUploads"][:2]), ["a.txt", "b.txt"])
        self.assertEqual(second["recentUploads"][2], "first.txt")

        # Nothing new: the list from before stays.
        third = self.sync(pair["id"])
        self.assertEqual(third["uploaded"], 0)
        self.assertEqual(third["recentUploads"], second["recentUploads"])

    def test_missing_local_folder_is_reported_not_synced(self):
        pair = json.loads(self.add_pair(self.local, "Docs").stdout)
        self.write(os.path.join(self.drive, "Docs", "x.txt"), "x")
        os.rmdir(self.local)
        result = self.sync(pair["id"])
        self.assertEqual(result["state"], "error")
        self.assertFalse(os.path.exists(self.local))

    def test_watch_reports_changes_without_names(self):
        if not shutil.which("inotifywait"):
            self.skipTest("inotifywait is not installed")
        pair = json.loads(self.add_pair(self.local, "Docs").stdout)
        proc = subprocess.Popen([sys.executable, HELPER, "watch", pair["id"]], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.0)
            self.write(os.path.join(self.local, "secret-name.txt"), "x")
            line = proc.stdout.readline()
            self.assertEqual(line, "change\n")
        finally:
            proc.terminate()
            # communicate() also closes both pipes.
            proc.communicate(timeout=10)
        children = subprocess.run(["pgrep", "-f", "inotifywait.*" + self.local], capture_output=True, text=True)
        self.assertEqual(children.stdout.strip(), "", "inotifywait outlived the helper")


if __name__ == "__main__":
    unittest.main()
