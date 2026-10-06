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
        raw = b""

    if b"serverSeedSHA256" not in raw:
        continue

    print("\nSEED", i + 1)

    for n in range(max(0, i - 30), min(len(frames), i + 31)):
        try:
            r = base64.b64decode(str(frames[n].get("data", "")))
        except:
            continue

        if b"roundId" in r:
            p = r.find(b"roundId")
            print("ROUND FRAME", n + 1, r[p:p+30].hex(" "))
