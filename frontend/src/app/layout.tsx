import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { ThemeToggle } from "../components/ui/ThemeToggle";
import { THEME_INIT_SCRIPT } from "../lib/theme";
import "./globals.css";
import styles from "./layout.module.css";

export const metadata: Metadata = {
  title: "Visual Attendance — Dashboard",
  description:
    "Operator dashboard for the AI-Powered Visual Attendance System (Phase 4).",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#0d1117" },
    { media: "(prefers-color-scheme: light)", color: "#f5f7fa" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/*
          Applied before first paint so there is no theme flash. `suppressHydrationWarning`
          is required on <html> because this script mutates its attributes before
          React hydrates; React state itself is still reconciled normally.
        */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>
        <header className={styles.header}>
          <span className={styles.brand}>
            <span className={styles.mark} aria-hidden="true" />
            <span className={styles.brandText}>Visual Attendance</span>
          </span>
          <ThemeToggle />
        </header>
        <main className={styles.main}>{children}</main>
      </body>
    </html>
  );
}
