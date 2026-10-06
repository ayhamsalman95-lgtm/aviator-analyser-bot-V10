import json
import base64
import zlib

HAR = "aviator_provably_fair.har"

with open(HAR, "r", encoding="utf-8") as f:
    j = json.load(f)

entries = [
    x for x in j["log"]["entries"]
    if x["request"]["url"].startswith("wss://")
]

keywords = [
    b"seed",
    b"hash",
    b"fair",
    b"round",
    b"server",
    b"client",
    b"provably",
    b"commit",
    b"nonce",
]

total = 0
zlib_count = 0
other_count = 0
keyword_count = 0

print("WS ENTRIES:", len(entries))
print()

for entry in entries:

    for frame_no, msg in enumerate(
        entry.get("_webSocketMessages", []), 1
    ):

        total += 1

        data = str(msg.get("data", ""))

        try:
            raw = base64.b64decode(data)
        except Exception:
            raw = data.encode("utf-8", "replace")

        pos = raw.find(b"\x78\x9c")

        decoded = None

        if pos >= 0:
            try:
                decoded = zlib.decompress(raw[pos:])
                zlib_count += 1
            except Exception:
                pass

        if decoded is None:
            other_count += 1

        haystack = decoded if decoded is not None else raw
        low = haystack.lower()

        found = [
            k.decode()
            for k in keywords
            if k in low
        ]

        if found:
            keyword_count += 1

            print("=" * 80)
            print("FRAME:", frame_no)
            print("TYPE:", msg.get("type"))
            print("RAW LENGTH:", len(raw))
            print("ZLIB:", decoded is not None)

            if decoded is not None:
                print("DECOMPRESSED LENGTH:", len(decoded))

            print("KEYWORDS:", found)
            print("FIRST 64 RAW BYTES:")
            print(raw[:64].hex(" "))

            if decoded is not None:
                print("FIRST 64 DECOMPRESSED BYTES:")
                print(decoded[:64].hex(" "))

            print("=" * 80)
            print()

print("=" * 80)
print("TOTAL FRAMES:", total)
print("ZLIB FRAMES:", zlib_count)
print("NON-ZLIB/UNDECODED:", other_count)
print("FRAMES WITH KEYWORDS:", keyword_count)