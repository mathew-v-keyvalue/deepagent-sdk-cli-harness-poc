# cybersierra CLI — full command reference

Every command the live `cybersierra` manifest exposes right now (`cybersierra
manifest`, manifest version `367f09699890`, 37 commands), cross-checked
against the actual source of truth for that manifest —
`morpheus_backend/morpheus/src/cli/manifest/agent-manifest.ts` — which
carries gotcha comments the live manifest's `description` field doesn't
surface at all.

**Note on the "Known traps for an agent" section below**: it was originally
written from source-code reading alone, before `logs/harness.log` from an
actual `dataset/run_dataset.py` run was available to check against. Once
that log evidence existed, the real dominant causes of the 8–15-`execute`-call
runs turned out to be different from what source-reading predicted — see the
section itself for the corrected, log-confirmed diagnosis and what was fixed
(`harness/agent.py`'s `recursion_limit` and `SYSTEM_PROMPT_APPENDIX`,
`harness/sandbox.py`'s auth-command denylist). The CLI facts below (POST vs
GET, the flag-naming inconsistency) are still true and still worth knowing —
they just weren't what broke these particular runs.

`SAFE` = read-only (`safe:true`, in `dataset/query_dataset.json`). `WRITE` =
mutates real data (`safe:false`, deliberately excluded from the dataset).

## health

| Command | Method/Path | Params | What it does |
|---|---|---|---|
| `health status get` | `GET /health` | — | Backend liveness check. |

## permissions

| Command | Method/Path | Params | What it does |
|---|---|---|---|
| `permissions policies list` | `GET /permissions/policies` | `subject*` | List permission policies for a subject. `subject` isn't documented as "userId" or "role name" anywhere in the manifest — the CLI/backend accepts both, so an agent has to ask or infer which one it has. |

## notifications

| Command | Method/Path | Params | What it does |
|---|---|---|---|
| `notifications feed list` | `POST /notifications/list` | `body` | List in-app notifications for the caller. **POST, not GET** — a plausible wrong first guess. Filters *and* paging both go inside `--data` as one JSON object, and paging is a **nested** `{page, limit}` object (both required if you page at all, limit max 1000) that **defaults to 10** if omitted — an unpaged call silently returns only 10 rows. Always check `meta.pagination.totalPage`. Example body: `{"eventTypes":["assessment.question_audited_using_ai_analyst"],"startDate":"2026-05-01T00:00:00Z","pagination":{"page":1,"limit":1000}}`. |
| `notifications feed unread-count` | `GET /notifications/unread-count` | — | Unread notification count for the caller. |

## user-management

| Command | Method/Path | Params | What it does |
|---|---|---|---|
| `user-management me get` | `GET /api/v1/user_management/user_info` | — | Identity and tenant context of the authenticated caller. |
| `user-management users list` | `GET /api/v1/user_management/get_users` | — | List users in your tenant (userId, email, name, roles). Exists specifically to resolve a human-readable username into the userId every actor-scoped query (activity logs, permissions subject, etc.) actually needs. |
| `user-management user-groups list` | `GET /api/v1/user_management/user-groups` | `page`, `limit`, `search` | List user groups in your tenant, paginated and searchable by name. |

## tprm — assessments

| Command | Method/Path | Params | What it does |
|---|---|---|---|
| `tprm assessments list` | `GET /tprm/assessments` | many optional filters (`statuses`, `assesseeId`, `templateNames`, `sort`, `limit*`, `page*`, `submissionType*`, ...) | List third-party risk assessments visible to you. |
| `tprm assessments get` | `GET /tprm/assessments/{id}` | `id*` + the same filter set as `list` | Full detail of one assessment by ID — includes real status fields (`status`, `vendorStatus`, `enterpriseStatus`, `progress`). |
| `tprm assessments comparison` | `GET /tprm/assessments/comparison` | `ids*` | Compare multiple assessments side by side. |
| `tprm assessments comments` | `GET /tprm/assessments/{id}/comments` | `keyword*`, `id*` | List comments on an assessment. |
| `tprm assessments verification` | `GET /tprm/assessments/{id}/verification` | `id*` + filter set | Verification detail (which questions were verified) for an assessment. |
| `tprm assessments send` **(WRITE)** | `POST /tprm/assessments/send` | `body` | Send assessment(s) to assessees. |
| `tprm assessments submit` **(WRITE)** | `PATCH /tprm/assessments/{id}/submit` | `id*`, `body` + filter set | Submit an assessment. |
| `tprm assessments update` **(WRITE)** | `PUT /tprm/assessments/{id}` | `id*`, `body` | Update an assessment's risk level and due date. |

## tprm — assessment drafts, questions, templates

| Command | Method/Path | Params | What it does |
|---|---|---|---|
| `tprm assessment-drafts list` | `GET /tprm/assessments/drafts` | — | List assessment drafts. |
| `tprm assessment-drafts get` | `GET /tprm/assessments/drafts/{id}` | `id*` | Detail of one assessment draft. |
| `tprm assessment-questions get` | `GET /tprm/assessments/{assessmentId}/questions/{questionId}` | `questionId*`, `assessmentId*`, `fileId*` | Detail of a single assessment question. Three required path-derived params — easy for an agent to under-supply. |
| `tprm assessment-templates list` | `GET /tprm/assessment-templates` | — | List assessment templates. |
| `tprm assessment-templates get` | `GET /tprm/assessment-templates/{id}` | `id*` | Detail of one assessment template. |

