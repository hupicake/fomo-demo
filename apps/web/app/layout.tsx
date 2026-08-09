import type { Metadata, Viewport } from "next";

import "./globals.css";
import { AuthBootstrap } from "@/components/auth/auth-bootstrap";

export const metadata: Metadata = {
  title: {
    default: "FOMO | Coding-agent workbench",
    template: "%s | FOMO",
  },
  description: "A coding-agent workbench: describe a page, follow the work log, and open a live preview.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#f8f9fc",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <AuthBootstrap>{children}</AuthBootstrap>
      </body>
    </html>
  );
}
