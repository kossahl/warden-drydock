# Handout Safety

Handouts are player-facing transformations of reviewed campaign information.
They are not a place to summarize the Warden's full source records.

1. Identify the explicit audience and the facts already available to them.
2. Read the relevant Warden records, but copy only reviewed, audience-approved
   information into the handout.
3. Preserve in-fiction uncertainty, attribution, and unreliable sources.
4. Do not include Warden truth, secrets, hidden objectives, unrevealed
   information, or links that expose Warden-only records.
5. Create the record deterministically with
   `python scripts/drydock.py new handout <id> --name "<name>"`.
6. Fill in the audience before review. Keep the record `draft` until the Warden
   approves exactly what players will receive.
7. Run `python scripts/drydock.py validate` before sharing it.

If safe transformation would require guessing what the players know, stop and
ask the Warden. Never resolve ambiguity by revealing more information.
