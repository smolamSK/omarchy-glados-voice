import QtQuick
import Quickshell
import Quickshell.Io

// Owns the one reader process for the whole shell. Omarchy builds a bar per
// monitor, so the process cannot live on the widget or every screen would
// read each notification again.
//
// `bin/glados-voice run` downloads the voice on first use, then reads
// notifications until this service is torn down (plugin disabled or shell
// stopped), which stops the process with it.
Item {
  id: root
  width: 0
  height: 0
  visible: false

  property var shell: null
  property var manifest: null
  property var pluginRegistry: null
  property var barWidgetRegistry: null
  property string omarchyPath: ""

  // Reader phase: "starting", "bootstrapping", "ready", "restarting" or "error <reason>".
  property string phase: "starting"
  property bool muted: false

  readonly property string stateDir:
    (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME") + "/.local/state") + "/glados-voice"
  readonly property string script:
    Qt.resolvedUrl("bin/glados-voice").toString().replace(/^file:\/\//, "")

  property var pendingSettings: null
  property var pendingMuted: null
  property int restartDelay: 30000

  // Set by the reader's first status line, which it prints once it has made
  // the state directory private. Nothing is written there before that:
  // FileView would create the directory itself, with the shell's looser umask.
  property bool stateReady: false

  function setMuted(value) {
    if (!root.stateReady) {
      root.pendingMuted = value
      return
    }
    // The icon changes once the file is saved (onSaved), so it never shows
    // muted while the reader still reads 0.
    mutedFile.setText(value ? "1\n" : "0\n")
  }

  function applySettings(values) {
    root.pendingSettings = values
    if (root.phase === "ready") flushSettings()
  }

  function flushSettings() {
    if (root.pendingSettings === null) return
    settingsFile.setText(JSON.stringify(root.pendingSettings, null, 2) + "\n")
    root.pendingSettings = null
  }

  Process {
    id: reader
    command: [root.script, "run"]
    running: true
    stdout: SplitParser {
      onRead: function(line) {
        if (line.indexOf("@status ") !== 0) return
        if (!root.stateReady) {
          root.stateReady = true
          if (root.pendingMuted !== null) root.setMuted(root.pendingMuted)
          root.pendingMuted = null
        }
        root.phase = line.slice(8)
        if (root.phase === "ready") {
          root.restartDelay = 30000
          mutedFile.reload()
          root.flushSettings()
        }
      }
    }
    onExited: function(code) {
      // 3: a dependency is missing; retrying will not help until it is installed.
      if (code === 3) return
      if (root.phase.indexOf("error") !== 0) root.phase = "restarting"
      restart.interval = root.restartDelay
      root.restartDelay = Math.min(root.restartDelay * 2, 600000)
      restart.restart()
    }
  }

  Timer {
    id: restart
    onTriggered: if (!reader.running) reader.running = true
  }

  FileView {
    id: mutedFile
    path: root.stateDir + "/muted"
    watchChanges: true
    printErrors: false
    onLoaded: root.muted = text().trim() === "1"
    onFileChanged: reload()
    onSaved: root.muted = text().trim() === "1"
    onSaveFailed: console.warn("GLaDOS Voice: could not save the mute state")
  }

  FileView {
    id: settingsFile
    path: root.stateDir + "/settings.json"
    printErrors: false
  }
}
