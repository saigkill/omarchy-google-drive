// Pure logic for the Google Drive plugin: command builders, settings parsing,
// decoding of the helper's status document, and everything the bar and the
// panel display. Nothing in here touches a process or a file, so all of it is
// covered by tests/test_model.js without a running shell.
//
// Every command below runs gdrive_helper.py, never rclone and never a shell.
// The helper owns rclone, the pair list and the bisync state; the panel passes
// it only a remote name, a pair id and fixed flags. The local path of a new
// pair travels in GDRIVE_PAIR_JSON on the environment (see addPairEnv).

var DEFAULT_REMOTE = "gdrive"
var DEFAULT_INTERVAL_MINUTES = 15
var DEFAULT_DEBOUNCE_SECONDS = 20

// Events the helper's own sync causes (a download, a conflict rename) arrive
// right after it finishes. Changes inside this window after a sync are not
// treated as user edits, or every sync would schedule the next one.
var WATCH_GRACE_MS = 5000

// Gives the shell, the network and the notification service time to come up
// before the first sync after login.
var STARTUP_DELAY_MS = 30000

// A watcher that dies is restarted, but not in a tight loop: inotify running
// out of watches fails instantly every time.
var WATCH_RETRY_MS = 60000

var REMOTE_NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/
var PAIR_ID_RE = /^[a-f0-9]{12}$/

// ------------------------------------------------------------------ settings

function intSetting(value, fallback, min, max) {
  var n = parseInt(value, 10)
  if (!isFinite(n)) return fallback
  if (n < min) return min
  if (n > max) return max
  return n
}

function boolSetting(value, fallback) {
  if (value === true || value === false) return value
  var text = String(value).trim().toLowerCase()
  if (text === "true" || text === "1" || text === "yes") return true
  if (text === "false" || text === "0" || text === "no") return false
  return fallback
}

function remoteName(value) {
  var text = String(value === undefined || value === null ? "" : value).trim()
  return REMOTE_NAME_RE.test(text) ? text : DEFAULT_REMOTE
}

function parseSettings(raw) {
  var s = raw && typeof raw === "object" ? raw : {}
  return {
    remote: remoteName(s.remote),
    // 0 switches the timer off; changes are then synced by the watcher only.
    intervalMinutes: intSetting(s.intervalMinutes, DEFAULT_INTERVAL_MINUTES, 0, 1440),
    watchChanges: boolSetting(s.watchChanges, true),
    debounceSeconds: intSetting(s.debounceSeconds, DEFAULT_DEBOUNCE_SECONDS, 3, 600),
    notify: boolSetting(s.notify, true)
  }
}

// ------------------------------------------------------------ helper commands

function isPairId(id) {
  return PAIR_ID_RE.test(String(id))
}

function statusCmd(helper, remote) {
  return [helper, "status", remoteName(remote)]
}

function addPairCmd(helper) {
  return [helper, "add-pair"]
}

function addPairEnv(local, remote) {
  return { GDRIVE_PAIR_JSON: JSON.stringify({ local: String(local || ""), remote: String(remote || "") }) }
}

function removePairCmd(helper, id) {
  if (!isPairId(id)) return null
  return [helper, "remove-pair", String(id)]
}

function setEnabledCmd(helper, id, enabled) {
  if (!isPairId(id)) return null
  return [helper, "set-enabled", String(id), enabled ? "1" : "0"]
}

function syncCmd(helper, remote, id, options) {
  if (!isPairId(id)) return null
  var cmd = [helper, "sync", remoteName(remote), String(id)]
  var o = options || {}
  if (o.resync) cmd.push("--resync")
  if (o.force) cmd.push("--force")
  return cmd
}

function loginCmd(helper, remote) {
  return [helper, "login", remoteName(remote)]
}

function logoutCmd(helper, remote) {
  return [helper, "logout", remoteName(remote)]
}

function watchCmd(helper, id) {
  if (!isPairId(id)) return null
  return [helper, "watch", String(id)]
}

// The panel opens folders with xdg-open and Drive in the browser. The Drive
// URL is fixed; a pair's remote path is not part of it because Drive's web
// UI addresses folders by id, not by path.
var DRIVE_WEB_URL = "https://drive.google.com/drive/my-drive"

