"""Fresh Docker container for each generated test. No host bind mounts or secrets."""

import re
import subprocess
import time
import uuid
from pathlib import Path

from .core import read_json, safe_relative, write_json


def validate_test(test, source):
    path = safe_relative(test["path"])
    if path.name != "test_rag_regression.py":
        raise ValueError("Generated file must be named test_rag_regression.py")
    target = Path(source).joinpath(*path.parts)
    if (target.exists() or not target.parent.is_dir() or any(part.startswith(".") for part in path.parts)
        or any(Path(source).joinpath(*path.parts[:n]).is_symlink() for n in range(1, len(path.parts) + 1))):
        raise ValueError("Test must be a new file in an existing non-hidden directory")
    if not test["code"].strip() or len(test["code"].encode("utf-8")) > 40_000:
        raise ValueError("Empty or oversized test")
    # Syntax errors are left for the runner so the agent can repair them.
    return str(path)


def classify(report, exit_code, log, timed_out=False):
    result = {"exit_code": exit_code, "executed": 0, "failed": [], "passed": [], "skipped": [],
              "status": "environment_error", "timed_out": timed_out}
    if timed_out or report is None:
        result["detail"] = ("timeout" if timed_out else "runner did not produce execution evidence") + "\n" + log[-4000:]
        return result
    calls = [case for case in report.get("cases", []) if case["phase"] == "call" and not case.get("xfail")]
    result["failed"] = sorted({c["id"] for c in calls if c["outcome"] == "failed"})
    result["passed"] = sorted({c["id"] for c in calls if c["outcome"] == "passed"})
    result["skipped"] = sorted({c["id"] for c in report.get("cases", []) if c["outcome"] == "skipped" or c.get("xfail")})
    result["executed"] = len(set(result["failed"] + result["passed"]))
    details = "\n".join(report.get("collection_errors", []) + [c.get("detail", "") for c in report.get("cases", [])])
    result["detail"] = details[-12000:]
    phase_errors = any(c["phase"] != "call" and c["outcome"] == "failed" for c in report.get("cases", []))
    if re.search(r"\b(SyntaxError|IndentationError|ImportError|ModuleNotFoundError)\b", details):
        result["status"] = "syntax_import_error"
    elif report.get("collection_errors") or phase_errors:
        result["status"] = "test_error"
    elif result["failed"] and exit_code == 1:
        result["status"] = "test_failure"  # Relevance still requires separate judgment.
    elif result["passed"] and not result["skipped"] and exit_code == 0:
        result["status"] = "passed"
    elif not result["executed"]:
        result["status"] = "not_executed"
    else:
        result["status"] = "unclear"
    return result


def image_name(task):
    identifier = task["instance_id"].lower().replace("__", "_1776_")
    return "swebench/sweb.eval.x86_64." + identifier + ":latest"


def docker_command(arguments, timeout=60, check=True):
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("Docker command failed: " + result.stderr[-1500:])
    return result


def prepare_image(task_directory, image=None, platform="linux/amd64", backend=None):
    directory = Path(task_directory)
    task = read_json(directory / "task.json")
    image = image or image_name(task)
    # Explicit preparation step; agent execution never pulls new images.
    existing = docker_command(["image", "inspect", image], check=False)
    if existing.returncode:
        result = subprocess.run(["docker", "pull", "--platform", platform, image])
        if result.returncode:
            raise RuntimeError("Image unavailable. Supply --image with a compatible locally built SWE-bench image.")
    identity = docker_command(["image", "inspect", "--format", "{{.Id}}", image]).stdout.strip()
    write_json(directory / "runtime.json", {"image": identity, "source_image": image,
               "platform": platform, "backend": backend or ("django" if task["repo"] == "django/django" else "pytest"),
               "python": "/opt/miniconda3/envs/testbed/bin/python"})


