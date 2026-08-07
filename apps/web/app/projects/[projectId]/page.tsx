import type { Metadata } from "next";

import { ProjectWorkbench } from "@/components/workbench/project-workbench";

type Props = {
  params: Promise<{ projectId: string }>;
  searchParams: Promise<{ run?: string }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { projectId } = await params;
  return { title: `Project ${projectId}` };
}

export default async function ProjectPage({ params, searchParams }: Props) {
  const [{ projectId }, { run }] = await Promise.all([params, searchParams]);
  return <ProjectWorkbench initialRunId={run} projectId={projectId} />;
}
