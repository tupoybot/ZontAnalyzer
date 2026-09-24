"""Check that release publication remains gated by the complete CI workflow."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow(name: str) -> dict:
    # BaseLoader keeps the GitHub Actions `on` key as text rather than YAML 1.1 boolean.
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def test_release_calls_complete_ci_before_publishing() -> None:
    ci = workflow("ci.yml")
    release = workflow("application-release.yml")

    assert "pull_request" in ci["on"]
    assert "workflow_call" in ci["on"]
    assert "push" not in ci["on"]
    assert ci["on"]["workflow_call"]["inputs"]["build_release_candidate"]["type"] == "boolean"
    assert ci["permissions"] == {"contents": "read"}
    assert ci["jobs"]["test"]["runs-on"] == "ubuntu-latest"
    assert ci["jobs"]["test"]["steps"][0]["with"]["ref"] == "${{ github.sha }}"
    ci_commands = "\n".join(step.get("run", "") for step in ci["jobs"]["test"]["steps"])
    for check in (
        "deploy/check-local.sh build",
        "deploy/check-local.sh check-prebuilt",
        "docker build --target cli",
        "test_nginx_basic_auth.sh",
        "test_archive_browser.sh",
        "deploy/compose.yaml",
        "deploy/compose.local.yaml",
        "docker build --target production",
        "deploy/smoke-cloud-local.sh",
        "docker save",
        "sha256sum application-candidate.tar.gz",
    ):
        assert check in ci_commands
    ci_steps = ci["jobs"]["test"]["steps"]
    candidate_build = next(step for step in ci_steps if step.get("name") == "Build and smoke the cloud candidate")
    assert "if" not in candidate_build
    assert "docker build --target production" in candidate_build["run"]
    assert "deploy/smoke-cloud-local.sh" in candidate_build["run"]
    candidate_export = next(
        step for step in ci["jobs"]["test"]["steps"] if step.get("name", "").startswith("Export tested")
    )
    assert ci_steps.index(candidate_build) < ci_steps.index(candidate_export)
    assert candidate_export["if"] == "inputs.build_release_candidate"
    candidate_upload = next(
        step for step in ci["jobs"]["test"]["steps"] if step.get("with", {}).get("name") == "application-candidate"
    )
    assert candidate_upload["if"] == "inputs.build_release_candidate"
    assert candidate_upload["with"]["retention-days"] == "1"
    assert candidate_upload["with"]["overwrite"] == "true"

    verify = release["jobs"]["verify"]
    publish = release["jobs"]["release"]
    assert verify["uses"] == "./.github/workflows/ci.yml"
    assert verify["with"] == {"build_release_candidate": "true"}
    assert verify["permissions"] == {"contents": "read"}
    assert verify["if"] == publish["if"]
    assert "always()" not in verify["if"] and "always()" not in publish["if"]
    assert "continue-on-error" not in verify and "continue-on-error" not in publish
    assert publish["needs"] == "verify"
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    publish_commands = "\n".join(step.get("run", "") for step in publish["steps"])
    assert "deploy/check-local.sh all" not in publish_commands
    assert "docker build" not in publish_commands
    assert "sha256sum -c application-candidate.tar.gz.sha256" in publish_commands
    assert "docker load -i application-candidate.tar.gz" in publish_commands
    assert "deploy/smoke-cloud-local.sh" in publish_commands
    assert "docker push" in publish_commands
    assert "application-provenance.json" in publish_commands
    assert all("continue-on-error" not in step for step in publish["steps"])
    candidate_download = next(step for step in publish["steps"] if "download-artifact@" in step.get("uses", ""))
    assert candidate_download["with"]["name"] == "application-candidate"
    assert "run-id" not in candidate_download["with"]
    assert "repository" not in candidate_download["with"]
    assert candidate_download["if"] == "steps.existing.outputs.published != 'true'"


def test_workflow_changes_trigger_release_and_infrastructure_checks() -> None:
    release = workflow("application-release.yml")
    infrastructure = workflow("infrastructure-check.yml")
    release_paths = set(release["on"]["push"]["paths"])
    infrastructure_push = infrastructure["on"]["push"]
    infrastructure_paths = set(infrastructure_push["paths"])

    assert infrastructure_push["branches"] == release["on"]["push"]["branches"]
    for path in (
        ".github/workflows/ci.yml",
        ".github/workflows/application-release.yml",
        "deploy/check-local.sh",
        "deploy/check-ydb.sh",
        "deploy/check-tests.sh",
        "deploy/assert-ydb-memory.sh",
        "deploy/smoke-cloud-local.sh",
    ):
        assert path in release_paths
        assert path in infrastructure_paths
    assert ".github/workflows/infrastructure-deploy.yml" in infrastructure_paths
    assert set(infrastructure["on"]["pull_request"]["paths"]) == infrastructure_paths
