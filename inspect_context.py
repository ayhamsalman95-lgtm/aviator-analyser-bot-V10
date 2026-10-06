import json
import base64

HAR = "aviator_provably_fair.har"

with open(HAR, "r", encoding="utf-8") as f:
    j = json.load(f)

entries = [
    x for x in j["log"]["entries"]
    if x["request"]["url"].startswith("wss://")
]

targets = {112, 114, 606, 609, 677, 679, 840, 841}

for entry in entries:
    frames = entry.get("_webSocketMessages", [])

    for frame_no in sorted(targets):
        if frame_no > len(frames):
            continue

        print("=" * 100)
        print("TARGET FRAME:", frame_no)

        start = max(1, frame_no - 5)
        end = min(len(frames), frame_no + 5)

        for n in range(start, end + 1):
            msg = frames[n - 1]
            data = str(msg.get("data", ""))

            try:
                raw = base64.b64decode(data)
            except Exception:
                raw = data.encode("utf-8", "replace")

            text = "".join(
                chr(b) if 32 <= b <= 126 else "."
                for b in raw
            )

            print()
            print("FRAME:", n)
            print("TYPE:", msg.get("type"))
            print("LEN:", len(raw))
            print("ASCII:", text)

        print()