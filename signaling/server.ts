// signaling/server.ts
import { WebSocketServer, WebSocket } from 'ws';
import type { IncomingMessage } from 'http';

type Role = 'publisher' | 'subscriber';

type HelloMsg = { type: 'hello'; role: Role };
type ControlMsg = { type: 'control'; payload?: unknown };
type ControlStatusMsg = { type: 'control-status'; payload?: unknown };
type ViewerReadyMsg = { type: 'viewer-ready' };

// pass-through for SDP/ICE/etc
type AnyMsg = { type?: string; [k: string]: unknown };

type OutMsg =
  | {
      type: 'hello';
      ok: boolean;
      role?: Role;
      error?: string;
      subscriberId?: string;
    }
  | { type: 'error'; error: string }
  | { type: 'info'; message: string }
  | {
      type: 'peer';
      event: 'connected' | 'disconnected';
      role: Role;
      subscriberId?: string;
    }
  | (AnyMsg & { from?: Role; to?: Role; subscriberId?: string });

const PORT = Number(process.env.PORT ?? 8080);
const wss = new WebSocketServer({ port: PORT });

// publisher: single WebSocket
let publisher: WebSocket | null = null;

// subscribers: Map<subscriberId, WebSocket>
const subscribers = new Map<string, WebSocket>();

// ws -> subscriberId mapping (for cleanup)
const wsToSubscriberId = new WeakMap<WebSocket, string>();

// Generate unique subscriber ID
let subscriberIdCounter = 0;
function generateSubscriberId(): string {
  return `subscriber-${Date.now()}-${++subscriberIdCounter}`;
}

function safeSend(ws: WebSocket | null | undefined, msg: OutMsg): void {
  if (!ws) return;
  if (ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(msg));
}

function isRole(x: unknown): x is Role {
  return x === 'publisher' || x === 'subscriber';
}

function parseJson(data: WebSocket.RawData): AnyMsg | null {
  try {
    return JSON.parse(data.toString()) as AnyMsg;
  } catch {
    return null;
  }
}

