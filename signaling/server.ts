// signaling/server.ts
import { WebSocketServer, WebSocket } from "ws";
import type { IncomingMessage } from "http";

type Role = "publisher" | "subscriber";

type HelloMsg = { type: "hello"; role: Role };
type ControlMsg = { type: "control"; payload?: unknown };
type ControlStatusMsg = { type: "control-status"; payload?: unknown };

// pass-through for SDP/ICE/etc
type AnyMsg = { type?: string; [k: string]: unknown };

type OutMsg =
  | { type: "hello"; ok: boolean; role?: Role; error?: string }
  | { type: "error"; error: string }
  | { type: "info"; message: string }
  | { type: "peer"; event: "connected" | "disconnected"; role: Role }
  | (AnyMsg & { from?: Role; to?: Role });

const PORT = Number(process.env.PORT ?? 8080);
const wss = new WebSocketServer({ port: PORT });

// role -> ws
const peers = new Map<Role, WebSocket>();

function safeSend(ws: WebSocket | undefined, msg: OutMsg): void {
  if (!ws) return;
  if (ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(msg));
}

function otherRole(role: Role): Role {
  return role === "publisher" ? "subscriber" : "publisher";
}

function isRole(x: unknown): x is Role {
  return x === "publisher" || x === "subscriber";
}

function takeRole(role: Role, ws: WebSocket): void {
  // if someone else already holds this role, kick them
  const existing = peers.get(role);
  if (existing && existing !== ws) {
    try {
      safeSend(existing, { type: "error", error: `role '${role}' taken; disconnecting` });
      existing.close(1011, "role taken");
    } catch {
      // ignore
    }
  }
  peers.set(role, ws);
}

function parseJson(data: WebSocket.RawData): AnyMsg | null {
  try {
    return JSON.parse(data.toString()) as AnyMsg;
  } catch {
    return null;
  }
}

wss.on("connection", (ws: WebSocket, _req: IncomingMessage) => {
  let role: Role | null = null;

  safeSend(ws, {
    type: "info",
    message: "connected; send {type:'hello', role:'publisher'|'subscriber'}",
  });

  ws.on("message", (data) => {
    const msg = parseJson(data);
    if (!msg) {
      safeSend(ws, { type: "error", error: "invalid json" });
      return;
    }

    // 1) Role registration
    if (msg.type === "hello") {
      const hello = msg as Partial<HelloMsg>;
      if (!isRole(hello.role)) {
        safeSend(ws, { type: "hello", ok: false, error: "invalid role" });
        return;
      }

      role = hello.role;
      takeRole(role, ws);

      safeSend(ws, { type: "hello", ok: true, role });

      // optional: notify the other side
      safeSend(peers.get(otherRole(role)), { type: "peer", event: "connected", role });

      return;
    }

    if (!role) {
      safeSend(ws, { type: "error", error: "must send hello first" });
      return;
    }

    // 2) Control (subscriber -> publisher)
    if (msg.type === "control") {
      if (role !== "subscriber") {
        safeSend(ws, { type: "error", error: "only subscriber can send control" });
        return;
      }

      const target = peers.get("publisher");
      if (!target) {
        safeSend(ws, { type: "error", error: "no publisher connected" });
        return;
      }

      const m = msg as ControlMsg;
      safeSend(target, {
        type: "control",
        from: "subscriber",
        to: "publisher",
        payload: m.payload ?? null,
      });

      return;
    }

    // 3) Control-status (publisher -> subscriber)
    if (msg.type === "control-status") {
      if (role !== "publisher") {
        safeSend(ws, { type: "error", error: "only publisher can send control-status" });
        return;
      }

      const target = peers.get("subscriber");
      if (!target) return;

      const m = msg as ControlStatusMsg;
      safeSend(target, {
        type: "control-status",
        from: "publisher",
        to: "subscriber",
        payload: m.payload ?? null,
      });

      return;
    }

    // 4) Default: forward everything else to the opposite role
    const target = peers.get(otherRole(role));
    if (!target) return;

    safeSend(target, msg as OutMsg);
  });

  ws.on("close", () => {
    if (role && peers.get(role) === ws) {
      peers.delete(role);
      safeSend(peers.get(otherRole(role)), { type: "peer", event: "disconnected", role });
    }
  });

  ws.on("error", () => {
    // close handler will clean up
  });
});

console.log(`WS signaling relay listening on ws://0.0.0.0:${PORT}`);
