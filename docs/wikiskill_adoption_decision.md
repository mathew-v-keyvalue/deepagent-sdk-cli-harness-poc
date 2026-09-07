# Proposal: use WikiSkill's layered-memory pattern as the concrete design for D4's skill store

| | |
|---|---|
| **Prepared by** | Mathew |
| **Status** | Proposed — requesting a decision, not announcing one |
| **Decides** | Whether `arXiv:2608.27454` ("WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution") should shape *how* we build the skill store D4 already commits us to, and if so, how much of it |
| **Does not decide** | Whether to build a skill store at all — that's already decided (D4, `AGENT_RUNTIME_DECISION.md`) — or the harness/model/MCP decisions (D1–D3), which are out of scope here |

## 1 · Summary

Our client's own `AGENT_RUNTIME_DECISION.md` (D4, §3) already commits us to building a tenant-scoped, versioned, human-approval-gated skill store, budgeted at 3–5 engineer-weeks for v1, and already names "rollback & audit trail" as a required feature. A recent paper, WikiSkill, publishes a benchmarked architecture for almost exactly that shape of problem — and its central ablation result is that the specific *design choice* we're least likely to get right by improvising (whether the audit/rollback layer is a flat log or a curated, persistent knowledge base) is the single biggest lever in their results. This doc explains the paper in enough depth to judge that claim, maps it precisely onto D4's already-committed scope, and proposes adopting the paper's layering as the concrete design for D4's audit trail and skill-proposal step — with the paper's own gate mechanism explicitly kept *subordinate* to D4's existing human-approval requirement, not a replacement for it. Three options are laid out at the end; this is a request for a decision, not a fait accompli.

## 2 · The problem this addresses (already ours, already committed)

D4 in `AGENT_RUNTIME_DECISION.md` is unambiguous that skill authoring cannot be a free-for-all:

> "Recommendation: skills become tenant-scoped, versioned rows in the database, not files on a local disk. An agent may draft a new skill; a human must approve it before it is persisted as executable — full stop, no author-and-immediately-execute path, for any tenant, in this domain."

It also already specifies four sub-requirements: storage & versioning (one row per skill version, with a diff against the prior version), tenant isolation (reusing the `agent_safe` Postgres schema's session-GUC pattern already proven in production), generation-vs-execution separation (the human gate), and — the piece most relevant here — **rollback & audit trail**: "every skill invocation... gets logged... feeding into the platform's existing activity-log feature."

This repo's own port of the real `cyber-sierra` pipeline reproduces the *pre*-D4 state on purpose, as a data point: Reflect-&-Learn writes straight to `_generated/<name>/SKILL.md` + `metadata.json`, appended to `_generated/index.json`, with no validation, no versioning, no tenant scoping, and no gate on the write itself (see `README.md`, "Porting the real pipeline," and `ARCHITECTURE.md`'s `GAP` node). D4 already says this has to change. **The open question is not whether to build the audit/versioning layer — it's what shape it should take**, and that's what WikiSkill has an actual, benchmarked answer for.

## 3 · What WikiSkill actually is

### 3.1 The question it's answering

Prior "skill discovery" methods extract procedures from an agent's execution traces automatically, but the *reasoning behind* each accepted or rejected change is scattered across whatever optimization log happened to be kept, if any. WikiSkill's question: can an agent's experience be compiled into **persistent knowledge that survives the skill itself being rolled back**, so that the reasoning isn't lost just because a particular skill version didn't work out?

### 3.2 The architecture — three layers, not one log

- **`raw/`** — immutable execution traces. Every rollout, kept verbatim, never edited.
- **`wiki/`** — structured, *evolving* knowledge, separate from any one skill version: a pattern catalog (recurring failure modes and what fixed them), an evolution log (every accept/reject decision and why), and impact trackers (which patterns actually led to which proposals). This layer is never rolled back.
- **`skills/`** — the executable procedures an agent actually runs, each one linked back to the specific wiki pattern that motivated it via a `PURPOSE.md`.

### 3.3 The four roles in the loop

1. **Inference Agent** — runs rollouts using the *current* skill set. Deliberately **denied direct access to the wiki** during this step.
2. **Wiki Maintainer** — an LLM agent that does root-cause analysis on failed trajectories and consolidates successful strategies into the wiki, updating the catalog and evolution log.
3. **Wiki-Informed Skill Proposer** — a ReAct-style agent that reads the wiki's indices and impact trackers, actively chooses which patterns/traces to inspect, and proposes one atomic skill edit at a time.
4. **Gating & Rollback** — the proposal is validated against a held-out set; the formal rule is *accept if* `ℛ(𝒯val,k) > ℛbest`, *else roll back*. Critically: **the skill can be rolled back; the wiki entry that produced it is not** — the knowledge persists even when the specific change it produced didn't pan out.

