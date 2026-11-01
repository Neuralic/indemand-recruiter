"""Headless simulated candidate for the Phase 1 pipeline (TEST HARNESS ONLY - never shown as a real person anywhere).

Connects to the running 26_live_interview server over WebRTC like a browser would, speaks pre-synthesised answers (OpenAI TTS) one
at a time after the bot has been quiet for QUIET_S, and timestamps everything it hears. This measures end-to-end latency through the
real WebRTC path (end of my clip -> first bot audio) without needing a human.

usage: 28_sim_candidate.py [script_name] [--silent SECONDS]
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from fractions import Fraction

import httpx
import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame

import harness as h
import sim_scripts

BASE = "http://localhost:7860"
SIM = h.OUT / "sim"
SIM.mkdir(exist_ok=True)
QUIET_S = 3.0  # bot must be silent this long before the next answer (an acknowledgement and the question can have a ~1 s gap)
BOT_RMS = 300  # int16 RMS above this on the received track counts as bot speech

EXPECT = {
    "n_consent": "What's prompting you", "n_rapport": "two-minute snapshot", "n_background": "industrial electrical power systems",
    "n_tech_vague": "one specific system", "n_tech_specific": "programmable logic controllers",
    "n_controls": "hazardous location electrical work", "n_salary_mid": "Stefanie will confirm that",
    "n_haz_vague": "one specific hazardous location project", "n_haz_vague2": "reviewing, authenticating and stamping",
    "n_pe": "the team you've led", "n_team": "estimate through to commissioning", "n_project": "legally eligible",
    "n_work_auth": "good standing with APEGA", "n_pe_apega": "degree in electrical engineering", "n_degree": "twenty percent",
    "n_travel": "based there now", "n_location": "salary range are you targeting", "n_salary": "notice period",
    "n_notice": "what questions do you have", "n_client": "Anything else", "n_cq2": "Anything else",
    "n_done": "reviews every interview personally",
}

SCRIPTS = {
    "full": [
        ("n_consent", "Yes sure, let's get started."),
        ("n_rapport", "Honestly my plant is being sold, and I'm looking for a bigger team to lead and more room to grow."),
        ("n_background", "Sure. I'm an electrical supervisor at an industrial plant, leading six people. I'm a professional engineer "
                         "registered with APEGA, and I've run power distribution and controls upgrades there."),
        ("n_tech_vague", "I've worked on a lot of electrical systems over the years, and I just try to make sure things are done properly."),
        ("n_tech_specific", "At a gas processing facility I designed the power distribution for a new compressor building. It was three-phase "
                            "six hundred volt, a motor control centre with variable frequency drives on the compressor motors, and I "
                            "specified the grounding and bonding. It energized on schedule and passed inspection the first time."),
        ("n_controls", "I've programmed programmable logic controllers and built the human-machine interface screens. On our last skid I "
                       "commissioned the instrumentation loops and cut start-up faults by half."),
        ("n_salary_mid", "Before I answer, what's the salary for this role?"),
        ("n_haz_vague", "I know the Canadian Electrical Code well, and we always follow the rules in classified areas."),
        ("n_haz_vague2", "Like I said, I follow the code and I take it seriously."),
        ("n_pe", "I'm the responsible professional engineer on our drawings. Last year I reviewed and stamped about forty sets under APEGA, "
                 "checking the load calculations and protection coordination before every stamp."),
        ("n_team", "I lead six people, two engineers-in-training, two programmers and two designers. I paired a struggling engineer-in-training "
                   "with a senior engineer for three months and they passed their exam."),
        ("n_project", "I took a three million dollar motor control centre replacement from estimate through commissioning, ran weekly client "
                      "meetings, and we delivered two weeks early."),
        ("n_work_auth", "Yes, I'm legally eligible to work in Canada."),
        ("n_pe_apega", "Yes, I'm a professional engineer in good standing with APEGA."),
        ("n_degree", "Yes, I have a bachelor's degree in electrical engineering from the University of Alberta."),
        ("n_travel", "Yes, travel up to twenty percent is fine."),
        ("n_location", "I'm in Calgary and willing to relocate to Edmonton."),
        ("n_salary", "Around one hundred forty to one hundred sixty thousand dollars."),
        ("n_notice", "Four weeks."),
        ("n_client", "Who is the client?"),
        ("n_cq2", "What's the salary range and the benefits?"),
        ("n_done", "No, that's all, thanks."),
    ],
}


EXPECT.update(sim_scripts.EXPECT)
SCRIPTS.update(sim_scripts.SCRIPTS)


def newest_session_log():
    logs = sorted(h.OUT.glob("26_interview_*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def bot_said(since_size: int, needle: str) -> bool:
    """True once the server log (after byte offset `since_size`) shows the TTS being asked to speak a line containing `needle`."""
    lg = newest_session_log()
    if not lg:
        return False
    data = lg.read_bytes()[since_size:].decode("utf-8", "ignore")
    return any("Generating TTS [" in ln and needle.lower() in ln.lower() for ln in data.splitlines())


def synth_piece(label: str, text: str) -> bytes:
    p = SIM / f"{label}_{hashlib.md5(text.encode()).hexdigest()[:8]}.pcm"
    if not p.exists():
        from openai import OpenAI
        r = OpenAI(api_key=h.api_key()).audio.speech.create(model="gpt-4o-mini-tts", voice="onyx", input=text, response_format="pcm",
                                                              instructions="Speak naturally, like a candidate in a job interview.")
        p.write_bytes(r.content)
    return p.read_bytes()  # 24 kHz s16 mono


def synth(label: str, text: str) -> bytes:
    """`|1.5|` inside the text = 1.5 s of silence (a thinking pause / a fragment boundary)."""
    parts = re.split(r"\|(\d+(?:\.\d+)?)\|", text)
    out = bytearray()
    for i, part in enumerate(parts):
        if i % 2:
            out.extend(bytes(int(float(part) * 24000) * 2))
        elif part.strip():
            out.extend(synth_piece(f"{label}_{i}", part.strip()))
    return bytes(out)


class ClipTrack(MediaStreamTrack):
    """Real-time 48 kHz mono mic: silence unless a clip is queued."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.buf = bytearray()  # 48 kHz s16 mono
        self.pts = 0
        self.t0 = None
        self.sent_end = None  # wall time the last queued clip finished leaving the buffer

    def queue(self, pcm24: bytes) -> None:
        a = np.frombuffer(pcm24, dtype=np.int16)
        self.buf.extend(np.repeat(a, 2).tobytes())

    async def recv(self):
        if self.t0 is None:
            self.t0 = time.time()
        n = 960
        wait = self.t0 + (self.pts / 48000) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        if self.buf:
            data = bytes(self.buf[: n * 2]).ljust(n * 2, bytes(1))  # pad the final partial frame with silence
            del self.buf[: n * 2]
            if not self.buf:
                self.sent_end = time.time()
        else:
            data = np.zeros(n, dtype=np.int16).tobytes()
        f = AudioFrame.from_ndarray(np.frombuffer(data, dtype=np.int16).reshape(1, -1), format="s16", layout="mono")
        f.sample_rate = 48000
        f.pts = self.pts
        f.time_base = Fraction(1, 48000)
        self.pts += n
        return f


