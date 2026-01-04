# publisher/main.py
# Unified WebRTC (GStreamer) + Roku control publisher
from __future__ import annotations

import argparse
import asyncio
import json
import threading
from typing import Any, Dict, Optional

import websockets

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

from .roku import RokuECP  # noqa: E402

Gst.init(None)

STUN_SERVER = "stun://stun.l.google.com:19302"


def jdump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


async def send_json(ws, msg: Dict[str, Any]) -> None:
    await ws.send(jdump(msg))


def sdp_from_text(sdp_text: str) -> GstSdp.SDPMessage:
    _res, msg = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(sdp_text.encode("utf-8"), msg)
    return msg


def build_pipeline(
    video_dev: str,
    video_size: str,
    fps: int,
    audio_dev: Optional[str],
) -> Gst.Pipeline:
    w, h = video_size.split("x")

    audio_branch = ""
    if audio_dev:
        audio_branch = f"""
          alsasrc device={audio_dev} !
            queue !
            audioconvert ! audioresample !
            opusenc bitrate=64000 !
            rtpopuspay pt=111 !
            application/x-rtp,media=audio,encoding-name=OPUS,payload=111 !
            webrtc.
        """

    desc = f"""
      webrtcbin name=webrtc bundle-policy=max-bundle stun-server={STUN_SERVER}

      v4l2src device={video_dev} !
        image/jpeg,width={w},height={h},framerate={fps}/1 !
        queue !
        jpegdec !
        videoconvert !
        video/x-raw,format=I420 !
        x264enc tune=zerolatency speed-preset=veryfast bitrate=2500 key-int-max={fps} bframes=0 !
        video/x-h264,profile=baseline !
        rtph264pay config-interval=1 pt=96 !
        application/x-rtp,media=video,encoding-name=H264,payload=96 !
        webrtc.

      {audio_branch}
    """
    return Gst.parse_launch(desc)


