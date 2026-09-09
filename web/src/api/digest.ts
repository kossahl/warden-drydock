function jsonString(value: string): string {
  return JSON.stringify(value).replace(/[^\x20-\x7e]/g, (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
      .map(([key, item]) => `${jsonString(key)}:${canonicalJson(item)}`)
      .join(",")}}`;
  }
  return typeof value === "string" ? jsonString(value) : JSON.stringify(value) ?? "null";
}

export async function sha256(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
    .map((part) => part.toString(16).padStart(2, "0"))
    .join("");
}

export function digest(value: unknown): Promise<string> {
  return sha256(canonicalJson(value));
}
