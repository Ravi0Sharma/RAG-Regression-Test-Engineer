"""Symbol-aware chunks, BM25/dense retrieval, routing and reciprocal rank fusion."""

import ast
import fnmatch
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from .core import digest, read_json, write_json


def tokens(text):
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return re.findall(r"[a-z0-9]+", text.lower().replace("_", " "))


def category(path):
    parts = Path(path).parts
    if any(part in {"test", "tests", "testing"} for part in parts) or Path(path).name.startswith(("test_", "conftest")):
        return "tests"
    return "code" if path.endswith(".py") else "docs"


def source_hash(source):
    return digest("\n".join(str(p.relative_to(source)) + ":" + digest(p.read_bytes())
                            for p in sorted(Path(source).rglob("*")) if p.is_file() and not p.is_symlink()))


def chunks_for_file(path, text, task, max_lines):
    lines = text.splitlines()
    ranges = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
            cursor = 1
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    if cursor < start:
                        ranges.append((cursor, start - 1, "<module>"))
                    ranges.append((start, node.end_lineno, node.name))
                    cursor = node.end_lineno + 1
            if cursor <= len(lines):
                ranges.append((cursor, len(lines), "<module>"))
        except SyntaxError:
            ranges = []  # Older-language syntax uses traceable line windows.
    if not ranges:
        ranges = [(1, len(lines), "<text>")]
    for start, end, symbol in ranges:
        for position in range(start, end + 1, max_lines):
            last = min(end, position + max_lines - 1)
            # Bound embedding inputs while keeping accurate line metadata.
            current, length = position, 0
            for number in range(position, last + 1):
                size = len(lines[number - 1].encode("utf-8"))
                if length and length + size > 6000:
                    yield _chunk(path, lines, current, number - 1, symbol, task)
                    current, length = number, 0
                if size > 6000:
                    if current < number:
                        yield _chunk(path, lines, current, number - 1, symbol, task)
                    current, length = number + 1, 0
                    continue  # Generated/minified long lines are not indexed.
                length += size
            if current <= last:
                yield _chunk(path, lines, current, last, symbol, task)


def _chunk(path, lines, start, end, symbol, task):
    body = "\n".join(lines[start - 1:end])
    return {"id": digest(path + str(start) + body)[:20], "path": path,
            "start": start, "end": end, "symbol": symbol, "kind": category(path),
            "repo": task["repo"], "commit": task["base_commit"], "text": body}


def build_index(task_directory, config, embed=None):
    directory = Path(task_directory)
    task = read_json(directory / "task.json")
    source = directory / "source"
    if not source.is_dir():
        raise ValueError("Prepare source before indexing")
    snapshot_path = directory / "snapshot.json"
    if snapshot_path.exists():
        snapshot = read_json(snapshot_path)
        if snapshot["base_commit"] != task["base_commit"] or snapshot["source_sha256"] != source_hash(source):
            raise ValueError("Prepared source differs from the downloaded snapshot")
    chunks = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix not in {".py", ".md", ".rst", ".txt"}:
            continue
        if path.stat().st_size > 500_000:
            continue
        relative = str(path.relative_to(source))
        if any(part.startswith(".") or part in {"node_modules", "__pycache__"} for part in path.relative_to(source).parts):
            continue
        chunks.extend(chunks_for_file(relative, path.read_text(encoding="utf-8", errors="replace"),
                                      task, config.chunk_lines))
    if not chunks:
        raise ValueError("No indexable source")
    vectors = []
    if config.retrieval == "hybrid":
        if embed is None:
            raise ValueError("Hybrid indexing requires an embedding client")
        for offset in range(0, len(chunks), 16):
            vectors.extend(embed([c["path"] + "\n" + c["text"] for c in chunks[offset:offset + 16]]))
    index = {"repo": task["repo"], "commit": task["base_commit"],
             "source_sha256": source_hash(source), "chunks": chunks, "vectors": vectors,
             "embedding_model": config.embedding_model if vectors else None,
             "chunk_lines": config.chunk_lines}
    write_json(directory / "index.json", index)
    return index


def rrf(rankings, k=60):
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, identifier in enumerate(dict.fromkeys(ranking), start=1):
            scores[identifier] += 1 / (k + rank)
    return sorted(scores, key=lambda identifier: (-scores[identifier], identifier)), dict(scores)


class Retriever:
    def __init__(self, index, task, config, embed=None):
        if index["repo"] != task["repo"] or index["commit"] != task["base_commit"]:
            raise ValueError("Index repository/version mismatch")
        if config.retrieval == "hybrid" and (not index["vectors"] or index["embedding_model"] != config.embedding_model):
            raise ValueError("Rebuild index for selected embedding model")
        if any(c["repo"] != task["repo"] or c["commit"] != task["base_commit"] for c in index["chunks"]):
            raise ValueError("Chunk provenance mismatch")
        self.index, self.config, self.embed = index, config, embed
        self.chunks = {chunk["id"]: chunk for chunk in index["chunks"]}
        self.counts = {key: Counter(tokens(c["path"] + " " + c["symbol"] + " " + c["text"]))
                       for key, c in self.chunks.items()}
        self.df = Counter(word for counter in self.counts.values() for word in counter)
        self.average = sum(sum(c.values()) for c in self.counts.values()) / max(1, len(self.counts))

    def search(self, queries, kinds, path_glob="*"):
        queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))[:self.config.max_queries]
        if not queries or not kinds or not set(kinds) <= {"code", "tests", "docs"}:
            raise ValueError("Invalid routed query")
        candidates = [key for key, c in self.chunks.items()
                      if c["kind"] in kinds and fnmatch.fnmatch(c["path"], path_glob or "*")]
        rankings, details = [], []
        vectors = self.embed(queries) if self.config.retrieval == "hybrid" else []
        for query_number, query in enumerate(queries):
            scores = {}
            for key in candidates:
                counter = self.counts[key]
                length = sum(counter.values())
                score = 0.0
                for word in set(tokens(query)):
                    frequency = counter[word]
                    inverse = math.log(1 + (len(self.counts) - self.df[word] + .5) / (self.df[word] + .5))
                    score += inverse * frequency * 2.5 / (frequency + 1.5 * (.25 + .75 * length / self.average))
                if score > 0:
                    scores[key] = score
            lexical = sorted(scores, key=lambda key: (-scores[key], key))[:self.config.top_k]
            ranking = lexical
            if vectors:
                vector_map = zip(self.index["chunks"], self.index["vectors"])
                dense = {c["id"]: cosine(vectors[query_number], vector) for c, vector in vector_map if c["id"] in candidates}
                dense_rank = sorted(dense, key=lambda key: (-dense[key], key))[:self.config.top_k]
                ranking, _ = rrf([lexical, dense_rank])
                ranking = ranking[:self.config.top_k]
            rankings.append(ranking)
            details.append({"query": query, "ranking": ranking})
        fused, scores = rrf(rankings)
        hits = [{**self.chunks[key], "score": scores[key]} for key in fused[:self.config.top_k]]
        return hits, {"queries": details, "kinds": kinds, "path_glob": path_glob,
                      "repo": self.index["repo"], "commit": self.index["commit"],
                      "fusion": [{"id": c["id"], "score": c["score"]} for c in hits]}


def cosine(left, right):
    if len(left) != len(right):
        raise ValueError("Embedding dimensions differ")
    denominator = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    return sum(x*y for x, y in zip(left, right)) / denominator if denominator else 0.0
