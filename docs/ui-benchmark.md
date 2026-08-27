# AIME interface benchmark and TraceForge choices

## Research boundary

The comparison was performed read-only against the signed-in AIME workspace on 2026-08-27.
AIME is a general agent platform; TraceForge is a local coding agent with a much narrower promise.
The goal is therefore to extract interaction principles, not copy its navigation, marketplace, or
visual identity.

## What works well in AIME

| Pattern | Why it works | TraceForge response |
| --- | --- | --- |
| One dominant **New task** action beside persistent history | The next action and the way back are both obvious | Already present; keep direct/project choice inside the composer |
| A large, centered task composer with model/context controls nearby | Configuration is available without competing with the request | Keep the focused composer and provider readiness callout |
| Long executions grouped into collapsible stages | Hundreds of tool rows do not bury the outcome | Adopt evidence chapters for Planning, Build/Repair, and Verification |
| Rich final response followed by artifact actions | Result and handoff are adjacent | Keep the evidence board and make Proof Pack the delivery action |
| Sticky “waiting for your next instruction” context | The system's current mode remains legible | Use explicit state, approval, and connection badges rather than a chat composer during execution |

## What TraceForge should not copy

- Resource, skill, trigger, template, and marketplace navigation would dilute a course project whose
  differentiator is trustworthy local execution.
- A generic follow-up chat box is less useful than explicit Stop, Resume, approval, verification,
  and rollback controls for a bounded coding run.
- Raw operational detail should not remain expanded by default. TraceForge keeps exact persisted
  evidence available, but uses progressive disclosure and continues credential redaction.
- A light general-product aesthetic is not itself the insight. TraceForge keeps its compact dark
  mission-control identity so model narrative, machine evidence, and human decisions remain
  visually distinct.

## Chosen change

The main run feed becomes a progressive-disclosure projection over the persisted event ledger:

1. Planning and decisions.
2. Build, followed by a separate repair chapter whenever verification returns findings.
3. Independent verification rounds.

Only the newest chapter opens automatically. Completed chapters remain one click away, individual
tool output is collapsed again inside each chapter, and the right-side Timeline still exposes every
event in exact sequence. This reduces scan cost without deleting evidence or inventing a second
source of truth.