wss.on('connection', (ws: WebSocket, _req: IncomingMessage) => {
  let role: Role | null = null;
  let subscriberId: string | null = null;

  safeSend(ws, {
    type: 'info',
    message: "connected; send {type:'hello', role:'publisher'|'subscriber'}",
  });

  ws.on('message', (data) => {
    const msg = parseJson(data);
    if (!msg) {
      safeSend(ws, { type: 'error', error: 'invalid json' });
      return;
    }

    // 1) Role registration
    if (msg.type === 'hello') {
      const hello = msg as Partial<HelloMsg>;
      if (!isRole(hello.role)) {
        safeSend(ws, { type: 'hello', ok: false, error: 'invalid role' });
        return;
      }

      role = hello.role;

      if (role === 'publisher') {
        // Only one publisher allowed
        if (publisher && publisher !== ws) {
          safeSend(publisher, {
            type: 'error',
            error: 'publisher role taken; disconnecting',
          });
          publisher.close(1011, 'role taken');
        }
        publisher = ws;
        safeSend(ws, { type: 'hello', ok: true, role });

        // Notify all subscribers that publisher connected
        for (const [sid, subWs] of subscribers.entries()) {
          safeSend(subWs, {
            type: 'peer',
            event: 'connected',
            role: 'publisher',
          });
        }
      } else if (role === 'subscriber') {
        // Multiple subscribers allowed
        subscriberId = generateSubscriberId();
        subscribers.set(subscriberId, ws);
        wsToSubscriberId.set(ws, subscriberId);
        safeSend(ws, { type: 'hello', ok: true, role, subscriberId });

        // Notify publisher that a subscriber connected
        safeSend(publisher, {
          type: 'peer',
          event: 'connected',
          role: 'subscriber',
          subscriberId,
        });
      }

      return;
    }

    if (!role) {
      safeSend(ws, { type: 'error', error: 'must send hello first' });
      return;
    }

    // 2) Viewer-ready (subscriber -> publisher) - triggers offer creation
    if (msg.type === 'viewer-ready') {
      if (role !== 'subscriber' || !subscriberId) {
        safeSend(ws, {
          type: 'error',
          error: 'only subscribers can send viewer-ready',
        });
        return;
      }

      if (!publisher) {
        safeSend(ws, { type: 'error', error: 'no publisher connected' });
        return;
      }

      safeSend(publisher, {
        type: 'viewer-ready',
        subscriberId,
        from: 'subscriber',
        to: 'publisher',
      });

      return;
    }

    // 3) Control (subscriber -> publisher)
    if (msg.type === 'control') {
      if (role !== 'subscriber') {
        safeSend(ws, {
          type: 'error',
          error: 'only subscriber can send control',
        });
        return;
      }

      if (!publisher) {
        safeSend(ws, { type: 'error', error: 'no publisher connected' });
        return;
      }

      const m = msg as ControlMsg;
      safeSend(publisher, {
        type: 'control',
        from: 'subscriber',
        to: 'publisher',
        subscriberId: subscriberId ?? undefined,
        payload: m.payload ?? null,
      });

      return;
    }

    // 4) Control-status (publisher -> specific subscriber)
    if (msg.type === 'control-status') {
      if (role !== 'publisher') {
        safeSend(ws, {
          type: 'error',
          error: 'only publisher can send control-status',
        });
        return;
      }

      const m = msg as ControlStatusMsg & { subscriberId?: string };
      const targetId = m.subscriberId;

      if (targetId) {
        // Send to specific subscriber
        const target = subscribers.get(targetId);
        if (target) {
          safeSend(target, {
            type: 'control-status',
            from: 'publisher',
            to: 'subscriber',
            payload: m.payload ?? null,
          });
        }
      } else {
        // Broadcast to all subscribers (backward compatibility)
        for (const subWs of subscribers.values()) {
          safeSend(subWs, {
            type: 'control-status',
            from: 'publisher',
            to: 'subscriber',
            payload: m.payload ?? null,
          });
        }
      }

      return;
    }

    // 5) WebRTC signaling messages (offer/answer/candidate) - route with subscriberId
    if (role === 'subscriber' && subscriberId) {
      // Subscriber -> Publisher (with subscriberId)
      if (!publisher) return;

      const forwarded = {
        ...msg,
        subscriberId,
        from: 'subscriber',
        to: 'publisher',
      };
      safeSend(publisher, forwarded as OutMsg);
      return;
    }

    if (role === 'publisher') {
      // Publisher -> Specific Subscriber (extract subscriberId from message)
      const targetId = (msg as AnyMsg & { subscriberId?: string }).subscriberId;

      if (targetId) {
        const target = subscribers.get(targetId);
        if (target) {
          const forwarded = { ...msg, from: 'publisher', to: 'subscriber' };
          safeSend(target, forwarded as OutMsg);
        }
      } else {
        // Broadcast to all subscribers (backward compatibility for offers)
        for (const subWs of subscribers.values()) {
          const forwarded = { ...msg, from: 'publisher', to: 'subscriber' };
          safeSend(subWs, forwarded as OutMsg);
        }
      }
      return;
    }
  });

  ws.on('close', () => {
    if (role === 'publisher' && publisher === ws) {
      publisher = null;
      // Notify all subscribers
      for (const subWs of subscribers.values()) {
        safeSend(subWs, {
          type: 'peer',
          event: 'disconnected',
          role: 'publisher',
        });
      }
    } else if (role === 'subscriber' && subscriberId) {
      subscribers.delete(subscriberId);
      // Notify publisher
      safeSend(publisher, {
        type: 'peer',
        event: 'disconnected',
        role: 'subscriber',
        subscriberId,
      });
    }
  });

  ws.on('error', () => {
    // close handler will clean up
  });
});

console.log(`WS signaling relay listening on ws://0.0.0.0:${PORT}`);
