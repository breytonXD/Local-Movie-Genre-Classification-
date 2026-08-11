import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import urllib.parse
import urllib.request
import difflib

MISSPELLINGS = {
    "psyco": "psycho",
}
def fix_common_misspellings(s: str) -> str:
    t = s
    for misspelled, correct in MISSPELLINGS.items():
        t = re.sub(rf"\b{re.escape(misspelled)}\b", correct, t, flags=re.IGNORECASE)
    return t


QUALITY_TAGS = [
    r"2160p", r"1080p", r"720p", r"480p", r"4k", r"UHD", r"HDR10", r"DV", r"REMUX",
    r"WEB[- ]?DL", r"WEB[- ]?RIP", r"WEBRIP",
    r"BLURAY", r"BRRIP", r"BDRIP", r"DVDRIP", r"CAM", r"TS", r"HDRIP",
    r"XVID", r"x264", r"x265", r"HEVC", r"AV1",
    r"H\.?264", r"H\.?265",
    r"DDP\d+(\.\d+)?", r"DTS[- ]?HD", r"TRUEHD", r"AAC\d*", r"FLAC",
    r"EXTENDED", r"PROPER", r"REPACK", r"UNRATED", r"THEATRICAL", r"DC",
]
SITE_TAGS = [
    r"YTS(?:\.[A-Z]{2})?", r"RARBG", r"GalaxyRG", r"TGx", r"AMZN", r"NF", r"WEB", r"BLURAY", r"BRRip", r"BluRay"
]
SITE_RE = re.compile(r"(?:^|\b)(?:" + "|".join(SITE_TAGS) + r")(?:\b|$)", re.IGNORECASE)
SHORT_ALLCAPS_RE = re.compile(r"(?:^|\s)([A-Z]{2,3})(?=\s|$)")
QUALITY_RE = re.compile(r"(?:^|\b)(?:" + "|".join(QUALITY_TAGS) + r")(?:\b|$)", re.IGNORECASE)
PARENS_YEAR_RE = re.compile(r"[\(\[\{](\d{4})[\)\]\}]")
TRAILING_YEAR_RE = re.compile(r"(?:^|[^\d])(?P<year>19\d{2}|20\d{2})(?:\D|$)")
DELIMS_RE = re.compile(r"[._]")
JUNK_TOKENS = [
    "MULTI", "SUBS", "REMASTERED", "INTERNAL", "LIMITED", "READNFO", "RETAIL", "DL", "HC", "HDR",
    "DUAL[- ]AUDIO", "MULTI[- ]AUDIO", "UNRATED", "DIRECTORS", "CUT", "DC", "THEATRICAL", "REMASTERED"
]
JUNK_RE = re.compile(r"(?:^|\b)(?:" + "|".join(JUNK_TOKENS) + r")(?:\b|$)", re.IGNORECASE)

def clean_folder_name(name: str) -> Tuple[str, Optional[int]]:
    # replace dots/underscores with spaces
    base = DELIMS_RE.sub(" ", name)

    # try to find the year
    year = None
    m = PARENS_YEAR_RE.search(base)
    if m:
        try:
            year = int(m.group(1))
        except ValueError:
            year = None
        base = PARENS_YEAR_RE.sub("", base)

    # remove stuff in brackets
    base = re.sub(r"\[[^\]]+\]", " ", base)
    base = re.sub(r"\([^\)]+\)", " ", base)
    base = re.sub(r"\{[^\}]+\}", " ", base)

    # remove quality and codec tags
    base = QUALITY_RE.sub(" ", base)
    # remove torrent/site tags
    base = SITE_RE.sub(" ", base)
    # remove short leftover tags
    base = re.sub(r"\b([A-Za-z]{2,3})\b", lambda m: "" if m.group(1).isupper() and len(m.group(1)) <= 3 else m.group(0), base)

    # look for a year again if needed
    if year is None:
        m2 = TRAILING_YEAR_RE.search(base)
        if m2:
            try:
                year = int(m2.group("year"))
            except ValueError:
                year = None
            base = TRAILING_YEAR_RE.sub(" ", base, count=1)

    # clean up leftover brackets
    base = re.sub(r"[\[\]\(\)\{\}]", " ", base)
    # remove extra spaces
    title = " ".join(base.split())

    # fix common spelling mistakes
    title = fix_common_misspellings(title)

    # remove a dash at the end
    title = re.sub(r"\s*-\s*$", "", title)

    return title.strip(), year

