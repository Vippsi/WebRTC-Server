# publisher/gst_publisher.py
import asyncio
import json
import threading
from typing import Optional

import websockets

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC

Gst.init(None)

STUN_SERVER = "stun://stun.l.google.com:19302"


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

    # NOTE: This assumes the capture card can output MJPEG at the requested caps.
    # If you later decide to use raw YUY2, swap the jpeg branch accordingly.
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


def sdp_from_text(sdp_text: str) -> GstSdp.SDPMessage:
    _res, msg = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(sdp_text.encode("utf-8"), msg)
    return msg


async def main() -> None:
    import argparse

    p = argparse.ArgumentParser(
        description="GStreamer webrtcbin publisher (video-only)"
    )
    p.add_argument("--signaling", required=True, help="ws://host:8080")

    p.add_argument("--video-device", default="/dev/video0")
    p.add_argument("--video-size", default="1280x720")
    p.add_argument("--framerate", type=int, default=30)

    p.add_argument(
        "--audio-device", default=None, help='ALSA device like "hw:2,0" (optional)'
    )
    args = p.parse_args()

    loop = asyncio.get_running_loop()

    # Run GLib mainloop for GStreamer in a background thread
    glib_loop = GLib.MainLoop()
    threading.Thread(target=glib_loop.run, daemon=True).start()

    pipeline = build_pipeline(
        args.video_device, args.video_size, args.framerate, args.audio_device
    )
    webrtc = pipeline.get_by_name("webrtc")
    assert webrtc is not None

    async with websockets.connect(
        args.signaling, ping_interval=20, ping_timeout=20
    ) as ws:
        await ws.send(json.dumps({"type": "hello", "role": "publisher"}))
        print("[gst] connected + hello as publisher")

        # ---- GStreamer -> WS ICE candidates ----
        def on_ice_candidate(_webrtc, mlineindex, candidate):
            msg = {
                "type": "candidate",
                "candidate": {
                    "candidate": candidate,
                    "sdpMLineIndex": int(mlineindex),
                },
            }
            asyncio.run_coroutine_threadsafe(ws.send(json.dumps(msg)), loop)

        webrtc.connect("on-ice-candidate", on_ice_candidate)

        # ---- Create offer and send to subscriber ----
        def on_offer_created(promise, _):
            promise.wait()
            reply = promise.get_reply()
            offer = reply.get_value(
                "offer"
            )  # GstWebRTCSessionDescription (may be None)
            if offer is None:
                try:
                    txt = reply.to_string()
                except Exception:
                    txt = None
                print("[gst] offer is None; promise reply:", txt)
                return

            # Set local description
            webrtc.emit("set-local-description", offer, Gst.Promise.new())

            # Send offer SDP over WS
            sdp_text = offer.sdp.as_text()
            msg = {"type": "offer", "sdp": {"type": "offer", "sdp": sdp_text}}
            asyncio.run_coroutine_threadsafe(ws.send(json.dumps(msg)), loop)
            print("[gst] sent offer")

        def create_offer():
            promise = Gst.Promise.new_with_change_func(on_offer_created, None)
            webrtc.emit("create-offer", None, promise)

        # Create offer only when negotiation is needed
        def on_negotiation_needed(_webrtc):
            print("[gst] on-negotiation-needed")
            create_offer()

        webrtc.connect("on-negotiation-needed", on_negotiation_needed)

        pipeline.set_state(Gst.State.PLAYING)

        # ---- WS receive loop: answer, ICE ----
        async for raw in ws:
            msg = json.loads(raw)
            t = msg.get("type")

            if t == "answer":
                sdp_obj = msg.get("sdp") or {}
                sdp_text = sdp_obj.get("sdp")
                if not sdp_text:
                    print("[gst] answer missing sdp")
                    continue

                sdpmsg = sdp_from_text(sdp_text)
                answer = GstWebRTC.WebRTCSessionDescription.new(
                    GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
                )
                webrtc.emit("set-remote-description", answer, Gst.Promise.new())
                print("[gst] set remote description (answer)")
                continue

            if t == "candidate":
                c = msg.get("candidate") or {}
                cand = c.get("candidate")
                mline = c.get("sdpMLineIndex")
                if cand is not None and mline is not None:
                    webrtc.emit("add-ice-candidate", int(mline), cand)
                continue

    # cleanup (normally not reached unless WS loop exits)
    pipeline.set_state(Gst.State.NULL)
    glib_loop.quit()


if __name__ == "__main__":
    asyncio.run(main())
