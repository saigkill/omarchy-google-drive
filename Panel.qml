import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Management panel: connect the Google account, add and remove folder pairs,
// sync now, and see what went wrong. Every action goes to the singleton
// service through `hostWidget.svc`; the panel keeps only form state.
Panel {
  id: root
  moduleName: "saigkill.google-drive"
  ipcTarget: "saigkill.google-drive"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null

  readonly property var svc: hostWidget ? hostWidget.svc : null
  readonly property var status: svc ? svc.status : Model.emptyStatus()
  readonly property var pairs: status.pairs
  readonly property string syncState: svc ? svc.syncState : "loading"
  readonly property string syncingId: svc ? svc.syncingId : ""
  readonly property var queue: svc ? svc.queue : []
  readonly property var progress: svc ? svc.progress : null
  readonly property real now: svc ? svc.now : Date.now()
  readonly property string home: Quickshell.env("HOME") || ""

  readonly property color warningColor: "#c9a227"
  readonly property color contentForeground: bar ? bar.barForeground : Color.foreground
  readonly property color dimForeground: Qt.darker(contentForeground, 1.5)
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family

  // ---- form state --------------------------------------------------------
  property bool adding: false
  property string newLocal: ""
  property string newRemote: ""

  // ---- confirmation ------------------------------------------------------
  // One dialog for the three actions that need one: "remove", "logout" and
  // "force" (bypassing bisync's delete safety check).
  property string confirmAction: ""
  property string confirmPairId: ""
  property string confirmMessage: ""

  function open() { root.controller.show() }
  function close() {
    root.adding = false
    root.controller.hide()
  }
  function toggle() {
    if (root.opened) root.close()
    else root.open()
  }
  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.hostWidget || root, direction)
    return false
  }

  function startAdd() {
    root.newLocal = ""
    root.newRemote = ""
    root.adding = true
    if (root.svc) root.svc.actionError = ""
  }

  function saveAdd() {
    if (!root.svc || String(root.newLocal).trim() === "") return
    root.svc.addPair(String(root.newLocal).trim(), String(root.newRemote).trim())
  }

  function pickFolder() {
    if (pickProc.running) return
    pickProc.command = ["omarchy-file-select", "--title", "Folder to sync with Google Drive", "--directory"]
    pickProc.running = true
  }

  function ask(action, pair) {
    root.confirmAction = action
    root.confirmPairId = pair ? pair.id : ""
    if (action === "remove")
      root.confirmMessage = "Stop syncing " + Model.homeRelative(pair.local, root.home)
        + "? No files are deleted, on either side."
    else if (action === "force")
      root.confirmMessage = "Sync " + Model.pairTitle(pair) + " anyway? bisync stopped because many files"
        + " would be deleted. Only continue if those deletions are intended."
    else
      root.confirmMessage = "Disconnect Google Drive? The rclone remote '" + root.status.remote
        + "' is deleted. Your files stay where they are."
    confirmDialog.opened = true
  }

  function confirmed() {
    confirmDialog.opened = false
    if (!root.svc) return
    if (root.confirmAction === "remove") root.svc.removePair(root.confirmPairId)
    else if (root.confirmAction === "force") root.svc.syncPair(root.confirmPairId, { force: true })
    else if (root.confirmAction === "logout") root.svc.logout()
    root.confirmAction = ""
  }

  Connections {
    target: root.svc
    ignoreUnknownSignals: true
    function onPairAdded(id) { root.adding = false }
  }

  function pairColor(pair) {
    if (pair.id === root.syncingId) return Color.accent
    if (!pair.exists) return Color.urgent
    if (!pair.enabled || !pair.result) return root.dimForeground
    if (Model.isFailure(pair.result.state)) return Color.urgent
    if (pair.result.state === "conflict") return root.warningColor
    return root.dimForeground
  }

  function pairDetail(pair) {
    var parts = []
    var r = pair.result
    if (r && r.at) parts.push(Model.relativeTime(r.at, root.now))
    var detail = Model.resultDetail(r)
    if (detail !== "") parts.push(detail)
    return parts.join(" · ")
  }

  Process {
    id: pickProc
    stdout: StdioCollector { id: pickOut; waitForEnd: true }
    onExited: function(exitCode) {
      var path = pickOut.text.trim().split("\n")[0]
      if (exitCode === 0 && path !== "") root.newLocal = path
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(500))
    contentHeight: panel.fittedContentHeight(listColumn.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: !!(root.adding || confirmDialog.opened)
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: listScroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: listColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: listColumn
          width: listScroll.width
          spacing: Style.space(8)

          // ---- header ------------------------------------------------------
          Item {
            width: parent.width
            height: headerRow.height
            implicitHeight: headerRow.height

            Row {
              id: headerRow
              spacing: Style.space(8)

              PanelSectionHeader {
                anchors.verticalCenter: parent.verticalCenter
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                text: "GOOGLE DRIVE"
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: Model.stateLabel(root.syncState)
                color: root.syncState === "error" ? Color.urgent
                  : (root.syncState === "conflict" ? root.warningColor : root.dimForeground)
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.caption
              }
            }

            Row {
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(4)
              visible: root.status.connected

              PanelActionButton {
                iconText: ""
                tooltipText: "Sync all folders now"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: if (root.svc) root.svc.syncAll()
              }

              PanelActionButton {
                iconText: ""
                tooltipText: "Add folder"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: root.startAdd()
              }

              PanelActionButton {
                iconText: ""
                tooltipText: "Edit sync filters"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: if (root.svc) root.svc.openFilters()
              }

              PanelActionButton {
                iconText: ""
                tooltipText: "Open Google Drive in the browser"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: if (root.svc) root.svc.openDrive()
              }

              PanelActionButton {
                iconText: ""
                tooltipText: "Disconnect Google Drive"
                foreground: Qt.darker(root.contentForeground, 1.3)
                fontFamily: root.contentFontFamily
                onClicked: root.ask("logout", null)
              }
            }
          }

          // ---- rclone missing ----------------------------------------------
          Text {
            visible: root.status.loaded && !root.status.rcloneInstalled
            height: visible ? implicitHeight : 0
            width: parent.width
            text: "rclone is not installed. Install it with  sudo pacman -S rclone  and reopen this panel."
            color: Color.urgent
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // ---- connect -----------------------------------------------------
          Column {
            visible: root.status.rcloneInstalled && !root.status.connected
            height: visible ? implicitHeight : 0
            width: parent.width
            spacing: Style.space(8)

            Text {
              width: parent.width
              text: root.status.remoteType !== "" && root.status.remoteType !== "drive"
                ? "The rclone remote '" + root.status.remote + "' exists but is of type "
                  + root.status.remoteType + ". Choose another remote name in the widget settings."
                : (root.svc && root.svc.loginPending
                  ? "Finish the login in your browser. rclone is waiting for Google's answer."
                  : "Connect a Google account. Your browser opens Google's consent page; the token is stored by rclone, not by this plugin.")
              color: root.contentForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            Row {
              spacing: Style.space(10)

              Button {
                visible: !(root.svc && root.svc.loginPending)
                text: "Connect Google account"
                bordered: true
                focusable: true
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: if (root.svc) root.svc.login()
              }

              Button {
                visible: !!(root.svc && root.svc.loginPending)
                text: "Cancel login"
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: if (root.svc) root.svc.cancelLogin()
              }
            }

            Text {
              visible: !!(root.svc && root.svc.loginError !== "")
              height: visible ? implicitHeight : 0
              width: parent.width
              text: root.svc ? root.svc.loginError : ""
              color: Color.urgent
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // ---- action error ------------------------------------------------
          Text {
            visible: !!(root.svc && root.svc.actionError !== "")
            height: visible ? implicitHeight : 0
            width: parent.width
            text: root.svc ? root.svc.actionError : ""
            color: Color.urgent
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          // ---- add form ----------------------------------------------------
          Rectangle {
            visible: root.adding && root.status.connected
            width: parent.width
            height: visible ? addColumn.implicitHeight + Style.space(14) : 0
            implicitHeight: visible ? addColumn.implicitHeight + Style.space(14) : 0
            radius: Style.cornerRadius
            color: Style.controlFill(false, false, root.contentForeground, Color.accent)

            Column {
              id: addColumn
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.leftMargin: Style.space(12)
              anchors.rightMargin: Style.space(12)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(8)

              Row {
                width: parent.width
                spacing: Style.space(8)

                TextField {
                  id: localField
                  width: parent.width - browseButton.width - parent.spacing
                  foreground: root.contentForeground
                  text: root.newLocal
                  placeholderText: "Local folder, e.g. ~/Documents/Drive"
                  onTextChanged: root.newLocal = text
                }

                Button {
                  id: browseButton
                  anchors.verticalCenter: parent.verticalCenter
                  text: "Browse…"
                  foreground: root.contentForeground
                  fontFamily: root.contentFontFamily
                  onClicked: root.pickFolder()
                }
              }

              TextField {
                width: parent.width
                foreground: root.contentForeground
                text: root.newRemote
                placeholderText: "Folder on Drive, e.g. Documents (empty: all of My Drive)"
                onTextChanged: root.newRemote = text
              }

              Text {
                width: parent.width
                text: "The first sync merges both folders: nothing is deleted, and where a file differs the newer copy wins."
                color: root.dimForeground
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }

              Row {
                spacing: Style.space(10)

                Button {
                  text: root.svc && root.svc.busy ? "Adding…" : "Add and sync"
                  bordered: true
                  focusable: true
                  foreground: root.contentForeground
                  fontFamily: root.contentFontFamily
                  onClicked: root.saveAdd()
                }

                Button {
                  text: "Cancel"
                  foreground: root.contentForeground
                  fontFamily: root.contentFontFamily
                  onClicked: root.adding = false
                }
              }
            }
          }

          PanelSeparator {
            visible: root.status.connected
            foreground: root.contentForeground
          }

          // ---- empty state -------------------------------------------------
          Text {
            visible: root.status.connected && root.pairs.length === 0 && !root.adding
            height: visible ? implicitHeight : 0
            width: parent.width
            text: "No folders yet. Use the + button to pair a local folder with a folder on Drive."
            color: root.dimForeground
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // ---- pairs -------------------------------------------------------
          Repeater {
            model: root.status.connected ? root.pairs : []

            Item {
              id: pairItem
              required property var modelData
              readonly property var result: modelData.result
              readonly property bool syncing: modelData.id === root.syncingId
              readonly property var recent: Model.recentUploads(modelData, root.syncingId, root.progress)
              width: parent.width
              height: pairRow.implicitHeight
              implicitHeight: pairRow.implicitHeight

              Row {
                id: pairRow
                anchors.left: parent.left
                anchors.right: parent.right
                spacing: Style.space(10)

                Column {
                  width: parent.width - pairButtons.width - parent.spacing
                  spacing: Style.space(2)

                  Text {
                    width: parent.width
                    text: Model.pairTitle(pairItem.modelData)
                    color: pairItem.modelData.enabled ? root.contentForeground : root.dimForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                    elide: Text.ElideRight
                  }

                  Text {
                    width: parent.width
                    text: Model.homeRelative(pairItem.modelData.local, root.home) + "  ⇄  "
                      + Model.remoteLabel(pairItem.modelData.remote)
                    color: root.dimForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideMiddle
                  }

                  Text {
                    width: parent.width
                    text: Model.pairStateLabel(pairItem.modelData, root.syncingId, root.queue, root.progress)
                    color: root.pairColor(pairItem.modelData)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                  }

                  Text {
                    visible: text !== ""
                    height: visible ? implicitHeight : 0
                    width: parent.width
                    text: pairItem.syncing ? "" : root.pairDetail(pairItem.modelData)
                    color: Qt.darker(root.contentForeground, 1.7)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }

                  // The last files that went up to Drive, newest first.
                  Text {
                    visible: pairItem.recent.length > 0
                    height: visible ? implicitHeight : 0
                    width: parent.width
                    text: "Recently uploaded"
                    color: root.dimForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Repeater {
                    model: pairItem.recent

                    Text {
                      required property var modelData
                      width: parent.width
                      text: "  " + modelData
                      color: Qt.darker(root.contentForeground, 1.7)
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideMiddle
                    }
                  }

                  // Conflicts are listed by name, so they can be found and
                  // merged by hand. bisync keeps both versions side by side.
                  Repeater {
                    model: pairItem.result && pairItem.result.state === "conflict"
                      ? pairItem.result.conflicts.slice(0, 5) : []

                    Text {
                      required property var modelData
                      width: parent.width
                      text: "  " + modelData
                      color: root.warningColor
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideMiddle
                    }
                  }

                  Text {
                    visible: !!(pairItem.result && pairItem.result.conflictCount > 5)
                    height: visible ? implicitHeight : 0
                    width: parent.width
                    text: pairItem.result ? "  … and " + (pairItem.result.conflictCount - 5) + " more" : ""
                    color: root.warningColor
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Row {
                    visible: !!(pairItem.result && !pairItem.syncing
                      && (pairItem.result.state === "needs-resync" || pairItem.result.state === "safety-abort"))
                    height: visible ? implicitHeight : 0
                    spacing: Style.space(10)

                    Button {
                      visible: !!(pairItem.result && pairItem.result.state === "needs-resync")
                      text: "Resync"
                      bordered: true
                      foreground: root.contentForeground
                      fontFamily: root.contentFontFamily
                      onClicked: if (root.svc) root.svc.syncPair(pairItem.modelData.id, { resync: true })
                    }

                    Button {
                      visible: !!(pairItem.result && pairItem.result.state === "safety-abort")
                      text: "Sync anyway"
                      bordered: true
                      foreground: root.contentForeground
                      fontFamily: root.contentFontFamily
                      onClicked: root.ask("force", pairItem.modelData)
                    }
                  }
                }

                Row {
                  id: pairButtons
                  spacing: Style.space(4)

                  PanelActionButton {
                    iconText: ""
                    tooltipText: "Sync now"
                    foreground: pairItem.syncing ? Color.accent : root.contentForeground
                    fontFamily: root.contentFontFamily
                    onClicked: if (root.svc) root.svc.syncPair(pairItem.modelData.id, {})
                  }

                  PanelActionButton {
                    iconText: pairItem.modelData.enabled ? "" : ""
                    tooltipText: pairItem.modelData.enabled ? "Pause" : "Resume"
                    foreground: root.contentForeground
                    fontFamily: root.contentFontFamily
                    onClicked: if (root.svc) root.svc.setEnabled(pairItem.modelData.id, !pairItem.modelData.enabled)
                  }

                  PanelActionButton {
                    iconText: ""
                    tooltipText: "Open local folder"
                    foreground: root.contentForeground
                    fontFamily: root.contentFontFamily
                    onClicked: if (root.svc) root.svc.openFolder(pairItem.modelData.local)
                  }

                  PanelActionButton {
                    iconText: ""
                    tooltipText: "Stop syncing this folder"
                    foreground: Qt.darker(root.contentForeground, 1.3)
                    fontFamily: root.contentFontFamily
                    onClicked: root.ask("remove", pairItem.modelData)
                  }
                }
              }
            }
          }

          // ---- footer ------------------------------------------------------
          Text {
            visible: root.status.connected && root.status.rcloneVersion !== ""
            height: visible ? implicitHeight : 0
            width: parent.width
            text: "rclone " + root.status.rcloneVersion + " · remote '" + root.status.remote + "'"
              + (root.svc && root.svc.watching ? " · watching for changes" : "")
            color: Qt.darker(root.contentForeground, 1.8)
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }
      }
    }
  }

  ConfirmDialog {
    id: confirmDialog
    anchors.fill: parent
    message: root.confirmMessage
    confirmText: root.confirmAction === "force" ? "Sync anyway"
      : (root.confirmAction === "logout" ? "Disconnect" : "Stop syncing")
    foreground: root.contentForeground
    fontFamily: root.contentFontFamily
    onConfirmed: root.confirmed()
    onCanceled: {
      root.confirmAction = ""
      confirmDialog.opened = false
    }
  }
}
