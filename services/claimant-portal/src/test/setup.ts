import '@testing-library/jest-dom/vitest';
import { webcrypto } from 'node:crypto';

// jsdom does not implement ResizeObserver, which Recharts' ResponsiveContainer
// relies on to measure its parent. Provide a no-op polyfill so chart-bearing
// components (e.g. the investigator SHAP chart) can mount under test.
if (typeof globalThis.ResizeObserver === 'undefined') {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  Object.defineProperty(globalThis, 'ResizeObserver', {
    value: ResizeObserverStub,
    configurable: true,
    writable: true,
  });
}

// jsdom does not implement the Web Crypto API. Browsers provide a global
// `crypto` with `subtle`; expose Node's WebCrypto so PKCE helpers can run
// under test exactly as they would in a real browser.
if (!globalThis.crypto || !globalThis.crypto.subtle) {
  Object.defineProperty(globalThis, 'crypto', {
    value: webcrypto,
    configurable: true,
    writable: true,
  });
}
