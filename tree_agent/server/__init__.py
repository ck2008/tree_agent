"""Server-side storage for a shared Tree Agent workspace.

A single service host owns one SQLite database on its own local disk and is the
only process that opens it. Desktop clients talk to this package over HTTP; they
never see the `.db` file. See `docs/sqlite-storage-implementation-spec.md`.
"""
