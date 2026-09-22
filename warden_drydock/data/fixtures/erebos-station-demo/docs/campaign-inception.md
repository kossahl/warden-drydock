# Campaign Inception

After bootstrap succeeds, ask the Warden about tone, boundaries, starting
situation, characters, important organizations, and immediately relevant
places. Do not invent canon to fill unanswered questions.

Create only the records needed for the agreed starting situation with
`python scripts/drydock.py new <type> <id> --name "<name>"`. Typical records
are `character`, `system`, `location`, `faction`, `npc`, `ship`, and
`adventure`. Keep them `idea` or `draft`. Present assumptions and open
questions for Warden review before promoting anything to canon. Then run
`python scripts/drydock.py validate` and `python scripts/drydock.py context`.
