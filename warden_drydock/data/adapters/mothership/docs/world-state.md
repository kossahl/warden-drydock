# World-State Maintenance

Use existing entity records as the source of current fictional state. Create
new `consequence`, `clock`, `faction-turn`, or entity records through
`python scripts/drydock.py new`; edit campaign-owned records when established
entities change. Never rewrite adapter templates as campaign state.

Summarize proposed changes and conflicts for Warden review. Preserve history
in session/debrief records rather than silently replacing facts. Validate after
changes and rebuild context after approved canon or current-state updates.
