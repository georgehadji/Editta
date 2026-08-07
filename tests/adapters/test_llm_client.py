from editta.adapters.llm.client import build_user_content
from editta.domain.models import CachePolicy, PromptParts

PARTS = PromptParts(system="sys", manuscript_block="manuscript body", instructions="do X")


def test_cache_control_attached_when_caching_requested() -> None:
    for policy in (CachePolicy.WRITE, CachePolicy.READ):
        content = build_user_content(PARTS, policy)
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        assert content[1] == {"type": "text", "text": "do X"}


def test_cache_control_omitted_when_not_requested() -> None:
    content = build_user_content(PARTS, CachePolicy.NONE)
    assert "cache_control" not in content[0]
