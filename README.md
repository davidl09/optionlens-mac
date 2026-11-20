# OptionLens for Mac

OptionLens is a self-hosted options and stock analytics workstation for one user. It is one Mac app:

- **The app** (Electron): the analytics UI, served from `app://optionlens`.
- **The engine** (Python, `engine/`): FastAPI + DuckDB + a scheduler. The app starts it as a local
  sidecar on `127.0.0.1` with a random per-launch token and stops it when you quit. Your data stays in
  `~/Library/Application Support/OptionLens/data`.
- **The IBKR bridge** (in the app): connects to your running **IB Gateway** (or TWS) and pushes option
  chains, universe-wide option statistics, IV/HV history and VIX-family data into the engine.

OptionLens keeps running in the menu bar when you close its window, so collection and the bridge
continue. Use **Quit OptionLens** in the menu-bar menu to stop everything.

## Install

1. Download the dmg for your Mac from the
   [Releases](https://github.com/davidl09/optionlens-mac/releases) page:
   `OptionLens-<version>-arm64.dmg` for Apple silicon (M1 or later), `-x64` for Intel Macs.
2. Open the dmg and drag **OptionLens** to **Applications**.
3. The app is not signed with an Apple Developer ID. The first time, **right-click OptionLens in
   Applications → Open**, then click **Open** in the warning dialog. On macOS 15 or later, if no Open
   button appears, open **System Settings → Privacy & Security** and click **Open Anyway**.
   If macOS says the app "is damaged", remove the download quarantine flag and open it again:
   ```
   xattr -dr com.apple.quarantine /Applications/OptionLens.app
   ```

### Importing data from an old OptionLens server (one time)

If you ran OptionLens as a separate server before, copy its `optionlens.duckdb` (from the server's
`OPTIONLENS_DATA_DIR`, with the server stopped) to the Mac. Then open **Settings → Engine → Import data…**
and pick the file. The engine stops, the file is copied into the app's data folder (the current
database is kept as a `.bak-<time>` backup) and the engine starts again.

## Connect IB Gateway (IBKR)

The bridge talks to the IBKR API of IB Gateway or TWS on this Mac. It finds it automatically on the
default ports, in this order: **4001** (IB Gateway, live), **7496** (TWS, live), **4002** (IB Gateway, paper),
**7497** (TWS, paper). You can set a different port in **Settings → Bridge connection**. The menu-bar
menu and Settings show which one is connected and whether the account is live or paper.

In IB Gateway, open **Configure → Settings → API → Settings**:

- Keep the default **Socket port** (4001 live / 4002 paper).
- Turn on **Read-Only API** (recommended: the bridge only reads data and never places orders).
- Under **Trusted IPs**, add `127.0.0.1`.
- In TWS, also turn on **Enable ActiveX and Socket Clients** (Global Configuration → API → Settings).

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

Historical requests follow IBKR's pacing rules: at least 10 s apart, at most 55 in any 10 minutes, and
a growing pause after a pacing violation. Backfills can be resumed: after a restart, the bridge
continues where it stopped. You can cancel bridge work from the menu-bar menu or from Settings.

## Notifications

The app checks your alerts' notifications every minute and shows each new one as a macOS notification.
Click a notification to open its page in OptionLens.

## Troubleshooting

- **"Starting the engine…" does not go away**: open **Settings → Engine** for the last error, or the log
  at `~/Library/Logs/OptionLens/main.log`. **Restart engine** starts it again.
- **IBKR: not connected**: check that IB Gateway is running and logged in, that the API port is one of the
  ports above (or set it in Settings), and that `127.0.0.1` is a trusted IP. "Client id in use" means
  another API app uses the same client ID. The bridge tries the next 3 IDs; you can change the ID in Settings.
- **Prices but no options**: you have no OPRA subscription and delayed option data is not enabled for your
  account. Check the IBKR market data settings in the Client Portal.
- **Pacing violation** messages in the log are expected during large backfills. The bridge pauses and
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
development. Packaging (CI does this on macOS for arm64 and x64): `./scripts/bundle-engine.sh`
(relocatable CPython 3.13 from python-build-standalone via uv, with the engine and its locked
dependencies), then `npm run dist:mac -- --arm64` (or `--x64`). Releases: push a `v*` tag; the CI workflow
builds both dmgs and attaches them to the GitHub Release.

### Layout

- `src/`: renderer (React/Vite). `src/api/types.ts` is the API JSON contract (the engine's contract
  tests read it).
- `electron/main`: engine sidecar, window, `app://` protocol and CSP, tray, IPC, notifications, settings.
- `electron/bridge`: IBKR bridge. `tws.ts` is the TWS contract. `ib-adapter.ts` implements it with
  [`@stoqey/ib`](https://github.com/stoqey/ib), a maintained TypeScript port of the official TWS API.
- `electron/preload`: the `window.optionlens` API.
- `engine/`: the Python engine (see `engine/README.md`). `docs/`: specification and contracts.
