import { ShellLayout } from "@/components/shell/ShellLayout";

export default function WorkspaceLayout({ children }: { children: React.ReactNode }) {
  return <ShellLayout surface="workspace">{children}</ShellLayout>;
}
