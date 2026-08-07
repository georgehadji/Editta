"""Editorial report synthesis pass. No recommendation/score field: product safety rail."""

import json

from editta.domain.models import ManuscriptText, PassResult, PromptParts, SynthesisReport

SYNTHESIS_INSTRUCTIONS = (
    "You are synthesizing four editorial analysis passes (language, structure, metadata, "
    "integrity) into one cohesive report for a human editor. Do not add new findings not "
    "present in the pass results below. Do not include any recommendation, verdict, or "
    "numeric quality score — that judgment belongs to the human editor, not to you. Group "
    "related findings, write a short overview, and organize sections clearly."
)


class SynthesisPass:
    name = "synthesis"
    effort = "high"
    output_schema = SynthesisReport

    def build_prompt(
        self, manuscript: ManuscriptText, pass_results: list[PassResult]
    ) -> PromptParts:
        compact_results = [
            {"pass": r.pass_name, "findings": [f.model_dump(mode="json") for f in r.findings]}
            for r in pass_results
        ]
        return PromptParts(
            system=SYNTHESIS_INSTRUCTIONS,
            manuscript_block=manuscript.text,
            instructions=json.dumps(compact_results, ensure_ascii=False),
        )
