/// <reference types="vite/client" />
// subscriber/src/webrtc.ts

import { getVideo, log, normalizeIce } from './utils';
import type { CandidateMsg } from './types';

const turnUsername = import.meta.env.VITE_TURN_USERNAME ?? 'webrtcuser';
const turnCredential = import.meta.env.VITE_TURN_CREDENTIAL ?? 'REPLACE_WITH_STRONG_PASSWORD';

export async function addIceToPC(
  targetPC: RTCPeerConnection,
  ice: unknown
): Promise<void> {
  const init = normalizeIce(ice);
  if (!init) {
    log('ICE: could not normalize candidate');
    return;
  }
  try {
    await targetPC.addIceCandidate(new RTCIceCandidate(init));
  } catch (e) {
    log('addIceCandidate error:', e instanceof Error ? e.message : String(e));
  }
}

export function makePC(socket: WebSocket): RTCPeerConnection {
  const next = new RTCPeerConnection({
    iceServers: [
      { urls: ['stun:stun.l.google.com:19302'] },
      {
        urls: ['turn:turn.vippsi.dev:3478?transport=udp'],
        username: turnUsername,
        credential: turnCredential,
      },
    ],
  });

  next.ontrack = (ev) => {
    const stream = ev.streams[0];
    const vid = getVideo('v');
    if (vid && stream) {
      // autoplay hygiene
      vid.autoplay = true;
      vid.playsInline = true;
      vid.muted = true;
      vid.srcObject = stream;

      // kick play for Safari/Chrome autoplay policies
      void vid.play().catch(() => {});
    }
    log('track:', ev.track.kind);
  };

  next.onicecandidate = (ev) => {
    if (!ev.candidate) return;
    socket.send(
      JSON.stringify({
        type: 'candidate',
        candidate: ev.candidate.toJSON(),
      })
    );
  };

  next.oniceconnectionstatechange = () => log('ice:', next.iceConnectionState);
  next.onconnectionstatechange = () => log('pc:', next.connectionState);

  return next;
}
