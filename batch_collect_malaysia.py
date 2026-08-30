"""Stable launcher for the append-only LOCUS-T V4 Malaysia collector."""

from batch_collect_malaysia_v2 import make_parser, run_batch


if __name__ == "__main__":
    raise SystemExit(run_batch(make_parser().parse_args()))
