# Deployment config — every env var, in all three repos

## deepagent-sdk-cli-poc (this repo)

| Var | Required for v2? | Value | Notes |
|---|---|---|---|
| `DEEPAGENT_SERVICE_AUTH` | Yes | any sufficiently random shared secret | Must exactly match morpheus_backend's own value of the same name. Generate with e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `CYBERSIERRA_INJECT_ACCESS_TOKEN` | Yes | `1` | Off is the wrong setting for this integration. |
| `CYBERSIERRA_BASE_URL` | Yes | the real backend this deployment targets | Unconditional, deployment-wide — translated to `MORPHEUS_BASE_URL` for the CLI subprocess. Local dev against a local morpheus_backend: `http://localhost:8080`. Real deployment: the actual `morpheus-api.{env}.cybersierra.ai` URL. |
| `FRONTEND_ORIGINS` | No | — | Only matters for the bundled `frontend/index.html` test page now; Tracy no longer calls this service directly from the browser. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Yes (pre-existing) | real key | Unrelated to v2, but required for the service to function at all. |
| `NETRA_TRACING`, `NETRA_API_KEY`, etc. | No | — | Unrelated to v2; pre-existing observability config. |

Everything else in `.env.example` is unrelated to v2.

## morpheus_backend

| Var | Required for v2? | Value | Notes |
|---|---|---|---|
| `DEEPAGENT_URL` | Yes | base URL of the AI service | Local dev: `http://localhost:8010` (chosen to avoid colliding with morpheus_fe's own dev server, which defaults to port 8000). |
| `DEEPAGENT_SERVICE_AUTH` | Yes | same value as above | Must exactly match. |

Nothing else needed — the `tracy` module reuses the existing FusionAuth
JWT validation (`verifyToken`) already wired into every other route.

## morpheus_fe

No new env vars. `REACT_APP_API_URL` (already existing, already correct)
is all that's needed — Tracy now calls `${API_URL}/tracy/chat`, same base
URL every other API call in the app already uses.
`REACT_APP_API_DEEPAGENT_URL` was removed as dead config (see
[frontend-changes.md](frontend-changes.md)) — if you still see it
referenced anywhere, that's stale.

## Local dev port map (as configured for this v2 work)

| Service | Port | Why |
|---|---|---|
| morpheus_fe dev server | 8000 | Umi's own default |
| morpheus_backend | 8080 | Existing `APP_PORT` |
| deepagent-sdk-cli-poc | 8010 | Chosen to avoid colliding with morpheus_fe's 8000 |

## One operational gotcha worth repeating here

morpheus_backend's dev process (`nodemon --watch 'dist/**/*'`) watches
**compiled output**, not TypeScript source. After pulling this branch (or
any change to `morpheus/src/modules/tracy/`), run `npm run build` before
expecting the route to be live — a bare restart of an already-running
`nodemon` process will not pick up new source files that were never
compiled.