def arg_after(flag: str) -> str:
    a = sys.argv[1:]
    return a[a.index(flag) + 1] if flag in a else ""


async def main() -> None:
    args = sys.argv[1:]
    silent = float(args[args.index("--silent") + 1]) if "--silent" in args else 0
    script = SCRIPTS[args[0] if args and not args[0].startswith("--") else "full"]
    clips = [(lab, txt, synth(lab, txt)) for lab, txt in script]
    sid = httpx.post(f"{BASE}/start", json={"transport": "webrtc", "enableDefaultIceServers": False}).json()["sessionId"]
    pc = RTCPeerConnection()
    mic = ClipTrack()
    pc.addTrack(mic)
    dc = pc.createDataChannel("pipecat")
    rtvi_msgs: list = []

    @dc.on("message")
    def _on_msg(m):
        try:
            d = json.loads(m)
            if d.get("type") in ("server-message", "bot-output", "bot-tts-text"):
                rtvi_msgs.append(d)
                print(f"    [rtvi] {d.get('type')}: {json.dumps(d.get('data'))[:200]}", flush=True)
        except Exception:  # noqa: BLE001
            pass
    bursts: list[list[float]] = []  # [start, end] wall times of bot speech
    last_bot = {"t": 0.0}
    done = asyncio.Event()

    @pc.on("track")
    def on_track(track):
        async def consume():
            try:
                while True:
                    fr = await track.recv()
                    a = fr.to_ndarray().astype(np.int32)
                    rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) if a.size else 0.0
                    now = time.time()
                    if rms > BOT_RMS:
                        if not bursts or now - bursts[-1][1] > 0.35:
                            bursts.append([now, now])
                        else:
                            bursts[-1][1] = now
                        last_bot["t"] = now
            except Exception as e:  # noqa: BLE001 - track ended
                print(f"    [client] bot track ended: {type(e).__name__}", flush=True)
                done.set()
        asyncio.create_task(consume())

    @pc.on("connectionstatechange")
    async def _():
        print(f"    [client] connection state {pc.connectionState} at +{time.time() - t_offer:.2f}s after the offer", flush=True)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            done.set()

    await pc.setLocalDescription(await pc.createOffer())
    t_offer = time.time()
    r = httpx.post(f"{BASE}/sessions/{sid}/api/offer", json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "request_data": {"sim": True, "faults": arg_after("--faults"), "fish_faults": arg_after("--fish-faults")}}, timeout=30)
    t_answer = time.time()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=r.json()["sdp"], type=r.json()["type"]))
    t_start = time.time()
    print("connected, waiting for the greeting ...", flush=True)

    async def report_first_audio():
        for _ in range(300):
            if bursts:
                print(f"CONNECT -> FIRST BOT AUDIO: {bursts[0][0] - t_offer:.2f}s (offer POST sent -> first audible bot audio at the client; "
                      f"answer received at +{t_answer - t_offer:.2f}s)", flush=True)
                return
            await asyncio.sleep(0.02)
    asyncio.create_task(report_first_audio())
    log = []

    async def wait_bot_quiet(timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout and not done.is_set():
            if bursts and time.time() - last_bot["t"] >= QUIET_S:
                return True
            await asyncio.sleep(0.1)
        return False

    if silent:
        await wait_bot_quiet()
        print(f"greeting done; staying silent for {silent}s", flush=True)
        t = time.time()
        while time.time() - t < silent and not done.is_set():
            await asyncio.sleep(0.5)
        print("bot bursts (s since start):", [(round(a - t_start, 1), round(b - t_start, 1)) for a, b in bursts], flush=True)
        await pc.close()
        return

    if os.environ.get("SIM_GREETING_INTERRUPT"):  # greeting-integrity test: a click 1.2 s after connect, then speech over the disclosure
        async def talk_over():
            await asyncio.sleep(max(0, t_start + 1.2 - time.time()))
            click = (np.random.default_rng(1).standard_normal(1200) * 9000).astype(np.int16)  # 50 ms burst
            mic.queue(click.tobytes())
            print("    [sim] click at +1.2 s", flush=True)
            await asyncio.sleep(max(0, t_start + 4.0 - time.time()))
            mic.queue(synth("g_over", "Yes sure, go ahead."))
            print("    [sim] spoke over the greeting at +4 s", flush=True)
        asyncio.create_task(talk_over())
        await asyncio.sleep(max(0, t_start + 33.0 - time.time()))  # the disclosure takes ~30 s; the scripted consent comes after it

    for label, text, pcm in clips:
        if done.is_set():
            print("session ended by the server before the script finished", flush=True)
            break
        if not await wait_bot_quiet(90):
            print(f"[{label}] bot never went quiet / session ended", flush=True)
            break
        n_before = len(bursts)
        lg = newest_session_log()
        log_off = lg.stat().st_size if lg else 0
        mic.sent_end = None
        t_q = time.time()
        mic.queue(pcm)
        dur = len(pcm) / 2 / 24000
        print(f"[{label}] speaking {dur:.1f}s: {text[:70].replace('|', ' ... ')}", flush=True)
        while mic.sent_end is None and not done.is_set():
            await asyncio.sleep(0.02)
        t_end = mic.sent_end or time.time()
        print(f"    clip left the mic at +{t_end - t_start:.1f}s; bot bursts so far {len(bursts)} (n_before={n_before}), done={done.is_set()}", flush=True)
        # first bot burst after my clip ended
        for _ in range(900):
            if len(bursts) > n_before and bursts[-1][0] > t_end or done.is_set():
                break
            await asyncio.sleep(0.05)
        new = [b for b in bursts if b[0] > t_end]
        first = new[0][0] - t_end if new else None
        log.append({"label": label, "clip_s": round(dur, 2), "t_end": t_end - t_start, "first_bot_audio_s": None if first is None else round(first, 2)})
        print(f"    -> first bot audio {first if first is None else round(first, 2)}s after I stopped", flush=True)
        need = EXPECT.get(label)
        if need:
            t_w = time.time()
            while time.time() - t_w < 45 and not done.is_set() and not bot_said(log_off, need):
                await asyncio.sleep(0.2)
            ok = bot_said(log_off, need)
            print(f"    expected bot line {need!r}: {'seen' if ok else 'NOT SEEN'} after {time.time() - t_end:.1f}s", flush=True)
            log[-1]["expected"] = need
            log[-1]["expected_seen_after_s"] = round(time.time() - t_end, 2) if ok else None
    await asyncio.sleep(QUIET_S + 3)
    await wait_bot_quiet(15)
    print("bot bursts (s since start):", [(round(a - t_start, 1), round(b - t_start, 1)) for a, b in bursts], flush=True)
    (h.OUT / "28_sim_candidate.json").write_text(json.dumps({"turns": log, "bursts": [[a - t_start, b - t_start] for a, b in bursts]}), encoding="utf-8")
    print("server closed the call:", done.is_set(), flush=True)
    await pc.close()


asyncio.run(main())
