/**
 * Base primitives, in the shadcn/ui shape: unstyled behaviour plus a variant table,
 * owned in-repo rather than installed. Same reasoning as `effects.tsx` — the console
 * ships as one self-contained bundle, and these are small enough to read in full.
 *
 * Two rules hold throughout:
 *
 * * Every interactive element is a real `<button>`, `<input>`, or `<label>`, so keyboard
 *   and screen-reader behaviour comes for free rather than being reimplemented badly.
 * * Anything that can fail renders its failure. This console is used to answer "why was
 *   my agent stopped", and a component that hides an error is worse than no component.
 */

import { motion } from "framer-motion";
import {
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/format";

// ---------------------------------------------------------------------------
// Button
// ---------------------------------------------------------------------------

type Variant = "primary" | "ghost" | "outline" | "danger" | "approve" | "subtle";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-brand-500 text-white hover:bg-brand-400 shadow-[0_6px_24px_-10px_var(--color-brand-500)]",
  ghost: "text-ink-300 hover:text-ink-100 hover:bg-ink-800/70",
  outline: "border border-ink-600 text-ink-200 hover:bg-ink-800/70 hover:border-ink-500",
  danger:
    "border border-[color-mix(in_oklab,var(--color-block)_50%,transparent)] bg-[color-mix(in_oklab,var(--color-block)_14%,transparent)] text-block hover:bg-[color-mix(in_oklab,var(--color-block)_24%,transparent)]",
  approve:
    "border border-[color-mix(in_oklab,var(--color-allow)_50%,transparent)] bg-[color-mix(in_oklab,var(--color-allow)_14%,transparent)] text-allow hover:bg-[color-mix(in_oklab,var(--color-allow)_24%,transparent)]",
  subtle: "bg-ink-800 text-ink-200 hover:bg-ink-750",
};

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  className,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: "sm" | "md";
  loading?: boolean;
}) {
  return (
    <button
      {...props}
      disabled={props.disabled || loading}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-lg font-medium transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-45",
        size === "sm" ? "px-3 py-1.5 text-[13px]" : "px-4 py-2.5 text-sm",
        VARIANTS[variant],
        className,
      )}
    >
      {loading && <Spinner className="h-3.5 w-3.5" />}
      {children}
    </button>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="Working"
      className={cn(
        "inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent",
        className,
      )}
    />
  );
}

// ---------------------------------------------------------------------------
// Surfaces
// ---------------------------------------------------------------------------

export function Card({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-2xl border border-ink-700/70 bg-ink-850/60 backdrop-blur-xl",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function SectionTitle({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 className="text-[15px] font-semibold tracking-tight text-ink-100">{title}</h2>
        {hint && <p className="mt-1 max-w-2xl text-[13px] leading-relaxed text-ink-400">{hint}</p>}
      </div>
      {action}
    </div>
  );
}

export function Badge({
  children,
  className,
  tone = "neutral",
}: {
  children: ReactNode;
  className?: string;
  tone?: "neutral" | "brand" | "warn" | "good" | "bad";
}) {
  const tones = {
    neutral: "border-ink-600 text-ink-300 bg-ink-800/60",
    brand: "border-brand-600/60 text-brand-400 bg-brand-500/10",
    warn: "border-[color-mix(in_oklab,var(--color-hitl)_45%,transparent)] text-hitl bg-[color-mix(in_oklab,var(--color-hitl)_12%,transparent)]",
    good: "border-[color-mix(in_oklab,var(--color-allow)_45%,transparent)] text-allow bg-[color-mix(in_oklab,var(--color-allow)_12%,transparent)]",
    bad: "border-[color-mix(in_oklab,var(--color-block)_45%,transparent)] text-block bg-[color-mix(in_oklab,var(--color-block)_12%,transparent)]",
  } as const;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[11px] tracking-wide",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

const FIELD_BASE =
  "w-full rounded-lg border border-ink-700 bg-ink-900/80 px-3 py-2.5 text-sm text-ink-100 placeholder:text-ink-500 transition-colors focus:border-brand-500 focus:outline-none";

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-[12px] font-medium uppercase tracking-wider text-ink-400">
        {label}
      </span>
      {children}
      {hint && <span className="mt-1.5 block text-[12px] leading-relaxed text-ink-500">{hint}</span>}
    </label>
  );
}

export function Input({
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cn(FIELD_BASE, className)} />;
}

export function Textarea({
  className,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      spellCheck={false}
      {...props}
      className={cn(FIELD_BASE, "font-mono text-[13px] leading-relaxed", className)}
    />
  );
}

