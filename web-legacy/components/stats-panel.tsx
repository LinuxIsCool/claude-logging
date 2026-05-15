"use client";

import { MessageSquare, Calendar, Hash, Zap } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDate } from "@/lib/utils";

interface Stats {
  session_count: number;
  event_count: number;
  total_tokens: number;
  first_session: string | null;
  last_session: string | null;
}

export function StatsPanel({ stats }: { stats: Stats | null }) {
  if (!stats) {
    return (
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {[...Array(4)].map((_, i) => (
          <Card key={i}>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">
                <div className="h-4 w-24 shimmer rounded" />
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="h-8 w-16 shimmer rounded" />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  const statCards = [
    {
      title: "Total Sessions",
      value: stats.session_count.toLocaleString(),
      icon: MessageSquare,
      description: stats.first_session
        ? `Since ${formatDate(stats.first_session)}`
        : "No sessions yet",
    },
    {
      title: "Total Events",
      value: stats.event_count.toLocaleString(),
      icon: Hash,
      description: stats.session_count
        ? `~${Math.round(stats.event_count / stats.session_count)} per session`
        : "No events yet",
    },
    {
      title: "Total Tokens",
      value: formatTokens(stats.total_tokens),
      icon: Zap,
      description: stats.session_count
        ? `~${formatTokens(Math.round(stats.total_tokens / stats.session_count))} per session`
        : "No tokens yet",
    },
    {
      title: "Last Activity",
      value: stats.last_session
        ? formatDate(stats.last_session)
        : "Never",
      icon: Calendar,
      description: stats.last_session
        ? getRelativeTime(stats.last_session)
        : "No activity yet",
    },
  ];

  return (
    <div className="space-y-6">
      {/* Stats Grid */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {statCards.map((stat) => (
          <Card key={stat.title}>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">
                {stat.title}
              </CardTitle>
              <stat.icon className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{stat.value}</div>
              <p className="text-xs text-muted-foreground">{stat.description}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Activity Overview */}
      <Card>
        <CardHeader>
          <CardTitle>Activity Overview</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">
                Average events per session
              </span>
              <span className="font-medium">
                {stats.session_count
                  ? Math.round(stats.event_count / stats.session_count)
                  : 0}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">
                Average tokens per session
              </span>
              <span className="font-medium">
                {stats.session_count
                  ? formatTokens(Math.round(stats.total_tokens / stats.session_count))
                  : 0}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">
                Average tokens per event
              </span>
              <span className="font-medium">
                {stats.event_count
                  ? formatTokens(Math.round(stats.total_tokens / stats.event_count))
                  : 0}
              </span>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Usage Tips */}
      <Card>
        <CardHeader>
          <CardTitle>Quick Tips</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="space-y-2 text-sm text-muted-foreground">
            <li>
              💡 Use <code className="text-foreground">/log-search</code> in Claude Code for quick searches
            </li>
            <li>
              💡 Enable semantic search for conceptual queries like &ldquo;authentication flow&rdquo;
            </li>
            <li>
              💡 Filter by event type to focus on prompts, tool calls, or responses
            </li>
            <li>
              💡 Open in Obsidian with <code className="text-foreground">/obsidian</code> for graph view
            </li>
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}

function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000) {
    return `${(tokens / 1_000_000).toFixed(1)}M`;
  }
  if (tokens >= 1_000) {
    return `${(tokens / 1_000).toFixed(1)}K`;
  }
  return tokens.toString();
}

function getRelativeTime(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays} days ago`;
  if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`;
  if (diffDays < 365) return `${Math.floor(diffDays / 30)} months ago`;
  return `${Math.floor(diffDays / 365)} years ago`;
}
