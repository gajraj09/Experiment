const tabs = document.querySelectorAll(".tab");
const intervalTitle = document.querySelector("#interval-title");
const tickerPrice = document.querySelector("#ticker-price");
const positionCard = document.querySelector("#position-card");
const positionSide = document.querySelector("#position-side");
const positionMeta = document.querySelector("#position-meta");
const openCandle = document.querySelector("#open-candle");
const tradeLevels = document.querySelector("#trade-levels");
const tradeExecutionStatus = document.querySelector("#trade-execution-status");
const closedCandles = document.querySelector("#closed-candles");
const tradeCount = document.querySelector("#trade-count");
const tradeHistory = document.querySelector("#trade-history");
const tradeEmpty = document.querySelector("#trade-empty");
const tradeSummary = document.querySelector("#trade-summary");
const statusLine = document.querySelector("#status");
const fillStatusLabel = document.querySelector("#fill-status");
const fillStatusCard = document.querySelector("#position-card1");
const liveStatusDot = document.querySelector("#live-status");
const liveStatusText = document.querySelector("#live-status-text");

let activeInterval = "5m";
let refreshTimer;
 
const marketCache = new Map();
const intervals = ["5m", "15m"];

function formatTime(value) {
  if (value === null || value === undefined || value === '') return '--';
  // numeric timestamps (ms)
  if (typeof value === 'number') return new Date(value).toLocaleString();
  // try parsing ISO or other Date-parsable strings
  const d = new Date(value);
  if (!isNaN(d.getTime())) return d.toLocaleString();
  // fallback: value is already a formatted display string (e.g. "17-06-2026 23:15:00 IST")
  return String(value);
}

function formatTimeOnly(value) {
  return new Date(value).toLocaleTimeString();
}

function ohlcCells(candle) {
  return ["open", "high", "low", "close"]
    .map(
      (field) => `
        <div class="cell">
          <span>${field}</span>
          <strong>${candle[field]}</strong>
        </div>
      `
    )
    .join("");
}

function renderClosedCandles(candles) {
  return candles
    .map(
      (candle) => `
        <article class="candle">
          <h2>${formatTime(candle.open_time)}</h2>
          <div class="ohlc-grid">${ohlcCells(candle)}</div>
        </article>
      `
    )
    .join("");
}

function levelCard(label, value, tone = "neutral") {
  return `
    <div class="cell level-card ${tone}">
      <span>${label}</span>
      <strong>${value}</strong>
    </div>
  `;
}

function renderTradeLevels(levels) {
  tradeLevels.innerHTML = [
    levelCard("Current Candle Open Time", formatTimeOnly(levels.current_open_time)),
    levelCard("Buy Trigger Price", levels.buy_trigger_price, "buy"),
    levelCard("Sell Trigger Price", levels.sell_trigger_price, "sell"),
    levelCard("ATR Value", levels.atr_value),
  ].join("");
}

function positionLabel(position) {
  if (!position) {
    return "Flat";
  }

  return position.type === "long" ? "Long" : "Short";
}

function renderTradeExecutionStatus(data) {
  const executionState = data.trade_execution_state || {};
  tradeExecutionStatus.innerHTML = [
    levelCard(
      "Trade Placed In Current Candle",
      executionState.tradePlacedInCurrentCandle ? "Yes" : "No"
    ),
    levelCard("Current Position", positionLabel(data.current_position)),
    levelCard(
      "Trade Number",
      executionState.executedTradeNumber != null ? executionState.executedTradeNumber : "--"
    ),
    levelCard(
      "Execution Time",
      executionState.executedTradeTime ? formatTime(executionState.executedTradeTime) : "--"
    ),
  ].join("");
}

function renderFillStatus(tradeHistoryRows) {
  const lastTrade = tradeHistoryRows.length ? tradeHistoryRows[tradeHistoryRows.length - 1] : null;
  const status = lastTrade?.fill_status || "None";

  if (fillStatusCard) {
    fillStatusCard.classList.toggle("filled", status.toLowerCase() === "filled");
    fillStatusCard.classList.toggle("unfilled", status.toLowerCase() === "unfilled");
  }

  if (fillStatusLabel) {
    fillStatusLabel.textContent = status === "None" ? "None" : status;
  }
}

function calculatePnlForRow(row, quantity) {
  return row.netPnl;
}

function formatNumber(value, digits = 4) {
  return Number(value).toFixed(digits);
}

function formatPnl(value) {
  if (value === null || value === undefined) {
    return '<span class="neutral">--</span>';
  }

  const amount = Number(value);
  const sign = amount >= 0 ? "+" : "";
  const className = amount > 0 ? "profit" : amount < 0 ? "loss" : "neutral";
  return `<span class="${className}">${sign}${amount.toFixed(3)}</span>`;
}

