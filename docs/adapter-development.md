# Adapter Development

Adapters supply RPG-system behavior without changing the system-agnostic core.
The Mothership adapter is the reference implementation, not a core assumption.

## Layout

An adapter under `warden_drydock/data/adapters/ADAPTER/` may contribute:

- `00-drydock/adapter.json`: declarative entity registry;
- `00-drydock/system-principles.md`: agent-facing system guidance;
- `templates/`: entity templates;
- `docs/`: generated campaign documentation.

The generator overlays these files on the generic project template.

## Entity registry

Each `entity_types` entry declares:

- `template`: repository-relative template path;
- `destination`: repository-relative output pattern containing `{id}`;
- `required_fields`: frontmatter fields enforced outside `templates/`.
- `nonempty_fields`: fields that must contain a value;
- `required_values`: exact field values for the entity type;
- `forbidden_headings`: headings rejected in campaign-owned records.

Top-level adapter validation declarations define allowed field values and
forbidden field combinations. `legacy_paths` provides non-destructive warnings
and canonical migration guidance. Adapter identity and version come from
`adapter.json` and propagate into generated manifests and ownership locks.

Paths must remain within the campaign. Entity IDs use lowercase letters,
numbers, and hyphens. Templates should have an empty `id`, an explicit
provisional `status`, and `ownership: shared`. Creation changes ownership to
`campaign` and refuses duplicate IDs or existing destinations.

## Adapter rules

- Do not reproduce proprietary rules text.
- Keep generic canon, relationship, session, and validation concepts in core.
- Put system terminology, design philosophy, entity shapes, and workflows in
  the adapter.
- Add an adapter only for a real supported system; avoid speculative common
  abstractions.
- Add generation, entity creation, semantic validation, and standalone smoke
  tests for every adapter.
