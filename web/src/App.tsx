import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronDown,
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
  PanelLeft,
  PanelRight,
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
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import ReactMarkdown from "react-markdown";
import {
  buildActivityChapters,
  isActiveState,
  parseDiff,
  presentState,
  shouldSubmitPrompt,
} from "./lib";
import type {
  ClarificationAnswer,
  DirectoryListing,
  PlanGate,
  Project,
  ProofPack,
  ProviderConfig,
  ProviderProbe,
  ProviderUpdate,
  Run,
  RunEvent,
  RunState,
  RunTarget,
  TaskPlan,
} from "./types";
import { useTraceForge } from "./useTraceForge";

type InspectorTab = "timeline" | "diff" | "checks" | "verifier";

function dialogFocusables(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>(
    "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), "
      + "textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
  ));
}

function useDialogFocus(onClose: () => void) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    const initial = dialog?.querySelector<HTMLElement>("[data-dialog-initial-focus]")
      ?? (dialog ? dialogFocusables(dialog)[0] : null)
      ?? dialog;
    initial?.focus();
    return () => {
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  const onDialogKeyDown = useCallback((event: ReactKeyboardEvent<HTMLElement>) => {
    const dialog = dialogRef.current;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeRef.current();
      return;
    }
    if (event.key !== "Tab" || !dialog) return;
    const focusable = dialogFocusables(dialog);
    const first = focusable[0];
    const last = focusable.at(-1);
    if (!first || !last) {
      event.preventDefault();
      dialog.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }, []);
  return { dialogRef, onDialogKeyDown };
}

function useDrawerFocus(open: boolean, onClose: () => void) {
  const drawerRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    drawerRef.current?.querySelector<HTMLElement>("[data-drawer-initial-focus]")?.focus();
    return () => {
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);
  const onDrawerKeyDown = useCallback((event: ReactKeyboardEvent<HTMLElement>) => {
    const drawer = drawerRef.current;
    if (event.key === "Escape") {
      event.preventDefault();
      closeRef.current();
      return;
    }
    if (event.key !== "Tab" || !drawer) return;
    const focusable = dialogFocusables(drawer);
    const first = focusable[0];
    const last = focusable.at(-1);
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }, []);
  return { drawerRef, onDrawerKeyDown };
}