export function Select({
  className,
  children,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select {...props} className={cn(FIELD_BASE, "cursor-pointer", className)}>
      {children}
    </select>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  hint?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="flex w-full items-start gap-3 rounded-lg border border-ink-700 bg-ink-900/60 p-3 text-left transition-colors hover:border-ink-600"
    >
      <span
        className={cn(
          "mt-0.5 flex h-5 w-9 shrink-0 items-center rounded-full p-0.5 transition-colors",
          checked ? "bg-brand-500" : "bg-ink-700",
        )}
      >
        <motion.span
          layout
          transition={{ type: "spring", stiffness: 500, damping: 34 }}
          className={cn("h-4 w-4 rounded-full bg-white", checked && "ml-auto")}
        />
      </span>
      <span className="min-w-0">
        <span className="block text-[13px] font-medium text-ink-100">{label}</span>
        {hint && <span className="mt-0.5 block text-[12px] leading-snug text-ink-500">{hint}</span>}
      </span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Data display
// ---------------------------------------------------------------------------

export function CodeBlock({
  code,
  className,
  maxHeight = "22rem",
}: {
  code: string;
  className?: string;
  maxHeight?: string;
}) {
  const [copied, setCopied] = useState(false);

  return (
    <div className={cn("group relative", className)}>
      <button
        type="button"
        onClick={() => {
          void navigator.clipboard?.writeText(code);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1400);
        }}
        className="absolute right-2 top-2 z-10 rounded-md border border-ink-700 bg-ink-900/90 px-2 py-1 font-mono text-[11px] text-ink-400 opacity-0 transition-opacity hover:text-ink-100 focus-visible:opacity-100 group-hover:opacity-100"
      >
        {copied ? "copied" : "copy"}
      </button>
      <pre
        className="overflow-auto rounded-xl border border-ink-700/70 bg-ink-950/80 p-3.5 font-mono text-[12.5px] leading-relaxed text-ink-200"
        style={{ maxHeight }}
      >
        {code}
      </pre>
    </div>
  );
}

export function KeyValue({
  label,
  value,
  mono = false,
  title,
}: {
  label: string;
  value: ReactNode;
  mono?: boolean;
  title?: string;
}) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] uppercase tracking-wider text-ink-500">{label}</div>
      <div
        title={title}
        className={cn(
          "mt-0.5 truncate text-[13px] text-ink-100",
          mono && "font-mono text-[12.5px]",
        )}
      >
        {value}
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  detail,
  icon = "◦",
  action,
}: {
  title: string;
  detail: string;
  icon?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-ink-700 bg-ink-900/40 px-6 py-14 text-center">
      <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-full border border-ink-700 bg-ink-850 font-mono text-lg text-ink-500">
        {icon}
      </div>
      <p className="text-sm font-medium text-ink-200">{title}</p>
      <p className="mt-1.5 max-w-md text-[13px] leading-relaxed text-ink-500">{detail}</p>
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "animate-pulse rounded-lg bg-gradient-to-r from-ink-800 via-ink-750 to-ink-800",
        className,
      )}
    />
  );
}

/**
 * Errors, rendered rather than logged.
 *
 * Shows the status, what the server said, what that status *means* for the operator, and
 * the `x-request-id` — which is the value to quote when asking why a particular decision
 * came out the way it did. A generic "failed to load" would strip all four.
 */
export function ErrorNote({
  error,
  className,
}: {
  error: unknown;
  className?: string;
}) {
  if (!error) return null;

  const api = error instanceof ApiError ? error : null;
  const message = error instanceof Error ? error.message : String(error);

  return (
    <div
      role="alert"
      className={cn(
        "rounded-xl border border-[color-mix(in_oklab,var(--color-block)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-block)_9%,transparent)] p-4",
        className,
      )}
    >
      <div className="flex items-center gap-2">
        <span className="h-1.5 w-1.5 rounded-full bg-block" />
        <span className="font-mono text-[12px] font-semibold tracking-wide text-block">
          {api ? (api.status === 0 ? "UNREACHABLE" : `HTTP ${api.status}`) : "ERROR"}
        </span>
        {api?.requestId && (
          <span className="ml-auto font-mono text-[11px] text-ink-500">
            request {api.requestId.slice(0, 8)}
          </span>
        )}
      </div>
      <p className="mt-2 text-[13px] leading-relaxed text-ink-200">{message}</p>
      {api?.guidance && (
        <p className="mt-2 text-[12.5px] leading-relaxed text-ink-400">{api.guidance}</p>
      )}
    </div>
  );
}

/** A horizontal set of tabs, driven by the parent. */
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: ReadonlyArray<{ id: T; label: string; count?: number }>;
  value: T;
  onChange: (next: T) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1 rounded-xl border border-ink-700/70 bg-ink-900/60 p-1">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          onClick={() => onChange(tab.id)}
          className={cn(
            "relative rounded-lg px-3 py-1.5 text-[13px] transition-colors",
            value === tab.id ? "text-ink-100" : "text-ink-400 hover:text-ink-200",
          )}
        >
          {value === tab.id && (
            <motion.span
              layoutId="tab-pill"
              className="absolute inset-0 rounded-lg bg-ink-750"
              transition={{ type: "spring", stiffness: 420, damping: 34 }}
            />
          )}
          <span className="relative">
            {tab.label}
            {tab.count !== undefined && (
              <span className="ml-1.5 font-mono text-[11px] text-ink-500">{tab.count}</span>
            )}
          </span>
        </button>
      ))}
    </div>
  );
}
