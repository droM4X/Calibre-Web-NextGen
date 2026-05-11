# Kobo policy: `common_filters` on `get_book_by_uuid` paths

Source audit: `notes/KOBO-AUDIT-2026-05-10.md` § B4. Decision date:
2026-05-11. Decision owner: new-usemame.

## The question

`calibre_db.get_book_by_uuid(book_uuid)` bypasses `common_filters`, so
the Kobo blueprint can hand back books that the user's web UI wouldn't
show them. Five call sites in `cps/kobo.py`.

`common_filters` (cps/db.py:940) enforces:

1. **archived** (`ArchivedBook`) — books the user has hidden from sync.
   *On the Kobo path, this table doubles as the device's "I deleted this
   locally" track.* So Kobo paths must always pass
   `allow_show_archived=True`. The Kobo sync handler at line 255/274
   already does this.
2. **hidden** (`UserHiddenBook`) — per-user declutter (NextGen issue #64).
3. **language** — user's preferred language filter.
4. **denied tags** — admin-set per-user denied tag list.
5. **allowed tags** — admin-set per-user allowed tag list (positive ACL).
6. **restricted custom column** — admin-set per-user custom-column ACL.

## Policy framework

The Kobo endpoints fall into two categories:

**Policy boundary endpoints** — surfaces where the user's web policy
(hidden / denied / language / cc-ACL) *should* propagate to the Kobo
side. Symmetric with what they'd see in the browser. ENFORCE filters.

**Device-trailing endpoints** — operations on books already on the
device. The sync push (`HandleSyncRequest`) is the policy boundary for
the device — it decides what reaches the Kobo. Once a book is on the
device, the user must be able to manage their state and clean up
without 4xx loops. DO NOT enforce filters.

## Per-callsite decisions

### Line 469 — `HandleMetadataRequest` (GET /v1/library/<uuid>/metadata)

**ENFORCE filters.** Returns Kobo metadata (title, authors, series,
cover URL, format URL) for a book. If the user can't see the book in
their web UI (denied-tagged, hidden, wrong language, cc-ACL out), they
shouldn't get its metadata via Kobo either. The device falls back to
cached metadata gracefully on 404.

This is symmetric with what `HandleSyncRequest` already filters on the
push side. If filters change between syncs, the metadata endpoint
hardens the boundary.

### Line 750 — `add_items_to_shelf` (POST /v1/library/tags/<tag_id>/items)

**ENFORCE filters.** The device is asking us to add a book to a Kobo
shelf (a CW shelf marked `kobo_sync=True`). Adding a book the user
isn't permitted to see is a policy violation — the resulting shelf
would then surface that book on every other Kobo synced from the same
account. We return the book as "unknown to calibre" via the existing
`items_unknown_to_calibre` path, which the device handles silently.

### Line 822 — `HandleTagRemoveItem` (POST /v1/library/tags/<tag_id>/items/delete)

**DO NOT enforce filters.** User-initiated cleanup. The user is telling
the Kobo to remove a book from a shelf. Blocking this on policy
grounds would leave the book on the shelf forever — even after the
underlying book was hidden / denied / archived. Destructive,
user-initiated operations must never be blocked by policy filters.

### Line 949 — `HandleStateRequest` (GET/PUT /v1/library/<uuid>/state)

**DO NOT enforce filters.** Reading-state sync for a book already on
the device. If a user finishes a book that later becomes
denied-tagged, the Kobo's reading position MUST still sync (otherwise
the device retries forever, fills its sync queue, and the user sees a
visible failure). This is the canonical device-trailing endpoint.

### Line 1244 — `HandleBookDeletionRequest` (DELETE /v1/library/<uuid>)

**DO NOT enforce filters.** User-initiated deletion. Same logic as the
shelf-remove case: cleanup ops never block. The Kobo's delete handler
either archives the book (per-user) or just removes it from sync —
both are net-positive even when the book is otherwise denied.

## Summary table

| Line | Endpoint | Enforce filters? | Reason |
|------|----------|------------------|--------|
| 469  | metadata GET | YES | Policy boundary. Symmetric with web/OPDS. |
| 750  | shelf-add | YES | Policy boundary. Adding denied books to shelves leaks them. |
| 822  | shelf-remove | NO | Destructive, user-initiated. Never block. |
| 949  | state GET/PUT | NO | Device-trailing. Required for sync to keep working. |
| 1244 | book DELETE | NO | Destructive, user-initiated. Never block. |

## Implementation

`cps/db.py` gets a new helper:

```python
def get_book_by_uuid_for_kobo(self, book_uuid, *, enforce_policy):
    """Return Books row by uuid. When enforce_policy=True, applies
    common_filters (with allow_show_archived=True; see KOBO-B4 doc)."""
    self.ensure_session()
    q = self.session.query(Books).filter(Books.uuid == book_uuid)
    if enforce_policy:
        q = q.filter(self.common_filters(allow_show_archived=True))
    return q.first()
```

The five Kobo callsites switch to this helper with an explicit
`enforce_policy=` kwarg. The kwarg-only signature forces the caller to
state which policy applies, so future maintainers can't accidentally
get the wrong default.

`allow_show_archived=True` because `ArchivedBook` is reused on Kobo paths
as the "device-deleted" semantic. Filtering by it here would 404 every
book the device archived locally, breaking the device.

## What this doesn't cover

- The sync push (`HandleSyncRequest`). Already uses `common_filters`
  correctly at lines 255 and 274.
- Cover endpoints (cover, image-id, thumbnails). They take book_id,
  not uuid, and route through the cover cache which already enforces
  permissions.
- Notebook-sync POST. Has its own auth/permission flow.
- `download_required` decorator on metadata. Adds a download-permission
  check on top of the filter — that's complementary, not redundant.
