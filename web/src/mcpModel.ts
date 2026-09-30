export interface AllowedMcpTool { name: string }
export interface McpTool {
  name: string;
  description?: string;
  annotations?: { readOnlyHint?: boolean };
  inputSchema?: unknown;
}

export function updateMcpTool(selected: AllowedMcpTool[], name: string, enabled: boolean): AllowedMcpTool[] {
  if (!enabled) return selected.filter(t => t.name !== name);
  return selected.some(t => t.name === name)
    ? selected
    : [...selected, { name }];
}
