"use client";

import dynamic from "next/dynamic";

const MonacoEditor = dynamic(() => import("@monaco-editor/react"), {
  ssr: false,
  loading: () => <div className="grid h-full min-h-80 place-items-center bg-[#0d1117] font-mono text-xs text-slate-400">Loading editor…</div>,
});

export function LazyMonacoEditor({
  language,
  onChange,
  value,
}: {
  language: string;
  onChange: (value: string) => void;
  value: string;
}) {
  return (
    <MonacoEditor
      height="100%"
      language={language}
      onChange={(next) => onChange(next || "")}
      options={{
        automaticLayout: true,
        fontFamily: "var(--font-geist-mono), ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 13,
        minimap: { enabled: false },
        padding: { top: 14 },
        scrollBeyondLastLine: false,
        wordWrap: "on",
      }}
      theme="vs-dark"
      value={value}
    />
  );
}
