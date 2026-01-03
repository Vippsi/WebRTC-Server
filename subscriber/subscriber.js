const logEl = document.getElementById("log");
const log = (...a) => (logEl.textContent += a.join(" ") + "\n");

const WS_URL = `ws://${location.hostname}:8080`;

const pc = new RTCPeerConnection({
  iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
});

let dc = null;

pc.ontrack = (ev) => {
  const [stream] = ev.streams;
  document.getElementById("v").srcObject = stream;
  log("track:", ev.track.kind);
};

pc.ondatachannel = (ev) => {
  dc = ev.channel;
  dc.onopen = () => log("datachannel open:", dc.label);
  dc.onmessage = (m) => log("dc msg:", m.data);
};

pc.onicecandidate = (ev) => {
  if (ev.candidate) ws.send(JSON.stringify({ type: "candidate", candidate: ev.candidate }));
};

const ws = new WebSocket(WS_URL);

ws.onopen = () => {
  ws.send(JSON.stringify({ type: "hello", role: "subscriber" }));
  log("ws connected");
};

ws.onmessage = async (ev) => {
  const msg = JSON.parse(ev.data);

  if (msg.type === "offer") {
    log("got offer");
    await pc.setRemoteDescription(msg.sdp);
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    ws.send(JSON.stringify({ type: "answer", sdp: pc.localDescription }));
    log("sent answer");
  } else if (msg.type === "candidate") {
    await pc.addIceCandidate(msg.candidate);
  }
};

// quick control tests
function sendControl(obj) {
  if (!dc || dc.readyState !== "open") return log("dc not open");
  dc.send(JSON.stringify(obj));
}

document.getElementById("tap").onclick = () => sendControl({ type: "tap", x: 100, y: 100 });
document.getElementById("text").onclick = () => sendControl({ type: "text", text: "hello" });
document.getElementById("enter").onclick = () => sendControl({ type: "key", code: "ENTER" });


// --- Control wiring: keyboard -> WS "control" messages ---

/**
 * Maps browser keyboard events to our generic control keys.
 * Generic keys are what the publisher/roku.py expects.
 */
function mapKeyToControl(e) {
    // Prefer e.key for arrows/enter/backspace; use e.code for some keyboards if needed
    switch (e.key) {
      case "ArrowUp": return "UP";
      case "ArrowDown": return "DOWN";
      case "ArrowLeft": return "LEFT";
      case "ArrowRight": return "RIGHT";
      case "Enter": return "ENTER";
      case "Backspace": return "BACK";
      case "Home": return "HOME";
      default:
        return null;
    }
  }
  
  /**
   * Send a control payload to the signaling server.
   * Server will forward it to the publisher.
   */
  function sendControl(payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "control", payload }));
  }
  
  // Optional: show a small hint in console
  console.log("Control enabled: arrows, Enter, Backspace, Home");
  
  // Keydown handler
  window.addEventListener("keydown", (e) => {
    // Avoid spamming when holding keys
    if (e.repeat) return;
  
    const key = mapKeyToControl(e);
    if (!key) return;
  
    // Prevent the browser from scrolling the page on arrow keys/backspace
    e.preventDefault();
  
    sendControl({ kind: "key", key });
  }, { passive: false });
  