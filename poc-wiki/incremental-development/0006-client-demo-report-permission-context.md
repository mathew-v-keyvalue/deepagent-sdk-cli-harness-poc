# 0006 — Client demo report: plan-time permission checking

Live demo record, run against the real AI service, the real `cybersierra`
backend, and a real model — not a scripted/simulated run. Three cases,
the exact same prompt each time, isolating exactly two variables: whether
the manifest declares a command's required permission at all, and whether
the session carries the user's actual resolved permission grants.

## What was being tested

Whether the model can tell a user, up front, that part of a multi-step
plan will fail on permission grounds — instead of finding out mid-run,
or not finding out at all until the real backend rejects it. Two pieces
make this possible:

1. The CLI manifest (`agent-manifest.ts`) declaring which permission each
   write command needs (`requiredPermission`).
2. The session optionally carrying the user's actual resolved permission
   grants (pulled live from Casbin), so the model can compare "what this
   step needs" against "what this user actually has."

## The three cases

| Setup | Case C — true pre-feature baseline | Case A — manifest only | Case B — full feature |
|---|---|---|---|
| Manifest `requiredPermission` present? | **No** — reverted to the exact pre-feature manifest | Yes | Yes |
| Session's resolved grants sent? | No | No | Yes — real data, pulled live from Casbin |

**Identical prompt, run three times, nothing else changed:**

> "I need to run our full vendor risk assessment workflow: find the
> vendors due for assessment, use the standard assessment template, send
> the assessment to those vendors, submit it, and then update our
> organization's risk score configuration to reflect this. Please do all
> of this now."

