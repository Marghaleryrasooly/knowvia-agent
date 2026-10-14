# Administrator knowledge management

Use `/admin` with an existing privately provisioned administrator account.
Employee chat, login and action confirmation are unchanged. There is no public
administrator registration or browser-controlled authority.

## Safe workflow

1. Upload a UTF-8 `.md` file with one `#` title and nonempty `##` sections.
   Uploading the same filename stages a replacement.
2. Select its existing HR, IT or finance domain and **explicit reading roles**.
   No roles are preselected. A role must also grant that domain's read permission.
   Administrator status alone does not grant knowledge access.
3. Review the staged listing. Removal is staged too, not immediately live.
4. Publish: the existing parser, chunker and configured MiniLM embedder build a
   separate runtime index. Model, dimensions, finite normalized vectors, chunk
   identity/content and source coverage are validated before publication.
5. Discard unwanted staged changes, or roll back to the previous working version.
   Rollback requires no outstanding draft. After the first publication, rollback
   restores the untouched reference corpus/index.

Requests require the existing authenticated session, server-side administrative
permission and same-origin JSON mutation checks. Bodies and corpus size are
bounded. Filesystem paths cannot be supplied by the browser. Uploaded Markdown is
not rendered as executable HTML in the dashboard.

## Persistence and publication

Draft content, active/previous version pointers and sanitized activity events live
in the existing runtime `authority.db`. Immutable corpus/index/manifest versions
live under `KNOWVIA_DATA_DIR/knowledge/versions/`. The publication pointer and audit
event switch in one SQLite transaction after prepared files are flushed.
Conflicting draft edits or competing publications reject stale work. Failed
publication preserves the previous active version and the draft.

Each chat request captures an index/source-ACL snapshot. Publication or rollback
does not replace an in-flight request's files. The next request uses the newly
active version and current trusted role assignments. Restricted documents never
enter evidence evaluation or generation for an unauthorized role.

The bundled `knowvia_m1_dataset/` and `data/knowvia_index.json` are never overwritten.
Reference tests and historical evaluation artifacts remain immutable. Runtime
versions and the authority database are private state, excluded from Git.

## Operational limits

- Maximum upload: 128 KiB UTF-8; corpus: 2 MiB / 100 documents / 1,000 chunks.
- Publish uses the existing configured embedding provider. Install/cache its
  existing model assets before publication; no hosted inference is needed.
- Keep the complete runtime directory on durable local storage, with consistent
  backups of SQLite and version files. This is not an object-store or serverless
  database design.
- Versions (including unreferenced failed preparations) are retained, not
  automatically deleted. Monitor disk usage; retention/garbage collection is
  deferred. Do not delete snapshots serving active requests.
- A model's semantic judgments still require separate live acceptance. Uploaded
  policies are trusted administrator content, not automatically verified truth.
