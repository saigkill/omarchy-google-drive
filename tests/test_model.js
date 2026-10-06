// Tests for Model.js: command builders, settings parsing, status decoding and
// the state the bar shows.
//
// Run with: node tests/test_model.js

const assert = require("assert")
const path = require("path")
const Model = require(path.join(__dirname, "..", "Model.js"))

const HELPER = "/plugin/gdrive_helper.py"
const ID = "0123456789ab"

let passed = 0
function test(name, body) {
  try {
    body()
    passed++
  } catch (error) {
    console.error("FAIL: " + name)
    console.error("      " + error.message)
    process.exitCode = 1
  }
}

// Every builder in the plugin. Each must run the helper (or, for opening a
// folder, xdg-open with a single absolute path) and nothing else.
const COMMANDS = {
  statusCmd: Model.statusCmd(HELPER, "gdrive"),
  addPairCmd: Model.addPairCmd(HELPER),
  removePairCmd: Model.removePairCmd(HELPER, ID),
  setEnabledCmd: Model.setEnabledCmd(HELPER, ID, true),
  syncCmd: Model.syncCmd(HELPER, "gdrive", ID, {}),
  "syncCmd resync force": Model.syncCmd(HELPER, "gdrive", ID, { resync: true, force: true }),
  loginCmd: Model.loginCmd(HELPER, "gdrive"),
  logoutCmd: Model.logoutCmd(HELPER, "gdrive"),
  watchCmd: Model.watchCmd(HELPER, ID),
}

