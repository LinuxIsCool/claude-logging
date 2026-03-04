"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import {
  MessageSquare,
  Clock,
  FolderOpen,
  ChevronRight,
  ChevronDown,
  Loader2,
  Search,
  Filter,
  Wrench,
  Bot,
  ImageIcon,
  X,
  Download,
  ExternalLink,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  getSessions,
  getSession,
  getSubagentTranscript,
  search as searchApi,
  getImageUrl,
  type Session,
  type Event,
  type SearchResult,
  type SubagentTranscript,
  type ImageReference,
} from "@/lib/api";
import {
  useScoringSettings,
  transformScore,
  scoreToBackground,
  formatScorePercent,
  calculateSessionScore,
} from "@/lib/scoring-settings";
import { ScoringSettingsDropdown } from "@/components/scoring-settings-dropdown";
import {
  formatDateTime,
  getEventTypeLabel,
  truncate,
  cn,
} from "@/lib/utils";
import { SearchProvider, useMarkdownHighlighting } from "@/lib/search-context";
import { HighlightedText } from "@/components/highlighted-text";

// All filterable event types shown in the filter bar
// Matches official Claude Code hook events + AssistantResponse (for logging Claude's text)
const EVENT_TYPES = [
  "SessionStart",
  "Setup",
  "UserPromptSubmit",
  "SubagentStart",
  "PreToolUse",
  "PermissionRequest",
  "PostToolUse",
  "PostToolUseFailure",
  "Notification",
  "Stop",
  "SubagentStop",
  "PreCompact",
  "SessionEnd",
  "AssistantResponse",
];

// Default filter - the most useful event types for reviewing sessions
const DEFAULT_FILTER_TYPES = [
  "SessionStart",
  "UserPromptSubmit",
  "SubagentStart",
  "PreToolUse",
  "PostToolUse",
  "SubagentStop",
  "PreCompact",
  "SessionEnd",
  "AssistantResponse",
];

// Emojis for event types (used in filter buttons and session cards)
const EVENT_TYPE_EMOJIS: Record<string, string> = {
  SessionStart: "💫",
  Setup: "🔧",
  UserPromptSubmit: "🍄",
  SubagentStart: "🚀",
  PreToolUse: "🔨",
  PermissionRequest: "🔐",
  PostToolUse: "🏰",
  PostToolUseFailure: "❌",
  Notification: "🟡",
  Stop: "🟢",
  SubagentStop: "🔵",
  PreCompact: "♻️",
  SessionEnd: "⭐",
  AssistantResponse: "🌲",
};

