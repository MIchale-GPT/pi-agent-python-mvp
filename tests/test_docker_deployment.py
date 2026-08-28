import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_script(
    path: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env is not None:
        process_env.update(env)
    return subprocess.run(
        ["bash", path],
        cwd=ROOT,
        env=process_env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_docker_image_installs_dataquery_cli_contract() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.12-slim")
    assert "COPY --from=ghcr.io/astral-sh/uv:0.11.28" in dockerfile
    assert "uv sync --frozen --no-dev --extra dataquery --no-editable" in dockerfile
    assert "uv run --frozen --no-sync tau --help" in dockerfile
    assert "pip install" not in dockerfile
    assert 'ENTRYPOINT ["tau"]' in dockerfile
    assert 'CMD ["--help"]' in dockerfile


def test_docker_build_context_excludes_runtime_secrets_and_outputs() -> None:
    patterns = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())

    assert {
        ".env",
        ".env.*",
        "*.env",
        "*.env.*",
        "**/.env",
        "**/.env.*",
        "**/*.env",
        "**/*.env.*",
        "*credentials*",
        "**/*credentials*.json",
        "dist",
    } <= patterns


def test_arm64_build_dry_run_covers_build_smoke_save_and_checksum() -> None:
    result = _run_script("./build-tau-arm64.sh", env={"DRY_RUN": "1"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "docker buildx build" in result.stdout
    assert "--platform linux/arm64" in result.stdout
    assert "--load" in result.stdout
    assert "docker run --rm --platform linux/arm64" in result.stdout
    assert "--print --help" in result.stdout
    assert "docker save" in result.stdout
    assert "sha256sum" in result.stdout


def test_start_dry_run_uses_sag_network_and_overrides_tau_entrypoint(tmp_path: Path) -> None:
    env_file = tmp_path / "tau.env.production"
    env_file.write_text("TAU_SAG_PLANNING_MODE=agent\n", encoding="utf-8")
    tau_home = tmp_path / ".tau"
    tau_home.mkdir()

    result = _run_script(
        "./tau-start.sh",
        env={
            "DRY_RUN": "1",
            "TAU_ENV_FILE": str(env_file),
            "TAU_HOME_DIR": str(tau_home),
            "SAG_API_IP": "172.30.0.8",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--network host" in result.stdout
    assert "--add-host api:172.30.0.8" in result.stdout
    assert "--entrypoint /bin/sleep" in result.stdout
    assert "tau:arm64-tui-latest infinity" in result.stdout
    assert "--network sag_default" not in result.stdout


def test_offline_credentials_template_has_dataquery_secret_keys() -> None:
    credentials = json.loads((ROOT / "tau.credentials.json.example").read_text(encoding="utf-8"))

    assert set(credentials) == {"dataquery.dws.password", "dataquery.sag.token"}
