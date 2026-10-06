import json
import base64
import struct

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

    if b"serverSeedSHA256" not in raw:
        continue

    p = raw.find(b"serverSeedSHA256")
    h = raw[p+18:p+82].decode("ascii", "ignore")

    print()
    print("HASH FRAME:", i + 1)
    print("HASH:", h)

    found = []

    for n in range(max(0, i - 100), min(len(frames), i + 101)):
        try:
            r = base64.b64decode(str(frames[n].get("data", "")))
        except:
            continue

        p2 = r.find(b"roundId")

        if p2 >= 0 and p2 + 12 <= len(r):
            try:
                rid = struct.unpack(">I", r[p2+8:p2+12])[0]
                if rid not in found:
                    found.append(rid)
            except:
                pass

    print("ROUND IDs NEAR HASH:", found)