export default function App() {
  const forge = useTraceForge();
  const [showComposer, setShowComposer] = useState(false);
  const [composerProjectId, setComposerProjectId] = useState<string | null>(null);
  const [showProject, setShowProject] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showProof, setShowProof] = useState(false);
  const [mobilePane, setMobilePane] = useState<"sidebar" | "inspector" | null>(null);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("timeline");

  useEffect(() => {
    if (forge.run?.state === "verifying") setInspectorTab("verifier");
    if (forge.run?.state === "succeeded") setInspectorTab("checks");
  }, [forge.run?.state]);

  useEffect(() => setShowProof(false), [forge.run?.id]);
  useEffect(() => {
    const closeObsoleteDrawer = () => setMobilePane((current) => {
      if (current === "sidebar" && window.innerWidth > 680) return null;
      if (current === "inspector" && window.innerWidth > 980) return null;
      return current;
    });
    window.addEventListener("resize", closeObsoleteDrawer);
    return () => window.removeEventListener("resize", closeObsoleteDrawer);
  }, []);

  const openDirectComposer = () => {
    setComposerProjectId(null);
    setShowComposer(true);
    setMobilePane(null);
  };
  const openProjectComposer = (projectId: string) => {
    setComposerProjectId(projectId);
    setShowComposer(true);
    setMobilePane(null);
  };
  const selectRun = (runId: string) => {
    forge.selectRun(runId);
    setShowComposer(false);
    setMobilePane(null);
  };
  const composerProject = forge.projects.find((project) => project.id === composerProjectId) ?? null;

  const addProject = async () => {
    const choice = await forge.chooseDirectory();
    if (!choice.supported) {
      setShowProject(true);
      return;
    }
    if (!choice.path) return;
    await forge.createProject(projectNameFromPath(choice.path), choice.path, false);
  };

  return (
    <div className="app-shell">
      <Header
        status={forge.status}
        connected={forge.connected}
        run={forge.run}
        providerReady={Boolean(forge.provider?.api_key_configured)}
        mobilePane={mobilePane}
        onHistory={() => setMobilePane((current) => current === "sidebar" ? null : "sidebar")}
        onInspector={() => setMobilePane((current) => current === "inspector" ? null : "inspector")}
        onSettings={() => setShowSettings(true)}
      />
      {forge.error && (
        <div className="global-error" role="alert">
          <AlertTriangle size={16} />
          <span>{systemMessageLabel(forge.error)}</span>
          <button type="button" aria-label="关闭错误提示" onClick={forge.clearError}><X size={15} /></button>
        </div>
      )}
      <main className="workspace-grid">
        <Sidebar
          runs={forge.runs}
          projects={forge.projects}
          selectedRunId={forge.selectedRunId}
          demoMode={forge.status?.mode === "demo"}
          onSelect={selectRun}
          onNewDirect={openDirectComposer}
          onNewProject={openProjectComposer}
          onAddProject={() => void addProject().catch(() => undefined)}
          mobileOpen={mobilePane === "sidebar"}
          onMobileClose={() => setMobilePane(null)}
        />
        <section className="main-stage">
          {!forge.run || showComposer ? (
            <TaskComposer
              key={`${forge.status?.suggested_task ?? "standard"}-${composerProjectId ?? "direct"}`}
              suggestedTask={forge.status?.suggested_task ?? ""}
              defaultWorkspace={forge.status?.workspace ?? ""}
              project={composerProject}
              demoMode={forge.status?.mode === "demo"}
              providerReady={Boolean(forge.provider?.api_key_configured)}
              onOpenSettings={() => setShowSettings(true)}
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
          mobileOpen={mobilePane === "inspector"}
          onMobileClose={() => setMobilePane(null)}
        />
      </main>
      {mobilePane && (
        <button
          className="drawer-backdrop"
          type="button"
          aria-label="关闭侧边面板"
          onClick={() => setMobilePane(null)}
        />
      )}
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
      {showProject && (
        <ProjectDialog
          initialDirectory={forge.status?.last_workspace ?? forge.status?.workspace ?? ""}
          onClose={() => setShowProject(false)}
          onListDirectories={forge.listDirectories}
          onCreate={async (name, root, createDirectory) => {
            await forge.createProject(name, root, createDirectory);
            setShowProject(false);
          }}
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
  mobilePane,
  onHistory,
  onInspector,
  onSettings,
}: {
  status: ReturnType<typeof useTraceForge>["status"];
  connected: boolean;
  run: Run | null;
  providerReady: boolean;
  mobilePane: "sidebar" | "inspector" | null;
  onHistory: () => void;
  onInspector: () => void;
  onSettings: () => void;
}) {
  const localReady = Boolean(status);
  const connectionOnline = run ? connected : localReady;
  const connectionLabel = run
    ? connected ? "实时" : "正在重连"
    : localReady ? "本地就绪" : "正在连接";
  const connectionTitle = run
    ? connected ? "已连接实时运行事件" : "正在重连持久化运行事件"
    : localReady
      ? providerReady
        ? "本地服务已就绪，模型凭证已配置"
        : "本地服务已就绪，仍需配置模型"
      : "正在连接本地 TraceForge 服务";
  return (
    <header className="topbar">
      <button
        className="icon-button mobile-nav-button history-toggle"
        type="button"
        onClick={onHistory}
        aria-label="任务与项目"
        aria-expanded={mobilePane === "sidebar"}
      ><PanelLeft size={18} /></button>
      <div className="brand">
        <div className="brand-mark"><Hammer size={18} /></div>
        <div>
          <strong>TraceForge</strong>
          <span>证据驱动的编程智能体</span>
        </div>
      </div>
      <div className="topbar-context">
        <button
          className="icon-button mobile-nav-button inspector-toggle"
          type="button"
          onClick={onInspector}
          aria-label="运行证据"
          aria-expanded={mobilePane === "inspector"}
        ><PanelRight size={18} /></button>
        <div className="context-item workspace-path" title={run?.workspace ?? status?.last_workspace}>
          <GitBranch size={14} />
          <span>{run?.workspace ?? status?.last_workspace ?? "正在连接…"}</span>
        </div>
        <div className="context-item"><Sparkles size={14} /><span>{status?.model ?? "—"}</span></div>
        <div
          className={`sandbox-status ${status?.sandbox.enforced ? "enforced" : status ? "degraded" : "pending"}`}
          title={status ? sandboxDetailLabel(status.sandbox.detail) : "正在检测命令沙箱…"}
        >
          <ShieldCheck size={13} />
          <span>{!status ? "检测中" : status.sandbox.enforced ? status.sandbox.backend : "仅策略限制"}</span>
        </div>
        {run && <div className="context-item"><Wrench size={14} /><span>{run.step_count} 步</span></div>}
        {run && (
          <div
            className="context-item"
            title={`预计已使用 ${run.context_tokens.toLocaleString()} / ${run.context_limit.toLocaleString()} 个上下文 token`}
          >
            <Gauge size={14} />
            <span>{formatTokens(run.context_tokens)} / {formatTokens(run.context_limit)} 上下文</span>
          </div>
        )}
        <div
          className={`connection ${connectionOnline ? "online" : "offline"}`}
          title={connectionTitle}
        >
          {connectionOnline ? <Wifi size={14} /> : <WifiOff size={14} />}
          {connectionLabel}
        </div>
        <button
          className={`icon-button settings-button ${providerReady ? "" : "needs-attention"}`}
          type="button"
          onClick={onSettings}
          title={providerReady ? "模型设置" : "配置模型凭证"}
          aria-label="模型设置"
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
  demoMode,
  onNewDirect,
  onNewProject,
  onAddProject,
  mobileOpen,
  onMobileClose,
}: {
  runs: Run[];
  projects: Project[];
  selectedRunId: string | null;
  onSelect: (id: string) => void;
  demoMode: boolean;
  onNewDirect: () => void;
  onNewProject: (projectId: string) => void;
  onAddProject: () => void;
  mobileOpen: boolean;
  onMobileClose: () => void;
}) {
  const [collapsedProjects, setCollapsedProjects] = useState<Set<string>>(new Set());
  const directRuns = runs.filter((run) => run.project_id === null);
  const { drawerRef, onDrawerKeyDown } = useDrawerFocus(mobileOpen, onMobileClose);

  const toggleProject = (projectId: string) => {
    setCollapsedProjects((current) => {
      const next = new Set(current);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  };

  return (
    <aside
      ref={drawerRef}
      className={`sidebar panel-edge ${mobileOpen ? "mobile-open" : ""}`}
      role={mobileOpen ? "dialog" : undefined}
      aria-modal={mobileOpen || undefined}
      aria-label={mobileOpen ? "任务与项目" : undefined}
      onKeyDown={onDrawerKeyDown}
    >
      <div className="drawer-mobile-heading">
        <strong>任务与项目</strong>
        <button className="icon-button" type="button" onClick={onMobileClose} aria-label="关闭任务与项目" data-drawer-initial-focus><X size={17} /></button>
      </div>
      <div className="sidebar-actions">
        <button className="sidebar-action primary" type="button" onClick={onNewDirect}>
          <Plus size={16} /> 新建任务
        </button>
        <button
          className="sidebar-action"
          type="button"
          onClick={onAddProject}
          disabled={demoMode}
          title={demoMode ? "固定演示不连接真实项目；请运行 traceforge serve" : "使用系统选择器添加项目"}
        >
          <FolderOpen size={16} /> 添加项目
        </button>
      </div>
      {demoMode && (
        <div className="demo-mode-note">
          <Sparkles size={14} />
          <span><strong>固定演示</strong><small>只运行预置案例，不会把其他输入套进脚本。</small></span>
        </div>
      )}
      <div className="run-list" aria-label="任务与项目">
        {runs.length === 0 && <p className="muted empty-copy">还没有运行记录。</p>}
        {directRuns.length > 0 && <div className="sidebar-section-label"><History size={14} /> 直接任务</div>}
        {directRuns.map((run) => (
          <SidebarRun key={run.id} run={run} selected={selectedRunId === run.id} onSelect={onSelect} />
        ))}
        {projects.map((project) => {
          const projectRuns = runs.filter((run) => run.project_id === project.id);
          const collapsed = collapsedProjects.has(project.id);
          return (
            <section className="project-group" key={project.id}>
              <div className="project-heading">
                <button
                  className="project-toggle"
                  type="button"
                  onClick={() => toggleProject(project.id)}
                  aria-expanded={!collapsed}
                  title={project.root}
                >
                  <ChevronDown size={15} />
                  {collapsed ? <Folder size={16} /> : <FolderOpen size={16} />}
                  <span><strong>{project.name}</strong><small>{projectRuns.length} 个任务</small></span>
                </button>
                <button
                  className="icon-button project-add"
                  type="button"
                  onClick={() => onNewProject(project.id)}
                  aria-label={`在 ${project.name} 中新建任务`}
                  title={`在 ${project.name} 中新建任务`}
                  disabled={demoMode}
                ><Plus size={15} /></button>
              </div>
              {!collapsed && (
                <div className="project-runs">
                  {projectRuns.length === 0 && <p className="project-empty">项目中暂无任务</p>}
                  {projectRuns.map((run) => (
                    <SidebarRun key={run.id} run={run} selected={selectedRunId === run.id} onSelect={onSelect} nested />
                  ))}
                </div>
              )}
            </section>
          );
        })}
      </div>
      <div className="sidebar-footer">
        <ShieldCheck size={14} />
        <span>仅在本地 · 受工作区边界保护</span>
      </div>
    </aside>
  );
}

function SidebarRun({
  run,
  selected,
  nested = false,
  onSelect,
}: {
  run: Run;
  selected: boolean;
  nested?: boolean;
  onSelect: (id: string) => void;
}) {
  const state = presentState(run.state);
  return (
    <button
      type="button"
      className={`run-item ${nested ? "nested" : ""} ${selected ? "selected" : ""}`}
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
        <span className="run-scope">{nested ? "项目任务" : "独立目录"}</span>
      </div>
    </button>
  );
}

function TaskComposer({
  suggestedTask,
  defaultWorkspace,
  project,
  demoMode,
  providerReady,
  onOpenSettings,
  onSubmit,
  onCancel,
  canCancel,
}: {
  suggestedTask: string;
  defaultWorkspace: string;
  project: Project | null;
  demoMode: boolean;
  providerReady: boolean;
  onOpenSettings: () => void;
  onSubmit: (task: string, verifier: boolean, target: RunTarget) => Promise<void>;
  onCancel: () => void;
  canCancel: boolean;
}) {
  const [task, setTask] = useState(suggestedTask);
  const [verifier, setVerifier] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const targetLabel = project ? project.name : demoMode ? "固定演示" : "直接任务";
  return (
    <div className="composer-wrap">
      <div className="hero-symbol"><Code2 size={30} /></div>
      <p className="eyebrow">{targetLabel} · 新建证据任务</p>
      <h1>{project ? `你希望在 ${project.name} 中完成什么？` : "你希望 TraceForge 完成并证明什么？"}</h1>
      <p className="hero-copy">
        {project
          ? `任务会在 ${project.root} 中执行，并归入这个项目。`
          : demoMode
            ? "这是可重复的固定导览，只接受下方预置案例；真实任务请使用 traceforge serve。"
            : "只需描述结果。TraceForge 会在默认路径下自动创建独立目录，并用证据证明完成情况。"}
      </p>
      <form
        className="task-composer"
        onSubmit={(event) => {
          event.preventDefault();
          if (!task.trim()) return;
          setSubmitting(true);
          const target: RunTarget = project
            ? { project_id: project.id }
            : demoMode
              ? {}
              : { create_direct_workspace: true };
          void onSubmit(task.trim(), verifier, target)
            .catch(() => undefined)
            .finally(() => setSubmitting(false));
        }}
      >
        {!providerReady && (
          <button className="setup-callout" type="button" onClick={onOpenSettings}>
            <AlertTriangle size={15} />
            <span><strong>需要配置模型</strong><small>添加本地凭证文件，并验证原生工具调用。</small></span>
            <ArrowRight size={15} />
          </button>
        )}
        <div className="composer-target" title={project?.root ?? defaultWorkspace}>
          {project ? <FolderOpen size={16} /> : <Code2 size={16} />}
          <span><strong>{targetLabel}</strong><small>{project?.root ?? (demoMode ? defaultWorkspace : "自动创建独立任务目录")}</small></span>
        </div>
        <textarea
          autoFocus
          value={task}
          onChange={(event) => setTask(event.target.value)}
          readOnly={demoMode}
          aria-readonly={demoMode}
          onKeyDown={(event) => {
            if (
              shouldSubmitPrompt({
                key: event.key,
                shiftKey: event.shiftKey,
                isComposing: event.nativeEvent.isComposing,
              })
              && providerReady
              && !submitting
            ) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder="例如：修复多租户缓存串读，保持 TTL 语义，补充回归测试并确保全部检查通过。"
          rows={6}
        />
        <div className="composer-actions">
          <label className="toggle-row">
            <input type="checkbox" checked={verifier} onChange={(event) => setVerifier(event.target.checked)} />
            <span className="toggle" />
            <span><strong>独立验证</strong><small>检查通过后进行只读审查</small></span>
          </label>
          <div className="button-row">
            {canCancel && <button className="button ghost" type="button" onClick={onCancel}>取消</button>}
            <button
              className="button primary"
              type="submit"
              disabled={!task.trim() || !providerReady || submitting}
            >
              {submitting ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={16} />}
              开始任务
            </button>
          </div>
        </div>
      </form>
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
            <FolderOpen size={14} /> 浏览
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
  const { dialogRef, onDialogKeyDown } = useDialogFocus(onClose);

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
      <section ref={dialogRef} className="modal directory-modal" role="dialog" aria-modal="true" aria-labelledby="directory-title" tabIndex={-1} onKeyDown={onDialogKeyDown}>
        <div className="modal-heading">
          <div><p className="eyebrow">本地工作区</p><h2 id="directory-title">选择目录</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭目录浏览器"><X size={17} /></button>
        </div>
        <div className="path-entry">
          <input value={path} onChange={(event) => setPath(event.target.value)} aria-label="目录路径" data-dialog-initial-focus />
          <button className="button" type="button" onClick={() => load(path)}>前往</button>
        </div>
        {error && <p className="inline-error">{systemMessageLabel(error)}</p>}
        <div className="directory-list">
          {loading && <div className="thinking-row"><LoaderCircle className="spin" size={15} /> 正在加载…</div>}
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
          {!loading && listing && listing.children.length === 0 && <p className="muted empty-copy">没有可见的子目录。</p>}
        </div>
        <div className="modal-actions">
          <span className="selected-path" title={listing?.current}>{listing?.current ?? "请选择可读取的目录"}</span>
          <div className="button-row">
            <button className="button ghost" type="button" onClick={onClose}>取消</button>
            <button className="button primary" type="button" disabled={!listing} onClick={() => listing && onChoose(listing.current)}>选择当前目录</button>
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
  const { dialogRef, onDialogKeyDown } = useDialogFocus(onClose);
  const root = mode === "create"
    ? `${directory.replace(/\/$/, "")}/${folderName.trim()}`
    : directory;
  const valid = Boolean(name.trim() && directory.trim() && (mode === "open" || folderName.trim()));

  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={dialogRef} className="modal" role="dialog" aria-modal="true" aria-labelledby="project-title" tabIndex={-1} onKeyDown={onDialogKeyDown}>
        <div className="modal-heading">
          <div><p className="eyebrow">项目工作区</p><h2 id="project-title">添加项目</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭项目对话框"><X size={17} /></button>
        </div>
        <div className="segmented wide" aria-label="项目目录方式">
          <button type="button" className={mode === "open" ? "active" : ""} onClick={() => setMode("open")}>打开已有目录</button>
          <button type="button" className={mode === "create" ? "active" : ""} onClick={() => setMode("create")}>创建空目录</button>
        </div>
        <label className="field-label"><span>项目名称</span><input value={name} onChange={(event) => setName(event.target.value)} data-dialog-initial-focus /></label>
        <DirectoryField
          label={mode === "create" ? "父目录" : "已有目录"}
          value={directory}
          onChange={setDirectory}
          onListDirectories={onListDirectories}
        />
        {mode === "create" && (
          <label className="field-label"><span>新文件夹名称</span><input value={folderName} onChange={(event) => setFolderName(event.target.value)} placeholder="traceforge-project" /></label>
        )}
        <div className="path-preview"><span>项目根目录</span><code>{root}</code></div>
        <div className="modal-actions">
          <span className="muted">项目用于归组运行记录；文件始终留在所选本地目录中。</span>
          <div className="button-row">
            <button className="button ghost" type="button" onClick={onClose}>取消</button>
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
              {submitting ? <LoaderCircle className="spin" size={15} /> : <Plus size={15} />} 添加项目
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
  onSave: (config: ProviderUpdate) => Promise<ProviderConfig>;
  onTest: () => Promise<ProviderProbe>;
}) {
  const [model, setModel] = useState(provider.model);
  const [baseUrl, setBaseUrl] = useState(provider.base_url ?? "");
  const [apiKey, setApiKey] = useState("");
  const [credentialFile, setCredentialFile] = useState(provider.credential_file ?? "");
  const [working, setWorking] = useState<"save" | "test" | null>(null);
  const [probe, setProbe] = useState<ProviderProbe | null>(null);
  const { dialogRef, onDialogKeyDown } = useDialogFocus(onClose);

  const config: ProviderUpdate = {
    model: model.trim(),
    base_url: baseUrl.trim() || null,
    credential_file: apiKey.trim() ? null : credentialFile.trim() || null,
    ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
  };

  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={dialogRef} className="modal provider-modal" role="dialog" aria-modal="true" aria-labelledby="provider-title" tabIndex={-1} onKeyDown={onDialogKeyDown}>
        <div className="modal-heading">
          <div><p className="eyebrow">模型服务</p><h2 id="provider-title">连接设置</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭模型设置"><X size={17} /></button>
        </div>
        <div className={`credential-status ${provider.api_key_configured ? "ready" : "missing"}`}>
          {provider.api_key_configured ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}
          <div>
            <strong>{provider.api_key_configured ? "凭证来源已就绪" : "需要凭证"}</strong>
            <small>{provider.credential_source === "file" ? "已安全保存在仅当前用户可读的本地文件" : provider.credential_source === "environment" ? provider.credential_env : "在下方输入 API Key，或设置 OPENAI_API_KEY"}</small>
          </div>
        </div>
        <label className="field-label"><span>模型</span><input value={model} onChange={(event) => setModel(event.target.value)} data-dialog-initial-focus /></label>
        <label className="field-label"><span>OpenAI 兼容接口地址</span><input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.deepseek.com" /></label>
        <label className="field-label">
          <span>API Key</span>
          <input type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={provider.api_key_configured ? "已配置；留空则保持不变" : "输入模型服务的 API Key"} />
          <small>只写入本机仅当前用户可读的私密文件；页面、数据库与运行记录都不会保存或回显 Key。</small>
        </label>
        <details className="advanced-settings">
          <summary>高级：使用已有凭证文件</summary>
          <label className="field-label">
            <span>凭证文件路径</span>
            <input value={credentialFile} onChange={(event) => setCredentialFile(event.target.value)} placeholder="/absolute/path/to/owner-only-key-file" disabled={Boolean(apiKey.trim())} />
            <small>文件必须只有一行，并使用仅所有者权限（chmod 600）。输入 API Key 时会忽略此路径。</small>
          </label>
        </details>
        {probe && (
          <div className={`probe-result ${probe.ok ? "ready" : "failed"}`} role="status">
            {probe.ok ? <CheckCircle2 size={16} /> : <OctagonX size={16} />}
            <span><strong>{probe.ok ? "原生工具调用已验证" : "连接检查失败"}</strong><small>{systemMessageLabel(probe.detail)} · {probe.latency_ms} 毫秒</small></span>
          </div>
        )}
        <div className="modal-actions">
          <span className="muted">任务停止或安全暂停时可以修改设置；继续任务会使用新连接。</span>
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
              {working === "test" ? <LoaderCircle className="spin" size={15} /> : <Wifi size={15} />} 测试连接
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
              {working === "save" ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />} 保存设置
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
  const [confirmRollback, setConfirmRollback] = useState(false);
  return (
    <div className="run-stage">
      <div className="run-header">
        <div>
          <div className="run-header-meta">
            <StateBadge state={run.state} />
            {run.plan_gate && <PlanGateBadge gate={run.plan_gate} />}
            <span>运行 {run.id.slice(0, 8).toUpperCase()}</span>
          </div>
          <h2>{run.task}</h2>
        </div>
        <div className="button-row">
          {isActiveState(run.state) && (
            <button className="button danger-ghost" type="button" onClick={onCancel}><Square size={14} /> 停止</button>
          )}
          {run.state === "interrupted" && (
            <button className="button" type="button" onClick={onResume}><Play size={14} /> 继续</button>
          )}
          {["succeeded", "failed", "cancelled", "interrupted"].includes(run.state) && (
            <button className="button ghost" type="button" onClick={() => setConfirmRollback(true)}><RotateCcw size={14} /> 回滚</button>
          )}
        </div>
      </div>
      <div className="phase-ribbon">
        {(["计划", "执行", "验证", "证据"] as const).map((label, index) => {
          const current = phaseIndex(run.state);
          return (
            <div className={index < current ? "done" : index === current ? "current" : ""} key={label}>
              <span>{index < current ? <Check size={12} /> : index + 1}</span>{label}
            </div>
          );
        })}
      </div>
      <ActivityFeed events={events} state={run.state} />
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
          <Notice icon={<Pause size={18} />} title="任务已安全暂停">
            工作区和运行历史均已保留。如有需要，请先修复连接，再点击“继续”；
            未完成的命令不会被自动重放。
          </Notice>
        )}
        {run.state === "cancelled" && (
          <Notice icon={<Square size={18} />} title="任务已停止">
            后续动作不会再执行。已经写入的文件修改仍保留在工作区；如需恢复，
            请使用“回滚”处理本次运行记录的快照。
          </Notice>
        )}
        {run.error && <Notice icon={<OctagonX size={18} />} title={run.state === "interrupted" ? "暂停原因" : "停止原因"} danger={run.state !== "interrupted"}>{systemMessageLabel(run.error)}</Notice>}
        {run.state === "succeeded" && <EvidenceBoard run={run} onProof={onProof} />}
      </div>
      {confirmRollback && (
        <RollbackDialog
          onClose={() => setConfirmRollback(false)}
          onConfirm={() => {
            setConfirmRollback(false);
            onRollback();
          }}
        />
      )}
    </div>
  );
}

function RollbackDialog({ onClose, onConfirm }: { onClose: () => void; onConfirm: () => void }) {
  const { dialogRef, onDialogKeyDown } = useDialogFocus(onClose);
  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={dialogRef} className="modal" role="dialog" aria-modal="true" aria-labelledby="rollback-title" tabIndex={-1} onKeyDown={onDialogKeyDown}>
        <div className="modal-heading">
          <div><p className="eyebrow">冲突感知回滚</p><h2 id="rollback-title">回滚本次运行？</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭回滚确认"><X size={17} /></button>
        </div>
        <p className="modal-copy">
          TraceForge 会恢复本次运行修改过的文件。用户之后的编辑会作为冲突保留；
          回滚一旦完成，无法自动重做。
        </p>
        <div className="modal-actions">
          <span>只处理本次运行记录的快照。</span>
          <div className="button-row">
            <button className="button ghost" type="button" onClick={onClose} data-dialog-initial-focus>取消</button>
            <button className="button warning" type="button" onClick={onConfirm}>
              <RotateCcw size={14} /> 回滚文件
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

function ActivityFeed({ events, state }: { events: RunEvent[]; state: RunState }) {
  const end = useRef<HTMLDivElement>(null);
  const chapters = useMemo(() => buildActivityChapters(events), [events]);
  const latestId = chapters.at(-1)?.id ?? null;
  const [expanded, setExpanded] = useState<string | null>(null);
  useEffect(() => {
    if (latestId) setExpanded(latestId);
  }, [latestId]);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [events.length]);
  return (
    <div className="activity-feed">
      {chapters.length === 0 && (
        <div className="thinking-row"><LoaderCircle className="spin" size={16} /><span>{presentState(state).label}…</span></div>
      )}
      {chapters.map((chapter, index) => {
        const current = index === chapters.length - 1 && isActiveState(state);
        const PhaseIcon = chapter.phase === "planning" ? ClipboardCheck : chapter.phase === "building" ? Hammer : ShieldCheck;
        const toolCount = chapter.events.filter((event) => event.type === "tool.completed").length;
        return (
          <section className={`activity-chapter ${expanded === chapter.id ? "expanded" : ""}`} key={chapter.id}>
            <button
              className="chapter-heading"
              type="button"
              aria-expanded={expanded === chapter.id}
              onClick={() => setExpanded((value) => value === chapter.id ? null : chapter.id)}
            >
              <span className={`chapter-icon ${current ? "live" : "complete"}`}>{current ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />}</span>
              <span className="chapter-title"><small>{activityPhaseLabel(chapter.phase)}</small><strong>{chapter.label}</strong></span>
              <span className="chapter-meta"><PhaseIcon size={12} /> {toolCount} 个工具动作 · {chapter.events.length} 条事件</span>
              <ChevronDown className="chapter-chevron" size={14} />
            </button>
            {expanded === chapter.id && <div className="chapter-body">{chapter.events.map((event) => <ActivityItem event={event} key={event.seq} />)}</div>}
          </section>
        );
      })}
      <div ref={end} />
    </div>
  );
}

function ActivityItem({ event }: { event: RunEvent }) {
  if (event.type === "message") {
    return (
      <article className="activity message-card">
        <div className="activity-icon"><MessageSquareMore size={15} /></div>
        <div><span className="activity-label">{agentPhaseLabel(String(event.payload.phase ?? "agent"))}</span><ReactMarkdown>{String(event.payload.content ?? "")}</ReactMarkdown></div>
      </article>
    );
  }
  if (event.type === "tool.completed") {
    return <ToolActivityItem event={event} />;
  }
  if (event.type === "plan.gated") {
    const decision = String(event.payload.decision ?? "assessed");
    const reasons = Array.isArray(event.payload.reasons) ? event.payload.reasons.map(String) : [];
    return (
      <article className="activity evidence-activity policy-activity">
        <div className="activity-icon"><Zap size={15} /></div>
        <div><span className="activity-label">确定性计划门</span><strong>{planDecisionLabel(decision)} · {riskLabel(String(event.payload.risk ?? "unknown"))}</strong>{reasons.length > 0 && <p>{reasons.map(planGateReasonLabel).join(" · ")}</p>}</div>
      </article>
    );
  }
  if (event.type === "verification.completed") {
    return (
      <article className="activity evidence-activity verifier-activity">
        <div className="activity-icon"><ShieldCheck size={15} /></div>
        <div><span className="activity-label">独立验证结论</span><strong>{verdictLabel(String(event.payload.verdict ?? "inconclusive"))}</strong><p>{String(event.payload.summary ?? "没有记录摘要。")}</p></div>
      </article>
    );
  }
  if (event.type === "run.resumed") {
    const repaired = Number(event.payload.incomplete_tool_calls_repaired ?? 0);
    return (
      <article className="activity evidence-activity recovery-activity">
        <div className="activity-icon"><RotateCcw size={15} /></div>
        <div><span className="activity-label">持久化恢复</span><strong>{recoveryStrategyLabel(String(event.payload.strategy ?? "resumed"))}</strong><p>{repaired > 0 ? `${repaired} 个未完成工具调用已关闭且未重放` : "执行任何新动作前会先检查当前工作区。"}</p></div>
      </article>
    );
  }
  if (event.type === "repair.started") {
    return (
      <article className="activity evidence-activity repair-activity">
        <div className="activity-icon"><Wrench size={15} /></div>
        <div><span className="activity-label">有界修复</span><strong>第 {String(event.payload.cycle ?? "?")} / {String(event.payload.limit ?? "?")} 轮</strong><p>{String(event.payload.summary ?? "独立验证要求进行修复。")}</p></div>
      </article>
    );
  }
  if (event.type === "model.retry") {
    return (
      <article className="activity evidence-activity recovery-activity">
        <div className="activity-icon"><Wifi size={15} /></div>
        <div><span className="activity-label">模型连接恢复</span><strong>第 {String(event.payload.next_attempt ?? "?")} / {String(event.payload.max_attempts ?? "?")} 次尝试</strong><p>{providerErrorLabel(String(event.payload.category ?? "provider"))} · {String(event.payload.delay_seconds ?? "?")} 秒后重试</p></div>
      </article>
    );
  }
  if (event.type === "error") {
    const recoverable = event.payload.recoverable === true;
    return <Notice icon={<OctagonX size={17} />} title={recoverable ? "可恢复暂停" : "错误"} danger={!recoverable}>{systemMessageLabel(String(event.payload.message ?? "未知错误"))}</Notice>;
  }
  return null;
}

function ToolActivityItem({ event }: { event: RunEvent }) {
  const call = (event.payload.call ?? {}) as { name?: string; arguments?: Record<string, unknown> };
  const result = (event.payload.result ?? {}) as { ok?: boolean; output?: string; error?: string; metadata?: Record<string, unknown> };
  const sandbox = result.metadata?.sandbox as { status?: string; backend?: string } | undefined;
  const [expanded, setExpanded] = useState(!result.ok);
  return (
    <article className={`activity tool-card ${result.ok ? "ok" : "bad"}`}>
      <div className="activity-icon"><TerminalSquare size={15} /></div>
      <div className="tool-body">
        <button className="tool-summary" type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
          <span className="tool-summary-copy"><span className="tool-title"><code>{call.name ?? "工具"}</code><em>{result.ok ? "已完成" : "失败"}</em></span><span className="tool-args">{formatArguments(call.arguments)}</span></span>
          {sandbox && <span className={`sandbox-evidence ${sandbox.status ?? "not_used"}`}>{sandbox.status === "enforced" ? sandbox.backend : sandboxStatusLabel(sandbox.status ?? "not_used")}</span>}
          <ChevronDown className={`tool-chevron ${expanded ? "expanded" : ""}`} size={13} />
        </button>
        {expanded && (result.output || result.error) && <pre>{result.error ?? result.output}</pre>}
      </div>
    </article>
  );
}

function ClarificationPanel({ request, onSubmit }: { request: NonNullable<Run["clarification"]>; onSubmit: (answers: ClarificationAnswer[]) => void }) {
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const complete = request.questions.every((question) => selected[question.id] || custom[question.id]?.trim());
  return (
    <div className="decision-panel clarification-panel">
      <div className="decision-heading"><MessageSquareMore size={19} /><div><p>需求澄清 · 第 {request.round} 轮</p><h3>这些选择会影响具体实现</h3></div></div>
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
                <span><strong>{option.label}{option.recommended && <em>推荐</em>}</strong><small>{option.description}</small></span>
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
      <div className="decision-actions"><span className="muted">TraceForge 会依据这些选择重新规划。</span><button className="button primary" type="button" disabled={!complete} onClick={() => onSubmit(request.questions.map((question) => custom[question.id]?.trim() ? { question_id: question.id, custom_text: custom[question.id].trim() } : { question_id: question.id, option_id: selected[question.id] }))}><Send size={15} /> 继续</button></div>
    </div>
  );
}

function PlanPanel({ plan, gate, onDecision }: { plan: TaskPlan; gate: PlanGate | null; onDecision: (decision: "approve" | "revise", feedback?: string) => void }) {
  const [revising, setRevising] = useState(false);
  const [feedback, setFeedback] = useState("");
  return (
    <div className="decision-panel plan-panel">
      <div className="decision-heading"><ClipboardCheck size={19} /><div><p>计划审批</p><h3>{plan.summary}</h3></div></div>
      {gate && <PlanGateSummary gate={gate} />}
      <ol className="plan-steps">{plan.steps.map((step) => <li key={step.id}><span>{step.id}</span><div><strong>{step.title}</strong>{step.description && <small>{step.description}</small>}</div></li>)}</ol>
      {plan.impacted_files.length > 0 && <div className="impact-files"><span>计划修改的文件</span>{plan.impacted_files.map((path) => <code key={path}>{path}</code>)}</div>}
      <div className="plan-checks"><p>完成契约</p>{plan.acceptance_checks.map((check) => <div key={check.id}><ShieldCheck size={14} /><span>{check.label}</span>{check.command && <code>{check.command.join(" ")}</code>}</div>)}</div>
      {plan.risks.length > 0 && <div className="risk-strip"><AlertTriangle size={15} /><span>{plan.risks.join(" · ")}</span></div>}
      {revising && <textarea className="revision-input" autoFocus placeholder="这份计划需要怎样调整？" value={feedback} onChange={(event) => setFeedback(event.target.value)} />}
      <div className="decision-actions"><span className="muted">尚未修改任何文件。</span><div className="button-row">{revising ? <><button className="button ghost" type="button" onClick={() => setRevising(false)}>返回</button><button className="button" type="button" disabled={!feedback.trim()} onClick={() => onDecision("revise", feedback.trim())}>提交修改意见</button></> : <><button className="button ghost" type="button" onClick={() => setRevising(true)}>调整计划</button><button className="button primary" type="button" onClick={() => onDecision("approve")}><Check size={15} /> 批准并执行</button></>}</div></div>
    </div>
  );
}

function ApprovalPanel({ approval, onDecision }: { approval: NonNullable<Run["pending_approval"]>; onDecision: (approved: boolean) => void }) {
  return (
    <div className="decision-panel approval-panel">
      <div className="decision-heading"><AlertTriangle size={19} /><div><p>动作审批 · {riskLabel(approval.risk)}</p><h3>{approval.summary}</h3></div></div>
      <p className="approval-reason">{approval.reason}</p>
      <pre>{JSON.stringify(approval.tool_call.arguments, null, 2)}</pre>
      <div className="decision-actions"><span className="muted">未知命令绝不会静默执行。</span><div className="button-row"><button className="button danger-ghost" type="button" onClick={() => onDecision(false)}>拒绝</button><button className="button warning" type="button" onClick={() => onDecision(true)}>仅运行一次</button></div></div>
    </div>
  );
}

function EvidenceBoard({ run, onProof }: { run: Run; onProof: () => void }) {
  return (
    <div className="evidence-board">
      <div className="evidence-seal"><CheckCircle2 size={24} /></div>
      <div><p className="eyebrow">完成证据</p><h3>工作已被证明，而不只是宣称完成</h3><p>{run.verification?.summary}</p></div>
      <div className="evidence-actions"><div className="evidence-stats"><div><strong>{run.plan?.acceptance_checks.filter((check) => check.status === "passed").length ?? 0}</strong><span>项检查通过</span></div><div><strong>{run.step_count}</strong><span>个工具步骤</span></div><div><strong>{run.repair_cycles}</strong><span>轮修复</span></div></div><button className="button" type="button" onClick={onProof}><Fingerprint size={14} /> 证据包</button></div>
    </div>
  );
}

function Inspector({
  run,
  events,
  diff,
  tab,
  onTab,
  mobileOpen,
  onMobileClose,
}: {
  run: Run | null;
  events: RunEvent[];
  diff: string;
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  mobileOpen: boolean;
  onMobileClose: () => void;
}) {
  const { drawerRef, onDrawerKeyDown } = useDrawerFocus(mobileOpen, onMobileClose);
  const tabs: Array<{ id: InspectorTab; label: string; icon: typeof History }> = [
    { id: "timeline", label: "时间线", icon: History },
    { id: "diff", label: "差异", icon: FileDiff },
    { id: "checks", label: "计划", icon: ClipboardCheck },
    { id: "verifier", label: "验证", icon: ShieldCheck },
  ];
  return (
    <aside
      ref={drawerRef}
      className={`inspector panel-edge ${mobileOpen ? "mobile-open" : ""}`}
      role={mobileOpen ? "dialog" : undefined}
      aria-modal={mobileOpen || undefined}
      aria-label={mobileOpen ? "运行证据" : undefined}
      onKeyDown={onDrawerKeyDown}
    >
      <div className="drawer-mobile-heading inspector-drawer-heading">
        <strong>运行证据</strong>
        <button className="icon-button" type="button" onClick={onMobileClose} aria-label="关闭运行证据" data-drawer-initial-focus><X size={17} /></button>
      </div>
      <nav className="inspector-tabs" aria-label="运行证据视图">{tabs.map(({ id, label, icon: Icon }) => <button type="button" className={tab === id ? "active" : ""} aria-label={label} title={label} onClick={() => onTab(id)} key={id}><Icon size={14} /><span>{label}</span></button>)}</nav>
      <div className="inspector-content">
        {!run && <div className="inspector-empty"><FileDiff size={26} /><p>请选择一条运行记录以查看证据。</p></div>}
        {run && tab === "timeline" && <Timeline events={events} />}
        {run && tab === "diff" && <DiffView diff={diff} />}
        {run && tab === "checks" && <ChecksView run={run} />}
        {run && tab === "verifier" && <VerifierView run={run} />}
      </div>
    </aside>
  );
}

function Timeline({ events }: { events: RunEvent[] }) {
  return <div className="timeline">{events.length === 0 && <p className="muted">正在等待证据…</p>}{events.map((event) => <div className="timeline-row" key={event.seq}><span className={`timeline-marker ${event.type.includes("error") ? "bad" : event.type.includes("completed") ? "good" : ""}`} /><div><strong>{event.type}</strong><small>{clockTime(event.created_at)} · #{event.seq}</small><p>{eventSummary(event)}</p></div></div>)}</div>;
}

function DiffView({ diff }: { diff: string }) {
  const lines = useMemo(() => parseDiff(diff), [diff]);
  if (!diff) return <div className="inspector-empty"><FileDiff size={26} /><p>智能体尚未修改文件。</p></div>;
  return <pre className="diff-view">{lines.map((line, index) => <span className={`diff-${line.kind}`} key={`${index}-${line.text}`}><i>{index + 1}</i>{line.text || " "}</span>)}</pre>;
}

function ChecksView({ run }: { run: Run }) {
  if (!run.plan) return <div className="inspector-empty"><ClipboardCheck size={26} /><p>规划完成后会显示检查项。</p></div>;
  return <div className="checks-view">{run.plan_gate && <PlanGateSummary gate={run.plan_gate} />}<div className="section-kicker">可见计划</div><ol className="plan-steps inspector-plan">{run.plan.steps.map((step) => <li key={step.id}><span>{step.status === "completed" ? <Check size={11} /> : step.id}</span><div><strong>{step.title}</strong>{step.description && <small>{step.description}</small>}</div></li>)}</ol>{run.plan.impacted_files.length > 0 && <div className="impact-files"><span>计划修改的文件</span>{run.plan.impacted_files.map((path) => <code key={path}>{path}</code>)}</div>}<div className="section-kicker contract-kicker">验收契约</div>{run.plan.acceptance_checks.map((check) => <article className={`check-row ${check.status}`} key={check.id}><div className="check-icon">{check.status === "passed" ? <Check size={14} /> : check.status === "failed" ? <X size={14} /> : <Circle size={10} />}</div><div><strong>{check.label}</strong>{check.command && <code>{check.command.join(" ")}</code>}{check.evidence && <pre>{check.evidence}</pre>}</div><span>{checkStatusLabel(check.status)}</span></article>)}</div>;
}

function PlanGateBadge({ gate }: { gate: PlanGate }) {
  return <span className={`plan-gate-badge ${gate.decision === "auto_approved" ? "fast" : "reviewed"}`}>{gate.decision === "auto_approved" ? <Zap size={10} /> : <ShieldCheck size={10} />}{gate.decision === "auto_approved" ? "快速路径" : riskLabel(gate.risk)}</span>;
}

function PlanGateSummary({ gate }: { gate: PlanGate }) {
  return <div className={`plan-gate-summary ${gate.decision === "auto_approved" ? "fast" : "review"}`}><div>{gate.decision === "auto_approved" ? <Zap size={15} /> : <ShieldCheck size={15} />}<span><strong>{gate.decision === "auto_approved" ? "低风险快速路径" : "需要审批计划"}</strong><small>确定性评估 · {riskLabel(gate.risk)}</small></span></div><ul>{gate.reasons.map((reason) => <li key={reason}>{planGateReasonLabel(reason)}</li>)}</ul></div>;
}

function ProofPackDialog({ pack, runId, onClose }: { pack: ProofPack | null; runId: string; onClose: () => void }) {
  const { dialogRef, onDialogKeyDown } = useDialogFocus(onClose);
  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={dialogRef} className="modal proof-modal" role="dialog" aria-modal="true" aria-labelledby="proof-title" tabIndex={-1} onKeyDown={onDialogKeyDown}>
        <div className="modal-heading"><div><p className="eyebrow">可审计完成记录</p><h2 id="proof-title">证据包</h2></div><button className="icon-button" type="button" onClick={onClose} aria-label="关闭证据包"><X size={17} /></button></div>
        {!pack ? <div className="proof-loading"><LoaderCircle className="spin" size={18} /> 正在汇总持久化证据…</div> : <>
          <div className={`proof-verdict ${pack.proof_status}`}><div className="evidence-seal"><Fingerprint size={22} /></div><div><span>证明状态</span><strong>{proofStatusLabel(pack.proof_status)}</strong><small>{pack.verification?.summary ?? "仍在汇总证据。"}</small></div><div><span>新鲜检查</span><strong>{pack.checks_fresh ? "是" : "否"}</strong></div></div>
          <div className="proof-grid"><article><span>计划门</span><strong>{pack.plan_gate ? planDecisionLabel(pack.plan_gate.decision) : "未评估"}</strong><small>{pack.plan_gate?.reasons.map(planGateReasonLabel).join(" · ")}</small></article><article><span>变更范围</span><strong>{pack.changed_files.length} 个文件</strong><small>{pack.changed_files.join(" · ") || "没有快照"} · {diffSourceLabel(pack.diff_source)}</small></article><article><span>命令沙箱</span><strong>{sandboxStatusLabel(pack.command_sandbox.status)}</strong><small>{pack.command_sandbox.backends.join(" · ") || "未记录操作系统沙箱后端"} · {pack.command_sandbox.sandboxed_commands} 个已强制隔离 · {pack.command_sandbox.not_executed_commands} 个运行前拦截</small></article><article><span>回滚</span><strong>{rollbackStatusLabel(pack.rollback.status)}</strong><small>{pack.rollback.conflicts.length ? `保留 ${pack.rollback.conflicts.length} 个冲突` : "可感知冲突"}</small></article><article><span>事件账本</span><strong>{pack.event_count} 条事件</strong><small>{pack.step_count} 个工具步骤 · {pack.repair_cycles} 轮修复</small></article></div>
          <div className="proof-section"><div className="section-kicker">原始任务</div><p>{pack.task}</p></div>
          <div className="proof-section"><div className="section-kicker">验收证据</div>{pack.plan?.acceptance_checks.map((check) => <div className="proof-check" key={check.id}><CheckCircle2 size={14} /><span><strong>{check.label}</strong><small>{check.evidence || check.command?.join(" ") || "等待证据"}</small></span><em>{checkStatusLabel(check.status)}</em></div>) ?? <p className="muted">尚无完成契约。</p>}</div>
          <div className="digest-card"><Fingerprint size={15} /><span><small>稳定证据 SHA-256</small><code>{pack.evidence_sha256}</code></span></div>
        </>}
        <div className="modal-actions"><span className="muted">摘要覆盖已持久化的计划、差异、检查、结论、回滚状态和事件账本。</span><div className="button-row"><button className="button ghost" type="button" onClick={onClose}>关闭</button><a className={`button primary ${pack ? "" : "disabled"}`} href={pack ? `/api/runs/${runId}/proof-pack.md` : undefined} download><Download size={14} /> 下载 Markdown</a></div></div>
      </section>
    </div>
  );
}

function VerifierView({ run }: { run: Run }) {
  const report = run.verification;
  if (!report) return <div className="inspector-empty"><ShieldCheck size={26} /><p>{run.verifier_enabled ? "检查通过后会开始独立审查。" : "本次运行未启用独立验证。"}</p></div>;
  return <div className="verifier-view"><div className={`verdict ${report.verdict}`}><ShieldCheck size={22} /><div><span>独立验证结论</span><strong>{verdictLabel(report.verdict)}</strong></div></div><p>{report.summary}</p>{report.findings.map((finding) => <article className="finding" key={`${finding.severity}-${finding.title}`}><span>{severityLabel(finding.severity)}</span><strong>{finding.title}</strong><p>{finding.evidence}</p>{finding.suggested_fix && <small>建议修复：{finding.suggested_fix}</small>}</article>)}</div>;
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

function labelFromMap(value: string, labels: Record<string, string>): string {
  return labels[value.replaceAll(" ", "_")] ?? value.replaceAll("_", " ");
}

function activityPhaseLabel(phase: "planning" | "building" | "verifying"): string {
  return labelFromMap(phase, { planning: "规划", building: "执行", verifying: "验证" });
}

function agentPhaseLabel(phase: string): string {
  return labelFromMap(phase.toLowerCase(), {
    agent: "智能体",
    planning: "规划",
    building: "执行",
    executing: "执行",
    verifying: "验证",
    recovery: "恢复",
  });
}

function planDecisionLabel(decision: string): string {
  return labelFromMap(decision, {
    assessed: "已评估",
    auto_approved: "自动通过",
    approval_required: "需要审批",
  });
}

function riskLabel(risk: string): string {
  return labelFromMap(risk, {
    low: "低风险",
    medium: "中风险",
    high: "高风险",
    unknown: "未知风险",
    elevated: "较高风险",
    dangerous: "危险动作",
  });
}

function verdictLabel(verdict: string): string {
  return labelFromMap(verdict, { pass: "通过", fail: "未通过", inconclusive: "无法判断" });
}

function severityLabel(severity: string): string {
  return labelFromMap(severity, {
    critical: "严重",
    high: "高",
    medium: "中",
    low: "低",
  });
}

function recoveryStrategyLabel(strategy: string): string {
  return labelFromMap(strategy, {
    restart_planning: "重新规划",
    await_clarification: "等待补充",
    await_plan_approval: "等待计划审批",
    persisted_fast_path: "恢复低风险快速路径",
    inspect_before_execution: "执行前检查工作区",
    resumed: "已恢复",
  });
}

function providerErrorLabel(category: string): string {
  return labelFromMap(category, {
    connection: "连接失败",
    timeout: "请求超时",
    rate_limit: "请求限流",
    server: "服务端临时错误",
    provider_contract: "服务响应异常",
    provider: "模型服务异常",
  });
}

function sandboxStatusLabel(status: string): string {
  return labelFromMap(status, {
    enforced: "已强制隔离",
    bypassed: "已批准绕过",
    policy_only: "仅策略限制",
    mixed: "混合模式",
    not_used: "未使用",
  });
}

function sandboxDetailLabel(detail: string): string {
  const labels: Record<string, string> = {
    "Seatbelt limits writes to the workspace and isolated command temp, with loopback-only network.": "Seatbelt 将写入限制在工作区和隔离的命令临时目录中，网络仅允许环回访问。",
    "Seatbelt is unavailable; command safety is policy-only.": "Seatbelt 不可用；命令安全当前仅由策略限制。",
    "Setuid bubblewrap is rejected; install a current non-setuid build.": "已拒绝 setuid Bubblewrap；请安装当前版本的非 setuid 构建。",
    "Bubblewrap limits writes to the workspace and isolated command temp, with isolated processes and network.": "Bubblewrap 将写入限制在工作区和隔离的命令临时目录中，并隔离进程与网络。",
    "Install working bubblewrap for OS enforcement; command safety is policy-only.": "请安装可用的 Bubblewrap 以启用系统级隔离；命令安全当前仅由策略限制。",
    "This operating system has no TraceForge sandbox backend; safety is policy-only.": "当前操作系统没有 TraceForge 沙箱后端；安全性仅由策略限制。",
  };
  return labels[detail] ?? detail;
}

function systemMessageLabel(message: string): string {
  const exact: Record<string, string> = {
    "Scripted provider is ready.": "脚本化模型服务已就绪。",
    "Connection and native tool calling verified.": "连接和原生工具调用已验证。",
    "The model responded, but did not complete the native tool-call probe.": "模型已响应，但未完成原生工具调用探测。",
    "Credential file must be smaller than 16 KiB": "凭证文件必须小于 16 KiB",
    "Credential file must be owner-only; run chmod 600 on it": "凭证文件必须仅限所有者访问；请对它运行 chmod 600",
    "Credential file could not be read as UTF-8": "无法按 UTF-8 读取凭证文件",
    "Credential file must contain exactly one non-empty line": "凭证文件必须恰好包含一行非空内容",
    "Configure a credential file or set OPENAI_API_KEY before starting a run": "开始任务前，请配置凭证文件或设置 OPENAI_API_KEY",
    "Model must not be empty": "模型不能为空",
    "Base URL must be an absolute http:// or https:// URL": "接口地址必须是以 http:// 或 https:// 开头的绝对 URL",
    "Pause, stop, or finish running work before changing model settings": "请先暂停、停止或完成当前任务，再修改模型设置",
    "This workspace already has an active or interrupted run": "此工作区已有正在执行或已中断的任务",
    "Approval is no longer pending": "该审批已不再处于等待状态",
  };
  if (exact[message]) return exact[message];
  const prefixes: Array<[string, string]> = [
    ["Credential file is not readable:", "凭证文件不可读："],
    ["Credential path is not a file:", "凭证路径不是文件："],
    ["Workspace is not a directory:", "工作区不是目录："],
    ["Model rate limit was reached:", "模型请求达到限流："],
    ["Model request timed out:", "模型请求超时："],
    ["Model connection failed:", "模型连接失败："],
    ["Model service returned a temporary server error:", "模型服务返回临时服务器错误："],
    ["Model request was rejected:", "模型请求被拒绝："],
  ];
  const match = prefixes.find(([prefix]) => message.startsWith(prefix));
  return match ? `${match[1]}${message.slice(match[0].length).trimStart()}` : message;
}

function checkStatusLabel(status: string): string {
  return labelFromMap(status, {
    pending: "待执行",
    running: "执行中",
    passed: "通过",
    failed: "失败",
    waived: "已豁免",
  });
}

function proofStatusLabel(status: string): string {
  return labelFromMap(status, {
    in_progress: "进行中",
    proven: "已证实",
    checks_only: "仅检查通过",
    not_proven: "未证实",
  });
}

function diffSourceLabel(source: string): string {
  return labelFromMap(source, {
    completion_event: "完成事件快照",
    diff_event: "差异事件快照",
    live_workspace: "当前工作区",
  });
}

function rollbackStatusLabel(status: string): string {
  return labelFromMap(status, {
    available: "可回滚",
    completed: "已完成",
    not_available: "不可用",
  });
}

function planGateReasonLabel(reason: string): string {
  const exact: Record<string, string> = {
    "Material choices were clarified with the user": "已与用户澄清关键选择",
    "The plan must name exactly one impacted file for automatic approval": "仅明确影响一个文件时才能自动批准",
    "The task touches a sensitive or high-impact engineering area": "任务涉及敏感或高影响工程领域",
    "A planned command is not a recognized local verification check": "计划命令不是已识别的本地验证检查",
    "Explicit single-file scope with routine local verification only": "范围明确为单文件，且只包含常规本地验证",
  };
  const stepMatch = reason.match(/^The plan has more than (\d+) implementation steps$/);
  if (stepMatch) return `计划包含超过 ${stepMatch[1]} 个实现步骤`;
  const checkMatch = reason.match(/^The completion contract has more than (\d+) checks$/);
  if (checkMatch) return `完成契约包含超过 ${checkMatch[1]} 项检查`;
  return exact[reason] ?? reason;
}

function eventSummary(event: RunEvent): string {
  if (event.type === "state.changed") {
    const state = String(event.payload.state ?? "unknown");
    const known = [
      "created", "planning", "awaiting_clarification", "awaiting_plan_approval",
      "executing", "awaiting_action_approval", "verifying", "succeeded", "failed",
      "cancelled", "interrupted", "rolled_back",
    ].includes(state);
    return `→ ${known ? presentState(state as RunState).label : state}`;
  }
  if (event.type === "message") return String(event.payload.content ?? "").slice(0, 100);
  if (event.type === "tool.completed") {
    const call = event.payload.call as { name?: string } | undefined;
    return call?.name ?? "工具结果";
  }
  if (event.type === "plan.updated") return "完成契约已更新";
  if (event.type === "plan.gated") {
    const decision = planDecisionLabel(String(event.payload.decision ?? "assessed"));
    const risk = String(event.payload.risk ?? "unknown");
    return `${decision} · ${riskLabel(risk)}`;
  }
  if (event.type === "diff.updated") return "工作区差异已更新";
  if (event.type === "run.resumed") {
    return `恢复：${recoveryStrategyLabel(String(event.payload.strategy ?? "resumed"))}`;
  }
  if (event.type === "repair.started") {
    return `修复轮次 ${String(event.payload.cycle ?? "?")} 已开始`;
  }
  if (event.type === "model.retry") {
    return `模型重试：第 ${String(event.payload.next_attempt ?? "?")} / ${String(event.payload.max_attempts ?? "?")} 次`;
  }
  if (event.type === "error") {
    return event.payload.recoverable === true
      ? "模型异常，可恢复暂停"
      : String(event.payload.message ?? "错误").slice(0, 100);
  }
  return "证据已记录";
}

function relativeTime(value: string): string {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function projectNameFromPath(path: string): string {
  return path.replace(/\/$/, "").split("/").filter(Boolean).at(-1) ?? "本地项目";
}

function formatTokens(value: number): string {
  if (value < 1_000) return String(value);
  const compact = value / 1_000;
  return `${compact >= 10 ? Math.round(compact) : compact.toFixed(1).replace(/\.0$/, "")}k`;
}

function clockTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}
