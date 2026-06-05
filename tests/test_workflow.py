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


def test_release_script_saves_build_state_after_successful_release() -> None:
    release_path = Path(__file__).resolve().parents[1] / "release.bat"
    release_script = release_path.read_text(encoding="utf-8")

    release_index = release_script.index("gh release create")
    diff_index = release_script.index("python -m moegirl_yomitan diff-releases")
    upload_index = release_script.index('gh release upload "%BUILD_VERSION%" "%DIFF_HTML%"')
    save_state_index = release_script.index("python -m moegirl_yomitan save-build-state --fingerprint")

    assert release_index < save_state_index
    assert release_index < diff_index < upload_index < save_state_index
    assert 'set "DIFF_HTML=dist\\moegirl-yomitan-release-diff.html"' in release_script
    assert (
        "https://github.com/caocaochan/moegirl-yomitan/releases/download/"
        "%BUILD_VERSION%/moegirl-yomitan-release-diff.html"
    ) in release_script
    assert "Manual Yomitan dictionary build for version %BUILD_VERSION%." in release_script
    assert "Entries added: %DIFF_URL%" in release_script
    assert 'gh release create "%BUILD_VERSION%" "dist\\moegirl-yomitan.zip" "dist\\moegirl-yomitan-index.json" --title "%BUILD_VERSION%" --notes-file "%RELEASE_NOTES%"' in release_script
    assert 'save-build-state --fingerprint "%FINGERPRINT%"' in release_script