class GstWebRTCPublisher:
    """
    Owns the GStreamer pipeline and translates between:
      - GStreamer webrtcbin callbacks (offer, ICE)
      - WS signaling messages (answer, ICE)
    """

    def __init__(
        self,
        video_device: str,
        video_size: str,
        framerate: int,
        audio_device: Optional[str],
    ) -> None:
        self.video_device = video_device
        self.video_size = video_size
        self.framerate = framerate
        self.audio_device = audio_device

        self._glib_loop: Optional[GLib.MainLoop] = None
        self._pipeline: Optional[Gst.Pipeline] = None
        self._webrtc: Optional[Gst.Element] = None

        self._ws = None
        self._aio_loop: Optional[asyncio.AbstractEventLoop] = None

        self._making_offer = False
        self._have_remote_answer = False

    def start(self, aio_loop: asyncio.AbstractEventLoop, ws) -> None:
        """
        Start GLib main loop thread + pipeline PLAYING, and wire callbacks to the given WS.
        """
        self._aio_loop = aio_loop
        self._ws = ws

        # Run GLib loop in background thread
        self._glib_loop = GLib.MainLoop()
        threading.Thread(target=self._glib_loop.run, daemon=True).start()

        # Build + start pipeline
        self._pipeline = build_pipeline(
            self.video_device, self.video_size, self.framerate, self.audio_device
        )
        webrtc = self._pipeline.get_by_name("webrtc")
        assert webrtc is not None
        self._webrtc = webrtc

        # Drain bus + log errors/warnings (prevents bus queue overflow)
        bus = self._pipeline.get_bus()
        if bus:
            bus.add_signal_watch()

            def on_bus_message(_bus, message):
                t = message.type
                if t == Gst.MessageType.ERROR:
                    err, dbg = message.parse_error()
                    print("[gst] ERROR:", err, dbg)
                elif t == Gst.MessageType.WARNING:
                    err, dbg = message.parse_warning()
                    print("[gst] WARNING:", err, dbg)

            bus.connect("message", on_bus_message)

        # ICE candidates from GStreamer -> WS
        def on_ice_candidate(_webrtc, mlineindex, candidate):
            msg = {
                "type": "candidate",
                "candidate": {
                    "candidate": candidate,
                    "sdpMLineIndex": int(mlineindex),
                },
            }
            loop = self._aio_loop
            if loop:
                asyncio.run_coroutine_threadsafe(self._ws.send(jdump(msg)), loop)

        self._webrtc.connect("on-ice-candidate", on_ice_candidate)

        # Offer creation callback
        def on_offer_created(promise, _):
            try:
                promise.wait()
                reply = promise.get_reply()
                offer = reply.get_value("offer")  # GstWebRTCSessionDescription
                if offer is None:
                    try:
                        txt = reply.to_string()
                    except Exception:
                        txt = None
                    print("[gst] offer is None; promise reply:", txt)
                    self._making_offer = False
                    return

                # Set local description
                self._webrtc.emit("set-local-description", offer, Gst.Promise.new())

                sdp_text = offer.sdp.as_text()
                msg = {"type": "offer", "sdp": {"type": "offer", "sdp": sdp_text}}

                loop = self._aio_loop
                if loop:
                    asyncio.run_coroutine_threadsafe(self._ws.send(jdump(msg)), loop)
                print("[gst] sent offer")
            finally:
                # allow future renegotiation if needed
                self._making_offer = False

        def create_offer():
            # guard against repeated offers from noisy negotiation-needed
            if self._making_offer:
                return
            self._making_offer = True
            promise = Gst.Promise.new_with_change_func(on_offer_created, None)
            self._webrtc.emit("create-offer", None, promise)

        # Negotiation-needed from GStreamer
        def on_negotiation_needed(_webrtc):
            print("[gst] on-negotiation-needed")
            create_offer()

        self._webrtc.connect("on-negotiation-needed", on_negotiation_needed)

        self._pipeline.set_state(Gst.State.PLAYING)

        # If subscriber comes later, we can re-offer when we see a peer event.
        # (No offer here yet; negotiation-needed will usually fire quickly anyway.)

    def stop(self) -> None:
        try:
            if self._pipeline:
                self._pipeline.set_state(Gst.State.NULL)
        finally:
            if self._glib_loop:
                try:
                    self._glib_loop.quit()
                except Exception:
                    pass

    def on_peer_connected(self, role: str) -> None:
        # When subscriber connects, ensure we (re)offer so start order doesn't matter.
        if role != "subscriber":
            return
        if not self._webrtc:
            return
        print("[gst] peer connected subscriber -> ensure offer")

        # Create-offer must be called on the GLib/GStreamer thread context.
        # We can schedule it using GLib.idle_add safely.
        def _do():
            # reset remote-answer marker, allow new offer
            self._have_remote_answer = False
            # trigger offer explicitly
            if not self._making_offer:
                self._making_offer = True
                promise = Gst.Promise.new_with_change_func(
                    # reuse the same callback used in start()
                    lambda p,
                    u: None,  # placeholder (we'll call create-offer via emit below)
                    None,
                )

            # Better: just emit create-offer and let existing on_offer_created handle it
            # BUT on_offer_created closure is inside start(). So instead, we just poke negotiation.
            # Easiest reliable approach: call "emit('create-offer')" again with a fresh promise+callback.
            def on_offer_created_local(promise, _):
                try:
                    promise.wait()
                    reply = promise.get_reply()
                    offer = reply.get_value("offer")
                    if offer is None:
                        self._making_offer = False
                        return
                    self._webrtc.emit("set-local-description", offer, Gst.Promise.new())
                    sdp_text = offer.sdp.as_text()
                    msg = {"type": "offer", "sdp": {"type": "offer", "sdp": sdp_text}}
                    loop = self._aio_loop
                    if loop:
                        asyncio.run_coroutine_threadsafe(
                            self._ws.send(jdump(msg)), loop
                        )
                    print("[gst] sent offer (peer-connect)")
                finally:
                    self._making_offer = False

            if self._making_offer:
                return
            self._making_offer = True
            promise = Gst.Promise.new_with_change_func(on_offer_created_local, None)
            self._webrtc.emit("create-offer", None, promise)
            return False  # remove idle handler

        GLib.idle_add(_do)

    def handle_answer(self, msg: Dict[str, Any]) -> None:
        if not self._webrtc:
            return
        sdp_obj = msg.get("sdp") or {}
        sdp_text = sdp_obj.get("sdp")
        if not sdp_text:
            print("[gst] answer missing sdp")
            return

        sdpmsg = sdp_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
        )

        def _do():
            self._webrtc.emit("set-remote-description", answer, Gst.Promise.new())
            self._have_remote_answer = True
            print("[gst] set remote description (answer)")
            return False

        GLib.idle_add(_do)

    def handle_candidate(self, msg: Dict[str, Any]) -> None:
        if not self._webrtc:
            return
        c = msg.get("candidate") or {}
        cand = c.get("candidate")
        mline = c.get("sdpMLineIndex")
        if cand is None or mline is None:
            return

        def _do():
            self._webrtc.emit("add-ice-candidate", int(mline), cand)
            return False

        GLib.idle_add(_do)


