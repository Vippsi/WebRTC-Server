# publisher/roku.py
#
# Minimal Roku ECP client:
# - POST /keypress/<Key>
# - POST /keypress/Lit_<char> (text)
# - (optional) POST /launch/<appId>
#
# Usage:
#   from publisher.roku import RokuECP
#   roku = RokuECP("192.168.1.50")
#   await roku.key("HOME")
#   await roku.text("netflix")
#   await roku.launch("12")  # example app id

from __future__ import annotations

import asyncio
import urllib.parse
from dataclasses import dataclass

import aiohttp


# Generic -> Roku ECP mapping
GENERIC_TO_ROKU_KEY = {
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    "ENTER": "Select",
    "BACK": "Back",
    "HOME": "Home",
    # Roku "Play" typically toggles play/pause on most devices
    "PLAY": "Play",
    "PAUSE": "Play",
    "PLAY_PAUSE": "Play",
}


@dataclass
class RokuECP:
    """
    Roku External Control Protocol (ECP) client over HTTP.

    Notes:
      - Most Roku devices listen on port 8060.
      - Endpoints accept POST with an empty body.
      - Key presses are stateless; no key-down/up needed.
    """

    ip: str
    port: int = 8060
    timeout_s: float = 3.0
    retries: int = 2
    retry_backoff_s: float = 0.15

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    async def key(
        self, generic_key: str, session: aiohttp.ClientSession | None = None
    ) -> None:
        """
        Send a single keypress using a generic key name:
          UP, DOWN, LEFT, RIGHT, ENTER, BACK, HOME, PLAY/PAUSE/PLAY_PAUSE
        """
        if not generic_key:
            raise ValueError("generic_key is required")

        generic_key = generic_key.upper()
        roku_key = GENERIC_TO_ROKU_KEY.get(generic_key)
        if not roku_key:
            raise ValueError(
                f"Unsupported key '{generic_key}'. Supported: {sorted(GENERIC_TO_ROKU_KEY.keys())}"
            )

        await self._post(f"/keypress/{roku_key}", session=session)

    async def roku_keypress(
        self, roku_key: str, session: aiohttp.ClientSession | None = None
    ) -> None:
        """
        Send a raw Roku keypress (bypass generic mapping).
        Example: roku_keypress("InstantReplay")
        """
        if not roku_key:
            raise ValueError("roku_key is required")
        await self._post(f"/keypress/{roku_key}", session=session)

    async def text(
        self,
        text: str,
        session: aiohttp.ClientSession | None = None,
        per_char_delay_s: float = 0.02,
    ) -> None:
        """
        Type text by sending Lit_<char> keypresses.

        Roku expects each character via:
          POST /keypress/Lit_<url-encoded-char>

        per_char_delay_s helps prevent overruns on some networks/devices.
        """
        if text is None:
            raise ValueError("text cannot be None")

        owns_session = session is None
        if owns_session:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s)
            session = aiohttp.ClientSession(timeout=timeout)

        try:
            for ch in text:
                encoded = urllib.parse.quote(ch, safe="")
                await self._post(f"/keypress/Lit_{encoded}", session=session)
                if per_char_delay_s > 0:
                    await asyncio.sleep(per_char_delay_s)
        finally:
            if owns_session and session:
                await session.close()

    async def launch(
        self, app_id: str, session: aiohttp.ClientSession | None = None
    ) -> None:
        """
        Launch a Roku channel by appId:
          POST /launch/<appId>
        """
        if not app_id:
            raise ValueError("app_id is required")
        app_id_enc = urllib.parse.quote(str(app_id), safe="")
        await self._post(f"/launch/{app_id_enc}", session=session)

    async def _post(
        self, path: str, session: aiohttp.ClientSession | None = None
    ) -> None:
        """
        Internal POST helper with retries.

        Retries on network-ish errors (timeouts, connection reset, etc).
        Does NOT retry on non-2xx HTTP responses.
        """
        url = self.base_url + path

        owns_session = session is None
        if owns_session:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s)
            session = aiohttp.ClientSession(timeout=timeout)

        try:
            for attempt in range(self.retries + 1):
                try:
                    async with session.post(url, data=b"") as resp:
                        if resp.status < 200 or resp.status >= 300:
                            body = await resp.text()
                            raise RuntimeError(
                                f"Roku ECP POST {path} failed: {resp.status} {body[:200]}"
                            )
                        return
                except RuntimeError:
                    # Don't retry non-2xx responses
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    if attempt < self.retries:
                        await asyncio.sleep(self.retry_backoff_s * (2**attempt))
                    else:
                        raise
        finally:
            if owns_session and session:
                await session.close()


# Optional: simple CLI smoke test
#   python -m publisher.roku --ip 192.168.1.50 --key HOME
#   python -m publisher.roku --ip 192.168.1.50 --text "netflix"
#   python -m publisher.roku --ip 192.168.1.50 --launch 12
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Minimal Roku ECP client")
    parser.add_argument("--ip", required=True, help="Roku IP address (LAN)")
    parser.add_argument(
        "--port", type=int, default=8060, help="Roku ECP port (default: 8060)"
    )
    parser.add_argument(
        "--key",
        help="Generic key: UP/DOWN/LEFT/RIGHT/ENTER/BACK/HOME/PLAY/PAUSE/PLAY_PAUSE",
    )
    parser.add_argument("--text", help="Text to type using Lit_ keypresses")
    parser.add_argument("--launch", help="App ID to launch (optional)")
    args = parser.parse_args()

    async def _main():
        roku = RokuECP(args.ip, port=args.port)
        if args.key:
            await roku.key(args.key)
            print(f"Sent key: {args.key}")
        if args.text is not None:
            await roku.text(args.text)
            print(f"Typed text: {args.text!r}")
        if args.launch:
            await roku.launch(args.launch)
            print(f"Launched app: {args.launch}")

        if not args.key and args.text is None and not args.launch:
            print("Nothing to do. Provide --key and/or --text and/or --launch.")

    asyncio.run(_main())
