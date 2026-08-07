"use client";

import { create } from "zustand";

export type WorkspaceTab = "preview" | "code" | "terminal" | "problems" | "versions";
export type DeviceViewport = "desktop" | "tablet" | "mobile";

type WorkbenchStore = {
  device: DeviceViewport;
  lastSeqByRun: Record<string, number>;
  selectedFile?: string;
  selectedTab: WorkspaceTab;
  setDevice: (device: DeviceViewport) => void;
  setLastSeq: (runId: string, seq: number) => void;
  setSelectedFile: (path?: string) => void;
  setSelectedTab: (tab: WorkspaceTab) => void;
};

export const useWorkbenchStore = create<WorkbenchStore>((set) => ({
  device: "desktop",
  lastSeqByRun: {},
  selectedFile: undefined,
  selectedTab: "preview",
  setDevice: (device) => set({ device }),
  setLastSeq: (runId, seq) =>
    set((state) => ({
      lastSeqByRun: seq > (state.lastSeqByRun[runId] || 0)
        ? { ...state.lastSeqByRun, [runId]: seq }
        : state.lastSeqByRun,
    })),
  setSelectedFile: (selectedFile) => set({ selectedFile }),
  setSelectedTab: (selectedTab) => set({ selectedTab }),
}));
