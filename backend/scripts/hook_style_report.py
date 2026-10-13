"""Which hook arm actually got watched?

A zero-view diagnostic job ships each clip with a different hook style. Until the
style was recorded on the clip row and the platform's view counts were read back,
the experiment produced videos and no evidence. This prints the two together, so
a style is a claim about the channel's own audience instead of a guess from a
scoring model.

Usage:
    python -m scripts.hook_style_report                 # what the database already knows
    python -m scripts.hook_style_report --sync          # refresh view counts from YouTube first
    python -m scripts.hook_style_report --job-id 42     # one job's arms
    python -m scripts.hook_style_report --json          # machine-readable output

Read-only apart from --sync, which writes the four stats columns (and nothing
else). It never uploads, re-publishes or schedules anything.
"""
import argparse
import json
import sys
from typing import Optional

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from database.session import SessionLocal
from services.hook_analytics import UNASSIGNED, summarize_hook_styles

COLUMNS = ("arm", "clips", "uploaded", "measured", "total_views",
           "mean_views", "median_views", "best_clip")
WIDTHS = (16, 6, 9, 9, 12, 11, 13, 18)


def _row(values, widths=WIDTHS):
    cells = []
    for value, width in zip(values, widths):
        text = "" if value is None else str(value)
        # Labels read down their column, numbers read up it; both are padded to
        # the same width so the header and the rows line up.
        cells.append((text.ljust if isinstance(value, str) else text.rjust)(width))
    return "".join(cells)


def format_table(groups: dict) -> str:
    if not groups:
        return "No clips in the database."
    lines = [_row(COLUMNS), "-" * sum(WIDTHS)]
    for arm, data in groups.items():
        best = ""
        if data["top_clip_id"] is not None:
            best = f"clip {data['top_clip_id']} = {data['top_views']}"
        lines.append(_row((
            arm, data["clips"], data["uploaded"], data["with_stats"],
            data["total_views"], data["mean_views"], data["median_views"], best,
        )))
    return "\n".join(lines)


def notes(groups: dict) -> list:
    out = []
    unmeasured = sum(g["clips"] - g["with_stats"] for g in groups.values())
    if unmeasured:
        out.append(f"{unmeasured} clip(s) have no view count recorded yet; run with --sync "
                   f"once YouTube is connected.")
    if UNASSIGNED in groups:
        count = groups[UNASSIGNED]["clips"]
        out.append(f"{count} clip(s) carry no hook arm (they predate the experiment, or their "
                   f"job was not a diagnostic one) - they are reported separately so they do "
                   f"not distort an arm's average.")
    arms = [a for a in groups if a != UNASSIGNED and groups[a]["mean_views"] is not None]
    if len(arms) >= 2:
        best, worst = arms[0], arms[-1]
        spread = groups[best]["mean_views"] - groups[worst]["mean_views"]
        out.append(f"{best} leads {worst} by {spread:.1f} mean views "
                   f"(small samples move a lot; a handful of clips is a hint, not a result).")
    elif arms:
        out.append("Only one arm has been measured - there is nothing to compare it with yet.")
    return out


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report view counts per hook style (read-only unless --sync).")
    parser.add_argument("--sync", action="store_true",
                        help="ask YouTube for current view counts before reporting")
    parser.add_argument("--job-id", type=int, default=None, help="restrict to one job")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    db: Session = SessionLocal()
    try:
        sync_summary = None
        if args.sync:
            from services.youtube_stats import sync_clip_stats
            try:
                sync_summary = sync_clip_stats(db, job_id=args.job_id)
            except ImportError as exc:
                print(f"--sync needs the Google API client: {exc}", file=sys.stderr)
                return 2
            except Exception as exc:
                print(f"--sync failed ({exc}); reporting what the database already has.",
                      file=sys.stderr)

        groups = summarize_hook_styles(db, job_id=args.job_id)
    except OperationalError as exc:
        print("Refusing to read: the clips table has no hook_style / stats columns yet. "
              "Start the backend once - the schema sync at boot adds them, it never removes "
              f"anything.\nDetails: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()

    if args.json:
        print(json.dumps({"sync": sync_summary, "hook_styles": groups},
                         default=str, indent=2, ensure_ascii=False))
        return 0

    if sync_summary is not None:
        print(f"YouTube: {sync_summary['videos_queried']} video(s) queried, "
              f"{sync_summary['updated']} clip(s) updated, "
              f"{sync_summary['missing_on_youtube']} id(s) no longer on YouTube, "
              f"{sync_summary['placeholders']} placeholder id(s) skipped.\n")
    print(format_table(groups))
    for line in notes(groups):
        print(f"\n{line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