class DockerRunner:
    def __init__(self, task, config, runtime):
        self.task, self.config, self.runtime = task, config, runtime
        if runtime["backend"] not in {"pytest", "unittest", "django"}:
            raise ValueError("Unsupported test backend")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", runtime["image"]):
            raise ValueError("Prepare and pin the image before running")
        if not re.fullmatch(r"[0-9a-f]{40}", task["base_commit"]):
            raise ValueError("Invalid base commit")

    def run(self, test, directory, timeout, patch=None):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        code_path = directory / "test_rag_regression.py"
        code_path.write_text(test["code"], encoding="utf-8")
        name = "rag-regression-" + uuid.uuid4().hex[:16]
        started = time.monotonic()
        raw, report, exit_code, timed_out = "", None, None, False
        command = [self.runtime["python"], "/tmp/rag-runtime.py", self.runtime["backend"],
                   test["path"], "/tmp/rag-result.json"]
        def execute(arguments, **kwargs):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            return docker_command(arguments, timeout=remaining, **kwargs)
        try:
            execute(["create", "--name", name, "--platform", self.runtime["platform"],
                "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "128", "--memory", self.config.docker_memory,
                "--cpus", str(self.config.docker_cpus), "--workdir", "/testbed",
                "--user", "0:0", "--entrypoint", "/bin/bash", self.runtime["image"], "-c", "sleep infinity"])
            execute(["start", name])
            setup = execute(["exec", name, "git", "reset", "--hard", self.task["base_commit"]])
            raw += setup.stdout + setup.stderr
            execute(["exec", name, "git", "clean", "-fdx"])
            head = execute(["exec", name, "git", "rev-parse", "HEAD"]).stdout.strip()
            if head != self.task["base_commit"]:
                raise RuntimeError("Container commit mismatch")
            if patch is not None:
                patch_path = directory / "fix.patch"
                patch_path.write_text(patch, encoding="utf-8")
                execute(["cp", str(patch_path), name + ":/tmp/rag-fix.patch"])
                execute(["exec", name, "git", "apply", "--check", "/tmp/rag-fix.patch"])
                execute(["exec", name, "git", "apply", "/tmp/rag-fix.patch"])
            execute(["exec", name, "rm", "-rf", "/testbed/.git", "/tmp/rag-fix.patch"])
            execute(["cp", str(Path(__file__).with_name("runtime.py")), name + ":/tmp/rag-runtime.py"])
            execute(["cp", str(code_path), name + ":/tmp/rag-test.py"])
            # docker cp may preserve a host UID. Re-copy as container root before chmod.
            execute(["exec", name, "cp", "/tmp/rag-test.py", "/testbed/" + test["path"]])
            execute(["exec", name, "rm", "/tmp/rag-test.py"])
            execute(["exec", name, "chmod", "-R", "a+rX,a-w", "/testbed"])
            # No model-selected shell command and no credentials passed to Docker.
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            result = execute(["exec", "--user", "65534:65534", "-e", "HOME=/tmp",
                              "-e", "MPLCONFIGDIR=/tmp/matplotlib", name, *command], check=False)
            raw += result.stdout + result.stderr
            exit_code = result.returncode
            copied = execute(["cp", name + ":/tmp/rag-result.json", str(directory / "execution.json")], check=False)
            if copied.returncode == 0:
                report = read_json(directory / "execution.json")
        except subprocess.TimeoutExpired as error:
            timed_out = True
            raw += str(error) + "\n"
            for value in (error.stdout, error.stderr):
                if value:
                    raw += value.decode(errors="replace") if isinstance(value, bytes) else value
        except (RuntimeError, OSError, ValueError) as error:
            raw += str(error) + "\n"
        finally:
            try:
                docker_command(["rm", "-f", name], check=False)
            except (OSError, subprocess.TimeoutExpired):
                raw += "Container cleanup failed: " + name + "\n"
        (directory / "output.log").write_text(raw, encoding="utf-8")
        result = classify(report, exit_code, raw, timed_out)
        result.update(seconds=round(time.monotonic() - started, 3), command=command,
                      image=self.runtime["image"], backend=self.runtime["backend"])
        write_json(directory / "result.json", result)
        return result
