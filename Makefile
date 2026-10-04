.PHONY: check test lint type
check: lint type test
lint:
	uv run ruff check src tests
type:
	uv run mypy src/bmm/pricing src/bmm/spotcheck
test:
	uv run pytest -q
