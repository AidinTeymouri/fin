import http from "node:http";
import type { AddressInfo, Socket } from "node:net";
import { test, expect, type Page } from "@playwright/test";

// SPEC §12 key scenarios, run against the app container with LLM_MOCK=true.
// Tests share one backend (single user), so they run serially in file order
// (see playwright.config.ts) and assert relative to a baseline read from the API.

const DEFAULT_TICKERS = [
  "AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
  "NVDA", "META", "JPM", "V", "NFLX",
];

const GREEN = "rgba(63, 185, 80";
const RED = "rgba(248, 81, 73";

interface Position {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
}

interface Portfolio {
  cash_balance: number;
  total_value: number;
  positions: Position[];
}

/** "$10,000.00" -> 10000 */
function money(text: string | null): number {
  return Number((text ?? "").replace(/[^0-9.-]/g, ""));
}

async function getPortfolio(page: Page): Promise<Portfolio> {
  const res = await page.request.get("/api/portfolio");
  expect(res.ok()).toBeTruthy();
  return res.json();
}

function heldQty(portfolio: Portfolio, ticker: string): number {
  return portfolio.positions.find((p) => p.ticker === ticker)?.quantity ?? 0;
}

/** A dashboard panel, located by its heading. */
function panel(page: Page, title: string) {
  return page.locator("div.bg-bg-panel", {
    has: page.getByRole("heading", { name: title, exact: true }),
  });
}

function headerValue(page: Page, label: "Portfolio" | "Cash") {
  return page
    .locator("header div")
    .filter({ hasText: new RegExp(`^${label}\\s*\\$[\\d,.]+$`) })
    .locator("span")
    .last();
}

async function headerMoney(page: Page, label: "Portfolio" | "Cash") {
  return money(await headerValue(page, label).textContent());
}

function statusText(page: Page) {
  return page.locator("header").getByText(/^(connected|reconnecting|disconnected)$/);
}

function statusDot(page: Page) {
  return page.locator("header div.rounded-full");
}

function watchlistRow(page: Page, ticker: string) {
  return panel(page, "Watchlist")
    .locator("tbody tr")
    .filter({ has: page.locator("td").first().getByText(ticker, { exact: true }) });
}

function positionRow(page: Page, ticker: string) {
  return panel(page, "Positions")
    .locator("tbody tr")
    .filter({ has: page.locator("td").first().getByText(ticker, { exact: true }) });
}

async function waitForLive(page: Page) {
  await expect(statusText(page)).toHaveText("connected", { timeout: 15_000 });
  await expect(watchlistRow(page, "AAPL").locator("td").nth(1)).toHaveText(
    /^\d+\.\d{2}$/,
    { timeout: 15_000 },
  );
}

/** Submit a trade from the trade bar; returns the fill price from its status line. */
async function tradeViaBar(
  page: Page,
  side: "buy" | "sell",
  ticker: string,
  quantity: number,
): Promise<number> {
  const bar = panel(page, "Trade");
  await bar.getByPlaceholder("Ticker").fill(ticker);
  await bar.getByPlaceholder("Qty").fill(String(quantity));
  await bar.getByRole("button", { name: side.toUpperCase(), exact: true }).click();
  const status = bar.getByText(
    new RegExp(`^${side.toUpperCase()} ${quantity} ${ticker} @ \\$[\\d.]+$`),
  );
  await expect(status).toBeVisible();
  return money((await status.textContent())!.split("@")[1]);
}

/**
 * HTTP proxy in front of the app whose connections the test can cut, so the
 * browser sees a genuine network drop on its open SSE stream (Playwright's
 * offline mode and request routing leave an already-open stream untouched).
 */
