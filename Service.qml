import QtQuick
import QtQml
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// Headless singleton. Owns the sync schedule, the file watchers and the one
// sync queue. The shell creates a bar widget per monitor; if each of them ran
// its own timer and watchers, every folder would be synced once per screen and
// two bisync runs of the same pair would fight over its lock. The widgets and
// the panel are views over this object and call into it.
Item {
  id: root

  // Injected by shell.qml after createObject() returns, see bootstrapTimer.
  property var shell: null
  property var manifest: null
  property var pluginRegistry: null
  property string omarchyPath: ""

  readonly property string moduleId: "saigkill.google-drive"
  property string helperPath: ""
  readonly property string home: Quickshell.env("HOME") || ""

  // ---- settings mirrored from the bar entry -------------------------------
  property var config: Model.parseSettings(null)
  property bool settingsApplied: false

  // ---- state ----------------------------------------------------------------
  property var status: Model.emptyStatus()
  property var queue: []
  property string syncingId: ""
  property var syncingJob: null
  // Live progress of the running sync, from the helper's progress lines.
  property var progress: null
  property string syncResultLine: ""
  property var lastFinished: ({})     // pair id -> ms, for the watcher grace window
  property var dirty: ({})            // pair ids changed locally since the last debounce
  property bool startupSyncDone: false
  property real now: Date.now()

  property bool loginPending: false
  property string loginError: ""
  readonly property bool busy: addProc.running || removeProc.running || enabledProc.running || logoutProc.running
  property string actionError: ""

  // Not `state`: that name belongs to Item's state machine.
  readonly property string syncState: Model.overallState(root.status, root.syncingId, root.queue.length)
  readonly property bool watching: root.config.watchChanges && root.status.connected

  // The watcher model is a list of ids that is only reassigned when the set
  // of ids really changes. Bound straight to status.pairs, it would restart
  // every inotifywait after every sync, because each result replaces the
  // pairs array, and changes made during the restart would go unseen.
  readonly property string watchKey: root.watching
    ? Model.syncablePairs(root.status.pairs).map(function(p) { return p.id }).join(",") : ""
  property var watchIds: []
  onWatchKeyChanged: root.watchIds = root.watchKey === "" ? [] : root.watchKey.split(",")

  signal pairAdded(string id)

  Component.onCompleted: bootstrapTimer.start()

  // `shell` and `manifest` are still null in Component.onCompleted.
  Timer {
    id: bootstrapTimer
    interval: 0
    repeat: false
    onTriggered: root.bootstrap()
  }

  function bootstrap() {
    root.helperPath = root.pluginDir() + "/gdrive_helper.py"
    // A widget that loaded first may already have pushed its settings and
    // triggered a status read before the helper path was known.
    root.refreshStatus()
  }

  // The manifest carries the plugin's own directory; the fallback keeps a
  // hand-installed copy working if that field is ever absent.
  function pluginDir() {
    if (root.manifest && root.manifest.__sourceDir) return String(root.manifest.__sourceDir).replace(/\/$/, "")
    return root.home + "/.config/omarchy/plugins/" + root.moduleId
  }

  // Called by every bar widget instance; only a real change does anything.
  function applySettings(raw) {
    var next = Model.parseSettings(raw)
    var previous = root.config
    if (root.settingsApplied && JSON.stringify(next) === JSON.stringify(previous)) return
    root.config = next
    root.settingsApplied = true
    if (next.remote !== previous.remote) root.refreshStatus()
  }

  // ---- status ---------------------------------------------------------------
  function refreshStatus() {
    if (root.helperPath === "") return
    if (statusProc.running) {
      statusProc.again = true
      return
    }
    statusProc.command = Model.statusCmd(root.helperPath, root.config.remote)
    statusProc.running = true
  }

  function onStatusExited(exitCode) {
    if (exitCode !== 0) {
      console.warn("saigkill.google-drive: status failed: " + statusErr.text.trim())
      return
    }
    var decoded = Model.decodeStatus(Model.parseJson(statusOut.text))
    // A pair that is syncing right now keeps the result the panel already
    // shows; the file on disk still has the previous one.
    root.status = decoded
    if (decoded.connected && !root.startupSyncDone && !startupTimer.running) startupTimer.start()
  }

  Process {
    id: statusProc
    property bool again: false
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    stderr: StdioCollector { id: statusErr; waitForEnd: true }
    onExited: function(exitCode) {
      root.onStatusExited(exitCode)
      if (again) {
        again = false
        root.refreshStatus()
      }
    }
  }

  // ---- sync queue -----------------------------------------------------------
  function syncAll() {
    if (!root.status.connected) return
    var pairs = Model.syncablePairs(root.status.pairs)
    var next = root.queue
    for (var i = 0; i < pairs.length; i++) {
      if (pairs[i].id !== root.syncingId) next = Model.enqueue(next, pairs[i].id, {})
    }
    root.queue = next
    root.pump()
  }

  function syncPair(id, options) {
    if (!root.status.connected || !Model.findPair(root.status.pairs, id)) return
    if (id === root.syncingId && !(options && (options.resync || options.force))) return
    root.queue = Model.enqueue(root.queue, id, options)
    root.pump()
  }

  function pump() {
    if (syncProc.running || root.queue.length === 0) return
    var job = root.queue[0]
    root.queue = root.queue.slice(1)
    var cmd = Model.syncCmd(root.helperPath, root.config.remote, job.id, job)
    if (!cmd || !Model.findPair(root.status.pairs, job.id)) {
      root.pump()
      return
    }
    root.syncingId = job.id
    root.syncingJob = job
    root.progress = null
    root.syncResultLine = ""
    syncProc.command = cmd
    syncProc.running = true
  }

  function onSyncExited(exitCode) {
    var id = root.syncingId
    var parsed = Model.lastJsonLine(root.syncResultLine)
    var result = parsed ? Model.decodeResult(parsed) : {
      state: "error",
      message: syncErr.text.trim().split("\n").pop() || "The sync helper failed",
      at: Date.now(), seconds: 0, resync: false, transfers: 0, deletes: 0, bytes: 0,
      conflictCount: 0, conflicts: []
    }
    var pair = Model.findPair(root.status.pairs, id)
    if (pair && result.state !== "busy") {
      root.notify(Model.notificationFor(pair.result, result, pair))
      var nextStatus = {}
      for (var key in root.status) nextStatus[key] = root.status[key]
      nextStatus.pairs = Model.withResult(root.status.pairs, id, result)
      root.status = nextStatus
    }
    var finished = {}
    for (var k in root.lastFinished) finished[k] = root.lastFinished[k]
    finished[id] = Date.now()
    root.lastFinished = finished
    root.syncingId = ""
    root.syncingJob = null
    root.progress = null
    root.syncResultLine = ""
    root.pump()
  }

  Process {
    id: syncProc
    // Progress lines while bisync runs, the result as the last line.
    stdout: SplitParser {
      onRead: function(line) {
        var progress = Model.decodeProgress(Model.lastJsonLine(line))
        if (progress) root.progress = progress
        else if (line.trim() !== "") root.syncResultLine = line
      }
    }
    stderr: StdioCollector { id: syncErr; waitForEnd: true }
    onExited: function(exitCode) { root.onSyncExited(exitCode) }
  }

  // The first sync after the shell starts picks up whatever changed while it
  // was not running, on either side.
  Timer {
    id: startupTimer
    interval: Model.STARTUP_DELAY_MS
    repeat: false
    onTriggered: {
      root.startupSyncDone = true
      root.syncAll()
    }
  }

  Timer {
    id: intervalTimer
    interval: Math.max(1, root.config.intervalMinutes) * 60 * 1000
    running: root.config.intervalMinutes > 0 && root.status.connected
    repeat: true
    onTriggered: root.syncAll()
  }

  // Relative times in the panel and the tooltip.
  Timer {
    interval: 30000
    running: true
    repeat: true
    onTriggered: root.now = Date.now()
  }

  // ---- watchers -------------------------------------------------------------
  function onLocalChange(id) {
    if (id === root.syncingId) return
    var finished = root.lastFinished[id] || 0
    if (Date.now() - finished < Model.WATCH_GRACE_MS) return
    var next = {}
    for (var k in root.dirty) next[k] = true
    next[id] = true
    root.dirty = next
    debounceTimer.restart()
  }

  // Saving a document is several events in a row, and an editor that writes
  // every few seconds would otherwise sync every few seconds. The timer
  // restarts on each change, so a sync runs once things have gone quiet.
  Timer {
    id: debounceTimer
    interval: root.config.debounceSeconds * 1000
    repeat: false
    onTriggered: {
      var ids = Object.keys(root.dirty)
      root.dirty = ({})
      for (var i = 0; i < ids.length; i++) root.syncPair(ids[i], {})
    }
  }

  // One `gdrive_helper.py watch <id>` per enabled pair. The helper prints a
  // bare "change" line, never a file name.
  Instantiator {
    model: root.watchIds

    delegate: QtObject {
      id: watcher
      required property var modelData
      readonly property string pairId: String(modelData)

      property Process proc: Process {
        command: Model.watchCmd(root.helperPath, watcher.pairId)
        running: root.helperPath !== ""
        stdout: SplitParser {
          onRead: function(line) { if (line === "change") root.onLocalChange(watcher.pairId) }
        }
        stderr: StdioCollector { id: watchErr; waitForEnd: true }
        onExited: function(exitCode) {
          console.warn("saigkill.google-drive: watcher for " + watcher.pairId + " stopped ("
            + exitCode + "): " + watchErr.text.trim())
          watcher.retry.start()
        }
      }

      property Timer retry: Timer {
        interval: Model.WATCH_RETRY_MS
        repeat: false
        onTriggered: watcher.proc.running = true
      }
    }
  }

  // ---- pair management ------------------------------------------------------
  function addPair(local, remote) {
    root.actionError = ""
    addProc.environment = Model.addPairEnv(local, remote)
    addProc.command = Model.addPairCmd(root.helperPath)
    addProc.running = true
  }

  Process {
    id: addProc
    stdout: StdioCollector { id: addOut; waitForEnd: true }
    stderr: StdioCollector { id: addErr; waitForEnd: true }
    onExited: function(exitCode) {
      var pair = Model.parseJson(addOut.text)
      if (exitCode !== 0 || !pair || !Model.isPairId(pair.id)) {
        root.actionError = addErr.text.trim() || "Could not add the folder"
        return
      }
      root.pairAdded(pair.id)
      root.pendingFirstSync = pair.id
      root.refreshStatus()
    }
  }

  // A new pair is synced (that is, resynced: both sides merged) as soon as the
  // status read that knows about it is back.
  property string pendingFirstSync: ""
  onStatusChanged: {
    if (root.pendingFirstSync === "" || !Model.findPair(root.status.pairs, root.pendingFirstSync)) return
    var id = root.pendingFirstSync
    root.pendingFirstSync = ""
    root.syncPair(id, {})
  }

  function removePair(id) {
    if (id === root.syncingId) {
      root.actionError = "Wait for the running sync of this folder to finish"
      return
    }
    var cmd = Model.removePairCmd(root.helperPath, id)
    if (!cmd) return
    root.actionError = ""
    root.queue = Model.dropFromQueue(root.queue, id)
    removeProc.command = cmd
    removeProc.running = true
  }

  Process {
    id: removeProc
    stderr: StdioCollector { id: removeErr; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) root.actionError = removeErr.text.trim() || "Could not remove the folder"
      root.refreshStatus()
    }
  }

  function setEnabled(id, enabled) {
    var cmd = Model.setEnabledCmd(root.helperPath, id, enabled)
    if (!cmd) return
    root.actionError = ""
    if (!enabled) root.queue = Model.dropFromQueue(root.queue, id)
    enabledProc.command = cmd
    enabledProc.running = true
  }

  Process {
    id: enabledProc
    stderr: StdioCollector { id: enabledErr; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) root.actionError = enabledErr.text.trim() || "Could not change the folder"
      root.refreshStatus()
    }
  }

  // ---- account --------------------------------------------------------------
  // rclone opens the browser and waits for Google's redirect itself; this only
  // waits for it to finish. The token never passes through here.
  function login() {
    if (loginProc.running) return
    root.loginError = ""
    root.loginPending = true
    loginProc.command = Model.loginCmd(root.helperPath, root.config.remote)
    loginProc.running = true
  }

  function cancelLogin() {
    if (loginProc.running) {
      loginProc.cancelled = true
      loginProc.running = false
    }
  }

  Process {
    id: loginProc
    property bool cancelled: false
    stdout: StdioCollector { id: loginOut; waitForEnd: true }
    stderr: StdioCollector { id: loginErr; waitForEnd: true }
    onExited: function(exitCode) {
      root.loginPending = false
      if (cancelled) {
        cancelled = false
        root.loginError = "Login cancelled"
      } else if (exitCode !== 0) {
        root.loginError = loginErr.text.trim() || "Login failed"
      }
      root.refreshStatus()
    }
  }

  function logout() {
    root.queue = []
    root.dirty = ({})
    root.startupSyncDone = true
    logoutProc.command = Model.logoutCmd(root.helperPath, root.config.remote)
    logoutProc.running = true
  }

  Process {
    id: logoutProc
    stderr: StdioCollector { id: logoutErr; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) root.actionError = logoutErr.text.trim() || "Could not disconnect"
      root.refreshStatus()
    }
  }

  // ---- misc -----------------------------------------------------------------
  function notify(n) {
    if (!n || !root.config.notify) return
    // U+F02B6 is nf-md-google_drive, written as a surrogate pair.
    Quickshell.execDetached(["omarchy-notification-send", "-g", "󰊶", "-u", n.urgency, n.title, n.body])
  }

  function openFolder(path) {
    var cmd = Model.openFolderCmd(path)
    if (cmd) Quickshell.execDetached(cmd)
  }

  function openDrive() {
    Qt.openUrlExternally(Model.DRIVE_WEB_URL)
  }

  function openFilters() {
    if (root.status.filtersFile !== "") Quickshell.execDetached(["xdg-open", root.status.filtersFile])
  }
}
