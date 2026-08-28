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
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import ReactMarkdown from "react-markdown";
import {
  availableProofTurnIndexes,
  isActiveState,
  parseDiff,
  presentState,
  proofPackTurnIndex,
  projectConversationEvents,
  projectProgressEvents,
  reasoningErrorLabel,
  shouldSubmitPrompt,
  supportedReasoningEffort,
  taskTitle,
} from "./lib";
import type {
  ApprovalMode,
  ClarificationAnswer,
  DirectoryListing,
  InteractionMode,
  PlanGate,
  Project,
  ProofPack,
  ProviderConfig,
  ProviderProbe,
  ProviderUpdate,
  ReasoningEffort,
  RollbackResult,
  Run,
  RunEvent,
  RunState,
  RunTarget,
  TaskPlan,
} from "./types";
import { useTraceForge, type ProofLoadState } from "./useTraceForge";

type InspectorTab = "timeline" | "diff" | "checks" | "verifier";

const PANEL_PREFERENCE_VERSION = "v1";
const LEFT_PANEL_MIN = 220;
const LEFT_PANEL_MAX = 420;
const LEFT_PANEL_DEFAULT = 276;
const RIGHT_PANEL_MIN = 300;
const RIGHT_PANEL_MAX = 560;
const RIGHT_PANEL_DEFAULT = 390;
const APPROVAL_MODE_PREFERENCE_KEY = "traceforge:approval-mode:v1";
const DEFAULT_REASONING_EFFORTS: ReasoningEffort[] = ["auto"];

function storedApprovalMode(): ApprovalMode {
  try {
    const value = window.localStorage.getItem(APPROVAL_MODE_PREFERENCE_KEY);
    return value === "manual" ? value : "automatic";
  } catch {
    return "automatic";
  }
}

function persistApprovalMode(value: ApprovalMode): void {
  try {
    window.localStorage.setItem(
      APPROVAL_MODE_PREFERENCE_KEY,
      value === "full_access" ? "automatic" : value,
    );
  } catch {
    // Permission preference persistence is best-effort; every run also stores its mode.
  }
}

function panelPreferenceKey(name: string): string {
  return `traceforge:layout:${PANEL_PREFERENCE_VERSION}:${name}`;
}

function storedBoolean(name: string, fallback: boolean): boolean {
  try {
    const value = window.localStorage.getItem(panelPreferenceKey(name));
    return value === null ? fallback : value === "true";
  } catch {
    return fallback;
  }
}

function storedPanelWidth(name: string, fallback: number, minimum: number, maximum: number): number {
  try {
    const value = Number(window.localStorage.getItem(panelPreferenceKey(name)));
    return Number.isFinite(value) && value >= minimum && value <= maximum ? value : fallback;
  } catch {
    return fallback;
  }
}

function persistPanelPreference(name: string, value: boolean | number): void {
  try {
    window.localStorage.setItem(panelPreferenceKey(name), String(value));
  } catch {
    // Layout preferences are best-effort and must never block the local agent UI.
  }
}

