import unittest

from aviator.sfs_codec import SfsDecoder


class SfsDecoderBoundaryTests(unittest.TestCase):
    def test_invalid_consumed_values_are_rejected(self):
        for consumed in (0, -1, True, False, 6):
            result = SfsDecoder(
                decode=lambda _data, consumed=consumed: (object(), consumed),
                parse=lambda _obj: ("cmd", {}),
            ).decode_frame(b"abcde")
            self.assertEqual(result.packets, 0)
            self.assertIn(result.decode_status, {"malformed", "incomplete"})

    def test_decoder_exception_preserves_failure_metadata(self):
        def decode(_data):
            raise RuntimeError("decoder exploded")
        result = SfsDecoder(decode=decode, parse=lambda _obj: ("cmd", {})).decode_frame(b"abc")
        self.assertEqual(result.packets, 0)
        self.assertEqual(result.decode_status, "exception")
        self.assertEqual(result.error_stage, "decode")
        self.assertIn("decoder exploded", result.error)

    def test_parse_exception_preserves_decoded_packet(self):
        result = SfsDecoder(
            decode=lambda _data: ({"raw": True}, 3),
            parse=lambda _obj: (_ for _ in ()).throw(ValueError("bad parse")),
        ).decode_frame(b"abc")
        self.assertEqual(result.packets, 1)
        self.assertEqual(result.decode_status, "packet_error")
        self.assertEqual(result.commands[0][0], None)
        self.assertEqual(result.commands[0][1], {"raw": True})

    def test_trailing_bytes_are_observable(self):
        result = SfsDecoder(
            decode=lambda _data: ({"raw": True}, 2),
            parse=lambda _obj: ("cmd", {}),
        ).decode_frame(b"abc")
        self.assertEqual(result.packets, 1)
        self.assertEqual(result.leftover, 1)
        self.assertEqual(result.decode_status, "incomplete")
        self.assertIsNotNone(result.remaining_b64)

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
