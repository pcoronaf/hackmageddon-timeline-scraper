# Hackmageddon Timeline Scraper

Scrapes the Hackmageddon **Cyber Attacks Timeline** category, lists the timeline posts it finds, and then for each timeline:

- **downloads** a CSV if the article exposes a direct CSV link, or
- **builds** a CSV from the HTML table if no direct CSV is available.

It is compatible with **Python 3.8+** and includes a fallback parser to avoid the common `SoupStrainer` / `pandas.read_html()` compatibility issue seen on some stacks.

## Features

- scans Hackmageddon timeline category pages
- lists all found timeline posts before processing
- supports `--list-only` to enumerate timelines without downloading
- supports `--dry-run` to test extraction without writing files
- reports for each timeline whether it was `downloaded` or `built`
- prints a final summary with:
  - `success`
  - `fails`
  - `downloaded`
  - `built`
  - `output`

## Files

- `hackmageddon_timeline_scraper.py` — main scraper
- `requirements.txt` — Python dependencies
- `archive/` — previous working iterations kept for reference
- `GITHUB_PUSH_COMMANDS.md` — commands to publish this as a new GitHub project

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Run the scraper and save results to the default output folder `hackmageddon_csv`:

```bash
python3 hackmageddon_timeline_scraper.py
```

Only list found timelines:

```bash
python3 hackmageddon_timeline_scraper.py --list-only
```

Dry run without writing files:

```bash
python3 hackmageddon_timeline_scraper.py --dry-run
```

Choose a custom output folder:

```bash
python3 hackmageddon_timeline_scraper.py --output downloads/hackmageddon
```

Limit scanned category pages:

```bash
python3 hackmageddon_timeline_scraper.py --max-pages 5
```

Scan a page range:

```bash
python3 hackmageddon_timeline_scraper.py --start-page 2 --end-page 8
```

## Example output

```text
Found 24 timeline posts:

001. 1-15 March 2026 Cyber Attacks Timeline
     https://example.com/post-1
002. 16-31 March 2026 Cyber Attacks Timeline
     https://example.com/post-2

OK   | downloaded | 1-15 March 2026 Cyber Attacks Timeline | 1-15-March-2026-Cyber-Attacks-Timeline.csv
OK   | built | 16-31 March 2026 Cyber Attacks Timeline | 16-31-March-2026-Cyber-Attacks-Timeline.csv
FAIL | 1-15 April 2026 Cyber Attacks Timeline | No timeline table found in article HTML

Summary
-------
success: 24
fails: 1
downloaded: 8
built: 16
output: /absolute/path/hackmageddon_csv
```

## Notes

- The script trusts the **visible article title** rather than the URL slug because some slugs do not perfectly match the title/date shown on the page.
- CSV files are written with UTF-8 BOM (`utf-8-sig`) so they open cleanly in Excel on Windows.
- Keep a polite delay between requests.
- A direct GitHub upload was not performed from this environment because no GitHub account access or GitHub connector is available here. The repository is prepared so you can push it directly.