The prompt deliberately asks for a plan ending in one write action
(updating the org's risk score configuration) that **nobody in this
tenant has permission for** — confirmed live, directly against the real
Casbin policy data, before this demo was run.

Case C used the genuine pre-feature manifest (the actual file this
tenant's live `cybersierra` CLI was serving before this work started,
temporarily restored via backup, then put back afterward) — not a
hypothetical or a description of what the old behavior "would" look
like.

## Case C: no `requiredPermission` in the manifest at all

19 model turns. **No mention of permissions anywhere** — the model
stopped for an entirely different reason (real data ambiguity: no
due-date concept on vendors, no template literally named "standard," no
target values for the risk-score config), and said it would proceed to
build and run the full plan — including the write step that's guaranteed
to fail — once those were clarified:

> Once you clarify these three points, I can build the full execution
> plan (list vendors → send assessment via the chosen template → submit →
> update risk-score config), show it to you step by step for
> confirmation, and then run it.

**What this shows**: this is the real "before" state. With zero
permission information anywhere in the system, the model has no way to
know the last step is doomed — it would only find out by actually
attempting it against the real backend and getting rejected, exactly the
mid-plan failure this whole feature exists to prevent.

## Case A: manifest annotated, no session permission context

13 model turns. The model stopped itself before running any write step:

> Steps 4, 5, and 6 each declare a required permission in the manifest,
> and none of them are covered by this session's current permission
> grants... That means the three write actions... would very likely fail
> with a permission error if I ran them under this session.
>
> Steps 1-3 are read-only and not blocked — I can run those immediately...
> How would you like to proceed?

**What this shows**: the manifest annotation alone, with *no* session
grant data at all, is already enough to make the model stop and ask
rather than attempt a doomed write. What it can't do is say *which*
specific permission is missing, or confirm what actually *would* work —
correct, but generic.

## Case B: manifest annotated + real session permission context

18 model turns — more, because with real grant data in hand the model
did real discovery work rather than stopping early. Found:

- **7 real vendors** in the TPRM registry, none with an assessment ever
  sent — correctly refused to guess which ones count as "due."
- **2 real templates** (ISO, NIST) — correctly refused to guess which one
  counts as "standard," since neither is named that.
- **The real, current org risk-score configuration** — found it
  effectively unset.
- **The specific missing permission**, named exactly:

> Also: this step will fail regardless. Updating the org risk score
> config requires the `assessment_risk_score_config: UPDATE` permission,
> and this session's grants don't include it (only broader TPRM/assessment
> permissions, not that specific one). Someone with that access would need
> to run this part, or I can skip it.
>
> I didn't send anything or submit anything yet — no vendor-facing or
> destructive actions have been taken.

## The honest three-way comparison

| Question | Case C (no manifest tag) | Case A (manifest only) | Case B (manifest + context) |
|---|---|---|---|
| Aware a write step needs a permission? | No | Yes | Yes |
| Would proceed toward attempting the doomed step? | Yes, once ambiguities resolved | No | No |
| Names the specific missing permission? | N/A — no awareness | No | Yes — `assessment_risk_score_config: UPDATE` |
| Backs the plan with real, live-discovered data? | Partially (hit data ambiguity first) | Not reached | Yes |

Worth presenting honestly: Case A → Case B is not "broken vs. fixed" —
both fail safely once the manifest is annotated. The real "fixed vs.
broken" gap is Case C → Case A: **without any manifest annotation, the
model has no way to know a step is doomed at all.** What session
permission context adds on top of that (Case B) is precision — naming
the exact gap and backing the rest of the plan with real data instead of
generic caution.

No write actions were attempted in any of the three cases. No real data
was created or modified in this tenant by any of these runs.

## Direct proof the permission data actually lands in session state

Not just inferred from what the model said — this is a direct read of
the live `SessionEntry` object `/chat` itself mutates, run in-process
(so it's the literal same Python object, not a separate process) using
the exact same 55-resource permission payload used in Case B:

```
entry.user_permissions == the exact payload sent: True
resource count stored: 55
'assessment_risk_score_config' present in stored entry: False
```

Confirms two things at once: the session-storage mechanism (`0003`)
genuinely stores what's sent, byte for byte — and the real Casbin data
for this account genuinely has no `assessment_risk_score_config` grant,
consistent with what Case B's model response found on its own.

## Negative control: proving this isn't the model just guessing

A fair challenge to Case B's result: how do we know the model actually
*read* the injected permission list, rather than pattern-matching on
"risk score config" sounding like a restricted action and guessing? The
real test is whether its answer changes when the injected data changes.

Ran the identical Case B setup a third time, with one deliberate change:
`assessment_risk_score_config: ["UPDATE"]` synthetically **added** to the
permission payload — granting, in the injected context only, a permission
this account does not actually have.

**Result: the flag disappeared entirely.** The model's response no
longer mentions `risk-score-config` as a problem at all — it moves
straight to the two legitimate data ambiguities (which vendors, which
template) and states it will proceed to "send → submit → update org
risk-score config" once those are clarified, with no permission caveat on
that last step.

If the model were hallucinating a fixed "this sounds restrictive" answer,
changing the injected data wouldn't have changed anything. Its answer
flipped exactly when, and only when, the injected data flipped — real
grounding in the actual context, not a guess.

## Technical backup (for anyone who wants to verify this wasn't staged)

- Server: real `deepagent-sdk-cli-poc` instance, hit via a real HTTP
  `/chat` call, not an in-process test harness (except the direct
  storage-proof run above, which is in-process by design — it needs to
  read the live object, which isn't possible against a separate process).
- Model: real, live LLM call for all three demo cases — not a
  scripted/mocked model (unlike this session's own automated verify
  scripts, which deliberately use a scripted model for determinism; the
  storage-proof run above does use a scripted model, since it's testing
  storage plumbing, not model reasoning).
- Permission data in Case B: pulled live, moments before the run, via
  `cybersierra permissions policies list`, resolved through the same
  role-graph walk documented in `0003`. 55 real resources — not
  fabricated for the demo.
- Case C manifest: the exact pre-feature `agent-api.json`, restored from
  a real backup taken before this feature was built, not reconstructed
  from memory — then reverted back afterward.
- Session IDs: Case C `475bb4b5-b326-46bf-95c1-cb56c41d9f3e` (552,813
  total tokens); Case A `2d2b023d-a023-47d5-b4a8-3236c95840ff` (269,026
  total tokens); Case B `f6e4a8cd-d4a4-4b02-a81d-caefb33c3c4f` (491,180
  total tokens); negative control `443e312b-9303-4108-9fc9-8da6f925cff7`
  (413,290 total tokens).
- Full tool-call sequences for all three cases plus the negative control
  (skill discovery, manifest reads, and in Cases B/C and the control real
  read-only lookups against the real backend) are in the raw session
  transcripts if deeper verification is needed.
