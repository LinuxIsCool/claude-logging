"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2, ZoomIn, ZoomOut, Maximize2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getEventTypeColor, getEventTypeLabel, formatDateTime } from "@/lib/utils";

interface EmbeddingNode {
  id: string;
  x: number;
  y: number;
  type: string;
  content: string;
  session_id: string;
  timestamp: string;
}

// Mock data for demonstration - in production, this would come from the API
const MOCK_NODES: EmbeddingNode[] = Array.from({ length: 100 }, (_, i) => ({
  id: `node-${i}`,
  x: Math.random() * 800 - 400,
  y: Math.random() * 600 - 300,
  type: ["UserPromptSubmit", "Stop", "PreToolUse", "PostToolUse"][Math.floor(Math.random() * 4)],
  content: `Event ${i}: Sample content for embedding visualization`,
  session_id: `session-${Math.floor(i / 10)}`,
  timestamp: new Date(Date.now() - Math.random() * 7 * 24 * 60 * 60 * 1000).toISOString(),
}));

export function EmbeddingGraph() {
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [nodes, setNodes] = useState<EmbeddingNode[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [selectedNode, setSelectedNode] = useState<EmbeddingNode | null>(null);
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 });
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });

  // Load nodes (mock data for now)
  useEffect(() => {
    const timer = setTimeout(() => {
      setNodes(MOCK_NODES);
      setIsLoading(false);
    }, 500);
    return () => clearTimeout(timer);
  }, []);

  // Render canvas
  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Set canvas size
    const rect = container.getBoundingClientRect();
    canvas.width = rect.width;
    canvas.height = rect.height;

    // Clear canvas
    ctx.fillStyle = "hsl(240 10% 3.9%)";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    // Apply transform
    ctx.save();
    ctx.translate(canvas.width / 2 + transform.x, canvas.height / 2 + transform.y);
    ctx.scale(transform.scale, transform.scale);

    // Draw connections between nearby nodes
    ctx.strokeStyle = "rgba(100, 100, 100, 0.2)";
    ctx.lineWidth = 0.5;
    nodes.forEach((node, i) => {
      nodes.slice(i + 1).forEach((other) => {
        const dist = Math.hypot(node.x - other.x, node.y - other.y);
        if (dist < 100) {
          ctx.beginPath();
          ctx.moveTo(node.x, node.y);
          ctx.lineTo(other.x, other.y);
          ctx.stroke();
        }
      });
    });

    // Draw nodes
    nodes.forEach((node) => {
      const isSelected = selectedNode?.id === node.id;
      const radius = isSelected ? 8 : 5;

      // Get color based on event type
      let color: string;
      switch (node.type) {
        case "UserPromptSubmit":
          color = "#3b82f6"; // blue
          break;
        case "Stop":
        case "SubagentStop":
          color = "#22c55e"; // green
          break;
        case "PreToolUse":
        case "PostToolUse":
          color = "#a855f7"; // purple
          break;
        default:
          color = "#6b7280"; // gray
      }

      // Draw node
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = isSelected ? "#ffffff" : color;
      ctx.fill();

      if (isSelected) {
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    });

    ctx.restore();
  }, [nodes, transform, selectedNode]);

  // Handle canvas interactions
  const handleMouseDown = (e: React.MouseEvent) => {
    setIsDragging(true);
    setDragStart({ x: e.clientX - transform.x, y: e.clientY - transform.y });
  };

  const handleMouseMove = (e: React.MouseEvent) => {
    if (isDragging) {
      setTransform((prev) => ({
        ...prev,
        x: e.clientX - dragStart.x,
        y: e.clientY - dragStart.y,
      }));
    }
  };

  const handleMouseUp = () => {
    setIsDragging(false);
  };

  const handleClick = (e: React.MouseEvent) => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const rect = canvas.getBoundingClientRect();
    const clickX = (e.clientX - rect.left - canvas.width / 2 - transform.x) / transform.scale;
    const clickY = (e.clientY - rect.top - canvas.height / 2 - transform.y) / transform.scale;

    // Find clicked node
    const clickedNode = nodes.find((node) => {
      const dist = Math.hypot(node.x - clickX, node.y - clickY);
      return dist < 10;
    });

    setSelectedNode(clickedNode || null);
  };

  const handleZoom = (delta: number) => {
    setTransform((prev) => ({
      ...prev,
      scale: Math.max(0.5, Math.min(3, prev.scale + delta)),
    }));
  };

  const handleReset = () => {
    setTransform({ x: 0, y: 0, scale: 1 });
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="w-8 h-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="flex gap-6 h-full">
      {/* Graph */}
      <div className="flex-1 relative">
        <div
          ref={containerRef}
          className="graph-container cursor-grab active:cursor-grabbing"
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
          onClick={handleClick}
        >
          <canvas ref={canvasRef} className="w-full h-full" />
        </div>

        {/* Controls */}
        <div className="absolute top-4 right-4 flex flex-col gap-2">
          <Button
            variant="secondary"
            size="icon"
            onClick={() => handleZoom(0.2)}
          >
            <ZoomIn className="w-4 h-4" />
          </Button>
          <Button
            variant="secondary"
            size="icon"
            onClick={() => handleZoom(-0.2)}
          >
            <ZoomOut className="w-4 h-4" />
          </Button>
          <Button variant="secondary" size="icon" onClick={handleReset}>
            <Maximize2 className="w-4 h-4" />
          </Button>
        </div>

        {/* Legend */}
        <div className="absolute bottom-4 left-4 flex gap-4 text-xs">
          <div className="flex items-center gap-1">
            <div className="w-3 h-3 rounded-full bg-blue-500" />
            <span>Prompts</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="w-3 h-3 rounded-full bg-green-500" />
            <span>Responses</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="w-3 h-3 rounded-full bg-purple-500" />
            <span>Tools</span>
          </div>
        </div>
      </div>

      {/* Node Details */}
      <div className="w-80">
        <Card className="h-full">
          <CardHeader>
            <CardTitle className="text-lg">Node Details</CardTitle>
          </CardHeader>
          <CardContent>
            {selectedNode ? (
              <div className="space-y-4">
                <div>
                  <span className={`event-badge ${getEventTypeColor(selectedNode.type)}`}>
                    {getEventTypeLabel(selectedNode.type)}
                  </span>
                </div>
                <div>
                  <p className="text-xs text-muted-foreground">Timestamp</p>
                  <p className="text-sm">{formatDateTime(selectedNode.timestamp)}</p>
                </div>
                <div>
                  <p className="text-xs text-muted-foreground">Session</p>
                  <p className="text-sm font-mono">{selectedNode.session_id}</p>
                </div>
                <div>
                  <p className="text-xs text-muted-foreground">Content</p>
                  <p className="text-sm">{selectedNode.content}</p>
                </div>
                <div>
                  <p className="text-xs text-muted-foreground">Position</p>
                  <p className="text-sm font-mono">
                    ({selectedNode.x.toFixed(1)}, {selectedNode.y.toFixed(1)})
                  </p>
                </div>
              </div>
            ) : (
              <p className="text-muted-foreground text-sm">
                Click a node to see details
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
