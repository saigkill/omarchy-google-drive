import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Google Drive bar widget: a Drive icon whose color says how the sync is
// doing, and the host for the panel. One of these exists per monitor, so it
// never syncs anything itself; Service.qml owns the timer, the watchers and
// the queue, and this is a view over it.
BarWidget {
  id: root
  moduleName: "saigkill.google-drive"

  // shell._services is reassigned wholesale, so reading it in a binding
  // registers a dependency and this re-fires when the service instantiates.
  readonly property var svc: (bar && bar.shell && typeof bar.shell.serviceFor === "function")
    ? bar.shell.serviceFor(moduleName) : null

  readonly property string syncState: svc ? svc.syncState : "loading"
  readonly property color warningColor: "#c9a227"

  readonly property color stateColor: {
    var normal = bar ? bar.barForeground : Color.foreground
    switch (root.syncState) {
      case "error":
      case "missing":
      case "disconnected": return Color.urgent
      case "conflict": return root.warningColor
      case "syncing": return Color.accent
      case "paused":
      case "empty":
      case "loading": return Qt.darker(normal, 1.6)
      default: return normal
    }
  }

  readonly property string tooltipText: svc
    ? Model.tooltip(svc.status, root.syncState, svc.syncingId, svc.now)
    : "Google Drive: starting"

  // Push bar-entry settings down into the singleton service, from whichever
  // widget instance loads first; the service ignores redundant applies.
  onSvcChanged: pushSettings()
  onSettingsChanged: {
    pushSettings()
    injectPanel()
  }
  function pushSettings() {
    if (!svc || typeof svc.applySettings !== "function") return
    svc.applySettings({
      remote: setting("remote", Model.DEFAULT_REMOTE),
      intervalMinutes: setting("intervalMinutes", Model.DEFAULT_INTERVAL_MINUTES),
      watchChanges: setting("watchChanges", true),
      debounceSeconds: setting("debounceSeconds", Model.DEFAULT_DEBOUNCE_SECONDS),
      notify: setting("notify", true)
    })
  }

  function syncNow() { if (svc) svc.syncAll() }

  // ---- panel lifecycle (shape contract for shell summon/hide/toggle) ---
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  IpcHandler {
    target: "saigkill.google-drive"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function sync(): void { root.syncNow() }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // U+F02B6 is nf-md-google_drive, written as a surrogate pair because the
    // codepoint lies outside the BMP. The Font Awesome brand glyph U+F3AA is
    // not part of JetBrainsMono Nerd Font.
    text: "󰊶"
    slotSize: Style.bar.statusSlot
    fontSize: Style.font.caption
    active: root.syncState === "error"
    enabled: true
    foreground: root.stateColor
    tooltipText: root.tooltipText

    onPressed: function(mouseButton) {
      if (mouseButton === Qt.LeftButton) root.toggle()
      else if (mouseButton === Qt.MiddleButton) root.syncNow()
    }
  }
}
