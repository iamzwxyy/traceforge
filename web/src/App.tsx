import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  Circle,
  ClipboardCheck,
  Code2,
  Download,
  FileDiff,
  Fingerprint,
  Folder,
  FolderOpen,
  Gauge,
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
  Settings,
  ShieldCheck,
  Sparkles,
  Square,
  TerminalSquare,
  Wifi,
  WifiOff,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { isActiveState, parseDiff, presentState } from "./lib";
import type {
  ClarificationAnswer,
  DirectoryListing,
  PlanGate,
  Project,
  ProofPack,
  ProviderConfig,
  ProviderProbe,
  Run,
  RunEvent,
  RunState,
  RunTarget,
  TaskPlan,
} from "./types";
import { useTraceForge } from "./useTraceForge";

type InspectorTab = "timeline" | "diff" | "checks" | "verifier";

export default function App() {
  const forge = useTraceForge();
  const [showComposer, setShowComposer] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showProof, setShowProof] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("timeline");

  useEffect(() => {
    if (forge.run?.state === "verifying") setInspectorTab("verifier");
    if (forge.run?.state === "succeeded") setInspectorTab("checks");
  }, [forge.run?.state]);

  useEffect(() => setShowProof(false), [forge.run?.id]);

  return (
    <div className="app-shell">
      <Header
        status={forge.status}
        connected={forge.connected}
        run={forge.run}
        providerReady={Boolean(forge.provider?.api_key_configured)}
        onSettings={() => setShowSettings(true)}
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
          projects={forge.projects}
          selectedRunId={forge.selectedRunId}
          onSelect={forge.selectRun}
          onNew={() => setShowComposer(true)}
        />
        <section className="main-stage">
          {!forge.run || showComposer ? (
            <TaskComposer
              key={forge.status?.suggested_task ?? "standard"}
              suggestedTask={forge.status?.suggested_task ?? ""}
              lastWorkspace={forge.status?.last_workspace ?? forge.status?.workspace ?? ""}
              projects={forge.projects}
              providerReady={Boolean(forge.provider?.api_key_configured)}
              onOpenSettings={() => setShowSettings(true)}
              onListDirectories={forge.listDirectories}
              onCreateProject={forge.createProject}
              onCancel={() => setShowComposer(false)}
              onSubmit={async (task, verifier, target) => {
                await forge.createRun(task, verifier, target);
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
              onProof={() => {
                setShowProof(true);
                void forge.loadProofPack(forge.run!.id);
              }}
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
      {showSettings && forge.provider && (
        <ProviderDialog
          provider={forge.provider}
          onClose={() => setShowSettings(false)}
          onSave={forge.saveProvider}
          onTest={forge.testProvider}
        />
      )}
      {showProof && forge.run && (
        <ProofPackDialog
          pack={forge.proofPack}
          runId={forge.run.id}
          onClose={() => setShowProof(false)}
        />
      )}
    </div>
  );
}

function Header({
  status,
  connected,
  run,
  providerReady,
  onSettings,
}: {
  status: ReturnType<typeof useTraceForge>["status"];
  connected: boolean;
  run: Run | null;
  providerReady: boolean;
  onSettings: () => void;
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
        <div className="context-item workspace-path" title={run?.workspace ?? status?.last_workspace}>
          <GitBranch size={14} />
          <span>{run?.workspace ?? status?.last_workspace ?? "Connecting…"}</span>
        </div>
        <div className="context-item"><Sparkles size={14} /><span>{status?.model ?? "—"}</span></div>
        {run && <div className="context-item"><Wrench size={14} /><span>{run.step_count} steps</span></div>}
        {run && (
          <div
            className="context-item"
            title={`${run.context_tokens.toLocaleString()} of ${run.context_limit.toLocaleString()} estimated tokens`}
          >
            <Gauge size={14} />
            <span>{formatTokens(run.context_tokens)} / {formatTokens(run.context_limit)} ctx</span>
          </div>
        )}
        <div className={`connection ${!run || connected ? "online" : "offline"}`}>
          {!run || connected ? <Wifi size={14} /> : <WifiOff size={14} />}
          {!run ? "Ready" : connected ? "Live" : "Reconnecting"}
        </div>
        <button
          className={`icon-button settings-button ${providerReady ? "" : "needs-attention"}`}
          type="button"
          onClick={onSettings}
          title={providerReady ? "Model settings" : "Configure model credentials"}
          aria-label="Model settings"
        >
          <Settings size={16} />
        </button>
      </div>
    </header>
  );
}

function Sidebar({
  runs,
  projects,
  selectedRunId,
  onSelect,
  onNew,
}: {
  runs: Run[];
  projects: Project[];
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
          const project = projects.find((item) => item.id === run.project_id);
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
              <div className="run-item-foot">
                <span className="run-id">{run.id.slice(0, 8)}</span>
                <span className="run-scope">{project?.name ?? "Direct"}</span>
              </div>
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
  suggestedTask,
  lastWorkspace,
  projects,
  providerReady,
  onOpenSettings,
  onListDirectories,
  onCreateProject,
  onSubmit,
  onCancel,
  canCancel,
}: {
  suggestedTask: string;
  lastWorkspace: string;
  projects: Project[];
  providerReady: boolean;
  onOpenSettings: () => void;
  onListDirectories: (path?: string) => Promise<DirectoryListing>;
  onCreateProject: (name: string, root: string, createDirectory: boolean) => Promise<Project>;
  onSubmit: (task: string, verifier: boolean, target: RunTarget) => Promise<void>;
  onCancel: () => void;
  canCancel: boolean;
}) {
  const [task, setTask] = useState(suggestedTask);
  const [verifier, setVerifier] = useState(true);
  const [targetMode, setTargetMode] = useState<"direct" | "project">("direct");
  const [workspace, setWorkspace] = useState(lastWorkspace);
  const [projectId, setProjectId] = useState(projects[0]?.id ?? "");
  const [showProject, setShowProject] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => {
    if (!workspace && lastWorkspace) setWorkspace(lastWorkspace);
  }, [lastWorkspace, workspace]);
  useEffect(() => {
    if (!projectId && projects.length) setProjectId(projects[0].id);
  }, [projectId, projects]);
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
          if (!task.trim() || (targetMode === "project" && !projectId)) return;
          setSubmitting(true);
          const target = targetMode === "project" ? { project_id: projectId } : { workspace };
          void onSubmit(task.trim(), verifier, target)
            .catch(() => undefined)
            .finally(() => setSubmitting(false));
        }}
      >
        {!providerReady && (
          <button className="setup-callout" type="button" onClick={onOpenSettings}>
            <AlertTriangle size={15} />
            <span><strong>Model setup required</strong><small>Add a credential file and verify native tool calling.</small></span>
            <ArrowRight size={15} />
          </button>
        )}
        <div className="target-panel">
          <div className="segmented" aria-label="Run target">
            <button type="button" className={targetMode === "direct" ? "active" : ""} onClick={() => setTargetMode("direct")}>Direct task</button>
            <button type="button" className={targetMode === "project" ? "active" : ""} onClick={() => setTargetMode("project")}>Project</button>
          </div>
          {targetMode === "direct" ? (
            <DirectoryField
              label="Workspace directory"
              value={workspace}
              onChange={setWorkspace}
              onListDirectories={onListDirectories}
            />
          ) : (
            <div className="project-target-row">
              <label className="field-label">
                <span>Project</span>
                <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
                  <option value="" disabled>{projects.length ? "Choose a project" : "No projects yet"}</option>
                  {projects.map((project) => <option value={project.id} key={project.id}>{project.name} — {project.root}</option>)}
                </select>
              </label>
              <button className="button" type="button" onClick={() => setShowProject(true)}><Plus size={14} /> New project</button>
            </div>
          )}
        </div>
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
            <button
              className="button primary"
              type="submit"
              disabled={!task.trim() || !providerReady || submitting || (targetMode === "project" && !projectId)}
            >
              {submitting ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={16} />}
              Start run
            </button>
          </div>
        </div>
      </form>
      {showProject && (
        <ProjectDialog
          initialDirectory={workspace || lastWorkspace}
          onClose={() => setShowProject(false)}
          onListDirectories={onListDirectories}
          onCreate={async (name, root, createDirectory) => {
            const project = await onCreateProject(name, root, createDirectory);
            setProjectId(project.id);
            setTargetMode("project");
            setShowProject(false);
          }}
        />
      )}
    </div>
  );
}

