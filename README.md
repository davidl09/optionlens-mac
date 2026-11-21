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

OptionLens needs macOS 12.3 (Monterey) or later. In Terminal:

```
curl -fsSL https://raw.githubusercontent.com/davidl09/optionlens-mac/main/install.sh | bash
```

The [installer](install.sh) downloads the latest release for your Mac (Apple silicon or Intel) from
[GitHub Releases](https://github.com/davidl09/optionlens-mac/releases), checks its SHA-256 against the
digest GitHub publishes for the file, checks the app's bundle id, version and signature, quits a running
OptionLens, keeps the installed app as a backup (in `~/Library/Application Support/OptionLens/backups`,
the newest 2), moves the new app to **Applications** and opens it. It does not use `sudo`; if
`/Applications` is not writable for your user it stops and says so.

**Manual install** (the same steps by hand): download `OptionLens-<version>-arm64.zip` (Apple silicon) or
`OptionLens-<version>-x64.zip` (Intel) from the Releases page, check it with `shasum -a 256` against the
SHA-256 shown for the file on the release page, and extract it with `ditto -x -k OptionLens-<version>-<arch>.zip .`
(or double-click it in Finder). Move **OptionLens** to **Applications**. The app is ad-hoc signed, not
signed with an Apple Developer ID, so a browser download is quarantined: remove the flag once with
`xattr -dr com.apple.quarantine /Applications/OptionLens.app` (or right-click → **Open**). Files that the
installer or the app itself download are not quarantined, so they open without that step.

## Updating

OptionLens updates itself: **Settings → Updates** shows the installed and latest versions and the release
notes, with **Check now** and **Download and install**. It checks GitHub at launch and every 6 hours; when
a new version is out, the menu-bar menu shows **Update available (x.y.z)…** and you get one notification
for it. An update is downloaded over HTTPS, checked against the SHA-256 digest that GitHub publishes for
the release file (no digest, no install), extracted, and its bundle id, version and signature are checked.
OptionLens then quits (the engine stops normally) and a small helper swaps the app, keeps the previous one
as a backup and opens the new version.

- **Install updates automatically** (off by default) downloads new versions in the background and installs
  them when the Mac has been idle for 5 minutes with no OptionLens window focused, or when you click
  **Restart now** on the "ready" notification or in the menu-bar menu. Never while the engine collects the
  closing data (16:15–16:50 New York time on weekdays) or while a data import runs.
- Before a new version's engine first opens your database, the database is copied to
  `~/Library/Application Support/OptionLens/backups/db-<previous version>-<time>.duckdb` (the newest 2 are kept).
- If a new version's engine does not become healthy within 60 s, or the app crashes twice in its first
  2 minutes, OptionLens restores the previous version from its backup, opens it, tells you, and does not
  install that version automatically again. It tries this once; the updater log is
  `~/Library/Logs/OptionLens/updater.log`.
- The app must run from a writable folder such as **Applications**. When macOS runs it from a read-only
  place (a disk image, or a randomized "translocated" copy of a quarantined download), Settings says
  **Move OptionLens to the Applications folder first**.
- The update source is a GitHub repository (`owner/repo`, default `davidl09/optionlens-mac`); only assets
  of that repository's releases are downloaded. Drafts and prereleases are ignored, and older versions are
  never installed.

**From 0.1.0–0.1.2** (dmg builds without the updater): run the one-line installer above once. Your data
and settings stay where they are.

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

All historical requests are for daily bars, which IBKR does not hard-limit like small bars: they are
sent about 1 s apart, and after a real pacing violation the bridge pauses (30 s, growing) and spaces
requests further apart for a while. When IBKR says your account has no market-data permission for some
data (for example option bars without OPRA), the bridge skips that symbol and kind for the day and the
status lists it. Backfills can be resumed: after a restart or a reconnect, the bridge continues where it
stopped. You can cancel bridge work from the menu-bar menu or from Settings.

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
development. Packaging (CI does this on macOS for arm64 and x64): `./scripts/bundle-engine.sh`
(relocatable CPython 3.13 from python-build-standalone via uv, with the engine and its locked
dependencies), then `npm run dist:mac -- --arm64` (or `--x64`) builds `OptionLens.app` and
`./scripts/zip-mac-app.sh arm64` packs it with `ditto` into `release/OptionLens-<version>-arm64.zip`.
`./scripts/verify-mac-app.sh` and `./scripts/verify-mac-zip.sh` are the checks CI runs on the app and on
the extracted zip. Releases: bump the version in `package.json` and `engine/pyproject.toml`, push a
`v<version>` tag; the CI workflow builds both zips and attaches them to the GitHub Release.

### Layout

- `src/`: renderer (React/Vite). `src/api/types.ts` is the API JSON contract (the engine's contract
  tests read it).
- `electron/main`: engine sidecar, window, `app://` protocol and CSP, tray, IPC, notifications, settings.
  `electron/main/updater`: the self-updater (GitHub release check, verified download, staging, the swap
  helper, rollback, safe-window policy).
- `electron/bridge`: IBKR bridge. `tws.ts` is the TWS contract. `ib-adapter.ts` implements it with
  [`@stoqey/ib`](https://github.com/stoqey/ib), a maintained TypeScript port of the official TWS API.
- `electron/preload`: the `window.optionlens` API.
- `engine/`: the Python engine (see `engine/README.md`). `docs/`: specification and contracts.
