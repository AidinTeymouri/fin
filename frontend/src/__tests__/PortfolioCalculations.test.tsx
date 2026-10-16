import { render, screen, within } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import PositionsTable from "@/components/PositionsTable";
import PortfolioHeatmap from "@/components/PortfolioHeatmap";
import type { Position } from "@/lib/api";
import type { PriceMap } from "@/hooks/useMarketData";

const positions: Position[] = [
  {
    ticker: "AAPL",
    quantity: 10,
    avg_cost: 100,
    current_price: 110,
    unrealized_pnl: 100,
    pnl_percent: 10,
  },
  {
    ticker: "TSLA",
    quantity: 5,
    avg_cost: 200,
    current_price: 180,
    unrealized_pnl: -100,
    pnl_percent: -10,
  },
];

function livePrice(ticker: string, price: number): PriceMap {
  return {
    [ticker]: {
      ticker,
      price,
      previous_price: price,
      timestamp: "2026-01-01T00:00:00Z",
      direction: "flat",
    },
  };
}

function rowFor(ticker: string) {
  return screen.getByText(ticker).closest("tr")!;
}

describe("PositionsTable calculations", () => {
  it("shows empty state with no positions", () => {
    render(<PositionsTable positions={[]} prices={{}} />);
    expect(screen.getByText("No positions")).toBeInTheDocument();
  });

  it("computes P&L from position price when no live price", () => {
    render(<PositionsTable positions={positions} prices={{}} />);

    const aapl = within(rowFor("AAPL"));
    expect(aapl.getByText("10")).toBeInTheDocument();
    expect(aapl.getByText("$100.00")).toBeInTheDocument();
    expect(aapl.getByText("$110.00")).toBeInTheDocument();
    expect(aapl.getByText("+$100.00")).toHaveClass("text-green");
    expect(aapl.getByText("+10.0%")).toHaveClass("text-green");

    const tsla = within(rowFor("TSLA"));
    expect(tsla.getByText("$180.00")).toBeInTheDocument();
    expect(tsla.getByText("-$100.00")).toHaveClass("text-red");
    expect(tsla.getByText("-10.0%")).toHaveClass("text-red");
  });

  it("recomputes P&L from live streamed price", () => {
    render(<PositionsTable positions={positions} prices={livePrice("AAPL", 125)} />);

    const aapl = within(rowFor("AAPL"));
    expect(aapl.getByText("$125.00")).toBeInTheDocument();
    expect(aapl.getByText("+$250.00")).toBeInTheDocument();
    expect(aapl.getByText("+25.0%")).toBeInTheDocument();
  });

  it("flips to a loss when live price drops below avg cost", () => {
    render(<PositionsTable positions={positions} prices={livePrice("AAPL", 90)} />);

    const aapl = within(rowFor("AAPL"));
    expect(aapl.getByText("-$100.00")).toHaveClass("text-red");
    expect(aapl.getByText("-10.0%")).toHaveClass("text-red");
  });
});

describe("PortfolioHeatmap calculations", () => {
  it("shows empty state with no positions", () => {
    render(<PortfolioHeatmap positions={[]} prices={{}} />);
    expect(screen.getByText("No positions")).toBeInTheDocument();
  });

  it("renders a tile per position with P&L percent and color", () => {
    render(<PortfolioHeatmap positions={positions} prices={{}} />);

    const aaplTile = screen.getByText("AAPL").parentElement!;
    expect(within(aaplTile).getByText("+10.0%")).toHaveClass("text-green");
    expect(aaplTile.style.backgroundColor).toMatch(/^rgba\(63, 185, 80/);

    const tslaTile = screen.getByText("TSLA").parentElement!;
    expect(within(tslaTile).getByText("-10.0%")).toHaveClass("text-red");
    expect(tslaTile.style.backgroundColor).toMatch(/^rgba\(248, 81, 73/);
  });

  it("orders tiles by position value, largest first", () => {
    // AAPL 10 * 110 = 1100, TSLA 5 * 180 = 900
    const { container } = render(<PortfolioHeatmap positions={positions} prices={{}} />);
    const labels = Array.from(container.querySelectorAll("span.font-bold")).map(
      (el) => el.textContent
    );
    expect(labels).toEqual(["AAPL", "TSLA"]);
  });

  it("weights tiles using live prices", () => {
    // TSLA live 400 → 5 * 400 = 2000 > AAPL 1100
    const { container } = render(
      <PortfolioHeatmap positions={positions} prices={livePrice("TSLA", 400)} />
    );
    const labels = Array.from(container.querySelectorAll("span.font-bold")).map(
      (el) => el.textContent
    );
    expect(labels).toEqual(["TSLA", "AAPL"]);
    expect(screen.getByText("+100.0%")).toBeInTheDocument();
  });
});
