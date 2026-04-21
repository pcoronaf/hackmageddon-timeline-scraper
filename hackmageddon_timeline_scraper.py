#!/usr/bin/env python3
"""
Scrape Hackmageddon Cyber Attacks Timeline posts and save one CSV per timeline.

What it does
------------
1. Walks the category pages under:
   https://www.hackmageddon.com/category/security/cyber-attacks-timeline/
2. Collects article links whose title contains "Cyber Attacks Timeline"
3. Lists all found timeline posts
4. For each article, first looks for a direct CSV download link
5. If no public CSV link is found, extracts the main timeline table from the HTML
   and writes its own CSV export

Usage
-----
python hackmageddon_timeline_scraper.py
python hackmageddon_timeline_scraper.py --list-only
python hackmageddon_timeline_scraper.py --output data/hackmageddon
python hackmageddon_timeline_scraper.py --max-pages 5 --delay 1.5
python hackmageddon_timeline_scraper.py --start-page 2 --end-page 4
python hackmageddon_timeline_scraper.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_CATEGORY_URL = "https://www.hackmageddon.com/category/security/cyber-attacks-timeline/"
DEFAULT_OUTPUT_DIR = Path("hackmageddon_csv")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

TITLE_PATTERN = re.compile(r"\bCyber Attacks Timeline\b", re.IGNORECASE)
CSV_HREF_PATTERN = re.compile(r"\.csv(?:$|[?#])", re.IGNORECASE)
CSV_TEXT_PATTERN = re.compile(r"\b(csv|download csv|export csv)\b", re.IGNORECASE)
LIKELY_TIMELINE_COLUMNS = {
    "id",
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


def fetch(session: requests.Session, url: str, timeout: int = 30) -> requests.Response:
    logging.debug("GET %s", url)
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def slugify(text: str, max_len: int = 140) -> str:
    cleaned = normalize_whitespace(text)
    cleaned = cleaned.replace("/", "-")
    cleaned = re.sub(r"[^\w\-\. ]+", "", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"[\s\-]+", "-", cleaned).strip("-._")
    if not cleaned:
        cleaned = "timeline"
    return cleaned[:max_len]


def category_page_url(page_number: int) -> str:
    if page_number <= 1:
        return BASE_CATEGORY_URL
    return urljoin(BASE_CATEGORY_URL, "page/{}/".format(page_number))


def extract_post_links(category_html: str, base_url: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(category_html, "html.parser")
    results = []  # type: List[Tuple[str, str]]
    seen = set()  # type: Set[str]

    for anchor in soup.select("h1 a, h2 a, h3 a, article a, a"):
        href = anchor.get("href")
        title = normalize_whitespace(anchor.get_text(" ", strip=True))
        if not href or not title:
            continue
        if not TITLE_PATTERN.search(title):
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


def looks_like_timeline_table(df: pd.DataFrame) -> bool:
    normalized_cols = {normalize_whitespace(str(c)).lower() for c in df.columns}
    overlap = normalized_cols & LIKELY_TIMELINE_COLUMNS
    return len(overlap) >= 6


def flatten_multilevel_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            normalize_whitespace(" ".join(str(part) for part in col if str(part) != "nan"))
            for col in df.columns
        ]
    else:
        df.columns = [normalize_whitespace(str(c)) for c in df.columns]
    return df


def ensure_unique_headers(headers: Sequence[str]) -> List[str]:
    result = []
    counts = {}  # type: dict
    for idx, header in enumerate(headers):
        value = normalize_whitespace(header)
        if not value:
            value = "column_{}".format(idx + 1)
        if value not in counts:
            counts[value] = 0
            result.append(value)
        else:
            counts[value] += 1
            result.append("{}_{}".format(value, counts[value] + 1))
    return result


def normalize_row_length(values: Sequence[str], width: int) -> List[str]:
    row = list(values[:width])
    if len(row) < width:
        row.extend([""] * (width - len(row)))
    return row


def extract_tables_with_pandas(article_html: str) -> List[pd.DataFrame]:
    """
    Prefer the lxml parser to avoid BeautifulSoup parser bugs seen on some older
    pandas/bs4 combinations that raise:
    AttributeError: 'SoupStrainer' object has no attribute 'name'
    """
    tables = []  # type: List[pd.DataFrame]
    try:
        tables = pd.read_html(article_html, flavor="lxml")
    except (ValueError, ImportError, AttributeError, TypeError):
        tables = []
    normalized = []
    for df in tables:
        df = flatten_multilevel_columns(df)
        normalized.append(df)
    return normalized


def extract_tables_manually(article_html: str) -> List[pd.DataFrame]:
    soup = BeautifulSoup(article_html, "html.parser")
    dataframes = []  # type: List[pd.DataFrame]

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        parsed_rows = []  # type: List[Tuple[List[str], bool]]
        max_cols = 0

        for row in rows:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            values = [normalize_whitespace(cell.get_text(" ", strip=True)) for cell in cells]
            if not any(values):
                continue
            is_header = any(cell.name == "th" for cell in cells)
            parsed_rows.append((values, is_header))
            max_cols = max(max_cols, len(values))

        if not parsed_rows or max_cols < 4:
            continue

        header_index = None
        for idx, (_, is_header) in enumerate(parsed_rows):
            if is_header:
                header_index = idx
                break
        if header_index is None:
            header_index = 0

        header = ensure_unique_headers(normalize_row_length(parsed_rows[header_index][0], max_cols))
        body_rows = []

        for idx, (values, _) in enumerate(parsed_rows):
            if idx == header_index:
                continue
            body_rows.append(normalize_row_length(values, max_cols))

        if not body_rows:
            continue

        df = pd.DataFrame(body_rows, columns=header)
        df = flatten_multilevel_columns(df)
        dataframes.append(df)

    return dataframes


def extract_direct_csv_link(soup: BeautifulSoup, article_url: str) -> Optional[str]:
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        text = normalize_whitespace(anchor.get_text(" ", strip=True))
        absolute = urljoin(article_url, href)
        if CSV_HREF_PATTERN.search(absolute) or CSV_TEXT_PATTERN.search(text):
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
        text = normalize_whitespace(title_tag.get_text(" ", strip=True))
        if text:
            return text.split("–")[0].strip()
    raise ScraperError("Could not extract article title")


def cleanup_candidate_table(df: pd.DataFrame) -> pd.DataFrame:
    best = df.copy()

    if len(best) > 0:
        first_row_values = {normalize_whitespace(str(v)).lower() for v in best.iloc[0].tolist()}
        col_values = {normalize_whitespace(str(c)).lower() for c in best.columns.tolist()}
        if len(first_row_values & col_values) >= max(3, len(col_values) // 3):
            best = best.iloc[1:].reset_index(drop=True)

    best = best.replace(r"^\s*$", pd.NA, regex=True)
    best = best.dropna(how="all").reset_index(drop=True)
    header_set = [normalize_whitespace(str(c)).lower() for c in best.columns.tolist()]

    def is_header_like(row: pd.Series) -> bool:
        row_vals = [normalize_whitespace(str(v)).lower() for v in row.tolist()]
        overlap = sum(1 for v in row_vals if v in header_set)
        return overlap >= max(3, len(header_set) // 3)

    if not best.empty:
        mask = best.apply(is_header_like, axis=1)
        best = best.loc[~mask].reset_index(drop=True)

    return best


def extract_timeline_table(article_html: str) -> pd.DataFrame:
    candidates = []  # type: List[pd.DataFrame]

    for df in extract_tables_with_pandas(article_html):
        if looks_like_timeline_table(df):
            candidates.append(cleanup_candidate_table(df))

    if not candidates:
        for df in extract_tables_manually(article_html):
            if looks_like_timeline_table(df):
                candidates.append(cleanup_candidate_table(df))

    if not candidates:
        raise ScraperError("No timeline table found in article HTML")

    candidates.sort(key=lambda d: (len(d), len(d.columns)), reverse=True)
    return candidates[0].copy()


def safe_extension_from_url(url: str, default: str = ".csv") -> str:
    path = urlparse(url).path
    ext = Path(path).suffix.lower()
    if ext:
        return ext
    return default


def download_file(session: requests.Session, url: str, destination: Path) -> None:
    with session.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as file_handle:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    file_handle.write(chunk)


def save_dataframe_csv(df: pd.DataFrame, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(destination, index=False, encoding="utf-8-sig")


def process_article(
    session: requests.Session,
    title_hint: str,
    article_url: str,
    output_dir: Path,
    dry_run: bool = False,
) -> Tuple[str, str, str, str]:
    response = fetch(session, article_url)
    soup = BeautifulSoup(response.text, "html.parser")
    real_title = extract_title(soup)
    file_stem = slugify(real_title or title_hint)

    csv_url = extract_direct_csv_link(soup, article_url)
    if csv_url:
        ext = safe_extension_from_url(csv_url, default=".csv")
        destination = output_dir / "{}.{}".format(file_stem, ext.lstrip("."))
        if not dry_run:
            download_file(session, csv_url, destination)
        return real_title, article_url, "downloaded", destination.name

    df = extract_timeline_table(response.text)
    destination = output_dir / "{}.csv".format(file_stem)
    if not dry_run:
        save_dataframe_csv(df, destination)
    return real_title, article_url, "built", destination.name


def collect_all_timeline_posts(
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
        page_posts = extract_post_links(response.text, url)

        if not page_posts:
            logging.info("No timeline posts found on page %s. Stopping.", current_page)
            break

        new_count = 0
        for title, post_url in page_posts:
            if post_url not in seen_urls:
                posts.append((title, post_url))
                seen_urls.add(post_url)
                new_count += 1

        logging.info(
            "Found %s timeline posts on page %s (%s new)",
            len(page_posts),
            current_page,
            new_count,
        )
        pages_visited += 1
        current_page += 1
        polite_sleep(delay)

    return posts


def print_found_timelines(posts: List[Tuple[str, str]]) -> None:
    print("\nFound {} timeline posts:\n".format(len(posts)))
    for index, (title, url) in enumerate(posts, start=1):
        print("{:03d}. {}".format(index, title))
        print("     {}".format(url))
    print("")


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List and download/export Hackmageddon Cyber Attacks Timeline CSVs"
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
        "--dry-run",
        action="store_true",
        help="Discover posts and test extraction logic without writing files",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List found timeline posts and exit without processing them",
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
        posts = collect_all_timeline_posts(
            session=session,
            start_page=args.start_page,
            end_page=args.end_page,
            max_pages=args.max_pages,
            delay=args.delay,
        )

        if not posts:
            logging.warning("No timeline posts collected.")
            return 1

        print_found_timelines(posts)

        if args.list_only:
            logging.info("Listed timeline posts only. Exiting because --list-only was used.")
            return 0

        logging.info("Total timeline posts queued: %s", len(posts))

        success = 0
        failures = 0
        downloaded = 0
        built = 0
        results = []  # type: List[Dict[str, str]]

        for idx, (title, url) in enumerate(posts, start=1):
            logging.info("[%s/%s] Processing %s", idx, len(posts), title)
            try:
                real_title, article_url, method, filename = process_article(
                    session=session,
                    title_hint=title,
                    article_url=url,
                    output_dir=args.output,
                    dry_run=args.dry_run,
                )
                if method == "downloaded":
                    downloaded += 1
                elif method == "built":
                    built += 1

                status_label = method
                if args.dry_run:
                    status_label = "would-{}".format(method)

                results.append(
                    {
                        "title": real_title,
                        "url": article_url,
                        "status": status_label,
                        "file": filename,
                    }
                )
                logging.info("OK | %s | %s | %s | %s", real_title, article_url, status_label, filename)
                print("OK   | {} | {} | {}".format(status_label, real_title, filename))
                success += 1
            except Exception as exc:
                logging.exception("FAILED | %s | %s", title, url)
                results.append(
                    {
                        "title": title,
                        "url": url,
                        "status": "failed",
                        "file": "",
                        "error": str(exc),
                    }
                )
                print("FAIL | {} | {}".format(title, exc))
                failures += 1
            polite_sleep(args.delay)

        print("\nSummary")
        print("-------")
        print("success: {}".format(success))
        print("fails: {}".format(failures))
        print("downloaded: {}".format(downloaded))
        print("built: {}".format(built))
        print("output: {}".format(args.output.resolve()))

        logging.info(
            "Done. success=%s fails=%s downloaded=%s built=%s output=%s",
            success,
            failures,
            downloaded,
            built,
            args.output,
        )
        return 0 if success > 0 else 1

    except requests.HTTPError as exc:
        logging.error("HTTP error: %s", exc)
        return 1
    except requests.RequestException as exc:
        logging.error("Network error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
