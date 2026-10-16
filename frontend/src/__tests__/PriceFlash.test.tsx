import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import Watchlist from "@/components/Watchlist";
import type { PriceUpdate } from "@/hooks/useMarketData";

vi.mock("recharts", () => ({
  LineChart: ({ children }: { children: React.ReactNode }) => <svg>{children}</svg>,
  Line: () => null,
  YAxis: () => null,
}));

vi.mock("@/lib/api", () => ({
  api: {
    getWatchlist: vi.fn(),
    addTicker: vi.fn(),
    removeTicker: vi.fn(),
  },
  apiFetch: vi.fn(),
}));

import { api } from "@/lib/api";

function tick(price: number, previous: number, direction: PriceUpdate["direction"]) {
  return {
    AAPL: {
      ticker: "AAPL",
      price,
      previous_price: previous,
      open_price: previous,
      timestamp: "2026-01-01T00:00:00Z",
      direction,
    },
  };
}

function renderWatchlist(prices: Record<string, PriceUpdate>) {
  return render(
    <Watchlist
      prices={prices}
      priceHistory={{}}
      selectedTicker={null}
      onSelectTicker={vi.fn()}
    />
  );
}

async function getRow() {
  const cell = await screen.findByText("AAPL");
  return cell.closest("tr")!;
}

describe("Watchlist price flash", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getWatchlist).mockResolvedValue([{ ticker: "AAPL" }]);
  });

  it("applies flash-up on uptick and fades it out", async () => {
    const { rerender } = renderWatchlist({});
    const row = await getRow();
    expect(row).not.toHaveClass("flash-up");

    rerender(
      <Watchlist
        prices={tick(151, 150, "up")}
        priceHistory={{}}
        selectedTicker={null}
        onSelectTicker={vi.fn()}
      />
    );

    expect(row).toHaveClass("flash-up");
    expect(row).not.toHaveClass("flash-down");

    await waitFor(() => {
      expect(row).not.toHaveClass("flash-up");
      expect(row).toHaveClass("flash-fade");
    });
  });

  it("applies flash-down on downtick and fades it out", async () => {
    const { rerender } = renderWatchlist({});
    const row = await getRow();

    rerender(
      <Watchlist
        prices={tick(149, 150, "down")}
        priceHistory={{}}
        selectedTicker={null}
        onSelectTicker={vi.fn()}
      />
    );

    expect(row).toHaveClass("flash-down");
    expect(row).not.toHaveClass("flash-up");

    await waitFor(() => {
      expect(row).not.toHaveClass("flash-down");
      expect(row).toHaveClass("flash-fade");
    });
  });

  it("switches flash color when direction reverses", async () => {
    const { rerender } = renderWatchlist({});
    const row = await getRow();

    rerender(
      <Watchlist
        prices={tick(151, 150, "up")}
        priceHistory={{}}
        selectedTicker={null}
        onSelectTicker={vi.fn()}
      />
    );
    expect(row).toHaveClass("flash-up");

    rerender(
      <Watchlist
        prices={tick(150.5, 151, "down")}
        priceHistory={{}}
        selectedTicker={null}
        onSelectTicker={vi.fn()}
      />
    );
    expect(row).toHaveClass("flash-down");
    expect(row).not.toHaveClass("flash-up");
    expect(row).not.toHaveClass("flash-fade");
  });

  it("does not flash on flat ticks", async () => {
    const { rerender } = renderWatchlist({});
    const row = await getRow();

    rerender(
      <Watchlist
        prices={tick(150, 150, "flat")}
        priceHistory={{}}
        selectedTicker={null}
        onSelectTicker={vi.fn()}
      />
    );

    expect(row).not.toHaveClass("flash-up");
    expect(row).not.toHaveClass("flash-down");
  });
});
