# HTTP Fairness Classification Fix

## Issue
The network extractor was incorrectly classifying **all** HTTP responses as `http_fairness`, including:
- HTML pages
- JavaScript files  
- CSS files
- Images
- Generic JSON without fairness structure

This caused inflated `http_fairness` counts (e.g., 17,400 out of 17,400 HTTP responses marked as fairness when they were actually ordinary assets).

## Root Cause
In `tools/extract_network_evidence.py`, HTTP responses were automatically assigned `classification = "http_fairness"` without checking if the response body actually contained fairness evidence.

```python
# BEFORE (incorrect)
classification = "http_fairness"  # Assigned to ALL HTTP responses!

# Try to extract fairness, but don't use result to determine classification
fairness_recs = extract_fairness(body_data, max_nodes=500)
for fr in fairness_recs:
    if fr.meaningful():
        # Create fairness_evidence record
        pass

# classification stays as "http_fairness" regardless
```

## Solution
Only assign `classification = "http_fairness"` when the response body actually contains meaningful fairness evidence, verified by `extract_fairness()`.

```python
# AFTER (correct)
classification = "http_response"  # Default: ordinary response
has_fairness_evidence = False

fairness_recs = extract_fairness(body_data, max_nodes=500)
for fr in fairness_recs:
    if fr.meaningful():
        has_fairness_evidence = True
        # Create fairness_evidence record
        pass

# Only classify as http_fairness if actual fairness was found
if has_fairness_evidence:
    classification = "http_fairness"
```

## Implementation Details
- Modified `_extract_from_line()` method to check `extract_fairness()` results before assigning `http_fairness` classification
- Added counter `has_fairness_evidence` to track whether meaningful fairness records were actually extracted
- HTTP responses without fairness structure now correctly classified as ordinary `http_response`
- Only responses containing actual fairness evidence (recognized by `extract_fairness()`) are classified as `http_fairness`

## Test Coverage
Added new test: `test_http_response_not_fairness_unless_contains_fairness`
- Verifies HTML pages are NOT classified as `http_fairness`
- Verifies JavaScript files are NOT classified as `http_fairness`  
- Verifies CSS files are NOT classified as `http_fairness`
- Verifies generic JSON without fairness structure is NOT classified as `http_fairness`
- Confirms only responses with actual fairness evidence get `http_fairness` classification

## Expected Results After Fix
With the corrected classification logic applied to the real `game_network.jsonl` file (88,749 lines):

**Before fix:**
- http_fairness: 17,400 (all HTTP responses incorrectly classified)

**After fix:**
- http_fairness: significantly lower (only responses with actual fairness structure)
- http_response: accounts for all ordinary assets (HTML, JS, CSS, images, fonts, etc.)

## Files Modified
- `tools/extract_network_evidence.py` - HTTP response classification logic
- `tests/test_network_extractor.py` - Added classification verification test

## Test Results
- Total tests: 88 (87 pass, 1 skip)
- New test: `test_http_response_not_fairness_unless_contains_fairness` ✓ PASS
- All existing tests: ✓ PASS
