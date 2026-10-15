import QtQuick
import Quickshell
import qs.Ui

// Small bar button. The count is the only thing it shows. A click opens
// the terminal screen.
BarWidget {
  id: root
  moduleName: "io.github.j-c-m.tmsg"

  readonly property var svc: (bar && bar.shell && bar.shell.serviceFor)
    ? bar.shell.serviceFor(moduleName) : null
  readonly property int unread: svc ? svc.unread : 0

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.unread > 0 ? ("t💬" + root.unread) : "t💬"
    active: root.unread > 0
    tooltipText: root.unread > 0 ? (root.unread + " unread") : "Open tMsg"
    onPressed: function(buttonCode) {
      if (buttonCode !== Qt.LeftButton)
        return
      if (root.svc && root.svc.launchTui)
        root.svc.launchTui("")
    }
  }
}
