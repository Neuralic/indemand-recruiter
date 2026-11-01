"""ICE connectivity probe: a headless aiortc client in a SEPARATE process connects to the running 17_live_mic server over
real (non-loopback) host candidates, like Chrome would. Prints ICE states + candidates. No audio is sent."""
import asyncio
import sys
import time

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack

BASE = "http://localhost:7860"


def cands(d):
    return [l.strip() for l in d.sdp.splitlines() if l.startswith("a=candidate")]


async def main() -> None:
    sid = httpx.post(f"{BASE}/start", json={"transport": "webrtc", "enableDefaultIceServers": False}).json()["sessionId"]
    pc = RTCPeerConnection()
    pc.addTrack(AudioStreamTrack())
    pc.createDataChannel("pipecat")
    t0 = time.time()

    @pc.on("iceconnectionstatechange")
    async def _():
        print(f"+{time.time()-t0:5.1f}s client ICE state: {pc.iceConnectionState}", flush=True)

    await pc.setLocalDescription(await pc.createOffer())
    print("client offer candidates:", cands(pc.localDescription), flush=True)
    r = httpx.post(f"{BASE}/sessions/{sid}/api/offer", json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}, timeout=30)
    ans = r.json()
    print("server answer candidates:", cands(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"])), flush=True)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
    for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 20):
        await asyncio.sleep(1)
        if pc.iceConnectionState in ("completed", "connected"):
            await asyncio.sleep(2)
            break
    await asyncio.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 0)
    print("final client ICE state:", pc.iceConnectionState, flush=True)
    await pc.close()


asyncio.run(main())
