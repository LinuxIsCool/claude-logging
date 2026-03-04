"use client";

import React, { createContext, useContext, useState, useEffect, ReactNode } from "react";

/**
 * Scoring Settings for Search Highlighting
 *
 * Controls how cosine similarity scores are transformed into visual feedback.
 */

export type ScaleMode = "linear" | "logarithmic" | "ordinal";
export type SessionAggregateMode = "max" | "mean" | "sum";
export type KeywordHighlightMode = "spectrum" | "fixed-80" | "fixed-50";

export interface ScoringSettings {
  /** How to scale scores: linear (1:1), logarithmic (emphasizes top), ordinal (rank-based) */
  scaleMode: ScaleMode;
  /** How to aggregate event scores into session scores */
  sessionAggregate: SessionAggregateMode;
  /** How to highlight keyword matches */
  keywordHighlight: KeywordHighlightMode;
}

const DEFAULT_SETTINGS: ScoringSettings = {
  scaleMode: "logarithmic",
  sessionAggregate: "max",
  keywordHighlight: "spectrum",
};

const STORAGE_KEY = "logging-scoring-settings";

interface ScoringSettingsContextValue {
  settings: ScoringSettings;
  updateSettings: (updates: Partial<ScoringSettings>) => void;
}

const ScoringSettingsContext = createContext<ScoringSettingsContextValue | null>(null);

export function ScoringSettingsProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<ScoringSettings>(DEFAULT_SETTINGS);
  const [isLoaded, setIsLoaded] = useState(false);

  // Load settings from localStorage on mount
  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        setSettings({ ...DEFAULT_SETTINGS, ...parsed });
      }
    } catch (e) {
      console.error("Failed to load scoring settings:", e);
    }
    setIsLoaded(true);
  }, []);

  // Persist settings to localStorage (debounced to avoid excessive writes)
  useEffect(() => {
    if (!isLoaded) return;

    const timer = setTimeout(() => {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
      } catch (e) {
        console.error("Failed to save scoring settings:", e);
      }
    }, 300);

    return () => clearTimeout(timer);
  }, [settings, isLoaded]);

  const updateSettings = (updates: Partial<ScoringSettings>) => {
    setSettings((prev) => ({ ...prev, ...updates }));
  };

  return (
    <ScoringSettingsContext.Provider value={{ settings, updateSettings }}>
      {children}
    </ScoringSettingsContext.Provider>
  );
}

export function useScoringSettings(): ScoringSettingsContextValue {
  const context = useContext(ScoringSettingsContext);
  if (!context) {
    throw new Error("useScoringSettings must be used within ScoringSettingsProvider");
  }
  return context;
}

/**
 * Color calculation functions for similarity highlighting.
 */

/**
 * Transform a score based on scale mode.
 *
 * @param score - Raw cosine similarity (0-1)
 * @param mode - Scale mode
 * @param allScores - All scores in result set (needed for ordinal mode)
 * @returns Transformed score (0-1)
 */
export function transformScore(
  score: number,
  mode: ScaleMode,
  allScores?: number[]
): number {
  if (score <= 0) return 0;
  if (score >= 1) return 1;

  switch (mode) {
    case "linear":
      return score;

    case "logarithmic":
      // Log scale: emphasizes differences at the top
      // Map (0,1] to (0,1] using log transform
      // Using log10(x*9+1)/log10(10) = log10(x*9+1) gives nice curve
      return Math.log10(score * 9 + 1);

    case "ordinal":
      // Rank-based: position in sorted list determines intensity
      // Edge case: with only one result, fall back to linear (single result shouldn't
      // automatically get max intensity just because it's "first")
      if (!allScores || allScores.length === 0) return score;
      if (allScores.length === 1) return score; // Fall back to linear for single result

      // Sort descending, find position
      const sorted = [...allScores].sort((a, b) => b - a);
      const rank = sorted.indexOf(score);
      if (rank === -1) return score;

      // Best rank = 1.0, worst = 0.0
      // With N items: rank 0 → 1.0, rank N-1 → 0.0
      return 1 - rank / (sorted.length - 1);

    default:
      return score;
  }
}

/**
 * Convert transformed score to green-spectrum background color.
 *
 * @param transformedScore - Score after transformation (0-1)
 * @returns CSS color string (hsla)
 */
export function scoreToBackground(transformedScore: number): string {
  if (transformedScore <= 0) return "transparent";

  // Clamp to [0, 1]
  const t = Math.min(Math.max(transformedScore, 0), 1);

  // Green spectrum using HSL
  // H = 120 (green)
  // S = 30% → 70% (more saturated = more relevant)
  // L = 85% → 50% (darker = more relevant)
  // A = 0.1 → 0.5 (more opaque = more relevant)
  const saturation = 30 + t * 40;
  const lightness = 85 - t * 35;
  const alpha = 0.1 + t * 0.4;

  return `hsla(120, ${saturation}%, ${lightness}%, ${alpha})`;
}

/**
 * Format score as percentage for display.
 */
export function formatScorePercent(score: number): string {
  if (score <= 0) return "";
  return `${Math.round(score * 100)}%`;
}

/**
 * Calculate session score from event scores.
 */
export function calculateSessionScore(
  eventScores: number[],
  mode: SessionAggregateMode
): number {
  if (eventScores.length === 0) return 0;

  switch (mode) {
    case "max":
      return Math.max(...eventScores);
    case "mean":
      return eventScores.reduce((a, b) => a + b, 0) / eventScores.length;
    case "sum":
      // Normalize sum to avoid scores > 1
      return Math.min(eventScores.reduce((a, b) => a + b, 0), 1);
    default:
      return Math.max(...eventScores);
  }
}

/**
 * Get keyword highlight color based on settings and max score in results.
 */
export function getKeywordHighlightColor(
  mode: KeywordHighlightMode,
  maxScore: number
): string {
  switch (mode) {
    case "spectrum":
      // Use the max score's color
      return scoreToBackground(maxScore);
    case "fixed-80":
      return scoreToBackground(0.8);
    case "fixed-50":
      return scoreToBackground(0.5);
    default:
      return scoreToBackground(0.8);
  }
}