### 3.4 What was actually tested

Five domains (LiveMathematicianBench — math reasoning; SealQA — web search; a spreadsheet-manipulation benchmark; OfficeQA — long-context document QA; ALFWorld — interactive embodied tasks), five models (Qwen-3.5-4B, Qwen-3.5-9B, Qwen-3.6-27B, Gemma-4-31B, Gemini-3.5-Flash), against three named baselines (Trace2Skill, EvoSkill, SkillOpt) and a no-skill control.

### 3.5 The results, and what they actually mean

| Finding | Number | Why it matters for us |
|---|---|---|
| Overall gain vs. best competing baseline | +3.3 to +12.0 pp, across all 5 models | It isn't a one-benchmark fluke |
| Gains *increase* with model scale (Qwen family) | +12.3% (4B) → +17.5% (9B) → +23.9% (27B) | Cuts against "a bigger model will make this unnecessary" — the opposite trend holds |
| Small model + skills vs. big model, no skills | Qwen-3.5-9B+WikiSkill (47.4%) beats Qwen-3.6-27B, no skills (39.4%) | Evolved knowledge can substitute for raw model scale — relevant if D3's frontier-model cost is ever revisited |
| Cross-model skill transfer | Qwen-3.6-27B's skills take Qwen-3.5-9B on SpreadSheet from 24.3%→50.5% — *beating* that model's own self-evolved skills (33.6%) | Skills aren't tightly coupled to the model that produced them |
| **The load-bearing ablation**: does the skill proposer see the persistent wiki? | 48.7% → 63.7% (+15.0 pp) | This is the single largest effect in the paper — the wiki layer, not the gating mechanism, is what's doing the work |
| Counter-finding: give the *inference* agent (not the proposer) direct wiki access during training | 63.7% → 60.9% (regression) | Confirms the layering is deliberate, not incidental — direct access lets the agent shortcut around actually internalizing the skill, which is exactly the failure mode we'd risk by, e.g., dumping raw logs into the model's context instead of curating them |

### 3.6 The paper's own stated limitations — read in full, not cherry-picked

- **No retrieval evaluated.** Skills are injected directly into the prompt in their experiments; they don't test retrieval/triggering from a large library. This matters more as a skill library grows.
- **Strict, binary gating.** A proposal that's neutral-to-validation but might compound with a later change is rejected outright — no partial credit, no "bank it for later."
- **No automated pruning.** The wiki accumulates patterns indefinitely across long evolution runs; nothing in the paper trims it.
- **No long-horizon coverage.** Nothing in their benchmark suite spans hundreds of actions or multi-hour execution.
- **No within-session adaptation.** The mechanism operates across iterations of an evolving system, not within one long-running rollout.

## 4 · Mapping onto D4 specifically

| D4 requirement (already committed) | Gap in this repo's current port | WikiSkill mechanism that addresses it |
|---|---|---|
| "Rollback & audit trail... every skill invocation gets logged" | Flat `_generated/index.json` append, no structure, no rollback | `raw/` (verbatim invocation log) + `wiki/` (curated evolution log of accept/reject decisions) — this is a design for the audit trail's *content*, not just its plumbing |
| "Storage & versioning... one row per skill version... diff against prior version" | No versioning at all today | Gating/rollback loop gives a principled accept/reject/rollback state machine per version, which is what a versioned-rows schema needs to actually encode |
| "A human must approve it before it is persisted as executable — full stop" | No gate of any kind today | **Not replaced.** WikiSkill's automated validation gate becomes a *pre-filter that scores a proposal before a human ever sees it* — the human approval stays the sole authority to persist, exactly as D4 requires. This is presented as a strict addition, not a loosening, of D4's existing gate. |
| Tenant isolation via the `agent_safe` pattern | Not in scope for this port | Orthogonal — WikiSkill's raw/wiki/skills split composes cleanly with per-tenant schema partitioning; each tenant gets its own `raw/`, `wiki/`, and `skills/` rows |

