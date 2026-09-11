# Netra evaluator cleanup (2026-09-09)

Context: a batch of per-command "CLI Regex Match" evaluators (see
`EVALUATOR_FINDINGS.md`) was created against a dead span expression —
`spans[?name=='cli_call'].input`. No span is ever named `cli_call`; the
agent (`harness/agent.py`) only ever calls one tool, `run_execution_plan`,
so this expression could never resolve. The one evaluator that matters,
`Correct Rejection (No Tools Called)`, was already fixed to use the working
expression (`spans[?name=='Agent_Turn'] | [0].agent.actual_commands`), but
the broken batch plus one diagnostic probe built to isolate the bug were
never removed. This is that removal.

## How it was done

Neither the Netra MCP tools nor `netra-sdk` (v1.0.1) expose evaluator
deletion — confirmed by inspection: no `netra_delete_evaluator` MCP tool
exists, and the installed SDK's `evaluation/api.py` / `evaluation/client.py`
have zero evaluator-management methods (create/map/delete are all outside
the SDK's surface).

The dashboard's own (undocumented) REST API does support it —
`DELETE https://api.netra.dev.cybersierra.ai/evaluators/{id}` — found via
its network traffic. Run once, directly, via `curl`, authenticated with a
short-lived browser session JWT (`Authorization: Bearer <access_token>` +
`x-org-id: b01fa42f-c22f-41dd-be63-a2594371340b`) extracted from DevTools
for this one-time cleanup. Not a reusable script, not committed anywhere —
this is a private API that could change without notice, and the token used
is already expired.

One evaluator (`notifications feed unread-count`,
`cadd650d-f2ad-4c1b-a52a-dc59ee57c1d4`) had already been deleted manually
via the dashboard before this run (that was in fact the request used to
discover the endpoint). The remaining 20 were deleted here.

## Deleted (20/20, all HTTP 200)

**19 broken-expression evaluators** (`spans[?name=='cli_call'].input` —
dead expression):

- [x] CLI Regex Match — tprm dashboard-charts list — `9de01a30-4fa3-44fc-917c-188f5c839215`
- [x] CLI Regex Match — tprm dashboard-charts metadata — `e34101a9-f8f2-46f3-ae7c-f1342a47a803`
- [x] CLI Regex Match — tprm assessment-templates get — `3be577c4-36f4-43b9-8bed-33b1e1e1c8ca`
- [x] CLI Regex Match — permissions policies list — `a21ba34d-dae9-41f7-ac67-5e40bc437753`
- [x] CLI Regex Match — tprm assessees count — `c390ba73-1d26-4777-a467-b3e21ce16705`
- [x] CLI Regex Match — tprm assessments comparison — `aa4e71ed-0a5b-4dd1-b0e4-6b8b621588ca`
- [x] CLI Regex Match — tprm assessment-templates list — `e1e19953-b7b9-4823-a289-37d020688316`
- [x] CLI Regex Match — user-management me get — `fd06ad35-67e6-4f38-9e53-7b665967ec10`
- [x] CLI Regex Match — user-management users list — `0359c439-cb7e-49cb-a555-e980a94a3511`
- [x] CLI Regex Match — tprm assessees get — `f16dd4da-235a-4811-a8bc-72cd7f4ad5a7`
- [x] CLI Regex Match — tprm assessees list — `a7e17dc0-1dd9-46d2-9e39-fbe3ad14e9ea`
- [x] CLI Regex Match — tprm activity-logs list — `da1ccb1b-6989-48b9-8f4d-b7c33d0cf82a`
- [x] CLI Regex Match — tprm assessee-drafts list — `9840b276-88a3-4764-9ee8-68361355d483`
- [x] CLI Regex Match — tprm risk-score-config get-org — `8a1cd423-6a52-43e6-9c43-07d3a6ce254a`
- [x] CLI Regex Match — tprm assessments list — `5d6fbea3-1f91-461b-84cb-7464e0aa010b`
- [x] CLI Regex Match — tprm dashboard get — `5cea6a84-97bf-49c0-9755-8834391d69bb`
- [x] CLI Regex Match — tprm assessments get — `864a0a66-0742-43ed-8c4c-c501e8bbc715`
- [x] CLI Regex Match — notifications feed list — `04303ad4-330a-42f2-987d-502daa10ba38`
- [x] CLI Regex Match — retest v2 — `fa24bf9b-6b8d-45c8-80e6-77557a6e6742` (re-test copy of the original, same dead expression)

**1 diagnostic probe** (its job was done once the root cause was known):

- [x] Control — taskOutput sanity check — `c2015c23-f15b-4fc5-b4c4-3514f04652a5`

**Already deleted before this run** (manually, via dashboard):

- [x] CLI Regex Match — notifications feed unread-count — `cadd650d-f2ad-4c1b-a52a-dc59ee57c1d4`

## Kept — do not delete (superseded, see 2026-09-11 update below)

- ~~`Correct Rejection (No Tools Called)` — `9c0649bf-2a7f-4e20-b6cf-8af166d80ac0`~~
  — **deleted 2026-09-11, not required.** Was the only evaluator in the
  project as of this cleanup; see the update below for why it was removed
  and what replaced it.

**Caution** (historical, resolved 2026-09-11): `EVALUATOR_SETUP.md` recorded
this evaluator as applied via item-level override on 5 adversarial dataset
items in `cybersierra-morpheus-realistic-25`
(`73cb309e-78ac-416d-b9ce-68fe6ad64a3f`), but the live `netra_get_dataset_items`
response for that dataset doesn't surface any evaluator-override info at
all. This was confirmed still true on 2026-09-11 (re-checked empirically,
both before and after deleting the evaluator) — deleting an evaluator
record does **not** clear item-level override pointers to it; those 5
items' overrides were separately cleared via
`netra_update_dataset_item(evaluators=[])`.

## Result

`netra_list_evaluators` now returns exactly 1 evaluator
(`Correct Rejection (No Tools Called)`) for project
`24db2cb4-14af-4d64-b048-a7dc45c8a40c`. Building the actual scalable
CLI-correctness replacement (one custom evaluator matching
`agent.actual_commands` against each item's own `expectedOutput`, mapped
dataset-wide — see `EVALUATOR_FINDINGS.md`'s conclusion) is separate,
not-yet-started follow-up work.

## Update (2026-09-11)

Further cleanup, done by hand in the Netra dashboard's "My Evaluators" section:

- `Correct Rejection (No Tools Called)` (`9c0649bf-...`) — **deleted**, not required.
  Its 5 item-level overrides were cleared separately (see caution above).
- Both dead "CLI Correctness — actual vs expected commands" / "...v2" evaluators
  (ids `6f033e27-...`, `a45600f3-...`, see `EVALUATOR_SETUP.md`'s "Known dead
  evaluator" section and `NETRA_SDK_EXPRESSION_ENGINE_RCA.md`) — **deleted**.
- "Custom Cli Check Eval" (`706877e7-aa47-431c-ab1a-c22af5fbcb57`, `code` type,
  active on the `-noconfirm` dataset) — **renamed**, not deleted, to
  **"CyberSierra CLI Correctness (Dataset-Level) Evaluator"** — this is the one
  CLI-correctness-shaped evaluator that was actually working; renaming it
  instead of leaving 3 overlapping "CLI Correctness"-named evaluators around
  removes the naming confusion at its root.

Net effect: the project went from 1 evaluator (this file's original "Result"
above) to a brief peak of 6 (after adding pilot/session evaluators in a
separate track), back down to 1 clean evaluator post-cleanup, before
`EVALUATOR_FINDINGS.md`'s 2026-09-11 update added 3 more (Topic Adherence,
Plan Quality, Hallucination) for the "USE now" evaluator rollout — see that
file for the full account of what's live today.