## tprm — assessees (the vendors/third parties being assessed)

| Command | Method/Path | Params | What it does |
|---|---|---|---|
| `tprm assessees list` | **`POST` `/tprm/assessees/list`** | `body` (optional `--data '{"page":1,"limit":20}'`) | List assessees. **Source comment (`agent-manifest.ts:133-135`): the `GET /tprm/assessees/list` variant 500s server-side (a filed backend bug) and its page default is wrong (30) — the POST variant is the only one that actually works and returns correct pagination meta.** A model guessing "list = GET" here gets a real 500, not a hint to try POST. |
| `tprm assessees get` | `GET /tprm/assessees/{id}` | `id*` | Full profile of a single assessee. |
| `tprm assessees count` | `GET /tprm/assessees/count` | — | Count of assessees in the tenant. |
| `tprm assessees comments` | `GET /tprm/assessees/{id}/comments` | `keyword`, `cursor`, `limit`, `id*` | Comments on an assessee. |
| `tprm assessees create` **(WRITE)** | `POST /tprm/assessees` | `body` | Add a single assessee. |
| `tprm assessee-drafts list` | `GET /tprm/assessees/drafts` | `keyword`, `limit*`, `page*`, `sort`, `filters` | List draft assessees (vendor records started but not finished). |
| `tprm assessee-versions list` | `GET /tprm/assessees/versions` | `keyword`, `limit*`, `page*`, `sort`, `filters` | List assessee version history. |
| `tprm assessee-tenant-users list` | `GET /tprm/assessee-tenant-users/{assesseeId}` | `assesseeId*` | Users belonging to an assessee's own tenant. Note the flag is `--assesseeId`, not `--id` — different from every other `assessee*` command above. |
| `tprm assessee-scans list` | `GET /tprm/assessees/{id}/scans` | `page*`, `limit*`, `keyword`, `sort`, `scanTypes`, `id*` | Security scans for one assessee, paginated/filterable by scan type. Flag here reverts to `--id` (not `--assesseeId`) even though the resource name is `assessee-scans` — the inconsistency with `assessee-tenant-users` above is a real trap. |

## tprm — activity logs

| Command | Method/Path | Params | What it does |
|---|---|---|---|
| `tprm activity-logs list` | **`POST` `/tprm/activity-logs/list`** | `body` | List assessee/assessment/document activity. Same POST-not-GET shape as `assessees list`. Filters *and* paging go inside one `--data` JSON object, e.g. `{"objectType":"assessment","actorId":"<userId>","startDate":"2026-05-01T00:00:00Z","page":1,"limit":100}`. Defaults to `limit:10` if you don't pass paging explicitly. |
| `tprm activity-logs filter-options` | `GET /tprm/activity-logs/filters/options` | `objectType*`, `objectId*` | Valid filter values (actors, object types) for the command above. Manifest description literally says "agents cannot guess these" — this is meant to be called *first* to discover valid `objectType`/`actorId` values before calling `activity-logs list` with a filter. |

## tprm — dashboard, risk scoring, vendor info

| Command | Method/Path | Params | What it does |
|---|---|---|---|
| `tprm dashboard get` | `GET /tprm/assessment-dashboard` | — | Assessments dashboard data (compliance posture overview). |
| `tprm dashboard-charts list` | `GET /tprm/assessment-dashboard/public-charts` | `fetchData*` | List saved public assessment charts; `--fetchData true` includes each chart's actual data. |
| `tprm dashboard-charts metadata` | `GET /tprm/assessment-dashboard/public-charts/metadata` | — | Columns/table relationships available for building a custom chart. |
| `tprm risk-score-config get-org` | `GET /tprm/risk-score-config/org` | — | Organization-level risk score configuration. |
| `tprm risk-score-config update-org` **(WRITE)** | `PATCH /tprm/risk-score-config/org` | `body` | Update organization-level risk score configuration. |
| `tprm vendor-info get` | `GET /tprm/vendor-info` | — | Vendor info for the current authenticated user (when the caller is themselves a vendor/assessee, not an assessor). |

---

## What's deliberately *not* exposed at all

Straight from `agent-manifest.ts`'s own header comment: the TPRM module has
**~163 endpoints** in the real backend; only this curated ~30-command subset
(mostly reads, plus the 5 writes above) is exposed to the CLI/agent surface
at all. Explicitly excluded, on purpose, not just "not yet done": file
downloads/binary endpoints, internal/seeding/vector-sync routes, granular
section/evidence/reviewer micro-ops, version restore, document CRUD,
dashboard chart CRUD, and the destructive "delete all assessment data" —
which is *why* `adversarial-delete-vendor` in the dataset is correctly
unanswerable, not a dataset bug.

