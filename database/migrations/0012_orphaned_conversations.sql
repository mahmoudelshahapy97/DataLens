-- Clean up conversations that were created without an owner.
--
-- `bind_data_source` pins a thread to one database, and it runs before the agent
-- has written anything -- so it inserts the row itself. It inserted
-- `user_id = ''`, and `_write`'s conflict predicate requires the owner to match,
-- so from that point on every transcript update for that thread was silently
-- discarded. The row was also invisible to `summaries`, `get_conversation` and
-- `delete_conversation`, all of which filter on the owner: a conversation nobody
-- could read, list, or delete.
--
-- The client sends a data source on the first message of every new thread once a
-- workspace has more than one database registered, so on such a workspace this
-- was every conversation started since that feature shipped.
--
-- The code fix is two parts, both already in place:
--   * `bind_data_source` records the caller, so new rows are owned from the start.
--   * `_write` adopts a `user_id = ''` row instead of rejecting it, so a thread
--     orphaned by the old behaviour recovers on its next turn.
--
-- That leaves the rows whose transcript was already lost. They have no owner and
-- an empty document, so there is nothing in them to recover and nobody they could
-- be shown to.
--
-- Deliberately narrow: `document` must still be the empty placeholder. A row with
-- an owner, or with messages in it, is left alone -- the adoption rule above will
-- claim it on the next turn, and destroying a transcript to tidy up a bug would be
-- a worse outcome than the bug.

DELETE FROM vanna_app.conversations
 WHERE user_id = ''
   AND coalesce(document->'messages', '[]'::jsonb) = '[]'::jsonb;
