import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { mergeEvents } from "./lib";
import type { AppStatus, ClarificationAnswer, Run, RunEvent } from "./types";

const refreshEventTypes = new Set([
  "state.changed",
  "clarification.requested",
  "clarification.answered",
  "plan.updated",
  "approval.requested",
  "approval.resolved",
  "verification.completed",
  "run.completed",
  "rollback.completed",
]);

export function useTraceForge() {
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [diff, setDiff] = useState("");
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lastSeq = useRef(0);

  const refreshRuns = useCallback(async () => {
    const next = await api.listRuns();
    setRuns(next);
    return next;
  }, []);

  const refreshRun = useCallback(async (runId: string) => {
    const [nextRun, nextDiff] = await Promise.all([api.getRun(runId), api.getDiff(runId)]);
    setRun(nextRun);
    setDiff(nextDiff.diff);
    setRuns((current) => [nextRun, ...current.filter((item) => item.id !== nextRun.id)]);
  }, []);

  useEffect(() => {
    void Promise.all([api.status(), refreshRuns()])
      .then(([nextStatus, nextRuns]) => {
        setStatus(nextStatus);
        if (!selectedRunId && nextRuns.length) setSelectedRunId(nextRuns[0].id);
      })
      .catch((reason: unknown) => setError(String(reason)));
  }, [refreshRuns, selectedRunId]);

  useEffect(() => {
    if (!selectedRunId) {
      setRun(null);
      setEvents([]);
      setDiff("");
      return;
    }
    let disposed = false;
    let reconnectTimer: number | undefined;
    let socket: WebSocket | undefined;
    lastSeq.current = 0;

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
        if (refreshEventTypes.has(event.type)) void refreshRun(selectedRunId);
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
  }, [refreshRun, selectedRunId]);

  const perform = useCallback(
    async (operation: () => Promise<unknown>) => {
      setError(null);
      try {
        await operation();
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
    async (task: string, verifierEnabled: boolean) => {
      setError(null);
      const created = await api.createRun(task, verifierEnabled);
      setRuns((current) => [created, ...current]);
      setSelectedRunId(created.id);
    },
    [],
  );

  return {
    status,
    runs,
    run,
    events,
    diff,
    connected,
    error,
    clearError: () => setError(null),
    selectedRunId,
    selectRun: setSelectedRunId,
    createRun,
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
