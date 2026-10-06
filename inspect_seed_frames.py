import json
import base64

HAR = "aviator_provably_fair.har"

with open(HAR, "r", encoding="utf-8") as f:
    j = json.load(f)

entries = [
    x for x in j["log"]["entries"]
    if x["request"]["url"].startswith("wss://")
]

for entry in entries:

    for frame_no, msg in enumerate(
        entry.get("_webSocketMessages", []), 1
    ):

        data = str(msg.get("data", ""))

        try:
            raw = base64.b64decode(data)
        except Exception:
            raw = data.encode("utf-8", "replace")

        low = raw.lower()

        if b"serverseedsha256" not in low:
            continue

        print("=" * 100)
        print("FRAME:", frame_no)
        print("TYPE:", msg.get("type"))
        print("RAW LENGTH:", len(raw))
        print()

        print("ASCII:")
        text = "".join(
            chr(b) if 32 <= b <= 126 else "."
            for b in raw
        )
        print(text)

        print()
        print("HEX:")
        print(raw.hex(" "))

        print("=" * 100)
        print()
