import adapter from "../../../warden_drydock/data/adapters/mothership/00-drydock/adapter.json";
import type { EditorRecord } from "./editorClient";

// The personal pilot supports the shipped Mothership adapter. Vite reads the
// authoritative templates at build time; no copied vocabulary or API defaults.
const templates = import.meta.glob<string>("../../../warden_drydock/data/adapters/mothership/templates/*.md", { query: "?raw", import: "default", eager: true });
const projectTemplates = import.meta.glob<string>("../../../warden_drydock/data/project_template/**/*.md", { query: "?raw", import: "default", eager: true });
const basics = new Set(["id", "type", "name", "status", "visibility", "warden_only"]);
const headingId = (heading: string) => heading.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-|-$/g, "");
function definition(source: string) {
  const metadata: Record<string, string> = {};
  const frontmatter = /^---\n([\s\S]*?)\n---/.exec(source)?.[1] ?? "";
  for (const line of frontmatter.split("\n")) {
    const match = /^([a-z_]+):\s*(.*)$/.exec(line);
    if (match) metadata[match[1]] = match[2].replace(/^"|"$/g, "");
  }
  return { metadata, fields: Object.keys(metadata).filter((key) => !basics.has(key)),
    sections: Array.from(source.matchAll(/^## (.+)$/gm), (match) => ({ id: headingId(match[1]), label: match[1] })).filter((section) => section.id !== "connections"),
    requiredFields: [] as string[], nonemptyFields: [] as string[], requiredValues: {} as Record<string, string>, forbiddenHeadings: [] as string[] };
}
export const recordTypes = Object.keys(adapter.entity_types);
export const relationships = Object.keys(adapter.connections.relationships);
export const connectionStates = adapter.connections.states;
type RecordDefinition = { metadata: Record<string, string>; fields: string[]; sections: { id: string; label: string }[]; requiredFields: string[]; nonemptyFields: string[]; requiredValues: Record<string, string>; forbiddenHeadings: string[] };
export const recordDefinitions = Object.fromEntries([
  ...Object.values(projectTemplates).map(definition).filter((item) => item.metadata.type),
  ...Object.entries(adapter.entity_types).map(([type, spec]) => {
    const source = Object.entries(templates).find(([path]) => path.endsWith(`/${spec.template}`))?.[1];
    if (!source) throw new Error(`Missing adapter template for ${type}`);
    const rules = spec as { required_fields?: string[]; nonempty_fields?: string[]; required_values?: Record<string, string>; forbidden_headings?: string[] };
    return { ...definition(source), metadata: { ...definition(source).metadata, type }, requiredFields: rules.required_fields ?? [], nonemptyFields: rules.nonempty_fields ?? [], requiredValues: rules.required_values ?? {}, forbiddenHeadings: rules.forbidden_headings ?? [] };
  }),
].map((item) => [item.metadata.type, item])) as Record<string, RecordDefinition>;
export function newAdapterRecord(type = "npc", recordId = "new-record", name = "New record"): EditorRecord {
  const spec = recordDefinitions[type];
  const players = spec.metadata.visibility === "players";
  return { record_id: recordId, record_type: type, displayed_name: name,
    status: spec.metadata.status, authority: "preparation",
    visibility: players ? { audience: "players", warden_only: false } : { audience: "warden", warden_only: true },
    fields: spec.fields.map((field_id) => ({ field_id, value: field_id === "ownership" ? "campaign" : spec.metadata[field_id] })),
    sections: spec.sections.map((section) => ({ section_id: section.id, body: "" })),
    connections: [], content_digest: "0".repeat(64) };
}
