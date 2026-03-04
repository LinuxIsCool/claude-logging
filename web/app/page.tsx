"use client";

import { useState, useEffect } from "react";
import { BarChart3, MessageSquare, Sparkles, Clock } from "lucide-react";
import { SessionList } from "@/components/session-list";
import { StatsPanel } from "@/components/stats-panel";
import { EmbeddingGraph } from "@/components/embedding-graph";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScoringSettingsProvider } from "@/lib/scoring-settings";

export default function Home() {
  const [stats, setStats] = useState<{
    session_count: number;
    event_count: number;
    total_tokens: number;
    first_session: string | null;
    last_session: string | null;
  } | null>(null);

  useEffect(() => {
    fetch("/api/stats")
      .then((res) => res.json())
      .then(setStats)
      .catch(console.error);
  }, []);

  return (
    <ScoringSettingsProvider>
    <div className="flex flex-col h-screen">
      {/* Header */}
      <header className="border-b px-6 py-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-violet-500 to-purple-600 flex items-center justify-center">
              <MessageSquare className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-xl font-semibold">Claude Code Logs</h1>
              <p className="text-sm text-muted-foreground">
                {stats
                  ? `${stats.session_count.toLocaleString()} sessions · ${stats.event_count.toLocaleString()} events`
                  : "Loading..."}
              </p>
            </div>
          </div>

          {/* Quick stats */}
          <div className="flex items-center gap-6 text-sm">
            {stats?.last_session && (
              <div className="flex items-center gap-2 text-muted-foreground">
                <Clock className="w-4 h-4" />
                <span>Last: {new Date(stats.last_session).toLocaleDateString()}</span>
              </div>
            )}
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 overflow-hidden">
        <Tabs defaultValue="sessions" className="h-full flex flex-col">
          <div className="border-b px-6">
            <TabsList className="h-12">
              <TabsTrigger value="sessions" className="gap-2">
                <MessageSquare className="w-4 h-4" />
                Sessions
              </TabsTrigger>
              <TabsTrigger value="graph" className="gap-2">
                <Sparkles className="w-4 h-4" />
                Embeddings
              </TabsTrigger>
              <TabsTrigger value="stats" className="gap-2">
                <BarChart3 className="w-4 h-4" />
                Statistics
              </TabsTrigger>
            </TabsList>
          </div>

          <div className="flex-1 overflow-hidden">
            <TabsContent value="sessions" className="h-full m-0 p-6">
              <SessionList />
            </TabsContent>

            <TabsContent value="graph" className="h-full m-0 p-6">
              <EmbeddingGraph />
            </TabsContent>

            <TabsContent value="stats" className="h-full m-0 p-6">
              <StatsPanel stats={stats} />
            </TabsContent>
          </div>
        </Tabs>
      </main>
    </div>
    </ScoringSettingsProvider>
  );
}
