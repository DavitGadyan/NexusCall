import { ShellLayout } from "@/components/shell/ShellLayout";

export default function PlatformLayout({ children }: { children: React.ReactNode }) {
  return <ShellLayout surface="platform-admin">{children}</ShellLayout>;
}
