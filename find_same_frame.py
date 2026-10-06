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
        continue

    if b"serverSeedSHA256" in raw and b"roundId" in raw:
        print("FOUND SAME FRAME:", i + 1)
        print(raw.hex(" "))