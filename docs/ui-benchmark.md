# AIME interface benchmark and TraceForge choices

## Research boundary

The comparison was performed read-only against the signed-in AIME workspace on 2026-08-27.
AIME is a general agent platform; TraceForge is a local coding agent with a much narrower promise.
The goal is therefore to extract interaction principles, not copy its navigation, marketplace, or
visual identity.

## What works well in AIME

| Pattern | Why it works | TraceForge response |
| --- | --- | --- |
| One dominant **New task** action beside persistent history | The next action and the way back are both obvious | Put rectangular New Task/Add Project actions above history; choose scope from the sidebar rather than inside the composer |
| A large, centered task composer with model/context controls nearby | Configuration is available without competing with the request | Keep the focused composer and provider readiness callout |
| Long executions keep detail behind progressive disclosure | Hundreds of tool rows do not bury the outcome | Keep the conversation primary and place plan/tool/review evidence in one collapsed Trace |
| Rich final response followed by artifact actions | Result and handoff are adjacent | Keep the final answer primary, then attach a compact check summary and Proof Pack action |
| Sticky “waiting for your next instruction” context | The system's current mode remains legible | Use explicit state, approval, and connection badges rather than a chat composer during execution |

## Measured typography baseline

Computed styles were sampled from the visible signed-in AIME task page rather than estimated from
a screenshot. Its primary sidebar action and task titles use 14px type; task grouping is 13px, the
composer is 13px with a 19.5px line height, and secondary metadata is mainly 10–12px. TraceForge
previously rendered many operational labels at 6–9px, so this was a real readability gap.

TraceForge now uses 13–14px for primary task content, 10–12px for labels and metadata, and no
critical 6–9px text. This adopts AIME's information hierarchy without copying its light theme or
general-purpose product navigation.

## What TraceForge should not copy

- Resource, skill, trigger, template, and marketplace navigation would dilute a course project whose
  differentiator is trustworthy local execution.
- A follow-up composer is useful only when it retains the same coding-task context and does not
  replace explicit Stop, Resume, approval, completion-review, and rollback controls.
- Raw operational detail should not remain expanded by default. TraceForge keeps exact persisted
  evidence available, but uses progressive disclosure and continues credential redaction.
- A light general-product aesthetic is not itself the insight. TraceForge keeps its compact dark
  mission-control identity so model narrative, machine evidence, and human decisions remain
  visually distinct.

## Tianshu source and runtime comparison

The second read-only comparison was fixed to Tianshu commit
[`3e830de`](https://github.com/dmql98/tianshu/tree/3e830de4709f1b1336e97f7f1dd396630ee0beb9)
on 2026-08-28 and checked both source and a locally started UI. Its useful product hierarchy is
global navigation → workspace-path session groups → sibling Chat/Trajectory views → optional
role/project/goal detail. In particular, Chat and Trajectory stay mounted when switching tabs, and
tool calls start as compact expandable rows ([ChatArea](https://github.com/dmql98/tianshu/blob/3e830de4709f1b1336e97f7f1dd396630ee0beb9/dev/web/client/src/components/Chat/ChatArea.tsx),
[ToolCall](https://github.com/dmql98/tianshu/blob/3e830de4709f1b1336e97f7f1dd396630ee0beb9/dev/web/client/src/components/Chat/ToolCall.tsx)).

TraceForge adopts the underlying principle—conversation and compact outcomes first, exact
trajectory and evidence on demand—but not the literal layout:

- Tianshu's side panels use fixed widths and transient Boolean state. TraceForge makes both panels
  resizable and keyboard operable, persists desktop preferences, defaults the right panel closed,
  and turns panels into temporary drawers at narrow breakpoints.
- Tianshu's file panel infers paths from current messages and fixed read/write tool names
  ([FilePanel](https://github.com/dmql98/tianshu/blob/3e830de4709f1b1336e97f7f1dd396630ee0beb9/dev/web/client/src/components/Chat/FilePanel.tsx)).
  TraceForge fingerprints canonical paths around native edits, binds the result to a specific turn,
  and keeps it explicitly separate from the task-wide cumulative Diff.
- Tianshu can expose reasoning, raw prompts, and raw trajectory objects. TraceForge never exposes
  hidden chain-of-thought or treats model-authored evidence text as equivalent to tool results.
- Tianshu's current source has moved model, reasoning, execution, and approval controls into the
  composer even though its README screenshot still describes some of them in the right panel.
  TraceForge bases decisions on the pinned source/runtime rather than copying an outdated image.
- Tianshu exposes five flat execution choices and runs shell text without an OS sandbox. TraceForge
  keeps Plan separate and offers three accurately qualified action profiles: Manual (逐项),
  deterministic Automatic, and **完全访问（工作区）**. The last never silently becomes the next
  turn's default and cannot auto-run unknown code on a policy-only host.

## Chosen changes

The initial chapter UI improved scan cost, but still made the internal pipeline feel like the
product. The refined run feed now uses two layers:

1. A multi-turn conversation containing requests, concise Agent updates, and final summaries.
2. One collapsed Trace containing exact plans, tools, checks, repair rounds, and completion review.

The right-side Timeline still exposes every event in exact sequence, while the Plan view renders
the complete downloadable Markdown contract. This reduces process ceremony without deleting
evidence or inventing a second source of truth. A terminal task exposes a follow-up composer that
keeps the same run, workspace, and evidence history; Plan mode remains an optional per-turn switch.
The new-task composer presents action permissions as three explanatory cards beside that switch;
the compact follow-up composer retains an independent selector. The run header and tool evidence
badges keep the effective choice visible after submission.

Successful work now ends with the Agent's final answer, a turn-local native-edit file list, and a
single compact completion footer. The former full-width evidence board was removed; Proof Pack is
still reachable from the footer when the right details panel is closed. The right panel defaults
closed, while both desktop panels can be toggled, pointer-dragged, keyboard resized, reset, and
restored after reload without allowing their combined widths to erase the conversation.

The task-entry surface now follows the same principle: direct runs remain top-level, projects are
collapsible folders, and the project plus button determines the composer workspace. At narrow
breakpoints, history and task details become focus-trapped drawers instead of disappearing.
