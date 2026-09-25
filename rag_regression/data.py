"""Dataset import. Gold fields never enter the public task document."""

import io
import json
import random
import re
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

from .core import digest, read_json, safe_relative, write_json

PUBLIC_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement", "version")


def public_task(row):
    task = {key: row[key] for key in PUBLIC_FIELDS}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+", task["instance_id"]):
        raise ValueError("Invalid instance_id")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", task["repo"]):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"[0-9a-f]{40}", task["base_commit"]):
        raise ValueError("Expected full base commit SHA")
    if not task["problem_statement"].strip():
        raise ValueError("Missing problem statement")
    return task


def fetch_rows(dataset, split):
    rows, offset = [], 0
    while True:
        query = urllib.parse.urlencode(dict(dataset=dataset, config="default", split=split,
                                             offset=offset, length=100))
        with urllib.request.urlopen("https://datasets-server.huggingface.co/rows?" + query, timeout=60) as response:
            page = json.load(response)
        for entry in page["rows"]:
            if entry.get("truncated_cells"):
                raise ValueError("Dataset server truncated a row; import a complete JSONL export instead")
            rows.append(entry["row"])
        offset += len(page["rows"])
        if offset >= page["num_rows_total"]:
            return rows
        if not page["rows"]:
            raise ValueError("Incomplete dataset response")


def import_tasks(rows, destination, count, seed=42, repo=None, dataset="local", split="dev"):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Task set already exists; choose a new destination to preserve selection")
    eligible = [row for row in rows if repo is None or row["repo"] == repo]
    eligible.sort(key=lambda row: row["instance_id"])
    if count <= 0 or count > len(eligible):
        raise ValueError("Not enough eligible tasks for requested count")
    selected = random.Random(seed).sample(eligible, count)
    tasks = [public_task(row) for row in selected]
    if len({task["instance_id"] for task in tasks}) != count:
        raise ValueError("Duplicate task IDs")
    for row, task in zip(selected, tasks):
        identifier = task["instance_id"]
        write_json(destination / "public" / identifier / "task.json", task)
        write_json(destination / "oracle" / (identifier + ".json"), {
            "instance_id": identifier, "base_commit": task["base_commit"], "patch": row["patch"]})
    write_json(destination / "selection.json", {
        "dataset": dataset, "split": split, "seed": seed, "repo_filter": repo,
        "ids": [task["instance_id"] for task in tasks],
        "public_sha256": digest(json.dumps(tasks, sort_keys=True)),
        "source_rows_sha256": digest(json.dumps(rows, sort_keys=True)),
    })
    return tasks


def extract_snapshot(data, destination, strip_first=False):
    """Extract regular files only: no links, devices, traversal or git history."""
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Snapshot already exists")
    destination.mkdir(parents=True)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            parts = safe_relative(member.name).parts
            if strip_first:
                parts = parts[1:]
            if not parts or ".git" in parts or not member.isfile():
                continue
            total += member.size
            if total > 512 * 1024 * 1024:
                raise ValueError("Snapshot exceeds 512 MiB limit")
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())


def prepare_source(task_directory):
    task_directory = Path(task_directory)
    task = public_task(read_json(task_directory / "task.json"))
    url = "https://codeload.github.com/{}/tar.gz/{}".format(task["repo"], task["base_commit"])
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(128 * 1024 * 1024 + 1)
    if len(data) > 128 * 1024 * 1024:
        raise ValueError("Archive exceeds download limit")
    extract_snapshot(data, task_directory / "source", strip_first=True)
    from .retrieval import source_hash
    write_json(task_directory / "snapshot.json", {
        "url": url, "archive_sha256": digest(data), "base_commit": task["base_commit"],
        "source_sha256": source_hash(task_directory / "source")})
