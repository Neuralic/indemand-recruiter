"""Phase 0b: list candidate voices from Cartesia and Fish libraries (English, North American, warm/professional).

Writes out/08_voice_libs.json with raw candidates. Never prints keys. David picks; this only narrows the library.
"""

import json
import os

import httpx
from dotenv import load_dotenv

import harness as h

load_dotenv(override=True)
CARTESIA = os.environ["CARTESIA_API_KEY"]
FISH = os.environ["FISH_API_KEY"]


def cartesia_voices() -> list[dict]:
    out, after = [], None
    for _ in range(10):
        params = {"limit": 100, "language": "en", "expand[]": "preview_file_url"}
        if after:
            params["starting_after"] = after
        r = httpx.get("https://api.cartesia.ai/voices", params=params, timeout=30,
                      headers={"Authorization": f"Bearer {CARTESIA}", "Cartesia-Version": "2026-08-14"})
        r.raise_for_status()
        d = r.json()
        out += d["data"]
        if not d.get("has_more"):
            break
        after = d.get("next_page")
    return out


def fish_voices(query_tags: list[str]) -> list[dict]:
    out = {}
    for tag in query_tags:
        for page in (1, 2, 3):
            r = httpx.get("https://api.fish.audio/model", timeout=30, headers={"Authorization": f"Bearer {FISH}"},
                          params={"language": "en", "tag": tag, "page_size": 50, "page_number": page,
                                  "sort_by": "task_count", "type": "tts"} if False else
                          {"language": "en", "tag": tag, "page_size": 50, "page_number": page, "sort_by": "task_count"})
            r.raise_for_status()
            for m in r.json()["items"]:
                out[m["_id"]] = m
    return list(out.values())


if __name__ == "__main__":
    c = cartesia_voices()
    print("cartesia en voices:", len(c))
    print("keys:", sorted(c[0].keys()) if c else None)
    f = fish_voices(["female", "male", "professional", "narration", "conversational"])
    print("fish en voices:", len(f))
    print("keys:", sorted(f[0].keys()) if f else None)
    (h.OUT / "08_voice_libs.json").write_text(json.dumps({"cartesia": c, "fish": f}, indent=1), encoding="utf-8")
