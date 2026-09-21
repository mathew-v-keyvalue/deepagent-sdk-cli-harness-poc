# v2: Wiring Tracy (frontend → backend → AI service)

Status: **implemented, live-verified, ready to push** (this is "Part 1" —
the wiring. Evals/Netra integration is a separate, later piece of work, on
its own branch).

This folder is the single authoritative documentation for v2 — how the
Tracy chat widget in morpheus_fe reaches this AI service through
morpheus_backend, and how it authenticates all the way down to the real
`cybersierra` CLI, across all three repos. It exists here (in the AI
service repo), not scattered across the other two repos' own wikis,
because this is where the most complex and most security-sensitive part of
the work happened, and because keeping one authoritative copy avoids three
docs quietly drifting out of sync with each other.

## Read these in order

1. **[architecture.md](architecture.md)** — the full request path, one
   diagram, all three repos. Start here if you want the big picture in two
   minutes.
2. **[auth-flow.md](auth-flow.md)** — the complete auth story: two auth
   layers, how a user's token becomes the CLI's identity, the two real bugs
   found and fixed (wrong env var name, missing base URL), and the sandbox
   hardening that stops the model from ever touching auth itself. This is
   the most detailed and most important document in this folder.
3. **[session-handling.md](session-handling.md)** — who mints the session
   ID, how it flows through all three services, and the one code change
   needed in this repo to support it.
4. **[frontend-changes.md](frontend-changes.md)** — exactly what changed in
   morpheus_fe, file by file.
5. **[backend-changes.md](backend-changes.md)** — exactly what changed in
   morpheus_backend, file by file (the new `tracy` module).
6. **[ai-service-changes.md](ai-service-changes.md)** — exactly what
   changed in this repo, file by file.
7. **[deployment-config.md](deployment-config.md)** — every env var needed,
   in all three repos, to actually run this end to end.
8. **[known-limitations.md](known-limitations.md)** — what v2 deliberately
   does not do yet, and what's already been flagged for follow-up.

## The one-paragraph version

Tracy no longer calls this AI service directly from the browser. It now
goes: browser → morpheus_backend's new `POST /tracy/chat` → this
service's `POST /chat` → a sandboxed `cybersierra` CLI subprocess → the
real cybersierra/morpheus-api backend. Two independent auth checks happen
along the way: a shared service-to-service secret (proves the caller is
morpheus_backend), and the actual end user's own JWT, forwarded unchanged
and used to make the CLI subprocess run as that real user — not as
whichever identity happens to be logged in on the server's disk, if any.
The whole point of this section: **the end user's only login, ever, is
logging into the CyberSierra platform once** — everything downstream is
silent, automatic, and works even on a server that's never had a human
manually log in on it.
