"use client";

import { useSearch } from "@/lib/search-context";

interface HighlightedTextProps {
  /** The text content to potentially highlight */
  text: string;
  /** Whether this text belongs to a matching search result */
  isMatch: boolean;
  /** Additional CSS classes for the container */
  className?: string;
}

/**
 * Component that renders text with search keyword highlighting.
 *
 * Only applies highlighting when:
 * 1. The `isMatch` prop is true (this is a search result match)
 * 2. There's an active search query in context
 *
 * @example
 * ```tsx
 * <HighlightedText
 *   text={userPrompt}
 *   isMatch={matchingEventIds.has(event.id)}
 *   className="text-sm whitespace-pre-wrap"
 * />
 * ```
 */
export function HighlightedText({ text, isMatch, className }: HighlightedTextProps) {
  const { query, highlightText } = useSearch();

  // Only highlight if this is a matching event AND there's an active query
  const shouldHighlight = isMatch && query.trim().length > 0;

  if (shouldHighlight) {
    return (
      <span className={className}>
        {highlightText(text)}
      </span>
    );
  }

  // No highlighting needed - render plain text
  return <span className={className}>{text}</span>;
}
