# TrustMRR momentum screen

Finds young startups in a revenue band whose revenue has grown steadily for several months, ranks them, and groups them by category to surface emerging themes.

Defaults: launched in the last 12 months, $500–$5,000 MRR, at least 2 consecutive non-declining months, scored over the last 4 complete months.

## Run

Needs Python 3.9+ (standard library only) and a TrustMRR API key from the developer dashboard.

```bash
export TRUSTMRR_API_KEY=tmrr_...
python momentum_screen.py --inspect   # 1. check what the API returns
python momentum_screen.py             # 2. run the screen
```

Outputs go to `output/`:
- `ranked_startups.csv`: every startup that passed, ranked by momentum score
- `category_summary.csv`: categories ranked by number of steady (non-spiky) growers
- `raw_startups.json`: the full pull, to rerun offline with `--from-file output/raw_startups.json`
- `snapshots/YYYY-MM-DD.json`: one per run, used to build revenue history if the API doesn't return it

## First run: check the fields

The TrustMRR response format couldn't be verified when this was written. `--inspect` prints one raw record and what the script detected for each field. If a field shows `None`, add the right key name to `FIELDS` at the top of the script. If dollar amounts look 100x too large or small, flip `MONEY_IN_CENTS`.

If the API has no month-by-month revenue, the screen can't score growth on the first run. Run it once a month: each run saves a snapshot, and after 3 months there's enough history.

## Scoring

For each startup, the last N complete months of revenue are compared month over month:
- **growth_streak**: consecutive recent months without a drop larger than `--dip-tolerance` (5%)
- **positive_month_share**: share of months that grew
- **median_mom_growth**: typical monthly growth rate
- **spiky**: one month delivered more than `--spike-share` (60%) of the total gain; penalised, not removed

`momentum_score = streak*10 + positive_share*20 + min(median_growth, 100%)*50 − 25 if spiky`. Consistency counts more than raw growth.

## Rate limits

The API allows 20 requests/minute, so the script waits ~3s between calls. Details are fetched only for startups that pass the revenue and age filters, and only when the list response has no revenue history. Use `--no-details` to skip them.

See `python momentum_screen.py --help` for all options.
