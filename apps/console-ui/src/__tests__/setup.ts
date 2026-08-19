import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
});

// jsdom implements neither, and framer-motion plus the NumberTicker both reach for them.
// Stubbing here rather than in each test keeps the tests about behaviour.
if (!("IntersectionObserver" in globalThis)) {
  class StubIntersectionObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
    takeRecords(): [] {
      return [];
    }
    readonly root = null;
    readonly rootMargin = "";
    readonly thresholds: readonly number[] = [];
  }
  Object.defineProperty(globalThis, "IntersectionObserver", {
    writable: true,
    value: StubIntersectionObserver,
  });
}

if (!globalThis.matchMedia) {
  Object.defineProperty(globalThis, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}
