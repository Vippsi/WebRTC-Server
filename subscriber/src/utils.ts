// subscriber/src/utils.ts

export function getEl(id: string): HTMLElement | null {
  const el = document.getElementById(id);
  return el instanceof HTMLElement ? el : null;
}

export function getVideo(id: string): HTMLVideoElement | null {
  const el = document.getElementById(id);
  return el instanceof HTMLVideoElement ? el : null;
}

const logEl = getEl('log');
export function log(...a: unknown[]) {
  const line = a.map(String).join(' ');
  console.log(line);
  if (logEl) logEl.textContent += line + '\n';
}

// Normalize different candidate shapes into RTCIceCandidateInit
export function normalizeIce(ice: unknown): RTCIceCandidateInit | null {
  if (!ice || typeof ice !== 'object') return null;

  // possible shapes:
  // 1) { candidate: "candidate:...", sdpMLineIndex: 0 }
  // 2) { candidate: { candidate: "candidate:...", sdpMLineIndex: 0 } }
  // 3) RTCIceCandidateInit-ish
  const obj = ice as Record<string, unknown>;

  let candObj: unknown = obj;
  if (typeof obj.candidate === 'object' && obj.candidate) {
    candObj = obj.candidate;
  }

  if (!candObj || typeof candObj !== 'object') return null;
  const c = candObj as Record<string, unknown>;

  const candidateVal = c.candidate;
  if (typeof candidateVal !== 'string' || candidateVal.length === 0)
    return null;

  const mlineVal = c.sdpMLineIndex;
  if (typeof mlineVal === 'number') {
    return { candidate: candidateVal, sdpMLineIndex: mlineVal };
  }

  // last resort: candidate string only
  return { candidate: candidateVal };
}
