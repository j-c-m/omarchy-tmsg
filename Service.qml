import QtQuick
import Quickshell
import Quickshell.Io

// Headless half of the plugin. It stays loaded so a new text can still
// arrive while the terminal screen is closed. The Bluetooth work happens
// in messagesd, a separate program started with an argument list (no shell).
Item {
  id: root

  property var shell: null
  property var manifest: null
  property string omarchyPath: ""

  property int unread: 0

  function fileUrl(relative) {
    var url = Qt.resolvedUrl(relative).toString()
    if (url.indexOf("file://") === 0)
      url = url.substring(7)
    return decodeURIComponent(url)
  }

  function launchTui(thread) {
    var args = [root.fileUrl("bin/tmsg")]
    if (thread)
      args.push("--thread", String(thread))
    Quickshell.execDetached(args)
  }

  function applyStatus(status) {
    if (!status)
      return
    root.unread = Number(status.unread || 0)
  }

  function handleLine(line) {
    var text = String(line || "").trim()
    if (!text)
      return
    var event
    try {
      event = JSON.parse(text)
    } catch (ignore) {
      return
    }
    if (!event || !event.event)
      return
    if (event.event === "ready" || event.event === "status")
      root.applyStatus(event.status)
  }

  Process {
    id: bridge
    stdinEnabled: true
    command: ["setpriv", "--pdeathsig", "TERM", "python3", "-u", root.fileUrl("daemon/messagesd.py")]
    stdout: SplitParser {
      onRead: function(data) { root.handleLine(data) }
    }
    // Stderr is connection state only. It is drained so the pipe cannot fill
    // and stall the daemon.
    stderr: SplitParser {
      onRead: function(data) {}
    }
    onExited: restartTimer.restart()
  }

  Timer {
    id: restartTimer
    interval: 1500
    repeat: false
    onTriggered: bridge.running = true
  }

  Component.onCompleted: bridge.running = true
}
