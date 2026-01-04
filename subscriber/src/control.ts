// subscriber/src/control.ts

import { log } from './utils';
import type { ControlMsg, ControlPayload } from './types';

function mapKeyToControl(e: KeyboardEvent): string | null {
  switch (e.key) {
    case 'ArrowUp':
      return 'UP';
    case 'ArrowDown':
      return 'DOWN';
    case 'ArrowLeft':
      return 'LEFT';
    case 'ArrowRight':
      return 'RIGHT';
    case 'Enter':
      return 'ENTER';
    case 'Backspace':
      return 'BACK';
    case 'Home':
      return 'HOME';
    default:
      return null;
  }
}

export function sendControlWS(
  socket: WebSocket,
  payload: ControlPayload
): void {
  if (socket.readyState !== WebSocket.OPEN) {
    log('ws not open (cannot send control)');
    return;
  }
  const msg: ControlMsg = { type: 'control', payload };
  socket.send(JSON.stringify(msg));
}

export function setupKeyboardControl(socket: WebSocket): void {
  log('keyboard control enabled: arrows, Enter, Backspace, Home');
  window.addEventListener('click', () => window.focus(), { passive: true });

  window.addEventListener(
    'keydown',
    (e) => {
      if (e.repeat) return;

      const key = mapKeyToControl(e);
      if (!key) return;

      e.preventDefault();
      sendControlWS(socket, { kind: 'key', key });
    },
    { passive: false }
  );
}
