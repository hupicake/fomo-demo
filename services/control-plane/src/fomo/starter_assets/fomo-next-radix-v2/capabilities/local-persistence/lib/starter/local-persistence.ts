export type VersionedStorageEnvelope<T> = {
  version: number;
  data: T;
};

export type StorageMigration<T> = (stored: unknown) => VersionedStorageEnvelope<T> | null;

export type LocalStorageAdapter<T> = {
  load: () => VersionedStorageEnvelope<T> | null;
  save: (data: T) => boolean;
  clear: () => void;
};

export type LocalStorageAdapterOptions<T> = {
  key: string;
  version: number;
  validate: (value: unknown) => value is T;
  migrate?: StorageMigration<T>;
  storage?: Storage | null;
};

function browserLocalStorage(): Storage | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function isEnvelope(value: unknown): value is VersionedStorageEnvelope<unknown> {
  return typeof value === "object" && value !== null && "version" in value && "data" in value;
}

export function createLocalStorageAdapter<T>({
  key,
  version,
  validate,
  migrate,
  storage = browserLocalStorage(),
}: LocalStorageAdapterOptions<T>): LocalStorageAdapter<T> {
  if (!key.trim()) {
    throw new TypeError("Storage key must be nonempty.");
  }
  if (!Number.isSafeInteger(version) || version < 1) {
    throw new TypeError("Storage version must be a positive safe integer.");
  }

  const accept = (candidate: unknown): VersionedStorageEnvelope<T> | null => {
    if (!isEnvelope(candidate) || candidate.version !== version || !validate(candidate.data)) {
      return null;
    }
    return { version: candidate.version, data: candidate.data };
  };

  return {
    load() {
      if (!storage) {
        return null;
      }
      try {
        const raw = storage.getItem(key);
        if (raw === null) {
          return null;
        }
        const parsed: unknown = JSON.parse(raw);
        return accept(parsed) ?? accept(migrate?.(parsed));
      } catch {
        return null;
      }
    },
    save(data) {
      if (!storage || !validate(data)) {
        return false;
      }
      try {
        storage.setItem(key, JSON.stringify({ version, data } satisfies VersionedStorageEnvelope<T>));
        return true;
      } catch {
        return false;
      }
    },
    clear() {
      try {
        storage?.removeItem(key);
      } catch {
        // Storage is best-effort; callers can keep the in-memory source of truth.
      }
    },
  };
}
