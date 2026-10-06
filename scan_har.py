import json
import base64
import zlib
import re

HAR = "aviator_provably_fair.har"

with open(HAR, "r", encoding="utf-8") as f:
    j = json.load(f)

entries = [
    x for x in j["log"]["entries"]
    if x["request"]["url"].startswith("wss://")
]

keywords = [
    "seed",
    "hash",
    "fair",
    "round",
    "server",
    "client",
    "prov",
    "commit",
    "nonce",
    "salt",
    "random",
    "crash",
    "result",
]

print("WS ENTRIES:", len(entries))
print()

total = 0
compressed = 0
interesting = 0

for entry in entries:

    for frame_no, msg in enumerate(
        entry.get("_webSocketMessages", []), 1
    ):

        data = str(msg.get("data", ""))

        try:
            raw = base64.b64decode(data)
        except Exception:
            continue

        pos = raw.find(b"\x78\x9c")

        if pos < 0:
            continue

        compressed += 1

        try:
            decoded = zlib.decompress(raw[pos:])
        except Exception:
            continue

        total += 1

        # كل النصوص ASCII الموجودة داخل الـbinary
        strings = re.findall(
            rb"[A-Za-z][A-Za-z0-9_.-]{2,}",
            decoded
        )

        names = sorted(
            set(
                s.decode("ascii", "ignore")
                for s in strings
            )
        )

        found = []

        for name in names:
            low = name.lower()

            if any(k in low for k in keywords):
                found.append(name)

        if not found:
            continue

        interesting += 1

        print("=" * 80)
        print("INTERESTING FRAME:", frame_no)
        print("TYPE:", msg.get("type"))
        print("DECOMPRESSED:", len(decoded))
        print()
        print("KEYWORDS FOUND:")

        for name in found:
            print("  ", name)

        print()
        print("ALL TEXT FIELDS:")

        for name in names:
            print("  ", name)

        print()
        print("FIRST 256 BYTES:")
        print(decoded[:256].hex(" "))

        print("=" * 80)
        print()

print("=" * 80)
print("TOTAL COMPRESSED:", compressed)
print("TOTAL DECOMPRESSED:", total)
print("INTERESTING FRAMES:", interesting)