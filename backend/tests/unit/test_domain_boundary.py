import ast
from pathlib import Path

DOMAIN_DIRECTORY = Path(__file__).parents[2] / "app" / "domain"
WORKERS_DIRECTORY = Path(__file__).parents[2] / "app" / "workers"
IMPLEMENTATION_FILES = {
    "capacity.py",
    "ordering.py",
    "provisioning.py",
    "quota.py",
    "subscription_render.py",
}
FORBIDDEN_IMPORTS = {
    "docker",
    "httpx",
    "os",
    "pathlib",
    "requests",
    "shlex",
    "subprocess",
}
FORBIDDEN_CALLS = {"open", "exec", "eval"}
FORBIDDEN_ATTRIBUTES = {
    "create_subprocess_exec",
    "create_subprocess_shell",
    "execv",
    "execve",
    "popen",
    "spawnv",
    "spawnve",
    "system",
}


def test_domain_has_no_external_sdk_file_or_shell_access() -> None:
    files = sorted(DOMAIN_DIRECTORY.glob("*.py"))
    assert {path.name for path in files} >= IMPLEMENTATION_FILES

    violations: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.partition(".")[0]
                    if root in FORBIDDEN_IMPORTS:
                        violations.append(f"{path.name}:{node.lineno}: import {root}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.partition(".")[0]
                if root in FORBIDDEN_IMPORTS:
                    violations.append(f"{path.name}:{node.lineno}: from {root}")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                    violations.append(f"{path.name}:{node.lineno}: call {node.func.id}")
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in FORBIDDEN_ATTRIBUTES
                ):
                    violations.append(f"{path.name}:{node.lineno}: call {node.func.attr}")

    assert violations == []


def test_domain_and_workers_do_not_import_transport_implementation() -> None:
    files = sorted(DOMAIN_DIRECTORY.glob("*.py")) + sorted(WORKERS_DIRECTORY.glob("*.py"))
    violations: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if "providers.transport.subscription" in text:
            violations.append(str(path))
    assert violations == []
