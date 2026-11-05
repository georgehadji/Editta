"""`editta analyze manuscript.(docx|pdf) -o report.docx` entrypoint."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from editta.adapters.export.docx_builder import ReportDocxBuilder
from editta.adapters.llm.client import AnthropicLLMClient
from editta.domain.errors import EdittaError
from editta.services.pipeline import run_pipeline

DEFAULT_COST_CEILING_USD = 6.0


def main() -> None:
    parser = argparse.ArgumentParser(prog="editta")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="run editorial analysis on a manuscript")
    analyze.add_argument("manuscript", type=Path, help="path to a .docx or .pdf manuscript")
    analyze.add_argument(
        "-o", "--output", type=Path, required=True, help="path to write the .docx report"
    )
    analyze.add_argument(
        "--cost-ceiling",
        type=float,
        default=DEFAULT_COST_CEILING_USD,
        help="abort if projected cost exceeds this many USD",
    )

    args = parser.parse_args()

    api_key = os.environ.get("EDITTA_ANTHROPIC_API_KEY")
    if not api_key:
        print("EDITTA_ANTHROPIC_API_KEY is not set", file=sys.stderr)
        raise SystemExit(1)

    try:
        manuscript_bytes = args.manuscript.read_bytes()
    except OSError as exc:
        print(f"failed to read {args.manuscript}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    llm = AnthropicLLMClient(api_key=api_key)

    try:
        report = asyncio.run(
            run_pipeline(
                manuscript_bytes=manuscript_bytes,
                filename=args.manuscript.name,
                llm=llm,
                cost_ceiling_usd=args.cost_ceiling,
            )
        )
    except EdittaError as exc:
        print(f"analysis failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    args.output.write_bytes(ReportDocxBuilder(report).build())
    pass_findings = sum(len(r.findings) for r in report.pass_results)
    reported = sum(len(section.findings) for section in report.synthesis.sections)
    print(
        f"report written to {args.output} "
        f"({reported} findings reported, from {pass_findings} anchored pass findings)"
    )


if __name__ == "__main__":
    main()
