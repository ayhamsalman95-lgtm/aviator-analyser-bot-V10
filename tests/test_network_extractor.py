"""Tests for network evidence extraction."""
import json
import tempfile
import unittest
from pathlib import Path

from tools.extract_network_evidence import NetworkExtractor


class NetworkExtractorTests(unittest.TestCase):
    """Test network evidence extraction from game_network.jsonl."""

    def test_extract_fairness_evidence(self):
        """Verify extraction of fairness evidence from network data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            source = tmpdir / "game_network.jsonl"
            
            # Create sample network data with fairness evidence
            records = [
                {
                    "kind": "sfs_decoded",
                    "command": "roundChartInfo",
                    "timestamp": 1000000.0,
                    "source": "websocket",
                    "params": {
                        "roundId": 5001,
                        "serverSeed": "abc123def456",
                        "playerSeeds": ["seed1", "seed2"],
                    }
                },
                {
                    "kind": "http_response",
                    "timestamp": 1000001.0,
                    "url": "https://api.example.com/game",
                    "body": json.dumps({
                        "roundId": 5002,
                        "roundHashSha512": "abcdef0123456789" * 8,
                        "commitment": "0123456789abcdef" * 4,
                    })
                },
                {
                    "kind": "ws_binary_undecoded",
                    "timestamp": 1000002.0,
                    "url": "wss://api.example.com/ws",
                    "type": "binary"
                }
            ]
            
            with open(source, "w") as f:
                for rec in records:
                    f.write(json.dumps(rec) + "\n")
            
            # Run extraction
            output = tmpdir / "derived_evidence.jsonl"
            extractor = NetworkExtractor(source, output)
            stats = extractor.extract()
            
            # Verify stats
            self.assertEqual(stats["total_lines"], 3)
            self.assertEqual(stats["parsed"], 3)
            self.assertGreater(stats["fairness_evidence_count"], 0)
            self.assertEqual(stats["errors"], 0)
            
            # Verify output file exists and has content
            self.assertTrue(output.exists())
            lines = output.read_text().strip().split("\n")
            self.assertGreater(len(lines), 0)
            
            # Verify output format (each line is valid JSON)
            for line in lines:
                if line:
                    rec = json.loads(line)
                    self.assertIn("kind", rec)
                    self.assertIn("timestamp", rec)

    def test_network_extractor_preserves_complete_payloads(self):
        """Verify extractor preserves complete params, not just metadata."""
        test_data = [
            # roundChartInfo with large complete payload
            {
                'kind': 'sfs_decoded',
                'timestamp': 1700000000.0,
                'command': 'sfs:roundChartInfo',
                'url': 'ws://game.example.com/socket',
                'params': {
                    'round_id': 12345,
                    'maxMultiplier': 2.50,
                    'roundHash': 'abc123def456',
                    'extra_data': 'x' * 500,  # Large payload
                }
            },
            # changeState with complete payload
            {
                'kind': 'sfs_decoded',
                'timestamp': 1700000001.0,
                'command': 'sfs:changeState',
                'url': 'ws://game.example.com/socket',
                'params': {
                    'newStateid': 5,
                    'round_id': 12345,
                    'state_details': 'y' * 300,
                }
            },
        ]
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            for rec in test_data:
                f.write(json.dumps(rec) + '\n')
            test_file = Path(f.name)
        
        try:
            output_file = test_file.parent / 'test_derived.jsonl'
            extractor = NetworkExtractor(test_file, output_file)
            stats = extractor.extract()
            
            # Verify complete payloads were preserved
            self.assertTrue(output_file.exists(), "Output file should exist")
            
            with open(output_file) as f:
                records = [json.loads(line) for line in f]
            
            # Should have 2 records
            self.assertEqual(len(records), 2)
            
            # roundChartInfo record
            round_result = records[0]
            self.assertEqual(round_result['classification'], 'round_result')
            self.assertIn('complete_params', round_result)
            # CRITICAL: complete_params should include the extra_data
            self.assertIn('extra_data', round_result['complete_params'])
            self.assertEqual(len(round_result['complete_params']['extra_data']), 500)
            
            # changeState record
            change_state = records[1]
            self.assertEqual(change_state['classification'], 'change_state')
            self.assertIn('complete_params', change_state)
            # CRITICAL: complete_params should include the state_details
            self.assertIn('state_details', change_state['complete_params'])
            self.assertEqual(len(change_state['complete_params']['state_details']), 300)
            
            # Cleanup
            output_file.unlink()
        finally:
            test_file.unlink()

    def test_classify_sfs_messages(self):
        """Verify SFS message classification."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            source = tmpdir / "game_network.jsonl"
            
            records = [
                {
                    "kind": "sfs_decoded",
                    "command": "roundChartInfo",
                    "timestamp": 1000000.0,
                    "params": {"roundId": 5001, "data": "chart"}
                },
                {
                    "kind": "sfs_decoded",
                    "command": "changeState",
                    "timestamp": 1000001.0,
                    "params": {"newStateId": 1, "roundId": 5001}
                },
                {
                    "kind": "sfs_decoded",
                    "command": "fairnessResponse",
                    "timestamp": 1000002.0,
                    "params": {"seed": "xyz"}
                },
                {
                    "kind": "sfs_decoded",
                    "command": "unknown",
                    "timestamp": 1000003.0,
                    "params": {}
                }
            ]
            
            with open(source, "w") as f:
                for rec in records:
                    f.write(json.dumps(rec) + "\n")
            
            output = tmpdir / "derived.jsonl"
            extractor = NetworkExtractor(source, output)
            stats = extractor.extract()
            
            # Verify classification counts
            self.assertGreater(stats["sfs_decoded_count"], 0)
            self.assertTrue(output.exists())
            # Verify records were written (should have sfs classification records at minimum)
            lines = output.read_text().strip().split("\n")
            self.assertGreater(len([l for l in lines if l]), 0)

    def test_websocket_frame_extraction(self):
        """Verify WebSocket frame extraction."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            source = tmpdir / "game_network.jsonl"
            
            records = [
                {
                    "kind": "ws_binary",
                    "timestamp": 1000000.0,
                    "type": "binary",
                    "url": "wss://api.example.com",
                    "data": "frame_data"
                },
                {
                    "kind": "ws_binary_undecoded",
                    "timestamp": 1000001.0,
                    "type": "binary",
                    "url": "wss://api.example.com",
                    "payload": "undecoded_data"
                }
            ]
            
            with open(source, "w") as f:
                for rec in records:
                    f.write(json.dumps(rec) + "\n")
            
            output = tmpdir / "derived.jsonl"
            extractor = NetworkExtractor(source, output)
            stats = extractor.extract()
            
            # Verify both WebSocket formats were counted
            self.assertEqual(stats["ws_binary_count"] + stats["ws_binary_undecoded_count"], 2)
            self.assertTrue(output.exists())

    def test_error_handling(self):
        """Verify error handling for malformed JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            source = tmpdir / "game_network.jsonl"
            
            # Write valid and invalid JSON
            with open(source, "w") as f:
                f.write('{"kind": "event", "timestamp": 1000000.0}\n')
                f.write('{"invalid": json}\n')  # Malformed
                f.write('{"kind": "event", "timestamp": 1000001.0}\n')
            
            output = tmpdir / "derived.jsonl"
            extractor = NetworkExtractor(source, output)
            stats = extractor.extract()
            
            # total_lines counts only non-empty, non-comment lines
            self.assertGreaterEqual(stats["total_lines"], 2)
            self.assertEqual(stats["parsed"], 2)
            self.assertEqual(stats["errors"], 1)

    def test_large_file_handling(self):
        """Verify handling of large input files (no crashes on big data)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            source = tmpdir / "game_network.jsonl"
            
            # Generate 5000 lines of network data
            with open(source, "w") as f:
                for i in range(5000):
                    if i % 100 == 0:
                        # Include actual SFS decoded records
                        rec = {
                            "kind": "sfs_decoded",
                            "timestamp": 1000000.0 + i,
                            "command": "roundChartInfo",
                            "params": {"roundId": 5000 + i, "serverSeed": f"seed{i}"},
                        }
                    else:
                        rec = {
                            "kind": "ws_text",
                            "timestamp": 1000000.0 + i,
                            "url": "wss://api.example.com",
                            "index": i,
                        }
                    f.write(json.dumps(rec) + "\n")
            
            output = tmpdir / "derived.jsonl"
            extractor = NetworkExtractor(source, output)
            stats = extractor.extract()
            
            self.assertEqual(stats["total_lines"], 5000)
            self.assertEqual(stats["parsed"], 5000)
            self.assertEqual(stats["errors"], 0)
            self.assertTrue(output.exists())
            # Verify streaming worked - should have written records
            content = output.read_text()
            self.assertGreater(len(content), 0)

    def test_gzip_support(self):
        """Verify reading gzipped JSONL files."""
        import gzip
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            source = tmpdir / "game_network.jsonl.gz"
            
            records = [
                {
                    "kind": "sfs_decoded",
                    "command": "roundChartInfo",
                    "timestamp": 1000000.0,
                    "params": {"roundId": 5001, "serverSeed": "test"}
                }
            ]
            
            # Write gzipped JSON
            with gzip.open(source, "wt", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec) + "\n")
            
            output = tmpdir / "derived.jsonl"
            extractor = NetworkExtractor(source, output)
            stats = extractor.extract()
            
            self.assertEqual(stats["total_lines"], 1)
            self.assertEqual(stats["parsed"], 1)
            self.assertTrue(output.exists())
            # Verify something was written
            content = output.read_text()
            self.assertGreater(len(content), 0)

    def test_report_generation(self):
        """Verify extraction summary report."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            source = tmpdir / "game_network.jsonl"
            
            with open(source, "w") as f:
                f.write('{"kind": "sfs_decoded", "command": "test", "timestamp": 1000000.0, "params": {}}\n')
            
            output = tmpdir / "derived.jsonl"
            extractor = NetworkExtractor(source, output)
            stats = extractor.extract()
            report = extractor.report()
            
            self.assertIn("Network Evidence Extraction Summary", report)
            self.assertIn(str(source.name), report)  # Check filename, not full path
            self.assertIn(str(output.name), report)
            self.assertIn("Total lines parsed:", report)
            self.assertIn("Derived records by classification:", report)  # New format shows classifications

    def test_http_response_not_fairness_unless_contains_fairness(self):
        """Verify HTTP responses are only classified as http_fairness when they contain actual fairness evidence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            source = tmpdir / "game_network.jsonl"
            
            # Create sample network data with mixed HTTP responses
            records = [
                # Ordinary HTTP response - HTML page (should NOT be http_fairness)
                {
                    "kind": "http_response",
                    "timestamp": 1000.0,
                    "data": {
                        "url": "https://aviator.site/index.html",
                        "status": 200,
                        "body": "<html><body>Aviator Game Page</body></html>"
                    }
                },
                # Ordinary HTTP response - CSS file (should NOT be http_fairness)
                {
                    "kind": "http_response",
                    "timestamp": 1001.0,
                    "data": {
                        "url": "https://aviator.site/styles.css",
                        "status": 200,
                        "body": "body { color: red; background: blue; }"
                    }
                },
                # Ordinary HTTP response - JavaScript file (should NOT be http_fairness)
                {
                    "kind": "http_response",
                    "timestamp": 1002.0,
                    "data": {
                        "url": "https://aviator.site/app.js",
                        "status": 200,
                        "body": "console.log('test'); function game() { return 42; }"
                    }
                },
                # Ordinary HTTP response - generic JSON without fairness structure (should NOT be http_fairness)
                {
                    "kind": "http_response",
                    "timestamp": 1003.0,
                    "data": {
                        "url": "https://api.aviator.site/config",
                        "status": 200,
                        "body": json.dumps({"game": "aviator", "version": "1.0", "enabled": True})
                    }
                },
            ]
            
            with open(source, "w") as f:
                for rec in records:
                    f.write(json.dumps(rec) + "\n")
            
            output = tmpdir / "output.jsonl"
            extractor = NetworkExtractor(source, output)
            extractor.extract()
            
            # Read output and verify classification
            output_lines = output.read_text().strip().split('\n')
            
            http_fairness_count = 0
            http_response_count = 0
            
            for line in output_lines:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get('classification') == 'http_fairness':
                    http_fairness_count += 1
                elif record.get('kind') == 'http_response' and record.get('classification') == 'http_response':
                    http_response_count += 1
            
            # With the fix: all 4 responses should be ordinary http_response, NOT http_fairness
            self.assertEqual(http_fairness_count, 0, 
                           f"Expected 0 http_fairness records (HTML/JS/CSS/generic JSON should NOT be classified as fairness), got {http_fairness_count}")
            self.assertEqual(http_response_count, 4, 
                           f"Expected 4 ordinary http_response records, got {http_response_count}")


if __name__ == "__main__":
    unittest.main()
