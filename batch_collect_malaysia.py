"""Compatibility launcher for the adaptive Malaysia V2 collector."""

from batch_collect_malaysia_v2 import make_parser, run_batch


if __name__ == "__main__":
    raise SystemExit(run_batch(make_parser().parse_args()))