One route is excluded for a sharper reason, worth knowing if a future
manifest update ever adds it: `POST /tprm/assessment-dashboard/run-agent`
executes LLM-generated SQL via `knex.raw` (tenant-scoped, validated to a
single `SELECT`, fine for a human clicking a UI button) — but on an agent
surface its output would read as authoritative while actually being
non-deterministic. The comment is explicit: real audit analytics must come
from the ledger, not generated SQL.

## What actually caused the 8–15-`execute`-call runs (log-confirmed, fixed)

The paragraph below this one is the *original* source-reading-only theory
for why `dataset/results.json` runs were so expensive. It turned out to be
wrong about the dominant cause. Once `logs/harness.log` from a real
`dataset/run_dataset.py` run existed, tracing the actual command sequence
for every slow/failing entry showed three different, concrete root causes —
all now fixed in `harness/agent.py`/`harness/sandbox.py`, none of them
requiring a change to `skills/cyber-sierra/`:

1. **`recursion_limit` was never set, so it silently defaulted to
   LangGraph's 25.** Every "Recursion limit of 25 reached" failure matched
   this exactly. In the `tprm-activity-logs-list` case specifically, the
   model was actually on the right track — two wrong guesses, then `tprm
   --help`, then `activity-logs list` (a real, correct validation error:
   `"body must have required property 'objectType'"`), then a correct pivot
   to `activity-logs filter-options` to discover valid values — it simply
   ran out of graph steps before looping back to make the final,
   now-informed call. **Fix**: `harness/agent.py` now sets
   `GRAPH_RECURSION_LIMIT = 60` and passes it as `recursion_limit` in the
   `config` dict at both `astream_events` call sites.
2. **The model sometimes invoked `cybersierra auth login-browser`**, an
   interactive, browser-opening command that can never succeed headlessly —
   confirmed at `logs/harness.log:1829-1834`: two attempts, each timing out
   at the sandbox's own timeout ceiling (120s, then a self-escalated 300s),
   over 7 minutes burned on calls that were always going to fail, long
   enough to blow past `dataset/run_dataset.py`'s 180s client timeout
   (matches the `adversarial-scheduled-report` entry's
   `{"code": "http_error", "message": "timed out"}`). This directly
   contradicts this harness's own documented design (main README
   "Authentication model": *"this harness does **not** perform login
   itself, on purpose"*). **Fix**: `harness/sandbox.py` now denies
   `cybersierra auth login-browser` and `cybersierra auth login`
   specifically (`cybersierra auth whoami` and everything else unaffected),
   with a denial message telling the model auth is already handled rather
   than to try again.
3. **Discovery was shallow and got repeated instead of escalated.** In
   several slow/failing turns the model ran `cybersierra manifest --raw |
   python3 -c "...list(tree.keys())"` — which only returns top-level
   *module* names (`health`, `notifications`, `permissions`, `tprm`,
   `user-management`), never the actual resource/action commands under a
   module — then, instead of escalating to `cybersierra tprm --help`
   (which, every time it *was* tried, immediately produced the exact right
   resource list), it reverted to guessing plausible resource names
   (`tprm vendor list`, `tprm companies`, `tprm manage`, `notifications
   list`, `health status`, ...), sometimes 8–10 guesses in a row. **Fix**:
   `SYSTEM_PROMPT_APPENDIX` now has an explicit, still domain-agnostic rule
   — a shallow/high-level discovery result means "go one level deeper,"
   not "start guessing," and two failed guesses in a row means escalate to
   `--help` rather than try a third guess.

## Original theory (source-reading only) — kept as CLI trivia, not the cause

The facts below are still true, still worth knowing, and still not
documented anywhere in `skills/cyber-sierra/` or the live manifest's
`description` field — but log evidence shows they were **not** what broke
these particular runs (the CLI's action verbs don't expose an HTTP-method
choice to the model at all, so "guessing GET" was never actually a live
failure mode in practice):

1. Three `list` commands are `POST` with a JSON `--data` body, not `GET`
   with flags — `tprm assessees list`, `tprm activity-logs list`,
   `notifications feed list`.
2. `GET /tprm/assessees/list` 500s server-side (a real, filed backend bug,
   per the `agent-manifest.ts` source comment) — moot for an agent though,
   since the CLI itself only ever exposes the working POST form as
   `tprm assessees list`; there's no CLI-level way to accidentally invoke
   the broken GET variant.
3. The path-param-to-flag name is inconsistent across sibling
   `assessee-*` commands: `assessee-tenant-users list` takes
   `--assesseeId`, `assessee-scans list` takes `--id` for the same
   underlying assessee. Real, but the logs show the model got this right
   both times once it actually read the manifest for that command — it
   wasn't a source of wasted guesses in practice.
4. Multi-word resource names are all hyphenated (`activity-logs`,
   `assessee-scans`, `dashboard-charts`, ...) — plausible source of
   formatting guesses, and did appear as occasional wrong guesses in the
   logs, but was a minor contributor next to items 1–3 above.
5. `tprm activity-logs filter-options` exists because `objectType`/
   `objectId` values aren't guessable — the model consistently discovered
   and used this correctly once it got there; the issue was running out of
   turns before looping back (see fix 1 above), not failing to find this
   command.
