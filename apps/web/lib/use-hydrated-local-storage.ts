"use client";

import {
  useCallback,
  useMemo,
  useSyncExternalStore,
  type Dispatch,
  type SetStateAction,
} from "react";

export interface StorageCodec<T> {
  parse: (raw: string) => T;
  serialize: (value: T) => string;
}

const subscribeToNothing = () => () => {};

/**
 * Persisted client state with a deterministic server/browser first render.
 *
 * Client Components are still prerendered by Next.js. Reading localStorage in
 * a useState initializer can therefore render one value on the server and a
 * different value during hydration. This hook always renders initialValue
 * first, then restores browser state after hydration.
 */
export function useHydratedLocalStorage<T>(
  key: string,
  initialValue: T,
  codec: StorageCodec<T>,
): [T, Dispatch<SetStateAction<T>>, boolean] {
  const changeEvent = `eduagents:storage:${key}`;
  const subscribe = useCallback((notify: () => void) => {
    const onStorage = (event: StorageEvent) => {
      if (event.storageArea === window.localStorage && event.key === key) {
        notify();
      }
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener(changeEvent, notify);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(changeEvent, notify);
    };
  }, [changeEvent, key]);

  const raw = useSyncExternalStore(
    subscribe,
    () => window.localStorage.getItem(key),
    () => null,
  );
  const hydrated = useSyncExternalStore(
    subscribeToNothing,
    () => true,
    () => false,
  );

  const value = useMemo(() => {
    if (raw === null) return initialValue;
    try {
      return codec.parse(raw);
    } catch {
      return initialValue;
    }
  }, [codec, initialValue, raw]);

  const setValue = useCallback<Dispatch<SetStateAction<T>>>(
    (nextOrUpdater) => {
      let previous = initialValue;
      const stored = window.localStorage.getItem(key);
      if (stored !== null) {
        try {
          previous = codec.parse(stored);
        } catch {
          // Treat malformed persisted state as the safe initial value.
        }
      }
      const next =
        typeof nextOrUpdater === "function"
          ? (nextOrUpdater as (previous: T) => T)(previous)
          : nextOrUpdater;
      window.localStorage.setItem(key, codec.serialize(next));
      window.dispatchEvent(new Event(changeEvent));
    },
    [changeEvent, codec, initialValue, key],
  );

  return [value, setValue, hydrated];
}

export const stringStorageCodec: StorageCodec<string> = {
  parse: (raw) => raw,
  serialize: (value) => value,
};

export const booleanStorageCodec: StorageCodec<boolean> = {
  parse: (raw) => raw === "1",
  serialize: (value) => (value ? "1" : "0"),
};
