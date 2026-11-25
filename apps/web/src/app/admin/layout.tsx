import { ShellLayout } from "@/components/shell/ShellLayout";

export default function TenantAdminLayout({ children }: { children: React.ReactNode }) {
  return <ShellLayout surface="tenant-admin">{children}</ShellLayout>;
}
