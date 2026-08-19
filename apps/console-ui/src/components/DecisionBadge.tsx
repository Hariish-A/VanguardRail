/**
 * The verdict, rendered.
 *
 * The single most important pixel in this console. It is read under time pressure, so:
 *
 * * the word is always present — colour is a second channel, never the only one;
 * * `require_hitl` pulses, because it is the one outcome that is *not yet settled* and
 *   demands somebody do something;
 * * `block` is filled rather than outlined, so it is distinguishable from `allow` in
 *   greyscale and to a red/green colour-blind reader.
 */

import { motion } from "framer-motion";
import { cn } from "@/lib/format";
import { effectStyle } from "@/lib/format";

export function DecisionBadge({
  effect,
  size = "md",
  animate = false,
}: {
  effect: string;
  size?: "sm" | "md" | "lg";
  animate?: boolean;
}) {
  const style = effectStyle(effect);
  const isBlock = effect === "block";
  const isHitl = effect === "require_hitl";

  const sizing = {
    sm: "px-2 py-0.5 text-[10.5px] gap-1.5",
    md: "px-3 py-1 text-[12px] gap-2",
    lg: "px-4 py-1.5 text-[14px] gap-2.5",
  }[size];

  const content = (
    <span
      className={cn(
        "inline-flex items-center rounded-full border font-mono font-semibold tracking-[0.08em]",
        sizing,
        style.text,
        style.border,
        isBlock ? style.bg : "bg-ink-900/60",
        isHitl && "animate-pulse-ring",
      )}
    >
      <span
        className={cn(
          "rounded-full",
          size === "sm" ? "h-1.5 w-1.5" : "h-2 w-2",
          style.dot,
        )}
      />
      {style.label}
    </span>
  );

  if (!animate) return content;

  return (
    <motion.span
      initial={{ opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ type: "spring", stiffness: 380, damping: 24 }}
      className="inline-block"
    >
      {content}
    </motion.span>
  );
}

/** Status of a held decision, once a human (or the timeout) has settled it. */
export function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; className: string }> = {
    pending: {
      label: "PENDING",
      className:
        "text-hitl border-[color-mix(in_oklab,var(--color-hitl)_45%,transparent)]",
    },
    approved: {
      label: "APPROVED",
      className:
        "text-allow border-[color-mix(in_oklab,var(--color-allow)_45%,transparent)]",
    },
    denied: {
      label: "DENIED",
      className:
        "text-block border-[color-mix(in_oklab,var(--color-block)_45%,transparent)]",
    },
    expired: { label: "EXPIRED — AUTO-DENIED", className: "text-ink-400 border-ink-600" },
  };
  const style = map[status] ?? { label: status.toUpperCase(), className: "text-ink-400 border-ink-600" };

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border bg-ink-900/60 px-2.5 py-0.5 font-mono text-[11px] font-semibold tracking-[0.08em]",
        style.className,
      )}
    >
      {style.label}
    </span>
  );
}
