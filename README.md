# Editta

AI editorial assistant for publishing houses. Analyzes essay/book manuscripts and produces a
structured editorial report (language/style, structure/argument, metadata/positioning,
integrity-light) to support human editors. Never renders accept/reject verdicts, quality scores,
AI-detection, or plagiarism results.

Architecture and full build plan: [docs/PLAN.md](docs/PLAN.md).

## Dev setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
cp .env.example .env   # fill in real values
docker compose -f infra/docker-compose.yml up -d
pytest
```
