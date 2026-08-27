import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  Circle,
  ClipboardCheck,
  Code2,
  FileDiff,
  GitBranch,
  Hammer,
  History,
  LoaderCircle,
  MessageSquareMore,
  OctagonX,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Send,
  ShieldCheck,
  Sparkles,
  Square,
  TerminalSquare,
  Wifi,
  WifiOff,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { isActiveState, parseDiff, presentState } from "./lib";
import type {
  ClarificationAnswer,
  Run,
  RunEvent,
  RunState,
  TaskPlan,
} from "./types";
import { useTraceForge } from "./useTraceForge";

type InspectorTab = "timeline" | "diff" | "checks" | "verifier";

export default function App() {
  const forge = useTraceForge();
  const [showComposer, setShowComposer] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("timeline");

  useEffect(() => {
    if (forge.run?.state === "verifying") setInspectorTab("verifier");
    if (forge.run?.state === "succeeded") setInspectorTab("checks");
  }, [forge.run?.state]);

  return (
    <div className="app-shell">
      <Header
        status={forge.status}
        connected={forge.connected}
        run={forge.run}
      />
      {forge.error && (
        <div className="global-error" role="alert">
          <AlertTriangle size={16} />
          <span>{forge.error}</span>
          <button type="button" aria-label="Dismiss error" onClick={forge.clearError}><X size={15} /></button>
        </div>
      )}
      <main className="workspace-grid">
        <Sidebar
          runs={forge.runs}
          selectedRunId={forge.selectedRunId}
          onSelect={forge.selectRun}
          onNew={() => setShowComposer(true)}
        />
        <section className="main-stage">
          {!forge.run || showComposer ? (
            <TaskComposer
              onCancel={() => setShowComposer(false)}
              onSubmit={async (task, verifier) => {
                await forge.createRun(task, verifier);
                setShowComposer(false);
              }}
              canCancel={Boolean(forge.run)}
            />
          ) : (
            <RunStage
              run={forge.run}
              events={forge.events}
              onAnswer={(answers) => void forge.answerQuestions(answers)}
              onPlan={(decision, feedback) => void forge.decidePlan(decision, feedback)}
              onAction={(approved) => void forge.decideAction(approved)}
              onCancel={() => void forge.cancel()}
              onResume={() => void forge.resume()}
              onRollback={() => void forge.rollback()}
            />
          )}
        </section>
        <Inspector
          run={forge.run}
          events={forge.events}
          diff={forge.diff}
          tab={inspectorTab}
          onTab={setInspectorTab}
        />
      </main>
    </div>
  );
}

function Header({
  status,
  connected,
  run,
}: {
  status: ReturnType<typeof useTraceForge>["status"];
  connected: boolean;
  run: Run | null;
}) {
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-mark"><Hammer size={18} /></div>
        <div>
          <strong>TraceForge</strong>
          <span>Evidence-driven coding agent</span>
        </div>
      </div>
      <div className="topbar-context">
        <div className="context-item workspace-path" title={status?.workspace}>
          <GitBranch size={14} />
          <span>{status?.workspace ?? "Connecting…"}</span>
        </div>
        <div className="context-item"><Sparkles size={14} /><span>{status?.model ?? "—"}</span></div>
        {run && <div className="context-item"><Wrench size={14} /><span>{run.step_count} steps</span></div>}
        <div className={`connection ${!run || connected ? "online" : "offline"}`}>
          {!run || connected ? <Wifi size={14} /> : <WifiOff size={14} />}
          {!run ? "Ready" : connected ? "Live" : "Reconnecting"}
        </div>
      </div>
    </header>
  );
}

