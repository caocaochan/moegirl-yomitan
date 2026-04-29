from pathlib import Path


def test_build_workflow_publishes_zip_and_standalone_index_assets() -> None:
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "python -m moegirl_yomitan fetch" in workflow
    assert "python -m moegirl_yomitan check-build-change" in workflow
    assert "github.event_name == 'workflow_dispatch' || steps.change.outputs.changed == 'true'" in workflow
    assert "dist/moegirl-yomitan.zip" in workflow
    assert "dist/moegirl-yomitan-index.json" in workflow
    assert 'python -m moegirl_yomitan build --from-cache --output dist/moegirl-yomitan.zip' in workflow
    assert 'gh release create "${{ steps.version.outputs.build_version }}"' in workflow
    assert '"dist/moegirl-yomitan.zip" "dist/moegirl-yomitan-index.json"' in workflow
