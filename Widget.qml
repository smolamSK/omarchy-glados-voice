import QtQuick
import qs.Ui

// A voice in the bar: click to mute or unmute GLaDOS. The reader itself lives
// in Service.qml; this only shows its state and hands it the settings.
//
// Glyphs are \u escapes (U+F05CB account-voice, U+F0ED4 account-voice-off) so
// the source survives editors that mangle private-use codepoints.
BarWidget {
  id: root
  moduleName: "io.github.smolamsk.glados-voice"

  readonly property bool readBody: setting("readBody", true)
  readonly property int maxChars: setting("maxChars", 250)
  readonly property string ignoreApps: setting("ignoreApps", "")
  readonly property bool readWhenLocked: setting("readWhenLocked", false)

  property var voice: null
  readonly property bool muted: voice ? voice.muted : false
  readonly property string phase: voice ? voice.phase : "starting"

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function bindVoice() {
    if (!voice) {
      var host = bar && bar.shell ? bar.shell : null
      if (!host || typeof host.serviceFor !== "function") return
      voice = host.serviceFor("io.github.smolamsk.glados-voice") || null
    }
    pushSettings()
  }

  function pushSettings() {
    if (!voice) return
    voice.applySettings({
      readBody: readBody,
      maxChars: maxChars,
      ignoreApps: ignoreApps,
      readWhenLocked: readWhenLocked
    })
  }

  onBarChanged: bindVoice()
  onReadBodyChanged: pushSettings()
  onMaxCharsChanged: pushSettings()
  onIgnoreAppsChanged: pushSettings()
  onReadWhenLockedChanged: pushSettings()

  Timer {
    interval: 200
    running: root.voice === null
    repeat: true
    onTriggered: root.bindVoice()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.muted ? "\uDB83\uDED4" : "\uDB81\uDDCB"
    dimmed: root.muted || root.phase !== "ready"
    tooltipText: {
      if (root.phase === "bootstrapping") return "GLaDOS: downloading voice…"
      if (root.phase.indexOf("error") === 0) return "GLaDOS: " + root.phase.slice(6)
      if (root.phase !== "ready") return "GLaDOS: starting…"
      return root.muted ? "GLaDOS: muted" : "GLaDOS: reading notifications"
    }
    onPressed: function(b) {
      if (root.voice) root.voice.setMuted(!root.muted)
    }
  }
}
