import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { Effect } from "./types";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * Presentation for the four outcomes.
 *
 * Colour is never the only signal — `label` is always rendered alongside it, and the
 * badge shape differs per outcome. A reviewer looking at a projector, or with a red/green
 * deficiency, must still be able to tell a block from an allow.
 */
export const EFFECT_STYLE: Record<
  Effect,
  { label: string; text: string; bg: string; border: string; ring: string; dot: string }
> = {
  block: {
    label: "BLOCK",
    text: "text-block",
    bg: "bg-[color-mix(in_oklab,var(--color-block)_16%,transparent)]",
    border: "border-[color-mix(in_oklab,var(--color-block)_45%,transparent)]",
    ring: "shadow-[0_0_28px_-6px_color-mix(in_oklab,var(--color-block)_60%,transparent)]",
    dot: "bg-block",
  },
  require_hitl: {
    label: "REVIEW",
    text: "text-hitl",
    bg: "bg-[color-mix(in_oklab,var(--color-hitl)_16%,transparent)]",
    border: "border-[color-mix(in_oklab,var(--color-hitl)_45%,transparent)]",
    ring: "shadow-[0_0_28px_-6px_color-mix(in_oklab,var(--color-hitl)_60%,transparent)]",
    dot: "bg-hitl",
  },
  log_and_allow: {
    label: "LOG + ALLOW",
    text: "text-log",
    bg: "bg-[color-mix(in_oklab,var(--color-log)_16%,transparent)]",
    border: "border-[color-mix(in_oklab,var(--color-log)_45%,transparent)]",
    ring: "shadow-[0_0_28px_-6px_color-mix(in_oklab,var(--color-log)_60%,transparent)]",
    dot: "bg-log",
  },
  allow: {
    label: "ALLOW",
    text: "text-allow",
    bg: "bg-[color-mix(in_oklab,var(--color-allow)_16%,transparent)]",
    border: "border-[color-mix(in_oklab,var(--color-allow)_45%,transparent)]",
    ring: "shadow-[0_0_28px_-6px_color-mix(in_oklab,var(--color-allow)_60%,transparent)]",
    dot: "bg-allow",
  },
};

export function effectStyle(effect: string) {
  return EFFECT_STYLE[effect as Effect] ?? EFFECT_STYLE.allow;
}

/** What each outcome actually means, in one sentence, for a reader who has not read the docs. */
export const EFFECT_MEANING: Record<Effect, string> = {
  block: "The tool never ran. The agent is told why and can explain the refusal.",
  require_hitl:
    "Paused before execution and queued for a human. On timeout the default is deny.",
  log_and_allow:
    "Permitted, and recorded with full argument detail because it touches something sensitive.",
  allow: "Permitted. Still written to the audit chain — every decision is recorded.",
};

export function shortHash(hash: string, chars = 10): string {
  if (!hash) return "—";
  if (hash.length <= chars * 2) return hash;
  return `${hash.slice(0, chars)}…${hash.slice(-4)}`;
}

export function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function formatClock(seconds: number): string {
  if (seconds <= 0) return "expired";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function duration(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(0)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}
