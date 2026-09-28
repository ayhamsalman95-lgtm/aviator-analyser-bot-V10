import unittest

from aviator.sfs_codec import SfsDecoder


class SfsDecoderBoundaryTests(unittest.TestCase):
    def test_packet_spans_track_consumed_boundaries(self):
        packets = [(object(), 3), (object(), 2)]
        state = {"i": 0}

        def decode(_data):
            obj, consumed = packets[state["i"]]
            state["i"] += 1
            return obj, consumed

        def parse(obj):
            return "cmd", {"obj": id(obj)}

        result = SfsDecoder(decode=decode, parse=parse).decode_frame(b"abcde")
        self.assertEqual(result.packets, 2)
        self.assertEqual(result.consumed, 5)
        self.assertEqual(result.leftover, 0)
        self.assertEqual(result.packet_spans, [
            {"index": 0, "offset": 0, "end": 3, "length": 3},
            {"index": 1, "offset": 3, "end": 5, "length": 2},
        ])


if __name__ == "__main__":
    unittest.main()
