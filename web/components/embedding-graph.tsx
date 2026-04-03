"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function EmbeddingGraph() {
  return (
    <div className="flex items-center justify-center h-full">
      <Card className="max-w-lg">
        <CardHeader>
          <CardTitle className="text-lg">Embedding Visualization</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Embedding visualization is not yet available. To enable it:
          </p>
          <ol className="text-sm text-muted-foreground list-decimal list-inside space-y-2">
            <li>Install embedding dependencies: <code className="text-xs bg-muted px-1 py-0.5 rounded">uv sync --extra embeddings</code></li>
            <li>Generate embeddings: <code className="text-xs bg-muted px-1 py-0.5 rounded">uv run scripts/embed_backfill.py</code></li>
            <li>A 2D projection of your conversation embeddings will appear here</li>
          </ol>
          <p className="text-xs text-muted-foreground mt-4">
            See <code>examples/embedding-setup.md</code> for details.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
