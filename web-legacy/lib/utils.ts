import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(date: string | Date): string {
  const d = new Date(date);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export function formatDateTime(date: string | Date): string {
  const d = new Date(date);
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function formatDuration(ms: number): string {
  if (ms < 1) return "<1ms";
  if (ms < 1000) return `${ms.toFixed(1)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

export function truncate(str: string, length: number): string {
  if (str.length <= length) return str;
  return str.slice(0, length) + "...";
}

export function getEventTypeColor(type: string): string {
  const colors: Record<string, string> = {
    // Lifecycle events
    SessionStart: "event-badge-session",
    Setup: "event-badge-session",
    SessionEnd: "event-badge-session",
    // User/Claude events
    UserPromptSubmit: "event-badge-prompt",
    AssistantResponse: "event-badge-response",
    // Tool events
    PreToolUse: "event-badge-tool",
    PostToolUse: "event-badge-tool",
    PostToolUseFailure: "event-badge-error",
    // Agent events
    SubagentStart: "event-badge-response",
    SubagentStop: "event-badge-response",
    Stop: "event-badge-response",
    // System events
    PreCompact: "event-badge-system",
    PermissionRequest: "event-badge-system",
    Notification: "event-badge-system",
  };
  return colors[type] || "event-badge-system";
}

export function getEventTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    // Lifecycle
    SessionStart: "Start",
    Setup: "Setup",
    SessionEnd: "End",
    // User/Claude
    UserPromptSubmit: "Prompt",
    AssistantResponse: "Claude",
    // Tools
    PreToolUse: "Pre-Tool",
    PostToolUse: "Post-Tool",
    PostToolUseFailure: "Tool Error",
    // Agents
    SubagentStart: "Agent Start",
    SubagentStop: "Agent Stop",
    Stop: "Stop",
    // System
    PreCompact: "Compact",
    PermissionRequest: "Permission",
    Notification: "Notify",
  };
  return labels[type] || type;
}

export function highlightMatches(text: string, query: string): string {
  if (!query.trim()) return text;

  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  let result = text;

  for (const word of words) {
    const regex = new RegExp(`(${escapeRegex(word)})`, "gi");
    result = result.replace(regex, '<mark class="search-highlight">$1</mark>');
  }

  return result;
}

function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Format event content for human-readable display.
 * Handles both new human-readable content and old JSON-formatted content.
 */
export function formatEventContent(content: string, eventType: string): string {
  if (!content) return "";

  // Check if content looks like it's already human-readable
  if (!content.startsWith("{") && !content.includes('{"')) {
    // Already human-readable, but check for tool-style prefix
    const toolPrefixes = ["Bash ", "Read ", "Write ", "Edit ", "Glob ", "Grep ", "Task "];
    for (const prefix of toolPrefixes) {
      if (content.startsWith(prefix)) {
        // Try to parse the JSON part after the tool name
        const jsonPart = content.slice(prefix.length);
        try {
          const parsed = JSON.parse(jsonPart);
          return formatToolContent(prefix.trim(), parsed, eventType);
        } catch {
          // Not JSON, return as-is
          return content;
        }
      }
    }
    return content;
  }

  // Try to parse as JSON
  try {
    const parsed = JSON.parse(content);
    if (typeof parsed === "object" && parsed !== null) {
      // Format based on structure
      if ("command" in parsed) {
        return `Running: ${parsed.command}${parsed.description ? ` (${parsed.description})` : ""}`;
      }
      if ("file_path" in parsed) {
        return `File: ${parsed.file_path}`;
      }
      if ("pattern" in parsed) {
        return `Pattern: ${parsed.pattern}`;
      }
      if ("stdout" in parsed || "stderr" in parsed) {
        const stdout = parsed.stdout || "";
        const stderr = parsed.stderr || "";
        if (stdout) return `Output: ${truncate(stdout, 200)}`;
        if (stderr) return `Error: ${truncate(stderr, 200)}`;
        return "Completed (no output)";
      }
    }
  } catch {
    // Not JSON, continue
  }

  return content;
}

function formatToolContent(toolName: string, data: Record<string, unknown>, eventType: string): string {
  switch (toolName) {
    case "Bash":
      if (eventType.includes("Pre")) {
        const cmd = String(data.command || "");
        const desc = data.description ? ` (${data.description})` : "";
        return `Running: ${truncate(cmd, 100)}${desc}`;
      } else {
        const stdout = String(data.stdout || "");
        if (stdout) {
          const lines = stdout.trim().split("\\n");
          if (lines.length > 3) {
            return `Output (${lines.length} lines): ${truncate(lines[0], 100)}...`;
          }
          return `Output: ${truncate(stdout, 200)}`;
        }
        return "Command completed";
      }
    case "Read":
      return `Reading: ${data.file_path || data.filePath || "file"}`;
    case "Write":
      return `Writing: ${data.file_path || data.filePath || "file"}`;
    case "Edit":
      return `Editing: ${data.file_path || data.filePath || "file"}`;
    case "Glob":
      if (eventType.includes("Post") && data.numFiles !== undefined) {
        return `Found ${data.numFiles} files`;
      }
      return `Finding: ${data.pattern || "files"}`;
    case "Grep":
      return `Searching: ${data.pattern || "pattern"}`;
    case "Task":
      return `Agent: ${truncate(String(data.description || data.prompt || "task"), 100)}`;
    default:
      return `${toolName}: ${JSON.stringify(data).slice(0, 100)}`;
  }
}
