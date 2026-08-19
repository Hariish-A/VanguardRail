/**
 * Ambient visual components.
 *
 * These are adapted from the Aceternity UI patterns (aurora, spotlight, meteors,
 * glowing border, text-generate) and rewritten here rather than pulled from a registry:
 * every one is a handful of divs plus a keyframe, and the console must build with no
 * network access and no CDN — the deployed bundle is fully self-contained.
 *
 * All of them are decorative and `aria-hidden`. None conveys information that is not
 * also written in text, and every animation is disabled under `prefers-reduced-motion`
 * by the global rule in `index.css`.
 */

import { motion, useInView, useMotionValue, useSpring } from "framer-motion";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/format";

/** Slow drifting colour field. The page's ground, behind everything. */
export function Aurora({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn(
        "pointer-events-none fixed inset-0 -z-50 overflow-hidden bg-ink-950",
        className,
      )}
    >
      <div
        className="absolute -inset-[35%] opacity-[0.30] blur-3xl animate-aurora"
        style={{
          background:
            "conic-gradient(from 180deg at 50% 50%, #1b2a5e 0deg, #0d3b52 90deg, #2a1d55 200deg, #123a44 290deg, #1b2a5e 360deg)",
        }}
      />
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top,transparent_5%,var(--color-ink-950)_72%)]" />
    </div>
  );
}

/** A faint engineering grid. Signals "instrument", not "marketing page". */
export function GridField({
  className,
  flow = false,
}: {
  className?: string;
  flow?: boolean;
}) {
  return (
    <div
      aria-hidden
      className={cn(
        "pointer-events-none absolute inset-0 -z-40 opacity-[0.5]",
        flow && "animate-grid-flow",
        className,
      )}
      style={{
        backgroundImage:
          "linear-gradient(to right, color-mix(in oklab, var(--color-ink-700) 55%, transparent) 1px, transparent 1px), linear-gradient(to bottom, color-mix(in oklab, var(--color-ink-700) 55%, transparent) 1px, transparent 1px)",
        backgroundSize: "56px 56px",
        maskImage:
          "radial-gradient(ellipse 80% 60% at 50% 0%, black 20%, transparent 85%)",
      }}
    />
  );
}

/** The angled beam of light behind a hero. */
export function Spotlight({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden
      className={cn(
        "pointer-events-none absolute -top-40 left-0 -z-30 h-[160%] w-[180%] opacity-45",
        className,
      )}
      viewBox="0 0 3787 2842"
      fill="none"
    >
      <g filter="url(#spotlight-blur)">
        <ellipse
          cx="1924.71"
          cy="273.501"
          rx="1924.71"
          ry="273.501"
          transform="matrix(-0.822377 -0.568943 -0.568943 0.822377 3631.88 2291.09)"
          fill="#5b8def"
          fillOpacity="0.20"
        />
      </g>
      <defs>
        <filter id="spotlight-blur" x="0" y="0" width="3787" height="2842">
          <feGaussianBlur stdDeviation="151" />
        </filter>
      </defs>
    </svg>
  );
}

/** Streaks across a dark panel. Used once, on the landing hero. */
export function Meteors({ count = 14 }: { count?: number }) {
  const seeds = Array.from({ length: count }, (_, index) => index);
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
      {seeds.map((seed) => (
        <span
          key={seed}
          className="animate-meteor absolute h-0.5 w-0.5 rotate-[215deg] rounded-full bg-brand-400 shadow-[0_0_0_1px_#ffffff10] before:absolute before:top-1/2 before:h-px before:w-[52px] before:-translate-y-1/2 before:bg-gradient-to-r before:from-brand-400 before:to-transparent before:content-['']"
          style={{
            top: `${Math.round((seed * 37) % 90)}%`,
            left: `${Math.round((seed * 53) % 100)}%`,
            animationDelay: `${(seed * 0.7) % 6}s`,
            animationDuration: `${5 + ((seed * 3) % 6)}s`,
          }}
        />
      ))}
    </div>
  );
}

