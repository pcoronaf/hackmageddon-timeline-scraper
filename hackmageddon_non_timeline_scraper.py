#!/usr/bin/env python3
"""
Scrape non-timeline reports from Hackmageddon's Cyber Attacks Timelines category
and save one CSV per report.

Behavior
--------
1. Walk the category pages under:
   https://www.hackmageddon.com/category/security/cyber-attacks-timeline/
2. Collect article links published in that category that are NOT bi-weekly timeline posts
3. For each report page:
   - try to download a direct CSV link if one is exposed
   - otherwise extract the main data table and export it as CSV
4. Print a per-report status and a final summary

This script is compatible with Python 3.8+.
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_CATEGORY_URL = "https://www.hackmageddon.com/category/security/cyber-attacks-timeline/"
DEFAULT_OUTPUT_DIR = Path("hackmageddon_non_timeline_csv")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

TIMELINE_TITLE_PATTERN = re.compile(r"\bcyber attacks timeline\b", re.IGNORECASE)
CSV_HREF_PATTERN = re.compile(r"\.csv(?:$|[?#])", re.IGNORECASE)
CSV_TEXT_PATTERN = re.compile(r"\b(csv|download csv|export csv)\b", re.IGNORECASE)
CSV_ENDPOINT_PATTERN = re.compile(r"(?:export=csv|format=csv|type=csv|csv=1)", re.IGNORECASE)
LIKELY_REPORT_COLUMNS = {
    "id",
    "date",
    "date reported",
    "date occurred",
    "date discovered",
    "author",
    "target",
    "description",
    "attack",
    "target class",
    "attack class",
    "country",
    "link",
    "initial access",
    "records",
    "raw records",
    "vendor",
    "product",
    "cve",
    "technology",
    "ai used for",
}


class ScraperError(Exception):
    """Custom scraper error."""


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.hackmageddon.com/",
        }
    )
    return session


def polite_sleep(base_delay: float) -> None:
    time.sleep(base_delay + random.uniform(0, 0.4))


def fetch(session: requests.Session, url: str, timeout: int = 45) -> requests.Response:
    logging.debug("GET %s", url)
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def slugify(text: str, max_len: int = 160) -> str:
    cleaned = normalize_whitespace(text)
    cleaned = cleaned.replace("/", "-")
    cleaned = re.sub(r"[^\w\-\. ]+", "", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"[\s\-]+", "-", cleaned).strip("-._")
    return (cleaned or "report")[:max_len]


def category_page_url(page_number: int) -> str:
    if page_number <= 1:
        return BASE_CATEGORY_URL
    return urljoin(BASE_CATEGORY_URL, "page/{}/".format(page_number))


def is_timeline_title(title: str) -> bool:
    return bool(TIMELINE_TITLE_PATTERN.search(title or ""))


def extract_post_links(category_html: str, base_url: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(category_html, "html.parser")
    results = []  # type: List[Tuple[str, str]]
    seen = set()  # type: Set[str]

    for anchor in soup.select("h1 a, h2 a, h3 a, article a, a"):
        href = anchor.get("href")
        title = normalize_whitespace(anchor.get_text(" ", strip=True))
        if not href or not title:
            continue
        if title.lower().startswith("continue reading"):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        results.append((title, absolute))

    deduped = []  # type: List[Tuple[str, str]]
    seen_urls = set()  # type: Set[str]
    for title, url in results:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append((title, url))
    return deduped


def extract_non_timeline_posts(category_html: str, base_url: str) -> List[Tuple[str, str]]:
    posts = extract_post_links(category_html, base_url)
    filtered = []  # type: List[Tuple[str, str]]
    seen_urls = set()  # type: Set[str]
    for title, url in posts:
        if is_timeline_title(title):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        filtered.append((title, url))
    return filtered


def looks_like_report_table(df: pd.DataFrame) -> bool:
    normalized_cols = {normalize_whitespace(str(c)).lower() for c in df.columns}
    if len(normalized_cols) >= 3 and len(df) >= 1:
        overlap = normalized_cols & LIKELY_REPORT_COLUMNS
        if len(overlap) >= 2:
            return True
        if len(df) >= 5 and len(normalized_cols) >= 4:
            return True
    return False


def flatten_multilevel_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        flattened = []
        for col in df.columns:
            parts = []
            for part in col:
                part_str = normalize_whitespace(str(part))
                if part_str and part_str.lower() != "nan":
                    parts.append(part_str)
            flattened.append(normalize_whitespace(" ".join(parts)))
        df.columns = flattened
    else:
        df.columns = [normalize_whitespace(str(c)) for c in df.columns]
    return df


def extract_direct_csv_link(soup: BeautifulSoup, article_url: str) -> Optional[str]:
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        text = normalize_whitespace(anchor.get_text(" ", strip=True))
        title_attr = normalize_whitespace(anchor.get("title", ""))
        absolute = urljoin(article_url, href)
        if (
            CSV_HREF_PATTERN.search(absolute)
            or CSV_ENDPOINT_PATTERN.search(absolute)
            or CSV_TEXT_PATTERN.search(text)
            or CSV_TEXT_PATTERN.search(title_attr)
        ):
            return absolute
    return None


def extract_title(soup: BeautifulSoup) -> str:
    selectors = ["h1", "header h1", "main h1", "article h1"]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            title = normalize_whitespace(node.get_text(" ", strip=True))
            if title:
                return title
    title_tag = soup.find("title")
    if title_tag:
        title = normalize_whitespace(title_tag.get_text(" ", strip=True))
        if title:
            return title.split("–")[0].strip()
    raise ScraperError("Could not extract article title")


def dataframe_from_bs4_table(table) -> pd.DataFrame:
    rows = []
    max_cols = 0
    for tr in table.find_all("tr"):
        row = []
        cells = tr.find_all(["th", "td"])
        for cell in cells:
            text = normalize_whitespace(cell.get_text(" ", strip=True))
            colspan = 1
            try:
                colspan = int(cell.get("colspan", 1))
            except (TypeError, ValueError):
                colspan = 1
            row.extend([text] * max(1, colspan))
        if row:
            rows.append(row)
            max_cols = max(max_cols, len(row))

    if not rows or max_cols == 0:
        raise ScraperError("Encountered an empty HTML table")

    padded_rows = []
    for row in rows:
        padded_rows.append(row + [""] * (max_cols - len(row)))

    header = padded_rows[0]
    body = padded_rows[1:] if len(padded_rows) > 1 else []
    df = pd.DataFrame(body, columns=header)
    return flatten_multilevel_columns(df)


def select_best_table(tables: Sequence[pd.DataFrame]) -> pd.DataFrame:
    if not tables:
        raise ScraperError("No table candidates found")

    candidates = []  # type: List[pd.DataFrame]
    for df in tables:
        if df is None:
            continue
        df = flatten_multilevel_columns(df)
        if looks_like_report_table(df):
            candidates.append(df)

    if not candidates:
        candidates = [flatten_multilevel_columns(df.copy()) for df in tables if df is not None]

    candidates.sort(key=lambda d: (len(d), len(d.columns)), reverse=True)
    best = candidates[0].copy()
    best = best.dropna(how="all").reset_index(drop=True)

    if not best.empty:
        header_set = [normalize_whitespace(str(c)).lower() for c in best.columns.tolist()]

        def is_header_like(row: pd.Series) -> bool:
            row_vals = [normalize_whitespace(str(v)).lower() for v in row.tolist()]
            overlap = sum(1 for v in row_vals if v in header_set)
            return overlap >= max(2, len(header_set) // 3)

        mask = best.apply(is_header_like, axis=1)
        best = best.loc[~mask].reset_index(drop=True)

    return best


def extract_report_table(article_html: str) -> pd.DataFrame:
    # First choice: pandas+lxml to avoid the BeautifulSoup/html5lib SoupStrainer bug.
    try:
        tables = pd.read_html(article_html, flavor="lxml")
    except Exception:
        tables = []

    if tables:
        try:
            return select_best_table(tables)
        except Exception:
            pass

    # Second choice: manual parsing of every HTML table.
    soup = BeautifulSoup(article_html, "html.parser")
    manual_tables = []  # type: List[pd.DataFrame]
    for table in soup.find_all("table"):
        try:
            df = dataframe_from_bs4_table(table)
            manual_tables.append(df)
        except Exception:
            continue

    if manual_tables:
        return select_best_table(manual_tables)

    raise ScraperError("No report table found in article HTML")


def safe_extension_from_url(url: str, default: str = ".csv") -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    return ext or default


def download_file(session: requests.Session, url: str, destination: Path) -> None:
    with session.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    handle.write(chunk)


def save_dataframe_csv(df: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(destination, index=False, encoding="utf-8-sig")


def process_article(
    session: requests.Session,
    title_hint: str,
    article_url: str,
    output_dir: Path,
    direct_only: bool,
    dry_run: bool,
) -> Tuple[str, str, str, str]:
    response = fetch(session, article_url)
    soup = BeautifulSoup(response.text, "html.parser")
    real_title = extract_title(soup)
    file_stem = slugify(real_title or title_hint)

    csv_url = extract_direct_csv_link(soup, article_url)
    if csv_url:
        ext = safe_extension_from_url(csv_url, default=".csv")
        destination = output_dir / (file_stem + ext)
        if not dry_run:
            download_file(session, csv_url, destination)
        return real_title, article_url, "downloaded", destination.name

    if direct_only:
        return real_title, article_url, "skipped-no-direct-csv", ""

    df = extract_report_table(response.text)
    destination = output_dir / (file_stem + ".csv")
    if not dry_run:
        save_dataframe_csv(df, destination)
    return real_title, article_url, "built", destination.name


def collect_all_non_timeline_posts(
    session: requests.Session,
    start_page: int,
    end_page: Optional[int],
    max_pages: Optional[int],
    delay: float,
) -> List[Tuple[str, str]]:
    posts = []  # type: List[Tuple[str, str]]
    seen_urls = set()  # type: Set[str]
    current_page = start_page
    pages_visited = 0

    while True:
        if end_page is not None and current_page > end_page:
            break
        if max_pages is not None and pages_visited >= max_pages:
            break

        url = category_page_url(current_page)
        logging.info("Scanning category page %s -> %s", current_page, url)
        response = fetch(session, url)
        page_posts = extract_non_timeline_posts(response.text, url)

        if not page_posts:
            logging.info("No non-timeline reports found on page %s. Stopping.", current_page)
            break

        new_count = 0
        for title, post_url in page_posts:
            if post_url in seen_urls:
                continue
            posts.append((title, post_url))
            seen_urls.add(post_url)
            new_count += 1

        logging.info(
            "Found %s non-timeline reports on page %s (%s new)",
            len(page_posts),
            current_page,
            new_count,
        )
        pages_visited += 1
        current_page += 1
        polite_sleep(delay)

    return posts


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download or export Hackmageddon non-timeline report CSVs"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to store CSV files (default: {})".format(DEFAULT_OUTPUT_DIR),
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="Category page to start from (default: 1)",
    )
    parser.add_argument(
        "--end-page",
        type=int,
        default=None,
        help="Category page to stop at (inclusive)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum number of category pages to scan",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.2,
        help="Base delay between requests in seconds (default: 1.2)",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List the non-timeline reports found and exit",
    )
    parser.add_argument(
        "--direct-only",
        action="store_true",
        help="Only download direct CSV links; do not build CSVs from HTML tables",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover reports and test extraction logic without writing files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    if args.start_page < 1:
        logging.error("--start-page must be >= 1")
        return 2
    if args.end_page is not None and args.end_page < args.start_page:
        logging.error("--end-page must be >= --start-page")
        return 2
    if args.max_pages is not None and args.max_pages < 1:
        logging.error("--max-pages must be >= 1")
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    session = build_session()

    try:
        posts = collect_all_non_timeline_posts(
            session=session,
            start_page=args.start_page,
            end_page=args.end_page,
            max_pages=args.max_pages,
            delay=args.delay,
        )

        if not posts:
            logging.warning("No non-timeline reports collected.")
            return 1

        print("")
        print("Found {} non-timeline reports:".format(len(posts)))
        print("")
        for idx, (title, url) in enumerate(posts, start=1):
            print("{:03d}. {}".format(idx, title))
            print("     {}".format(url))

        if args.list_only:
            return 0

        print("")
        print("Processing reports...")
        print("")

        success = 0
        failures = 0
        downloaded = 0
        built = 0
        skipped = 0

        for idx, (title, url) in enumerate(posts, start=1):
            logging.info("[%s/%s] Processing %s", idx, len(posts), title)
            try:
                real_title, article_url, action, filename = process_article(
                    session=session,
                    title_hint=title,
                    article_url=url,
                    output_dir=args.output,
                    direct_only=args.direct_only,
                    dry_run=args.dry_run,
                )
                if action == "downloaded":
                    downloaded += 1
                    success += 1
                    print("OK   | downloaded | {} | {}".format(real_title, filename))
                elif action == "built":
                    built += 1
                    success += 1
                    print("OK   | built      | {} | {}".format(real_title, filename))
                elif action == "skipped-no-direct-csv":
                    skipped += 1
                    print("SKIP | no-direct-csv | {}".format(real_title))
                else:
                    success += 1
                    print("OK   | {} | {} | {}".format(action, real_title, filename))
            except Exception as exc:
                failures += 1
                logging.exception("FAILED | %s | %s", title, url)
                print("FAIL | {} | {}".format(title, exc))
            polite_sleep(args.delay)

        print("")
        print("Summary")
        print("-------")
        print("success: {}".format(success))
        print("fails: {}".format(failures))
        print("downloaded: {}".format(downloaded))
        print("built: {}".format(built))
        print("skipped: {}".format(skipped))
        print("output: {}".format(args.output.resolve()))

        return 0 if success > 0 else 1

    except requests.HTTPError as exc:
        logging.error("HTTP error: %s", exc)
        return 1
    except requests.RequestException as exc:
        logging.error("Network error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
