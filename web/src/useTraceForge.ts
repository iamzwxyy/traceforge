import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { mergeEvents, preferNewerRun } from "./lib";
import type {
  AppStatus,
  ClarificationAnswer,
  InteractionMode,
  Project,
  ProofPack,
  ProviderConfig,
  ProviderProbe,
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

export function useTraceForge() {
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [provider, setProvider] = useState<ProviderConfig | null>(null);
  const [proofPack, setProofPack] = useState<ProofPack | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [diff, setDiff] = useState("");
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lastSeq = useRef(0);
  const selectedRunIdRef = useRef<string | null>(null);

  const selectRun = useCallback((runId: string | null) => {
    selectedRunIdRef.current = runId;
    setSelectedRunId(runId);
  }, []);

  const refreshRuns = useCallback(async () => {
    const next = await api.listRuns();
    setRuns(next);
    return next;
  }, []);

  const storeRun = useCallback((nextRun: Run) => {
    setRun((current) =>
      selectedRunIdRef.current === nextRun.id ? preferNewerRun(current, nextRun) : current,
    );
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
    const [nextRun, nextDiff] = await Promise.all([api.getRun(runId), api.getDiff(runId)]);
    storeRun(nextRun);
    setDiff(nextDiff.diff);
  }, [storeRun]);

  useEffect(() => {
    void Promise.all([api.status(), refreshRuns(), api.listProjects(), api.getProvider()])
      .then(([nextStatus, nextRuns, nextProjects, nextProvider]) => {
        setStatus(nextStatus);
        setProjects(nextProjects);
        setProvider(nextProvider);
        if (!selectedRunId && nextRuns.length) selectRun(nextRuns[0].id);
      })
      .catch((reason: unknown) => setError(String(reason)));
  }, [refreshRuns, selectRun, selectedRunId]);

  useEffect(() => {
    if (!selectedRunId) {
      setRun(null);
      setEvents([]);
      setDiff("");
      setProofPack(null);
      return;
    }
    let disposed = false;
    let reconnectTimer: number | undefined;
    let socket: WebSocket | undefined;
    lastSeq.current = 0;
    setProofPack(null);

    const connect = () => {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(
        `${protocol}//${window.location.host}/api/runs/${selectedRunId}/events?after_seq=${lastSeq.current}`,
      );
      socket.onopen = () => setConnected(true);
      socket.onmessage = (message) => {
        const event = JSON.parse(message.data as string) as RunEvent;
        lastSeq.current = Math.max(lastSeq.current, event.seq);
        setEvents((current) => mergeEvents(current, [event]));
        if (refreshEventTypes.has(event.type)) void refreshRunMetadata(selectedRunId);
        if (event.type === "diff.updated") {
          const payloadDiff = event.payload.diff;
          if (typeof payloadDiff === "string") setDiff(payloadDiff);
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (!disposed) reconnectTimer = window.setTimeout(connect, 800);
      };
      socket.onerror = () => socket?.close();
    };

    void Promise.all([
      refreshRun(selectedRunId),
      api.getEvents(selectedRunId),
    ])
      .then(([, initialEvents]) => {
        if (disposed) return;
        setEvents(initialEvents);
        lastSeq.current = initialEvents.at(-1)?.seq ?? 0;
        connect();
      })
      .catch((reason: unknown) => setError(String(reason)));

    return () => {
      disposed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [refreshRun, refreshRunMetadata, selectedRunId]);

  const perform = useCallback(
    async (operation: () => Promise<unknown>) => {
      setError(null);
      try {
        await operation();
        setProofPack(null);
        if (selectedRunId) await refreshRun(selectedRunId);
        await refreshRuns();
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
        throw reason;
      }
    },
    [refreshRun, refreshRuns, selectedRunId],
  );

  const createRun = useCallback(
    async (task: string, mode: InteractionMode, target: RunTarget) => {
      setError(null);
      try {
        const created = await api.createRun(task, mode, target);
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

  const testProvider = useCallback(async (): Promise<ProviderProbe> => {
    setError(null);
    try {
      return await api.testProvider();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      throw reason;
    }
  }, []);

  const loadProofPack = useCallback(async (runId: string): Promise<ProofPack> => {
    setError(null);
    try {
      const pack = await api.getProofPack(runId);
      setProofPack(pack);
      return pack;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      throw reason;
    }
  }, []);

  return {
    status,
    projects,
    provider,
    proofPack,
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
    followUp: (prompt: string, mode: InteractionMode) =>
      run && perform(() => api.followUp(run.id, prompt, mode)),
    createProject,
    saveProvider,
    testProvider,
    loadProofPack,
    listDirectories: api.listDirectories,
    chooseDirectory,
    answerQuestions: (answers: ClarificationAnswer[]) =>
      run && perform(() => api.answerQuestions(run.id, answers)),
    decidePlan: (decision: "approve" | "revise", feedback = "") =>
      run && perform(() => api.decidePlan(run.id, decision, feedback)),
    decideAction: (approved: boolean) =>
      run?.pending_approval &&
      perform(() => api.decideAction(run.id, run.pending_approval!.id, approved)),
    cancel: () => run && perform(() => api.cancel(run.id)),
    resume: () => run && perform(() => api.resume(run.id)),
    rollback: () => run && perform(() => api.rollback(run.id)),
  };
}
