export interface SearchResult {
  event_id: string;
  session_id: string;
  event_type: string;
  content: string;
  score: number;  // RRF score for ranking
  timestamp: string;
  source: string;
  cosine_similarity: number;  // Semantic similarity (0.0-1.0) for display
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  time_ms: number;
}

export interface Session {
  id: string;
  started_at: string;
  ended_at: string | null;
  cwd: string | null;
  summary: string | null;
  event_count: number;
  event_type_counts?: Record<string, number>;  // Counts by event type
}

export interface Stats {
  session_count: number;
  event_count: number;
  total_tokens: number;
  first_session: string | null;
  last_session: string | null;
}

/**
 * Image reference for images extracted from user prompts.
 * When users paste images into Claude Code, they're extracted
 * and stored as files, with these references in the event data.
 */
export interface ImageReference {
  type: "image";
  path: string;           // Relative path: "images/{session_id}/{filename}"
  media_type: string;     // MIME type: "image/jpeg", "image/png", etc.
  size: number;           // File size in bytes
  index: number;          // Position in original content blocks
  url?: string;           // For URL-based images (not extracted)
}

export interface Event {
  id: string;
  type: string;
  ts: string;
  session_id: string;
  agent_session_num: number;
  data: Record<string, unknown>;
  content: string;
  images?: ImageReference[];  // Images extracted from UserPromptSubmit events
}

/**
 * Get the URL to serve an image from the API.
 * Returns empty string if path is invalid.
 */
export function getImageUrl(sessionId: string, imagePath: string | undefined): string {
  if (!imagePath) {
    return '';
  }
  // imagePath is like "images/{session_id}/{filename}"
  // We need just the filename part
  const filename = imagePath.split("/").pop();
  if (!filename) {
    return '';
  }
  return `${API_BASE}/images/${sessionId}/${filename}`;
}

const API_BASE = "/api";

export async function search(
  query: string,
  options: {
    limit?: number;
    event_types?: string[];
    date_from?: string;
    date_to?: string;
    use_semantic?: boolean;
  } = {}
): Promise<SearchResponse> {
  const response = await fetch(`${API_BASE}/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      limit: options.limit || 20,
      event_types: options.event_types,
      date_from: options.date_from,
      date_to: options.date_to,
      use_semantic: options.use_semantic || false,
    }),
  });

  if (!response.ok) {
    throw new Error(`Search failed: ${response.statusText}`);
  }

  return response.json();
}

export async function getSessions(
  options: {
    limit?: number;
    offset?: number;
    date_from?: string;
    date_to?: string;
  } = {}
): Promise<Session[]> {
  const params = new URLSearchParams();
  if (options.limit) params.set("limit", String(options.limit));
  if (options.offset) params.set("offset", String(options.offset));
  if (options.date_from) params.set("date_from", options.date_from);
  if (options.date_to) params.set("date_to", options.date_to);

  const response = await fetch(`${API_BASE}/sessions?${params}`);

  if (!response.ok) {
    throw new Error(`Failed to fetch sessions: ${response.statusText}`);
  }

  return response.json();
}

export async function getSession(
  sessionId: string
): Promise<{ session: Session; events: Event[] }> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}`);

  if (!response.ok) {
    throw new Error(`Failed to fetch session: ${response.statusText}`);
  }

  return response.json();
}

export async function getStats(): Promise<Stats> {
  const response = await fetch(`${API_BASE}/stats`);

  if (!response.ok) {
    throw new Error(`Failed to fetch stats: ${response.statusText}`);
  }

  return response.json();
}

export async function syncData(): Promise<{ synced: number }> {
  const response = await fetch(`${API_BASE}/sync`);

  if (!response.ok) {
    throw new Error(`Sync failed: ${response.statusText}`);
  }

  return response.json();
}

export async function getRecentEvents(
  options: {
    limit?: number;
    event_types?: string[];
  } = {}
): Promise<SearchResponse> {
  const params = new URLSearchParams();
  if (options.limit) params.set("limit", String(options.limit));
  if (options.event_types?.length) params.set("event_types", options.event_types.join(","));

  const response = await fetch(`${API_BASE}/events/recent?${params}`);

  if (!response.ok) {
    throw new Error(`Failed to fetch recent events: ${response.statusText}`);
  }

  return response.json();
}

export interface SubagentTranscript {
  agent_id: string;
  session_id: string;
  prompt: string;
  response: string;
  message_count: number;
}

export async function getSubagentTranscript(
  sessionId: string,
  agentId: string
): Promise<SubagentTranscript> {
  const response = await fetch(`${API_BASE}/subagent-transcript/${sessionId}/${agentId}`);

  if (!response.ok) {
    throw new Error(`Failed to fetch subagent transcript: ${response.statusText}`);
  }

  return response.json();
}

export function streamEvents(onEvent: (event: Event) => void): () => void {
  const eventSource = new EventSource(`${API_BASE}/events/stream`);

  eventSource.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      onEvent(event);
    } catch (err) {
      console.error("Failed to parse event:", err);
    }
  };

  eventSource.onerror = (err) => {
    console.error("SSE error:", err);
  };

  return () => eventSource.close();
}