function DirectoryField({
  label,
  value,
  onChange,
  onListDirectories,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  onListDirectories: (path?: string) => Promise<DirectoryListing>;
}) {
  const [showPicker, setShowPicker] = useState(false);
  return (
    <>
      <label className="field-label directory-field">
        <span>{label}</span>
        <div>
          <input value={value} onChange={(event) => onChange(event.target.value)} />
          <button className="button" type="button" onClick={() => setShowPicker(true)}>
            <FolderOpen size={14} /> Browse
          </button>
        </div>
      </label>
      {showPicker && (
        <DirectoryDialog
          initialPath={value}
          onClose={() => setShowPicker(false)}
          onChoose={(path) => {
            onChange(path);
            setShowPicker(false);
          }}
          onListDirectories={onListDirectories}
        />
      )}
    </>
  );
}

function DirectoryDialog({
  initialPath,
  onClose,
  onChoose,
  onListDirectories,
}: {
  initialPath?: string;
  onClose: () => void;
  onChoose: (path: string) => void;
  onListDirectories: (path?: string) => Promise<DirectoryListing>;
}) {
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [path, setPath] = useState(initialPath ?? "");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback((nextPath?: string) => {
    setLoading(true);
    setError(null);
    void onListDirectories(nextPath || undefined)
      .then((next) => {
        setListing(next);
        setPath(next.current);
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setLoading(false));
  }, [onListDirectories]);

  useEffect(() => load(initialPath), [initialPath, load]);

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal directory-modal" role="dialog" aria-modal="true" aria-labelledby="directory-title">
        <div className="modal-heading">
          <div><p className="eyebrow">LOCAL WORKSPACE</p><h2 id="directory-title">Choose a directory</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close directory browser"><X size={17} /></button>
        </div>
        <div className="path-entry">
          <input value={path} onChange={(event) => setPath(event.target.value)} aria-label="Directory path" />
          <button className="button" type="button" onClick={() => load(path)}>Go</button>
        </div>
        {error && <p className="inline-error">{error}</p>}
        <div className="directory-list">
          {loading && <div className="thinking-row"><LoaderCircle className="spin" size={15} /> Loading…</div>}
          {!loading && listing?.parent && (
            <button type="button" onClick={() => load(listing.parent ?? undefined)}>
              <FolderOpen size={16} /><span><strong>..</strong><small>{listing.parent}</small></span>
            </button>
          )}
          {!loading && listing?.children.map((entry) => (
            <button type="button" key={entry.path} onClick={() => load(entry.path)}>
              <Folder size={16} /><span><strong>{entry.name}</strong><small>{entry.path}</small></span>
            </button>
          ))}
          {!loading && listing && listing.children.length === 0 && <p className="muted empty-copy">No visible subdirectories.</p>}
        </div>
        <div className="modal-actions">
          <span className="selected-path" title={listing?.current}>{listing?.current ?? "Choose a readable directory"}</span>
          <div className="button-row">
            <button className="button ghost" type="button" onClick={onClose}>Cancel</button>
            <button className="button primary" type="button" disabled={!listing} onClick={() => listing && onChoose(listing.current)}>Choose this directory</button>
          </div>
        </div>
      </section>
    </div>
  );
}

