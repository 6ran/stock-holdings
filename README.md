# My Holdings

A personal Android app for Unknown's Wealthsimple holdings: overview, charts, per-holding detail,
watch-outs, analyst views and related articles.

## How it works

- `positions.json` holds what you own: quantities, accounts, options, margin loan.
- **Update holdings data** (GitHub Actions) runs every weekday at 21:30 UTC (5:30 PM Toronto in summer,
  4:30 PM in winter). It prices everything with Yahoo Finance and rewrites `data.json`.
  It also runs whenever `positions.json` changes, and on demand from the Actions tab.
- **Build Android app** (GitHub Actions) builds `MyHoldings.apk` whenever the `android/` folder changes
  and publishes it on the `app-latest` release.
- The app reads `data.json` from this repo each time you open it, so data updates never need a reinstall.

## When your positions change

Edit `positions.json` on GitHub (pencil icon), change the quantity, account or option, and commit.
The data refreshes within a few minutes. Also update `positionsAsOf`, and `wealthsimple.totalCad`
and `accounts` if you want the Wealthsimple figures in the app to match.

## Notes

- The repo must be **public** so the app can read `data.json` without a password. Anyone with the
  link can see your holdings and values (no account numbers or personal details are stored).
- Prices are from Yahoo Finance. Option prices can be missing on quiet days; the last value is kept.
- The app is signed with a key stored in this repo (`android/app/holdings.keystore`) so updates install
  over the previous version. Only install APKs from your own repo's release page.
- Watch-outs and analyst views are information, not investment advice.
