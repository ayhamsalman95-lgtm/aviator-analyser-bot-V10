import json
import base64

with open("aviator_provably_fair.har", "r", encoding="utf-8") as f:
    j = json.load(f)

frames = [
    m
    for e in j["log"]["entries"]
    if e["request"]["url"].startswith("wss://")
    for m in e.get("_webSocketMessages", [])
]

for i, m in enumerate(frames):
    try:
        raw = base64.b64decode(str(m.get("data", "")))
    except:
        raw = str(m.get("data", "")).encode()

    low = raw.lower()

    if b"serverseedsha256" in low:
        print("=" * 60)
        print("SEED FRAME:", i + 1)

        for n in range(max(0, i - 2), min(len(frames), i + 3)):
            try:
                r = base64.b64decode(str(frames[n].get("data", "")))
            except:
                r = str(frames[n].get("data", "")).encode()

            text = "".join(chr(x) if 32 <= x <= 126 else " " for x in r)

            interesting = [
                x for x in [
                    "roundId",
                    "serverSeedSHA256",
                    "serverSeedHandler",
                    "serverSeedResponse",
                    "roundResult"
                ]
                if x.lower() in text.lower()
            ]

            if interesting:
                print(n + 1, frames[n].get("type"), interesting)
                print(text[:500])