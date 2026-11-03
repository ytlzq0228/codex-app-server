# Request context archival

The archive job runs only on a configured application host with a verified disk UUID.
Install the host launcher and archiver together outside the application release directory.
Use a daily systemd timer with `Persistent=false`; do not install it on standby nodes.
A missed run is skipped. The next scheduled run includes all remaining eligible records.

The cutoff is a rolling 14 days measured by PostgreSQL time. Only the request JSON
keys `input`, `messages`, `instructions`, `system`, `contents`, and
`systemInstruction` are moved out. All other parameters, including model, tools,
metadata, generation settings, and previous response ID, remain in the database.
Response text, requester identity, request IDs, billing, observation, correlation,
and history fingerprints are untouched. The original request JSON is archived
losslessly so its context can be reconstructed.

Each gzip archive is published atomically, read back and SHA-256 checked, and synced
before the row is updated. Row locking and a transaction couple the JSON projection
and archive index entry. A session advisory lock prevents concurrent jobs. A failed
write leaves the row intact; a failed DB transaction may leave a reusable archive
file. Archive files are not automatically deleted. Stop on low disk space (10 GiB
reserve). Monitor capacity independently.

The launcher requires a private JSON configuration containing `hostname`, `node_id`,
`gateway`, `mount`, and the filesystem `uuid`. It reuses the running gateway image
and Docker network in a resource-limited one-shot container. Credentials are passed
through the environment without printing or writing them to files.

Commands (run the host launcher as a Docker-authorized administrator):
- `python3 run_context_archive.py --config <private-config> --initialize`: create index table.
- `python3 run_context_archive.py --config <private-config> --dry-run`: eligible row count.
- `python3 run_context_archive.py --config <private-config>`: archive eligible records.
- `python3 run_context_archive.py --config <private-config> --read <request-id>`:
  retrieve verified original request JSON to standard output; it contains private content.

Existing history and billing pages keep working. Archived request JSON contains an
explicit `_gateway_context_archive` reference in place of the moved fields.
Historical context is retrieved by an administrator using the command above;
automatic retrieval through the web UI is not provided by this standalone job.
If the archive host is unavailable, old context cannot be retrieved until it returns.
Do not interpret missing archived input as an originally empty request.

Stopping/disabling the timer stops new archival. Archived data stays on disk.
Do not roll back by deleting the archive index or disk contents.
Normal PostgreSQL vacuum makes cleared space reusable; it does not guarantee
immediate filesystem shrinkage. No blocking table rewrite is scheduled.

Validation covers projection, lossless Unicode/NUL-escape storage, repeated runs,
checksum/identity checks, corruption, and filesystem failure. The PostgreSQL
integration verification uses connection-local temporary tables to verify the
14-day boundary, preserved accounting fields, retry idempotence, and rollback.
