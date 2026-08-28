import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { mergeEvents, preferNewerRun, proofPackTurnIndex } from "./lib";
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
  "approval.requested",
  "approval.resolved",
  "verification.completed",
  "repair.started",
  "run.resumed",
  "run.completed",
  "turn.started",
  "turn.completed",
  "rollback.completed",
]);

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
  const lastSeq = useRef(0);
  const selectedRunIdRef = useRef<string | null>(null);
  const diffRequestVersions = useRef(new Map<string, number>());
  const diffEventVersions = useRef(new Map<string, number>());
  const proofRequestVersion = useRef(0);
  const run = selectedRunId
    ? runs.find((candidate) => candidate.id === selectedRunId) ?? null
    : null;

  const selectRun = useCallback((runId: string | null) => {
    if (selectedRunIdRef.current === runId) return;
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

  const refreshRuns = useCallback(async () => {
    const next = await api.listRuns();
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
    let socket: WebSocket | undefined;
    lastSeq.current = 0;
    setProofPack(null);
    setProofLoadState(null);
    const ownsSelection = () => !disposed && selectedRunIdRef.current === selectedRunId;

    const connect = () => {
      if (!ownsSelection()) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(
        `${protocol}//${window.location.host}/api/runs/${selectedRunId}/events?after_seq=${lastSeq.current}`,
      );
      socket.onopen = () => {
        if (ownsSelection()) setConnected(true);
      };
      socket.onmessage = (message) => {
        if (!ownsSelection()) return;
        const event = JSON.parse(message.data as string) as RunEvent;
        if (event.run_id !== selectedRunId) return;
        lastSeq.current = Math.max(lastSeq.current, event.seq);
        setEvents((current) => mergeEvents(current, [event]));
        if (refreshEventTypes.has(event.type)) {
          void refreshRunMetadata(selectedRunId).catch((reason: unknown) => {
            if (ownsSelection()) setError(String(reason));
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
      socket.onclose = () => {
        if (!ownsSelection()) return;
        setConnected(false);
        reconnectTimer = window.setTimeout(connect, 800);
      };
      socket.onerror = () => socket?.close();
    };

    void Promise.all([
      refreshRun(selectedRunId),
      api.getEvents(selectedRunId),
    ])
      .then(([, initialEvents]) => {
        if (!ownsSelection()) return;
        const ownedEvents = initialEvents.filter((event) => event.run_id === selectedRunId);
        setEvents(ownedEvents);
        lastSeq.current = ownedEvents.at(-1)?.seq ?? 0;
        connect();
      })
      .catch((reason: unknown) => {
        if (ownsSelection()) setError(String(reason));
      });

    return () => {
      disposed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [refreshRun, refreshRunMetadata, selectedRunId]);

  const perform = useCallback(
    async (runId: string, operation: () => Promise<unknown>) => {
      if (selectedRunIdRef.current !== runId) {
        throw new Error("当前任务已切换，请在当前任务中重试此操作");
      }
      if (selectedRunIdRef.current === runId) setError(null);
      try {
        await operation();
        if (selectedRunIdRef.current === runId) setProofPack(null);
        await refreshRun(runId);
        await refreshRuns();
      } catch (reason) {
        if (selectedRunIdRef.current === runId) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
        throw reason;
      }
    },
    [refreshRun, refreshRuns],
  );

  const createRun = useCallback(
    async (
      task: string,
      mode: InteractionMode,
      approvalMode: ApprovalMode,
      reasoningEffort: ReasoningEffort,
      target: RunTarget,
    ) => {
      setError(null);
      try {
        const created = await api.createRun(
          task,
          mode,
          approvalMode,
          reasoningEffort,
          target,
        );
        setRuns((current) => [created, ...current]);
        selectRun(created.id);
        setStatus(await api.status());
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
        throw reason;
      }
    },
    [selectRun],
  );

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
    setError(null);
    try {
      const result = await api.openWorkspace(runId);
      if (!result.supported) throw new Error("当前系统没有可用的文件管理器");
      return result;
    } catch (reason) {
      if (selectedRunIdRef.current === runId) {
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
    followUp: (
      prompt: string,
      mode: InteractionMode,
      approvalMode: ApprovalMode,
      reasoningEffort: ReasoningEffort,
    ) => run && perform(run.id, () => api.followUp(
      run.id,
      prompt,
      mode,
      approvalMode,
      reasoningEffort,
    )),
    createProject,
    saveProvider,
    testProvider,
    loadProofPack,
    listDirectories: api.listDirectories,
    chooseDirectory,
    openWorkspace,
    answerQuestions: (answers: ClarificationAnswer[]) =>
      run && perform(run.id, () => api.answerQuestions(run.id, answers)),
    decidePlan: (decision: "approve" | "revise", feedback = "") =>
      run && perform(run.id, () => api.decidePlan(run.id, decision, feedback)),
    decideAction: (approved: boolean) =>
      run?.pending_approval &&
      perform(run.id, () => api.decideAction(run.id, run.pending_approval!.id, approved)),
    cancel: () => run && perform(run.id, () => api.cancel(run.id)),
    resume: () => run && perform(run.id, () => api.resume(run.id)),
    rollback: () => run && perform(run.id, () => api.rollback(run.id)),
  };
}
