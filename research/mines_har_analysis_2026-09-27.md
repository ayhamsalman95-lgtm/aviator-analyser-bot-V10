# 1xLite Gems & Mines HAR analysis — 2026-09-27

## Scope
Temporary analysis of the demo game at 1xlite-130003.top. Raw HAR was removed because it contained session/authentication data.

## Observed API
Game identifier: 508
Demo endpoint family:
- POST /games-frame/service-api/games-demo-gems-and-mines/MakeBet
- POST /games-frame/service-api/games-demo-gems-and-mines/MakeAction

## MakeBet
Observed request shape (redacted to gameplay fields):
`{"WH":55,"LG":"ar","GT":508,"BAC":26428222,"BS":1,"MC":2}`

Observed response shape:
`{"SB":1,"CF":0,"SW":0,"AB":98,"RS":{"GF":[[0,0,0,0,0],[0,0,0,0,0],[0,0,0,0,0],[0,0,0,0,0],[0,0,0,0,0]],"GL":23,"MC":8,"OG":0,"PWS":1.07},"AN":1,"BS":1,"AI":26428222}`

Important observation: GF is all zeros immediately after starting the round. The complete mine layout is therefore not exposed in the MakeBet response.

## MakeAction
Observed request shape:
`{"WH":55,"LG":"ar","GT":508,"AN":1,"SC":{"X":0,"Y":0}}`

The next actions use incremented AN and the selected cell coordinates in SC.X / SC.Y.

Observed safe responses show GF being updated with 1 at selected safe cells and 0 elsewhere. For example, after selecting (0,0), GF began with 1 at that position.

## Meaning of GF
A later completed/lost round returned a full 5x5 GF matrix containing only 1 and 2 values, while the action had selected a cell whose value became 2. In the same final response the configured game had two mines.

Working interpretation supported by the HAR:
- 0 = unrevealed cell
- 1 = gem/safe cell
- 2 = mine

This is strongest evidence available from the captured traffic and should be verified with another independent round.

## Example final board observed
The final response for a two-mine round contained:
`[[1,1,1,1,1],[1,2,1,1,1],[1,1,1,1,1],[1,1,1,2,1],[1,1,1,1,1]]`

Thus the two mine coordinates in that observed round were:
- X=1, Y=1
- X=3, Y=3

The client had selected (1,1) on the action that ended the round.

## Current conclusion
1. The round is created by MakeBet.
2. The server receives each selected coordinate through MakeAction.
3. Before a cell is selected, the network response does not expose the other hidden cells.
4. After a mine is hit, the response exposes the complete board.
5. The HAR contained no visible serverSeed/clientSeed/nonce fields in the gameplay requests/responses that were inspected.
6. The next investigation target is the game's fairness/settings interface or its initialization/configuration traffic, not the ordinary MakeAction response.

## Next forensic targets
Search a fresh demo HAR for:
serverSeed
clientSeed
playerSeed
nonce
SHA256
SHA512
fair
provably
verify
seed
commit
hash

Do not capture or publish Authorization headers, cookies, session IDs, fingerprints, or other authentication material.