function formatExcursion(value, positive) {
  if (value === null || value === undefined) {
    return '<span class="neutral">--</span>';
  }

  const amount = Math.abs(Number(value));
  const sign = positive ? "+" : "-";
  const className = positive ? "profit" : "loss";
  return `
    <span class="${className}">${sign}${amount.toFixed(3)}</span>
    <small>${sign}${(amount * 100).toFixed(2)}%</small>
  `;
}

function tradeSummaryStats(rows) {
  const exitRows = rows.filter((row) => row.signal === "Exit" && row.netPnl !== null);
  const winners = exitRows.filter((row) => row.netPnl > 0).length;
  const losers = exitRows.filter((row) => row.netPnl < 0).length;
  const totalNetPnl = rows.reduce((sum, row) => sum + (Number(row.netPnl) || 0), 0);
  const lastCumulative = [...rows].reverse().find((row) => row.cumulativePnl !== null)?.cumulativePnl ?? 0;
  const winRate = exitRows.length ? (winners / exitRows.length) * 100 : 0;
  const totalTrades = new Set(rows.map((row) => row.tradeNumber)).size;

  return { totalTrades, winners, losers, totalNetPnl, lastCumulative, winRate };
}

function renderTradeSummary(rows) {
  const stats = tradeSummaryStats(rows);
  tradeSummary.innerHTML = `
    <span>Total Trades <strong>${stats.totalTrades}</strong></span>
    <span>Winners <strong class="profit">${stats.winners}</strong></span>
    <span>Losers <strong class="loss">${stats.losers}</strong></span>
    <span>Win Rate <strong>${stats.winRate.toFixed(1)}%</strong></span>
    <span>Total Net P&L <strong>${formatPnl(stats.totalNetPnl)}</strong></span>
    <span>Cumulative P&L <strong>${formatPnl(stats.lastCumulative)}</strong></span>
  `;
}

function renderTradeHistory(rows) {
  const displayRows = [...rows].reverse();
  const totalTrades = new Set(rows.map((row) => row.tradeNumber)).size;
  tradeCount.textContent = `${totalTrades} trade${totalTrades === 1 ? "" : "s"}`;
  tradeEmpty.hidden = rows.length > 0;

  const evaluatedRows = displayRows;

  tradeHistory.innerHTML = evaluatedRows
    .map(
      (row) => `
        <tr>
          <td>${row.tradeNumber}</td>
          <td><span class="type-badge ${row.type}">${row.type}</span></td>
          <td>${formatTime(row.dateTime)}</td>
          <td><span class="signal ${row.signal.toLowerCase()}">${row.signal}</span></td>
          <td>${formatNumber(row.price)}</td>
          <td>${formatNumber(row.size, 2)}</td>
          <td>
            ${row.fill_status ? `<span class="fill-badge ${row.fill_status.toLowerCase()}">${row.fill_status}</span>` : '<span class="neutral">--</span>'}
          </td>
          <td>${row.fill_time ? formatTime(row.fill_time) : '<span class="neutral">--</span>'}</td>
          <td>${row.openCandleTime ? formatTime(row.openCandleTime) : '<span class="neutral">--</span>'}</td>
          <td>${formatPnl(row.netPnl)}</td>
        </tr>
      `
    )
    .join("");

  renderTradeSummary(rows);
}

function exportRowsToCSV(rows) {
  if (!rows || !rows.length) return null;
  const headers = [
    'tradeNumber','type','dateTime','signal','price','size','fill_status','fill_time','openCandleTime','netPnl','favorableExcursion','adverseExcursion','cumulativePnl'
  ];

  const csv = [headers.join(',')];
  for (const r of rows) {
    const values = headers.map((h) => {
      let v = r[h];
      if (v === null || v === undefined) return '';
      // if date, format as ISO
      if (h === 'dateTime' || h === 'fill_time' || h === 'openCandleTime') {
        try { return `"${new Date(v).toISOString()}"`; } catch(e) { return `"${v}"`; }
      }
      // escape quotes and commas
      if (typeof v === 'string') return `"${v.replace(/"/g, '""')}"`;
      return String(v);
    });
    csv.push(values.join(','));
  }

  return csv.join('\n');
}

