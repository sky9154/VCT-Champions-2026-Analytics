import { useEffect, useRef, useState } from "react";
import { isAbortError, toApiError, type ApiError } from "../api/client";
import { API_DATA_REFRESH_EVENT } from "../api/refresh";


export type ApiResourceState<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "success"; data: T }
  | { status: "error"; error: ApiError };

export const useApiResource = <T,>(
  key: string | null,
  load: (signal: AbortSignal) => Promise<T>,
  options: { keepPreviousData?: boolean } = {}
) => {
  const loadRef = useRef(load);
  const previousData = useRef<{ data: T } | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [completedRefreshVersion, setCompletedRefreshVersion] = useState(0);
  const [resource, setResource] = useState<{
    identity: string | null;
    state: ApiResourceState<T>;
  }>({ identity: null, state: { status: "idle" } });
  const [retryIndex, setRetryIndex] = useState(0);
  const identity = key === null ? null : JSON.stringify([key, retryIndex, refreshVersion]);
  const keepPreviousData = options.keepPreviousData ?? false;

  loadRef.current = load;

  useEffect(() => {
    const handleRefresh = () => setRefreshVersion((current) => current + 1);
    window.addEventListener(API_DATA_REFRESH_EVENT, handleRefresh);

    return () => window.removeEventListener(API_DATA_REFRESH_EVENT, handleRefresh);
  }, []);

  useEffect(() => {
    if (key === null) {
      setResource({ identity: null, state: { status: "idle" } });
      previousData.current = null;

      return;
    }

    const controller = new AbortController();
    setResource({ identity, state: { status: "loading" } });

    loadRef.current(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) {
          previousData.current = { data };
          setCompletedRefreshVersion(refreshVersion);
          setResource({ identity, state: { status: "success", data } });
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !isAbortError(error)) {
          setCompletedRefreshVersion(refreshVersion);
          setResource({ identity, state: { status: "error", error: toApiError(error) } });
        }
      });

    return () => controller.abort();
  }, [identity, key, refreshVersion]);

  const currentState = resource.identity === identity
    ? resource.state
    : key === null
      ? { status: "idle" as const }
      : { status: "loading" as const };

  const preservingRefresh = refreshVersion > completedRefreshVersion;
  const retainedData = (keepPreviousData || preservingRefresh) && key !== null ? previousData.current : null;
  const hasPreviousData = retainedData !== null;
  const isRefreshing = hasPreviousData && currentState.status !== "success" && currentState.status !== "error";
  const refreshError = hasPreviousData && currentState.status === "error" ? currentState.error : null;
  const state = retainedData !== null && currentState.status !== "success"
    ? { status: "success" as const, data: retainedData.data }
    : currentState;

  return {
    state,
    isInitialLoading: state.status === "loading" || state.status === "idle",
    isRefreshing,
    refreshError,
    retry: () => setRetryIndex((current) => current + 1)
  };
};
