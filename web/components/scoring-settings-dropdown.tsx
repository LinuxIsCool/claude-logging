"use client";

import React from "react";
import { Settings } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  useScoringSettings,
  ScaleMode,
  SessionAggregateMode,
  KeywordHighlightMode,
  transformScore,
  scoreToBackground,
} from "@/lib/scoring-settings";

/**
 * Settings dropdown for search scoring visualization.
 *
 * Placed in the top-right of the app, allows users to configure:
 * - Scale mode (linear/logarithmic/ordinal)
 * - Session aggregate (max/mean/sum)
 * - Keyword highlight color
 */
export function ScoringSettingsDropdown() {
  const { settings, updateSettings } = useScoringSettings();
  const [isOpen, setIsOpen] = React.useState(false);
  const dropdownRef = React.useRef<HTMLDivElement>(null);

  // Close on click outside
  React.useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const scaleOptions: { value: ScaleMode; label: string; desc: string }[] = [
    { value: "linear", label: "Linear", desc: "Direct 1:1 mapping" },
    { value: "logarithmic", label: "Logarithmic", desc: "Top scores pop more" },
    { value: "ordinal", label: "Ordinal", desc: "Rank-based intensity" },
  ];

  const aggregateOptions: { value: SessionAggregateMode; label: string; desc: string }[] = [
    { value: "max", label: "Max", desc: "Best match in session" },
    { value: "mean", label: "Mean", desc: "Average relevance" },
    { value: "sum", label: "Sum", desc: "Total relevance" },
  ];

  const keywordOptions: { value: KeywordHighlightMode; label: string; desc: string }[] = [
    { value: "spectrum", label: "Spectrum", desc: "Match top result color" },
    { value: "fixed-80", label: "80%", desc: "Fixed high intensity" },
    { value: "fixed-50", label: "50%", desc: "Fixed medium intensity" },
  ];

  return (
    <div className="relative" ref={dropdownRef}>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setIsOpen(!isOpen)}
        className="h-8 w-8 p-0"
        title="Scoring settings"
      >
        <Settings className="h-4 w-4" />
      </Button>

      {isOpen && (
        <div className="absolute right-0 top-10 z-50 w-72 rounded-lg border bg-popover p-4 shadow-lg">
          <h3 className="mb-3 font-semibold text-sm">Search Highlighting</h3>

          {/* Scale Mode */}
          <div className="mb-4">
            <label className="block text-xs text-muted-foreground mb-2">
              Score Scale
            </label>
            <div className="flex gap-1">
              {scaleOptions.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => updateSettings({ scaleMode: opt.value })}
                  className={`flex-1 rounded px-2 py-1.5 text-xs transition-colors ${
                    settings.scaleMode === opt.value
                      ? "bg-primary text-primary-foreground"
                      : "bg-muted hover:bg-muted/80"
                  }`}
                  title={opt.desc}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {/* Session Aggregate */}
          <div className="mb-4">
            <label className="block text-xs text-muted-foreground mb-2">
              Session Score
            </label>
            <div className="flex gap-1">
              {aggregateOptions.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => updateSettings({ sessionAggregate: opt.value })}
                  className={`flex-1 rounded px-2 py-1.5 text-xs transition-colors ${
                    settings.sessionAggregate === opt.value
                      ? "bg-primary text-primary-foreground"
                      : "bg-muted hover:bg-muted/80"
                  }`}
                  title={opt.desc}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {/* Keyword Highlight */}
          <div>
            <label className="block text-xs text-muted-foreground mb-2">
              Keyword Highlight
            </label>
            <div className="flex gap-1">
              {keywordOptions.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => updateSettings({ keywordHighlight: opt.value })}
                  className={`flex-1 rounded px-2 py-1.5 text-xs transition-colors ${
                    settings.keywordHighlight === opt.value
                      ? "bg-primary text-primary-foreground"
                      : "bg-muted hover:bg-muted/80"
                  }`}
                  title={opt.desc}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {/* Preview - shows how selected scale mode transforms scores */}
          <div className="mt-4 pt-3 border-t">
            <label className="block text-xs text-muted-foreground mb-2">
              Preview
            </label>
            <div className="flex gap-1 h-6">
              {[0.2, 0.4, 0.6, 0.8, 1.0].map((score) => {
                const allScores = [0.2, 0.4, 0.6, 0.8, 1.0];
                const transformed = transformScore(score, settings.scaleMode, allScores);
                return (
                  <div
                    key={score}
                    className="flex-1 rounded text-[10px] flex items-center justify-center"
                    style={{ backgroundColor: scoreToBackground(transformed) }}
                  >
                    {Math.round(score * 100)}%
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
