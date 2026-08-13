"""Real-account smoke test for search and optional playlist insertion."""

import argparse
import json
import sys

from server import add_tracks_to_playlist, search_music


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search YouTube Music and optionally add the first result to a playlist."
    )
    parser.add_argument("query", help="Music search text")
    parser.add_argument(
        "--filter",
        choices=("songs", "videos", "albums", "artists", "playlists"),
        default="songs",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--add-first-to",
        metavar="PLAYLIST_ID",
        help="Offer to add the first search result to this playlist",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt when adding"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results = search_music(args.query, filter=args.filter, limit=args.limit)
    print(json.dumps(results, indent=2, ensure_ascii=False))

    if not args.add_first_to:
        print("\nSearch test passed. Nothing was added to a playlist.")
        return 0
    if not results or not results[0].get("videoId"):
        print("\nThe first result has no videoId, so nothing was added.", file=sys.stderr)
        return 1

    first = results[0]
    confirmed = args.yes
    if not confirmed:
        answer = input(
            f"\nAdd '{first.get('title')}' to playlist {args.add_first_to}? [y/N] "
        )
        confirmed = answer.strip().lower() in {"y", "yes"}
    if not confirmed:
        print("Cancelled. Nothing was added.")
        return 0

    print(add_tracks_to_playlist(args.add_first_to, [first["videoId"]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