function triggerDownload(filename, content, mime = 'text/csv') {
  const blob = new Blob([content], { type: mime + ';charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Attach download handler immediately (script is loaded at end of body)
const _downloadBtn = document.querySelector('#download-trades');
if (_downloadBtn) {
  _downloadBtn.addEventListener('click', async () => {
    try {
      console.log('Download button clicked for interval', activeInterval);
      let market = marketCache.get(activeInterval);
      if (!market) {
        console.log('No cached market, fetching...');
        market = await fetchMarket(activeInterval).catch((e) => {
          console.error('fetchMarket failed', e);
          return null;
        });
      }

      let rows = market?.trade_history || [];
      if (!rows || rows.length === 0) {
        // fallback: call trades endpoint which returns { trade_history: [...] }
        console.log('No rows in market response, fetching /api/trades');
        try {
          const resp = await fetch(`/api/trades/${activeInterval}`, { cache: 'no-store' });
          if (resp.ok) {
            const json = await resp.json();
            rows = json.trade_history || [];
          } else {
            console.warn('Failed to fetch /api/trades', await resp.text());
          }
        } catch (e) {
          console.error('Error fetching /api/trades', e);
        }
      }

      const csv = exportRowsToCSV(rows);
      if (!csv) {
        alert('No trade data to download');
        return;
      }
      const now = new Date().toISOString().replace(/[:.]/g, '-');
      const filename = `trades-${activeInterval}-${now}.csv`;
      console.log('Triggering download', filename, 'rows:', rows.length);
      triggerDownload(filename, csv, 'text/csv');
    } catch (err) {
      console.error('Download handler error', err);
      alert('Unable to download trade data. See console for details.');
    }
  });
}

function renderCurrentPosition(position) {
  positionCard.classList.remove("long", "short");

  if (!position) {
    positionSide.textContent = "Flat";
    positionMeta.textContent = "No open position";
    return;
  }

  positionCard.classList.add(position.type);
  positionSide.textContent = position.type === "long" ? "Long" : "Short";
  positionMeta.textContent = `${formatNumber(position.size, 2)} @ ${formatNumber(position.entryPrice)}`;
}

function renderMarket(data) {
  intervalTitle.textContent = data.interval === "5m" ? "5min" : "15min";
  tickerPrice.textContent = data.ticker_price;
  renderCurrentPosition(data.current_position);
  renderFillStatus(data.trade_history || []);
  openCandle.innerHTML = ohlcCells(data.open_candle);
  renderTradeLevels(data.trade_levels);
  renderTradeExecutionStatus(data);
  closedCandles.innerHTML = renderClosedCandles(data.closed_candles);
  renderTradeHistory(data.trade_history || []);
}

async function fetchMarket(interval) {
  const response = await fetch(`/api/market/${interval}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(await response.text());
  }

  const data = await response.json();
  marketCache.set(interval, data);
  return data;
}

async function loadMarket(interval = activeInterval, shouldRender = interval === activeInterval) {
  try {
    const data = await fetchMarket(interval);
    if (shouldRender && interval === activeInterval) {
      renderMarket(data);
      statusLine.textContent = `Updated ${new Date().toLocaleTimeString()}`;
      updateLiveStatus(true);
    }
  } catch (error) {
    if (shouldRender) {
      statusLine.textContent = "Unable to load Binance market data right now.";
      updateLiveStatus(false);
    }
    console.error(error);
    return null;
  }
}

function updateLiveStatus(online) {
  if (!liveStatusDot || !liveStatusText) {
    return;
  }

  if (online) {
    liveStatusDot.classList.remove("offline");
    liveStatusDot.classList.add("online");
    liveStatusDot.setAttribute("aria-label", "Ticker online");
    liveStatusText.textContent = "Live";
  } else {
    liveStatusDot.classList.remove("online");
    liveStatusDot.classList.add("offline");
    liveStatusDot.setAttribute("aria-label", "Ticker offline");
    liveStatusText.textContent = "Offline";
  }
}

function refreshAllMarkets() {
  intervals.forEach((interval) => {
    loadMarket(interval, interval === activeInterval);
  });
}

function setIntervalTab(interval) {
  activeInterval = interval;
  tabs.forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.interval === interval);
  });

  intervalTitle.textContent = interval === "5m" ? "5min" : "15min";
  const cachedMarket = marketCache.get(interval);
  if (cachedMarket) {
    renderMarket(cachedMarket);
    statusLine.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    // update download link
    const dl = document.querySelector('#download-trades');
    if (dl) dl.href = `/api/trades/${interval}/download`;
  } else {
    statusLine.textContent = "Loading market data...";
    loadMarket(interval, true);
  }
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => setIntervalTab(tab.dataset.interval));
});


refreshAllMarkets();
refreshTimer = window.setInterval(refreshAllMarkets, 250);
setIntervalTab(activeInterval);