export function SessionList() {
  // Search state
  const [query, setQuery] = useState("");
  const [selectedTypes, setSelectedTypes] = useState<string[]>(DEFAULT_FILTER_TYPES);
  const [isSearching, setIsSearching] = useState(false);
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [matchingSessionIds, setMatchingSessionIds] = useState<Set<string>>(new Set());

  // Scoring state - maps event_id to cosine_similarity for highlighting
  const [eventScores, setEventScores] = useState<Map<string, number>>(new Map());
  const { settings } = useScoringSettings();

  // Session state
  const [sessions, setSessions] = useState<Session[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [selectedSession, setSelectedSession] = useState<string | null>(null);
  const [sessionEvents, setSessionEvents] = useState<Event[]>([]);
  const [isLoadingEvents, setIsLoadingEvents] = useState(false);

  const inputRef = useRef<HTMLInputElement>(null);

  // Load all sessions and auto-select the most recent one
  useEffect(() => {
    getSessions({ limit: 100 })
      .then((loadedSessions) => {
        setSessions(loadedSessions);
        // Auto-select the most recent session (first in the list)
        if (loadedSessions.length > 0) {
          const firstSession = loadedSessions[0];
          setSelectedSession(firstSession.id);
          // Load the session's events
          getSession(firstSession.id)
            .then((data) => setSessionEvents(data.events))
            .catch(console.error);
        }
      })
      .catch(console.error)
      .finally(() => setIsLoading(false));
  }, []);

  // Perform search
  const handleSearch = useCallback(async () => {
    if (!query.trim()) {
      setSearchResults([]);
      setMatchingSessionIds(new Set());
      setEventScores(new Map());
      return;
    }

    setIsSearching(true);
    try {
      const response = await searchApi(query, {
        limit: 100,
        event_types: selectedTypes.length > 0 ? selectedTypes : undefined,
        use_semantic: true, // Always use semantic search
      });

      setSearchResults(response.results);
      // Extract unique session IDs from results
      const sessionIds = new Set(response.results.map((r) => r.session_id));
      setMatchingSessionIds(sessionIds);

      // Build event score map from cosine_similarity
      const scoreMap = new Map<string, number>();
      for (const result of response.results) {
        // Use cosine_similarity if available, otherwise 0
        scoreMap.set(result.event_id, result.cosine_similarity || 0);
      }
      setEventScores(scoreMap);
    } catch (err) {
      console.error("Search failed:", err);
    } finally {
      setIsSearching(false);
    }
  }, [query, selectedTypes]);

  // Search on Enter
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      handleSearch();
    }
  };

  // Clear search when query is empty
  useEffect(() => {
    if (!query.trim()) {
      setSearchResults([]);
      setMatchingSessionIds(new Set());
      setEventScores(new Map());
    }
  }, [query]);

  // Calculate session scores based on event scores and settings
  const sessionScores = React.useMemo(() => {
    const scores = new Map<string, number>();
    const sessionEventScores = new Map<string, number[]>();

    // Group scores by session
    for (const result of searchResults) {
      const score = eventScores.get(result.event_id) || 0;
      const existing = sessionEventScores.get(result.session_id) || [];
      existing.push(score);
      sessionEventScores.set(result.session_id, existing);
    }

    // Calculate aggregate score per session
    for (const [sessionId, eventScoreList] of sessionEventScores) {
      scores.set(sessionId, calculateSessionScore(eventScoreList, settings.sessionAggregate));
    }

    return scores;
  }, [searchResults, eventScores, settings.sessionAggregate]);

  // Get all cosine scores for ordinal mode transformation
  const allCosineScores = React.useMemo(() => {
    return Array.from(eventScores.values()).filter((s) => s > 0);
  }, [eventScores]);

  // Toggle event type filter
  const toggleEventType = (type: string) => {
    setSelectedTypes((prev) =>
      prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type]
    );
  };

  // Select a session
  const handleSelectSession = async (sessionId: string) => {
    if (selectedSession === sessionId) {
      setSelectedSession(null);
      setSessionEvents([]);
      return;
    }

    setSelectedSession(sessionId);
    setIsLoadingEvents(true);

    try {
      const data = await getSession(sessionId);
      setSessionEvents(data.events);
    } catch (err) {
      console.error("Failed to load session:", err);
      setSessionEvents([]);
    } finally {
      setIsLoadingEvents(false);
    }
  };

  // Filter sessions based on search and type filters
  const filteredSessions = sessions.filter((session) => {
    // If searching, only show sessions with matches
    if (query.trim() && matchingSessionIds.size > 0) {
      return matchingSessionIds.has(session.id);
    }
    return true;
  });

  // Get matching event IDs for the selected session
  const matchingEventIds = new Set(
    searchResults
      .filter((r) => r.session_id === selectedSession)
      .map((r) => r.event_id)
  );

  // Calculate event type counts for filter badges
  // If searching: count from search results
  // Otherwise: aggregate from all sessions
  const filterBadgeCounts: Record<string, number> = {};
  if (searchResults.length > 0) {
    // Count from search results
    for (const result of searchResults) {
      filterBadgeCounts[result.event_type] = (filterBadgeCounts[result.event_type] || 0) + 1;
    }
  } else {
    // Aggregate from all sessions' event_type_counts
    for (const session of sessions) {
      if (session.event_type_counts) {
        for (const [type, count] of Object.entries(session.event_type_counts)) {
          filterBadgeCounts[type] = (filterBadgeCounts[type] || 0) + count;
        }
      }
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="w-8 h-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full gap-4">
      {/* Search Bar */}
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Search conversations..."
            className={cn("pl-10", query && "pr-10")}
          />
          {/* Clear search button */}
          {query && (
            <button
              onClick={() => {
                setQuery("");
                setSearchResults([]);
                setMatchingSessionIds(new Set());
                setEventScores(new Map());
                inputRef.current?.focus();
              }}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
              title="Clear search"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
        <Button onClick={handleSearch} disabled={isSearching || !query.trim()}>
          {isSearching ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            "Search"
          )}
        </Button>
        <ScoringSettingsDropdown />
      </div>

      {/* Event Type Filters */}
      <div className="flex items-center gap-2 flex-wrap">
        <Filter className="w-4 h-4 text-muted-foreground" />
        {EVENT_TYPES.map((type) => {
          const count = filterBadgeCounts[type] || 0;
          return (
            <Badge
              key={type}
              variant={selectedTypes.includes(type) ? "default" : "outline"}
              className="cursor-pointer"
              onClick={() => toggleEventType(type)}
            >
              {EVENT_TYPE_EMOJIS[type]} {getEventTypeLabel(type)}
              {count > 0 && (
                <span className="ml-1 text-[10px] opacity-70">({count})</span>
              )}
            </Badge>
          );
        })}
        {selectedTypes.length > 0 && (
          <button
            onClick={() => setSelectedTypes([])}
            className="text-xs text-muted-foreground hover:text-foreground underline ml-2"
          >
            Clear filters
          </button>
        )}
      </div>

      {/* Search Results Summary */}
      {query.trim() && searchResults.length > 0 && (
        <div className="text-sm text-muted-foreground">
          Found {searchResults.length} matches in {matchingSessionIds.size} session
          {matchingSessionIds.size !== 1 ? "s" : ""}
        </div>
      )}

      {/* Main Content: Sessions List + Events Timeline */}
      <div className="flex gap-6 flex-1 min-h-0">
        {/* Session List */}
        <div className="w-1/3 min-w-[280px] flex flex-col">
          <h2 className="text-lg font-semibold mb-3">
            Sessions ({filteredSessions.length})
          </h2>
          <ScrollArea className="flex-1">
            <div className="space-y-2 pr-4">
              {filteredSessions.map((session) => {
                const rawScore = sessionScores.get(session.id) || 0;
                const transformedScore = transformScore(rawScore, settings.scaleMode, allCosineScores);
                return (
                  <SessionCard
                    key={session.id}
                    session={session}
                    isSelected={selectedSession === session.id}
                    onClick={() => handleSelectSession(session.id)}
                    matchCount={
                      searchResults.filter((r) => r.session_id === session.id).length
                    }
                    hasMatches={matchingSessionIds.has(session.id)}
                    selectedTypes={selectedTypes}
                    similarityScore={rawScore}
                    transformedScore={transformedScore}
                  />
                );
              })}
              {filteredSessions.length === 0 && query.trim() && (
                <div className="text-center py-8 text-muted-foreground">
                  No sessions match your search
                </div>
              )}
            </div>
          </ScrollArea>
        </div>

        {/* Events Timeline */}
        <div className="flex-1 min-w-0">
          {selectedSession ? (
            <SearchProvider query={query}>
              <EventsTimeline
                events={sessionEvents}
                sessionId={selectedSession}
                isLoading={isLoadingEvents}
                matchingEventIds={matchingEventIds}
                selectedTypes={selectedTypes}
                eventScores={eventScores}
                scaleMode={settings.scaleMode}
                allCosineScores={allCosineScores}
              />
            </SearchProvider>
          ) : (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              <div className="text-center">
                <MessageSquare className="w-12 h-12 mx-auto mb-4 opacity-50" />
                <p>Select a session to view its transcript</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Display order for event types in sidebar (chronological flow)
const SIDEBAR_EVENT_ORDER = [
  "SessionStart",
  "Setup",
  "UserPromptSubmit",
  "SubagentStart",
  "PreToolUse",
  "PermissionRequest",
  "PostToolUse",
  "PostToolUseFailure",
  "Notification",
  "Stop",
  "SubagentStop",
  "PreCompact",
  "SessionEnd",
  "AssistantResponse",
];

function EventTypeCounts({
  counts,
  selectedTypes,
}: {
  counts: Record<string, number>;
  selectedTypes: string[];
}) {
  // Only show types that match the current filter (or all if no filter)
  const displayCounts = SIDEBAR_EVENT_ORDER
    .filter(type => {
      // Must have events of this type
      if (!counts[type] || counts[type] <= 0) return false;
      // If filters are active, only show filtered types
      if (selectedTypes.length > 0 && !selectedTypes.includes(type)) return false;
      return true;
    })
    .map(type => ({
      type,
      count: counts[type],
      emoji: EVENT_TYPE_EMOJIS[type] || "•",
    }));

  if (displayCounts.length === 0) {
    // Fallback: show total from all types
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    return <span className="text-xs text-muted-foreground">{total}</span>;
  }

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      {displayCounts.map(({ type, count, emoji }) => (
        <span
          key={type}
          className="text-xs text-muted-foreground flex items-center gap-0.5"
          title={type}
        >
          <span>{emoji}</span>
          <span>{count}</span>
        </span>
      ))}
    </div>
  );
}

function SessionCard({
  session,
  isSelected,
  onClick,
  matchCount,
  hasMatches,
  selectedTypes,
  similarityScore,
  transformedScore,
}: {
  session: Session;
  isSelected: boolean;
  onClick: () => void;
  matchCount: number;
  hasMatches: boolean;
  selectedTypes: string[];
  similarityScore?: number;  // Raw cosine similarity (0-1)
  transformedScore?: number; // Transformed for display (0-1)
}) {
  // Calculate background color from transformed score
  const backgroundColor = transformedScore && transformedScore > 0
    ? scoreToBackground(transformedScore)
    : undefined;

  return (
    <Card
      className={cn(
        "cursor-pointer transition-colors",
        isSelected ? "border-primary bg-accent" : "hover:bg-accent/50",
        // Only show yellow border if no similarity score (keyword-only match)
        hasMatches && !isSelected && !similarityScore && "border-yellow-500/50",
        // Green border for semantic matches
        hasMatches && !isSelected && similarityScore && similarityScore > 0 && "border-green-500/50"
      )}
      onClick={onClick}
      style={{ backgroundColor: isSelected ? undefined : backgroundColor }}
    >
      <CardContent className="p-3">
        <div className="flex items-center justify-between">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <Clock className="w-3 h-3 text-muted-foreground" />
              <span className="text-sm">{formatDateTime(session.started_at)}</span>
              {matchCount > 0 && (
                <Badge variant="secondary" className="text-xs">
                  {matchCount} match{matchCount !== 1 ? "es" : ""}
                </Badge>
              )}
              {/* Similarity score badge */}
              {similarityScore !== undefined && similarityScore > 0 && (
                <Badge
                  variant="outline"
                  className="text-green-400 border-green-500/40 text-xs"
                >
                  {formatScorePercent(similarityScore)}
                </Badge>
              )}
            </div>
            {session.cwd && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground truncate">
                <FolderOpen className="w-3 h-3 flex-shrink-0" />
                <span className="truncate">{session.cwd.split("/").slice(-2).join("/")}</span>
              </div>
            )}
            {session.summary && (
              <p className="text-sm text-muted-foreground mt-1 line-clamp-2">
                {session.summary}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2 ml-2">
            {session.event_type_counts ? (
              <EventTypeCounts counts={session.event_type_counts} selectedTypes={selectedTypes} />
            ) : (
              <Badge variant="outline" className="text-xs">
                {session.event_count}
              </Badge>
            )}
            <ChevronRight
              className={cn(
                "w-4 h-4 transition-transform",
                isSelected && "rotate-90"
              )}
            />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// Helper to group consecutive tool events
interface GroupedEvent {
  type: "single" | "tool-group" | "ask-user-question";
  event?: Event;
  events?: Event[];
  startTime?: string;
  responseEvent?: Event; // For AskUserQuestion - the PostToolUse response
}

function groupEvents(events: Event[]): GroupedEvent[] {
  const grouped: GroupedEvent[] = [];
  let currentToolGroup: Event[] = [];

  // Helper to check if an event is AskUserQuestion
  const isAskUserQuestion = (e: Event) => {
    if (e.type !== "PreToolUse") return false;
    const data = e.data as Record<string, unknown>;
    return data.tool_name === "AskUserQuestion";
  };

  // Find matching PostToolUse for AskUserQuestion
  const findResponseEvent = (preEvent: Event, startIdx: number): Event | undefined => {
    const preData = preEvent.data as Record<string, unknown>;
    const toolUseId = preData.tool_use_id as string;

    for (let i = startIdx; i < events.length; i++) {
      const e = events[i];
      if (e.type === "PostToolUse") {
        const postData = e.data as Record<string, unknown>;
        if (postData.tool_use_id === toolUseId || postData.tool_name === "AskUserQuestion") {
          return e;
        }
      }
    }
    return undefined;
  };

  const flushToolGroup = () => {
    if (currentToolGroup.length > 0) {
      // Don't create empty tool groups (PostToolUse-only groups)
      // This happens when Task subagents complete - their PostToolUse comes after
      // SubagentStop, separated from their PreToolUse
      const hasPreToolUse = currentToolGroup.some(e => e.type === "PreToolUse");
      if (hasPreToolUse) {
        grouped.push({
          type: "tool-group",
          events: [...currentToolGroup],
          startTime: currentToolGroup[0].ts,
        });
      }
      // Note: Orphan PostToolUse events are intentionally dropped from display
      // They provide no useful info without their PreToolUse context
      currentToolGroup = [];
    }
  };

  for (let i = 0; i < events.length; i++) {
    const event = events[i];

    // Handle AskUserQuestion specially
    if (isAskUserQuestion(event)) {
      flushToolGroup();
      const responseEvent = findResponseEvent(event, i + 1);
      grouped.push({
        type: "ask-user-question",
        event,
        responseEvent,
      });
      continue;
    }

    // Skip PostToolUse for AskUserQuestion (handled with PreToolUse)
    if (event.type === "PostToolUse") {
      const data = event.data as Record<string, unknown>;
      if (data.tool_name === "AskUserQuestion") {
        continue;
      }
    }

    if (event.type === "PreToolUse" || event.type === "PostToolUse") {
      currentToolGroup.push(event);
    } else {
      flushToolGroup();
      grouped.push({ type: "single", event });
    }
  }
  flushToolGroup();

  return grouped;
}

function EventsTimeline({
  events,
  sessionId,
  isLoading,
  matchingEventIds,
  selectedTypes,
  eventScores,
  scaleMode,
  allCosineScores,
}: {
  events: Event[];
  sessionId: string;
  isLoading: boolean;
  matchingEventIds: Set<string>;
  selectedTypes: string[];
  eventScores: Map<string, number>;
  scaleMode: "linear" | "logarithmic" | "ordinal";
  allCosineScores: number[];
}) {
  // Filter events by type if filters are active
  const filteredEvents = selectedTypes.length > 0
    ? events.filter((e) => selectedTypes.includes(e.type))
    : events;

  // Group tool events together
  const groupedEvents = groupEvents(filteredEvents);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (filteredEvents.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        {selectedTypes.length > 0
          ? "No events match the selected filters"
          : "No events in this session"}
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      <h2 className="text-lg font-semibold mb-3">
        Transcript ({filteredEvents.length} events)
      </h2>
      <ScrollArea className="flex-1">
        <div className="space-y-1 pr-4">
          {groupedEvents.map((group, idx) => {
            if (group.type === "tool-group" && group.events) {
              const hasMatch = group.events.some((e) => matchingEventIds.has(e.id));
              return (
                <ToolGroupCard
                  key={`tool-group-${idx}`}
                  events={group.events}
                  hasMatch={hasMatch}
                />
              );
            } else if (group.type === "ask-user-question" && group.event) {
              const isMatch = matchingEventIds.has(group.event.id);
              return (
                <AskUserQuestionCard
                  key={group.event.id}
                  event={group.event}
                  responseEvent={group.responseEvent}
                  isMatch={isMatch}
                />
              );
            } else if (group.event) {
              const isMatch = matchingEventIds.has(group.event.id);
              const rawScore = eventScores.get(group.event.id) || 0;
              const transformedScore = transformScore(rawScore, scaleMode, allCosineScores);

              // Use SubagentCard for SubagentStop events
              if (group.event.type === "SubagentStop") {
                return (
                  <SubagentCard
                    key={group.event.id}
                    event={group.event}
                    sessionId={sessionId}
                    allEvents={filteredEvents}
                    isMatch={isMatch}
                    similarityScore={rawScore}
                    transformedScore={transformedScore}
                  />
                );
              }

              return (
                <EventCard
                  key={group.event.id}
                  event={group.event}
                  isMatch={isMatch}
                  similarityScore={rawScore}
                  transformedScore={transformedScore}
                />
              );
            }
            return null;
          })}
        </div>
      </ScrollArea>
    </div>
  );
}

// Emojis for event types (matching the markdown format)
const EVENT_EMOJIS: Record<string, string> = {
  SessionStart: "💫",
  SessionEnd: "⭐",
  UserPromptSubmit: "🍄",
  AssistantResponse: "🌲",
  PreToolUse: "🔨",
  PostToolUse: "🏰",
  Stop: "🟢",
  SubagentStop: "🔵",
  Notification: "🟡",
  PreCompact: "♻️",
};

// Events that should be collapsible (long content)
const COLLAPSIBLE_TYPES = ["AssistantResponse", "SubagentStop"];

// Events that should always show expanded (user input is important)
const ALWAYS_EXPANDED_TYPES = ["UserPromptSubmit"];

// Shared prose classes for markdown content
const PROSE_CLASSES = cn(
  "text-sm prose prose-sm prose-invert max-w-none",
  "prose-p:my-1 prose-headings:my-2 prose-pre:my-2 prose-ul:my-1 prose-ol:my-1",
  "prose-code:text-violet-300 prose-code:bg-violet-950/50 prose-code:px-1 prose-code:rounded",
  "prose-pre:bg-zinc-900 prose-pre:border prose-pre:border-zinc-700"
);

// Helper to format time consistently
function formatTime(ts: string): string {
  return new Date(ts).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Helper to check if a prompt is a task notification
function isTaskNotificationPrompt(promptText: string): boolean {
  return promptText.includes("<task-notification>");
}

// Helper to extract task result from notification
function extractTaskResult(promptText: string): { summary: string; content: string } {
  const summaryMatch = promptText.match(/<summary>([^<]+)<\/summary>/);
  const resultMatch = promptText.match(/<result>([\s\S]*?)<\/result>/);
  return {
    summary: summaryMatch ? summaryMatch[1] : "Task completed",
    content: resultMatch ? resultMatch[1].trim() : promptText,
  };
}

function EventCard({
  event,
  isMatch,
  similarityScore,
  transformedScore,
}: {
  event: Event;
  isMatch: boolean;
  similarityScore?: number;  // Raw cosine similarity (0-1)
  transformedScore?: number; // Transformed for display (0-1)
}) {
  // Extract prompt text once for task notification checks
  const promptText = event.type === "UserPromptSubmit"
    ? ((event.data as Record<string, unknown>).prompt as string || event.content || "")
    : "";
  const isTaskNotification = isTaskNotificationPrompt(promptText);

  // Task notifications default to collapsed, regular user prompts default to expanded
  const [isExpanded, setIsExpanded] = useState(
    isMatch || (ALWAYS_EXPANDED_TYPES.includes(event.type) && !isTaskNotification)
  );

  const time = formatTime(event.ts);

  // Use shared hook for markdown highlighting
  const markdownComponents = useMarkdownHighlighting(isMatch);

  // Get content and summary based on event type
  const getEventInfo = () => {
    const data = event.data as Record<string, unknown>;

    switch (event.type) {
      case "UserPromptSubmit": {
        if (isTaskNotification) {
          const { summary } = extractTaskResult(promptText);
          return { summary, content: null, isMarkdown: false };
        }
        return { summary: "User", content: null, isMarkdown: false };
      }

      case "AssistantResponse": {
        const response = (data.response as string) || event.content || "";
        // Get the full first line (no truncation) for the header
        const lines = response.split("\n");
        const firstLine = lines[0];
        // Content starts from line 2 to avoid duplicating the first line in expanded view
        const remainingContent = lines.slice(1).join("\n").trim();
        return {
          summary: `Claude: ${firstLine}`,
          content: remainingContent || null, // null if only one line (nothing to expand)
          isMarkdown: true,
        };
      }

      case "SessionStart": {
        const source = data.source || "startup";
        const model = (data.model as string) || "";
        const modelShort = model.includes("opus")
          ? "Opus"
          : model.includes("sonnet")
          ? "Sonnet"
          : model.includes("haiku")
          ? "Haiku"
          : "";
        return {
          summary: `Session started (${source})${modelShort ? ` - ${modelShort}` : ""}`,
          content: null,
          isMarkdown: false,
        };
      }

      case "PreToolUse": {
        const toolName = (data.tool_name as string) || "Unknown";
        const toolInput = (data.tool_input as Record<string, unknown>) || {};
        let preview = "";
        if (toolName === "Bash") {
          preview = `\`${truncate(String(toolInput.command || ""), 60)}\``;
        } else if (toolName === "Read" || toolName === "Write" || toolName === "Edit") {
          preview = `\`${toolInput.file_path || "file"}\``;
        } else if (toolName === "Glob") {
          preview = `\`${toolInput.pattern || "pattern"}\``;
        } else if (toolName === "Grep") {
          preview = `\`${toolInput.pattern || "pattern"}\``;
        } else if (toolName === "Task") {
          preview = truncate(String(toolInput.description || ""), 50);
        }
        return {
          summary: `${toolName} ${preview}`,
          content: null,
          isMarkdown: true,
        };
      }

      case "PostToolUse": {
        const toolName = (data.tool_name as string) || "Unknown";
        const response = (data.tool_response as Record<string, unknown>) || {};
        if (toolName === "Bash") {
          const stdout = (response.stdout as string) || "";
          if (stdout) {
            const lines = stdout.trim().split("\n");
            return {
              summary: `Output (${lines.length} lines)`,
              content: "```\n" + truncate(stdout, 500) + "\n```",
              isMarkdown: true,
            };
          }
          return { summary: "Completed", content: null, isMarkdown: false };
        } else if (toolName === "Glob") {
          return {
            summary: `Found ${response.numFiles || 0} files`,
            content: null,
            isMarkdown: false,
          };
        }
        return { summary: `${toolName} completed`, content: null, isMarkdown: false };
      }

      case "Stop": {
        const stopHookActive = data.stop_hook_active as boolean;
        if (stopHookActive) {
          return { summary: "⚡ Stop hook triggered (continuation forced)", content: null, isMarkdown: false };
        }
        return { summary: "Claude finished responding", content: null, isMarkdown: false };
      }

      case "Notification":
        return {
          summary: (data.message as string) || "Notification",
          content: null,
          isMarkdown: false,
        };

      case "PermissionRequest": {
        const toolName = (data.tool_name as string) || "";
        return {
          summary: toolName ? `Permission requested for ${toolName}` : "Permission requested",
          content: null,
          isMarkdown: false,
        };
      }

      case "SessionEnd":
        return { summary: "Session ended", content: null, isMarkdown: false };

      case "PreCompact":
        return { summary: "Context compaction starting", content: null, isMarkdown: false };

      case "SubagentStop": {
        const agentType = (data.agent_type as string) || "agent";
        return {
          summary: `Agent (${agentType}) completed`,
          content: null,
          isMarkdown: false,
        };
      }

      default:
        return {
          summary: event.content || JSON.stringify(data).slice(0, 100),
          content: null,
          isMarkdown: false,
        };
    }
  };

  const { summary, content, isMarkdown } = getEventInfo();

  // Task notifications are collapsible, plus the regular collapsible types
  const isCollapsible = isTaskNotification || (COLLAPSIBLE_TYPES.includes(event.type) && content);

  // Use tree emoji for task notifications (they contain agent responses)
  const emoji = isTaskNotification ? "🌲" : (EVENT_EMOJIS[event.type] || "•");

  const isUserOrAssistant =
    (event.type === "UserPromptSubmit" && !isTaskNotification) || event.type === "AssistantResponse";

  // Calculate background color: green spectrum for semantic matches, yellow fallback for keyword-only
  const hasSemanticScore = transformedScore !== undefined && transformedScore > 0;
  const backgroundColor = hasSemanticScore
    ? scoreToBackground(transformedScore)
    : undefined;

  return (
    <div
      className={cn(
        "relative pl-6 pb-2 rounded-lg",
        // Only apply yellow background if keyword match without semantic score
        isMatch && !hasSemanticScore && "bg-yellow-500/10"
      )}
      style={{ backgroundColor }}
    >
      {/* Timeline line */}
      <div className="timeline-line" />
      {/* Timeline dot with emoji */}
      <div
        className={cn(
          "absolute left-0 w-5 h-5 flex items-center justify-center text-sm",
          isMatch && !hasSemanticScore && "ring-2 ring-yellow-500 rounded-full",
          isMatch && hasSemanticScore && "ring-2 ring-green-500 rounded-full"
        )}
      >
        {emoji}
      </div>

      <div
        className={cn(
          "ml-4 rounded-lg overflow-hidden",
          isUserOrAssistant ? "bg-accent/50" : "bg-muted/30",
          isMatch && !hasSemanticScore && "ring-1 ring-yellow-500/50",
          isMatch && hasSemanticScore && "ring-1 ring-green-500/50"
        )}
      >
        {/* Header - clickable for collapsible items */}
        <div
          className={cn(
            "flex items-center gap-2 px-3 py-2 text-xs",
            isCollapsible && "cursor-pointer hover:bg-accent/30"
          )}
          onClick={() => isCollapsible && setIsExpanded(!isExpanded)}
        >
          {isCollapsible && (
            <span className="text-muted-foreground">
              {isExpanded ? (
                <ChevronDown className="w-3 h-3" />
              ) : (
                <ChevronRight className="w-3 h-3" />
              )}
            </span>
          )}
          <span className="text-muted-foreground">{time}</span>
          <span
            className={cn(
              "flex-1",
              isUserOrAssistant ? "font-medium text-foreground" : "text-muted-foreground"
            )}
          >
            {isMarkdown ? (
              <span className="inline-markdown">
                <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{summary}</ReactMarkdown>
              </span>
            ) : (
              summary
            )}
          </span>
          {/* Similarity score badge */}
          {similarityScore !== undefined && similarityScore > 0 && (
            <span className="text-green-400 text-xs ml-auto">
              {formatScorePercent(similarityScore)}
            </span>
          )}
        </div>

        {/* Expanded content for collapsible types */}
        {(isExpanded || !isCollapsible) && content && (
          <div className={cn("px-3 pb-3", PROSE_CLASSES)}>
            <ReactMarkdown
              remarkPlugins={[remarkGfm, remarkBreaks]}
              components={markdownComponents}
            >
              {content}
            </ReactMarkdown>
          </div>
        )}

        {/* User prompt content */}
        {event.type === "UserPromptSubmit" && (
          isTaskNotification ? (
            // Task notification: show extracted result when expanded
            isExpanded && (
              <div className={cn("px-3 pb-3", PROSE_CLASSES)}>
                <ReactMarkdown
                  remarkPlugins={[remarkGfm, remarkBreaks]}
                  components={markdownComponents}
                >
                  {extractTaskResult(promptText).content}
                </ReactMarkdown>
              </div>
            )
          ) : (
            // Regular user prompt: always show text and any images
            <div className="px-3 pb-3 space-y-2">
              {promptText && (
                <HighlightedText
                  text={promptText}
                  isMatch={isMatch}
                  className="text-sm text-foreground whitespace-pre-wrap block"
                />
              )}
              {/* Render attached images if present */}
              {event.images && event.images.length > 0 && (
                <ImageThumbnails
                  images={event.images}
                  sessionId={event.session_id}
                />
              )}
            </div>
          )
        )}
      </div>
    </div>
  );
}

// ImageThumbnails - Display images extracted from user prompts
function ImageThumbnails({
  images,
  sessionId,
}: {
  images: ImageReference[];
  sessionId: string;
}) {
  const [modalImage, setModalImage] = useState<ImageReference | null>(null);

  if (!images || images.length === 0) {
    return null;
  }

  return (
    <>
      {/* Thumbnail grid */}
      <div className="flex gap-2 flex-wrap mt-2">
        {images.map((img, idx) => (
          <ImageThumbnail
            key={`${img.path}-${idx}`}
            image={img}
            sessionId={sessionId}
            onClick={() => setModalImage(img)}
          />
        ))}
      </div>

      {/* Modal for full-size image */}
      {modalImage && (
        <ImageModal
          image={modalImage}
          sessionId={sessionId}
          onClose={() => setModalImage(null)}
        />
      )}
    </>
  );
}

// Individual image thumbnail with loading and error states
function ImageThumbnail({
  image,
  sessionId,
  onClick,
}: {
  image: ImageReference;
  sessionId: string;
  onClick: () => void;
}) {
  const [isLoading, setIsLoading] = useState(true);
  const [isError, setIsError] = useState(false);

  // Get the image URL - handle both extracted files and URL-based images
  const imageUrl = image.url || (image.path ? getImageUrl(sessionId, image.path) : '');

  // Handle invalid image reference (no URL or path)
  if (!imageUrl) {
    return (
      <div
        className={cn(
          "flex items-center justify-center rounded border border-border",
          "w-24 h-24 bg-muted/50 text-muted-foreground"
        )}
        title="Invalid image reference"
      >
        <div className="text-center text-xs">
          <ImageIcon className="w-6 h-6 mx-auto mb-1 opacity-50" />
          <span>Invalid</span>
        </div>
      </div>
    );
  }

  if (isError) {
    return (
      <div
        className={cn(
          "flex items-center justify-center rounded border border-border",
          "w-24 h-24 bg-muted/50 text-muted-foreground"
        )}
        title={`Failed to load: ${image.path || image.url}`}
      >
        <div className="text-center text-xs">
          <ImageIcon className="w-6 h-6 mx-auto mb-1 opacity-50" />
          <span>Error</span>
        </div>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "relative group cursor-pointer rounded border border-border overflow-hidden",
        "w-24 h-24 bg-muted/30 hover:border-primary transition-colors"
      )}
      onClick={onClick}
      title={`${image.media_type} (${formatBytes(image.size)})\nClick to view full size`}
    >
      {/* Loading spinner */}
      {isLoading && (
        <div className="absolute inset-0 flex items-center justify-center bg-muted/50">
          <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />
        </div>
      )}

      {/* Image */}
      <img
        src={imageUrl}
        alt={`Attached image ${image.index + 1}`}
        className={cn(
          "w-full h-full object-cover transition-opacity",
          isLoading && "opacity-0"
        )}
        onLoad={() => setIsLoading(false)}
        onError={() => {
          setIsLoading(false);
          setIsError(true);
        }}
        loading="lazy"
      />

      {/* Hover overlay */}
      <div className="absolute inset-0 bg-black/50 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center">
        <span className="text-white text-xs font-medium">View</span>
      </div>

      {/* Image count badge (for multiple images) */}
      <div className="absolute bottom-1 right-1 bg-black/70 text-white text-xs px-1.5 py-0.5 rounded">
        {formatBytes(image.size)}
      </div>
    </div>
  );
}

// Full-screen image modal
function ImageModal({
  image,
  sessionId,
  onClose,
}: {
  image: ImageReference;
  sessionId: string;
  onClose: () => void;
}) {
  const imageUrl = image.url || (image.path ? getImageUrl(sessionId, image.path) : '');

  // Handle escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const handleDownload = () => {
    const a = document.createElement("a");
    a.href = imageUrl;
    a.download = image.path?.split("/").pop() || "image";
    a.click();
  };

  const handleOpenInNewTab = () => {
    window.open(imageUrl, "_blank");
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/90 flex items-center justify-center"
      onClick={onClose}
    >
      {/* Close button */}
      <button
        className="absolute top-4 right-4 text-white hover:text-gray-300 transition-colors"
        onClick={onClose}
        title="Close (Esc)"
      >
        <X className="w-8 h-8" />
      </button>

      {/* Action buttons */}
      <div className="absolute top-4 left-4 flex gap-2">
        <button
          className="p-2 bg-white/10 hover:bg-white/20 rounded text-white transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            handleDownload();
          }}
          title="Download"
        >
          <Download className="w-5 h-5" />
        </button>
        <button
          className="p-2 bg-white/10 hover:bg-white/20 rounded text-white transition-colors"
          onClick={(e) => {
            e.stopPropagation();
            handleOpenInNewTab();
          }}
          title="Open in new tab"
        >
          <ExternalLink className="w-5 h-5" />
        </button>
      </div>

      {/* Image info */}
      <div className="absolute bottom-4 left-4 text-white/80 text-sm">
        <span>{image.media_type}</span>
        <span className="mx-2">·</span>
        <span>{formatBytes(image.size)}</span>
      </div>

      {/* Image container */}
      <div
        className="max-w-[90vw] max-h-[90vh] overflow-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <img
          src={imageUrl}
          alt="Full size image"
          className="max-w-full max-h-[90vh] object-contain"
        />
      </div>
    </div>
  );
}

// Helper to format bytes to human-readable size
function formatBytes(bytes: number | undefined): string {
  if (bytes === undefined || bytes === null) return "";
  if (bytes === 0) return "0 B";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

// SubagentCard - Enhanced view for SubagentStop events with transcript
function SubagentCard({
  event,
  sessionId,
  allEvents,
  isMatch,
  similarityScore,
  transformedScore,
}: {
  event: Event;
  sessionId: string;
  allEvents: Event[];
  isMatch: boolean;
  similarityScore?: number;
  transformedScore?: number;
}) {
  const [isExpanded, setIsExpanded] = useState(isMatch);
  const [showPrompt, setShowPrompt] = useState(false);
  const [showResponse, setShowResponse] = useState(false);
  const [transcript, setTranscript] = useState<SubagentTranscript | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const data = event.data as Record<string, unknown>;
  const agentId = (data.agent_id as string) || "unknown";

  // Find the Task PreToolUse event that spawned this subagent
  // Look for the most recent Task event before this SubagentStop
  const taskEvent = allEvents
    .filter(e =>
      e.type === "PreToolUse" &&
      (e.data as Record<string, unknown>).tool_name === "Task" &&
      new Date(e.ts) < new Date(event.ts)
    )
    .sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime())[0];

  const taskData = taskEvent?.data as Record<string, unknown> | undefined;
  const taskInput = taskData?.tool_input as Record<string, unknown> | undefined;
  const agentType = (taskInput?.subagent_type as string) || "Subagent";
  const description = (taskInput?.description as string) || "";

  const time = formatTime(event.ts);

  // Use shared hook for markdown highlighting
  const markdownComponents = useMarkdownHighlighting(isMatch);

  // Fetch transcript when expanded
  const fetchTranscript = async () => {
    if (transcript || isLoading) return;

    setIsLoading(true);
    setError(null);

    try {
      const data = await getSubagentTranscript(sessionId, agentId);
      setTranscript(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load transcript");
    } finally {
      setIsLoading(false);
    }
  };

  // Fetch on expand
  useEffect(() => {
    if (isExpanded && !transcript && !isLoading) {
      fetchTranscript();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isExpanded]);

  // Calculate background color: green spectrum for semantic matches
  const hasSemanticScore = transformedScore !== undefined && transformedScore > 0;
  const backgroundColor = hasSemanticScore
    ? scoreToBackground(transformedScore)
    : undefined;

  return (
    <div
      className={cn(
        "relative pl-6 pb-2 rounded-lg",
        isMatch && !hasSemanticScore && "bg-yellow-500/10"
      )}
      style={{ backgroundColor }}
    >
      {/* Timeline line */}
      <div className="timeline-line" />
      {/* Timeline dot */}
      <div
        className={cn(
          "absolute left-0 w-5 h-5 flex items-center justify-center text-sm",
          isMatch && !hasSemanticScore && "ring-2 ring-yellow-500 rounded-full",
          isMatch && hasSemanticScore && "ring-2 ring-green-500 rounded-full"
        )}
      >
        🔵
      </div>

      <div
        className={cn(
          "ml-4 rounded-lg overflow-hidden bg-blue-500/10 border border-blue-500/30",
          isMatch && !hasSemanticScore && "ring-1 ring-yellow-500/50",
          isMatch && hasSemanticScore && "ring-1 ring-green-500/50"
        )}
      >
        {/* Header - clickable to expand */}
        <div
          className="flex items-center gap-2 px-3 py-2 text-xs cursor-pointer hover:bg-blue-500/20"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <span className="text-muted-foreground">
            {isExpanded ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
          </span>
          <span className="text-muted-foreground">{time}</span>
          <Bot className="w-3 h-3 text-blue-400" />
          <span className="text-blue-300 font-medium">{agentType}</span>
          {description && (
            <span className="text-blue-200/80 truncate max-w-[300px]">
              ({description})
            </span>
          )}
          <code className="text-blue-400/50 text-xs font-mono ml-auto">{agentId}</code>
          {transcript && (
            <span className="text-muted-foreground text-xs">
              {transcript.message_count} msgs
            </span>
          )}
        </div>

        {/* Expanded content */}
        {isExpanded && (
          <div className="px-3 pb-3 space-y-2 border-t border-blue-500/20 pt-2">
            {isLoading && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Loader2 className="w-3 h-3 animate-spin" />
                Loading transcript...
              </div>
            )}

            {error && (
              <div className="text-xs text-red-400">
                {error}
              </div>
            )}

            {transcript && (
              <>
                {/* Prompt section */}
                <div className="space-y-1">
                  <button
                    onClick={() => setShowPrompt(!showPrompt)}
                    className="flex items-center gap-1 text-xs text-blue-300 hover:text-blue-200"
                  >
                    {showPrompt ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                    <span className="font-medium">Prompt</span>
                    {!showPrompt && transcript.prompt && (
                      <span className="text-muted-foreground ml-2 truncate max-w-[300px]">
                        {transcript.prompt.slice(0, 80)}...
                      </span>
                    )}
                  </button>
                  {showPrompt && transcript.prompt && (
                    <div className={cn("ml-4 p-2 bg-zinc-900/50 rounded", PROSE_CLASSES)}>
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm, remarkBreaks]}
                        components={markdownComponents}
                      >
                        {transcript.prompt}
                      </ReactMarkdown>
                    </div>
                  )}
                </div>

                {/* Response section */}
                <div className="space-y-1">
                  <button
                    onClick={() => setShowResponse(!showResponse)}
                    className="flex items-center gap-1 text-xs text-green-300 hover:text-green-200"
                  >
                    {showResponse ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                    <span className="font-medium">Response</span>
                    {!showResponse && transcript.response && (
                      <span className="text-muted-foreground ml-2 truncate max-w-[300px]">
                        {transcript.response.slice(0, 80)}...
                      </span>
                    )}
                  </button>
                  {showResponse && transcript.response && (
                    <div className={cn("ml-4 p-2 bg-zinc-900/50 rounded", PROSE_CLASSES)}>
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm, remarkBreaks]}
                        components={markdownComponents}
                      >
                        {transcript.response}
                      </ReactMarkdown>
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// AskUserQuestion Card - Special rendering for question events
function AskUserQuestionCard({
  event,
  responseEvent,
  isMatch,
}: {
  event: Event;
  responseEvent?: Event;
  isMatch: boolean;
}) {
  const [isExpanded, setIsExpanded] = useState(isMatch);

  const data = event.data as Record<string, unknown>;
  const toolInput = data.tool_input as Record<string, unknown>;
  const questions = (toolInput?.questions as Array<{
    question: string;
    header: string;
    options: Array<{ label: string; description: string }>;
    multiSelect?: boolean;
  }>) || [];

  // Get response if available
  const responseData = responseEvent?.data as Record<string, unknown> | undefined;
  const toolResponse = responseData?.tool_response as Record<string, unknown> | undefined;
  const answers = toolResponse?.answers as Record<string, string> | undefined;

  const time = formatTime(event.ts);

  // Get first question for summary (no truncation - CSS handles overflow)
  const firstQuestion = questions[0];
  const questionPreview = firstQuestion?.question || "Question";

  return (
    <div className={cn("relative pl-6 pb-2", isMatch && "bg-yellow-500/10 rounded-lg")}>
      {/* Timeline line */}
      <div className="timeline-line" />
      {/* Timeline dot */}
      <div
        className={cn(
          "absolute left-0 w-5 h-5 flex items-center justify-center text-sm",
          isMatch && "ring-2 ring-yellow-500 rounded-full"
        )}
      >
        ❓
      </div>

      <div
        className={cn(
          "ml-4 rounded-lg overflow-hidden bg-amber-500/10 border border-amber-500/30",
          isMatch && "ring-1 ring-yellow-500/50"
        )}
      >
        {/* Header - clickable to expand */}
        <div
          className="flex items-center gap-2 px-3 py-2 text-xs cursor-pointer hover:bg-amber-500/20"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <span className="text-muted-foreground">
            {isExpanded ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
          </span>
          <span className="text-muted-foreground">{time}</span>
          <span className="text-amber-300 font-medium">Question</span>
          {!isExpanded && (
            <span className="text-amber-200/80 flex-1">
              {questionPreview}
            </span>
          )}
          {answers && (
            <Badge variant="outline" className="ml-auto text-green-400 border-green-500/30 text-xs">
              Answered
            </Badge>
          )}
        </div>

        {/* Expanded content */}
        {isExpanded && (
          <div className="px-3 pb-3 space-y-3 border-t border-amber-500/20 pt-2">
            {questions.map((q, idx) => (
              <div key={idx} className="space-y-2">
                {/* Question header badge */}
                {q.header && (
                  <Badge variant="outline" className="text-amber-400 border-amber-500/40 text-xs">
                    {q.header}
                  </Badge>
                )}
                {/* Question text */}
                <p className="text-sm text-amber-100">{q.question}</p>
                {/* Options */}
                <div className="grid gap-1.5 ml-2">
                  {q.options.map((opt, optIdx) => {
                    const isSelected = answers && Object.values(answers).some(
                      a => a.toLowerCase().includes(opt.label.toLowerCase())
                    );
                    return (
                      <div
                        key={optIdx}
                        className={cn(
                          "flex items-start gap-2 p-2 rounded text-xs",
                          isSelected
                            ? "bg-green-500/20 border border-green-500/40"
                            : "bg-zinc-800/50 border border-zinc-700/50"
                        )}
                      >
                        <span className={cn(
                          "mt-0.5",
                          isSelected ? "text-green-400" : "text-muted-foreground"
                        )}>
                          {isSelected ? "✓" : "○"}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className={cn(
                            "font-medium",
                            isSelected ? "text-green-300" : "text-foreground"
                          )}>
                            {opt.label}
                          </div>
                          {opt.description && (
                            <div className="text-muted-foreground text-xs mt-0.5">
                              {opt.description}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
                {/* Custom answer if provided */}
                {answers && !q.options.some(opt =>
                  Object.values(answers).some(a => a.toLowerCase().includes(opt.label.toLowerCase()))
                ) && (
                  <div className="ml-2 p-2 bg-green-500/20 border border-green-500/40 rounded text-xs">
                    <span className="text-green-400 mr-2">✓</span>
                    <span className="text-green-300">{Object.values(answers)[0]}</span>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// Structured tool call data
interface ToolCall {
  id: string;
  name: string;
  summary: string;
  input: Record<string, unknown>;
  inputPreview: string;
  output?: Record<string, unknown>;
  outputPreview?: string;
  hasExpandableInput: boolean;
  hasExpandableOutput: boolean;
}

// Parse tool events into structured tool calls
function parseToolCalls(events: Event[]): ToolCall[] {
  const calls: ToolCall[] = [];
  const pendingPre: Map<string, ToolCall> = new Map();

  for (const event of events) {
    const data = event.data as Record<string, unknown>;
    const toolName = (data.tool_name as string) || "Unknown";
    const toolUseId = (data.tool_use_id as string) || event.id;

    if (event.type === "PreToolUse") {
      const toolInput = (data.tool_input as Record<string, unknown>) || {};

      // Generate preview based on tool type
      let inputPreview = "";
      let hasExpandableInput = false;

      if (toolName === "Bash") {
        const cmd = String(toolInput.command || "");
        inputPreview = cmd.length > 80 ? cmd.slice(0, 80) + "..." : cmd;
        hasExpandableInput = cmd.length > 80;
      } else if (toolName === "Read" || toolName === "Write") {
        const path = String(toolInput.file_path || "");
        inputPreview = path.split("/").slice(-2).join("/");
      } else if (toolName === "Edit") {
        const path = String(toolInput.file_path || "");
        inputPreview = path.split("/").slice(-2).join("/");
        hasExpandableInput = !!(toolInput.old_string || toolInput.new_string);
      } else if (toolName === "Glob") {
        inputPreview = String(toolInput.pattern || "");
      } else if (toolName === "Grep") {
        inputPreview = String(toolInput.pattern || "");
        hasExpandableInput = Object.keys(toolInput).length > 1;
      } else if (toolName === "Task") {
        const desc = String(toolInput.description || "");
        inputPreview = desc.length > 60 ? desc.slice(0, 60) + "..." : desc;
        hasExpandableInput = !!(toolInput.prompt);
      } else {
        inputPreview = Object.keys(toolInput).slice(0, 2).join(", ");
        hasExpandableInput = Object.keys(toolInput).length > 0;
      }

      const call: ToolCall = {
        id: toolUseId,
        name: toolName,
        summary: `${toolName}`,
        input: toolInput,
        inputPreview,
        hasExpandableInput,
        hasExpandableOutput: false,
      };

      pendingPre.set(toolUseId, call);
      calls.push(call);
    } else if (event.type === "PostToolUse") {
      const response = (data.tool_response as Record<string, unknown>) || {};
      const existingCall = pendingPre.get(toolUseId);

      if (existingCall) {
        existingCall.output = response;

        // Generate output preview
        if (toolName === "Bash") {
          const stdout = (response.stdout as string) || "";
          const stderr = (response.stderr as string) || "";
          const interrupted = response.interrupted as boolean;

          if (interrupted) {
            existingCall.outputPreview = "⚠ interrupted";
            existingCall.hasExpandableOutput = !!(stdout || stderr);
          } else if (stdout) {
            const lines = stdout.trim().split("\n");
            existingCall.outputPreview = `${lines.length} line${lines.length !== 1 ? "s" : ""}`;
            existingCall.hasExpandableOutput = true;
          } else if (stderr) {
            existingCall.outputPreview = "⚠ stderr";
            existingCall.hasExpandableOutput = true;
          } else {
            existingCall.outputPreview = "✓ (no output)";
            existingCall.hasExpandableOutput = false;
          }
        } else if (toolName === "Glob") {
          const files = (response.filePaths as string[]) || [];
          existingCall.outputPreview = `${files.length} file${files.length !== 1 ? "s" : ""}`;
          existingCall.hasExpandableOutput = files.length > 0;
        } else if (toolName === "Grep") {
          const content = response.content as string;
          if (content) {
            const lines = content.trim().split("\n");
            existingCall.outputPreview = `${lines.length} match${lines.length !== 1 ? "es" : ""}`;
          } else {
            existingCall.outputPreview = "no matches";
          }
          existingCall.hasExpandableOutput = !!content;
        } else if (toolName === "Read") {
          const content = response.content as string;
          if (content) {
            const lines = content.split("\n").length;
            existingCall.outputPreview = `${lines} lines`;
            existingCall.hasExpandableOutput = true;
          } else {
            existingCall.outputPreview = "✓";
            existingCall.hasExpandableOutput = false;
          }
        } else if (toolName === "Write" || toolName === "Edit") {
          existingCall.outputPreview = "✓ saved";
          existingCall.hasExpandableOutput = false;
        } else {
          existingCall.outputPreview = "✓";
          existingCall.hasExpandableOutput = Object.keys(response).length > 0;
        }
      }
    }
  }

  return calls;
}

// Individual tool item component with its own expand state
function ToolItem({ call }: { call: ToolCall }) {
  const [showInput, setShowInput] = useState(false);
  const [showOutput, setShowOutput] = useState(false);

  const formatInput = () => {
    if (call.name === "Bash") {
      return "```bash\n" + (call.input.command || "") + "\n```";
    } else if (call.name === "Edit") {
      let result = "";
      if (call.input.old_string) {
        result += "**old_string:**\n```\n" + call.input.old_string + "\n```\n";
      }
      if (call.input.new_string) {
        result += "**new_string:**\n```\n" + call.input.new_string + "\n```";
      }
      return result || JSON.stringify(call.input, null, 2);
    } else if (call.name === "Task") {
      return "**prompt:**\n" + (call.input.prompt || call.input.description || "");
    }
    return "```json\n" + JSON.stringify(call.input, null, 2) + "\n```";
  };

  const formatOutput = () => {
    if (!call.output) return "";

    if (call.name === "Bash") {
      const stdout = (call.output.stdout as string) || "";
      const stderr = (call.output.stderr as string) || "";
      let result = "";
      if (stdout) result += "```\n" + stdout + "\n```";
      if (stderr) result += "\n**stderr:**\n```\n" + stderr + "\n```";
      return result || "*No output*";
    } else if (call.name === "Glob") {
      const files = (call.output.filePaths as string[]) || [];
      if (files.length === 0) return "*No files found*";
      return files.map(f => "- `" + f + "`").join("\n");
    } else if (call.name === "Grep") {
      const content = (call.output.content as string) || "";
      if (!content) return "*No matches*";
      return "```\n" + content + "\n```";
    } else if (call.name === "Read") {
      const content = (call.output.content as string) || "";
      if (!content) return "*Empty file*";
      // Show with line numbers like the tool does
      return "```\n" + content + "\n```";
    }
    return "```json\n" + JSON.stringify(call.output, null, 2) + "\n```";
  };

  return (
    <div className="border-l-2 border-purple-500/30 pl-3 py-1">
      {/* Tool header line */}
      <div className="flex items-center gap-2 text-xs">
        <span className="text-purple-400">🔨</span>
        <span className="text-purple-300 font-medium">{call.name}</span>
        {call.inputPreview && (
          <code className="text-violet-300 bg-violet-950/50 px-1.5 py-0.5 rounded text-xs font-mono">
            {call.inputPreview}
          </code>
        )}
        {call.outputPreview && (
          <span className="text-green-400 text-xs">→ {call.outputPreview}</span>
        )}
      </div>

      {/* Expandable toggles */}
      <div className="flex gap-3 mt-1 ml-5">
        {call.hasExpandableInput && (
          <button
            onClick={() => setShowInput(!showInput)}
            className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
          >
            {showInput ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            Input
          </button>
        )}
        {call.hasExpandableOutput && (
          <button
            onClick={() => setShowOutput(!showOutput)}
            className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
          >
            {showOutput ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            Output
          </button>
        )}
      </div>

      {/* Expanded input */}
      {showInput && (
        <div className={cn("mt-2 ml-5", PROSE_CLASSES, "prose-pre:text-xs prose-pre:overflow-x-auto")}>
          <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{formatInput()}</ReactMarkdown>
        </div>
      )}

      {/* Expanded output */}
      {showOutput && (
        <div className={cn("mt-2 ml-5", PROSE_CLASSES, "prose-pre:text-xs prose-pre:overflow-x-auto")}>
          <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{formatOutput()}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}

// Tool group card - displays grouped PreToolUse/PostToolUse events
function ToolGroupCard({
  events,
  hasMatch,
}: {
  events: Event[];
  hasMatch: boolean;
}) {
  const [isExpanded, setIsExpanded] = useState(hasMatch);

  // Parse into structured tool calls
  const toolCalls = parseToolCalls(events);

  // Count tools by name for summary
  const toolCounts: Record<string, number> = {};
  for (const call of toolCalls) {
    toolCounts[call.name] = (toolCounts[call.name] || 0) + 1;
  }

  const toolSummary = Object.entries(toolCounts)
    .map(([name, count]) => (count > 1 ? `${name} (${count})` : name))
    .join(", ");

  const time = formatTime(events[0].ts);

  return (
    <div className={cn("relative pl-6 pb-2", hasMatch && "bg-yellow-500/10 rounded-lg")}>
      {/* Timeline line */}
      <div className="timeline-line" />
      {/* Timeline dot with wrench icon */}
      <div
        className={cn(
          "absolute left-0 w-5 h-5 flex items-center justify-center text-sm",
          hasMatch && "ring-2 ring-yellow-500 rounded-full"
        )}
      >
        🔧
      </div>

      <div
        className={cn(
          "ml-4 rounded-lg overflow-hidden bg-muted/30",
          hasMatch && "ring-1 ring-yellow-500/50"
        )}
      >
        {/* Collapsible header */}
        <div
          className="flex items-center gap-2 px-3 py-2 text-xs cursor-pointer hover:bg-accent/30"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <span className="text-muted-foreground">
            {isExpanded ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
          </span>
          <span className="text-muted-foreground">{time}</span>
          <Wrench className="w-3 h-3 text-purple-400" />
          <span className="text-muted-foreground">
            <span className="text-purple-300 font-medium">
              {toolCalls.length} tool{toolCalls.length !== 1 ? "s" : ""}
            </span>
            : {toolSummary}
          </span>
        </div>

        {/* Expanded tool list */}
        {isExpanded && (
          <div className="px-3 pb-3 space-y-2 border-t border-border/50 pt-2">
            {toolCalls.map((call) => (
              <ToolItem key={call.id} call={call} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
