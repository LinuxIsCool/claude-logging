/** Shared lifecycle normalization for Pi-family agent harnesses. */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const emitter = join(dirname(fileURLToPath(import.meta.url)), "emit.py");

export interface PiFamilyOptions {
  runtime: string;
  captureSource: string;
}

type Context = {
  cwd: string;
  model?: { id?: string };
  sessionManager: { getSessionId(): string };
};

type Extension = { on(event: string, handler: (event: any, ctx: Context) => void): void };

function emit(options: PiFamilyOptions, event: string, ctx: Context, data: Record<string, unknown> = {}) {
  const payload = {
    session_id: ctx.sessionManager.getSessionId(), cwd: ctx.cwd,
    hook_event_name: event, data, model: ctx.model?.id,
  };
  spawnSync("python3", [emitter, options.runtime, options.captureSource, event], {
    input: JSON.stringify(payload), stdio: ["pipe", "ignore", "ignore"],
  });
}

function text(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.filter((block) => block && typeof block === "object" && "text" in block)
    .map((block) => String((block as { text?: unknown }).text || "")).join("\n");
}

function thinking(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content.filter((block) => block && typeof block === "object" && (block as { type?: string }).type === "thinking")
    .map((block) => String((block as { thinking?: unknown; text?: unknown }).thinking || (block as { text?: unknown }).text || ""))
    .filter(Boolean).join("\n");
}

export function installPiFamilyLogging(extension: Extension, options: PiFamilyOptions) {
  const send = (event: string, ctx: Context, data: Record<string, unknown> = {}) => emit(options, event, ctx, data);
  extension.on("session_start", (event, ctx) => send("SessionStart", ctx, { ...event, source: event.reason }));
  extension.on("session_info_changed", (event, ctx) => send("SessionInfo", ctx, { name: event.name }));
  extension.on("session_shutdown", (event, ctx) => send("SessionEnd", ctx, event));
  extension.on("before_agent_start", (event, ctx) => send("UserPromptSubmit", ctx, { prompt: event.prompt, images: event.images, input_source: "agent" }));
  extension.on("message_end", (event, ctx) => {
    if (event.message.role !== "assistant") return;
    const reasoning = thinking(event.message.content);
    const response = text(event.message.content);
    if (reasoning) send("Reasoning", ctx, { reasoning, message: event.message });
    if (response) send("AssistantResponse", ctx, { response, message: event.message });
  });
  extension.on("tool_execution_start", (event, ctx) => send("PreToolUse", ctx, { tool_name: event.toolName, tool_input: event.args, tool_use_id: event.toolCallId }));
  extension.on("tool_execution_end", (event, ctx) => send(event.isError ? "PostToolUseFailure" : "PostToolUse", ctx, { tool_name: event.toolName, tool_response: event.result, tool_use_id: event.toolCallId }));
  extension.on("session_before_compact", (event, ctx) => send("PreCompact", ctx, { reason: event.reason, will_retry: event.willRetry }));
  extension.on("session_compact", (event, ctx) => send("PostCompact", ctx, { reason: event.reason, compaction: event.compactionEntry }));
  extension.on("model_select", (event, ctx) => send("ModelChange", ctx, { model: event.model, source: event.source }));
  extension.on("thinking_level_select", (event, ctx) => send("ThinkingLevelChange", ctx, { level: event.level, previous_level: event.previousLevel }));
}
