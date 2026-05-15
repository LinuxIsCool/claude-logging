"use client";

import React, { createContext, useContext, ReactNode, useMemo, useCallback } from "react";

/**
 * Search Context for propagating search query to deeply nested components.
 *
 * This enables keyword highlighting without prop drilling through multiple
 * component layers (SessionList → EventsTimeline → EventCard → text content).
 */

interface SearchContextValue {
  /** The current search query string */
  query: string;
  /**
   * Highlight matching words in text.
   * Returns array of React nodes with highlighted <mark> spans.
   * Only highlights when there's an active query.
   */
  highlightText: (text: string) => ReactNode[];
}

const SearchContext = createContext<SearchContextValue | null>(null);

/**
 * Escape regex special characters in a string.
 */
function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Split text by matching words and return React nodes with highlights.
 *
 * @param text - The text to process
 * @param query - Space-separated search terms
 * @returns Array of React nodes (strings and <mark> elements)
 */
function createHighlightedNodes(text: string, query: string): ReactNode[] {
  if (!query.trim() || !text) {
    return [text];
  }

  // Split query into individual words and escape for regex
  // Limit words to prevent regex DoS with very long queries
  const MAX_SEARCH_WORDS = 20;
  const words = query.toLowerCase().split(/\s+/).filter(Boolean).slice(0, MAX_SEARCH_WORDS);
  if (words.length === 0) {
    return [text];
  }

  // Create regex pattern matching any of the words (case-insensitive)
  const pattern = new RegExp(
    `(${words.map(escapeRegex).join("|")})`,
    "gi"
  );

  // Split text by matches, keeping the delimiters
  const parts = text.split(pattern);

  // Convert to React nodes, wrapping matches in <mark>
  return parts.map((part, index) => {
    // Check if this part matches any search word
    const isMatch = words.some(
      (word) => part.toLowerCase() === word.toLowerCase()
    );

    if (isMatch) {
      return (
        <mark key={index} className="search-highlight">
          {part}
        </mark>
      );
    }

    return part;
  });
}

interface SearchProviderProps {
  query: string;
  children: ReactNode;
}

/**
 * Provider component that makes search query available to all child components.
 *
 * @example
 * ```tsx
 * <SearchProvider query={searchQuery}>
 *   <EventsTimeline events={events} />
 * </SearchProvider>
 * ```
 */
export function SearchProvider({ query, children }: SearchProviderProps) {
  // Memoize the highlight function to prevent unnecessary re-renders
  const highlightText = useCallback(
    (text: string): ReactNode[] => createHighlightedNodes(text, query),
    [query]
  );

  const value = useMemo(
    () => ({ query, highlightText }),
    [query, highlightText]
  );

  return (
    <SearchContext.Provider value={value}>
      {children}
    </SearchContext.Provider>
  );
}

/**
 * Hook to access search context in child components.
 *
 * @throws Error if used outside SearchProvider
 *
 * @example
 * ```tsx
 * function EventCard({ isMatch }: { isMatch: boolean }) {
 *   const { query, highlightText } = useSearch();
 *
 *   if (isMatch && query) {
 *     return <>{highlightText(content)}</>;
 *   }
 *   return <>{content}</>;
 * }
 * ```
 */
export function useSearch(): SearchContextValue {
  const context = useContext(SearchContext);
  if (!context) {
    throw new Error("useSearch must be used within a SearchProvider");
  }
  return context;
}

/**
 * Hook that returns memoized ReactMarkdown components with highlighting support.
 *
 * This hook extracts the duplicate markdown component logic that was previously
 * in both EventCard and SubagentCard into a single reusable hook.
 *
 * @param isMatch - Whether this content is from a search match
 * @returns Custom ReactMarkdown components object, or undefined if no highlighting needed
 *
 * @example
 * ```tsx
 * const markdownComponents = useMarkdownHighlighting(isMatch);
 * <ReactMarkdown components={markdownComponents}>{content}</ReactMarkdown>
 * ```
 */
export function useMarkdownHighlighting(isMatch: boolean) {
  const { query, highlightText } = useSearch();

  return useMemo(() => {
    if (!isMatch || !query.trim()) {
      return undefined; // Use default rendering when not highlighting
    }

    // Helper to recursively process children and highlight text
    const processChildren = (children: React.ReactNode): React.ReactNode => {
      if (typeof children === "string") {
        return highlightText(children);
      }
      if (Array.isArray(children)) {
        // Use highlightText directly - it already returns keyed elements
        return children.flatMap((child) =>
          typeof child === "string" ? highlightText(child) : child
        );
      }
      return children;
    };

    return {
      // Custom paragraph renderer
      p: ({ children, ...props }: React.ComponentPropsWithoutRef<"p">) => (
        <p {...props}>{processChildren(children)}</p>
      ),
      // Custom list item renderer
      li: ({ children, ...props }: React.ComponentPropsWithoutRef<"li">) => (
        <li {...props}>{processChildren(children)}</li>
      ),
      // Strong/bold text
      strong: ({ children, ...props }: React.ComponentPropsWithoutRef<"strong">) => (
        <strong {...props}>{processChildren(children)}</strong>
      ),
      // Emphasis/italic
      em: ({ children, ...props }: React.ComponentPropsWithoutRef<"em">) => (
        <em {...props}>{processChildren(children)}</em>
      ),
    };
  }, [isMatch, query, highlightText]);
}