async function startDroppableProxy(target: string) {
  const upstream = new URL(target);
  const sockets = new Set<Socket>();
  let refusing = false;

  const server = http.createServer((req, res) => {
    if (refusing) {
      req.socket.destroy();
      return;
    }
    const proxied = http.request(
      {
        hostname: upstream.hostname,
        port: upstream.port || 80,
        path: req.url,
        method: req.method,
        headers: { ...req.headers, host: upstream.host },
      },
      (up) => {
        res.writeHead(up.statusCode ?? 502, up.headers);
        up.pipe(res);
      },
    );
    proxied.on("error", () => res.destroy());
    res.on("close", () => proxied.destroy());
    req.pipe(proxied);
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));

  return {
    url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
    /** Kill every open connection and refuse new ones. */
    drop() {
      refusing = true;
      for (const socket of sockets) socket.destroy();
    },
    restore() {
      refusing = false;
    },
    close: () =>
      new Promise<void>((resolve) => {
        for (const socket of sockets) socket.destroy();
        server.close(() => resolve());
      }),
  };
}

test.beforeEach(async ({ page }) => {
  await page.goto("/");
});

test.describe("Fresh start", () => {
  test("default watchlist, $10k balance, streaming prices", async ({ page }) => {
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("FinAlly");

    // The ten default tickers
    const rows = panel(page, "Watchlist").locator("tbody tr");
    await expect(rows).toHaveCount(DEFAULT_TICKERS.length);
    const shown = await rows.locator("td:first-child").allTextContents();
    expect(shown.sort()).toEqual([...DEFAULT_TICKERS].sort());

    // $10,000.00 cash, nothing invested
    await expect(headerValue(page, "Cash")).toHaveText("$10,000.00");
    await expect(headerValue(page, "Portfolio")).toHaveText("$10,000.00");
    await expect(panel(page, "Positions").getByText("No positions")).toBeVisible();

    // Prices stream in for every ticker and keep moving
    await waitForLive(page);
    await expect(statusDot(page)).toHaveClass(/bg-green/);
    for (const ticker of DEFAULT_TICKERS) {
      await expect(watchlistRow(page, ticker).locator("td").nth(1)).toHaveText(
        /^\d+\.\d{2}$/,
      );
    }
    const prices = () =>
      panel(page, "Watchlist").locator("tbody tr td:nth-child(2)").allTextContents();
    const first = await prices();
    await expect.poll(prices, { timeout: 15_000 }).not.toEqual(first);
  });
});

test.describe("Watchlist", () => {
  test("add a ticker, it streams a price, then remove it", async ({ page }) => {
    const ticker = "SNAP";
    await expect(panel(page, "Watchlist").locator("tbody tr")).not.toHaveCount(0);
    await expect(watchlistRow(page, ticker)).toHaveCount(0);

    const input = page.getByPlaceholder("Add ticker");
    await input.fill(ticker.toLowerCase());
    await input.press("Enter");

    const row = watchlistRow(page, ticker);
    await expect(row).toHaveCount(1);
    // The newly added ticker receives live prices from the stream
    await expect(row.locator("td").nth(1)).toHaveText(/^\d+\.\d{2}$/, {
      timeout: 15_000,
    });
    // ...and it is persisted server-side
    await expect
      .poll(async () =>
        (await (await page.request.get("/api/watchlist")).json()).map(
          (w: { ticker: string }) => w.ticker,
        ),
      )
      .toContain(ticker);

    await row.getByTitle("Remove").click();
    await expect(watchlistRow(page, ticker)).toHaveCount(0);
    await expect
      .poll(async () =>
        (await (await page.request.get("/api/watchlist")).json()).map(
          (w: { ticker: string }) => w.ticker,
        ),
      )
      .not.toContain(ticker);

    // Still gone after a reload
    await page.reload();
    await expect(panel(page, "Watchlist").locator("tbody tr")).toHaveCount(
      DEFAULT_TICKERS.length,
    );
    await expect(watchlistRow(page, ticker)).toHaveCount(0);
  });
});

