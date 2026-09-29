type ReferenceNode = {
  id: string;
  label?: string;
  packId?: string;
  properties?: Record<string, unknown>;
};

function boundedMatch(text: string, value: string): boolean {
  if (!text.includes(value)) return false;
  const escaped = value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[^0-9A-Za-z가-힣])${escaped}($|[^0-9A-Za-z가-힣])`, "s").test(text);
}

/** Map source IDs to displayed IDs within the queried pack, never another pack. */
export function resolveGraphReferences(
  nodes: ReferenceNode[],
  packId: string,
  referencedNodes: { id: string }[] | undefined,
  legacyText = "",
): string[] {
  const explicitIds = referencedNodes === undefined ? null : new Set(referencedNodes.map((node) => node.id));
  const result = new Set<string>();
  const prefix = `${packId}::`;
  for (const node of nodes) {
    const scope = String(node.properties?.pack_id || node.packId || "");
    if (scope && scope !== packId) continue;
    if (!scope && node.id.includes("::") && !node.id.startsWith(prefix)) continue;
    const sourceId = String(node.properties?.original_id ?? (node.id.startsWith(prefix) ? node.id.slice(prefix.length) : node.id));
    const hit = explicitIds
      ? explicitIds.has(sourceId) || explicitIds.has(node.id)
      : (Boolean(node.label && node.label.length >= 4 && boundedMatch(legacyText, node.label))
        || boundedMatch(legacyText, node.id)
        || (sourceId.length >= 6 && boundedMatch(legacyText, sourceId)));
    if (hit) result.add(node.id);
    if (result.size >= 300) break;
  }
  return [...result];
}
