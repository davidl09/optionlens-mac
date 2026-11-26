# OptionLens

OptionLens is a self-hosted options and stock analytics workstation for one user. It is one desktop app
for macOS, Windows (x64) and Linux (x64):

- **The app** (Electron): the analytics UI, served from `app://optionlens`.
- **The engine** (Python, `engine/`): FastAPI + DuckDB + a scheduler. The app starts it as a local
  sidecar on `127.0.0.1` with a random per-launch token and stops it when you quit. Your data stays on
  your computer (see [Where things are](#where-things-are)).
- **The IBKR bridge** (in the app): connects to your running **IB Gateway** (or TWS) and pushes option
  chains, universe-wide option statistics, IV/HV history and VIX-family data into the engine.

OptionLens keeps running in the tray (the macOS menu bar, the Windows notification area, the Linux
AppIndicator area) when you close its window, so collection and the bridge continue. Use **Quit OptionLens**
in the tray menu to stop everything. On a Linux desktop without a tray (plain GNOME without the
AppIndicator extension), closing the window quits OptionLens.

## Install

The installers download the latest release from [GitHub Releases](https://github.com/davidl09/optionlens-mac/releases),
check its SHA-256 against the digest GitHub publishes for the file (no digest, no install), and never need
administrator rights.

**macOS** 12.3 (Monterey) or later, in Terminal:

```
curl -fsSL https://raw.githubusercontent.com/davidl09/optionlens-mac/main/install.sh | bash
```

[install.sh](install.sh) picks the zip for your Mac (Apple silicon or Intel), checks the app's bundle id,
version and signature, quits a running OptionLens, keeps the installed app as a backup (in
`~/Library/Application Support/OptionLens/backups`, the newest 2), moves the new app to **Applications** and
opens it. If `/Applications` is not writable for your user it stops and says so.

**Windows 10 or 11 (x64)**, in PowerShell (Windows PowerShell 5.1 or PowerShell 7):

```
irm https://raw.githubusercontent.com/davidl09/optionlens-mac/main/install.ps1 | iex
```

[install.ps1](install.ps1) downloads `OptionLens-<version>-win-x64-setup.exe`, checks it with `Get-FileHash`,
keeps a copy in `%APPDATA%\OptionLens\backups` (so a later update can roll back to it), installs it silently
for your user (in `%LOCALAPPDATA%\Programs`, with Start-menu and desktop shortcuts) and starts OptionLens.
Quit a running OptionLens first.

**Linux (x86_64)** with glibc 2.28 or newer, GTK 3 and FUSE (CI tests Ubuntu 24.04 and runs the engine on
Ubuntu 22.04). In a terminal:

```
curl -fsSL https://raw.githubusercontent.com/davidl09/optionlens-mac/main/install.sh | bash
```

On Linux, install.sh downloads `OptionLens-<version>-linux-x64.AppImage`, checks it with `sha256sum` and
that it is the OptionLens AppImage of that version, moves it to `~/.local/bin/OptionLens.AppImage`
(`OPTIONLENS_BIN_DIR` sets another folder), adds **OptionLens** to your applications menu
(`~/.local/share/applications/optionlens.desktop`) and starts it. AppImages need FUSE (`fusermount3` or
`fusermount`; Debian/Ubuntu: `sudo apt install fuse3`), not `libfuse2`.

**Manual downloads** from the Releases page; check the file against the SHA-256 shown for it there first
(`shasum -a 256 <file>` on macOS, `Get-FileHash <file>` on Windows, `sha256sum <file>` on Linux):

- macOS: `OptionLens-<version>-arm64.zip` (Apple silicon) or `OptionLens-<version>-x64.zip` (Intel). Extract it
  with `ditto -x -k OptionLens-<version>-<arch>.zip .` (or double-click it in Finder) and move **OptionLens** to
  **Applications**. The app is ad-hoc signed, not signed with an Apple Developer ID, so a browser download is
  quarantined: remove the flag once with `xattr -dr com.apple.quarantine /Applications/OptionLens.app` (or
  right-click → **Open**).
- Windows: `OptionLens-<version>-win-x64-setup.exe` installs for your user and updates itself.
  `OptionLens-<version>-win-x64.zip` is a portable folder (run `OptionLens.exe` from it); it does not update
  itself. The files are not code-signed, so for a browser download Windows SmartScreen shows "Windows protected
  your PC": click **More info**, then **Run anyway**.
- Linux: `OptionLens-<version>-linux-x64.AppImage`. Make it executable (`chmod +x`) and run it; keep it in a
  folder you own (such as `~/.local/bin`) so it can update itself.

Files that the installers or the app itself download are not quarantined (macOS) and carry no
Mark-of-the-Web (Windows), so they open without a Gatekeeper or SmartScreen prompt.

## Where things are

| | macOS | Windows | Linux |
|---|---|---|---|
| App | `/Applications/OptionLens.app` | `%LOCALAPPDATA%\Programs\optionlens-mac\OptionLens.exe` | `~/.local/bin/OptionLens.AppImage` |
| Data (database) | `~/Library/Application Support/OptionLens/data` | `%APPDATA%\OptionLens\data` | `~/.config/OptionLens/data` |
| Settings, update state | `~/Library/Application Support/OptionLens` | `%APPDATA%\OptionLens` | `~/.config/OptionLens` |
| Backups (app, database) | `…/OptionLens/backups` | `%APPDATA%\OptionLens\backups` | `~/.config/OptionLens/backups` |
| Logs (`main.log`, `updater.log`) | `~/Library/Logs/OptionLens` | `%APPDATA%\OptionLens\logs` | `~/.config/OptionLens/logs` |
| Launch at login | Login item (System Settings → General → Login Items) | Startup app (Task Manager → Startup apps) | `~/.config/autostart/optionlens.desktop` |

Uninstalling (Windows: Settings → Apps; macOS/Linux: delete the app) keeps your data.

## Updating

OptionLens updates itself: **Settings → Updates** shows the installed and latest versions and the release
notes, with **Check now** and **Download and install**. It checks GitHub at launch and every 6 hours; when
a new version is out, the tray menu shows **Update available (x.y.z)…** and you get one notification for
it. An update is downloaded over HTTPS and checked against the SHA-256 digest that GitHub publishes for the
release file (no digest, no install); only the file for your system is used. Then it is checked and
installed after OptionLens quits (the engine stops normally), and the new version opens:

- **macOS**: the zip is extracted and the app's bundle id, version and signature are checked; a small helper
  swaps the app and keeps the previous one as a backup.
- **Windows**: the installer's version information (product name and version) is checked; after OptionLens
  quits, it runs silently for your user (no prompts, no administrator rights) and OptionLens starts again.
  The installer is kept in the backups folder; if it fails, the previous version's installer runs instead. A
  copy from the portable zip does not update itself: Settings says how to install the new version.
- **Linux**: the AppImage's name and version are checked, it is put next to the running AppImage, and after
  OptionLens quits it replaces it (one rename) and starts; the previous AppImage is kept as a backup. Only an
  AppImage in a folder you can write to updates itself; otherwise Settings says what to do.

- **Install updates automatically** (off by default) downloads new versions in the background and installs
  them when the computer has been idle for 5 minutes with no OptionLens window focused, or when you click
  **Restart now** on the "ready" notification or in the tray menu. Never while the engine collects the
  closing data (16:15–16:50 New York time on weekdays) or while a data import runs.
- Before a new version's engine first opens your database, the database is copied to
  `backups/db-<previous version>-<time>.duckdb` (the newest 2 are kept), on every OS.
- If a new version's engine does not become healthy within 60 s, or the app crashes twice in its first
  2 minutes, OptionLens restores the previous version from its backup, opens it, tells you, and does not
  install that version automatically again. It tries this once; the details are in `updater.log`.
- macOS: the app must run from a writable folder such as **Applications**. When macOS runs it from a
  read-only place (a disk image, or a randomized "translocated" copy of a quarantined download), Settings
  says **Move OptionLens to the Applications folder first**.
- The update source is a GitHub repository (`owner/repo`, default `davidl09/optionlens-mac`); only assets
  of that repository's releases are downloaded. Drafts and prereleases are ignored, and older versions are
  never installed.

**From 0.1.0–0.1.2 on macOS** (dmg builds without the updater): run the one-line installer above once. Your
data and settings stay where they are.

### Importing data from an old OptionLens server (one time)

If you ran OptionLens as a separate server before, copy its `optionlens.duckdb` (from the server's
`OPTIONLENS_DATA_DIR`, with the server stopped) to this computer. Then open **Settings → Engine → Import data…**
and pick the file. The engine stops, the file is copied into the app's data folder (the current
database is kept as a `.bak-<time>` backup) and the engine starts again.

## Connect IB Gateway (IBKR)

The bridge talks to the IBKR API of IB Gateway or TWS on this computer (IB Gateway runs on macOS, Windows
and Linux; the ports and settings are the same on each). It finds it automatically on the
default ports, in this order: **4001** (IB Gateway, live), **7496** (TWS, live), **4002** (IB Gateway, paper),
**7497** (TWS, paper). You can set a different port in **Settings → Bridge connection**. The menu-bar
menu and Settings show which one is connected and whether the account is live or paper.

In IB Gateway, open **Configure → Settings → API → Settings**:

- Keep the default **Socket port** (4001 live / 4002 paper).
- Turn on **Read-Only API** (recommended: the bridge only reads data and never places orders).
- Under **Trusted IPs**, add `127.0.0.1`.
- In TWS, also turn on **Enable ActiveX and Socket Clients** (Global Configuration → API → Settings).

IB Gateway must accept connections from `127.0.0.1` on the same computer; nothing else needs to be opened:
the Windows Firewall prompt that IB Gateway (Java) may show is for other computers, and loopback connections
work without allowing it. On Linux, run IB Gateway as your desktop user (its installer puts it in
`~/Jts`); the bridge connects to it like on the other systems.

IB Gateway restarts every night. The bridge reconnects automatically. If you log in to IBKR somewhere
else with the same username (for example the mobile app), IBKR stops market data for this session; the
bridge then shows **"IBKR session in use elsewhere — market data paused"** and tries again quietly
every 5 minutes.

### Market data subscriptions

The bridge asks IBKR for real-time data and falls back to IBKR's free 15-minute delayed data for each
contract class you do not subscribe to. Delayed pushes are labelled **delayed** in the UI, and the status
shows **"IBKR delayed (no OPRA subscription)"** when option quotes are delayed.

- **US options**: *OPRA Top of Book (L1)* for real-time option quotes and greeks.
- **US stocks**: *US Securities Snapshot and Futures Value Bundle* or *US Equity and Options Add-On
  Streaming Bundle* (the free non-consolidated IBKR quotes also work for stock prices).
- **Indices** (SPX, NDX, RUT, VIX family): *CBOE Market Data* / *Nasdaq Indexes* for real-time index values.

Some generic ticks (option volume and open interest per underlying) are not sent with delayed data.
The bridge then sends those values as unknown (never zero) and the status says so.

### Canadian listings on IBKR

The bridge collects the same data for Canadian symbols (RY.TO, RCI.B.TO, …) as for US ones: option chains from the
Montréal Exchange (merged with Questrade's, the better chain is used), closing chains, IV and HV history (IV rank and
percentile), option-bar catch-up, underlying stats, quotes, intraday bars and dividend-adjusted history. It works
on the TSX calendar for them (for example on US Thanksgiving, when only Toronto trades), in CAD. IBKR positions in
Canadian stocks and options are valued with the Canadian listing's prices on the Portfolio page.

Canadian market data is its own set of IBKR subscriptions (*Toronto Stock Exchange*, *TSX Venture Exchange*,
*Montréal Exchange*). Without them the bridge uses IBKR's delayed data where IBKR offers it; where IBKR refuses the
data, the bridge stops asking for that exchange for 20 hours and the status lists it ("No IBKR market-data permission
(skipped): TSX stocks"). US work is not affected either way. This support follows IBKR's documented contract
conventions and has not been checked against every account type: if Canadian symbols stay empty, the app's log lines
starting with `bridge:` say what IBKR answered.

**Market data lines**: IBKR limits how many instruments you can stream at once (100 by default).
Set your limit in **Settings → Bridge connection**. The bridge uses about 90% of it in each batch. If TWS
says the limit is reached (error 101), the bridge uses smaller batches until it reconnects, and the status
says so. A closing chain is archived only when at least 95% of its contracts were quoted; otherwise the
bridge tries again later.

### What the bridge does

It polls the engine's `GET /api/ingest/wants` every 5 minutes and right after connecting, then works
through the work one task at a time:

- **On-demand refresh** always goes first: when you open a symbol page, the app fetches that symbol's
  chain (first 8 expiries, strikes within ±25%) and reloads the page. It does not wait for a long task
  to finish (it runs between that task's batches), and it is dropped when you leave the page before it ran.
- Everything else shares IBKR time, so each kind makes progress all the time (largest share first):
  **closing chains** for watchlists and the chain universe after the close; the **chain universe**
  refresh (near-the-money chains for the most liquid symbols and your watchlists, 09:00–20:00 ET);
  **underlying stats** for the whole universe (option volume, open interest, IV, HV per stock);
  VIX-family quotes and daily bars; **option-bar catch-up** for sessions without an end-of-day archive;
  and the **IV / HV history backfill**, up to 10 years.
- **First launch outside market hours** (evening, weekend, before the open): when the last session has no
  data yet, the bridge collects its closing values from IBKR's frozen data right after connecting — the
  underlying stats for the whole universe first (a few minutes), then the chain universe — so reports and
  the dashboard fill in without waiting for the next session. Settings shows the progress ("Populating last
  session's data: 340/1145"), and such values carry a "Last session's close (collected after hours)"
  badge. They never replace data collected during the session or its closing snapshot.

All historical requests are for daily bars, which IBKR does not hard-limit like small bars: they are
sent about 1 s apart, and after a real pacing violation the bridge pauses (30 s, growing) and spaces
requests further apart for a while. When IBKR says your account has no market-data permission for some
data (for example option bars without OPRA), the bridge skips that symbol and kind for the day — a whole
Canadian exchange when nothing of it is served — and the status lists it. Backfills can be resumed: after a restart or a reconnect, the bridge continues where it
stopped. You can cancel bridge work from the menu-bar menu or from Settings.

## Connect Questrade (optional)

With a Questrade account the engine can collect option chains itself — the whole universe, also without IB Gateway:

1. Log in to Questrade, open **API Centre** (menu under your name), activate the API and accept the agreement.
2. **Register a personal app** (any name) with the permission **Retrieve delayed and real-time market data**, then
   under it **New manual authorization** → **Generate new token** → **Copy token**.
3. In OptionLens open **Settings → Data sources → Questrade**, paste the token and click **Connect**. The engine
   checks it with one market-data call and says "Token OK: market data permitted" or what is wrong (e.g. the app
   lacks the market-data permission: enable it, generate a new token, paste it again).

The engine exchanges the token at once and keeps the new one Questrade returns (stored only on this computer, in
the app's data folder, readable by your user only). If the card says **token expired** (a token that was already
used, revoked or left unused too long), generate a new one the same way. Quotes are real-time only with a Questrade
real-time data package; otherwise they are delayed and badged so. The engine uses at most 70% of Questrade's API
limits. IBKR and Questrade are equal sources: opening a symbol fetches from both at once, and the better chain is
shown — a closing snapshot over an intraday one, real-time over delayed, more quoted contracts, then the fresher
fetch; every chain shows its source. With both connected, Questrade sweeps the universe and IBKR adds what only it
provides (IV/HV history, index history, option-bar catch-up) plus the symbols Questrade could not fetch; if Questrade
is unavailable, IBKR takes over its chains. If Questrade refuses access (e.g. a missing permission), it stops at once
and Settings → Data sources says what to fix.

Questrade also fills data the free sources lack: once a day each symbol's P/E, EPS, market cap, shares outstanding,
52-week range, average volumes, sector and current dividend (one value per figure on every page; P/E and market cap
follow the live price), VWAP and trading halts in quotes (HALTED badge, `halted` / `resumed` alerts), and
extended-hours quotes of the whole universe for the pre-market / after-hours movers. Dividends cover every US listing:
Nasdaq's history where it has one, else dividends derived from IBKR's dividend-adjusted daily closes (the IBKR bridge
backfills ten years per symbol, a few hours on the first connection), else Questrade's current dividend — each with
its source, and amounts that disagree between sources flagged.

### Canadian markets (with Questrade)

With Questrade connected OptionLens also follows Canada: about 200 of the largest TSX / TSX Venture stocks with options
on the Montréal Exchange (MX), plus the Canadian ETFs with MX options (XIU, XIC, XEG, XFN, ZEB, HXT, …), chosen weekly
from the MX option list by Questrade's market cap. Canadian symbols carry their exchange suffix — **RY.TO** is Royal
Bank on the TSX, **RY** its NYSE listing; search shows both with exchange and currency. Canadian prices are in CAD
(shown as **C$**; in a table holding both currencies US amounts read **US$**) and are never converted or added to US
amounts. Reports have a **Market** filter (US by default, Canada, or All; totals are per market). Canadian chains,
price history, intraday charts and live quotes come from Questrade, sessions follow the TSX holiday calendar, and options
are priced with the Bank of Canada 3-month T-bill rate. Questrade's daily figures (P/E, EPS, market cap, current
dividend, …) cover Canadian listings too. Their earnings come from **TMX Money** (earnings calendar with BMO / AMC and
EPS, 3 years back, and each company's next date), else **Yahoo Finance** (next date) — both unofficial endpoints that
may change or stop answering; each event shows its source, Status → Data sources shows how each did, and a listing
without a date says why (e.g. "TMX Money and Yahoo Finance were unavailable"). OptionLens asks them as itself, once a
second, once a day (docs/INTEGRATION.md §12). TMX Money also fills their **About** (company or fund description, sector,
industry, exchange, website), **News** (press releases, linked to the story on money.tmx.com) and **Insiders** tabs
(TMX's 3- and 12-month totals and the SEDI insider reports of the last 12 months: shares are signed, negative =
disposed; the company's own buybacks are left out), fetched when a tab is opened and cached like the Nasdaq data of US
listings (§12.1), and their **dividend histories** (ex-dates with declared, record and pay dates; the Dividends
tab, the dividend reports with the Market filter, the chart's ex-dividend markers and the yields): amounts are in
today's shares (adjusted for later splits) and only payments in Canadian dollars are kept — a listing that pays in US
dollars says so instead of showing TMX's history (§12.2); the IBKR-derived dividends and Questrade's current
dividend stay as the fallbacks. Not
available for Canadian listings (no free source): financial statements — the Fundamentals tab says so under the key
figures. Pre-market / after-hours
quotes are US-only (the TSX has no extended-hours trading). The IBKR bridge covers Canadian listings too (see
"Canadian listings on IBKR" under Connect IB Gateway).

## IV surface

Symbol → Skew shows, under the smile, the implied volatility of every expiry up to a year out as a heatmap: rows by
delta (10Δ puts … ATM … 10Δ calls), by moneyness (−20% … +20% of spot) or by strike, coloured by IV, by IV minus the
expiry's ATM IV, or by the change since the previous session's stored closing chain. Only contracts with a bid, a
spread of at most 40% of the mid and open interest feed the surface; cells they cannot give are greyed with the reason
in the tooltip. The first expiry after the next earnings is marked **E**. Clicking a cell adds its contract to the
calculator (at the live mid); contracts already there are outlined.

## Strategy calculator

Symbol → Calculator prices a position of up to 8 option / stock legs on the stored chain (live mid / IV while quotes
flow): P/L at expiry, at an evaluation date and today, breakevens, max profit / loss, probability of profit, expected
value under IV and HV30, position greeks and vega per expiry. When the next earnings fall inside the position, the
engine estimates the implied earnings move from the ATM term structure and values each leg through the IV crush: a
leg's IV drops to its ex-earnings base after the reaction session (a note above the chart shows the move and the
back month's IV after earnings, with a switch to turn the crush off; the legs table shows the IV used when it differs
from yours). EV under HV30 is not shown when earnings fall before the first expiry (HV30 holds no earnings move).

## Portfolio (read-only)

**My → Portfolio** shows your positions from Questrade and IBKR: totals (net liquidation, cash, buying power, market
value, unrealized and day P/L, delta $, theta, vega, beta-weighted delta vs SPY), positions grouped by underlying
with live prices and P/L, option greeks, a risk graph per underlying and **Open in calculator** (your positions at
their cost basis). Totals in USD or CAD use the exchange rate your broker reported (shown); without one, each currency
is shown on its own. OptionLens only *reads* positions and balances — it has no way to place, change or cancel
orders — account numbers show only their last 3 digits, and nothing leaves your computer.

- **Questrade**: your personal app also needs the permission **Retrieve balances, positions, orders and executions**.
  In Questrade's **API Centre** open your personal app, tick that permission (keep **Retrieve delayed and real-time
  market data**; never enable trading), save, then **New manual authorization** → **Generate new token** → **Copy
  token**, and paste it in **Settings → Data sources → Questrade** → **Connect**. Until then the Portfolio page and the
  Questrade card say what to enable; market data keeps working.
- **IBKR**: nothing to set up beyond the bridge (IB Gateway / TWS connected); the API's "Read-Only API" setting can stay
  on.
- **Settings → Data sources → Show my positions** (per broker, on by default) hides a broker's positions and stops
  fetching them.

## Live quotes

While the market is open, what is on screen updates by itself: the symbol header, the option chain (the expiry and
strikes you are looking at), the calculator, watchlists, the dashboard and report rows (price, change, bid/ask/last,
volume; reports also re-run every 45 s), and the chart's last candle. The header says where the data comes from:
**LIVE · Questrade**, **DELAYED** (your data package is delayed), **MARKET CLOSED** (nothing is fetched; stored values
are shown) or **NOT CONNECTED**. Values from a delayed feed are labelled "delayed", never "live" (the calculator's
price / IV / spot tags, the portfolio's price source). Changed values are briefly tinted. With Questrade connected the engine polls only
what is visible, every 2–10 s depending on how much is on screen, within 70% of Questrade's limits together with the
background sweeps; without Questrade, IB Gateway snapshots the visible symbols. The symbols you follow (portfolio
holdings, watchlists, recently opened; at most 40) are also kept live in the background during their sessions, so
their 1-minute chart is already up to date when you open it (Questrade's own candles arrive ~15 minutes late). A hidden or minimized window stops
live updates. Questrade's Level 1 streaming (tick-by-tick) can be turned on instead of polling in Settings → Data sources →
Questrade ("Stream live quotes"; it falls back to polling on its own when streaming fails), but Questrade says it freezes the market data of any other Questrade platform you have open at the same time.

## AI agents (MCP)

OptionLens can give an AI agent on your computer — Claude Code, Claude Desktop or any MCP client — the same view of
your data that you have: every page (symbols, chains, volatility, earnings…), all reports with their parameters, the
market overview, comparisons, status, live quotes, your watchlists, alerts, presets and notifications, and **what you
are looking at right now** (`get_current_view`: the page, its parameters, filters, sort and visible rows, with the same
data). Agents can also edit watchlists, alerts and presets and mark notifications read. No trading is possible:
OptionLens has no orders or account access.

Turn it on in **Settings → AI agents (MCP)** (off by default). Settings then shows who is connected and the last call,
a **Regenerate key** button, and copyable setup snippets with this computer's paths:

- **Claude Code** (macOS): `claude mcp add optionlens -- "/Applications/OptionLens.app/Contents/Resources/mcp/optionlens-mcp"`
- **Claude Code** (Windows): `claude mcp add optionlens -- cmd /c "%LOCALAPPDATA%\Programs\optionlens-mac\resources\mcp\optionlens-mcp.cmd"` (Settings shows the expanded path)
- **Claude Code** (Linux AppImage): `claude mcp add optionlens -- "$HOME/.local/bin/OptionLens.AppImage" --no-sandbox --ozone-platform=headless --mcp`
- **Claude Desktop**: add the JSON Settings shows to `claude_desktop_config.json` (`{"mcpServers": {"optionlens": {"command": …, "args": […]}}}`).
- **Any MCP client**: the stdio command Settings shows.

OptionLens must be running (the agent's tools then say "OptionLens is not running" until it is). About 60 tools in groups —
`symbol_*` (26: summary, chain, volatility, earnings, …), `report_list` / `report_run`, `market_overview`,
`compare_*`, `watchlist_*`, `alert_*`, `preset_*`, `notification_*`, `live_*`, `calc_*`, `backtest_*`, `portfolio_*` (only when allowed),
`system_status`, `source_questrade_status`, `get_current_view` and `describe_units` (units: percentages and IV are
fractions, option prices are per share). Long tables come in pages of 100 rows (`limit` / `offset`, `columns` to pick
report columns). Resources: `optionlens://current-view`, `optionlens://status`, `optionlens://conventions`,
`optionlens://symbol/{symbol}/summary`.

Security: agents with this access can read all your OptionLens data and edit watchlists/alerts/presets. Your
portfolio is separate: agents see it (tools `portfolio_positions`, `portfolio_risk`; account numbers masked) only
after you also turn on **Allow agents to see my portfolio** (off by default, and off again whenever agent access is
turned off). The MCP key
is separate from the app's per-launch token, lives only in `mcp-connection.json` in the data folder (readable by your
user only), is accepted from this computer only (loopback, never from a web page) and only for the agent tools;
internal endpoints (the IBKR bridge's ingest, the Questrade token, live-mode switching, agent-access settings) refuse
it. Turning access off revokes the key at once; turning it on again or **Regenerate key** makes a new one. Details:
[docs/INTEGRATION.md §10](docs/INTEGRATION.md#10-ai-agents-mcp).

## Notifications

The app checks your alerts' notifications every minute and shows each new one as a system notification
(macOS Notification Center, Windows notifications, or your Linux desktop's notifications via libnotify).
Click a notification to open its page in OptionLens. On Windows, notifications need the Start-menu shortcut
that the installer creates (the portable zip has none).

## Troubleshooting

- **"Starting the engine…" does not go away**: open **Settings → Engine** for the last error, or `main.log`
  (see [Where things are](#where-things-are)). **Restart engine** starts it again.
- **Linux: the AppImage does not start**: install FUSE (`sudo apt install fuse3`), or run it once with
  `--appimage-extract-and-run`. OptionLens needs glibc 2.28 or newer.
- **IBKR: not connected**: check that IB Gateway is running and logged in, that the API port is one of the
  ports above (or set it in Settings), and that `127.0.0.1` is a trusted IP. "Client id in use" means
  another API app uses the same client ID. The bridge tries the next 3 IDs; you can change the ID in Settings.
- **Prices but no options**: you have no OPRA subscription and delayed option data is not enabled for your
  account. Check the IBKR market data settings in the Client Portal.
- **Pacing violation** messages in the log can happen during large backfills. The bridge pauses and
  continues.

## Development

```
npm ci
(cd engine && uv sync)          # the engine's virtualenv (used by `npm start`)
npm start                       # build the renderer + main process, launch Electron
npm run dev                     # renderer only in a browser (vite; /api proxied to 127.0.0.1:8473)
npm run typecheck && npm test   # TypeScript (renderer + Electron) and vitest unit tests
npm run e2e                     # mocked Playwright smoke suite against `vite preview`
(cd engine && uv run pytest -q) # engine tests
```

`OPTIONLENS_PYTHON` and `OPTIONLENS_ENGINE_DIR` override the engine interpreter and directory in
development. Packaging (CI does this for every OS): `./scripts/bundle-engine.sh` (relocatable CPython 3.13
from python-build-standalone via uv, with the engine and its locked dependencies; on Windows run it in Git
Bash), then:

- macOS: `npm run dist:mac -- --arm64` (or `--x64`) builds `OptionLens.app`, and `./scripts/zip-mac-app.sh arm64`
  packs it with `ditto` into `release/OptionLens-<version>-arm64.zip`. `./scripts/verify-mac-app.sh` and
  `./scripts/verify-mac-zip.sh` are the checks CI runs on the app and on the extracted zip.
- Windows: `npm run dist:win` builds `release/OptionLens-<version>-win-x64-setup.exe` (NSIS, per user) and
  `release/OptionLens-<version>-win-x64.zip`.
- Linux: `npm run dist:linux` builds `release/OptionLens-<version>-linux-x64.AppImage`;
  `./scripts/check-linux-glibc.sh release/linux-unpacked` checks the glibc baseline (2.28).

`./scripts/smoke-bundled-engine.sh <bundled python>` starts the bundled engine as the app does and stops it
by closing its stdin (every OS); `e2e/packaged.spec.ts` (with `PACKAGED_APP`) drives the packaged app.
`scripts/test-install-sh.sh` and `scripts/test-install-ps1.ps1` test the installers. Releases: bump the
version in `package.json` and `engine/pyproject.toml`, push a `v<version>` tag; the CI workflow builds every
OS (a failed leg blocks the release) and attaches the macOS zips, the Windows setup.exe and zip and the Linux
AppImage to the GitHub Release.

### Layout

- `src/`: renderer (React/Vite). `src/api/types.ts` is the API JSON contract (the engine's contract
  tests read it).
- `electron/main`: engine sidecar, window, `app://` protocol and CSP, tray, IPC, notifications, settings.
  `electron/main/platform`: what differs per OS (launch at login, tray icons, notifications, menus, where the
  bundled Python is). `electron/main/updater`: the self-updater (GitHub release check, verified download,
  safe-window policy, rollback); `updater/installers` holds the per-OS install steps (macOS app swap, Windows
  NSIS installer, Linux AppImage swap).
- `electron/bridge`: IBKR bridge. `tws.ts` is the TWS contract. `ib-adapter.ts` implements it with
  [`@stoqey/ib`](https://github.com/stoqey/ib), a maintained TypeScript port of the official TWS API.
- `electron/preload`: the `window.optionlens` API.
- `engine/`: the Python engine (see `engine/README.md`). `docs/`: specification and contracts.