**Where this repo is already ahead of the paper's own evaluation setup**: DeepAgents' `SkillsMiddleware` already does progressive disclosure — skill name + description shown up front, full content read on demand (`ARCHITECTURE.md`'s `SKILLSMW` component). That's exactly the retrieval/triggering gap the WikiSkill authors admit they didn't evaluate. We're not starting from zero on that axis.

**Where the paper does not solve our remaining problem, stated plainly**: it has no answer for wiki pruning (our `_generated/index.json` growth problem persists either way) and the strict-gate tradeoff means a "not yet provably better, but plausibly useful" proposal gets discarded rather than banked — both carried forward here as open risks, not hidden.

## 5 · Options considered

**Option A — Adopt the full pattern as D4's v1 design.** Build D4's store as raw/wiki/skills, with the automated validation gate as a pre-filter feeding the required human approval step. Cost is inside D4's existing 3–5 engineer-week estimate — this is a design choice for that build, not new scope. Risk: more moving parts for a v1 than a flat schema; the wiki-maintainer role is a new agent to build and validate, not just a data-migration exercise.

**Option B — Partial adoption: wiki-as-audit-trail only.** Build D4's required audit trail as a curated wiki (pattern catalog + evolution log) rather than a flat activity log, but skip the automated gating/rollback machinery — D4's human approval remains the *only* gate, with no automated pre-filter. Cheaper and lower-risk than A; directly satisfies D4's audit-trail requirement with a materially better design than a flat log, per §3.5's ablation. Defers the harder gating-design question rather than solving it.

**Option C — Build D4 generically, treat WikiSkill as background research only.** Ship the flat versioned-rows-plus-activity-log design D4's text describes literally, without importing the paper's layering. Lowest near-term engineering risk; forgoes the specific, benchmarked result that a curated wiki outperforms a flat log for exactly this kind of proposal-quality problem (§3.5).

No option adds scope beyond D4's already-committed store — this decision is about *which design* fills already-budgeted work, not whether to spend more.

## 6 · Recommendation

**Option B**, as the near-term call: build D4's required audit trail as a wiki (pattern catalog + evolution log), keep D4's human approval as the sole gate for now, and treat Option A's automated pre-filter as the natural next step once the wiki layer has enough real evolution history to validate against — not abandoned, sequenced. This is lower-risk than committing to the full gating/rollback loop before we have our own held-out validation data to gate against, and it delivers the part of WikiSkill's result that's actually load-bearing (§3.5: the wiki, not the gate, drives the +15pp effect) without taking on the part that's hardest to get right on day one.

**Conditions under which Option A is the better call instead**: if D4's build is scheduled to start soon and staffed for the full 3–5 weeks in one push, building Option B first as a stand-alone step risks becoming throwaway scaffolding — better to design the store as raw/wiki/skills from day one and add the automated pre-filter once basic version tracking exists, rather than bolt it on after.

**Conditions under which Option C is the better call instead**: if D4's timeline is compressed enough that even the wiki-curation step (a genuinely new agent role, not just a schema choice) can't be justified for v1 — ship the flat design now, revisit the wiki layering as a v2 once the store exists and real usage data shows whether proposal quality is actually a problem worth solving this way.

This recommendation is offered, not asserted — happy to be overridden with specifics.

## 7 · Open questions for engineering leadership

1. Should the automated validation gate (Option A, if we go there) ever be visible to the human approver as a *score/recommendation*, or kept purely internal to avoid anchoring their judgment?
2. The cross-model/cross-tenant skill-transfer result (§3.5) raises a real product idea — a platform-level wiki that seeds new tenants faster — but that requires sharing *structural* patterns (not raw tenant data) across tenant boundaries. Does this need Security/Compliance sign-off before even a prototype, given D4's tenant-isolation requirement?
3. D4 already reuses the `agent_safe` schema pattern and the platform's existing activity-log feature — does building a wiki layer on top of the activity log conflict with how that log is already consumed elsewhere, or is it additive?
4. Is 3.5's model-scale finding (bigger models benefit *more*, not less) something worth factoring into D3's frontier-model-by-default decision, or is that too speculative to act on pre-benchmark?
5. Given the paper's own gap (no automated pruning), do we want a stated pruning policy before this ships, or is "revisit once the wiki is large enough to be a problem" an acceptable answer for v1?

## 8 · References

- WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution. `arXiv:2608.27454`.
- `AGENT_RUNTIME_DECISION.md` (this client's `morpheus_backend` repo, `draft-docs/morpheus-agent-sdk/`), §3 D4 — the tenant-scoped/versioned/human-gated skill store commitment this proposal builds on, and the "Rollback & audit trail" requirement it's designed against.
- This repo's `README.md`, "Porting the real pipeline" section, and `ARCHITECTURE.md`'s pipeline-mapping diagram — the pre-D4 state (`_generated/index.json`, no gate) this proposal is measured against.