for (const [name, cmd] of Object.entries(COMMANDS)) {
  test(name + " runs the helper without a shell", () => {
    assert.ok(Array.isArray(cmd), "not an array")
    assert.strictEqual(cmd[0], HELPER)
    for (const arg of cmd) {
      assert.strictEqual(typeof arg, "string")
      assert.ok(!/[;&|`$<>\n]/.test(arg), "shell metacharacter in " + JSON.stringify(arg))
    }
    assert.ok(cmd.indexOf("sh") === -1 && cmd.indexOf("-c") === -1)
  })
}

test("the local path of a new pair travels in the environment, not argv", () => {
  const local = "/home/someone/Secret Project"
  assert.deepStrictEqual(Model.addPairCmd(HELPER), [HELPER, "add-pair"])
  const env = Model.addPairEnv(local, "Work")
  assert.deepStrictEqual(Object.keys(env), ["GDRIVE_PAIR_JSON"])
  assert.deepStrictEqual(JSON.parse(env.GDRIVE_PAIR_JSON), { local: local, remote: "Work" })
})

test("builders refuse an id that is not a pair id", () => {
  for (const bad of ["", "../../etc", "0123456789AB", "0123456789abc", "--force", "x; rm -rf ~"]) {
    assert.strictEqual(Model.removePairCmd(HELPER, bad), null, bad)
    assert.strictEqual(Model.setEnabledCmd(HELPER, bad, true), null, bad)
    assert.strictEqual(Model.syncCmd(HELPER, "gdrive", bad, {}), null, bad)
    assert.strictEqual(Model.watchCmd(HELPER, bad), null, bad)
  }
})

test("an invalid remote name falls back to the default", () => {
  assert.strictEqual(Model.remoteName("work-drive"), "work-drive")
  for (const bad of ["", "a:b", "-flag", "has space", "../x", null, undefined, "x".repeat(65)]) {
    assert.strictEqual(Model.remoteName(bad), "gdrive", String(bad))
  }
  assert.deepStrictEqual(Model.statusCmd(HELPER, "--config=/tmp/evil"), [HELPER, "status", "gdrive"])
})

test("sync flags are fixed strings", () => {
  assert.deepStrictEqual(Model.syncCmd(HELPER, "gdrive", ID, { resync: true }),
    [HELPER, "sync", "gdrive", ID, "--resync"])
  assert.deepStrictEqual(Model.syncCmd(HELPER, "gdrive", ID, { force: "yes please" }),
    [HELPER, "sync", "gdrive", ID, "--force"])
})

test("openFolderCmd only takes an absolute path", () => {
  assert.deepStrictEqual(Model.openFolderCmd("/home/x/Drive"), ["xdg-open", "/home/x/Drive"])
  assert.strictEqual(Model.openFolderCmd("https://evil.example"), null)
  assert.strictEqual(Model.openFolderCmd("-x"), null)
  assert.strictEqual(Model.openFolderCmd(""), null)
})

test("settings are parsed defensively", () => {
  const d = Model.parseSettings(null)
  assert.deepStrictEqual(d, { remote: "gdrive", intervalMinutes: 15, watchChanges: true, debounceSeconds: 20, notify: true })
  const s = Model.parseSettings({ remote: "x", intervalMinutes: "0", watchChanges: "false", debounceSeconds: 1, notify: "no" })
  assert.deepStrictEqual(s, { remote: "x", intervalMinutes: 0, watchChanges: false, debounceSeconds: 3, notify: false })
  assert.strictEqual(Model.parseSettings({ intervalMinutes: "abc" }).intervalMinutes, 15)
  assert.strictEqual(Model.parseSettings({ intervalMinutes: 99999 }).intervalMinutes, 1440)
  assert.strictEqual(Model.parseSettings({ debounceSeconds: -5 }).debounceSeconds, 3)
})

test("decodeStatus survives garbage", () => {
  for (const raw of [null, "x", 42, [], {}, { pairs: "nope" }, { pairs: [null, 1, { id: "bad" }] }]) {
    const s = Model.decodeStatus(raw)
    assert.ok(Array.isArray(s.pairs))
    assert.strictEqual(s.pairs.length, 0)
  }
  assert.strictEqual(Model.decodeStatus(null).loaded, false)
})

test("decodeStatus reads a real document", () => {
  const s = Model.decodeStatus({
    rclone: { installed: true, version: "v1.75.1" },
    remote: { name: "gdrive", type: "drive", configured: true },
    filtersFile: "/s/filters.txt",
    pairs: [{
      id: ID, local: "/home/x/Drive", remote: "Docs", enabled: true, initialized: true, exists: true,
      result: { state: "conflict", message: "1 conflict file", at: 1700000000, transfers: 2, bytes: 2048,
                conflictCount: 1, conflicts: ["a.txt.conflict1"] }
    }]
  })
  assert.strictEqual(s.connected, true)
  assert.strictEqual(s.pairs.length, 1)
  assert.strictEqual(s.pairs[0].result.at, 1700000000 * 1000)
  assert.deepStrictEqual(s.pairs[0].result.conflicts, ["a.txt.conflict1"])
})

test("an unknown result state is an error", () => {
  assert.strictEqual(Model.decodeResult({ state: "exploded" }).state, "error")
  assert.strictEqual(Model.decodeResult(null), null)
})

test("lastJsonLine skips noise", () => {
  assert.deepStrictEqual(Model.lastJsonLine("progress\n{\"state\":\"ok\"}\n\n"), { state: "ok" })
  assert.strictEqual(Model.lastJsonLine("nothing here"), null)
})

test("enqueue keeps one job per pair and merges flags", () => {
  let q = []
  q = Model.enqueue(q, "a", {})
  q = Model.enqueue(q, "b", {})
  q = Model.enqueue(q, "a", { resync: true })
  assert.deepStrictEqual(q, [{ id: "a", resync: true, force: false }, { id: "b", resync: false, force: false }])
  q = Model.enqueue(q, "a", {})
  assert.strictEqual(q[0].resync, true, "a later plain sync must not drop the resync")
  assert.deepStrictEqual(Model.dropFromQueue(q, "a").map(j => j.id), ["b"])
})

test("withResult replaces only the one pair and leaves the input alone", () => {
  const pairs = [{ id: "a", result: null, initialized: false }, { id: "b", result: null }]
  const next = Model.withResult(pairs, "a", { state: "ok" })
  assert.strictEqual(pairs[0].result, null)
  assert.strictEqual(next[0].result.state, "ok")
  assert.strictEqual(next[0].initialized, true)
  assert.strictEqual(next[1], pairs[1])
  assert.strictEqual(Model.withResult(pairs, "a", { state: "needs-resync" })[0].initialized, false)
})

function status(pairs, extra) {
  return Object.assign({ loaded: true, rcloneInstalled: true, connected: true, pairs: pairs }, extra || {})
}
const pair = (id, state, more) => Object.assign({ id: id, enabled: true, exists: true,
  result: state ? { state: state, message: state, conflictCount: state === "conflict" ? 1 : 0 } : null }, more || {})

test("overallState orders by severity", () => {
  assert.strictEqual(Model.overallState(Model.emptyStatus(), "", 0), "loading")
  assert.strictEqual(Model.overallState(status([], { rcloneInstalled: false }), "", 0), "missing")
  assert.strictEqual(Model.overallState(status([], { connected: false }), "", 0), "disconnected")
  assert.strictEqual(Model.overallState(status([]), "", 0), "empty")
  assert.strictEqual(Model.overallState(status([pair("a", "ok")]), "", 0), "ok")
  assert.strictEqual(Model.overallState(status([pair("a", "ok")]), "a", 0), "syncing")
  assert.strictEqual(Model.overallState(status([pair("a", "conflict")]), "", 0), "conflict")
  assert.strictEqual(Model.overallState(status([pair("a", "conflict"), pair("b", "error")]), "a", 0), "error")
  assert.strictEqual(Model.overallState(status([pair("a", "safety-abort")]), "", 0), "error")
  assert.strictEqual(Model.overallState(status([pair("a", "ok", { enabled: false })]), "", 0), "paused")
  // A paused pair's old failure does not keep the bar red.
  assert.strictEqual(Model.overallState(status([pair("a", "error", { enabled: false }), pair("b", "ok")]), "", 0), "ok")
})

test("notifications fire on transitions only", () => {
  const p = { local: "/home/x/Docs" }
  const err = { state: "error", message: "offline", conflictCount: 0 }
  const ok = { state: "ok", conflictCount: 0 }
  assert.ok(Model.notificationFor(ok, err, p))
  assert.strictEqual(Model.notificationFor(err, err, p), null, "repeated failure is quiet")
  assert.strictEqual(Model.notificationFor(err, ok, p).urgency, "low", "recovery is announced")
  assert.strictEqual(Model.notificationFor(ok, ok, p), null)
  assert.strictEqual(Model.notificationFor(null, ok, p), null)
  const c1 = { state: "conflict", conflictCount: 1 }
  const c2 = { state: "conflict", conflictCount: 2 }
  assert.ok(Model.notificationFor(ok, c1, p))
  assert.strictEqual(Model.notificationFor(c1, c1, p), null)
  assert.ok(Model.notificationFor(c1, c2, p))
  assert.ok(Model.notificationFor(err, { state: "needs-resync", message: "x" }, p), "a different failure is new")
  assert.ok(Model.notificationFor(ok, err, p).title.indexOf("Docs") !== -1)
})

test("formatting", () => {
  assert.strictEqual(Model.homeRelative("/home/x/Drive", "/home/x"), "~/Drive")
  assert.strictEqual(Model.homeRelative("/home/xy/Drive", "/home/x"), "/home/xy/Drive")
  assert.strictEqual(Model.remoteLabel(""), "My Drive")
  assert.strictEqual(Model.remoteLabel("A/B"), "My Drive/A/B")
  assert.strictEqual(Model.pairTitle({ local: "/home/x/Work Docs" }), "Work Docs")
  assert.strictEqual(Model.relativeTime(0, 1000), "never")
  assert.strictEqual(Model.relativeTime(1000, 11000), "just now")
  assert.strictEqual(Model.relativeTime(0 + 1, 1 + 5 * 60000), "5 min ago")
  assert.strictEqual(Model.relativeTime(1, 1 + 26 * 3600000), "1 day ago")
  assert.strictEqual(Model.formatBytes(512), "512 B")
  assert.strictEqual(Model.formatBytes(1536), "1.5 KiB")
  assert.strictEqual(Model.formatBytes(50 * 1024 * 1024), "50 MiB")
  assert.strictEqual(Model.resultDetail({ transfers: 1, bytes: 10, deletes: 2, resync: true }),
    "1 file (10 B) · 2 deleted · full resync")
})

test("pairStateLabel", () => {
  const p = pair("a", "ok")
  assert.strictEqual(Model.pairStateLabel(p, "a", []), "Syncing…")
  assert.strictEqual(Model.pairStateLabel(p, "", [{ id: "a" }]), "Queued")
  assert.strictEqual(Model.pairStateLabel(pair("a", null, { exists: false }), "", []), "Local folder missing")
  assert.strictEqual(Model.pairStateLabel(pair("a", null, { enabled: false }), "", []), "Paused")
  assert.strictEqual(Model.pairStateLabel(pair("a", null), "", []), "Never synced")
})

test("decodeProgress reads progress lines and nothing else", () => {
  const p = Model.decodeProgress({ progress: { phase: "upload", uploaded: 3, uploadsTotal: 12, eta: 95, recentUploads: ["a", 7, "b"] } })
  assert.deepStrictEqual(p, { phase: "upload", uploaded: 3, uploadsTotal: 12, eta: 95, downloaded: 0, transferring: false, recentUploads: ["a", "b"] })
  assert.strictEqual(Model.decodeProgress({ state: "ok" }), null, "a result is not progress")
  assert.strictEqual(Model.decodeProgress(null), null)
  const odd = Model.decodeProgress({ progress: { phase: "weird", uploadsTotal: null, eta: -1 } })
  assert.strictEqual(odd.phase, "checking")
  assert.strictEqual(odd.uploadsTotal, null)
  assert.strictEqual(odd.eta, null)
})

test("formatEta", () => {
  assert.strictEqual(Model.formatEta(null), "")
  assert.strictEqual(Model.formatEta(20), "less than a minute")
  assert.strictEqual(Model.formatEta(150), "about 3 min")
  assert.strictEqual(Model.formatEta(3600), "about 1 h")
  assert.strictEqual(Model.formatEta(3600 + 25 * 60), "about 1 h 25 min")
})

test("progressLabel shows x / y and the time left", () => {
  const label = (p) => Model.progressLabel(Model.decodeProgress({ progress: p }))
  assert.strictEqual(label({ phase: "upload", uploaded: 3, uploadsTotal: 12, eta: 150 }), "Uploading 3 / 12 · about 3 min left")
  assert.strictEqual(label({ phase: "upload", uploaded: 4, uploadsTotal: null, eta: null }), "4 uploaded")
  assert.strictEqual(label({ phase: "checking", uploaded: 0, uploadsTotal: null, eta: 3 }), "Checking for changes")
  assert.strictEqual(label({ phase: "download", uploaded: 0, uploadsTotal: null, eta: 30, transferring: true }), "Downloading · less than a minute left")
  assert.strictEqual(label({ phase: "download", uploaded: 0, uploadsTotal: null, eta: 30, downloaded: 2 }), "2 downloaded · less than a minute left")
  assert.strictEqual(label({ phase: "download", uploaded: 0, uploadsTotal: null, eta: 30 }), "Checking for changes", "nothing in flight yet")
  assert.strictEqual(Model.progressLabel(null), "Syncing…")
})

test("pairStateLabel shows progress while syncing", () => {
  const p = pair("a", "ok")
  const progress = Model.decodeProgress({ progress: { phase: "upload", uploaded: 1, uploadsTotal: 2, eta: null } })
  assert.strictEqual(Model.pairStateLabel(p, "a", [], progress), "Uploading 1 / 2")
})

test("recentUploads merges the running sync with earlier ones", () => {
  const p = pair("a", "ok")
  p.result.recentUploads = ["old1", "old2"]
  assert.deepStrictEqual(Model.recentUploads(p, "", null), ["old1", "old2"])
  const progress = Model.decodeProgress({ progress: { phase: "upload", recentUploads: ["new", "old1"] } })
  assert.deepStrictEqual(Model.recentUploads(p, "a", progress), ["new", "old1", "old2"])
  assert.deepStrictEqual(Model.recentUploads(p, "b", progress), ["old1", "old2"], "other pairs keep their list")
  assert.strictEqual(Model.decodeResult({ state: "ok", recentUploads: ["1", "2", "3", "4", "5", "6"] }).recentUploads.length, 5)
})

test("the tooltip names failing folders", () => {
  const s = status([pair("a", "error", { local: "/h/Docs" }), pair("b", "ok", { local: "/h/Pics" })])
  const text = Model.tooltip(s, "error", "", Date.now())
  assert.ok(text.indexOf("Docs: error") !== -1, text)
  assert.ok(text.indexOf("Pics") === -1, text)
})

console.log(passed + " passed" + (process.exitCode ? ", some FAILED" : ""))
