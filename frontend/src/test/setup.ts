import "@testing-library/jest-dom/vitest";

/**
 * jsdom implements neither of these, and both are load-bearing in GraphCanvas:
 * the idle float drives itself with requestAnimationFrame, and the one-time
 * fit is triggered by a ResizeObserver. Without stubs, importing the component
 * throws before any assertion runs.
 */
if (!("ResizeObserver" in globalThis)) {
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