function openFolderCmd(path) {
  var text = String(path || "")
  if (text.charAt(0) !== "/") return null
  return ["xdg-open", text]
}

// ------------------------------------------------------------------- decoding

function parseJson(text) {
  try {
    return JSON.parse(String(text || "").trim())
  } catch (error) {
    return null
  }
}

// The last JSON line of an output, for commands that may print progress
// before their result.
function lastJsonLine(text) {
  var lines = String(text || "").split("\n")
  for (var i = lines.length - 1; i >= 0; i--) {
    var line = lines[i].trim()
    if (line === "") continue
    var parsed = parseJson(line)
    if (parsed !== null) return parsed
  }
  return null
}

var RESULT_STATES = ["ok", "conflict", "error", "needs-resync", "safety-abort", "busy"]

function decodeResult(raw) {
  if (!raw || typeof raw !== "object") return null
  var state = RESULT_STATES.indexOf(raw.state) === -1 ? "error" : raw.state
  var conflicts = []
  if (raw.conflicts && raw.conflicts.length) {
    for (var i = 0; i < raw.conflicts.length && i < 50; i++) conflicts.push(String(raw.conflicts[i]))
  }
  return {
    state: state,
    message: String(raw.message || ""),
    at: Number(raw.at) > 0 ? Number(raw.at) * 1000 : 0,
    seconds: Number(raw.seconds) || 0,
    resync: raw.resync === true,
    transfers: Math.max(0, Number(raw.transfers) || 0),
    deletes: Math.max(0, Number(raw.deletes) || 0),
    bytes: Math.max(0, Number(raw.bytes) || 0),
    conflictCount: Math.max(conflicts.length, Number(raw.conflictCount) || 0),
    conflicts: conflicts,
    uploaded: Math.max(0, Number(raw.uploaded) || 0),
    recentUploads: names(raw.recentUploads)
  }
}

var RECENT_UPLOADS = 5

function names(raw) {
  var list = []
  for (var i = 0; raw && raw.length && i < raw.length && list.length < RECENT_UPLOADS; i++) {
    if (typeof raw[i] === "string" && raw[i] !== "") list.push(raw[i])
  }
  return list
}

// A progress line from a running sync: { "progress": { ... } }.
function decodeProgress(raw) {
  var p = raw && typeof raw === "object" ? raw.progress : null
  if (!p || typeof p !== "object") return null
  var total = Number(p.uploadsTotal)
  var eta = Number(p.eta)
  return {
    phase: ["checking", "upload", "download"].indexOf(p.phase) === -1 ? "checking" : p.phase,
    uploaded: Math.max(0, Number(p.uploaded) || 0),
    uploadsTotal: p.uploadsTotal === null || p.uploadsTotal === undefined || !(total > 0) ? null : total,
    eta: p.eta === null || p.eta === undefined || !(eta >= 0) ? null : eta,
    downloaded: Math.max(0, Number(p.downloaded) || 0),
    transferring: p.transferring === true,
    recentUploads: names(p.recentUploads)
  }
}

function decodePair(raw) {
  if (!raw || typeof raw !== "object" || !isPairId(raw.id)) return null
  return {
    id: String(raw.id),
    local: String(raw.local || ""),
    remote: String(raw.remote || ""),
    enabled: raw.enabled !== false,
    initialized: raw.initialized === true,
    exists: raw.exists !== false,
    result: decodeResult(raw.result)
  }
}

function emptyStatus() {
  return {
    loaded: false,
    rcloneInstalled: false,
    rcloneVersion: "",
    remote: DEFAULT_REMOTE,
    remoteType: "",
    connected: false,
    filtersFile: "",
    pairs: []
  }
}

