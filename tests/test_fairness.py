import hashlib
import json
import unittest

from tests.helpers import FIXTURES, TempProject
from aviator import fairness
from aviator.extract import extract_fairness

VEC = json.loads((FIXTURES / "fairness_vector.json").read_text())


class FormulaTests(unittest.TestCase):
    def test_public_reference_hash(self):
        # Published worked example (round 4112625): first 13 hex f0fbffc79944c -> 16.53x
        h = "f0fbffc79944c21b4ac70740860fdf88b1e724c743eb04b65bec578492b1e737049d77536500ba1e12b7a1e326fafac594fd98e07b835ee863b804c8e86808b3"
        self.assertEqual(fairness.cents_from_hash(h), 1653)

    def test_minimum_is_one(self):
        self.assertEqual(fairness.cents_from_hash("0000000000000" + "0" * 115), 100)

    def test_sha512_concat_no_separator_no_nonce(self):
        d = fairness.round_hash(VEC["server_seed"], VEC["player_seeds"])
        self.assertEqual(d, hashlib.sha512((VEC["server_seed"] + "".join(VEC["player_seeds"])).encode()).hexdigest())


class VerifyTests(unittest.TestCase):
    def v(self, cents=None, ss=None, seeds=None, commits=(), panels=()):
        return fairness.verify_round(1, VEC["cents"] if cents is None else cents,
                                     [VEC["server_seed"]] if ss is None else ss,
                                     [VEC["player_seeds"]] if seeds is None else seeds, list(commits), list(panels),
                                     evidence_semantics_confirmed=True)

    def test_verification_requires_explicit_semantics(self):
        r = fairness.verify_round(1, VEC["cents"], [VEC["server_seed"]],
                                  [VEC["player_seeds"]], [VEC["commitment_sha256"]], [VEC["sha512"]])
        self.assertFalse(r.verified)
        self.assertEqual(r.status, "not_verifiable")

    def test_verified_only_when_reproduced_with_confirmed_semantics(self):
        r = self.v(commits=[VEC["commitment_sha256"]], panels=[VEC["sha512"]])
        self.assertTrue(r.verified)
        self.assertEqual(r.status, "verified")

    def test_wrong_multiplier(self):
        self.assertEqual(self.v(cents=VEC["cents"] + 1).status, "mismatch")

    def test_two_seeds_incomplete(self):
        r = self.v(seeds=[VEC["player_seeds"][:2]])
        self.assertFalse(r.verified)
        self.assertEqual(r.status, "incomplete")

    def test_wrong_commitment(self):
        r = self.v(commits=["b" * 64])
        self.assertFalse(r.verified)
        self.assertEqual(r.status, "mismatch")

    def test_conflicting_server_seeds(self):
        r = self.v(ss=[VEC["server_seed"], "OtherSeed1234567"])
        self.assertEqual(r.status, "conflict")

    def test_no_recorded_result(self):
        r = fairness.verify_round(1, None, [VEC["server_seed"]], [VEC["player_seeds"]], [], [])
        self.assertEqual(r.status, "not_verifiable")


