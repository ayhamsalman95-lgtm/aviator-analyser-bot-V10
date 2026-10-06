# Applying V12 to GitHub on a dedicated branch

Run from a clone of the repository (Git Bash or PowerShell), with the ZIP extracted to `..\v12`.

```bash
git checkout main && git pull
git checkout -b v12-canonical-storage

# 1) keep a local copy of the runtime evidence BEFORE untracking it
mkdir -p ../aviator-evidence-backup && cp rounds.json history.json seeds.jsonl diagnostics.jsonl \
  live_state.json collector_status.json subscribers.json telegram_sent.json telegram_errors.log \
  ../aviator-evidence-backup/ 2>/dev/null

# 2) stop tracking runtime/sensitive files (files stay on disk)
git rm --cached rounds.json history.json seeds.jsonl diagnostics.jsonl live_state.json \
  collector_status.json subscribers.json telegram_sent.json telegram_errors.log curl

# 3) remove replaced source files
git rm seeds.py store.py fairness.py predictor.py patch_fairness.py

# 4) copy V12 in
cp -r ../v12/* ../v12/.gitignore .
git add -A
git commit -m "V12: SQLite/JSONL canonical storage, strict fairness, leak-free predictions, tests"
git push -u origin v12-canonical-storage
```

Note: `rounds.json` etc. remain in the history of `main`. If the repository stays public and that
data is sensitive, rewrite history (e.g. `git filter-repo`) or make the repository private.