function decodeStatus(raw) {
  var status = emptyStatus()
  if (!raw || typeof raw !== "object") return status
  status.loaded = true
  var rclone = raw.rclone || {}
  var remote = raw.remote || {}
  status.rcloneInstalled = rclone.installed === true
  status.rcloneVersion = String(rclone.version || "")
  status.remote = remoteName(remote.name)
  status.remoteType = String(remote.type || "")
  status.connected = remote.configured === true
  status.filtersFile = String(raw.filtersFile || "")
  var pairs = raw.pairs && raw.pairs.length ? raw.pairs : []
  for (var i = 0; i < pairs.length; i++) {
    var pair = decodePair(pairs[i])
    if (pair) status.pairs.push(pair)
  }
  return status
}

function findPair(pairs, id) {
  for (var i = 0; i < (pairs ? pairs.length : 0); i++) {
    if (pairs[i].id === id) return pairs[i]
  }
  return null
}

function withResult(pairs, id, result) {
  var next = []
  for (var i = 0; i < pairs.length; i++) {
    var pair = pairs[i]
    if (pair.id !== id) {
      next.push(pair)
      continue
    }
    var copy = {}
    for (var key in pair) copy[key] = pair[key]
    copy.result = result
    if (result && result.state !== "needs-resync" && result.state !== "busy") copy.initialized = true
    next.push(copy)
  }
  return next
}

// ------------------------------------------------------------------- queueing

// The sync queue holds { id, resync, force }. A pair is queued at most once;
// queueing it again only adds flags, so a Resync pressed while a normal sync
// is already waiting turns that one into a resync instead of adding a second.
function enqueue(queue, id, options) {
  var o = options || {}
  var next = []
  var merged = false
  for (var i = 0; i < queue.length; i++) {
    var job = queue[i]
    if (job.id === id) {
      next.push({ id: id, resync: job.resync || o.resync === true, force: job.force || o.force === true })
      merged = true
    } else {
      next.push(job)
    }
  }
  if (!merged) next.push({ id: id, resync: o.resync === true, force: o.force === true })
  return next
}

function dropFromQueue(queue, id) {
  return queue.filter(function(job) { return job.id !== id })
}

// Pairs a scheduled sync should touch: enabled, and with a local folder that
// still exists. A missing folder (an unmounted disk) is skipped, not reported
// on every tick.
function syncablePairs(pairs) {
  return (pairs || []).filter(function(pair) { return pair.enabled && pair.exists })
}

// --------------------------------------------------------------- aggregation

// The bar shows one state for everything. The order is severity: something
// the user has to act on beats something in progress beats all good.
function overallState(status, syncingId, queueLength) {
  if (!status.loaded) return "loading"
  if (!status.rcloneInstalled) return "missing"
  if (!status.connected) return "disconnected"
  var active = syncablePairs(status.pairs)
  var worst = ""
  for (var i = 0; i < active.length; i++) {
    var r = active[i].result
    if (!r) continue
    if (r.state === "error" || r.state === "needs-resync" || r.state === "safety-abort") worst = "error"
    else if (r.state === "conflict" && worst !== "error") worst = "conflict"
  }
  if (worst === "error") return "error"
  if (syncingId || queueLength > 0) return "syncing"
  if (worst === "conflict") return "conflict"
  if (status.pairs.length === 0) return "empty"
  if (active.length === 0) return "paused"
  return "ok"
}

function stateLabel(state) {
  switch (state) {
    case "loading": return "Starting"
    case "missing": return "rclone is not installed"
    case "disconnected": return "Not connected"
    case "error": return "Sync problem"
    case "syncing": return "Syncing"
    case "conflict": return "Conflicts to resolve"
    case "empty": return "No folders configured"
    case "paused": return "All folders paused"
    default: return "Up to date"
  }
}

function formatEta(seconds) {
  if (seconds === null || seconds === undefined || !(seconds >= 0)) return ""
  if (seconds < 60) return "less than a minute"
  var minutes = Math.round(seconds / 60)
  if (minutes < 60) return "about " + minutes + " min"
  var hours = Math.floor(minutes / 60)
  var rest = minutes % 60
  return "about " + hours + " h" + (rest > 0 ? " " + rest + " min" : "")
}

