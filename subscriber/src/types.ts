// subscriber/src/types.ts

export type Role = 'publisher' | 'subscriber';

export type HelloMsg = { type: 'hello'; role: Role };
export type HelloAckMsg = {
  type: 'hello';
  ok?: boolean;
  role?: Role;
  subscriberId?: string;
};
export type OfferMsg = {
  type: 'offer';
  sdp: RTCSessionDescriptionInit;
  subscriberId?: string;
};
export type AnswerMsg = {
  type: 'answer';
  sdp: RTCSessionDescriptionInit;
  subscriberId?: string;
};
export type CandidateMsg = {
  type: 'candidate';
  candidate: unknown;
  subscriberId?: string;
};
export type ControlStatusMsg = { type: 'control-status'; payload: unknown };
export type ViewerReadyMsg = { type: 'viewer-ready' };

export type IncomingMsg =
  | HelloAckMsg
  | OfferMsg
  | CandidateMsg
  | ControlStatusMsg
  | { type: string; [k: string]: unknown };

export type ControlPayload =
  | { kind: 'key'; key: string }
  | { kind: 'text'; text: string }
  | { kind: 'launch'; appId: string };

export type ControlMsg = { type: 'control'; payload: ControlPayload };