/**
 * A card whose border lights up under the pointer.
 *
 * The glow follows the cursor via CSS custom properties rather than React state, so
 * pointer movement never triggers a re-render — this wraps tables that can hold a
 * couple of hundred audit rows.
 */
export function GlowCard({
  children,
  className,
  accent = "var(--color-brand-500)",
}: {
  children: ReactNode;
  className?: string;
  accent?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  return (
    <div
      ref={ref}
      onPointerMove={(event) => {
        const node = ref.current;
        if (!node) return;
        const rect = node.getBoundingClientRect();
        node.style.setProperty("--mx", `${event.clientX - rect.left}px`);
        node.style.setProperty("--my", `${event.clientY - rect.top}px`);
      }}
      className={cn(
        "group relative overflow-hidden rounded-2xl border border-ink-700/70 bg-ink-850/70 backdrop-blur-xl transition-colors duration-300 hover:border-ink-600",
        className,
      )}
      style={{ ["--accent" as string]: accent }}
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-300 group-hover:opacity-100"
        style={{
          background:
            "radial-gradient(320px circle at var(--mx, 50%) var(--my, 0%), color-mix(in oklab, var(--accent) 14%, transparent), transparent 65%)",
        }}
      />
      <div className="relative">{children}</div>
    </div>
  );
}

/** Words fading in one after another. Used exactly once, for the hero line. */
export function TextGenerate({
  text,
  className,
  delay = 0.16,
}: {
  text: string;
  className?: string;
  delay?: number;
}) {
  const words = text.split(" ");
  return (
    <span className={className}>
      {/* The animation splits the sentence into one element per word, which fragments
          it for a screen reader and for any text search over the page. So the real
          sentence is exposed once, and the animated copy is marked decorative. */}
      <span className="sr-only">{text}</span>
      <span aria-hidden="true">
        {words.map((word, index) => (
          <motion.span
            key={`${word}-${index}`}
            initial={{ opacity: 0, filter: "blur(8px)", y: 6 }}
            animate={{ opacity: 1, filter: "blur(0px)", y: 0 }}
            transition={{ duration: 0.5, delay: delay + index * 0.055 }}
            className="inline-block whitespace-pre"
          >
            {index < words.length - 1 ? `${word} ` : word}
          </motion.span>
        ))}
      </span>
    </span>
  );
}

/** A number that counts up when it scrolls into view. */
export function NumberTicker({
  value,
  className,
  decimals = 0,
  suffix = "",
}: {
  value: number;
  className?: string;
  decimals?: number;
  suffix?: string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: "-40px" });
  const motionValue = useMotionValue(0);
  const spring = useSpring(motionValue, { damping: 32, stiffness: 90 });
  const [display, setDisplay] = useState("0");

  useEffect(() => {
    if (inView) motionValue.set(value);
  }, [inView, value, motionValue]);

  useEffect(
    () =>
      spring.on("change", (latest: number) => {
        setDisplay(latest.toFixed(decimals));
      }),
    [spring, decimals],
  );

  return (
    <span ref={ref} className={className}>
      {display}
      {suffix}
    </span>
  );
}

/** A moving highlight along a border. Marks something that is live. */
export function ShimmerBorder({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "animate-shimmer pointer-events-none absolute inset-x-0 top-0 h-px",
        className,
      )}
      style={{
        background:
          "linear-gradient(90deg, transparent, var(--color-brand-400), transparent)",
        backgroundSize: "200% 100%",
      }}
    />
  );
}

/** A soft radial wash, tinted per outcome. Behind a decision verdict. */
export function VerdictGlow({ colour }: { colour: string }) {
  return (
    <motion.div
      aria-hidden
      initial={{ opacity: 0, scale: 0.85 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.55, ease: "easeOut" }}
      className="pointer-events-none absolute inset-0"
      style={{
        background: `radial-gradient(60% 90% at 12% 50%, color-mix(in oklab, ${colour} 22%, transparent), transparent 70%)`,
      }}
    />
  );
}
