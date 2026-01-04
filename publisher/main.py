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


class SubscriberConnection:
    """
    Manages a single subscriber's WebRTC connection (pipeline + webrtcbin)
    """

    def __init__(
        self,
        subscriber_id: str,
        video_device: str,
        video_size: str,
        framerate: int,
        audio_device: Optional[str],
        aio_loop: asyncio.AbstractEventLoop,
        ws,
    ) -> None:
        self.subscriber_id = subscriber_id
        self.video_device = video_device
        self.video_size = video_size
        self.framerate = framerate
        self.audio_device = audio_device
        self._aio_loop = aio_loop
        self._ws = ws

        self._pipeline: Optional[Gst.Pipeline] = None
        self._webrtc: Optional[Gst.Element] = None
        self._making_offer = False
        self._have_remote_answer = False

    def start(self) -> None:
        """Start the pipeline for this subscriber"""
        # Build + start pipeline
        self._pipeline = build_pipeline(
            self.video_device, self.video_size, self.framerate, self.audio_device
        )
        webrtc = self._pipeline.get_by_name("webrtc")
        assert webrtc is not None
        self._webrtc = webrtc

        # Drain bus + log errors/warnings
        bus = self._pipeline.get_bus()
        if bus:
            bus.add_signal_watch()

            def on_bus_message(_bus, message):
                t = message.type
                if t == Gst.MessageType.ERROR:
                    err, dbg = message.parse_error()
                    print(f"[gst-{self.subscriber_id}] ERROR:", err, dbg)
                elif t == Gst.MessageType.WARNING:
                    err, dbg = message.parse_warning()
                    print(f"[gst-{self.subscriber_id}] WARNING:", err, dbg)

            bus.connect("message", on_bus_message)

        # ICE candidates from GStreamer -> WS
        def on_ice_candidate(_webrtc, mlineindex, candidate):
            msg = {
                "type": "candidate",
                "subscriberId": self.subscriber_id,
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
                offer = reply.get_value("offer")
                if offer is None:
                    try:
                        txt = reply.to_string()
                    except Exception:
                        txt = None
                    print(
                        f"[gst-{self.subscriber_id}] offer is None; promise reply:", txt
                    )
                    self._making_offer = False
                    return

                # Set local description
                self._webrtc.emit("set-local-description", offer, Gst.Promise.new())

                sdp_text = offer.sdp.as_text()
                msg = {
                    "type": "offer",
                    "subscriberId": self.subscriber_id,
                    "sdp": {"type": "offer", "sdp": sdp_text},
                }

                loop = self._aio_loop
                if loop:
                    asyncio.run_coroutine_threadsafe(self._ws.send(jdump(msg)), loop)
                print(f"[gst-{self.subscriber_id}] sent offer")
            finally:
                self._making_offer = False

        def create_offer():
            if self._making_offer:
                return
            self._making_offer = True
            promise = Gst.Promise.new_with_change_func(on_offer_created, None)
            self._webrtc.emit("create-offer", None, promise)

        # Negotiation-needed from GStreamer
        def on_negotiation_needed(_webrtc):
            print(f"[gst-{self.subscriber_id}] on-negotiation-needed")
            create_offer()

        self._webrtc.connect("on-negotiation-needed", on_negotiation_needed)

        self._pipeline.set_state(Gst.State.PLAYING)
        # Immediately create offer for this subscriber
        create_offer()

    def stop(self) -> None:
        """Stop and cleanup this subscriber's pipeline"""
        try:
            if self._pipeline:
                self._pipeline.set_state(Gst.State.NULL)
        except Exception as e:
            print(f"[gst-{self.subscriber_id}] error stopping pipeline: {e}")

    def handle_answer(self, msg: Dict[str, Any]) -> None:
        """Handle SDP answer from subscriber"""
        if not self._webrtc:
            return
        sdp_obj = msg.get("sdp") or {}
        sdp_text = sdp_obj.get("sdp")
        if not sdp_text:
            print(f"[gst-{self.subscriber_id}] answer missing sdp")
            return

        sdpmsg = sdp_from_text(sdp_text)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
        )

        def _do():
            self._webrtc.emit("set-remote-description", answer, Gst.Promise.new())
            self._have_remote_answer = True
            print(f"[gst-{self.subscriber_id}] set remote description (answer)")
            return False

        GLib.idle_add(_do)

    def handle_candidate(self, msg: Dict[str, Any]) -> None:
        """Handle ICE candidate from subscriber"""
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


