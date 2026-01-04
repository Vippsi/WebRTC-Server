// subscriber/src/subscriber.ts

import { log } from './utils';
import { createWebSocket } from './websocket';
import { addIceToPC, makePC } from './webrtc';
import { setupKeyboardControl } from './control';
import type { AnswerMsg } from './types';

// ---- state ----
let pc: RTCPeerConnection | null = null;
let remoteDescriptionSet = false;
let subscriberId: string | undefined = undefined;
let offerCount = 0;

// IMPORTANT: keep this across offer until applied
let pendingCandidates: unknown[] = [];

// ---- WebSocket URL ----
const wsParam = new URLSearchParams(location.search).get('ws');
const WS_URL =
  (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';

log('WS_URL =', WS_URL);

// ---- WebSocket setup ----
const ws = createWebSocket(
  WS_URL,
  async (sdp) => {
    offerCount += 1;
    const existingState = pc?.connectionState ?? 'none';
    log(`got offer #${offerCount} (pc state: ${existingState})`);

    // Re-use an existing PC if it is alive; otherwise make a new one
    const shouldReuse =
      pc !== null &&
      pc.connectionState !== 'closed' &&
      pc.connectionState !== 'failed';

    if (!shouldReuse) {
      try {
        if (pc) pc.close();
      } catch {}
      pc = makePC(ws);
      // New session: drop any stale candidates from previous attempts
      pendingCandidates = [];
    }

    if (!pc) {
      log('failed to create/get PC');
      return;
    }

    // Always override onicecandidate to include subscriberId (for both new and reused PCs)
    pc.onicecandidate = (ev) => {
      if (ev.candidate) {
        ws.send(
          JSON.stringify({
            type: 'candidate',
            subscriberId: subscriberId,
            candidate: ev.candidate.toJSON(),
          })
        );
      }
    };

    remoteDescriptionSet = false;

    const thisPC = pc;

    await thisPC.setRemoteDescription(sdp);
    remoteDescriptionSet = true;

    // Apply ANY candidates we queued (including ones from before offer)
    const toApply = pendingCandidates;
    pendingCandidates = [];
    for (const c of toApply) await addIceToPC(thisPC, c);

    const answer = await thisPC.createAnswer();
    await thisPC.setLocalDescription(answer);

    const desc = thisPC.localDescription;
    if (!desc) {
      log('no localDescription after setLocalDescription??');
      return;
    }

    const out: AnswerMsg = { type: 'answer', subscriberId, sdp: desc };
    ws.send(JSON.stringify(out));
    log('sent answer');
  },
  async () => {
    // Drain all pending candidates when ready
    if (pc && remoteDescriptionSet) {
      const toApply = pendingCandidates;
      pendingCandidates = [];
      for (const c of toApply) await addIceToPC(pc, c);
    }
  },
  () => pc,
  () => remoteDescriptionSet,
  (candidate) => {
    pendingCandidates.push(candidate);
  },
  () => subscriberId,
  (id: string) => {
    subscriberId = id;
  }
);

// ---- Control setup ----
setupKeyboardControl(ws);