// "Uploading 3 / 12 · about 2 min left". A resync announces no upload total,
// so it counts without one. The download phase only reads "Downloading" once
// a file is in flight or done; before that bisync is still comparing.
function progressLabel(progress) {
  if (!progress) return "Syncing…"
  var parts = []
  var comparing = progress.phase === "checking"
    || (progress.phase === "download" && progress.downloaded === 0 && !progress.transferring)
  if (progress.uploadsTotal !== null) parts.push("Uploading " + Math.min(progress.uploaded, progress.uploadsTotal) + " / " + progress.uploadsTotal)
  else if (progress.uploaded > 0) parts.push(progress.uploaded + " uploaded")
  else if (comparing) parts.push("Checking for changes")
  else if (progress.downloaded > 0) parts.push(progress.downloaded + " downloaded")
  else parts.push("Downloading")
  var eta = formatEta(progress.eta)
  if (eta !== "" && !comparing) parts.push(eta + " left")
  return parts.length > 0 ? parts.join(" · ") : "Syncing…"
}

// Newest first: what the running sync uploaded, then what earlier syncs did.
function recentUploads(pair, syncingId, progress) {
  var earlier = pair && pair.result ? pair.result.recentUploads || [] : []
  if (!pair || pair.id !== syncingId || !progress) return earlier.slice(0, RECENT_UPLOADS)
  var merged = []
  var all = progress.recentUploads.concat(earlier)
  for (var i = 0; i < all.length && merged.length < RECENT_UPLOADS; i++) {
    if (merged.indexOf(all[i]) === -1) merged.push(all[i])
  }
  return merged
}

function pairStateLabel(pair, syncingId, queue, progress) {
  if (pair.id === syncingId) return progressLabel(progress)
  for (var i = 0; i < (queue ? queue.length : 0); i++) {
    if (queue[i].id === pair.id) return "Queued"
  }
  if (!pair.exists) return "Local folder missing"
  if (!pair.enabled) return "Paused"
  if (!pair.result) return pair.initialized ? "Not synced since restart" : "Never synced"
  return pair.result.message || "Done"
}

function lastSyncAt(pairs) {
  var latest = 0
  for (var i = 0; i < (pairs ? pairs.length : 0); i++) {
    var r = pairs[i].result
    if (r && (r.state === "ok" || r.state === "conflict") && r.at > latest) latest = r.at
  }
  return latest
}

// ------------------------------------------------------------- notifications

// A notification is sent on a transition only: into a failure, or into more
// conflicts than before. A sync that keeps failing every 15 minutes while
// offline notifies once, and the recovery notifies once.
function notificationFor(previous, next, pair) {
  if (!next || !pair) return null
  var name = pairTitle(pair)
  var wasBad = !!previous && isFailure(previous.state)
  if (isFailure(next.state)) {
    if (wasBad && previous.state === next.state) return null
    return { urgency: "critical", title: "Google Drive: " + name, body: next.message || "Sync failed" }
  }
  if (next.state === "conflict") {
    var before = previous && previous.state === "conflict" ? previous.conflictCount : 0
    if (next.conflictCount <= before) return null
    return {
      urgency: "normal",
      title: "Google Drive: " + name,
      body: next.conflictCount + (next.conflictCount === 1 ? " file was" : " files were")
        + " changed on both sides. Both versions are kept as .conflict files."
    }
  }
  if (wasBad && next.state === "ok") {
    return { urgency: "low", title: "Google Drive: " + name, body: "Syncing again" }
  }
  return null
}

function isFailure(state) {
  return state === "error" || state === "needs-resync" || state === "safety-abort"
}

// ---------------------------------------------------------------- formatting

function homeRelative(path, home) {
  var text = String(path || "")
  var h = String(home || "").replace(/\/+$/, "")
  if (h !== "" && (text === h || text.indexOf(h + "/") === 0)) return "~" + text.substring(h.length)
  return text
}

function remoteLabel(remotePath) {
  var text = String(remotePath || "")
  return text === "" ? "My Drive" : "My Drive/" + text
}

function pairTitle(pair) {
  var local = String(pair && pair.local || "")
  var slash = local.lastIndexOf("/")
  var base = slash === -1 ? local : local.substring(slash + 1)
  return base || local || "folder"
}

