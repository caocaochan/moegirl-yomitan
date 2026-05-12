from pathlib import Path


def test_github_workflow_validates_without_fetching_or_releasing() -> None:
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "name: Validate" in workflow
    assert "contents: read" in workflow
    assert 'pytest -q -k "not smoke_build_and_package"' in workflow
    assert "schedule:" not in workflow
    assert "actions/cache" not in workflow
    assert "python -m moegirl_yomitan fetch" not in workflow
    assert "gh release create" not in workflow
