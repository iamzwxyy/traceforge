import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import {
  backgroundRunRefreshDelay,
  mergeEvents,
  preferNewerRun,
  proofPackTurnIndex,
} from "./lib";
import type {
  AppStatus,
  ApprovalMode,
  ClarificationAnswer,
  InteractionMode,
  Project,
  ProofPack,
  ProviderConfig,
  ProviderProbe,
  ReasoningEffort,
  ProviderUpdate,
  Run,
  RunEvent,
  RollbackResult,
  RunTarget,
} from "./types";

const refreshEventTypes = new Set([
  "message",
  "tool.completed",
  "state.changed",
  "clarification.requested",
  "clarification.answered",
  "plan.updated",
  "plan.gated",
  "plan.resolved",
  "approval.requested",
  "approval.resolved",
  "decision.abandoned",
  "verification.completed",
  "repair.started",
  "run.resumed",
  "run.completed",
  "turn.started",
  "turn.completed",
  "rollback.completed",
]);

function isRunEvent(value: unknown): value is RunEvent {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const event = value as Record<string, unknown>;
  return typeof event.run_id === "string"
    && typeof event.seq === "number"
    && Number.isInteger(event.seq)
    && event.seq > 0
    && typeof event.type === "string"
    && typeof event.payload === "object"
    && event.payload !== null
    && !Array.isArray(event.payload)
    && typeof event.created_at === "string";
}

export interface ProofLoadState {
  runId: string;
  turnIndex: number | null;
  status: "loading" | "ready" | "error";
  error: string | null;
}