test.describe("Trading", () => {
  const ticker = "AAPL";

  test("buy then sell shares from the trade bar", async ({ page }) => {
    await waitForLive(page);
    const before = await getPortfolio(page);
    const startQty = heldQty(before, ticker);
    const investedBefore = before.total_value - before.cash_balance;
    await expect(headerValue(page, "Cash")).toHaveText(
      `$${before.cash_balance.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
    );

    await test.step("buy: cash decreases, position appears, total updates", async () => {
      const buyPrice = await tradeViaBar(page, "buy", ticker, 5);
      const expectedCash = before.cash_balance - 5 * buyPrice;

      await expect
        .poll(() => headerMoney(page, "Cash"))
        .toBeCloseTo(expectedCash, 1);
      const row = positionRow(page, ticker);
      await expect(row).toHaveCount(1);
      await expect(row.locator("td").nth(1)).toHaveText(String(startQty + 5));

      // Header total now includes the new position's market value
      await expect
        .poll(async () =>
          (await headerMoney(page, "Portfolio")) - (await headerMoney(page, "Cash")) - investedBefore,
        )
        .toBeGreaterThan(5 * buyPrice * 0.9);
      expect(
        (await headerMoney(page, "Portfolio")) - (await headerMoney(page, "Cash")) - investedBefore,
      ).toBeLessThan(5 * buyPrice * 1.1);
    });

    await test.step("partial sell: cash increases, position quantity updates", async () => {
      const cashBefore = await headerMoney(page, "Cash");
      const sellPrice = await tradeViaBar(page, "sell", ticker, 2);
      await expect
        .poll(() => headerMoney(page, "Cash"))
        .toBeCloseTo(cashBefore + 2 * sellPrice, 1);
      await expect(positionRow(page, ticker).locator("td").nth(1)).toHaveText(
        String(startQty + 3),
      );
    });

    await test.step("full sell: position disappears", async () => {
      const cashBefore = await headerMoney(page, "Cash");
      const qty = startQty + 3;
      const sellPrice = await tradeViaBar(page, "sell", ticker, qty);
      await expect
        .poll(() => headerMoney(page, "Cash"))
        .toBeCloseTo(cashBefore + qty * sellPrice, 1);
      await expect(positionRow(page, ticker)).toHaveCount(0);
    });
  });

  test("rejects selling more than owned", async ({ page }) => {
    await waitForLive(page);
    const before = await getPortfolio(page);
    const bar = panel(page, "Trade");
    await bar.getByPlaceholder("Ticker").fill("MSFT");
    await bar.getByPlaceholder("Qty").fill(String(heldQty(before, "MSFT") + 1000));
    await bar.getByRole("button", { name: "SELL", exact: true }).click();
    await expect(bar.getByText(/^Error:/)).toBeVisible();
    expect((await getPortfolio(page)).cash_balance).toBeCloseTo(before.cash_balance, 2);
  });
});

test.describe("Portfolio visualization", () => {
  test("heatmap tiles are colored by P&L and the P&L chart has data", async ({ page }) => {
    await waitForLive(page);
    // Hold two positions so the heatmap has more than one tile
    for (const ticker of ["JPM", "META"]) {
      const res = await page.request.post("/api/portfolio/trade", {
        data: { ticker, quantity: 1, side: "buy" },
      });
      expect(res.ok()).toBeTruthy();
    }
    await page.reload();
    const portfolio = await getPortfolio(page);
    expect(portfolio.positions.length).toBeGreaterThanOrEqual(2);

    // One tile per position, each green (P&L >= 0) or red (P&L < 0)
    const tiles = panel(page, "Portfolio Heatmap").locator("div[style*='background-color']");
    await expect(tiles).toHaveCount(portfolio.positions.length);
    // Tiles are sorted by live value, so compare as a set
    const shown = await tiles.locator("span:first-child").allTextContents();
    expect(shown.sort()).toEqual(portfolio.positions.map((p) => p.ticker).sort());

    const tileState = await tiles.evaluateAll((els) =>
      els.map((el) => ({
        bg: getComputedStyle(el).backgroundColor,
        pct: el.querySelector("span:last-child")?.textContent ?? "",
      })),
    );
    for (const { bg, pct } of tileState) {
      expect(pct).toMatch(/^[+-]\d+\.\d%$/);
      expect(bg.startsWith(pct.startsWith("-") ? RED : GREEN), `${bg} for ${pct}`).toBe(true);
    }

    // Every trade records a snapshot, so the P&L chart now has data points
    const history = await (await page.request.get("/api/portfolio/history")).json();
    expect(history.length).toBeGreaterThanOrEqual(2);
    const pnl = panel(page, "P&L");
    await expect(pnl.getByText("Waiting for data...")).toHaveCount(0);
    await expect(pnl.locator("path.recharts-line-curve")).toHaveAttribute("d", /^M/);
  });
});

test.describe("AI chat (mocked)", () => {
  test("send a message and receive a response", async ({ page }) => {
    const chat = page.locator("div.bg-bg-panel", {
      has: page.getByRole("heading", { name: "AI Chat" }),
    });
    await expect(
      chat.getByText("Ask about stocks, trade, or manage your watchlist."),
    ).toBeVisible();

    await chat.getByPlaceholder("Message...").fill("hello");
    await chat.getByRole("button", { name: "Send" }).click();

    await expect(chat.getByText("hello", { exact: true })).toBeVisible();
    await expect(chat.getByText(/I'm FinAlly, your AI trading assistant/)).toBeVisible({
      timeout: 15_000,
    });
  });

  test("trade request executes and is confirmed inline", async ({ page }) => {
    await waitForLive(page);
    const before = await getPortfolio(page);
    const startQty = heldQty(before, "NVDA");

    const chat = page.locator("div.bg-bg-panel", {
      has: page.getByRole("heading", { name: "AI Chat" }),
    });
    await chat.getByPlaceholder("Message...").fill("buy 2 NVDA");
    await chat.getByRole("button", { name: "Send" }).click();

    await expect(chat.getByText("Placing a market order to buy 2 NVDA.")).toBeVisible({
      timeout: 15_000,
    });
    await expect(chat.getByText("Bought 2 NVDA", { exact: true })).toBeVisible();

    // The trade lands in the portfolio and the UI refreshes
    await expect(positionRow(page, "NVDA").locator("td").nth(1)).toHaveText(
      String(startQty + 2),
    );
    await expect
      .poll(() => headerMoney(page, "Cash"))
      .toBeLessThan(before.cash_balance);
    expect(heldQty(await getPortfolio(page), "NVDA")).toBe(startQty + 2);
  });

  test("chat panel collapses and expands", async ({ page }) => {
    await page.getByLabel("Collapse chat").click();
    await expect(page.getByRole("heading", { name: "AI Chat" })).toHaveCount(0);
    await page.getByLabel("Expand chat").click();
    await expect(page.getByRole("heading", { name: "AI Chat" })).toBeVisible();
  });
});

test.describe("SSE resilience", () => {
  test("indicator drops when the stream is cut and recovers to green", async ({
    page,
    baseURL,
  }) => {
    const proxy = await startDroppableProxy(baseURL!);
    try {
      await page.goto(proxy.url);
      await waitForLive(page);
      await expect(statusDot(page)).toHaveClass(/bg-green/);

      // Sever the live stream and refuse reconnect attempts
      proxy.drop();
      await expect(statusText(page)).not.toHaveText("connected");
      await expect(statusDot(page)).toHaveClass(/bg-(accent-yellow|red)/);

      // Bring the network back; EventSource reconnects on its own
      proxy.restore();
      await expect(statusText(page)).toHaveText("connected", { timeout: 20_000 });
      await expect(statusDot(page)).toHaveClass(/bg-green/);

      // Prices flow again after the reconnect
      const priceCell = watchlistRow(page, "AAPL").locator("td").nth(1);
      const p0 = await priceCell.textContent();
      await expect.poll(() => priceCell.textContent(), { timeout: 15_000 }).not.toBe(p0);
    } finally {
      await proxy.close();
    }
  });
});