async def handle_control(roku: RokuECP, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
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


async def publisher_loop(args: argparse.Namespace) -> None:
    roku = RokuECP(args.roku_ip, port=args.roku_port)

    while True:
        gst_pub: Optional[GstWebRTCPublisher] = None
        try:
            print(f"[publisher] connecting to signaling: {args.signaling}")
            async with websockets.connect(
                args.signaling, ping_interval=20, ping_timeout=20
            ) as ws:
                await send_json(ws, {"type": "hello", "role": "publisher"})
                print("[publisher] sent hello as publisher")

                # Start GStreamer publisher wired to this WS
                gst_pub = GstWebRTCPublisher(
                    video_device=args.video_device,
                    video_size=args.video_size,
                    framerate=args.framerate,
                    audio_device=args.audio_device,
                )
                gst_pub.start(asyncio.get_running_loop(), ws)

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        print("[publisher] ignoring non-json message")
                        continue

                    mtype = msg.get("type")

                    if mtype == "hello":
                        # ack from server
                        continue

                    if mtype == "peer":
                        # signaling server notifies connection events
                        # {type:"peer", event:"connected"|"disconnected", role:"subscriber"|...}
                        event = msg.get("event")
                        role = msg.get("role")
                        if event == "connected" and isinstance(role, str) and gst_pub:
                            gst_pub.on_peer_connected(role)
                        continue

                    # --- WebRTC signaling (answer / candidate) ---
                    if mtype == "answer" and gst_pub:
                        gst_pub.handle_answer(msg)
                        continue

                    if mtype == "candidate" and gst_pub:
                        gst_pub.handle_candidate(msg)
                        continue

                    # --- Control ---
                    if mtype == "control":
                        payload = msg.get("payload") or {}
                        if not isinstance(payload, dict):
                            payload = {}
                        try:
                            result = await handle_control(roku, payload)
                            await send_json(
                                ws, {"type": "control-status", "payload": result}
                            )
                            # Optional: print for debugging
                            # print(f"[publisher] control ok: {result}")
                        except Exception as e:
                            err = {"ok": False, "error": str(e), "payload": payload}
                            await send_json(
                                ws, {"type": "control-status", "payload": err}
                            )
                            print(f"[publisher] control error: {err}")
                        continue

                    # ignore everything else (offer from other side, info/error, etc.)
        except (OSError, websockets.WebSocketException) as e:
            print(f"[publisher] signaling connection error: {e}")
        finally:
            if gst_pub:
                try:
                    gst_pub.stop()
                except Exception:
                    pass

        await asyncio.sleep(1.0)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Unified publisher: GStreamer WebRTC video + Roku ECP control"
    )
    p.add_argument("--signaling", required=True, help="ws://host:8080")
    p.add_argument("--roku-ip", required=True, help="Roku LAN IP (e.g. 192.168.50.200)")
    p.add_argument(
        "--roku-port", type=int, default=8060, help="Roku ECP port (default 8060)"
    )

    # GStreamer capture options (same defaults as gst_publisher.py)
    p.add_argument("--video-device", default="/dev/video0")
    p.add_argument("--video-size", default="1280x720")
    p.add_argument("--framerate", type=int, default=30)
    p.add_argument(
        "--audio-device", default=None, help='ALSA device like "hw:2,0" (optional)'
    )

    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(publisher_loop(args))
    except KeyboardInterrupt:
        print("\n[publisher] stopped")


if __name__ == "__main__":
    main()
