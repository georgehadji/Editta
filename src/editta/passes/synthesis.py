"""Editorial report synthesis pass. No recommendation/score field: product safety rail.

Shares the analysis passes' cached prefix (same system prompt, same manuscript block,
same effort — see passes/base.py), so the manuscript is billed at the cache-read rate.
The synthesis-specific instructions and the passes' findings go after the prefix.
Its output is still re-anchored in services/analysis.py: the prompt asks for "no new
findings", the domain layer enforces it.
"""

import json

from editta.domain.models import ManuscriptText, PassResult, PromptParts, SynthesisReport
from editta.passes.base import SHARED_EFFORT, SYSTEM_PREAMBLE

SYNTHESIS_INSTRUCTIONS = (
    "You are synthesizing four editorial analysis passes (language, structure, metadata, "
    "integrity) into one cohesive report for a human editor. Do not add new findings not "
    "present in the pass results below; reuse their quotes verbatim. Do not include any "
    "recommendation, verdict, or numeric quality score — that judgment belongs to the "
    "human editor, not to you. Group related findings, write a short overview, and "
    "organize sections clearly."
)


class SynthesisPass:
    name = "synthesis"
    effort = SHARED_EFFORT
    output_schema = SynthesisReport

    def build_prompt(
        self, manuscript: ManuscriptText, pass_results: list[PassResult]
    ) -> PromptParts:
        compact_results = [
            {"pass": r.pass_name, "findings": [f.model_dump(mode="json") for f in r.findings]}
            for r in pass_results
        ]
        return PromptParts(
            system=SYSTEM_PREAMBLE,
            manuscript_block=manuscript.text,
            instructions=(
                f"{SYNTHESIS_INSTRUCTIONS}\n\nPass results (JSON):\n"
                f"{json.dumps(compact_results, ensure_ascii=False)}"
            ),
        )
