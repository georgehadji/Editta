"""Editorial report synthesis pass. No recommendation/score field: product safety rail.

The model returns a SynthesisPlan that *cites* pass findings by id; the domain
resolves the ids to the anchored originals (domain/synthesis.py). It therefore cannot
add, alter or re-quote a finding — structurally, not by instruction.

Caching: synthesis cannot reuse the passes' cached manuscript. Its structured-output
schema differs, and per the Anthropic structured-outputs docs a change of
`output_config.format` invalidates the prompt cache; domain/budget.py prices it as
uncached input. It still uses SYSTEM_PREAMBLE, because the manuscript it sees is
untrusted content just as it is for the passes.
"""

import json

from editta.domain.models import ManuscriptText, PassResult, PromptParts, SynthesisPlan
from editta.domain.synthesis import catalog_findings
from editta.passes.base import SHARED_EFFORT, SYSTEM_PREAMBLE

SYNTHESIS_INSTRUCTIONS = (
    "You are synthesizing four editorial analysis passes (language, structure, metadata, "
    "integrity) into one cohesive report for a human editor. Each finding below has an "
    "`id`. Group related findings into sections by listing their ids in `finding_ids`; "
    "cite only ids that appear below and never invent findings. Write a short overview "
    "and a summary per section. Do not include any recommendation, verdict, or numeric "
    "quality score — that judgment belongs to the human editor, not to you."
)


class SynthesisPass:
    name = "synthesis"
    effort = SHARED_EFFORT
    output_schema = SynthesisPlan

    def build_prompt(
        self, manuscript: ManuscriptText, pass_results: list[PassResult]
    ) -> PromptParts:
        catalog = catalog_findings(pass_results)
        findings = [
            {"id": fid, **finding.model_dump(mode="json")} for fid, finding in catalog.items()
        ]
        return PromptParts(
            system=SYSTEM_PREAMBLE,
            manuscript_block=manuscript.text,
            instructions=(
                f"{SYNTHESIS_INSTRUCTIONS}\n\nFindings (JSON):\n"
                f"{json.dumps(findings, ensure_ascii=False)}"
            ),
        )
