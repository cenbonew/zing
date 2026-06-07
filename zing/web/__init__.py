"""Local web UI for zing (`zing serve`).

A thin, *local-first* layer over the same audit engine the CLI uses: it serves a
single-page app and streams live audit progress over SSE. Keys entered in the
browser are sent only to your own localhost server and used to reach the target
relay — they are never sent anywhere else, matching zing's CLI trust model.

The web extra is optional; install with ``pip install zing-audit[web]``.
"""
