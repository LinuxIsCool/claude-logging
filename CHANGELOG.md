# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-04-03

### Added
- All 25 Claude Code hook event types captured
- Hybrid search (FTS5 keyword + optional semantic) with RRF fusion
- Cross-platform support (Linux, macOS, Windows)
- REST API with FastAPI (search, sessions, stats, images, SSE streaming)
- Web interface for visual session browsing (experimental)
- Full subagent transcript capture and indexing
- Lightweight entity extraction (Person, Date, Money, Duration)
- Configurable heartbeat monitoring
- GitHub Actions CI (Linux, macOS, Windows; Python 3.10-3.13)
- Comprehensive test suite (173 tests)

### Security
- FTS5 query sanitization (prevents syntax injection)
- Path traversal protection on image serving
- Thread-safe SQLite with write locking

### Fixed
- TOCTOU race condition in image update
- False-positive agent session counting (JSON parsing vs string matching)
- Schema evolution crash on unknown fields
- Windows ImportError on fcntl
- sqlite-vec embedding storage type (bytes, not list)
- Partial JSONL line crash during concurrent sync

### Removed
- Hardcoded system-specific paths
- Dead `get_suggestions()` method with SQL injection risk
- Internal planning documents
