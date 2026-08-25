import type { ReactNode } from "react";

const linkPattern = /\[([^\]]+)\]\(([^)]+)\)/g;

function safeHref(value: string): string | null {
  const trimmed = value.trim();
  if ((trimmed.startsWith("/") && !trimmed.startsWith("//")) || trimmed.startsWith("#")) return trimmed;
  try {
    const url = new URL(trimmed);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? trimmed : null;
  } catch {
    return null;
  }
}

function inline(value: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let offset = 0;
  for (const match of value.matchAll(linkPattern)) {
    const index = match.index ?? 0;
    if (index > offset) nodes.push(value.slice(offset, index));
    const href = safeHref(match[2]);
    nodes.push(href ? <a key={`${index}-${href}`} href={href} rel={href.startsWith("http") ? "noreferrer" : undefined}>{match[1]}</a> : <span key={`${index}-rejected`}>{match[1]}</span>);
    offset = index + match[0].length;
  }
  if (offset < value.length) nodes.push(value.slice(offset));
  return nodes;
}

export function SafeMarkdown({ source, connections }: { source: string; connections?: ReactNode }) {
  const blocks: ReactNode[] = [];
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  let replacedConnections = false;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) {
      const content = inline(heading[2]);
      const level = heading[1].length;
      if (!replacedConnections && level === 2 && heading[2].trim().toLocaleLowerCase() === "connections" && connections !== undefined) {
        const start = index;
        replacedConnections = true;
        index += 1;
        while (index < lines.length) {
          const nextHeading = /^(#{1,4})\s+(.+)$/.exec(lines[index]);
          if (nextHeading && nextHeading[1].length <= level) break;
          index += 1;
        }
        blocks.push(<section className="record-connections" key={`connections-${start}`}><h3>{content}</h3>{connections}</section>);
        continue;
      }
      blocks.push(level === 1 ? <h2 key={index}>{content}</h2> : level === 2 ? <h3 key={index}>{content}</h3> : <h4 key={index}>{content}</h4>);
      index += 1;
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      const items: ReactNode[] = [];
      while (index < lines.length && /^[-*]\s+/.test(lines[index])) {
        items.push(<li key={index}>{inline(lines[index].replace(/^[-*]\s+/, ""))}</li>);
        index += 1;
      }
      blocks.push(<ul key={`list-${index}`}>{items}</ul>);
      continue;
    }
    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim() && !/^(#{1,4})\s+/.test(lines[index]) && !/^[-*]\s+/.test(lines[index])) {
      paragraph.push(lines[index]); index += 1;
    }
    blocks.push(<p key={`p-${index}`}>{inline(paragraph.join(" "))}</p>);
  }
  return <div className="markdown">{blocks}</div>;
}