function ProjectDialog({
  initialDirectory,
  onClose,
  onListDirectories,
  onCreate,
}: {
  initialDirectory: string;
  onClose: () => void;
  onListDirectories: (path?: string) => Promise<DirectoryListing>;
  onCreate: (name: string, root: string, createDirectory: boolean) => Promise<void>;
}) {
  const [mode, setMode] = useState<"open" | "create">("open");
  const [name, setName] = useState("");
  const [directory, setDirectory] = useState(initialDirectory);
  const [folderName, setFolderName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const root = mode === "create"
    ? `${directory.replace(/\/$/, "")}/${folderName.trim()}`
    : directory;
  const valid = Boolean(name.trim() && directory.trim() && (mode === "open" || folderName.trim()));

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="project-title">
        <div className="modal-heading">
          <div><p className="eyebrow">PROJECT WORKSPACE</p><h2 id="project-title">Add a project</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close project dialog"><X size={17} /></button>
        </div>
        <div className="segmented wide" aria-label="Project directory mode">
          <button type="button" className={mode === "open" ? "active" : ""} onClick={() => setMode("open")}>Open existing</button>
          <button type="button" className={mode === "create" ? "active" : ""} onClick={() => setMode("create")}>Create empty</button>
        </div>
        <label className="field-label"><span>Project name</span><input value={name} onChange={(event) => setName(event.target.value)} autoFocus /></label>
        <DirectoryField
          label={mode === "create" ? "Parent directory" : "Existing directory"}
          value={directory}
          onChange={setDirectory}
          onListDirectories={onListDirectories}
        />
        {mode === "create" && (
          <label className="field-label"><span>New folder name</span><input value={folderName} onChange={(event) => setFolderName(event.target.value)} placeholder="traceforge-project" /></label>
        )}
        <div className="path-preview"><span>Project root</span><code>{root}</code></div>
        <div className="modal-actions">
          <span className="muted">Projects group runs; files always stay in the selected local directory.</span>
          <div className="button-row">
            <button className="button ghost" type="button" onClick={onClose}>Cancel</button>
            <button
              className="button primary"
              type="button"
              disabled={!valid || submitting}
              onClick={() => {
                setSubmitting(true);
                void onCreate(name.trim(), root, mode === "create")
                  .catch(() => undefined)
                  .finally(() => setSubmitting(false));
              }}
            >
              {submitting ? <LoaderCircle className="spin" size={15} /> : <Plus size={15} />} Add project
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

function ProviderDialog({
  provider,
  onClose,
  onSave,
  onTest,
}: {
  provider: ProviderConfig;
  onClose: () => void;
  onSave: (config: Pick<ProviderConfig, "model" | "base_url" | "credential_file">) => Promise<ProviderConfig>;
  onTest: () => Promise<ProviderProbe>;
}) {
  const [model, setModel] = useState(provider.model);
  const [baseUrl, setBaseUrl] = useState(provider.base_url ?? "");
  const [credentialFile, setCredentialFile] = useState(provider.credential_file ?? "");
  const [working, setWorking] = useState<"save" | "test" | null>(null);
  const [probe, setProbe] = useState<ProviderProbe | null>(null);

  const config = {
    model: model.trim(),
    base_url: baseUrl.trim() || null,
    credential_file: credentialFile.trim() || null,
  };

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal provider-modal" role="dialog" aria-modal="true" aria-labelledby="provider-title">
        <div className="modal-heading">
          <div><p className="eyebrow">MODEL PROVIDER</p><h2 id="provider-title">Connection settings</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close model settings"><X size={17} /></button>
        </div>
        <div className={`credential-status ${provider.api_key_configured ? "ready" : "missing"}`}>
          {provider.api_key_configured ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}
          <div>
            <strong>{provider.api_key_configured ? "Credential source ready" : "Credential required"}</strong>
            <small>{provider.credential_source === "file" ? "Owner-only local file" : provider.credential_source === "environment" ? provider.credential_env : "Add a file path below or set OPENAI_API_KEY"}</small>
          </div>
        </div>
        <label className="field-label"><span>Model</span><input value={model} onChange={(event) => setModel(event.target.value)} /></label>
        <label className="field-label"><span>OpenAI-compatible base URL</span><input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.deepseek.com" /></label>
        <label className="field-label">
          <span>Credential file path</span>
          <input value={credentialFile} onChange={(event) => setCredentialFile(event.target.value)} placeholder="/absolute/path/to/owner-only-key-file" />
          <small>The file must contain one line and use owner-only permissions (chmod 600). Its content is never saved or returned.</small>
        </label>
        {probe && (
          <div className={`probe-result ${probe.ok ? "ready" : "failed"}`} role="status">
            {probe.ok ? <CheckCircle2 size={16} /> : <OctagonX size={16} />}
            <span><strong>{probe.ok ? "Native tool calling verified" : "Connection check failed"}</strong><small>{probe.detail} · {probe.latency_ms} ms</small></span>
          </div>
        )}
        <div className="modal-actions">
          <span className="muted">Settings can only change when no run is active or interrupted.</span>
          <div className="button-row">
            <button
              className="button"
              type="button"
              disabled={!config.model || Boolean(working)}
              onClick={() => {
                setWorking("test");
                setProbe(null);
                void onSave(config)
                  .then(() => onTest())
                  .then(setProbe)
                  .catch(() => undefined)
                  .finally(() => setWorking(null));
              }}
            >
              {working === "test" ? <LoaderCircle className="spin" size={15} /> : <Wifi size={15} />} Test connection
            </button>
            <button
              className="button primary"
              type="button"
              disabled={!config.model || Boolean(working)}
              onClick={() => {
                setWorking("save");
                void onSave(config)
                  .then(() => onClose())
                  .catch(() => undefined)
                  .finally(() => setWorking(null));
              }}
            >
              {working === "save" ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />} Save settings
            </button>
          </div>
        </div>
      </section>
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
  onProof,
}: {
  run: Run;
  events: RunEvent[];
  onAnswer: (answers: ClarificationAnswer[]) => void;
  onPlan: (decision: "approve" | "revise", feedback?: string) => void;
  onAction: (approved: boolean) => void;
  onCancel: () => void;
  onResume: () => void;
  onRollback: () => void;
  onProof: () => void;
}) {
  const state = presentState(run.state);
  return (
    <div className="run-stage">
      <div className="run-header">
        <div>
          <div className="run-header-meta">
            <StateBadge state={run.state} />
            {run.plan_gate && <PlanGateBadge gate={run.plan_gate} />}
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
          <PlanPanel plan={run.plan} gate={run.plan_gate} onDecision={onPlan} />
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
        {run.state === "succeeded" && <EvidenceBoard run={run} onProof={onProof} />}
      </div>
    </div>
  );
}

function ActivityFeed({ events, stateLabel }: { events: RunEvent[]; stateLabel: string }) {
  const end = useRef<HTMLDivElement>(null);
  const visible = events.filter((event) =>
    ["message", "tool.completed", "state.changed", "error"].includes(event.type),
  );
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [events.length]);
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

function PlanPanel({ plan, gate, onDecision }: { plan: TaskPlan; gate: PlanGate | null; onDecision: (decision: "approve" | "revise", feedback?: string) => void }) {
  const [revising, setRevising] = useState(false);
  const [feedback, setFeedback] = useState("");
  return (
    <div className="decision-panel plan-panel">
      <div className="decision-heading"><ClipboardCheck size={19} /><div><p>PLAN REVIEW</p><h3>{plan.summary}</h3></div></div>
      {gate && <PlanGateSummary gate={gate} />}
      <ol className="plan-steps">{plan.steps.map((step) => <li key={step.id}><span>{step.id}</span><div><strong>{step.title}</strong>{step.description && <small>{step.description}</small>}</div></li>)}</ol>
      {plan.impacted_files.length > 0 && <div className="impact-files"><span>PLANNED FILES</span>{plan.impacted_files.map((path) => <code key={path}>{path}</code>)}</div>}
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

function EvidenceBoard({ run, onProof }: { run: Run; onProof: () => void }) {
  return (
    <div className="evidence-board">
      <div className="evidence-seal"><CheckCircle2 size={24} /></div>
      <div><p className="eyebrow">COMPLETION EVIDENCE</p><h3>Work proven, not merely reported</h3><p>{run.verification?.summary}</p></div>
      <div className="evidence-actions"><div className="evidence-stats"><div><strong>{run.plan?.acceptance_checks.filter((check) => check.status === "passed").length ?? 0}</strong><span>checks passed</span></div><div><strong>{run.step_count}</strong><span>tool steps</span></div><div><strong>{run.repair_cycles}</strong><span>repair cycles</span></div></div><button className="button" type="button" onClick={onProof}><Fingerprint size={14} /> Proof Pack</button></div>
    </div>
  );
}

function Inspector({ run, events, diff, tab, onTab }: { run: Run | null; events: RunEvent[]; diff: string; tab: InspectorTab; onTab: (tab: InspectorTab) => void }) {
  const tabs: Array<{ id: InspectorTab; label: string; icon: typeof History }> = [
    { id: "timeline", label: "Timeline", icon: History },
    { id: "diff", label: "Diff", icon: FileDiff },
    { id: "checks", label: "Plan", icon: ClipboardCheck },
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
  return <div className="checks-view">{run.plan_gate && <PlanGateSummary gate={run.plan_gate} />}<div className="section-kicker">VISIBLE PLAN</div><ol className="plan-steps inspector-plan">{run.plan.steps.map((step) => <li key={step.id}><span>{step.status === "completed" ? <Check size={11} /> : step.id}</span><div><strong>{step.title}</strong>{step.description && <small>{step.description}</small>}</div></li>)}</ol>{run.plan.impacted_files.length > 0 && <div className="impact-files"><span>PLANNED FILES</span>{run.plan.impacted_files.map((path) => <code key={path}>{path}</code>)}</div>}<div className="section-kicker contract-kicker">ACCEPTANCE CONTRACT</div>{run.plan.acceptance_checks.map((check) => <article className={`check-row ${check.status}`} key={check.id}><div className="check-icon">{check.status === "passed" ? <Check size={14} /> : check.status === "failed" ? <X size={14} /> : <Circle size={10} />}</div><div><strong>{check.label}</strong>{check.command && <code>{check.command.join(" ")}</code>}{check.evidence && <pre>{check.evidence}</pre>}</div><span>{check.status}</span></article>)}</div>;
}

function PlanGateBadge({ gate }: { gate: PlanGate }) {
  return <span className={`plan-gate-badge ${gate.decision === "auto_approved" ? "fast" : "reviewed"}`}>{gate.decision === "auto_approved" ? <Zap size={10} /> : <ShieldCheck size={10} />}{gate.decision === "auto_approved" ? "Fast path" : `${gate.risk} risk`}</span>;
}

function PlanGateSummary({ gate }: { gate: PlanGate }) {
  return <div className={`plan-gate-summary ${gate.decision === "auto_approved" ? "fast" : "review"}`}><div>{gate.decision === "auto_approved" ? <Zap size={15} /> : <ShieldCheck size={15} />}<span><strong>{gate.decision === "auto_approved" ? "Low-risk fast path" : "Plan approval required"}</strong><small>Deterministic assessment · {gate.risk} risk</small></span></div><ul>{gate.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></div>;
}

function ProofPackDialog({ pack, runId, onClose }: { pack: ProofPack | null; runId: string; onClose: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal proof-modal" role="dialog" aria-modal="true" aria-labelledby="proof-title">
        <div className="modal-heading"><div><p className="eyebrow">AUDITABLE COMPLETION</p><h2 id="proof-title">Proof Pack</h2></div><button className="icon-button" type="button" onClick={onClose} aria-label="Close proof pack"><X size={17} /></button></div>
        {!pack ? <div className="proof-loading"><LoaderCircle className="spin" size={18} /> Assembling persisted evidence…</div> : <>
          <div className={`proof-verdict ${pack.proof_status}`}><div className="evidence-seal"><Fingerprint size={22} /></div><div><span>PROOF STATUS</span><strong>{pack.proof_status.replaceAll("_", " ")}</strong><small>{pack.verification?.summary ?? "Evidence is still being assembled."}</small></div><div><span>FRESH CHECKS</span><strong>{pack.checks_fresh ? "YES" : "NO"}</strong></div></div>
          <div className="proof-grid"><article><span>PLAN GATE</span><strong>{pack.plan_gate?.decision.replaceAll("_", " ") ?? "not assessed"}</strong><small>{pack.plan_gate?.reasons.join(" · ")}</small></article><article><span>CHANGE SCOPE</span><strong>{pack.changed_files.length} file{pack.changed_files.length === 1 ? "" : "s"}</strong><small>{pack.changed_files.join(" · ") || "No snapshots"} · {pack.diff_source.replaceAll("_", " ")}</small></article><article><span>ROLLBACK</span><strong>{pack.rollback.status}</strong><small>{pack.rollback.conflicts.length ? `${pack.rollback.conflicts.length} conflicts preserved` : "Conflict-aware"}</small></article><article><span>EVENT LEDGER</span><strong>{pack.event_count} events</strong><small>{pack.step_count} tool steps · {pack.repair_cycles} repairs</small></article></div>
          <div className="proof-section"><div className="section-kicker">REQUEST</div><p>{pack.task}</p></div>
          <div className="proof-section"><div className="section-kicker">ACCEPTANCE EVIDENCE</div>{pack.plan?.acceptance_checks.map((check) => <div className="proof-check" key={check.id}><CheckCircle2 size={14} /><span><strong>{check.label}</strong><small>{check.evidence || check.command?.join(" ") || "Awaiting evidence"}</small></span><em>{check.status}</em></div>) ?? <p className="muted">No completion contract yet.</p>}</div>
          <div className="digest-card"><Fingerprint size={15} /><span><small>STABLE EVIDENCE SHA-256</small><code>{pack.evidence_sha256}</code></span></div>
        </>}
        <div className="modal-actions"><span className="muted">The digest covers persisted plan, diff, checks, verdict, rollback state, and event ledger.</span><div className="button-row"><button className="button ghost" type="button" onClick={onClose}>Close</button><a className={`button primary ${pack ? "" : "disabled"}`} href={pack ? `/api/runs/${runId}/proof-pack.md` : undefined} download><Download size={14} /> Download Markdown</a></div></div>
      </section>
    </div>
  );
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
  if (event.type === "plan.gated") {
    const decision = String(event.payload.decision ?? "assessed").replaceAll("_", " ");
    const risk = String(event.payload.risk ?? "unknown");
    return `${decision} · ${risk} risk`;
  }
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

function formatTokens(value: number): string {
  if (value < 1_000) return String(value);
  const compact = value / 1_000;
  return `${compact >= 10 ? Math.round(compact) : compact.toFixed(1).replace(/\.0$/, "")}k`;
}

function clockTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}
