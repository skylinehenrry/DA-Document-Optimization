/*
Readable labels and grouped connections, without changing the saved graph.

- Treat both POSIX and Windows separators as path separators for display only.
- Keep canonical identifiers, evidence and every raw relationship unchanged.
- Group imports/calls/dependencies between the same code nodes into one arrow.
- Preserve direction and show amber dashes if any grouped member is unconfirmed.
- Never remove an explicit connection merely because an indirect route exists.
*/

export function basename(path) {
  let text = String(path ?? "");
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) text = text.split(/[?#]/, 1)[0];
  text = text.replace(/^[a-z]:(?![\\/])/i, "");
  return text.replaceAll("\\", "/").split("/").filter(Boolean).at(-1) ?? "";
}

export function nodeName(node) {
  const sourceFile = node.kind === "script" || (node.kind === "module" && node.source_path);
  if (sourceFile) return basename(node.source_path ?? node.label) || node.label;
  if (node.kind === "file") return basename(node.details?.normalized_path ?? node.label) || node.label;
  return node.label;
}

export function connectionGroups(graph) {
  const nodes = new Map(graph.nodes.map(node => [node.id, node]));
  const groups = new Map();
  for (const edge of graph.edges) {
    const codeNodes = [edge.source, edge.target].every(id => ["script", "module"].includes(nodes.get(id)?.kind));
    const family = codeNodes && ["imports", "calls", "depends_on"].includes(edge.kind) ? "code" : edge.kind;
    const key = JSON.stringify([edge.source, edge.target, family]);
    if (!groups.has(key)) groups.set(key, {...edge, member_ids: [], kinds: [], status: "confirmed"});
    const group = groups.get(key);
    group.member_ids.push(edge.id);
    if (!group.kinds.includes(edge.kind)) group.kinds.push(edge.kind);
    if (edge.status === "proposed") group.status = "proposed";
  }
  for (const group of groups.values()) {
    group.display_label = group.kinds.map(kind => kind.replaceAll("_", " ")).join(" · ");
    if (group.member_ids.length > 1) group.display_label += ` · ${group.member_ids.length} references`;
  }
  return [...groups.values()];
}
