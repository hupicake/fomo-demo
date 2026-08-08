"use client";

import { useCallback, useMemo, useState, type ReactNode } from "react";

export type CrudRecord = { id: string };

export type CrudCollectionState<T extends CrudRecord> = {
  items: readonly T[];
  isLoading: boolean;
  error: Error | null;
};

export type CrudCollectionActions<T extends CrudRecord> = {
  create: (item: T) => void;
  update: (id: string, updateItem: (current: T) => T) => void;
  remove: (id: string) => void;
  replace: (items: readonly T[]) => void;
  setLoading: (isLoading: boolean) => void;
  setError: (error: Error | null) => void;
};

export function useCrudCollection<T extends CrudRecord>(initialItems: readonly T[] = []) {
  const [items, setItems] = useState<T[]>(() => [...initialItems]);
  const [isLoading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const create = useCallback((item: T) => {
    setItems((current) => [...current, item]);
  }, []);

  const update = useCallback((id: string, updateItem: (current: T) => T) => {
    setItems((current) => current.map((item) => (item.id === id ? updateItem(item) : item)));
  }, []);

  const remove = useCallback((id: string) => {
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const replace = useCallback((nextItems: readonly T[]) => {
    setItems([...nextItems]);
  }, []);

  return useMemo(
    () => ({
      state: { items, isLoading, error } satisfies CrudCollectionState<T>,
      actions: { create, update, remove, replace, setLoading, setError } satisfies CrudCollectionActions<T>,
    }),
    [create, error, isLoading, items, remove, replace, update],
  );
}

export type CrudSlotsProps<T extends CrudRecord> = {
  state: CrudCollectionState<T>;
  renderItem: (item: T) => ReactNode;
  loading?: ReactNode;
  empty?: ReactNode;
  error?: (error: Error) => ReactNode;
};

export function CrudSlots<T extends CrudRecord>({ state, renderItem, loading, empty, error }: CrudSlotsProps<T>) {
  if (state.isLoading) {
    return <>{loading ?? null}</>;
  }
  if (state.error) {
    return <>{error?.(state.error) ?? null}</>;
  }
  if (state.items.length === 0) {
    return <>{empty ?? null}</>;
  }
  return <>{state.items.map(renderItem)}</>;
}
