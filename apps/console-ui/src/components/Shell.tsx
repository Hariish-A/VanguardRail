/**
 * The frame: brand, navigation, connection state.
 *
 * The header always states two things, because a governance console that is quietly
 * disconnected is dangerous: which endpoint it is talking to, and which key it is using.
 * Neither is ever implied.
 */

import { AnimatePresence, motion } from "framer-motion";
import type { ReactNode } from "react";
import { useSession } from "@/lib/store";
import { cn } from "@/lib/format";
import { Badge } from "./ui";

export interface NavItem {
  path: string;
  label: string;
  glyph: string;
  blurb: string;
}

export const NAV: NavItem[] = [
  { path: "/", label: "Overview", glyph: "◆", blurb: "What this is, and what it proves" },
  {
    path: "/agent",
    label: "Agent Console",
    glyph: "❯",
    blurb: "Run the AWS-hosted agent and watch it be governed",
  },
  {
    path: "/theatre",
    label: "Decision Theatre",
    glyph: "⌖",
    blurb: "Send any tool call and see the verdict",
  },
  {
    path: "/review",
    label: "Review Queue",
    glyph: "⏸",
    blurb: "Approve or deny actions held for a human",
  },
  {
    path: "/audit",
    label: "Audit & Chain",
    glyph: "⛓",
    blurb: "Every decision, and proof none were edited",
  },
  {
    path: "/health",
    label: "System Health",
    glyph: "♥",
    blurb: "Liveness, readiness, capacity and cost",
  },
];

export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <a
      href="#/"
      className="flex items-center gap-2.5 rounded-lg transition-opacity hover:opacity-90"
    >
      <span className="relative flex h-8 w-8 items-center justify-center">
        <svg viewBox="0 0 32 32" className="h-8 w-8">
          <path
            d="M16 3l11 4v9c0 7-4.7 11.6-11 13-6.3-1.4-11-6-11-13V7z"
            fill="url(#shield)"
          />
          <path
            d="M11 16l3.5 3.5L21 13"
            stroke="#06070c"
            strokeWidth="2.6"
            fill="none"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <defs>
            <linearGradient id="shield" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="#7ba7ff" />
              <stop offset="100%" stopColor="#3f6fd8" />
            </linearGradient>
          </defs>
        </svg>
      </span>
      {!compact && (
        <span className="leading-tight">
          <span className="block text-[15px] font-semibold tracking-tight text-ink-100">
            Guardrail
          </span>
          <span className="block text-[10.5px] uppercase tracking-[0.16em] text-ink-500">
            action layer
          </span>
        </span>
      )}
    </a>
  );
}

function ConnectionPill() {
  const { status, identity, session } = useSession();

  if (status !== "connected" || !identity) {
    return (
      <Badge tone={status === "connecting" ? "brand" : "warn"}>
        <span
          className={cn(
            "h-1.5 w-1.5 rounded-full",
            status === "connecting" ? "bg-brand-400 animate-pulse" : "bg-hitl",
          )}
        />
        {status === "connecting" ? "connecting" : "not connected"}
      </Badge>
    );
  }

  const host = (() => {
    try {
      return new URL(session.baseUrl).host;
    } catch {
      return session.baseUrl;
    }
  })();

  return (
    <a
      href="#/connect"
      className="flex items-center gap-2 rounded-full border border-ink-700 bg-ink-850/80 py-1 pl-1 pr-3 transition-colors hover:border-ink-600"
      title={`Connected to ${session.baseUrl} as ${identity.key_id}`}
    >
      <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[color-mix(in_oklab,var(--color-allow)_18%,transparent)]">
        <span className="h-1.5 w-1.5 rounded-full bg-allow" />
      </span>
      <span className="hidden font-mono text-[11.5px] leading-tight text-ink-300 sm:block">
        <span className="block text-ink-200">{identity.key_id}</span>
        <span className="block text-[10px] text-ink-500">
          {identity.role} · {host.slice(0, 22)}
        </span>
      </span>
      <span className="font-mono text-[11px] text-ink-500 sm:hidden">{identity.role}</span>
    </a>
  );
}

export function Shell({
  route,
  children,
}: {
  route: string;
  children: ReactNode;
}) {
  return (
    <div className="relative flex min-h-screen flex-col">
      <header className="sticky top-0 z-40 border-b border-ink-800/80 bg-ink-950/75 backdrop-blur-xl">
        <div className="mx-auto flex max-w-[1400px] items-center gap-4 px-5 py-3">
          <Logo />
          <nav className="ml-4 hidden flex-1 items-center gap-0.5 lg:flex">
            {NAV.map((item) => {
              const active =
                item.path === "/" ? route === "/" : route.startsWith(item.path);
              return (
                <a
                  key={item.path}
                  href={`#${item.path}`}
                  title={item.blurb}
                  className={cn(
                    "relative rounded-lg px-3 py-2 text-[13.5px] transition-colors",
                    active ? "text-ink-100" : "text-ink-400 hover:text-ink-200",
                  )}
                >
                  {active && (
                    <motion.span
                      layoutId="nav-pill"
                      className="absolute inset-0 rounded-lg border border-ink-700 bg-ink-850"
                      transition={{ type: "spring", stiffness: 420, damping: 36 }}
                    />
                  )}
                  <span className="relative flex items-center gap-1.5">
                    <span className="font-mono text-[11px] text-ink-500">{item.glyph}</span>
                    {item.label}
                  </span>
                </a>
              );
            })}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <ConnectionPill />
          </div>
        </div>

        {/* Below `lg` the nav becomes a horizontally scrolling strip rather than a
            hamburger: with six destinations, hiding them behind a tap costs more than
            the scroll does. */}
        <nav className="flex gap-1 overflow-x-auto border-t border-ink-800/70 px-4 py-1.5 lg:hidden">
          {NAV.map((item) => {
            const active = item.path === "/" ? route === "/" : route.startsWith(item.path);
            return (
              <a
                key={item.path}
                href={`#${item.path}`}
                className={cn(
                  "shrink-0 rounded-lg px-3 py-1.5 text-[13px] transition-colors",
                  active
                    ? "border border-ink-700 bg-ink-850 text-ink-100"
                    : "text-ink-400",
                )}
              >
                {item.label}
              </a>
            );
          })}
        </nav>
      </header>

      <main className="relative mx-auto w-full max-w-[1400px] flex-1 px-5 pb-24 pt-8">
        <AnimatePresence mode="wait">
          <motion.div
            key={route}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.22, ease: "easeOut" }}
          >
            {children}
          </motion.div>
        </AnimatePresence>
      </main>

      <footer className="border-t border-ink-800/70 px-5 py-6">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-x-6 gap-y-2 text-[12px] text-ink-500">
          <span>Guardrail — action-layer governance for AI agents</span>
          <span className="font-mono">PS-3.1</span>
          <span className="ml-auto font-mono">
            $0.00 AWS spend · always-free tier only
          </span>
        </div>
      </footer>
    </div>
  );
}
