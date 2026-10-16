import { renderHook, act, waitFor } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { useMarketData } from "@/hooks/useMarketData";
import { MockEventSource } from "./setup";

function update(ticker: string, price: number, previous: number) {
  return {
    ticker,
    price,
    previous_price: previous,
    open_price: previous,
    timestamp: "2026-01-01T00:00:00Z",
    direction: price > previous ? "up" : price < previous ? "down" : "flat",
  };
}

describe("useMarketData", () => {
  it("connects to the price stream and reports connected", async () => {
    const { result } = renderHook(() => useMarketData());

    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toBe("/api/stream/prices");

    await waitFor(() => {
      expect(result.current.connectionStatus).toBe("connected");
    });
  });

  it("applies named price events and accumulates history", async () => {
    const { result } = renderHook(() => useMarketData());
    const es = MockEventSource.instances[0];

    act(() => {
      es.emit("price", [update("AAPL", 101, 100), update("TSLA", 199, 200)]);
    });
    act(() => {
      es.emit("price", update("AAPL", 102, 101));
    });

    expect(result.current.prices.AAPL.price).toBe(102);
    expect(result.current.prices.TSLA.direction).toBe("down");
    expect(result.current.priceHistory.AAPL.map((p) => p.price)).toEqual([101, 102]);
  });

  it("reports reconnecting on stream error", async () => {
    const { result } = renderHook(() => useMarketData());
    const es = MockEventSource.instances[0];

    act(() => {
      es.onerror?.(new Event("error"));
    });

    expect(result.current.connectionStatus).toBe("reconnecting");
  });

  it("closes the stream on unmount", () => {
    const { unmount } = renderHook(() => useMarketData());
    const es = MockEventSource.instances[0];
    unmount();
    expect(es.close).toHaveBeenCalled();
  });
});
