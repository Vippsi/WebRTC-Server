// subscriber/src/websocket.ts

import { log } from './utils';
import type { IncomingMsg, HelloMsg, OfferMsg, CandidateMsg } from './types';

// Declare Window.ws for DevTools access
declare global {
  interface Window {
    ws?: WebSocket;
  }
}

export function createWebSocket(
  url: string,
  onOffer: (sdp: RTCSessionDescriptionInit) => Promise<void>,
  drainPendingCandidates: () => Promise<void>,
  getPC: () => RTCPeerConnection | null,
  getRemoteDescriptionSet: () => boolean,
  addPendingCandidate: (candidate: unknown) => void
): WebSocket {
  const ws = new WebSocket(url);
  window.ws = ws;

  ws.addEventListener('open', () => {
    const hello: HelloMsg = { type: 'hello', role: 'subscriber' };
    ws.send(JSON.stringify(hello));
    log('ws connected');
  });

  ws.addEventListener('close', () => log('ws closed'));
  ws.addEventListener('error', () => log('ws error'));

  function isOfferMsg(msg: IncomingMsg): msg is OfferMsg {
    return (
      msg.type === 'offer' && typeof msg.sdp === 'object' && msg.sdp !== null
    );
  }

  function isCandidateMsg(msg: IncomingMsg): msg is CandidateMsg {
    return msg.type === 'candidate' && 'candidate' in msg;
  }

  ws.onmessage = async (ev) => {
    let msg: IncomingMsg;
    try {
      msg = JSON.parse(String(ev.data)) as IncomingMsg;
    } catch {
      log('bad ws msg');
      return;
    }

    if (msg.type === 'hello' || msg.type === 'hello-ack') {
      log('hello ack');
      return;
    }

    if (msg.type === 'candidate') {
      // candidates can arrive before offer/PC/remote description
      addPendingCandidate(msg.candidate);

      const pc = getPC();
      const remoteDescriptionSet = getRemoteDescriptionSet();
      if (pc && remoteDescriptionSet) {
        // If we're ready, drain immediately
        await drainPendingCandidates();
      }
      return;
    }

    if (isOfferMsg(msg)) {
      await onOffer(msg.sdp);
      return;
    }

    if (msg.type === 'control-status') {
      log('control-status:', JSON.stringify(msg.payload));
      return;
    }
  };

  return ws;
}
