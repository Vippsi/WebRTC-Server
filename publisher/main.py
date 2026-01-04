# WebRTC publisher + control
# publisher/main.py

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, Optional

import websockets
from websockets.client import WebSocketClientProtocol

from .roku import RokuECP


def jdump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


async def send_json(ws: WebSocketClientProtocol, msg: Dict[str, Any]) -> None:
    await ws.send(jdump(msg))


async def handle_control(roku: RokuECP, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle a control payload and execute Roku commands.

    Expected payload shapes:
      { "kind": "key", "key": "UP" }
      { "kind": "text", "text": "netflix" }
      { "kind": "launch", "appId": "12" }
    """
    kind = payload.get("kind")
    if kind == "key":
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("control payload missing 'key'")
        await roku.key(key)
        return {"ok": True, "kind": "key", "handled": key}

    if kind == "text":
        text = payload.get("text", "")
        if not isinstance(text, str):
            raise ValueError("control payload 'text' must be a string")
        await roku.text(text)
        return {"ok": True, "kind": "text", "len": len(text)}

    if kind == "launch":
        app_id = payload.get("appId")
        if not isinstance(app_id, str) or not app_id:
            raise ValueError("control payload missing 'appId'")
        await roku.launch(app_id)
        return {"ok": True, "kind": "launch", "appId": app_id}

    raise ValueError(f"unknown control kind: {kind!r}")


async def publisher_loop(signaling_url: str, roku_ip: str, roku_port: int) -> None:
    roku = RokuECP(roku_ip, port=roku_port)

    while True:
        try:
            print(f"[publisher] connecting to signaling: {signaling_url}")
            async with websockets.connect(
                signaling_url, ping_interval=20, ping_timeout=20
            ) as ws:
                # Register as publisher (matches your server.js)
                await send_json(ws, {"type": "hello", "role": "publisher"})
                print("[publisher] sent hello as publisher")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        print("[publisher] ignoring non-json message")
                        continue

                    mtype = msg.get("type")

                    # You can log other signaling messages if you want:
                    # offer/answer/candidate will be ignored in Phase A.
                    if mtype == "hello":
                        print(f"[publisher] hello ack: {msg}")
                        continue

                    if mtype == "control":
                        payload = msg.get("payload") or {}
                        if not isinstance(payload, dict):
                            payload = {}

                        try:
                            result = await handle_control(roku, payload)
                            # Optional ack back to subscriber for debugging
                            await send_json(
                                ws, {"type": "control-status", "payload": result}
                            )
                            print(f"[publisher] control ok: {result}")
                        except Exception as e:
                            err = {"ok": False, "error": str(e), "payload": payload}
                            await send_json(
                                ws, {"type": "control-status", "payload": err}
                            )
                            print(f"[publisher] control error: {err}")
                        continue

                    # Phase A: ignore everything else (offer/answer/candidate)
                    # print(f"[publisher] ignoring message type={mtype}")
        except (OSError, websockets.WebSocketException) as e:
            print(f"[publisher] signaling connection error: {e}")

        # reconnect backoff
        await asyncio.sleep(1.0)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Publisher (control-only) for Roku via signaling WS"
    )
    p.add_argument("--signaling", required=True, help="ws://host:8080")
    p.add_argument("--roku-ip", required=True, help="Roku LAN IP (e.g. 192.168.1.50)")
    p.add_argument(
        "--roku-port", type=int, default=8060, help="Roku ECP port (default 8060)"
    )
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(publisher_loop(args.signaling, args.roku_ip, args.roku_port))
    except KeyboardInterrupt:
        print("\n[publisher] stopped")


if __name__ == "__main__":
    main()
