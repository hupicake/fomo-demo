import type { Metadata, Viewport } from "next";

import "./globals.css";
import { AuthBootstrap } from "@/components/auth/auth-bootstrap";
import { TooltipProvider } from "@/components/ui/tooltip";

const themeInitScript = `(function(){try{var s=localStorage.getItem('fomo:theme:v1');var v=s?JSON.parse(s):null;var t=v&&v.version===1&&v.data?v.data.theme:null;var m=window.matchMedia('(prefers-color-scheme: dark)').matches;var d=t==='dark'||((t!=='light')&&m);var r=document.documentElement;r.classList.toggle('dark',d);r.style.colorScheme=d?'dark':'light';}catch(e){}})();`;

export const metadata: Metadata = {
  title: {
    default: "FOMO | 编程工作台",
    template: "%s | FOMO",
  },
  description: "一个编程工作台：描述一个页面，跟着工作日志，打开真实预览。",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f8f9fc" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0a" },
  ],
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body>
        <AuthBootstrap>
          <TooltipProvider>{children}</TooltipProvider>
        </AuthBootstrap>
      </body>
    </html>
  );
}
