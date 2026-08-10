# Claude Web → Sessions projection boundary

`claude-claude-web` owns source custody, export versioning, schema validation,
KOI bundle identity, and re-ingestion of claude.ai data. `claude-logging` must
not copy or mutate that source store.

The shared Sessions application may consume a read-only projection with this
mapping:

| Claude Web concept | Sessions concept |
|---|---|
| conversation UUID | source-native session ID |
| conversation | session (`runtime=claude-web`, `source_kind=archive`) |
| human chat message | `UserPromptSubmit` canonical message |
| assistant chat message | `AssistantResponse` canonical message |
| message UUID/index | source-native event/turn identity |
| conversation project UUID | external project relationship |
| attachment/file | artifact relationship |
| design chat | specialized archived session with design metadata |

Memories, user/account records, and project definitions remain visible through
their source plugin and KOI namespaces; they are not logging sessions.

## Projection requirements

1. Read KOI `legion.claude-web.conversation` bundles through a source-owned API
   or accessor. Do not query private PostgreSQL table internals from logging.
2. Use deterministic IDs derived from source RID + message identity.
3. Retain the source RID on every projected session/event.
4. Mark all projected records `source_kind=archive`; never emit live status.
5. Projection is idempotent and rebuildable. Claude Web remains authoritative.
6. Unknown export schema stops in `claude-claude-web` before projection.
7. Session search may federate results, but deletion/correction routes back to
   the owning Claude Web record and export lineage.

## Required source-owned interface

The next implementation seam should be a paginated, read-only conversation
accessor supplied by `claude-claude-web`:

```text
list_conversations(cursor, limit, updated_after)
get_conversation(rid)
```

The returned object must include the RID, native UUID, title, timestamps,
project reference, ordered messages, attachments, and export/schema revision.
Logging can then adapt that public object to `legion.logging.event.v1` without
acquiring write access to the Claude Web database.