function dialogFocusables(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>(
    "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), "
      + "textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
  )).filter((element) => element.getClientRects().length > 0);
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
  const [proofSelection, setProofSelection] = useState<{
    runId: string;
    turnIndex: number;
  } | null>(null);
  const [mobilePane, setMobilePane] = useState<"sidebar" | "inspector" | null>(null);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("timeline");
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth);
  const [leftCollapsed, setLeftCollapsed] = useState(() => storedBoolean("left-collapsed", false));
  const [rightCollapsed, setRightCollapsed] = useState(() => storedBoolean("right-collapsed", true));
  const [leftWidth, setLeftWidth] = useState(() => storedPanelWidth(
    "left-width", LEFT_PANEL_DEFAULT, LEFT_PANEL_MIN, LEFT_PANEL_MAX,
  ));
  const [rightWidth, setRightWidth] = useState(() => storedPanelWidth(
    "right-width", RIGHT_PANEL_DEFAULT, RIGHT_PANEL_MIN, RIGHT_PANEL_MAX,
  ));
  const leftDrawerMode = viewportWidth <= 680;
  const rightDrawerMode = viewportWidth <= 980;
  const providerReady = Boolean(
    forge.provider?.api_key_configured && forge.provider.connection_verified,
  );
  const providerConfigured = Boolean(forge.provider?.api_key_configured);

  useEffect(() => {
    if (forge.run?.state === "verifying") setInspectorTab("verifier");
    if (forge.run?.state === "succeeded") setInspectorTab("checks");
  }, [forge.run?.state]);

  useEffect(() => setProofSelection(null), [forge.run?.id]);
  useEffect(() => {
    const closeObsoleteDrawer = () => {
      setViewportWidth(window.innerWidth);
      setMobilePane((current) => {
        if (current === "sidebar" && window.innerWidth > 680) return null;
        if (current === "inspector" && window.innerWidth > 980) return null;
        return current;
      });
    };
    window.addEventListener("resize", closeObsoleteDrawer);
    return () => window.removeEventListener("resize", closeObsoleteDrawer);
  }, []);

  useEffect(() => persistPanelPreference("left-collapsed", leftCollapsed), [leftCollapsed]);
  useEffect(() => persistPanelPreference("right-collapsed", rightCollapsed), [rightCollapsed]);
  useEffect(() => persistPanelPreference("left-width", leftWidth), [leftWidth]);
  useEffect(() => persistPanelPreference("right-width", rightWidth), [rightWidth]);

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
    const created = await forge.createProject(
      projectNameFromPath(choice.path),
      choice.path,
      false,
    );
    openProjectComposer(created.id);
  };

  const toggleHistory = () => {
    if (leftDrawerMode) {
      setMobilePane((current) => current === "sidebar" ? null : "sidebar");
    } else {
      setLeftCollapsed((current) => !current);
    }
  };
  const toggleInspector = () => {
    if (rightDrawerMode) {
      setMobilePane((current) => current === "inspector" ? null : "inspector");
    } else {
      setRightCollapsed((current) => !current);
    }
  };
  const mainStageMinimum = viewportWidth <= 1280 ? 360 : 480;
  const leftPanelMaximum = Math.min(
    LEFT_PANEL_MAX,
    Math.max(
      LEFT_PANEL_MIN,
      viewportWidth - (!rightCollapsed && !rightDrawerMode ? RIGHT_PANEL_MIN : 0) - mainStageMinimum,
    ),
  );
  const visibleLeftWidth = leftCollapsed || leftDrawerMode
    ? 0
    : Math.min(leftWidth, leftPanelMaximum);
  const rightPanelMaximum = Math.min(
    RIGHT_PANEL_MAX,
    Math.max(
      RIGHT_PANEL_MIN,
      viewportWidth - visibleLeftWidth - mainStageMinimum,
    ),
  );
  const visibleRightWidth = rightCollapsed || rightDrawerMode
    ? 0
    : Math.min(rightWidth, rightPanelMaximum);
  const workspaceGridStyle = {
    "--sidebar-width": `${visibleLeftWidth}px`,
    "--inspector-width": `${visibleRightWidth}px`,
  } as CSSProperties;

  return (
    <div className="app-shell">
      <Header
        status={forge.status}
        connected={forge.connected}
        run={forge.run}
        providerReady={providerReady}
        providerConfigured={providerConfigured}
        historyExpanded={leftDrawerMode ? mobilePane === "sidebar" : !leftCollapsed}
        inspectorExpanded={rightDrawerMode ? mobilePane === "inspector" : !rightCollapsed}
        onHistory={toggleHistory}
        onInspector={toggleInspector}
        onSettings={() => setShowSettings(true)}
      />
      {forge.error && (
        <div className="global-error" role="alert">
          <AlertTriangle size={16} />
          <span>{systemMessageLabel(forge.error)}</span>
          <button type="button" aria-label="关闭错误提示" onClick={forge.clearError}><X size={15} /></button>
        </div>
      )}
      <main className="workspace-grid" style={workspaceGridStyle}>
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
          collapsed={leftCollapsed && !leftDrawerMode}
          width={visibleLeftWidth || leftWidth}
          maximumWidth={leftPanelMaximum}
          onResize={setLeftWidth}
        />
        <section className="main-stage">
          {!forge.run || showComposer ? (
            <TaskComposer
              key={`${forge.status?.suggested_task ?? "standard"}-${composerProjectId ?? "direct"}`}
              suggestedTask={forge.status?.suggested_task ?? ""}
              defaultWorkspace={forge.status?.workspace ?? ""}
              project={composerProject}
              demoMode={forge.status?.mode === "demo"}
              provider={forge.provider}
              providerReady={providerReady}
              onOpenSettings={() => setShowSettings(true)}
              onCancel={() => setShowComposer(false)}
              onSubmit={async (task, mode, approvalMode, reasoningEffort, target) => {
                await forge.createRun(
                  task,
                  mode,
                  approvalMode,
                  reasoningEffort,
                  target,
                );
                setShowComposer(false);
              }}
              canCancel={Boolean(forge.run)}
            />
          ) : (
            <RunStage
              key={forge.run.id}
              run={forge.run}
              events={forge.events}
              provider={forge.provider}
              providerReady={providerReady}
              followUpEnabled={forge.status?.mode !== "demo"}
              rollbackResult={forge.rollbackResult}
              onAnswer={forge.answerQuestions}
              onPlan={forge.decidePlan}
              onAction={forge.decideAction}
              onCancel={() => {
                void forge.cancel()?.catch(() => undefined);
              }}
              onResume={() => {
                void forge.resume()?.catch(() => undefined);
              }}
              onRollback={forge.rollback}
              onProof={(turnIndex) => {
                const runId = forge.run!.id;
                setProofSelection({ runId, turnIndex });
                void forge.loadProofPack(runId, turnIndex).catch(() => undefined);
              }}
              onOpenWorkspace={() => forge.openWorkspace(forge.run!.id)}
              onSelectRun={forge.selectRun}
              onFollowUp={async (prompt, mode, approvalMode, reasoningEffort) => {
                await forge.followUp(prompt, mode, approvalMode, reasoningEffort);
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
          collapsed={rightCollapsed && !rightDrawerMode}
          width={visibleRightWidth || rightWidth}
          maximumWidth={rightPanelMaximum}
          onResize={setRightWidth}
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
      {proofSelection && forge.run?.id === proofSelection.runId && (
        <ProofPackDialog
          pack={forge.proofPack}
          loadState={forge.proofLoadState}
          runId={forge.run.id}
          turnIndexes={availableProofTurnIndexes(forge.run)}
          selectedTurnIndex={proofSelection.turnIndex}
          onTurnChange={(turnIndex) => {
            const runId = forge.run!.id;
            setProofSelection({ runId, turnIndex });
            void forge.loadProofPack(runId, turnIndex).catch(() => undefined);
          }}
          onRetry={() => {
            const { runId, turnIndex } = proofSelection;
            void forge.loadProofPack(runId, turnIndex).catch(() => undefined);
          }}
          onClose={() => setProofSelection(null)}
        />
      )}
      {showProject && (
        <ProjectDialog
          initialDirectory={forge.status?.last_workspace ?? forge.status?.workspace ?? ""}
          onClose={() => setShowProject(false)}
          onListDirectories={forge.listDirectories}
          onCreate={async (name, root, createDirectory) => {
            const created = await forge.createProject(name, root, createDirectory);
            setShowProject(false);
            openProjectComposer(created.id);
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
  providerConfigured,
  historyExpanded,
  inspectorExpanded,
  onHistory,
  onInspector,
  onSettings,
}: {
  status: ReturnType<typeof useTraceForge>["status"];
  connected: boolean;
  run: Run | null;
  providerReady: boolean;
  providerConfigured: boolean;
  historyExpanded: boolean;
  inspectorExpanded: boolean;
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
        ? "本地服务已就绪，模型连接已验证"
        : providerConfigured
          ? "本地服务已就绪，需要验证模型连接"
          : "本地服务已就绪，仍需配置模型"
      : "正在连接本地 TraceForge 服务";
  return (
    <header className="topbar">
      <button
        className="icon-button panel-nav-button history-toggle"
        type="button"
        onClick={onHistory}
        aria-label="任务与项目"
        aria-expanded={historyExpanded}
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
          className="icon-button panel-nav-button inspector-toggle"
          type="button"
          onClick={onInspector}
          aria-label="任务详情"
          aria-expanded={inspectorExpanded}
        ><PanelRight size={18} /></button>
        <div className="context-item workspace-path" title={run?.workspace ?? status?.workspace}>
          <GitBranch size={14} />
          <span>{run?.workspace ?? status?.workspace ?? "正在连接…"}</span>
        </div>
        <div className="context-item"><Sparkles size={14} /><span>{status?.model ?? "—"}</span></div>
        <div
          className={`sandbox-status ${status?.sandbox.enforced ? "enforced" : status ? "degraded" : "pending"}`}
          title={status ? sandboxDetailLabel(status.sandbox.detail) : "正在检测命令沙箱…"}
        >
          <ShieldCheck size={13} />
          <span>{!status ? "检测中" : status.sandbox.enforced ? status.sandbox.backend : "仅策略限制"}</span>
        </div>
        {run && (
          <div className="context-item" title="当前轮已计入预算的非终态工具动作">
            <Wrench size={14} /><span>{run.step_count} 本轮动作</span>
          </div>
        )}
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
          title={providerReady ? "模型设置" : providerConfigured ? "验证模型连接" : "配置模型凭证"}
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
  collapsed,
  width,
  maximumWidth,
  onResize,
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
  collapsed: boolean;
  width: number;
  maximumWidth: number;
  onResize: (width: number) => void;
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
      className={`sidebar panel-edge ${collapsed ? "collapsed" : ""} ${mobileOpen ? "mobile-open" : ""}`}
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
          title={demoMode ? "固定演示不连接真实项目；请运行 traceforge" : "使用系统选择器添加项目"}
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
      <PanelResizer
        side="left"
        width={width}
        minimum={LEFT_PANEL_MIN}
        maximum={maximumWidth}
        defaultWidth={LEFT_PANEL_DEFAULT}
        onResize={onResize}
      />
    </aside>
  );
}

function PanelResizer({
  side,
  width,
  minimum,
  maximum,
  defaultWidth,
  onResize,
}: {
  side: "left" | "right";
  width: number;
  minimum: number;
  maximum: number;
  defaultWidth: number;
  onResize: (width: number) => void;
}) {
  const drag = useRef<{ pointerX: number; width: number } | null>(null);
  const clamp = (value: number) => Math.min(maximum, Math.max(minimum, Math.round(value)));
  const resizeFromPointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!drag.current) return;
    const delta = side === "left"
      ? event.clientX - drag.current.pointerX
      : drag.current.pointerX - event.clientX;
    onResize(clamp(drag.current.width + delta));
  };
  return (
    <div
      className={`panel-resizer ${side}`}
      role="separator"
      aria-label={side === "left" ? "调整任务侧栏宽度" : "调整详情侧栏宽度"}
      aria-orientation="vertical"
      aria-valuemin={minimum}
      aria-valuemax={maximum}
      aria-valuenow={width}
      tabIndex={0}
      onDoubleClick={() => onResize(defaultWidth)}
      onPointerDown={(event) => {
        drag.current = { pointerX: event.clientX, width };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={resizeFromPointer}
      onPointerUp={(event) => {
        drag.current = null;
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      }}
      onPointerCancel={() => {
        drag.current = null;
      }}
      onLostPointerCapture={() => {
        drag.current = null;
      }}
      onKeyDown={(event) => {
        const direction = side === "left" ? 1 : -1;
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          onResize(clamp(width - 16 * direction));
        } else if (event.key === "ArrowRight") {
          event.preventDefault();
          onResize(clamp(width + 16 * direction));
        } else if (event.key === "Home") {
          event.preventDefault();
          onResize(minimum);
        } else if (event.key === "End") {
          event.preventDefault();
          onResize(maximum);
        }
      }}
    />
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
      <strong title={run.task}>{taskTitle(run)}</strong>
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
  provider,
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
  provider: ProviderConfig | null;
  providerReady: boolean;
  onOpenSettings: () => void;
  onSubmit: (
    task: string,
    mode: InteractionMode,
    approvalMode: ApprovalMode,
    reasoningEffort: ReasoningEffort,
    target: RunTarget,
  ) => Promise<void>;
  onCancel: () => void;
  canCancel: boolean;
}) {
  const [task, setTask] = useState(suggestedTask);
  const [planMode, setPlanMode] = useState(demoMode);
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>(
    demoMode ? "automatic" : storedApprovalMode,
  );
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("auto");
  const [submitting, setSubmitting] = useState(false);
  const supportedReasoning = provider?.supported_reasoning_efforts
    ?? DEFAULT_REASONING_EFFORTS;
  const effectiveReasoningEffort = supportedReasoningEffort(
    supportedReasoning,
    reasoningEffort,
  );
  useEffect(() => {
    if (effectiveReasoningEffort !== reasoningEffort) {
      setReasoningEffort(effectiveReasoningEffort);
    }
  }, [effectiveReasoningEffort, reasoningEffort]);
  const targetLabel = project ? project.name : demoMode ? "固定演示" : "直接任务";
  return (
    <div className="composer-wrap">
      <div className="hero-symbol"><Code2 size={30} /></div>
      <p className="eyebrow">{targetLabel} · 新建对话</p>
      <h1>{project ? `你想在 ${project.name} 中处理什么？` : "你想让 TraceForge 帮你做什么？"}</h1>
      <p className="hero-copy">
        {project
          ? `任务会在 ${project.root} 中执行，并归入这个项目。`
          : demoMode
            ? "这是可重复的固定导览，只接受下方预置案例；真实任务请运行 traceforge。"
            : "可以直接提问，也可以描述要实现的结果；需要实施时，TraceForge 会在独立目录中工作并提供完成证据。"}
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
          persistApprovalMode(approvalMode);
          void onSubmit(
            task.trim(),
            planMode ? "plan" : "agent",
            demoMode ? "automatic" : approvalMode,
            demoMode ? "auto" : effectiveReasoningEffort,
            target,
          )
            .catch(() => undefined)
            .finally(() => setSubmitting(false));
        }}
      >
        {!providerReady && (
          <button className="setup-callout" type="button" onClick={onOpenSettings}>
            <AlertTriangle size={15} />
            <span>
              <strong>{provider?.api_key_configured ? "需要验证模型连接" : "需要配置模型"}</strong>
              <small>
                {provider?.api_key_configured
                  ? "已保存凭证；重新测试原生工具调用后即可发送。"
                  : "填写 API Key，并验证原生工具调用。"}
              </small>
            </span>
            <ArrowRight size={15} />
          </button>
        )}
        <div className="composer-target" title={project?.root ?? defaultWorkspace}>
          {project ? <FolderOpen size={16} /> : <Code2 size={16} />}
          <span>
            <strong>{targetLabel}</strong>
            <small>
              {project?.root ?? (demoMode ? defaultWorkspace : `在 ${defaultWorkspace} 下自动创建独立目录`)}
            </small>
          </span>
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
          placeholder="例如：解释这个项目的结构；或修复多租户缓存串读，补充回归测试并确保检查通过。"
          rows={6}
        />
        <ApprovalModePicker
          value={approvalMode}
          onChange={(value) => {
            setApprovalMode(value);
            persistApprovalMode(value);
          }}
          disabled={demoMode}
        />
        <div className="composer-actions">
          <label className="toggle-row plan-mode-toggle">
            <input type="checkbox" checked={planMode} onChange={(event) => setPlanMode(event.target.checked)} disabled={demoMode} />
            <span className="toggle" />
            <span><strong>计划模式</strong><small>先生成完整方案，确认后再实施</small></span>
          </label>
          <ReasoningEffortPicker
            value={effectiveReasoningEffort}
            onChange={setReasoningEffort}
            provider={provider}
            disabled={demoMode}
          />
          <div className="composer-safeguards" aria-label="任务保障">
            <span><ShieldCheck size={13} /><strong>{approvalModeLabel(approvalMode)}</strong> · {approvalModeShortDescription(approvalMode)}</span>
            <span><CheckCircle2 size={13} /><strong>完成后复核</strong> · 独立只读审查</span>
          </div>
          <div className="button-row">
            {canCancel && <button className="button ghost" type="button" onClick={onCancel}>取消</button>}
            <button
              className="button primary"
              type="submit"
              disabled={!task.trim() || !providerReady || submitting}
            >
              {submitting ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={16} />}
              发送
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

function ApprovalModePicker({
  value,
  onChange,
  disabled = false,
}: {
  value: ApprovalMode;
  onChange: (value: ApprovalMode) => void;
  disabled?: boolean;
}) {
  const options: Array<{
    value: ApprovalMode;
    label: string;
    description: string;
  }> = [
    {
      value: "manual",
      label: "手动审批",
      description: "编辑与命令逐项确认；读取不打断",
    },
    {
      value: "automatic",
      label: "自动审批",
      description: "本地规则放行计划内动作；未知项询问",
    },
    {
      value: "full_access",
      label: "完全访问（工作区）",
      description: "沙箱内免询问；无 OS 沙箱时未知命令降级确认",
    },
  ];
  return (
    <fieldset className="approval-mode-picker">
      <legend>权限模式 <small>与计划模式独立</small></legend>
      <div>
        {options.map((option) => (
          <label
            className={`${value === option.value ? "selected" : ""} ${option.value}`}
            key={option.value}
          >
            <input
              type="radio"
              name="approval-mode"
              value={option.value}
              checked={value === option.value}
              disabled={disabled}
              onChange={() => onChange(option.value)}
            />
            <span className="radio-dot" />
            <span><strong>{option.label}</strong><small>{option.description}</small></span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function ReasoningEffortPicker({
  value,
  onChange,
  provider,
  disabled = false,
}: {
  value: ReasoningEffort;
  onChange: (value: ReasoningEffort) => void;
  provider: ProviderConfig | null;
  disabled?: boolean;
}) {
  const efforts = provider?.supported_reasoning_efforts ?? DEFAULT_REASONING_EFFORTS;
  const effectiveValue = supportedReasoningEffort(efforts, value);
  const description = efforts.length === 1
    ? reasoningCapabilityDescription(provider)
    : `${reasoningEffortDescription(effectiveValue)} ${reasoningCapabilityDescription(provider)}`;
  return (
    <div className="reasoning-effort-picker">
      <span><Gauge size={13} /> 思考强度</span>
      {!provider ? (
        <div
          className="reasoning-effort-fixed"
          role="status"
          aria-label="本轮思考强度：正在读取模型能力"
          title={description}
        >
          正在读取
          <span>能力加载中</span>
        </div>
      ) : efforts.length === 1 ? (
        <div
          className="reasoning-effort-fixed"
          role="group"
          aria-label={`本轮思考强度：${reasoningEffortTriggerLabel(effectiveValue, provider)}，唯一可用`}
          title={description}
        >
          {reasoningEffortTriggerLabel(effectiveValue, provider)}
          <span>唯一可用</span>
        </div>
      ) : (
        <label className="reasoning-effort-select-shell">
          <select
            aria-label="本轮思考强度"
            value={effectiveValue}
            disabled={disabled}
            onChange={(event) => onChange(event.target.value as ReasoningEffort)}
            title={description}
          >
            {efforts.map((effort) => (
              <option value={effort} key={effort}>
                {reasoningEffortTriggerLabel(effort, provider)}
              </option>
            ))}
          </select>
          <ChevronDown aria-hidden="true" size={13} />
        </label>
      )}
      <small>{description}</small>
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
  onTest: (config: ProviderUpdate) => Promise<ProviderProbe>;
}) {
  const [model, setModel] = useState(provider.model);
  const [baseUrl, setBaseUrl] = useState(provider.base_url ?? "");
  const [apiKey, setApiKey] = useState("");
  const [credentialFile, setCredentialFile] = useState(provider.credential_file ?? "");
  const [contextWindow, setContextWindow] = useState(
    provider.context_window?.toString() ?? "",
  );
  const [working, setWorking] = useState<"save" | "test" | null>(null);
  const [probe, setProbe] = useState<ProviderProbe | null>(null);
  const { dialogRef, onDialogKeyDown } = useDialogFocus(onClose);

  const parsedContextWindow = contextWindow.trim()
    ? Number(contextWindow.trim())
    : null;
  const contextWindowValid = parsedContextWindow === null || (
    Number.isInteger(parsedContextWindow)
    && parsedContextWindow >= 1
    && parsedContextWindow <= 10_000_000
  );
  const config: ProviderUpdate = {
    model: model.trim(),
    base_url: baseUrl.trim() || null,
    credential_file: apiKey.trim() ? null : credentialFile.trim() || null,
    context_window: contextWindowValid ? parsedContextWindow : null,
    ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
  };
  const routeDraftChanged = model.trim() !== provider.model
    || (baseUrl.trim() || null) !== provider.base_url;
  const connectionReady = provider.api_key_configured && provider.connection_verified;
  const credentialDescription = provider.credential_source === "file"
    ? "已安全保存在仅当前用户可读的本地文件"
    : provider.credential_source === "environment"
      ? provider.credential_env
      : "在下方输入 API Key，或设置 OPENAI_API_KEY";
  const verificationDescription = provider.verified_at
    ? `${new Date(provider.verified_at).toLocaleString("zh-CN")} 已验证原生工具调用`
    : provider.connection_verified
      ? "当前运行模式已验证原生工具调用"
      : "需要测试原生工具调用后才能开始或继续任务";

  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={dialogRef} className="modal provider-modal" role="dialog" aria-modal="true" aria-labelledby="provider-title" tabIndex={-1} onKeyDown={onDialogKeyDown}>
        <div className="modal-heading">
          <div><p className="eyebrow">模型服务</p><h2 id="provider-title">连接设置</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭模型设置"><X size={17} /></button>
        </div>
        <div className={`credential-status ${connectionReady ? "ready" : "missing"}`}>
          {connectionReady ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}
          <div>
            <strong>{connectionReady ? "连接已验证" : provider.api_key_configured ? "凭证已保存，等待验证" : "需要凭证"}</strong>
            <small>{credentialDescription} · {verificationDescription}</small>
          </div>
        </div>
        <label className="field-label"><span>模型</span><input value={model} onChange={(event) => { setModel(event.target.value); setProbe(null); }} data-dialog-initial-focus /></label>
        <label className="field-label"><span>OpenAI 兼容接口地址</span><input value={baseUrl} onChange={(event) => { setBaseUrl(event.target.value); setProbe(null); }} placeholder="https://api.deepseek.com" /></label>
        <div className={`reasoning-capability-note ${provider.reasoning_effort_source}`}>
          <Gauge size={16} />
          <span>
            <strong>当前已保存配置 · 思考强度：{provider.supported_reasoning_efforts.length > 1 ? "精确模型能力已识别" : "仅跟随模型默认"}</strong>
            <small>{routeDraftChanged ? "模型或接口草稿已变化；测试并保存后才会重新解析能力。 · " : ""}{reasoningCapabilityDescription(provider)} · 目录 {provider.reasoning_effort_catalog_version}</small>
          </span>
        </div>
        <label className="field-label">
          <span>API Key</span>
          <input type="password" autoComplete="off" value={apiKey} onChange={(event) => { setApiKey(event.target.value); setProbe(null); }} placeholder={provider.api_key_configured ? "已配置；留空则保持不变" : "输入模型服务的 API Key"} />
          <small>只写入本机仅当前用户可读的私密文件；页面、数据库与运行记录都不会保存或回显 Key。</small>
        </label>
        <details className="advanced-settings">
          <summary>高级设置</summary>
          <label className="field-label">
            <span>凭证文件路径</span>
            <input value={credentialFile} onChange={(event) => { setCredentialFile(event.target.value); setProbe(null); }} placeholder="/absolute/path/to/owner-only-key-file" disabled={Boolean(apiKey.trim())} />
            <small>文件必须只有一行，并使用仅所有者权限（chmod 600）。输入 API Key 时会忽略此路径。</small>
          </label>
          <label className="field-label">
            <span>上下文窗口（token，可选）</span>
            <input
              type="number"
              min={1}
              max={10_000_000}
              step={1}
              value={contextWindow}
              onChange={(event) => { setContextWindow(event.target.value); setProbe(null); }}
              placeholder="留空自动识别"
              aria-invalid={!contextWindowValid}
            />
            <small>
              {contextWindowValid
                ? `当前采用 ${provider.resolved_context_window.toLocaleString()} token（${contextWindowSourceLabel(provider.context_window_source)}）。留空时只精确识别已知模型，其他模型使用保守回退。`
                : "请输入 1–10,000,000 之间的整数。"}
            </small>
          </label>
        </details>
        {probe && (
          <div className={`probe-result ${probe.ok ? "ready" : "failed"}`} role="status">
            {probe.ok ? <CheckCircle2 size={16} /> : <OctagonX size={16} />}
            <span>
              <strong>
                {probe.ok
                  ? "原生工具调用已验证"
                  : probe.provider.connection_verified
                    ? "草稿检查失败，已保存连接仍有效"
                    : "连接检查失败"}
              </strong>
              <small>{systemMessageLabel(probe.detail)} · {probe.latency_ms} 毫秒</small>
            </span>
          </div>
        )}
        <div className="modal-actions">
          <span className="muted">测试成功才会保存并启用连接；单独保存会要求重新测试。</span>
          <div className="button-row">
            <button
              className="button"
              type="button"
              disabled={!config.model || !contextWindowValid || Boolean(working)}
              onClick={() => {
                setWorking("test");
                setProbe(null);
                void onTest(config)
                  .then((result) => {
                    setProbe(result);
                    if (!result.ok) return;
                    setModel(result.provider.model);
                    setBaseUrl(result.provider.base_url ?? "");
                    setApiKey("");
                    setCredentialFile(result.provider.credential_file ?? "");
                    setContextWindow(result.provider.context_window?.toString() ?? "");
                  })
                  .catch(() => undefined)
                  .finally(() => setWorking(null));
              }}
            >
              {working === "test" ? <LoaderCircle className="spin" size={15} /> : <Wifi size={15} />} 测试并保存
            </button>
            <button
              className="button primary"
              type="button"
              disabled={!config.model || !contextWindowValid || Boolean(working)}
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
  provider,
  providerReady,
  onAnswer,
  onPlan,
  onAction,
  onCancel,
  onResume,
  onRollback,
  onProof,
  onOpenWorkspace,
  onSelectRun,
  onFollowUp,
  followUpEnabled,
  rollbackResult,
}: {
  run: Run;
  events: RunEvent[];
  provider: ProviderConfig | null;
  providerReady: boolean;
  followUpEnabled: boolean;
  rollbackResult: RollbackResult | null;
  onAnswer: (answers: ClarificationAnswer[]) => Promise<void>;
  onPlan: (decision: "approve" | "revise", feedback?: string) => Promise<void>;
  onAction: (approved: boolean) => Promise<void>;
  onCancel: () => void;
  onResume: () => void;
  onRollback: () => Promise<RollbackResult>;
  onProof: (turnIndex: number) => void;
  onOpenWorkspace: () => Promise<unknown>;
  onSelectRun: (runId: string) => void;
  onFollowUp: (
    prompt: string,
    mode: InteractionMode,
    approvalMode: ApprovalMode,
    reasoningEffort: ReasoningEffort,
  ) => Promise<void>;
}) {
  const [confirmRollback, setConfirmRollback] = useState(false);
  const [openingWorkspace, setOpeningWorkspace] = useState(false);
  const rollbackSummaryRef = useRef<HTMLDivElement>(null);
  const answeredTaskHasEdits = run.state === "answered"
    && run.turns.some((turn) => turn.changed_files.length > 0);
  const proofTurnIndexes = availableProofTurnIndexes(run);
  const latestProofTurnIndex = proofTurnIndexes.at(-1) ?? null;
  const currentTurnIndex = run.turns.at(-1)?.index ?? 1;
  const currentProofTurnIndex = run.state === "succeeded"
    && proofTurnIndexes.includes(currentTurnIndex)
    ? currentTurnIndex
    : null;
  const historicalProofTurnIndex = run.state === "succeeded"
    ? proofTurnIndexes.filter((turnIndex) => turnIndex !== currentTurnIndex).at(-1) ?? null
    : latestProofTurnIndex;
  const effectiveRollbackResult = rollbackResult ?? latestRollbackResult(events);
  return (
    <div className="run-stage">
      <div className="run-header">
        <div>
          <div className="run-header-meta">
            <StateBadge state={run.state} />
            {run.mode === "plan" && run.plan_gate && <PlanGateBadge gate={run.plan_gate} />}
            <ApprovalModeBadge mode={run.approval_mode} />
            <ReasoningEffortBadge effort={run.reasoning_effort} />
            <span>{run.mode === "plan" ? "计划模式" : "普通 Agent"} · 第 {Math.max(run.turns.length, 1)} 轮</span>
            <span>任务 {run.id.slice(0, 8).toUpperCase()}</span>
            {run.parent_run_id && (
              <span title={`回滚前任务 ${run.parent_run_id}`}>续自回滚任务 {run.parent_run_id.slice(0, 8).toUpperCase()}</span>
            )}
          </div>
          <h2 title={run.task}>{taskTitle(run)}</h2>
        </div>
        <div className="button-row">
          <button
            className="button ghost"
            type="button"
            disabled={openingWorkspace}
            onClick={() => {
              setOpeningWorkspace(true);
              void onOpenWorkspace().catch(() => undefined).finally(() => setOpeningWorkspace(false));
            }}
            title="在本地文件管理器中定位任务目录"
          >
            {openingWorkspace ? <LoaderCircle className="spin" size={14} /> : <FolderOpen size={14} />}
            {openingWorkspace ? "正在打开" : "打开目录"}
          </button>
          {isActiveState(run.state) && (
            <button className="button danger-ghost" type="button" onClick={onCancel}><Square size={14} /> 停止</button>
          )}
          {run.state === "interrupted" && (
            <button
              className="button"
              type="button"
              onClick={onResume}
              disabled={!providerReady}
              title={providerReady ? "继续安全暂停的任务" : "请先测试并验证模型连接"}
            ><Play size={14} /> 继续</button>
          )}
          {(["succeeded", "failed", "cancelled", "interrupted"].includes(run.state) || answeredTaskHasEdits) && (
            <button className="button ghost" type="button" onClick={() => setConfirmRollback(true)}><RotateCcw size={14} /> 回滚</button>
          )}
        </div>
      </div>
      <ActivityFeed run={run} events={events} />
      <div className="interaction-dock">
        {run.state === "awaiting_clarification" && run.clarification && (
          <ClarificationPanel
            key={run.decision_request_id ?? `clarification:${run.clarification.round}`}
            request={run.clarification}
            onSubmit={onAnswer}
          />
        )}
        {run.state === "awaiting_plan_approval" && run.plan && (
          <PlanPanel
            key={run.decision_request_id ?? `plan:${run.updated_at}`}
            runId={run.id}
            plan={run.plan}
            gate={run.plan_gate}
            onDecision={onPlan}
          />
        )}
        {run.state === "awaiting_action_approval" && run.pending_approval && (
          <ApprovalPanel
            key={run.pending_approval.id}
            approval={run.pending_approval}
            onDecision={onAction}
          />
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
        {run.state === "answered" && (
          <Notice icon={<MessageSquareMore size={18} />} title="本轮已直接答复">
            本轮没有修改文件、运行命令或生成完成证明；需要继续分析或开始实施时，直接在下方输入即可。
          </Notice>
        )}
        {run.state === "rolled_back" && (
          <RollbackSummary
            result={effectiveRollbackResult}
            successorRunId={run.successor_run_id}
            onOpenSuccessor={onSelectRun}
            focusRef={rollbackSummaryRef}
          />
        )}
        {run.error && <Notice icon={<OctagonX size={18} />} title={run.state === "interrupted" ? "暂停原因" : "停止原因"} danger={run.state !== "interrupted"}>{systemMessageLabel(run.error)}</Notice>}
        {run.state === "succeeded" && (
          <CompletionSummary
            run={run}
            turnIndex={currentProofTurnIndex}
            onProof={onProof}
          />
        )}
        {historicalProofTurnIndex !== null && currentProofTurnIndex === null && (
          <HistoricalProofSummary turnIndex={historicalProofTurnIndex} onProof={onProof} />
        )}
        {followUpEnabled && ["answered", "succeeded", "failed", "cancelled", "rolled_back"].includes(run.state) && !(run.state === "rolled_back" && run.successor_run_id) && (
          <FollowUpComposer
            key={`${run.id}:${run.turns.length}`}
            defaultApprovalMode={run.approval_mode}
            defaultReasoningEffort={run.reasoning_effort}
            provider={provider}
            providerReady={providerReady}
            onSubmit={onFollowUp}
          />
        )}
      </div>
      {confirmRollback && (
        <RollbackDialog
          onClose={() => setConfirmRollback(false)}
          onConfirm={async () => {
            await onRollback();
            setConfirmRollback(false);
            window.requestAnimationFrame(() => rollbackSummaryRef.current?.focus());
          }}
        />
      )}
    </div>
  );
}

function RollbackDialog({
  onClose,
  onConfirm,
}: {
  onClose: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const close = () => {
    if (!submittingRef.current) onClose();
  };
  const { dialogRef, onDialogKeyDown } = useDialogFocus(close);
  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={dialogRef} className="modal" role="dialog" aria-modal="true" aria-labelledby="rollback-title" aria-busy={submitting} tabIndex={-1} onKeyDown={onDialogKeyDown}>
        <div className="modal-heading">
          <div><p className="eyebrow">冲突感知回滚</p><h2 id="rollback-title">回滚本次运行？</h2></div>
          <button className="icon-button" type="button" onClick={close} disabled={submitting} aria-label="关闭回滚确认"><X size={17} /></button>
        </div>
        <p className="modal-copy">
          TraceForge 会恢复本次任务记录过的文件修改。用户之后的编辑会作为冲突保留；
          回滚一旦完成，无法自动重做。
        </p>
        {localError && <p className="decision-error" role="alert">{systemMessageLabel(localError)}</p>}
        <div className="modal-actions">
          <span>{submitting ? "正在核对快照与用户后续修改…" : "只处理本次任务记录的快照。"}</span>
          <div className="button-row">
            <button className="button ghost" type="button" onClick={close} disabled={submitting} data-dialog-initial-focus>取消</button>
            <button
              className="button warning"
              type="button"
              disabled={submitting}
              onClick={() => {
                if (submittingRef.current) return;
                submittingRef.current = true;
                setSubmitting(true);
                setLocalError(null);
                void onConfirm()
                  .catch((reason: unknown) => {
                    submittingRef.current = false;
                    setLocalError(reason instanceof Error ? reason.message : String(reason));
                    setSubmitting(false);
                  });
              }}
            >
              {submitting ? <LoaderCircle className="spin" size={14} /> : <RotateCcw size={14} />}
              {submitting ? "正在回滚" : "回滚文件"}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

function ActivityFeed({ run, events }: { run: Run; events: RunEvent[] }) {
  const end = useRef<HTMLDivElement>(null);
  const conversation = projectConversationEvents(events);
  const progressSequences = new Set(
    projectProgressEvents(events).map((event) => event.seq),
  );
  const trace = events.filter((event) =>
    [
      "message",
      "tool.completed",
      "plan.gated",
      "verification.completed",
      "repair.started",
      "model.retry",
      "run.resumed",
      "error",
    ].includes(event.type) && (event.type !== "message" || progressSequences.has(event.seq))
  );
  const hasPersistedTurn = conversation.some((event) => event.type === "turn.started");
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [events.length]);
  return (
    <div className="activity-feed">
      {!hasPersistedTurn && (
        <article className="conversation-turn user-turn">
          <span>你</span><p>{run.task}</p>
        </article>
      )}
      {conversation.map((event) => {
        if (event.type === "turn.started") {
          const approvalMode = String(event.payload.approval_mode ?? "automatic") as ApprovalMode;
          const reasoningEffort = String(event.payload.reasoning_effort ?? "auto") as ReasoningEffort;
          return <article className="conversation-turn user-turn" key={event.seq}><span>你 · 第 {String(event.payload.index ?? "?")} 轮 · {approvalModeLabel(approvalMode)} · {reasoningEffortLabel(reasoningEffort)}</span><p>{String(event.payload.request ?? "")}</p></article>;
        }
        if (event.type === "turn.completed") {
          const index = Number(event.payload.index ?? 0);
          const eventFiles = Array.isArray(event.payload.changed_files)
            ? event.payload.changed_files.filter((path): path is string => typeof path === "string")
            : [];
          const changedFiles = eventFiles.length > 0
            ? eventFiles
            : run.turns.find((turn) => turn.index === index)?.changed_files ?? [];
          return (
            <Fragment key={event.seq}>
              <article className={`conversation-turn assistant-turn ${String(event.payload.outcome ?? "")}`}><span>TraceForge</span><ReactMarkdown>{String(event.payload.summary ?? "本轮已结束。")}</ReactMarkdown></article>
              {changedFiles.length > 0 && <TurnChangedFiles index={index} paths={changedFiles} />}
            </Fragment>
          );
        }
        return <article className="conversation-turn assistant-turn" key={event.seq}><span>TraceForge</span><ReactMarkdown>{String(event.payload.content ?? "")}</ReactMarkdown></article>;
      })}
      {conversation.length === 0 && isActiveState(run.state) && (
        <div className="thinking-row"><LoaderCircle className="spin" size={16} /><span>{presentState(run.state).label}…</span></div>
      )}
      {trace.length > 0 && (
        <details className="trace-details">
          <summary>
            {isActiveState(run.state) ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />}
            <span><strong>{isActiveState(run.state) ? presentState(run.state).label : "查看工作记录"}</strong><small>{trace.length} 条 Trace · 计划、工具、验证均可审计</small></span>
            <ChevronDown size={14} />
          </summary>
          <div className="trace-body">{trace.map((event) => <ActivityItem event={event} key={event.seq} />)}</div>
        </details>
      )}
      <div ref={end} />
    </div>
  );
}

function TurnChangedFiles({ index, paths }: { index: number; paths: string[] }) {
  return (
    <section className="turn-changed-files" aria-label={`第 ${index} 轮由编辑工具更改的文件`}>
      <div className="turn-files-heading">
        <FileDiff size={14} />
        <strong>本轮更改</strong>
        <span>{paths.length} 个文件</span>
      </div>
      <div className="turn-file-list">
        {paths.map((path) => <code title={path} key={path}>{path}</code>)}
      </div>
      <small>仅列出 TraceForge 编辑工具产生的实际内容变更；右侧“差异”是整个任务的累计净差异。</small>
    </section>
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
    const agentContinues = decision === "agent_continues";
    return (
      <article className="activity evidence-activity policy-activity">
        <div className="activity-icon"><Zap size={15} /></div>
        <div><span className="activity-label">{agentContinues ? "Agent 工作边界" : "计划模式检查点"}</span><strong>{planDecisionLabel(decision)} · {riskLabel(String(event.payload.risk ?? "unknown"))}</strong>{reasons.length > 0 && <p>{reasons.map(planGateReasonLabel).join(" · ")}</p>}</div>
      </article>
    );
  }
  if (event.type === "verification.completed") {
    return (
      <article className="activity evidence-activity verifier-activity">
        <div className="activity-icon"><ShieldCheck size={15} /></div>
        <div><span className="activity-label">完成后复核</span><strong>{verdictLabel(String(event.payload.verdict ?? "inconclusive"))}</strong><p>{String(event.payload.summary ?? "没有记录摘要。")}</p></div>
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
        <div><span className="activity-label">有界修复</span><strong>第 {String(event.payload.cycle ?? "?")} / {String(event.payload.limit ?? "?")} 轮</strong><p>{String(event.payload.summary ?? "完成后复核要求进行修复。")}</p></div>
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
  const permission = result.metadata?.permission as {
    mode?: ApprovalMode;
    outcome?: string;
  } | undefined;
  const [expanded, setExpanded] = useState(!result.ok);
  return (
    <article className={`activity tool-card ${result.ok ? "ok" : "bad"}`}>
      <div className="activity-icon"><TerminalSquare size={15} /></div>
      <div className="tool-body">
        <button className="tool-summary" type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
          <span className="tool-summary-copy"><span className="tool-title"><code>{call.name ?? "工具"}</code><em>{result.ok ? "已完成" : "失败"}</em></span><span className="tool-args">{formatArguments(call.arguments)}</span></span>
          <span className="tool-evidence-badges">
            {permission && <span className={`permission-evidence ${permission.mode ?? "automatic"}`}>{permissionOutcomeLabel(permission.mode, permission.outcome)}</span>}
            {sandbox && <span className={`sandbox-evidence ${sandbox.status ?? "not_used"}`}>{sandbox.status === "enforced" ? sandbox.backend : sandboxStatusLabel(sandbox.status ?? "not_used")}</span>}
          </span>
          <ChevronDown className={`tool-chevron ${expanded ? "expanded" : ""}`} size={13} />
        </button>
        {expanded && (result.output || result.error) && <pre>{result.error ?? result.output}</pre>}
      </div>
    </article>
  );
}

function ClarificationPanel({
  request,
  onSubmit,
}: {
  request: NonNullable<Run["clarification"]>;
  onSubmit: (answers: ClarificationAnswer[]) => Promise<void>;
}) {
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const complete = request.questions.every((question) => selected[question.id] || custom[question.id]?.trim());
  return (
    <div className="decision-panel clarification-panel" aria-busy={submitting}>
      <div className="decision-heading"><MessageSquareMore size={19} /><div><p>需求澄清 · 第 {request.round} 轮</p><h3>这些选择会影响具体实现</h3></div></div>
      {request.questions.map((question) => (
        <fieldset key={question.id} disabled={submitting}>
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
      {localError && <p className="decision-error" role="alert">{systemMessageLabel(localError)}</p>}
      <div className="decision-actions">
        <span className="muted">{submitting ? "答复已持久接收，正在应用…" : "TraceForge 会依据这些选择重新规划。"}</span>
        <button
          className="button primary"
          type="button"
          disabled={!complete || submitting}
          onClick={() => {
            if (submittingRef.current) return;
            const answers = request.questions.map((question) => (
              custom[question.id]?.trim()
                ? { question_id: question.id, custom_text: custom[question.id].trim() }
                : { question_id: question.id, option_id: selected[question.id] }
            ));
            submittingRef.current = true;
            setSubmitting(true);
            setLocalError(null);
            void onSubmit(answers).catch((reason: unknown) => {
              submittingRef.current = false;
              setLocalError(reason instanceof Error ? reason.message : String(reason));
              setSubmitting(false);
            });
          }}
        >
          {submitting ? <LoaderCircle className="spin" size={15} /> : <Send size={15} />}
          {submitting ? "正在提交" : "继续"}
        </button>
      </div>
    </div>
  );
}

function PlanPanel({
  runId,
  plan,
  gate,
  onDecision,
}: {
  runId: string;
  plan: TaskPlan;
  gate: PlanGate | null;
  onDecision: (decision: "approve" | "revise", feedback?: string) => Promise<void>;
}) {
  const [revising, setRevising] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const submit = (decision: "approve" | "revise", revision = "") => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setLocalError(null);
    void onDecision(decision, revision).catch((reason: unknown) => {
      submittingRef.current = false;
      setLocalError(reason instanceof Error ? reason.message : String(reason));
      setSubmitting(false);
    });
  };
  return (
    <div className="decision-panel plan-panel" aria-busy={submitting}>
      <div className="decision-heading"><ClipboardCheck size={19} /><div><p>计划审批</p><h3>{plan.summary}</h3></div></div>
      {gate && <PlanGateSummary gate={gate} />}
      <div className="plan-document"><ReactMarkdown>{plan.markdown}</ReactMarkdown></div>
      {revising && <textarea className="revision-input" autoFocus disabled={submitting} placeholder="这份计划需要怎样调整？" value={feedback} onChange={(event) => setFeedback(event.target.value)} />}
      {localError && <p className="decision-error" role="alert">{systemMessageLabel(localError)}</p>}
      <div className="decision-actions">
        <a className="button ghost" href={`/api/runs/${runId}/plan.md`} download><Download size={14} /> 下载 Markdown</a>
        <div className="button-row">
          {revising ? <>
            <button className="button ghost" type="button" disabled={submitting} onClick={() => setRevising(false)}>返回</button>
            <button className="button" type="button" disabled={!feedback.trim() || submitting} onClick={() => submit("revise", feedback.trim())}>
              {submitting && <LoaderCircle className="spin" size={14} />}
              {submitting ? "正在提交" : "提交修改意见"}
            </button>
          </> : <>
            <button className="button ghost" type="button" disabled={submitting} onClick={() => setRevising(true)}>调整计划</button>
            <button className="button primary" type="button" disabled={submitting} onClick={() => submit("approve")}>
              {submitting ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}
              {submitting ? "正在批准" : "批准并执行"}
            </button>
          </>}
        </div>
      </div>
    </div>
  );
}

function ApprovalPanel({
  approval,
  onDecision,
}: {
  approval: NonNullable<Run["pending_approval"]>;
  onDecision: (approved: boolean) => Promise<void>;
}) {
  const [submitting, setSubmitting] = useState<"approve" | "reject" | null>(null);
  const submittingRef = useRef(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const submit = (approved: boolean) => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(approved ? "approve" : "reject");
    setLocalError(null);
    void onDecision(approved).catch((reason: unknown) => {
      submittingRef.current = false;
      setLocalError(reason instanceof Error ? reason.message : String(reason));
      setSubmitting(null);
    });
  };
  return (
    <div className="decision-panel approval-panel" aria-busy={Boolean(submitting)}>
      <div className="decision-heading"><AlertTriangle size={19} /><div><p>动作审批 · {approvalModeLabel(approval.approval_mode)} · {riskLabel(approval.risk)}</p><h3>{approval.summary}</h3></div></div>
      <p className="approval-reason">{approval.reason}</p>
      <pre>{JSON.stringify(approval.tool_call.arguments, null, 2)}</pre>
      {localError && <p className="decision-error" role="alert">{systemMessageLabel(localError)}</p>}
      <div className="decision-actions"><span className="muted">{submitting ? "决策已持久接收，正在进入下一步…" : <>{approval.sandbox_bypass_on_approve ? "批准后将仅本次绕过 OS 沙箱；凭证环境仍会清理。" : "批准不会主动绕过命令沙箱；主机若显示“仅策略限制”，则仍无 OS 级隔离。"} 工作区路径规则与证据记录始终保留。</>}</span><div className="button-row"><button className="button danger-ghost" type="button" disabled={Boolean(submitting)} onClick={() => submit(false)}>{submitting === "reject" && <LoaderCircle className="spin" size={14} />} {submitting === "reject" ? "正在拒绝" : "拒绝"}</button><button className="button warning" type="button" disabled={Boolean(submitting)} onClick={() => submit(true)}>{submitting === "approve" && <LoaderCircle className="spin" size={14} />} {submitting === "approve" ? "正在批准" : approval.sandbox_bypass_on_approve ? "绕过并批准一次" : "批准并继续"}</button></div></div>
    </div>
  );
}

function CompletionSummary({
  run,
  turnIndex,
  onProof,
}: {
  run: Run;
  turnIndex: number | null;
  onProof: (turnIndex: number) => void;
}) {
  const passed = run.plan?.acceptance_checks.filter((check) => check.status === "passed").length ?? 0;
  return (
    <div className="completion-summary">
      <CheckCircle2 size={17} />
      <div>
        <strong>本轮已完成</strong>
        {turnIndex === null && <small className="proof-unavailable">暂无冻结证据</small>}
        <span>{passed} 项检查通过{run.verification?.summary ? ` · ${run.verification.summary}` : ""}</span>
      </div>
      {turnIndex !== null && (
        <button className="button ghost" type="button" onClick={() => onProof(turnIndex)}><Fingerprint size={14} /> 查看证据</button>
      )}
    </div>
  );
}

function HistoricalProofSummary({
  turnIndex,
  onProof,
}: {
  turnIndex: number;
  onProof: (turnIndex: number) => void;
}) {
  return (
    <div className="completion-summary historical-proof-summary">
      <Fingerprint size={17} />
      <div>
        <strong>第 {turnIndex} 轮已证实</strong>
        <span>查看截至该轮冻结的累计证据；不代表当前轮已完成。</span>
      </div>
      <button className="button ghost" type="button" onClick={() => onProof(turnIndex)}>
        查看历史证据
      </button>
    </div>
  );
}

function RollbackSummary({
  result,
  successorRunId,
  onOpenSuccessor,
  focusRef,
}: {
  result: RollbackResult | null;
  successorRunId: string | null;
  onOpenSuccessor: (runId: string) => void;
  focusRef: React.RefObject<HTMLDivElement | null>;
}) {
  const successorButton = successorRunId ? (
    <button
      className="button ghost"
      type="button"
      onClick={() => onOpenSuccessor(successorRunId)}
    >
      <ArrowRight size={14} /> 打开续跑任务
    </button>
  ) : null;
  if (!result) {
    return (
      <div className="rollback-summary-focus" ref={focusRef} tabIndex={-1}>
        <Notice icon={<RotateCcw size={18} />} title="本次任务已回滚">
          <div className="rollback-result">
            <p>回滚记录已持久保存。旧任务保留为审计历史；继续修改时会创建新的安全快照边界。</p>
            {successorButton}
          </div>
        </Notice>
      </div>
    );
  }
  const changed = result.restored.length + result.removed.length;
  return (
    <div className="rollback-summary-focus" ref={focusRef} tabIndex={-1}>
      <Notice
        icon={result.conflicts.length ? <AlertTriangle size={18} /> : <RotateCcw size={18} />}
        title={result.conflicts.length ? "已回滚，用户后续修改已保留" : "本次任务已安全回滚"}
        danger={false}
      >
        <div className="rollback-result">
          <p>
            {changed} 个文件已恢复或移除；{result.conflicts.length
              ? `${result.conflicts.length} 个冲突文件未覆盖。`
              : "没有检测到冲突。"}
          </p>
          {result.restored.length > 0 && <RollbackFileGroup label="已恢复" paths={result.restored} />}
          {result.removed.length > 0 && <RollbackFileGroup label="已移除" paths={result.removed} />}
          {result.conflicts.length > 0 && <RollbackFileGroup label="保留冲突" paths={result.conflicts} />}
          {successorButton}
        </div>
      </Notice>
    </div>
  );
}

function RollbackFileGroup({ label, paths }: { label: string; paths: string[] }) {
  return (
    <details>
      <summary>{label} · {paths.length}</summary>
      <ul>{paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul>
    </details>
  );
}

function latestRollbackResult(events: RunEvent[]): RollbackResult | null {
  const payload = [...events].reverse().find(
    (event) => event.type === "rollback.completed",
  )?.payload;
  if (!payload) return null;
  const paths = (key: keyof RollbackResult) => (
    Array.isArray(payload[key])
      ? payload[key].filter((value): value is string => typeof value === "string")
      : []
  );
  return {
    restored: paths("restored"),
    removed: paths("removed"),
    conflicts: paths("conflicts"),
  };
}

function FollowUpComposer({
  defaultApprovalMode,
  defaultReasoningEffort,
  provider,
  providerReady,
  onSubmit,
}: {
  defaultApprovalMode: ApprovalMode;
  defaultReasoningEffort: ReasoningEffort;
  provider: ProviderConfig | null;
  providerReady: boolean;
  onSubmit: (
    prompt: string,
    mode: InteractionMode,
    approvalMode: ApprovalMode,
    reasoningEffort: ReasoningEffort,
  ) => Promise<void>;
}) {
  const [prompt, setPrompt] = useState("");
  const [planMode, setPlanMode] = useState(false);
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>(
    defaultApprovalMode === "full_access" ? "automatic" : defaultApprovalMode,
  );
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>(
    supportedReasoningEffort(
      provider?.supported_reasoning_efforts ?? DEFAULT_REASONING_EFFORTS,
      defaultReasoningEffort,
    ),
  );
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const supportedReasoning = provider?.supported_reasoning_efforts
    ?? DEFAULT_REASONING_EFFORTS;
  const effectiveReasoningEffort = supportedReasoningEffort(
    supportedReasoning,
    reasoningEffort,
  );
  useEffect(() => {
    if (effectiveReasoningEffort !== reasoningEffort) {
      setReasoningEffort(effectiveReasoningEffort);
    }
  }, [effectiveReasoningEffort, reasoningEffort]);
  return (
    <form
      className="follow-up-composer"
      aria-busy={submitting}
      onSubmit={(event) => {
        event.preventDefault();
        const request = prompt.trim();
        if (!request || submittingRef.current || !providerReady) return;
        submittingRef.current = true;
        setSubmitting(true);
        persistApprovalMode(approvalMode);
        void onSubmit(
          request,
          planMode ? "plan" : "agent",
          approvalMode,
          effectiveReasoningEffort,
        )
          .then(() => setPrompt(""))
          .catch(() => undefined)
          .finally(() => {
            submittingRef.current = false;
            setSubmitting(false);
          });
      }}
    >
      <textarea
        disabled={submitting}
        value={prompt}
        onChange={(event) => setPrompt(event.target.value)}
        onKeyDown={(event) => {
          if (shouldSubmitPrompt({
            key: event.key,
            shiftKey: event.shiftKey,
            isComposing: event.nativeEvent.isComposing,
          }) && providerReady && !submitting) {
            event.preventDefault();
            event.currentTarget.form?.requestSubmit();
          }
        }}
        placeholder="继续提问或提出修改，TraceForge 会保留同一任务的上下文与证据…"
        rows={3}
        aria-label="继续此任务"
      />
      <div className="follow-up-actions">
        <label className="toggle-row">
          <input type="checkbox" disabled={submitting} checked={planMode} onChange={(event) => setPlanMode(event.target.checked)} />
          <span className="toggle" />
          <span><strong>计划模式</strong><small>本轮先审计划再执行</small></span>
        </label>
        <label className="approval-mode-select">
          <span>权限</span>
          <select
            aria-label="本轮权限模式"
            value={approvalMode}
            disabled={submitting}
            onChange={(event) => {
              const value = event.target.value as ApprovalMode;
              setApprovalMode(value);
              persistApprovalMode(value);
            }}
          >
            <option value="manual">手动审批（逐项）</option>
            <option value="automatic">自动审批（规则）</option>
            <option value="full_access">完全访问（工作区）</option>
          </select>
        </label>
        <ReasoningEffortPicker
          value={effectiveReasoningEffort}
          onChange={setReasoningEffort}
          provider={provider}
          disabled={submitting}
        />
        <span className="follow-up-hint">
          {providerReady
            ? "Enter 发送 · Shift+Enter 换行"
            : provider?.api_key_configured
              ? "需要验证模型连接：请在模型设置中重新测试"
              : "需要配置模型：请先填写凭证并测试连接"}
        </span>
        <button className="button primary" type="submit" disabled={!prompt.trim() || submitting || !providerReady}>
          {submitting ? <LoaderCircle className="spin" size={15} /> : <Send size={15} />}
          继续任务
        </button>
      </div>
    </form>
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
  collapsed,
  width,
  maximumWidth,
  onResize,
}: {
  run: Run | null;
  events: RunEvent[];
  diff: string;
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  mobileOpen: boolean;
  onMobileClose: () => void;
  collapsed: boolean;
  width: number;
  maximumWidth: number;
  onResize: (width: number) => void;
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
      className={`inspector panel-edge ${collapsed ? "collapsed" : ""} ${mobileOpen ? "mobile-open" : ""}`}
      role={mobileOpen ? "dialog" : undefined}
      aria-modal={mobileOpen || undefined}
      aria-label={mobileOpen ? "任务详情" : undefined}
      onKeyDown={onDrawerKeyDown}
    >
      <div className="drawer-mobile-heading inspector-drawer-heading">
        <strong>任务详情</strong>
        <button className="icon-button" type="button" onClick={onMobileClose} aria-label="关闭任务详情" data-drawer-initial-focus><X size={17} /></button>
      </div>
      <nav className="inspector-tabs" aria-label="任务详情视图">{tabs.map(({ id, label, icon: Icon }) => <button type="button" className={tab === id ? "active" : ""} aria-label={label} title={label} onClick={() => onTab(id)} key={id}><Icon size={14} /><span>{label}</span></button>)}</nav>
      <div className="inspector-content">
        {!run && <div className="inspector-empty"><FileDiff size={26} /><p>请选择一条任务查看详细记录。</p></div>}
        {run && tab === "timeline" && <Timeline events={events} />}
        {run && tab === "diff" && <DiffView diff={diff} />}
        {run && tab === "checks" && <ChecksView run={run} />}
        {run && tab === "verifier" && <VerifierView run={run} />}
      </div>
      <PanelResizer
        side="right"
        width={width}
        minimum={RIGHT_PANEL_MIN}
        maximum={maximumWidth}
        defaultWidth={RIGHT_PANEL_DEFAULT}
        onResize={onResize}
      />
    </aside>
  );
}

function Timeline({ events }: { events: RunEvent[] }) {
  return <div className="timeline">{events.length === 0 && <p className="muted">正在等待证据…</p>}{events.map((event) => <div className="timeline-row" key={event.seq}><span className={`timeline-marker ${event.type.includes("error") ? "bad" : event.type.includes("completed") ? "good" : ""}`} /><div><strong>{eventTypeLabel(event.type)}</strong><small>{clockTime(event.created_at)} · #{event.seq}</small><p>{eventSummary(event)}</p></div></div>)}</div>;
}

function DiffView({ diff }: { diff: string }) {
  const lines = useMemo(() => parseDiff(diff), [diff]);
  if (!diff) return <div className="inspector-empty"><FileDiff size={26} /><p>任务当前没有累计净差异。</p></div>;
  return <pre className="diff-view">{lines.map((line, index) => <span className={`diff-${line.kind}`} key={`${index}-${line.text}`}><i>{index + 1}</i>{line.text || " "}</span>)}</pre>;
}

function ChecksView({ run }: { run: Run }) {
  if (run.state === "answered") return <div className="inspector-empty"><ClipboardCheck size={26} /><p>本轮是直接答复，不需要实施计划或验收检查。</p></div>;
  if (!run.plan) return <div className="inspector-empty"><ClipboardCheck size={26} /><p>规划完成后会显示检查项。</p></div>;
  return <div className="checks-view">{run.plan_gate && <PlanGateSummary gate={run.plan_gate} />}<div className="section-heading"><div className="section-kicker">Markdown 计划</div><a href={`/api/runs/${run.id}/plan.md`} download title="下载计划"><Download size={13} /> 下载</a></div><div className="plan-document compact"><ReactMarkdown>{run.plan.markdown}</ReactMarkdown></div><div className="section-kicker contract-kicker">实时验收证据</div>{run.plan.acceptance_checks.map((check) => <article className={`check-row ${check.status}`} key={check.id}><div className="check-icon">{check.status === "passed" ? <Check size={14} /> : check.status === "failed" ? <X size={14} /> : <Circle size={10} />}</div><div><strong>{check.label}</strong>{check.command && <code>{check.command.join(" ")}</code>}{check.evidence && <pre>{check.evidence}</pre>}</div><span>{checkStatusLabel(check.status)}</span></article>)}</div>;
}

function PlanGateBadge({ gate }: { gate: PlanGate }) {
  const automatic = gate.decision !== "approval_required";
  return <span className={`plan-gate-badge ${automatic ? "fast" : "reviewed"}`}>{automatic ? <Zap size={10} /> : <ShieldCheck size={10} />}{gate.decision === "agent_continues" ? "Agent 自动" : gate.decision === "auto_approved" ? "快速路径" : riskLabel(gate.risk)}</span>;
}

function ApprovalModeBadge({ mode }: { mode: ApprovalMode }) {
  return (
    <span className={`approval-mode-badge ${mode}`} title={approvalModeDescription(mode)}>
      <ShieldCheck size={10} />{approvalModeLabel(mode)}
    </span>
  );
}

function ReasoningEffortBadge({ effort }: { effort: ReasoningEffort }) {
  return (
    <span
      className={`reasoning-effort-badge ${effort}`}
      title={reasoningEffortDescription(effort)}
    >
      <Gauge size={10} />{reasoningEffortLabel(effort)}
    </span>
  );
}

function PlanGateSummary({ gate }: { gate: PlanGate }) {
  const automatic = gate.decision !== "approval_required";
  const title = gate.decision === "agent_continues"
    ? "普通 Agent 已自动继续"
    : gate.decision === "auto_approved"
      ? "低风险快速路径"
      : "计划模式等待确认";
  return <div className={`plan-gate-summary ${automatic ? "fast" : "review"}`}><div>{automatic ? <Zap size={15} /> : <ShieldCheck size={15} />}<span><strong>{title}</strong><small>工作边界评估 · {riskLabel(gate.risk)}</small></span></div><ul>{gate.reasons.map((reason) => <li key={reason}>{planGateReasonLabel(reason)}</li>)}</ul></div>;
}

function ProofPackDialog({
  pack,
  loadState,
  runId,
  turnIndexes,
  selectedTurnIndex,
  onTurnChange,
  onRetry,
  onClose,
}: {
  pack: ProofPack | null;
  loadState: ProofLoadState | null;
  runId: string;
  turnIndexes: number[];
  selectedTurnIndex: number;
  onTurnChange: (turnIndex: number) => void;
  onRetry: () => void;
  onClose: () => void;
}) {
  const { dialogRef, onDialogKeyDown } = useDialogFocus(onClose);
  const visiblePack = pack?.run_id === runId
    && proofPackTurnIndex(pack) === selectedTurnIndex
    ? pack
    : null;
  const visibleLoadState = loadState?.runId === runId
    && loadState.turnIndex === selectedTurnIndex
    ? loadState
    : null;
  const loadError = visibleLoadState?.status === "error"
    ? visibleLoadState.error
    : null;
  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={dialogRef} className="modal proof-modal" role="dialog" aria-modal="true" aria-labelledby="proof-title" aria-busy={!visiblePack && !loadError} tabIndex={-1} onKeyDown={onDialogKeyDown}>
        <div className="modal-heading proof-heading">
          <div><p className="eyebrow">可审计完成记录</p><h2 id="proof-title">截至第 {selectedTurnIndex} 轮的累计证据包</h2></div>
          {turnIndexes.length > 1 && (
            <label className="proof-turn-picker">
              <span>证据轮次</span>
              <select
                aria-label="选择证据轮次"
                value={selectedTurnIndex}
                onChange={(event) => onTurnChange(Number(event.target.value))}
              >
                {turnIndexes.map((turnIndex) => (
                  <option value={turnIndex} key={turnIndex}>第 {turnIndex} 轮</option>
                ))}
              </select>
            </label>
          )}
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭证据包"><X size={17} /></button>
        </div>
        {!visiblePack && loadError ? (
          <div className="proof-load-error" role="alert">
            <AlertTriangle size={22} />
            <div><strong>证据读取失败</strong><span>{systemMessageLabel(loadError)}</span></div>
            <button className="button ghost" type="button" onClick={onRetry}>重试</button>
          </div>
        ) : !visiblePack ? (
          <div className="proof-loading" role="status" aria-live="polite"><LoaderCircle className="spin" size={18} /> 正在读取持久化证据…</div>
        ) : <>
          <div className={`proof-verdict ${visiblePack.proof_status}`}><div className="evidence-seal"><Fingerprint size={22} /></div><div><span>证明状态</span><strong>{proofStatusLabel(visiblePack.proof_status)}</strong><small>{visiblePack.verification?.summary ?? "仍在汇总证据。"}</small></div><div><span>新鲜检查</span><strong>{visiblePack.checks_fresh ? "是" : "否"}</strong></div></div>
          <div className="proof-grid"><article><span>工作边界</span><strong>{visiblePack.plan_gate ? planDecisionLabel(visiblePack.plan_gate.decision) : "未评估"}</strong><small>{visiblePack.plan_gate?.reasons.map(planGateReasonLabel).join(" · ")}</small></article><article><span>动作权限</span><strong>{approvalModeLabel(visiblePack.turns.at(-1)?.approval_mode ?? "automatic")}</strong><small>逐轮冻结 · 实际授权与 bypass 写入工具事件</small></article><article><span>思考强度</span><strong>{reasoningEffortLabel(visiblePack.turns.at(-1)?.reasoning_effort ?? "auto")}</strong><small>逐轮冻结 · 仅记录请求档位，不展示隐藏推理</small></article><article><span>累计变更范围</span><strong>{visiblePack.changed_files.length} 个文件</strong><small>截至第 {selectedTurnIndex} 轮 · {visiblePack.changed_files.join(" · ") || "没有快照"} · {diffSourceLabel(visiblePack.diff_source)}</small></article><article><span>命令沙箱</span><strong>{sandboxStatusLabel(visiblePack.command_sandbox.status)}</strong><small>{visiblePack.command_sandbox.backends.join(" · ") || "未记录操作系统沙箱后端"} · {visiblePack.command_sandbox.sandboxed_commands} 个已强制隔离 · {visiblePack.command_sandbox.not_executed_commands} 个运行前拦截</small></article><article><span>冻结时回滚能力</span><strong>{rollbackStatusLabel(visiblePack.rollback.status)}</strong><small>生成证据时的能力快照，不代表当前仍可回滚</small></article><article><span>事件账本</span><strong>截至事件 #{visiblePack.event_through_seq} · {visiblePack.event_count} 条</strong><small>第 {selectedTurnIndex} 轮 · {visiblePack.step_count} 个工具动作 · {visiblePack.repair_cycles} 轮修复</small></article></div>
          <div className="proof-section"><div className="section-kicker">原始任务</div><p>{visiblePack.task}</p></div>
          <div className="proof-section"><div className="section-kicker">截至该轮的验收证据</div>{visiblePack.plan?.acceptance_checks.map((check) => <div className="proof-check" key={check.id}><CheckCircle2 size={14} /><span><strong>{check.label}</strong><small>{check.evidence || check.command?.join(" ") || "等待证据"}</small></span><em>{checkStatusLabel(check.status)}</em></div>) ?? <p className="muted">尚无完成契约。</p>}</div>
          <div className="digest-card"><Fingerprint size={15} /><span><small>完整证据包 SHA-256</small><code>{visiblePack.artifact_sha256}</code><small>语义证据：{visiblePack.evidence_sha256}</small></span></div>
        </>}
        <div className="modal-actions"><span className="muted">这是截至所选成功轮冻结的累计快照；后续轮次不会改写它。</span><div className="button-row"><button className="button ghost" type="button" onClick={onClose}>关闭</button><a className={`button primary ${visiblePack ? "" : "disabled"}`} href={visiblePack ? `/api/runs/${runId}/proof-pack.md?turn_index=${selectedTurnIndex}` : undefined} download><Download size={14} /> 下载 Markdown</a></div></div>
      </section>
    </div>
  );
}

function VerifierView({ run }: { run: Run }) {
  const report = run.verification;
  if (run.state === "answered") return <div className="inspector-empty"><ShieldCheck size={26} /><p>本轮没有执行修改，因此不生成完成后复核结论或证据包。</p></div>;
  if (!report) return <div className="inspector-empty"><ShieldCheck size={26} /><p>{run.verifier_enabled ? "实现和检查完成后，将自动进行独立只读复核。" : "本次运行未启用完成后复核。"}</p></div>;
  return <div className="verifier-view"><div className={`verdict ${report.verdict}`}><ShieldCheck size={22} /><div><span>完成后复核</span><strong>{verdictLabel(report.verdict)}</strong></div></div><p>{report.summary}</p>{report.findings.map((finding) => <article className="finding" key={`${finding.severity}-${finding.title}`}><span>{severityLabel(finding.severity)}</span><strong>{finding.title}</strong><p>{finding.evidence}</p>{finding.suggested_fix && <small>建议修复：{finding.suggested_fix}</small>}</article>)}</div>;
}

function StateBadge({ state }: { state: RunState }) {
  const presentation = presentState(state);
  return <span className={`state-badge ${presentation.tone}`}>{presentation.tone === "active" && <LoaderCircle className="spin" size={12} />}{presentation.label}</span>;
}

function Notice({ icon, title, children, danger = false }: { icon: React.ReactNode; title: string; children: React.ReactNode; danger?: boolean }) {
  return <div className={`notice ${danger ? "danger" : ""}`}><div>{icon}</div><div><strong>{title}</strong><div className="notice-copy">{children}</div></div></div>;
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

function agentPhaseLabel(phase: string): string {
  return labelFromMap(phase.toLowerCase(), {
    agent: "智能体",
    planning: "规划",
    building: "执行",
    executing: "执行",
    answering: "答复",
    verifying: "验证",
    recovery: "恢复",
  });
}

function approvalModeLabel(mode: string): string {
  return labelFromMap(mode, {
    manual: "手动审批",
    automatic: "自动审批",
    full_access: "完全访问（工作区）",
  });
}

function approvalModeShortDescription(mode: ApprovalMode): string {
  return {
    manual: "编辑与命令逐项确认",
    automatic: "计划内自动，未知项询问",
    full_access: "沙箱内免询问",
  }[mode];
}

function approvalModeDescription(mode: ApprovalMode): string {
  return {
    manual: "读取自动执行；每次编辑和命令均等待你的确认。",
    automatic: "本地确定性规则自动放行计划内动作，未知或越界动作转人工。",
    full_access: "工作区与 OS 沙箱内不询问；不可关闭的高危命令和路径边界仍会拦截。",
  }[mode];
}

function reasoningEffortLabel(effort: string): string {
  return labelFromMap(effort, {
    auto: "模型默认",
    none: "关闭",
    minimal: "最小",
    low: "低",
    medium: "中",
    high: "高",
    xhigh: "极高",
    max: "最大",
  });
}

function reasoningEffortTriggerLabel(
  effort: ReasoningEffort,
  provider: ProviderConfig | null,
): string {
  if (effort !== "auto") return reasoningEffortLabel(effort);
  const knownDefault = provider?.default_reasoning_effort;
  return knownDefault
    ? `模型默认 · ${reasoningEffortLabel(knownDefault)}`
    : "模型默认";
}

function reasoningEffortDescription(effort: ReasoningEffort): string {
  if (effort === "auto") return "由当前模型决定，不表示最低强度。";
  if (effort === "none") return "关闭当前模型的思考模式。";
  if (["xhigh", "max"].includes(effort)) {
    return "适合最难任务，通常增加延迟与 token/额度用量，并不保证结果一定更好。";
  }
  if (effort === "high") return "为复杂任务请求更深入的推理，通常会增加延迟与用量。";
  if (effort === "medium") return "在质量、延迟与用量之间取平衡。";
  return "为简单或延迟敏感任务减少推理用量。";
}

function reasoningCapabilityDescription(provider: ProviderConfig | null): string {
  if (!provider) return "正在读取当前模型的精确能力。";
  if (provider.supported_reasoning_efforts.length === 1) {
    return "当前模型仅提供这个档位。";
  }
  return "只显示当前模型明确支持的档位；更高不一定更好。";
}

function permissionOutcomeLabel(
  mode: ApprovalMode | undefined,
  outcome: string | undefined,
): string {
  if (outcome === "denied") return "策略拦截";
  if (outcome === "user_rejected") return "用户拒绝";
  if (outcome === "user_approved") return "手动批准";
  return mode === "full_access" ? "工作区免询问" : "规则自动";
}

function planDecisionLabel(decision: string): string {
  return labelFromMap(decision, {
    assessed: "已评估",
    auto_approved: "自动通过",
    approval_required: "需要审批",
    agent_continues: "Agent 自动继续",
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

function contextWindowSourceLabel(source: ProviderConfig["context_window_source"]): string {
  return labelFromMap(source, {
    configured: "手动设置",
    catalog: "精确模型目录",
    fallback: "保守回退",
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
  const reasoningLabel = reasoningErrorLabel(message);
  if (reasoningLabel) return reasoningLabel;
  const exact: Record<string, string> = {
    "Scripted provider is ready.": "脚本化模型服务已就绪。",
    "Connection and native tool calling verified.": "连接和原生工具调用已验证。",
    "The model responded, but did not complete the native tool-call probe.": "模型已响应，但未完成原生工具调用探测。",
    "Credential file must be smaller than 16 KiB": "凭证文件必须小于 16 KiB",
    "Credential file must be owner-only; run chmod 600 on it": "凭证文件必须仅限所有者访问；请对它运行 chmod 600",
    "Credential file could not be read as UTF-8": "无法按 UTF-8 读取凭证文件",
    "Credential file must contain exactly one non-empty line": "凭证文件必须恰好包含一行非空内容",
    "Configure a credential file or set OPENAI_API_KEY before starting a run": "开始任务前，请配置凭证文件或设置 OPENAI_API_KEY",
    "Test and verify the model connection before starting or continuing a task": "开始或继续任务前，请先测试并验证模型连接",
    "Model must not be empty": "模型不能为空",
    "Base URL must be an absolute http:// or https:// URL": "接口地址必须是以 http:// 或 https:// 开头的绝对 URL",
    "Pause, stop, or finish running work before changing model settings": "请先暂停、停止或完成当前任务，再修改模型设置",
    "This workspace already has an active or interrupted run": "此工作区已有正在执行或已中断的任务",
    "Approval is no longer pending": "该审批已不再处于等待状态",
    "The local file manager could not be opened": "无法启动本地文件管理器",
    "The local file manager could not open this workspace": "本地文件管理器无法打开此工作目录",
    "The recorded workspace path no longer points to its original directory": "任务记录的工作目录已被替换，已拒绝打开",
    "Provider-private replay state could not be stored safely": "模型返回的私有推理状态无法安全保存，任务已停止且未保留该内容。",
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
    "Agent mode continues without a plan approval pause": "普通 Agent 不因计划审批而暂停",
    "Plan mode pauses for review before implementation": "计划模式会在实施前等待确认",
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
      "answered", "cancelled", "interrupted", "rolled_back",
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
  if (event.type === "model.requested") {
    return "内部模型调用已记录";
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

function eventTypeLabel(type: string): string {
  return type === "model.requested" ? "模型调用" : type;
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
