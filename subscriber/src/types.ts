// subscriber/src/types.ts

export type Role = 'publisher' | 'subscriber';

export type HelloMsg = { type: 'hello'; role: Role };
export type OfferMsg = { type: 'offer'; sdp: RTCSessionDescriptionInit };
export type AnswerMsg = { type: 'answer'; sdp: RTCSessionDescriptionInit };
export type CandidateMsg = { type: 'candidate'; candidate: unknown };
export type ControlStatusMsg = { type: 'control-status'; payload: unknown };

export type IncomingMsg =
  | { type: 'hello' | 'hello-ack'; ok?: boolean }
  | OfferMsg
  | CandidateMsg
  | ControlStatusMsg
  | { type: string; [k: string]: unknown };

export type ControlPayload =
  | { kind: 'key'; key: string }
  | { kind: 'text'; text: string }
  | { kind: 'launch'; appId: string };

export type ControlMsg = { type: 'control'; payload: ControlPayload };