function Sidebar({
  runs,
  selectedRunId,
  onSelect,
  onNew,
}: {
  runs: Run[];
  selectedRunId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  return (
    <aside className="sidebar panel-edge">
      <div className="sidebar-heading">
        <div><History size={15} /><span>Runs</span></div>
        <button className="icon-button" type="button" onClick={onNew} title="New run"><Plus size={17} /></button>
      </div>
      <div className="run-list">
        {runs.length === 0 && <p className="muted empty-copy">No runs yet.</p>}
        {runs.map((run) => {
          const state = presentState(run.state);
          return (
            <button
              type="button"
              className={`run-item ${selectedRunId === run.id ? "selected" : ""}`}
              key={run.id}
              onClick={() => onSelect(run.id)}
            >
              <div className="run-item-top">
                <span className={`state-dot ${state.tone}`} />
                <span className="run-state">{state.label}</span>
                <span className="run-time">{relativeTime(run.updated_at)}</span>
              </div>
              <strong>{run.task}</strong>
              <span className="run-id">{run.id.slice(0, 8)}</span>
            </button>
          );
        })}
      </div>
      <div className="sidebar-footer">
        <ShieldCheck size={14} />
        <span>Local only · workspace bounded</span>
      </div>
    </aside>
  );
}

function TaskComposer({
  onSubmit,
  onCancel,
  canCancel,
}: {
  onSubmit: (task: string, verifier: boolean) => Promise<void>;
  onCancel: () => void;
  canCancel: boolean;
}) {
  const [task, setTask] = useState("");
  const [verifier, setVerifier] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  return (
    <div className="composer-wrap">
      <div className="hero-symbol"><Code2 size={30} /></div>
      <p className="eyebrow">NEW EVIDENCE RUN</p>
      <h1>What should TraceForge prove?</h1>
      <p className="hero-copy">
        Describe the outcome. TraceForge will inspect the workspace, ask only material questions,
        propose a plan, and wait for your approval before changing files.
      </p>
      <form
        className="task-composer"
        onSubmit={(event) => {
          event.preventDefault();
          if (!task.trim()) return;
          setSubmitting(true);
          void onSubmit(task.trim(), verifier).finally(() => setSubmitting(false));
        }}
      >
        <textarea
          autoFocus
          value={task}
          onChange={(event) => setTask(event.target.value)}
          placeholder="例如：修复多租户缓存串读，保持 TTL 语义，补充回归测试并确保全部检查通过。"
          rows={6}
        />
        <div className="composer-actions">
          <label className="toggle-row">
            <input type="checkbox" checked={verifier} onChange={(event) => setVerifier(event.target.checked)} />
            <span className="toggle" />
            <span><strong>Independent verifier</strong><small>Read-only review after checks pass</small></span>
          </label>
          <div className="button-row">
            {canCancel && <button className="button ghost" type="button" onClick={onCancel}>Cancel</button>}
            <button className="button primary" type="submit" disabled={!task.trim() || submitting}>
              {submitting ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={16} />}
              Start run
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

function RunStage({
  run,
  events,
  onAnswer,
  onPlan,
  onAction,
  onCancel,
  onResume,
  onRollback,
}: {
  run: Run;
  events: RunEvent[];
  onAnswer: (answers: ClarificationAnswer[]) => void;
  onPlan: (decision: "approve" | "revise", feedback?: string) => void;
  onAction: (approved: boolean) => void;
  onCancel: () => void;
  onResume: () => void;
  onRollback: () => void;
}) {
  const state = presentState(run.state);
  return (
    <div className="run-stage">
      <div className="run-header">
        <div>
          <div className="run-header-meta">
            <StateBadge state={run.state} />
            <span>RUN {run.id.slice(0, 8).toUpperCase()}</span>
          </div>
          <h2>{run.task}</h2>
        </div>
        <div className="button-row">
          {isActiveState(run.state) && (
            <button className="button danger-ghost" type="button" onClick={onCancel}><Square size={14} /> Stop</button>
          )}
          {run.state === "interrupted" && (
            <button className="button" type="button" onClick={onResume}><Play size={14} /> Resume</button>
          )}
          {["succeeded", "failed", "cancelled", "interrupted"].includes(run.state) && (
            <button className="button ghost" type="button" onClick={onRollback}><RotateCcw size={14} /> Rollback</button>
          )}
        </div>
      </div>
      <div className="phase-ribbon">
        {(["Plan", "Build", "Verify", "Evidence"] as const).map((label, index) => {
          const current = phaseIndex(run.state);
          return (
            <div className={index < current ? "done" : index === current ? "current" : ""} key={label}>
              <span>{index < current ? <Check size={12} /> : index + 1}</span>{label}
            </div>
          );
        })}
      </div>
      <ActivityFeed events={events} stateLabel={state.label} />
      <div className="interaction-dock">
        {run.state === "awaiting_clarification" && run.clarification && (
          <ClarificationPanel request={run.clarification} onSubmit={onAnswer} />
        )}
        {run.state === "awaiting_plan_approval" && run.plan && (
          <PlanPanel plan={run.plan} onDecision={onPlan} />
        )}
        {run.state === "awaiting_action_approval" && run.pending_approval && (
          <ApprovalPanel approval={run.pending_approval} onDecision={onAction} />
        )}
        {run.state === "interrupted" && (
          <Notice icon={<Pause size={18} />} title="Run interrupted">
            No command will be replayed automatically. Resume to inspect the current workspace first.
          </Notice>
        )}
        {run.error && <Notice icon={<OctagonX size={18} />} title="Run stopped" danger>{run.error}</Notice>}
        {run.state === "succeeded" && <EvidenceBoard run={run} />}
      </div>
    </div>
  );
}

function ActivityFeed({ events, stateLabel }: { events: RunEvent[]; stateLabel: string }) {
  const end = useRef<HTMLDivElement>(null);
  const visible = events.filter((event) =>
    ["message", "tool.completed", "state.changed", "error"].includes(event.type),
  );
  useEffect(() => end.current?.scrollIntoView({ behavior: "smooth", block: "end" }), [events.length]);
  return (
    <div className="activity-feed">
      {visible.length === 0 && (
        <div className="thinking-row"><LoaderCircle className="spin" size={16} /><span>{stateLabel}…</span></div>
      )}
      {visible.map((event) => <ActivityItem event={event} key={event.seq} />)}
      <div ref={end} />
    </div>
  );
}

function ActivityItem({ event }: { event: RunEvent }) {
  if (event.type === "message") {
    return (
      <article className="activity message-card">
        <div className="activity-icon"><MessageSquareMore size={15} /></div>
        <div><span className="activity-label">{String(event.payload.phase ?? "Agent")}</span><ReactMarkdown>{String(event.payload.content ?? "")}</ReactMarkdown></div>
      </article>
    );
  }
  if (event.type === "tool.completed") {
    const call = (event.payload.call ?? {}) as { name?: string; arguments?: Record<string, unknown> };
    const result = (event.payload.result ?? {}) as { ok?: boolean; output?: string; error?: string; metadata?: Record<string, unknown> };
    return (
      <article className={`activity tool-card ${result.ok ? "ok" : "bad"}`}>
        <div className="activity-icon"><TerminalSquare size={15} /></div>
        <div className="tool-body">
          <div className="tool-title"><code>{call.name ?? "tool"}</code><span>{result.ok ? "completed" : "failed"}</span></div>
          <p className="tool-args">{formatArguments(call.arguments)}</p>
          {(result.output || result.error) && <pre>{result.error ?? result.output}</pre>}
        </div>
      </article>
    );
  }
  if (event.type === "error") {
    return <Notice icon={<OctagonX size={17} />} title="Error" danger>{String(event.payload.message ?? "Unknown error")}</Notice>;
  }
  const nextState = String(event.payload.state ?? "state change");
  return (
    <div className="state-transition"><Circle size={8} fill="currentColor" /><span>{nextState.replaceAll("_", " ")}</span><time>{clockTime(event.created_at)}</time></div>
  );
}

function ClarificationPanel({ request, onSubmit }: { request: NonNullable<Run["clarification"]>; onSubmit: (answers: ClarificationAnswer[]) => void }) {
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const complete = request.questions.every((question) => selected[question.id] || custom[question.id]?.trim());
  return (
    <div className="decision-panel clarification-panel">
      <div className="decision-heading"><MessageSquareMore size={19} /><div><p>Clarification · round {request.round}</p><h3>A few decisions change the implementation</h3></div></div>
      {request.questions.map((question) => (
        <fieldset key={question.id}>
          <legend>{question.prompt}</legend>
          <div className="option-grid">
            {question.options.map((option) => (
              <label className={`option-card ${selected[question.id] === option.id ? "selected" : ""}`} key={option.id}>
                <input
                  type="radio"
                  name={question.id}
                  checked={selected[question.id] === option.id}
                  onChange={() => {
                    setSelected((current) => ({ ...current, [question.id]: option.id }));
                    setCustom((current) => ({ ...current, [question.id]: "" }));
                  }}
                />
                <span className="radio-dot" />
                <span><strong>{option.label}{option.recommended && <em>Recommended</em>}</strong><small>{option.description}</small></span>
              </label>
            ))}
            <label className={`option-card custom-option ${custom[question.id] ? "selected" : ""}`}>
              <span className="radio-dot" />
              <input
                type="text"
                placeholder="其他答案…"
                value={custom[question.id] ?? ""}
                onChange={(event) => {
                  setCustom((current) => ({ ...current, [question.id]: event.target.value }));
                  setSelected((current) => ({ ...current, [question.id]: "" }));
                }}
              />
            </label>
          </div>
        </fieldset>
      ))}
      <div className="decision-actions"><span className="muted">TraceForge will re-plan from these choices.</span><button className="button primary" type="button" disabled={!complete} onClick={() => onSubmit(request.questions.map((question) => custom[question.id]?.trim() ? { question_id: question.id, custom_text: custom[question.id].trim() } : { question_id: question.id, option_id: selected[question.id] }))}><Send size={15} /> Continue</button></div>
    </div>
  );
}

function PlanPanel({ plan, onDecision }: { plan: TaskPlan; onDecision: (decision: "approve" | "revise", feedback?: string) => void }) {
  const [revising, setRevising] = useState(false);
  const [feedback, setFeedback] = useState("");
  return (
    <div className="decision-panel plan-panel">
      <div className="decision-heading"><ClipboardCheck size={19} /><div><p>PLAN REVIEW</p><h3>{plan.summary}</h3></div></div>
      <ol className="plan-steps">{plan.steps.map((step) => <li key={step.id}><span>{step.id}</span><div><strong>{step.title}</strong>{step.description && <small>{step.description}</small>}</div></li>)}</ol>
      <div className="plan-checks"><p>Completion contract</p>{plan.acceptance_checks.map((check) => <div key={check.id}><ShieldCheck size={14} /><span>{check.label}</span>{check.command && <code>{check.command.join(" ")}</code>}</div>)}</div>
      {plan.risks.length > 0 && <div className="risk-strip"><AlertTriangle size={15} /><span>{plan.risks.join(" · ")}</span></div>}
      {revising && <textarea className="revision-input" autoFocus placeholder="What should change in this plan?" value={feedback} onChange={(event) => setFeedback(event.target.value)} />}
      <div className="decision-actions"><span className="muted">No files have been changed.</span><div className="button-row">{revising ? <><button className="button ghost" type="button" onClick={() => setRevising(false)}>Back</button><button className="button" type="button" disabled={!feedback.trim()} onClick={() => onDecision("revise", feedback.trim())}>Send revision</button></> : <><button className="button ghost" type="button" onClick={() => setRevising(true)}>Revise</button><button className="button primary" type="button" onClick={() => onDecision("approve")}><Check size={15} /> Approve & build</button></>}</div></div>
    </div>
  );
}

function ApprovalPanel({ approval, onDecision }: { approval: NonNullable<Run["pending_approval"]>; onDecision: (approved: boolean) => void }) {
  return (
    <div className="decision-panel approval-panel">
      <div className="decision-heading"><AlertTriangle size={19} /><div><p>ACTION APPROVAL · {approval.risk}</p><h3>{approval.summary}</h3></div></div>
      <p className="approval-reason">{approval.reason}</p>
      <pre>{JSON.stringify(approval.tool_call.arguments, null, 2)}</pre>
      <div className="decision-actions"><span className="muted">Unknown commands never run silently.</span><div className="button-row"><button className="button danger-ghost" type="button" onClick={() => onDecision(false)}>Reject</button><button className="button warning" type="button" onClick={() => onDecision(true)}>Run once</button></div></div>
    </div>
  );
}

function EvidenceBoard({ run }: { run: Run }) {
  return (
    <div className="evidence-board">
      <div className="evidence-seal"><CheckCircle2 size={24} /></div>
      <div><p className="eyebrow">COMPLETION EVIDENCE</p><h3>Work proven, not merely reported</h3><p>{run.verification?.summary}</p></div>
      <div className="evidence-stats"><div><strong>{run.plan?.acceptance_checks.filter((check) => check.status === "passed").length ?? 0}</strong><span>checks passed</span></div><div><strong>{run.step_count}</strong><span>tool steps</span></div><div><strong>{run.repair_cycles}</strong><span>repair cycles</span></div></div>
    </div>
  );
}

function Inspector({ run, events, diff, tab, onTab }: { run: Run | null; events: RunEvent[]; diff: string; tab: InspectorTab; onTab: (tab: InspectorTab) => void }) {
  const tabs: Array<{ id: InspectorTab; label: string; icon: typeof History }> = [
    { id: "timeline", label: "Timeline", icon: History },
    { id: "diff", label: "Diff", icon: FileDiff },
    { id: "checks", label: "Checks", icon: ClipboardCheck },
    { id: "verifier", label: "Verifier", icon: ShieldCheck },
  ];
  return (
    <aside className="inspector panel-edge">
      <nav className="inspector-tabs">{tabs.map(({ id, label, icon: Icon }) => <button type="button" className={tab === id ? "active" : ""} onClick={() => onTab(id)} key={id}><Icon size={14} /><span>{label}</span></button>)}</nav>
      <div className="inspector-content">
        {!run && <div className="inspector-empty"><FileDiff size={26} /><p>Select a run to inspect evidence.</p></div>}
        {run && tab === "timeline" && <Timeline events={events} />}
        {run && tab === "diff" && <DiffView diff={diff} />}
        {run && tab === "checks" && <ChecksView run={run} />}
        {run && tab === "verifier" && <VerifierView run={run} />}
      </div>
    </aside>
  );
}

function Timeline({ events }: { events: RunEvent[] }) {
  return <div className="timeline">{events.length === 0 && <p className="muted">Waiting for evidence…</p>}{events.map((event) => <div className="timeline-row" key={event.seq}><span className={`timeline-marker ${event.type.includes("error") ? "bad" : event.type.includes("completed") ? "good" : ""}`} /><div><strong>{event.type}</strong><small>{clockTime(event.created_at)} · #{event.seq}</small><p>{eventSummary(event)}</p></div></div>)}</div>;
}

function DiffView({ diff }: { diff: string }) {
  const lines = useMemo(() => parseDiff(diff), [diff]);
  if (!diff) return <div className="inspector-empty"><FileDiff size={26} /><p>No agent-authored file changes yet.</p></div>;
  return <pre className="diff-view">{lines.map((line, index) => <span className={`diff-${line.kind}`} key={`${index}-${line.text}`}><i>{index + 1}</i>{line.text || " "}</span>)}</pre>;
}

function ChecksView({ run }: { run: Run }) {
  if (!run.plan) return <div className="inspector-empty"><ClipboardCheck size={26} /><p>Checks appear after planning.</p></div>;
  return <div className="checks-view"><div className="section-kicker">ACCEPTANCE CONTRACT</div>{run.plan.acceptance_checks.map((check) => <article className={`check-row ${check.status}`} key={check.id}><div className="check-icon">{check.status === "passed" ? <Check size={14} /> : check.status === "failed" ? <X size={14} /> : <Circle size={10} />}</div><div><strong>{check.label}</strong>{check.command && <code>{check.command.join(" ")}</code>}{check.evidence && <pre>{check.evidence}</pre>}</div><span>{check.status}</span></article>)}</div>;
}

function VerifierView({ run }: { run: Run }) {
  const report = run.verification;
  if (!report) return <div className="inspector-empty"><ShieldCheck size={26} /><p>{run.verifier_enabled ? "Independent review starts after checks pass." : "Verifier disabled for this run."}</p></div>;
  return <div className="verifier-view"><div className={`verdict ${report.verdict}`}><ShieldCheck size={22} /><div><span>INDEPENDENT VERDICT</span><strong>{report.verdict}</strong></div></div><p>{report.summary}</p>{report.findings.map((finding) => <article className="finding" key={`${finding.severity}-${finding.title}`}><span>{finding.severity}</span><strong>{finding.title}</strong><p>{finding.evidence}</p>{finding.suggested_fix && <small>Fix: {finding.suggested_fix}</small>}</article>)}</div>;
}

function StateBadge({ state }: { state: RunState }) {
  const presentation = presentState(state);
  return <span className={`state-badge ${presentation.tone}`}>{presentation.tone === "active" && <LoaderCircle className="spin" size={12} />}{presentation.label}</span>;
}

function Notice({ icon, title, children, danger = false }: { icon: React.ReactNode; title: string; children: React.ReactNode; danger?: boolean }) {
  return <div className={`notice ${danger ? "danger" : ""}`}><div>{icon}</div><div><strong>{title}</strong><p>{children}</p></div></div>;
}

function phaseIndex(state: RunState): number {
  if (["created", "planning", "awaiting_clarification", "awaiting_plan_approval"].includes(state)) return 0;
  if (["executing", "awaiting_action_approval"].includes(state)) return 1;
  if (state === "verifying") return 2;
  return 3;
}

function formatArguments(value?: Record<string, unknown>): string {
  if (!value) return "";
  if (Array.isArray(value.argv)) return value.argv.map(String).join(" ");
  if (typeof value.path === "string") return value.path;
  return JSON.stringify(value).slice(0, 220);
}

function eventSummary(event: RunEvent): string {
  if (event.type === "state.changed") return `→ ${String(event.payload.state ?? "unknown").replaceAll("_", " ")}`;
  if (event.type === "message") return String(event.payload.content ?? "").slice(0, 100);
  if (event.type === "tool.completed") {
    const call = event.payload.call as { name?: string } | undefined;
    return call?.name ?? "Tool result";
  }
  if (event.type === "plan.updated") return "Completion contract updated";
  if (event.type === "diff.updated") return "Workspace diff changed";
  return "Evidence recorded";
}

function relativeTime(value: string): string {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function clockTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}
