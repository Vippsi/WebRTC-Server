import { WebSocketServer } from "ws";

const wss = new WebSocketServer({ port: 8080 });

// roles: publisher, subscriber
const peers = new Map(); // role -> ws

function safeSend(ws, msg) {
  if (ws && ws.readyState === ws.OPEN) ws.send(JSON.stringify(msg));
}

function otherRole(role) {
  return role === "publisher" ? "subscriber" : "publisher";
}

wss.on("connection", (ws) => {
  let role = null;

  ws.on("message", (data) => {
    let msg;
    try {
      msg = JSON.parse(data.toString());
    } catch {
      return;
    }

    // 1) Register role (publisher/subscriber)
    if (msg.type === "hello") {
      if (msg.role !== "publisher" && msg.role !== "subscriber") {
        safeSend(ws, { type: "hello", ok: false, error: "invalid role" });
        return;
      }

      role = msg.role;
      peers.set(role, ws);
      safeSend(ws, { type: "hello", ok: true, role });
      return;
    }

    // Ignore anything until role is set
    if (!role) {
      safeSend(ws, { type: "error", error: "must send hello first" });
      return;
    }

    // 2) CONTROL messages (explicit direction + explicit shape)
    // Subscriber -> Publisher:
    // { type: "control", payload: { kind: "key", key: "UP" } }
    if (msg.type === "control") {
      if (role !== "subscriber") {
        safeSend(ws, { type: "error", error: "only subscriber can send control" });
        return;
      }

      const target = peers.get("publisher");
      safeSend(target, {
        type: "control",
        from: "subscriber",
        to: "publisher",
        payload: msg.payload ?? null,
      });
      return;
    }

    // (Optional) Publisher -> Subscriber status/acks/logs:
    // { type: "control-status", payload: { ok: true } }
    if (msg.type === "control-status") {
      if (role !== "publisher") {
        safeSend(ws, { type: "error", error: "only publisher can send control-status" });
        return;
      }

      const target = peers.get("subscriber");
      safeSend(target, {
        type: "control-status",
        from: "publisher",
        to: "subscriber",
        payload: msg.payload ?? null,
      });
      return;
    }

    // 3) Default behavior: forward signaling messages to the opposite role
    // (offer/answer/candidate/etc)
    const targetRole = otherRole(role);
    const target = peers.get(targetRole);
    safeSend(target, msg);
  });

  ws.on("close", () => {
    if (role && peers.get(role) === ws) peers.delete(role);
  });
});

console.log("WS signaling relay listening on ws://0.0.0.0:8080");