class ExtractTests(unittest.TestCase):
    def test_generic_hash_and_seed_ignored(self):
        data = {"hash": "a" * 64, "seed": "abcdef123456", "sh": "c" * 64, "cs": "x" * 32,
                "text": "server seed: " + "d" * 64, "blob": "e" * 128}
        self.assertEqual(extract_fairness(data), [])

    def test_nested_fairness_round_id_not_inherited(self):
        data = {"roundId": 900, "fairness": {"roundId": 899, "serverSeed": "SeedSeedSeed1234"}}
        recs = extract_fairness(data)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].round_id, 899)

    def test_fairness_without_own_round_id_is_unassociated(self):
        data = {"roundId": 900, "fairness": {"serverSeed": "SeedSeedSeed1234"}}
        self.assertIsNone(extract_fairness(data)[0].round_id)

    def test_player_seed_order_and_privacy(self):
        data = {"roundId": 5, "playerSeeds": [{"seed": "aaaa1111", "username": "bob", "profileImage": "x"},
                                              "bbbb2222", {"clientSeed": "cccc3333"}]}
        self.assertEqual(extract_fairness(data)[0].player_seeds, ["aaaa1111", "bbbb2222", "cccc3333"])

    def test_spribe_profile_confirms_seed_hash_fields(self):
        data = {
            "roundId": 4112625,
            "serverSeed": VEC["server_seed"],
            "playerSeeds": VEC["player_seeds"],
            "serverSeedSHA256": VEC["commitment_sha256"],
            "roundHashSHA512": VEC["sha512"],
        }
        recs = extract_fairness(data, profile=__import__("aviator.extract", fromlist=["SPRIBE_AVIATOR_PROFILE"]).SPRIBE_AVIATOR_PROFILE)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.commitment, VEC["commitment_sha256"])
        self.assertEqual(rec.round_hash, VEC["sha512"])
        self.assertEqual(
            {o["semantic_type"] for o in rec.crypto_observations},
            {"commitment_sha256", "round_hash_sha512"},
        )

    def test_spribe_profile_does_not_promote_wrong_hashes(self):
        data = {
            "roundId": 4112625,
            "serverSeed": VEC["server_seed"],
            "playerSeeds": VEC["player_seeds"],
            "serverSeedSHA256": "a" * 64,
            "roundHashSHA512": "b" * 128,
        }
        from aviator.extract import SPRIBE_AVIATOR_PROFILE
        rec = extract_fairness(data, profile=SPRIBE_AVIATOR_PROFILE)[0]
        self.assertIsNone(rec.commitment)
        self.assertIsNone(rec.round_hash)
        self.assertEqual({o["semantic_type"] for o in rec.crypto_observations}, {"unknown"})

    def test_no_nonce(self):
        recs = extract_fairness({"roundId": 5, "nonce": 77, "serverSeed": "SeedSeedSeed1234"})
        self.assertFalse(hasattr(recs[0], "nonce"))

    def test_seed_sha256_is_neutral_observation(self):
        for value in ("a" * 64, "b" * 128):
            recs = extract_fairness({"roundId": 123, "seedSHA256": value})
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0].round_id, 123)
            self.assertIsNone(recs[0].round_hash)
            self.assertEqual(recs[0].crypto_observations[0]["semantic_type"], "unknown")

    def test_round_hash_named_field_is_neutral_without_protocol_semantics(self):
        value = "b" * 128
        recs = extract_fairness({"roundId": 124, "roundHashSha512": value})
        self.assertEqual(len(recs), 1)
        self.assertIsNone(recs[0].round_hash)
        self.assertEqual(recs[0].crypto_observations[0]["algorithm"], "SHA-512")


class StoreFairnessTests(unittest.TestCase):
    def setUp(self):
        self.p = TempProject()
        self.s = self.p.store

    def tearDown(self):
        self.p.cleanup()

    def test_evidence_before_result_race(self):
        self.s.add_fairness_evidence("server_seed", VEC["server_seed"], "t", 55, "explicit")
        self.s.add_fairness_evidence("player_seeds", VEC["player_seeds"], "t", 55, "explicit")
        self.assertEqual(self.s.get_verification(55)["status"], "not_verifiable")
        self.s.insert_round(55, VEC["cents"] / 100, "sfs:roundChartInfo")
        v = self.s.get_verification(55)
        self.assertFalse(v["verified"])
        self.assertFalse(v["locked"])

    def test_ambiguous_verification_is_not_locked(self):
        self.s.insert_round(55, VEC["cents"] / 100, "sfs:roundChartInfo")
        self.s.add_fairness_evidence("server_seed", VEC["server_seed"], "t", 55, "explicit")
        self.s.add_fairness_evidence("player_seeds", VEC["player_seeds"], "t", 55, "explicit")
        self.assertFalse(self.s.get_verification(55)["verified"])
        self.s.add_fairness_evidence("server_seed", "Different1234567", "late", 55, "explicit")
        self.assertFalse(self.s.get_verification(55)["verified"])
        self.assertFalse(self.s.get_verification(55)["locked"])

    def test_wrong_early_value_can_be_corrected(self):
        # Ambiguous cryptographic observations remain non-verifiable rather than
        # being promoted to a conflict or verification result.
        self.s.insert_round(56, VEC["cents"] / 100, "sfs:roundChartInfo")
        self.s.add_fairness_evidence("commitment_sha256", "b" * 64, "t", 56, "explicit")
        self.s.add_fairness_evidence("commitment_sha256", VEC["commitment_sha256"], "t", 56, "explicit")
        self.assertEqual(self.s.get_verification(56)["status"], "not_verifiable")

    def test_unassociated_never_used(self):
        self.s.insert_round(57, VEC["cents"] / 100, "sfs:roundChartInfo")
        self.s.add_fairness_evidence("server_seed", VEC["server_seed"], "t", None, "unassociated", 57)
        self.s.add_fairness_evidence("player_seeds", VEC["player_seeds"], "t", None, "unassociated", 57)
        self.assertIsNone(self.s.get_verification(57))


if __name__ == "__main__":
    unittest.main()
