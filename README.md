# Gold Watch: setup guide for beginners

This turns the tracker into a real website that refreshes itself every 6 hours, for free.

## What you will have

- A website at `https://YOUR-USERNAME.github.io/gold-watch/`
- A robot (GitHub Actions) that runs 4 times a day, pulls fresh data, and republishes the site
- One file, `manual.json`, that you edit when you want to change the cycle phase, the China/ETF notes, or add events

## What it costs

Nothing, as long as the repository is **public**. GitHub Pages hosts public repositories on the free plan, and GitHub's documentation says standard runners are free for public repositories. The FRED data key is also free.

Because the repository is public, anyone can read it. Do not put anything private in it. The FRED key goes in GitHub's secret storage, never in a file.

## One-time setup (about 20 minutes, works from a computer)

1. **Make a GitHub account** at github.com (free).
2. **Get a free FRED key.** Make an account at fred.stlouisfed.org, open My Account, then API Keys, then request a key. Copy it.
3. **Create the repository.** On GitHub press New repository. Name it `gold-watch`. Choose **Public**. Create it.
4. **Upload the files.** Press Add file, then Upload files, and drag in everything from this folder: `index.html`, `data.json`, `manual.json`, `README.md`, the `scripts` folder and the `.github` folder. Commit the upload.
   - If the `.github` folder will not upload, press Add file, then Create new file, type `.github/workflows/update.yml` as the name (typing the slashes makes the folders), paste in the contents of `update.yml`, and commit.
5. **Save the key.** Open Settings, then Secrets and variables, then Actions, then New repository secret. Name: `FRED_API_KEY`. Value: your key.
6. **Turn on Pages.** Open Settings, then Pages. Under Source choose **GitHub Actions**.
7. **Start the first run.** Open the Actions tab, choose Update Gold Watch, press Run workflow. Wait about two minutes for a green tick.
8. Open `https://YOUR-USERNAME.github.io/gold-watch/`.

After this it runs by itself. The page also re-reads its data every 10 minutes while it is open.

## What updates itself and what does not

| Part | How it updates |
|---|---|
| Gold price, 20 and 50 day averages | Automatic (Stooq) |
| Real yield and dollar signals | Automatic (FRED), using the written rules below |
| Latest headlines | Automatic (Google News, Federal Reserve feed) |
| Fed rate path, China and central banks, ETF flows, positioning, stock stress | By hand in `manual.json` |
| Cycle phase and event log | By hand in `manual.json` |

There is no free automatic feed for China's gold reserves or ETF flows, so those stay manual. The page marks any manual note older than 21 days as stale.

## The rules behind the automatic signals

- **Real yield:** near its 20-day high is a headwind. Down 0.30 points in 20 days is a tailwind. Off the high by 0.10 and still falling over 5 days is turning.
- **Dollar index:** within 0.3% of its 20-day high is a headwind. Down 2% from the high is a tailwind. Down 0.8% and still falling is turning.
- **Gold price:** below both the 20-day and 50-day averages (with the 20 under the 50) is a headwind. Above both is a tailwind. Back above the 20-day but still under the 50-day is turning.

You can change the numbers in `scripts/update.py`. The rules are simple on purpose. They describe the trend, they do not predict it.

## Editing by hand

Open `manual.json` on GitHub, press the pencil icon, change the text, and commit. Keep the quote marks and commas as they are. If you break the format, the next run shows an error in the Actions tab, and the site keeps showing the last good data.

## If something goes wrong

- **The notice says some sources did not answer:** open the Actions tab, open the latest run, and read the "Refresh data" step. The message names the source.
- **FRED sources fail:** the `FRED_API_KEY` secret is missing or mistyped.
- **Stooq fails:** the free price feed is occasionally down or rate limited. The page keeps working and shows the last known price signal.
- **The page stops refreshing after a long quiet spell:** GitHub can pause scheduled jobs in a repository with no commits for 60 days. This setup saves `data.json` back to the repository on every run, which should count as activity. If it ever pauses, press Run workflow once.

## Not financial advice

This is a learning tool. It reads public data and applies simple rules.
