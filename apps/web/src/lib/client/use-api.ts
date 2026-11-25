"use client";
/** Data hooks over the fetch wrapper: one resource (`useApi`) and cursor-paginated lists (`useCursorList`). */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ApiError, toApiError } from "./api";

export type ApiState<T> = {
  data: T | undefined;
  error: ApiError | undefined;
  loading: boolean;
  reload: () => void;
  setData: (updater: (prev: T | undefined) => T | undefined) => void;
};

export function useApi<T>(path: string | null): ApiState<T> {
  const [data, setDataState] = useState<T | undefined>(undefined);
  const [error, setError] = useState<ApiError | undefined>(undefined);
  const [loading, setLoading] = useState<boolean>(path !== null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (path === null) {
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    setError(undefined);
    api<T>(path, { signal: ctrl.signal }).then(
      (value) => {
        if (ctrl.signal.aborted) return;
        setDataState(value);
        setLoading(false);
      },
      (err: unknown) => {
        if (ctrl.signal.aborted) return;
        setError(toApiError(err));
        setLoading(false);
      },
    );
    return () => ctrl.abort();
  }, [path, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const setData = useCallback((updater: (prev: T | undefined) => T | undefined) => setDataState(updater), []);
  return { data, error, loading, reload, setData };
}

export type CursorListState<T> = {
  items: T[];
  error: ApiError | undefined;
  loading: boolean;
  loadingMore: boolean;
  hasMore: boolean;
  loadMore: () => void;
  reload: () => void;
};

type PageShape<T> = { items: T[]; next_cursor?: string | null };

/** Cursor pagination over `{items, next_cursor}` lists. `path` may already carry filters. */
export function useCursorList<T>(path: string | null, pageSize = 50): CursorListState<T> {
  const [items, setItems] = useState<T[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | undefined>(undefined);
  const [loading, setLoading] = useState(path !== null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [nonce, setNonce] = useState(0);
  const generation = useRef(0);

  const pageUrl = useCallback(
    (c: string | null) => {
      if (!path) return null;
      const sep = path.includes("?") ? "&" : "?";
      const base = `${path}${sep}limit=${pageSize}`;
      return c ? `${base}&cursor=${encodeURIComponent(c)}` : base;
    },
    [path, pageSize],
  );

  useEffect(() => {
    const url = pageUrl(null);
    const gen = ++generation.current;
    setItems([]);
    setCursor(null);
    if (!url) {
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    setError(undefined);
    api<PageShape<T>>(url, { signal: ctrl.signal }).then(
      (page) => {
        if (gen !== generation.current) return;
        setItems(page.items ?? []);
        setCursor(page.next_cursor ?? null);
        setLoading(false);
      },
      (err: unknown) => {
        if (gen !== generation.current || ctrl.signal.aborted) return;
        setError(toApiError(err));
        setLoading(false);
      },
    );
    return () => ctrl.abort();
  }, [pageUrl, nonce]);

  const loadMore = useCallback(() => {
    const url = cursor ? pageUrl(cursor) : null;
    if (!url || loadingMore) return;
    const gen = generation.current;
    setLoadingMore(true);
    api<PageShape<T>>(url).then(
      (page) => {
        if (gen !== generation.current) return;
        setItems((prev) => [...prev, ...(page.items ?? [])]);
        setCursor(page.next_cursor ?? null);
        setLoadingMore(false);
      },
      (err: unknown) => {
        if (gen !== generation.current) return;
        setError(toApiError(err));
        setLoadingMore(false);
      },
    );
  }, [cursor, pageUrl, loadingMore]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { items, error, loading, loadingMore, hasMore: cursor !== null, loadMore, reload };
}
