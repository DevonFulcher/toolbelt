import json
import subprocess
from pathlib import Path

import boto3
import yaml


def get_aws_secret(secret_name: str, region: str = "us-east-1") -> dict:
    session = boto3.session.Session()
    client = session.client(service_name="secretsmanager", region_name=region)

    response = client.get_secret_value(SecretId=secret_name)
    if "SecretString" in response:
        return json.loads(response["SecretString"])
    raise RuntimeError(f"No secret string found for {secret_name}")


def escape_env_value(value: str) -> str:
    """
    Escape environment variable values that contain special characters.
    Returns the original value if no escaping is needed.
    """
    needs_escaping = any(
        (
            "\n" in value,  # newlines
            " " in value,  # spaces
            "<" in value,  # angle brackets
            ">" in value,
            '"' in value,  # double quotes
            "'" in value,  # single quotes
            "$" in value,  # shell variables
            "#" in value,  # comments
            ";" in value,  # command separators
            "&" in value,
            "|" in value,
            "(" in value,  # parentheses
            ")" in value,
            "[" in value,  # brackets
            "]" in value,
            "{" in value,  # braces
            "}" in value,
        )
    )

    if not needs_escaping:
        return value

    # Escape shell variables first
    escaped = value.replace("${", "\\${")
    # Escape any existing quotes
    escaped = escaped.replace('"', '\\"')
    # Wrap in quotes to preserve special characters
    return f'"{escaped}"'


def render_helm_yaml(
    repo_path: Path,
    git_projects_workdir: Path,
    service_name: str | None = None,
) -> dict:
    repo_name = Path.cwd().name if str(repo_path) == "." else repo_path.name
    charts_path = Path(
        git_projects_workdir,
        repo_name,
        "charts",
        service_name or repo_name,
    )
    if not charts_path.exists():
        return {}
    helm_params: list[str] = []
    service_default_values = charts_path / "values.yaml"
    if service_default_values.exists():
        helm_params.append("-f")
        helm_params.append(str(service_default_values))
    standard_values = (
        git_projects_workdir / "helm-releases/releases/dbt-labs/devspace/main.yaml"
    )
    if standard_values.exists():
        helm_params.append("-f")
        helm_params.append(str(standard_values))
    service_devspace_values = (
        git_projects_workdir
        / f"helm-releases/releases/dbt-labs/devspace/{service_name or repo_name}.yaml"
    )
    if service_devspace_values.exists():
        helm_params.append("-f")
        helm_params.append(str(service_devspace_values))
    if not helm_params:
        return {}
    process = subprocess.run(
        [
            "helm",
            "template",
            "dev",
            str(charts_path),
        ]
        + helm_params,
        capture_output=True,
        text=True,
        check=True,
    )
    if process.stderr:
        raise RuntimeError(f"Error running helm command: {process.stderr}")
    rendered_yaml = process.stdout
    documents = yaml.safe_load_all(rendered_yaml)
    dot_envs: dict[str, str] = {}
    for doc in documents:
        if isinstance(doc, dict):
            kind = doc.get("kind")
            if kind == "ConfigMap":
                config_data = doc.get("data", {})
                dot_envs.update(config_data)
            elif kind == "ExternalSecret":
                spec = doc.get("spec", {})
                if spec.get("backendType") == "secretsManager":
                    data_from = spec.get("dataFrom", [])
                    for secret_ref in data_from:
                        if isinstance(secret_ref, str):
                            secrets = get_aws_secret(secret_ref)
                            dot_envs.update(secrets)

    # direnv can't handle env vars with newlines even with escaping.
    return {k: escape_env_value(v) for k, v in dot_envs.items() if "\n" not in v}