export function useTraceForge() {
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [provider, setProvider] = useState<ProviderConfig | null>(null);
  const [proofPack, setProofPack] = useState<ProofPack | null>(null);
  const [proofLoadState, setProofLoadState] = useState<ProofLoadState | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [diff, setDiff] = useState("");
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [backgroundRefreshEpoch, setBackgroundRefreshEpoch] = useState(0);
  const [rollbackResults, setRollbackResults] = useState<Record<string, RollbackResult>>({});
  const lastSeq = useRef(0);
  const selectedRunIdRef = useRef<string | null>(null);
  const selectionVersion = useRef(0);
  const actionRequestVersions = useRef(new Map<string, number>());
  const workspaceRequestVersions = useRef(new Map<string, number>());
  const diffRequestVersions = useRef(new Map<string, number>());
  const diffEventVersions = useRef(new Map<string, number>());
  const proofRequestVersion = useRef(0);
  const creationRequestVersion = useRef(0);
  const runsRefreshPromise = useRef<Promise<Run[]> | null>(null);
  const run = selectedRunId
    ? runs.find((candidate) => candidate.id === selectedRunId) ?? null
    : null;

  const selectRun = useCallback((runId: string | null) => {
    if (selectedRunIdRef.current === runId) return;
    selectionVersion.current += 1;
    selectedRunIdRef.current = runId;
    if (runId) {
      diffRequestVersions.current.set(
        runId,
        (diffRequestVersions.current.get(runId) ?? 0) + 1,
      );
    }
    proofRequestVersion.current += 1;
    lastSeq.current = 0;
    setEvents([]);
    setDiff("");
    setProofPack(null);
    setProofLoadState(null);
    setConnected(false);
    setError(null);
    setSelectedRunId(runId);
  }, []);

  const refreshRuns = useCallback(async (forceFresh = false): Promise<Run[]> => {
    if (forceFresh && runsRefreshPromise.current) {
      await runsRefreshPromise.current.catch(() => undefined);
    }
    if (runsRefreshPromise.current) return runsRefreshPromise.current;
    const request = api.listRuns()
      .then((next) => {
        setRuns((current) => {
          const returnedIds = new Set(next.map((candidate) => candidate.id));
          return [
            ...next.map((candidate) => preferNewerRun(
              current.find((existing) => existing.id === candidate.id) ?? null,
              candidate,
            )),
            ...current.filter((candidate) => !returnedIds.has(candidate.id)),
          ].sort((left, right) => right.updated_at.localeCompare(left.updated_at));
        });
        return next;
      })
      .finally(() => {
        if (runsRefreshPromise.current === request) runsRefreshPromise.current = null;
      });
    runsRefreshPromise.current = request;
    return request;
  }, []);

  const storeRun = useCallback((nextRun: Run) => {
    setRuns((current) => {
      const previous = current.find((item) => item.id === nextRun.id) ?? null;
      const selected = preferNewerRun(previous, nextRun);
      return [selected, ...current.filter((item) => item.id !== nextRun.id)].sort(
        (left, right) => right.updated_at.localeCompare(left.updated_at),
      );
    });
  }, []);

  const refreshRunMetadata = useCallback(async (runId: string) => {
    const nextRun = await api.getRun(runId);
    storeRun(nextRun);
  }, [storeRun]);

  const refreshRun = useCallback(async (runId: string) => {
    const requestVersion = (diffRequestVersions.current.get(runId) ?? 0) + 1;
    diffRequestVersions.current.set(runId, requestVersion);
    const eventVersion = diffEventVersions.current.get(runId) ?? 0;
    const metadataRequest = api.getRun(runId).then((nextRun) => {
      storeRun(nextRun);
      return nextRun;
    });
    const [, nextDiff] = await Promise.all([metadataRequest, api.getDiff(runId)]);
    if (
      selectedRunIdRef.current === runId
      && diffRequestVersions.current.get(runId) === requestVersion
      && (diffEventVersions.current.get(runId) ?? 0) === eventVersion
    ) {
      setDiff(nextDiff.diff);
    }
  }, [storeRun]);

  useEffect(() => {
    void Promise.all([api.status(), refreshRuns(), api.listProjects(), api.getProvider()])
      .then(([nextStatus, nextRuns, nextProjects, nextProvider]) => {
        setStatus(nextStatus);
        setProjects(nextProjects);
        setProvider(nextProvider);
        if (!selectedRunIdRef.current && nextRuns.length) selectRun(nextRuns[0].id);
      })
      .catch((reason: unknown) => setError(String(reason)));
  }, [refreshRuns, selectRun]);

  const refreshRunsQuietly = useCallback(async (): Promise<"refreshed" | "failed" | "skipped"> => {
    if (document.visibilityState !== "visible") return "skipped";
    try {
      await refreshRuns();
      return "refreshed";
    } catch {
      return "failed";
    }
  }, [refreshRuns]);

  useEffect(() => {
    const refreshOnVisibility = () => {
      if (document.visibilityState === "visible") {
        void refreshRunsQuietly().then((result) => {
          if (result === "refreshed") setBackgroundRefreshEpoch((current) => current + 1);
        });
      }
    };
    const refreshOnFocus = () => {
      void refreshRunsQuietly().then((result) => {
        if (result === "refreshed") setBackgroundRefreshEpoch((current) => current + 1);
      });
    };
    window.addEventListener("focus", refreshOnFocus);
    document.addEventListener("visibilitychange", refreshOnVisibility);
    return () => {
      window.removeEventListener("focus", refreshOnFocus);
      document.removeEventListener("visibilitychange", refreshOnVisibility);
    };
  }, [refreshRunsQuietly]);

  const backgroundRefreshDelay = backgroundRunRefreshDelay(runs, selectedRunId);
  useEffect(() => {
    if (backgroundRefreshDelay === null) return;
    let disposed = false;
    let timer: number | undefined;
    let consecutiveFailures = 0;
    const schedule = (delay: number) => {
      timer = window.setTimeout(() => {
        void refreshRunsQuietly().then((result) => {
          if (disposed) return;
          if (result === "refreshed") consecutiveFailures = 0;
          else if (result === "failed") consecutiveFailures += 1;
          const nextDelay = result === "failed"
            ? Math.min(backgroundRefreshDelay * (2 ** consecutiveFailures), 60_000)
            : backgroundRefreshDelay;
          schedule(nextDelay);
        });
      }, delay);
    };
    schedule(backgroundRefreshDelay);
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [backgroundRefreshDelay, backgroundRefreshEpoch, refreshRunsQuietly]);

  useEffect(() => {
    if (!selectedRunId) {
      setEvents([]);
      setDiff("");
      setProofPack(null);
      setProofLoadState(null);
      return;
    }
    let disposed = false;
    let reconnectTimer: number | undefined;
    let activeSocket: WebSocket | undefined;
    lastSeq.current = 0;
    setProofPack(null);
    setProofLoadState(null);
    const ownsSelection = () => !disposed && selectedRunIdRef.current === selectedRunId;

    const connect = () => {
      if (!ownsSelection()) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const currentSocket = new WebSocket(
        `${protocol}//${window.location.host}/api/runs/${selectedRunId}/events?after_seq=${lastSeq.current}`,
      );
      activeSocket = currentSocket;
      const ownsSocket = () => ownsSelection() && activeSocket === currentSocket;
      currentSocket.onopen = () => {
        if (ownsSocket()) setConnected(true);
      };
      currentSocket.onmessage = (message) => {
        if (!ownsSocket()) return;
        let event: RunEvent;
        try {
          const candidate: unknown = JSON.parse(message.data as string);
          if (!isRunEvent(candidate)) throw new Error("invalid event shape");
          event = candidate;
        } catch {
          setError("实时事件格式无效；TraceForge 正在重新连接并从持久化记录恢复。");
          currentSocket.close();
          return;
        }
        if (event.run_id !== selectedRunId) return;
        lastSeq.current = Math.max(lastSeq.current, event.seq);
        setEvents((current) => mergeEvents(current, [event]));
        if (refreshEventTypes.has(event.type)) {
          void refreshRunMetadata(selectedRunId).catch((reason: unknown) => {
            if (ownsSocket()) setError(String(reason));
          });
        }
        if (event.type === "diff.updated") {
          const payloadDiff = event.payload.diff;
          if (typeof payloadDiff === "string") {
            diffEventVersions.current.set(
              selectedRunId,
              (diffEventVersions.current.get(selectedRunId) ?? 0) + 1,
            );
            setDiff(payloadDiff);
          }
        }
      };
      currentSocket.onclose = () => {
        if (!ownsSocket()) return;
        activeSocket = undefined;
        setConnected(false);
        reconnectTimer = window.setTimeout(connect, 800);
      };
      currentSocket.onerror = () => {
        if (ownsSocket()) currentSocket.close();
      };
    };

    const bootstrapEvents = () => {
      if (!ownsSelection()) return;
      void api.getEvents(selectedRunId).then((initialEvents) => {
        if (!ownsSelection()) return;
        const ownedEvents = initialEvents.filter((event) => event.run_id === selectedRunId);
        setEvents(ownedEvents);
        lastSeq.current = ownedEvents.at(-1)?.seq ?? 0;
        connect();
      })
      .catch((reason: unknown) => {
        if (!ownsSelection()) return;
        setError(String(reason));
        reconnectTimer = window.setTimeout(bootstrapEvents, 800);
      });
    };
    void refreshRun(selectedRunId).catch((reason: unknown) => {
      if (ownsSelection()) setError(String(reason));
    });
    bootstrapEvents();

    return () => {
      disposed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      const socketToClose = activeSocket;
      activeSocket = undefined;
      socketToClose?.close();
    };
  }, [refreshRun, refreshRunMetadata, selectedRunId]);

  const synchronizeAfterMutation = useCallback((
    runId: string,
    ownsFeedback: () => boolean,
  ) => {
    const synchronizationError = "操作已成功接收，但状态同步失败；TraceForge 正在重试。";
    const synchronize = () => Promise.all([refreshRun(runId), refreshRuns(true)]);
    void synchronize().catch(() => {
      if (ownsFeedback()) setError(synchronizationError);
      window.setTimeout(() => {
        void synchronize()
          .then(() => {
            if (ownsFeedback()) {
              setError((current) => current === synchronizationError ? null : current);
            }
          })
          .catch(() => undefined);
      }, 800);
    });
  }, [refreshRun, refreshRuns]);

  const perform = useCallback(
    async <Result,>(
      runId: string,
      operation: () => Promise<Result>,
      options: { surfaceOperationError?: boolean } = {},
    ): Promise<Result> => {
      if (selectedRunIdRef.current !== runId) {
        throw new Error("当前任务已切换，请在当前任务中重试此操作");
      }
      const surfaceOperationError = options.surfaceOperationError ?? true;
      const requestVersion = (actionRequestVersions.current.get(runId) ?? 0) + 1;
      actionRequestVersions.current.set(runId, requestVersion);
      const requestSelectionVersion = selectionVersion.current;
      const ownsFeedback = () => (
        selectedRunIdRef.current === runId
        && selectionVersion.current === requestSelectionVersion
        && actionRequestVersions.current.get(runId) === requestVersion
      );
      if (surfaceOperationError && ownsFeedback()) setError(null);
      let result: Result;
      try {
        result = await operation();
      } catch (reason) {
        if (surfaceOperationError && ownsFeedback()) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
        throw reason;
      }
      if (ownsFeedback()) setProofPack(null);
      synchronizeAfterMutation(runId, ownsFeedback);
      return result;
    },
    [synchronizeAfterMutation],
  );

  const createRun = useCallback(
    async (
      task: string,
      mode: InteractionMode,
      approvalMode: ApprovalMode,
      reasoningEffort: ReasoningEffort,
      target: RunTarget,
      ownsSurface: () => boolean,
    ): Promise<Run> => {
      const requestVersion = creationRequestVersion.current + 1;
      creationRequestVersion.current = requestVersion;
      const ownsFeedback = () => (
        creationRequestVersion.current === requestVersion && ownsSurface()
      );
      if (ownsFeedback()) setError(null);
      try {
        const created = await api.createRun(
          task,
          mode,
          approvalMode,
          reasoningEffort,
          target,
        );
        storeRun(created);
        void api.status()
          .then((nextStatus) => {
            if (creationRequestVersion.current === requestVersion) setStatus(nextStatus);
          })
          .catch(() => undefined);
        return created;
      } catch (reason) {
        if (ownsFeedback()) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
        throw reason;
      }
    },
    [storeRun],
  );

  const followUp = useCallback(async (
    prompt: string,
    mode: InteractionMode,
    approvalMode: ApprovalMode,
    reasoningEffort: ReasoningEffort,
  ): Promise<Run | undefined> => {
    if (!run) return undefined;
    const runId = run.id;
    if (run.state !== "rolled_back") {
      const continued = await perform(runId, () => api.followUp(
        runId,
        prompt,
        mode,
        approvalMode,
        reasoningEffort,
      ));
      storeRun(continued);
      return continued;
    }
    if (selectedRunIdRef.current !== runId) {
      throw new Error("当前任务已切换，请在当前任务中重试此操作");
    }
    const requestVersion = (actionRequestVersions.current.get(runId) ?? 0) + 1;
    actionRequestVersions.current.set(runId, requestVersion);
    const requestSelectionVersion = selectionVersion.current;
    const ownsFeedback = () => (
      selectedRunIdRef.current === runId
      && selectionVersion.current === requestSelectionVersion
      && actionRequestVersions.current.get(runId) === requestVersion
    );
    if (ownsFeedback()) setError(null);
    let successor: Run;
    try {
      successor = await api.followUp(
        runId,
        prompt,
        mode,
        approvalMode,
        reasoningEffort,
      );
    } catch (reason) {
      await Promise.allSettled([
        refreshRunMetadata(runId),
        refreshRuns(true),
      ]);
      if (ownsFeedback()) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
      throw reason;
    }
    storeRun(successor);
    if (ownsFeedback()) selectRun(successor.id);
    void refreshRuns(true).catch(() => undefined);
    return successor;
  }, [perform, refreshRunMetadata, refreshRuns, run, selectRun, storeRun]);

  const createProject = useCallback(
    async (name: string, root: string, createDirectory: boolean) => {
      setError(null);
      try {
        const created = await api.createProject(name, root, createDirectory);
        setProjects((current) => [created, ...current]);
        setStatus(await api.status());
        return created;
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
        throw reason;
      }
    },
    [],
  );

  const chooseDirectory = useCallback(async () => {
    setError(null);
    try {
      return await api.chooseDirectory();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      throw reason;
    }
  }, []);

  const openWorkspace = useCallback(async (runId: string) => {
    if (selectedRunIdRef.current !== runId) {
      throw new Error("当前任务已切换，请在当前任务中重试此操作");
    }
    const requestVersion = (workspaceRequestVersions.current.get(runId) ?? 0) + 1;
    workspaceRequestVersions.current.set(runId, requestVersion);
    const requestSelectionVersion = selectionVersion.current;
    const ownsFeedback = () => (
      selectedRunIdRef.current === runId
      && selectionVersion.current === requestSelectionVersion
      && workspaceRequestVersions.current.get(runId) === requestVersion
    );
    if (ownsFeedback()) setError(null);
    try {
      const result = await api.openWorkspace(runId);
      if (!result.supported) throw new Error("当前系统没有可用的文件管理器");
      return result;
    } catch (reason) {
      if (ownsFeedback()) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
      throw reason;
    }
  }, []);

  const saveProvider = useCallback(
    async (config: ProviderUpdate) => {
      setError(null);
      try {
        const saved = await api.updateProvider(config);
        setProvider(saved);
        setStatus(await api.status());
        return saved;
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
        throw reason;
      }
    },
    [],
  );

  const testProvider = useCallback(async (config: ProviderUpdate): Promise<ProviderProbe> => {
    setError(null);
    try {
      const result = await api.testProvider(config);
      setProvider(result.provider);
      setStatus(await api.status());
      return result;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      throw reason;
    }
  }, []);

  const loadProofPack = useCallback(async (
    runId: string,
    turnIndex?: number,
  ): Promise<ProofPack> => {
    if (selectedRunIdRef.current !== runId) {
      throw new Error("当前任务已切换，请在当前任务中重新打开证据包");
    }
    const requestVersion = proofRequestVersion.current + 1;
    proofRequestVersion.current = requestVersion;
    if (selectedRunIdRef.current === runId) {
      setError(null);
      setProofPack(null);
      setProofLoadState({
        runId,
        turnIndex: turnIndex ?? null,
        status: "loading",
        error: null,
      });
    }
    try {
      const pack = await api.getProofPack(runId, turnIndex);
      if (pack.run_id !== runId) {
        throw new Error("证据包响应与请求的任务不匹配");
      }
      if (turnIndex !== undefined && proofPackTurnIndex(pack) !== turnIndex) {
        throw new Error("证据包响应与请求的轮次不匹配");
      }
      if (
        selectedRunIdRef.current === runId
        && proofRequestVersion.current === requestVersion
      ) {
        setProofPack(pack);
        setProofLoadState({
          runId,
          turnIndex: proofPackTurnIndex(pack),
          status: "ready",
          error: null,
        });
      }
      return pack;
    } catch (reason) {
      if (
        selectedRunIdRef.current === runId
        && proofRequestVersion.current === requestVersion
      ) {
        setProofLoadState({
          runId,
          turnIndex: turnIndex ?? null,
          status: "error",
          error: reason instanceof Error ? reason.message : String(reason),
        });
      }
      throw reason;
    }
  }, []);

  return {
    status,
    projects,
    provider,
    proofPack,
    proofLoadState,
    rollbackResult: run ? rollbackResults[run.id] ?? null : null,
    runs,
    run,
    events,
    diff,
    connected,
    error,
    clearError: () => setError(null),
    selectedRunId,
    selectRun,
    createRun,
    followUp,
    createProject,
    saveProvider,
    testProvider,
    loadProofPack,
    listDirectories: api.listDirectories,
    chooseDirectory,
    openWorkspace,
    answerQuestions: (answers: ClarificationAnswer[]): Promise<void> => {
      if (!run?.decision_request_id) {
        return Promise.reject(new Error("澄清请求已更新，请按当前问题重试"));
      }
      return perform(run.id, () => api.answerQuestions(
        run.id,
        run.decision_request_id!,
        answers,
      ), { surfaceOperationError: false }).then(() => undefined);
    },
    decidePlan: (decision: "approve" | "revise", feedback = ""): Promise<void> => {
      if (!run?.decision_request_id) {
        return Promise.reject(new Error("计划审批请求已更新，请重新审阅当前计划"));
      }
      return perform(run.id, () => api.decidePlan(
        run.id,
        run.decision_request_id!,
        decision,
        feedback,
      ), { surfaceOperationError: false }).then(() => undefined);
    },
    decideAction: (approved: boolean): Promise<void> => {
      if (!run?.pending_approval) {
        return Promise.reject(new Error("动作审批请求已更新，请重新审阅当前动作"));
      }
      return perform(
        run.id,
        () => api.decideAction(run.id, run.pending_approval!.id, approved),
        { surfaceOperationError: false },
      ).then(() => undefined);
    },
    cancel: () => run && perform(run.id, () => api.cancel(run.id)).then((next) => {
      storeRun(next);
      return next;
    }),
    resume: () => run && perform(run.id, () => api.resume(run.id)).then((next) => {
      storeRun(next);
      return next;
    }),
    rollback: async (): Promise<RollbackResult> => {
      if (!run) throw new Error("请选择要回滚的任务");
      const runId = run.id;
      const result = await perform(
        runId,
        () => api.rollback(runId),
        { surfaceOperationError: false },
      );
      setRollbackResults((current) => ({ ...current, [runId]: result }));
      setRuns((current) => current.map((candidate) => (
        candidate.id === runId
          ? {
              ...candidate,
              state: "rolled_back",
              decision_request_id: null,
              decision_kind: null,
              clarification: null,
              pending_approval: null,
              updated_at: new Date().toISOString(),
            }
          : candidate
      )));
      return result;
    },
  };
}
