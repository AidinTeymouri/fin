import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  MockEventSource.instances = [];
});

// jsdom doesn't implement scrollTo
Element.prototype.scrollTo = () => {};

// jsdom doesn't implement ResizeObserver (used by MainChart)
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", MockResizeObserver);

// lightweight-charts needs canvas; mock the v5 API used by MainChart
// (createChart(...).addSeries(LineSeries, opts)).
vi.mock("lightweight-charts", () => ({
  createChart: vi.fn(() => ({
    addSeries: vi.fn(() => ({ setData: vi.fn(), update: vi.fn() })),
    timeScale: vi.fn(() => ({ fitContent: vi.fn() })),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  })),
  LineSeries: "Line",
  ColorType: { Solid: "solid" },
  LineType: { Simple: 0 },
}));

// EventSource mock supporting both on* handlers and named-event listeners
// (useMarketData listens for "price" events). Tests can push events with
// MockEventSource.instances[n].emit("price", payload).
export class MockEventSource {
  static instances: MockEventSource[] = [];

  url: string;
  readyState = 0;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  private listeners = new Map<string, Set<EventListener>>();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
    setTimeout(() => {
      if (this.readyState === 2) return;
      this.readyState = 1;
      this.onopen?.(new Event("open"));
    }, 0);
  }

  addEventListener(type: string, listener: EventListener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(listener);
  }

  removeEventListener(type: string, listener: EventListener) {
    this.listeners.get(type)?.delete(listener);
  }

  close = vi.fn(() => {
    this.readyState = 2;
  });

  emit(type: string, data: unknown) {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    if (type === "message") this.onmessage?.(event);
    this.listeners.get(type)?.forEach((l) => l(event));
  }
}
vi.stubGlobal("EventSource", MockEventSource);