class TMDbClient:
    def __init__(self, api_key: str, rate_limit_per_sec: float = 3.0):
        self.api_key = api_key
        self.min_interval = 1.0 / max(rate_limit_per_sec, 0.1)
        self._last_time = 0.0

    def _throttle(self):
        now = time.time()
        elapsed = now - self._last_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_time = time.time()

    def _get(self, url: str, params: Dict[str, str]) -> Dict:
        self._throttle()
        params["api_key"] = self.api_key
        params.setdefault("include_adult", "false")
        params.setdefault("language", "en-US")
        query = urllib.parse.urlencode(params)
        full_url = f"{url}?{query}"
        with urllib.request.urlopen(full_url) as resp:
            data = resp.read()
            return json.loads(data.decode("utf-8"))

    # TMDb searches
    def search_movie(self, title: str, year: Optional[int]) -> Optional[Dict]:
        # try with the year first
        if year:
            data = self._get("https://api.themoviedb.org/3/search/movie", {"query": title, "year": str(year)})
            if data.get("results"):
                return data["results"][0]
        # then without the year
        data = self._get("https://api.themoviedb.org/3/search/movie", {"query": title})
        if data.get("results"):
            return data["results"][0]
        # another search using the release year
        if year:
            data = self._get("https://api.themoviedb.org/3/search/movie", {"query": title, "primary_release_year": str(year)})
            if data.get("results"):
                return data["results"][0]
        # last try: general search
        data = self._get("https://api.themoviedb.org/3/search/multi", {"query": title})
        for r in data.get("results", []):
            if r.get("media_type") == "movie":
                return r
        return None

    def search_tv(self, title: str, year: Optional[int]) -> Optional[Dict]:
        # try with the year first
        if year:
            data = self._get("https://api.themoviedb.org/3/search/tv", {"query": title, "first_air_date_year": str(year)})
            if data.get("results"):
                return data["results"][0]
        # then without the year
        data = self._get("https://api.themoviedb.org/3/search/tv", {"query": title})
        if data.get("results"):
            return data["results"][0]
        # last try: general search
        data = self._get("https://api.themoviedb.org/3/search/multi", {"query": title})
        for r in data.get("results", []):
            if r.get("media_type") == "tv":
                return r
        return None

    def get_movie_details(self, movie_id: int) -> Dict:
        return self._get(f"https://api.themoviedb.org/3/movie/{movie_id}", {})

    def get_tv_details(self, tv_id: int) -> Dict:
        return self._get(f"https://api.themoviedb.org/3/tv/{tv_id}", {})

def get_genres(genres: List[Dict]) -> List[str]:
    genre_names = []

    for genre in genres:
        name = genre.get("name")

        if name == "Science Fiction":
            name = "Sci-Fi"
        elif name == "TV Movie":
            name = "TV"

        if name and name not in genre_names:
            genre_names.append(name)

    if len(genre_names) == 0:
        genre_names.append("Unknown")

    return genre_names

def make_folder(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def windows_file_url(path: str) -> str:
    path = path.replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    return f"file://{path}"

def create_url_shortcut(shortcut_path: Path, target_dir: Path, dry_run: bool):
    if dry_run:
        return
    make_folder(shortcut_path.parent)
    url = windows_file_url(str(target_dir))
    content = f"[InternetShortcut]\nURL={url}\nIconIndex=0\n"
    with open(shortcut_path, "w", encoding="utf-8") as f:
        f.write(content)

def action_link(src: Path, genre_dir: Path, dry_run: bool):
    if dry_run:
        return
    target = genre_dir / src.name
    if target.exists():
        return
    try:
        os.symlink(src, target, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name == "nt":
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(src)], check=True)

def action_copy(src: Path, genre_dir: Path, dry_run: bool):
    target = genre_dir / src.name
    if dry_run:
        return
    if target.exists():
        return
    shutil.copytree(src, target)

def action_move(src: Path, genre_dir: Path, dry_run: bool):
    target = genre_dir / src.name
    if dry_run:
        return
    if target.exists():
        return
    shutil.move(str(src), str(target))

def title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()