function relativeTime(thenMs, nowMs) {
  if (!thenMs) return "never"
  var seconds = Math.max(0, Math.round((nowMs - thenMs) / 1000))
  if (seconds < 45) return "just now"
  var minutes = Math.round(seconds / 60)
  if (minutes < 60) return minutes + " min ago"
  var hours = Math.round(minutes / 60)
  if (hours < 24) return hours + " h ago"
  var days = Math.round(hours / 24)
  return days + (days === 1 ? " day ago" : " days ago")
}

function formatBytes(bytes) {
  var n = Number(bytes) || 0
  if (n < 1024) return n + " B"
  var units = ["KiB", "MiB", "GiB", "TiB"]
  var i = -1
  do {
    n /= 1024
    i++
  } while (n >= 1024 && i < units.length - 1)
  return (n >= 10 ? Math.round(n) : Math.round(n * 10) / 10) + " " + units[i]
}

function resultDetail(result) {
  if (!result) return ""
  var parts = []
  if (result.transfers > 0) parts.push(result.transfers + (result.transfers === 1 ? " file" : " files")
    + " (" + formatBytes(result.bytes) + ")")
  if (result.deletes > 0) parts.push(result.deletes + " deleted")
  if (result.resync) parts.push("full resync")
  return parts.join(" · ")
}

function tooltip(status, state, syncingId, nowMs) {
  var lines = ["Google Drive: " + stateLabel(state)]
  if (state === "missing" || state === "disconnected" || state === "loading") return lines.join("\n")
  var latest = lastSyncAt(status.pairs)
  if (latest) lines.push("Last sync " + relativeTime(latest, nowMs))
  for (var i = 0; i < status.pairs.length; i++) {
    var pair = status.pairs[i]
    var r = pair.result
    if (pair.id === syncingId) lines.push(pairTitle(pair) + ": syncing")
    else if (r && r.state !== "ok") lines.push(pairTitle(pair) + ": " + r.message)
  }
  return lines.join("\n")
}

if (typeof module !== "undefined") {
  module.exports = {
    DEFAULT_REMOTE: DEFAULT_REMOTE,
    DEFAULT_INTERVAL_MINUTES: DEFAULT_INTERVAL_MINUTES,
    DEFAULT_DEBOUNCE_SECONDS: DEFAULT_DEBOUNCE_SECONDS,
    WATCH_GRACE_MS: WATCH_GRACE_MS,
    STARTUP_DELAY_MS: STARTUP_DELAY_MS,
    WATCH_RETRY_MS: WATCH_RETRY_MS,
    DRIVE_WEB_URL: DRIVE_WEB_URL,
    intSetting: intSetting,
    boolSetting: boolSetting,
    remoteName: remoteName,
    parseSettings: parseSettings,
    isPairId: isPairId,
    statusCmd: statusCmd,
    addPairCmd: addPairCmd,
    addPairEnv: addPairEnv,
    removePairCmd: removePairCmd,
    setEnabledCmd: setEnabledCmd,
    syncCmd: syncCmd,
    loginCmd: loginCmd,
    logoutCmd: logoutCmd,
    watchCmd: watchCmd,
    openFolderCmd: openFolderCmd,
    parseJson: parseJson,
    lastJsonLine: lastJsonLine,
    decodeProgress: decodeProgress,
    formatEta: formatEta,
    progressLabel: progressLabel,
    recentUploads: recentUploads,
    decodeResult: decodeResult,
    decodePair: decodePair,
    emptyStatus: emptyStatus,
    decodeStatus: decodeStatus,
    findPair: findPair,
    withResult: withResult,
    enqueue: enqueue,
    dropFromQueue: dropFromQueue,
    syncablePairs: syncablePairs,
    overallState: overallState,
    stateLabel: stateLabel,
    pairStateLabel: pairStateLabel,
    lastSyncAt: lastSyncAt,
    notificationFor: notificationFor,
    isFailure: isFailure,
    homeRelative: homeRelative,
    remoteLabel: remoteLabel,
    pairTitle: pairTitle,
    relativeTime: relativeTime,
    formatBytes: formatBytes,
    resultDetail: resultDetail,
    tooltip: tooltip
  }
}