class GstWebRTCPublisher:
    """
    Manages multiple subscriber connections, each with its own pipeline.
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
        self._ws = None
        self._aio_loop: Optional[asyncio.AbstractEventLoop] = None

        # Map subscriber_id -> SubscriberConnection
        self._subscribers: Dict[str, SubscriberConnection] = {}

    def start(self, aio_loop: asyncio.AbstractEventLoop, ws) -> None:
        """
        Start GLib main loop thread. Subscriber pipelines are created on-demand.
        """
        self._aio_loop = aio_loop
        self._ws = ws

        # Run GLib loop in background thread
        self._glib_loop = GLib.MainLoop()
        threading.Thread(target=self._glib_loop.run, daemon=True).start()

    def stop(self) -> None:
        """Stop all subscriber connections and GLib loop"""
        # Stop all subscriber pipelines
        for sub in list(self._subscribers.values()):
            try:
                sub.stop()
            except Exception:
                pass
        self._subscribers.clear()

        # Stop GLib loop
        if self._glib_loop:
            try:
                self._glib_loop.quit()
            except Exception:
                pass

    def create_subscriber_connection(self, subscriber_id: str) -> None:
        """Create a new pipeline for a subscriber"""
        if subscriber_id in self._subscribers:
            print(f"[publisher] subscriber {subscriber_id} already exists")
            return

        print(f"[publisher] creating connection for subscriber {subscriber_id}")
        sub = SubscriberConnection(
            subscriber_id=subscriber_id,
            video_device=self.video_device,
            video_size=self.video_size,
            framerate=self.framerate,
            audio_device=self.audio_device,
            aio_loop=self._aio_loop,
            ws=self._ws,
        )
        self._subscribers[subscriber_id] = sub
        sub.start()

    def remove_subscriber_connection(self, subscriber_id: str) -> None:
        """Remove a subscriber's connection"""
        sub = self._subscribers.pop(subscriber_id, None)
        if sub:
            print(f"[publisher] removing connection for subscriber {subscriber_id}")
            try:
                sub.stop()
            except Exception:
                pass

    def handle_answer(self, msg: Dict[str, Any]) -> None:
        """Route answer to the correct subscriber"""
        subscriber_id = msg.get("subscriberId")
        if not subscriber_id:
            print("[publisher] answer missing subscriberId")
            return

        sub = self._subscribers.get(subscriber_id)
        if not sub:
            print(f"[publisher] answer for unknown subscriber {subscriber_id}")
            return

        sub.handle_answer(msg)

    def handle_candidate(self, msg: Dict[str, Any]) -> None:
        """Route ICE candidate to the correct subscriber"""
        subscriber_id = msg.get("subscriberId")
        if not subscriber_id:
            print("[publisher] candidate missing subscriberId")
            return

        sub = self._subscribers.get(subscriber_id)
        if not sub:
            print(f"[publisher] candidate for unknown subscriber {subscriber_id}")
            return

        sub.handle_candidate(msg)


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
                        # {type:"peer", event:"connected"|"disconnected", role:"subscriber"|..., subscriberId?:string}
                        event = msg.get("event")
                        role = msg.get("role")
                        subscriber_id = msg.get("subscriberId")

                        if (
                            event == "connected"
                            and role == "subscriber"
                            and subscriber_id
                            and gst_pub
                        ):
                            # Subscriber connected - wait for viewer-ready message to create connection
                            print(f"[publisher] subscriber {subscriber_id} connected")
                        elif (
                            event == "disconnected"
                            and role == "subscriber"
                            and subscriber_id
                            and gst_pub
                        ):
                            # Subscriber disconnected - cleanup
                            gst_pub.remove_subscriber_connection(subscriber_id)
                        continue

                    if mtype == "viewer-ready":
                        # Subscriber is ready - create a new connection for them
                        subscriber_id = msg.get("subscriberId")
                        if subscriber_id and gst_pub:
                            print(f"[publisher] viewer-ready from {subscriber_id}")
                            gst_pub.create_subscriber_connection(subscriber_id)
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