def load_overrides(path: Optional[str]) -> Dict[str, Tuple[str, int]]:
    table: Dict[str, Tuple[str, int]] = {}
    if not path:
        return table
    p = Path(path)
    if not p.exists():
        return table
    with open(p, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("folder_name", "").strip()
            typ = (row.get("tmdb_type", "movie") or "movie").strip().lower()
            try:
                mid = int(row.get("tmdb_id", "").strip())
            except Exception:
                continue
            if name:
                table[name] = (typ, mid)
    return table

def main():
    parser = argparse.ArgumentParser(description="Categorise movie folders into genres using TMDb.")
    parser.add_argument("--tmdb-key", help="TMDb API key (v3)", default=os.getenv("TMDB_API_KEY"))
    parser.add_argument("--root", required=True, help="Path to the root folder containing movie folders")
    parser.add_argument("--out", required=True, help="Output base folder for per-genre results")
    parser.add_argument("--action", choices=["index", "shortcut", "link", "move", "copy"], default="index",
                        help="What to do per-genre: just index, create shortcuts, NTFS link, move, or copy")
    parser.add_argument("--overrides", help="CSV with explicit TMDb IDs for specific folder names")
    parser.add_argument("--min-score", type=float, default=3.0, help="Minimum TMDb popularity to accept a match")
    parser.add_argument("--min-sim", type=float, default=0.45, help="Minimum title similarity to accept a match")
    parser.add_argument("--max", type=int, default=0, help="Process at most N folders (0 = all)")
    parser.add_argument("--skip-hidden", action="store_true", help="Skip folders starting with '.'")
    parser.add_argument("--dry-run", action="store_true", help="Do not write CSV or change files; just print")
    parser.add_argument("--confirm", action="store_true", help="Required for --action move to proceed")
    parser.add_argument("--verbose", action="store_true", help="Print debug info")
    parser.add_argument("--index-name", default="movie_genres_index.csv", help="CSV filename to write in --out")
    args = parser.parse_args()

    if not args.tmdb_key:
        print("Error: TMDb API key is required. Provide --tmdb-key or set TMDB_API_KEY.", file=sys.stderr)
        sys.exit(2)

    root = Path(args.root).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    make_folder(out_dir)
    csv_path = out_dir / args.index_name

    client = TMDbClient(api_key=args.tmdb_key, rate_limit_per_sec=3.0)
    overrides = load_overrides(args.overrides)

 # get all movie folders
    try:
        # don't include the output folder
        out_dir = Path(args.out).expanduser().resolve()
        entries = [
            e for e in root.iterdir()
            if e.is_dir()
            and e.resolve() != out_dir
            and e.name != out_dir.name
        ]
    except FileNotFoundError:
        print(f"Root not found: {root}", file=sys.stderr)
        sys.exit(1)

    if args.skip_hidden:
        entries = [e for e in entries if not e.name.startswith(".")]
    entries.sort()
    if args.max > 0:
        entries = entries[:args.max]

    # choose what to do with matched folders
    if args.action == "link":
        do_action = action_link
    elif args.action == "copy":
        do_action = action_copy
    elif args.action == "move":
        if not args.confirm:
            print("Refusing to move folders without --confirm.", file=sys.stderr)
            sys.exit(3)
        do_action = action_move
    elif args.action == "shortcut":
        def do_action(src: Path, genre_dir: Path, dry_run: bool):
            shortcut_path = genre_dir / f"{src.name}.url"
            create_url_shortcut(shortcut_path, src, dry_run)
    else:  # index
        do_action = None

    # CSV columns
    fieldnames = ["folder_name", "title_guess", "year_guess", "tmdb_type", "tmdb_id",
                  "tmdb_title", "tmdb_year", "genres", "popularity", "similarity"]
    if not args.dry_run:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()

    processed = 0
    for folder in entries:
        title_guess, year_guess = clean_folder_name(folder.name)
        if not title_guess:
            title_guess = folder.name

        tmdb_type = ""
        tmdb_id = None
        tmdb_title = None
        tmdb_year = None
        genres = ["Unknown"]
        popularity = 0.0
        sim = 0.0

        # check manual overrides first
        if folder.name in overrides:
            tmdb_type, tmdb_id = overrides[folder.name]
            try:
                if tmdb_type == "movie":
                    details = client.get_movie_details(tmdb_id)
                    tmdb_title = details.get("title") or details.get("original_title")
                    rel = (details.get("release_date") or "0000-00-00").split("-")[0]
                    tmdb_year = int(rel) if rel.isdigit() else None
                    popularity = float(details.get("popularity") or 0.0)
                    genres = get_genres(details.get("genres", []))
                else:
                    details = client.get_tv_details(tmdb_id)
                    tmdb_title = details.get("name") or details.get("original_name")
                    rel = (details.get("first_air_date") or "0000-00-00").split("-")[0]
                    tmdb_year = int(rel) if rel.isdigit() else None
                    popularity = float(details.get("popularity") or 0.0)
                    genres = get_genres(details.get("genres", []))
                sim = title_similarity(title_guess, tmdb_title or "")
            except Exception as e:
                if args.verbose:
                    print(f"[DEBUG] Override failed for '{folder.name}': {e}", file=sys.stderr)
        else:
            # otherwise search TMDb
            try:
                hit = client.search_movie(title_guess, year_guess)
                if hit:
                    tmdb_type = "movie"
                    tmdb_id = hit.get("id")
                    tmdb_title = hit.get("title") or hit.get("original_title")
                    rel = (hit.get("release_date") or "0000-00-00").split("-")[0]
                    tmdb_year = int(rel) if rel.isdigit() else None
                    popularity = float(hit.get("popularity") or 0.0)
                    sim = title_similarity(title_guess, tmdb_title or "")
                    if popularity < args.min_score or sim < args.min_sim:
                        tmdb_type = ""
                        tmdb_id = None
                if tmdb_id is None:
                    tv_hit = client.search_tv(title_guess, year_guess)
                    if tv_hit:
                        tmdb_type = "tv"
                        tmdb_id = tv_hit.get("id")
                        tmdb_title = tv_hit.get("name") or tv_hit.get("original_name")
                        rel = (tv_hit.get("first_air_date") or "0000-00-00").split("-")[0]
                        tmdb_year = int(rel) if rel.isdigit() else None
                        popularity = float(tv_hit.get("popularity") or 0.0)
                        sim = title_similarity(title_guess, tmdb_title or "")
            except Exception as e:
                if args.verbose:
                    print(f"[DEBUG] Search failed for '{folder.name}': {e}", file=sys.stderr)

            # get full details and genres
            if tmdb_id is not None:
                try:
                    if tmdb_type == "movie":
                        details = client.get_movie_details(tmdb_id)
                        genres = get_genres(details.get("genres", []))
                        tmdb_title = details.get("title") or details.get("original_title") or tmdb_title
                        rel = (details.get("release_date") or "0000-00-00").split("-")[0]
                        tmdb_year = int(rel) if rel.isdigit() else tmdb_year
                        popularity = float(details.get("popularity") or popularity)
                    elif tmdb_type == "tv":
                        details = client.get_tv_details(tmdb_id)
                        genres = get_genres(details.get("genres", []))
                        tmdb_title = details.get("name") or details.get("original_name") or tmdb_title
                        rel = (details.get("first_air_date") or "0000-00-00").split("-")[0]
                        tmdb_year = int(rel) if rel.isdigit() else tmdb_year
                        popularity = float(details.get("popularity") or popularity)
                except Exception as e:
                    if args.verbose:
                        print(f"[DEBUG] Details failed for '{folder.name}' (id={tmdb_id} type={tmdb_type}): {e}", file=sys.stderr)

        if tmdb_id is None and args.verbose:
            print(f"[DEBUG] No match for '{folder.name}' (title_guess='{title_guess}', year_guess='{year_guess}')", file=sys.stderr)

        # add result to CSV
        row = {
            "folder_name": folder.name,
            "title_guess": title_guess,
            "year_guess": year_guess or "",
            "tmdb_type": tmdb_type or "",
            "tmdb_id": tmdb_id or "",
            "tmdb_title": tmdb_title or "",
            "tmdb_year": tmdb_year or "",
            "genres": "; ".join(genres) if genres else "Unknown",
            "popularity": f"{popularity:.1f}",
            "similarity": f"{sim:.2f}",
        }
        if not args.dry_run:
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=row.keys()).writerow(row)

        # do the selected file action
        if do_action and tmdb_id is not None:
            # moving uses the first genre only
            target_genres = [genres[0]] if args.action == "move" else genres
            for g in target_genres:
                g_dir = out_dir / g
                make_folder(g_dir)
                try:
                    do_action(folder, g_dir, args.dry_run)
                except Exception as e:
                    print(f"[WARN] Failed action for '{folder.name}' -> '{g}': {e}", file=sys.stderr)

        processed += 1
        tag = f"(TMDb: {tmdb_title or 'n/a'})"
        print(f"{folder.name} -> {row['genres']} {tag}")

    # done
    print(f"\nDone. Processed {processed} folders.")
    print(f"Index CSV: {csv_path}")
    if args.action == "index":
        print("No file operations performed (index only).")
    elif args.dry_run:
        print("Dry run: no changes were made.")
    elif args.action == "link":
        print("Symlinks/junctions created per-genre (NTFS + permissions required).")
    elif args.action == "copy":
        print("Copies created per-genre.")
    elif args.action == "move":
        print("Folders moved into per-genre directories.")
    elif args.action == "shortcut":
        print("Windows .url shortcuts created per-genre (cloud/non-NTFS friendly).")

if __name__ == "__main__":
    main()
