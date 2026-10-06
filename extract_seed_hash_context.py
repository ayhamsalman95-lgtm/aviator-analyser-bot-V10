import json
import base64
import re

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

    p = raw.find(b"serverSeedSHA256")
    if p < 0:
        continue

    # القيمة تبدأ بعد: serverSeedSHA256 + type/length bytes
    tail = raw[p:p+100]
    text = "".join(chr(x) if 32 <= x <= 126 else " " for x in tail)

    print("=" * 70)
    print("FRAME:", i + 1)
    print("TYPE:", m.get("type"))
    print("SEED MESSAGE:", text)

    print("\nNEARBY ROUND IDS:")

    for n in range(max(0, i - 80), min(len(frames), i + 81)):
        try:
            r = base64.b64decode(str(frames[n].get("data", "")))
        except:
            continue

        for match in re.finditer(b"roundId", r):
            pos = match.start()

            if pos + 12 <= len(r):
                value = int.from_bytes(r[pos+8:pos+12], "big")

                if 5_000_000 < value < 6_000_000:
                    print("FRAME", n + 1, "ROUND", value)

    print()