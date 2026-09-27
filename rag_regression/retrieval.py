"""Symbol-aware chunks, BM25/dense retrieval, routing and reciprocal rank fusion."""

import ast
import fnmatch
import math
import re
import textwrap
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
    return "code" if path.endswith((".py", ".sql")) else "docs"


def source_hash(source):
    return digest("\n".join(str(p.relative_to(source)) + ":" + digest(p.read_bytes())
                            for p in sorted(Path(source).rglob("*")) if p.is_file() and not p.is_symlink()))


def chunks_for_file(path, text, task, max_lines):
    lines = text.splitlines()
    ranges = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
            def split_symbols(body, cursor, end, parent=""):
                for node in body:
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        continue
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    if cursor < start:
                        ranges.append((cursor, start - 1, parent or "<module>"))
                    symbol = parent + "." + node.name if parent else node.name
                    if isinstance(node, ast.ClassDef):
                        split_symbols(node.body, start, node.end_lineno, symbol)
                    else:
                        ranges.append((start, node.end_lineno, symbol))
                    cursor = node.end_lineno + 1
                if cursor <= end:
                    ranges.append((cursor, end, parent or "<module>"))
            split_symbols(tree.body, 1, len(lines))
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
        if not path.is_file() or path.is_symlink() or path.suffix not in {".py", ".md", ".rst", ".txt", ".yml", ".yaml", ".sql"}:
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
        self.chunks = {chunk["id"]: chunk for chunk in index["chunks"] if chunk["text"].strip()}
        self.counts = {key: Counter(tokens(c["path"] + " " + c["symbol"] + " " + c["text"]))
                       for key, c in self.chunks.items()}
        self.df = Counter(word for counter in self.counts.values() for word in counter)
        self.average = sum(sum(c.values()) for c in self.counts.values()) / max(1, len(self.counts))

    def lexical_score(self, key, words):
        counter = self.counts.get(key, {})
        length = sum(counter.values())
        score = 0.0
        for word in words:
            frequency = counter.get(word, 0)
            inverse = math.log(1 + (len(self.counts) - self.df[word] + .5) / (self.df[word] + .5))
            score += inverse * frequency * 2.5 / (frequency + 1.5 * (.25 + .75 * length / (self.average or 1)))
        return score

    def rank_previous(self, sources, issue):
        """Prefer prior evidence matching the original issue, not the latest setup query."""
        words = set(tokens(issue))
        return sorted(sources, key=lambda source: -self.lexical_score(source['id'], words))

    def search(self, queries, kinds, path_glob="*"):
        queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))[:self.config.max_queries]
        if not queries or not kinds or not set(kinds) <= {"code", "tests", "docs"}:
            raise ValueError("Invalid routed query")
        candidates = [key for key, c in self.chunks.items()
                      if c["kind"] in kinds and fnmatch.fnmatch(c["path"], path_glob or "*")]
        rankings, pattern_rankings, details = [], [], []
        vectors = self.embed(queries) if self.config.retrieval == "hybrid" else []
        for query_number, query in enumerate(queries):
            scores = {}
            words = set(tokens(query))
            for key in candidates:
                score = self.lexical_score(key, words)
                if score > 0:
                    scores[key] = score
            lexical_all = sorted(scores, key=lambda key: (-scores[key], key))
            lexical = lexical_all[:self.config.top_k]
            patterns = [key for key in lexical_all if self.is_test_pattern(key)][:self.config.top_k]
            ranking = lexical
            if vectors:
                vector_map = zip(self.index["chunks"], self.index["vectors"])
                dense = {c["id"]: cosine(vectors[query_number], vector) for c, vector in vector_map if c["id"] in candidates}
                dense_all = sorted(dense, key=lambda key: (-dense[key], key))
                dense_rank = dense_all[:self.config.top_k]
                ranking, _ = rrf([lexical, dense_rank])
                ranking = ranking[:self.config.top_k]
                patterns, _ = rrf([patterns, [key for key in dense_all
                                             if self.is_test_pattern(key)][:self.config.top_k]])
                patterns = patterns[:self.config.top_k]
            rankings.append(ranking)
            pattern_rankings.append(patterns)
            details.append({"query": query, "ranking": ranking, "test_pattern_ranking": patterns})
        fused, scores = rrf(rankings)
        patterns, pattern_scores = rrf(pattern_rankings)
        # Reserve 1-2 primary slots (a quarter at the default size) for matching
        # examples. Fixtures remain searchable, but cannot crowd out every caller.
        reserved = patterns[:min(2, max(1, self.config.top_k // 4))]
        selected = list(dict.fromkeys(reserved + fused))[:self.config.top_k]
        hits = [{**self.chunks[key], "score": scores.get(key, 0),
                 "test_pattern_score": pattern_scores.get(key, 0)} for key in selected]
        # Same-file imports/helpers help interpret isolated method chunks. This is context,
        # not another search or a relevance claim; share the existing 2*top_k cap.
        context = list(dict.fromkeys(identifier for key in reserved for identifier in self.context_ids(key)
                                     if identifier not in selected))
        truncated = len(context) > max(0, 2 * self.config.top_k - len(hits))
        context = context[:max(0, 2 * self.config.top_k - len(hits))]
        hits.extend({**self.chunks[key], "score": 0} for key in context)
        return hits, {"queries": details, "kinds": kinds, "path_glob": path_glob,
                      "repo": self.index["repo"], "commit": self.index["commit"],
                      "fusion": [{"id": key, "score": scores[key]} for key in fused[:self.config.top_k]],
                      "test_pattern_ids": reserved, "context_ids": context,
                      "selected_ids": selected, "context_truncated": truncated}

    def context_ids(self, key):
        """Same-file symbol windows, class setup, imports and referenced helpers."""
        example = self.chunks[key]
        siblings = sorted((c for c in self.chunks.values() if c["path"] == example["path"]),
                          key=lambda c: c["start"])
        symbol = example["symbol"]
        parent = symbol.rsplit(".", 1)[0] if "." in symbol else ""
        try:
            tree = ast.parse(textwrap.dedent(example["text"]))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            names.update(n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute))
        except SyntaxError:
            names = set()
        helper_names = names | {parent + "." + name for name in names if parent}
        is_class = self._class_definition(example)
        owner = symbol if is_class else parent
        setup = {owner, *(owner + "." + n for n in
                          ("setUp", "setUpClass", "setUpTestData", "setup_method", "setup_class"))} if owner else set()
        imports = [c for c in siblings if c["symbol"] == "<module>"
                   and re.search(r"^(?:from \S+ import |import )", c["text"], re.M)]
        class_definition = [c for c in siblings if owner and c["symbol"] == owner
                            and self._class_definition(c)]
        base = self._local_base_context(owner, siblings) if owner else None
        setup_methods = [c for c in siblings if c["symbol"] in setup and c["symbol"] != owner]
        # Companion tests of the same command expose alternate fixture/settings
        # choices. Keep this bounded and in the same pinned source file.
        calls = self._literal_calls(example)
        related = [c for c in siblings if c["symbol"] != symbol
                   and self.is_test_pattern(c["id"])
                   and calls & self._literal_calls(c)][:2]
        groups = [imports, class_definition, [base] if base else [], setup_methods,
                  related,
                  [c for c in siblings if c["symbol"] in helper_names],
                  [c for c in siblings if c["symbol"] == symbol],
                  [c for c in siblings if is_class and c["symbol"].startswith(symbol + ".")]]
        return list(dict.fromkeys(c["id"] for group in groups for c in group
                                  if c and c["id"] != key))

    @staticmethod
    def _literal_calls(source):
        """Direct calls with a literal first argument, e.g. run_command('name')."""
        try:
            tree = ast.parse(textwrap.dedent(source["text"]))
        except SyntaxError:
            return set()
        return {(node.func.id, node.args[0].value) for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)}

    @staticmethod
    def _class_definition(source):
        name = source["symbol"].rsplit(".", 1)[-1]
        return bool(re.search(r"^\s*class\s+" + re.escape(name) + r"\b", source["text"], re.M))

    def _local_base_context(self, owner, siblings):
        """Resolve one unambiguous locally imported base class; never follow its dependencies."""
        class_chunk = next((c for c in siblings if c["symbol"] == owner and self._class_definition(c)), None)
        if class_chunk is None:
            return None
        class_name = owner.rsplit(".", 1)[-1]
        match = re.search(r"^\s*class\s+" + re.escape(class_name) + r"\s*\(([^)]*)\)",
                          class_chunk["text"], re.M | re.S)
        if match is None:
            return None
        bases = []
        for expression in match.group(1).split(","):
            name = expression.strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                bases.append(name)
        if not bases:
            return None

        path = Path(class_chunk["path"])
        module_chunks = [c for c in self.chunks.values()
                         if c["path"] == class_chunk["path"] and c["symbol"] == "<module>"]
        imports = defaultdict(set)
        rebound = set()
        for chunk in module_chunks:
            try:
                tree = ast.parse(chunk["text"])
            except SyntaxError:
                continue
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        package = path.parent.parts
                        ascend = node.level - 1
                        if not node.module or ascend >= len(package):
                            continue
                        package = package[:len(package) - ascend]
                        module_parts = package + tuple(node.module.split("."))
                        module_path = Path(*module_parts)
                    elif node.module:
                        module_path = Path(*node.module.split("."))
                    else:
                        continue
                    target_path = str(module_path.with_suffix(".py"))
                    for alias in node.names:
                        imports[alias.asname or alias.name].add((alias.name, target_path))
                    continue
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    rebound.add(node.name)
                elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    rebound.update(name.id for target in targets for name in ast.walk(target)
                                   if isinstance(name, ast.Name))
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    rebound.update(name.id for name in ast.walk(node.target) if isinstance(name, ast.Name))

        for base_name in bases:
            if base_name in rebound:
                continue
            imported = imports.get(base_name, set())
            if len(imported) != 1:
                continue
            original, target_path = next(iter(imported))
            matches = [c for c in self.chunks.values()
                       if c["path"] == target_path and c["symbol"] == original
                       and self._class_definition(c)]
            if len(matches) == 1:
                return matches[0]
        return None

    def read_context(self, identifiers, available_sources, limit=None):
        """Expand only previously supplied IDs in the pinned index, never arbitrary paths."""
        identifiers = list(dict.fromkeys(identifiers))
        allowed = {c["id"] for c in available_sources} & self.chunks.keys()
        if not identifiers or len(identifiers) > self.config.max_queries or not set(identifiers) <= allowed:
            raise ValueError("Context read requires bounded IDs from supplied sources")
        context = list(dict.fromkeys(key for identifier in identifiers for key in self.context_ids(identifier)
                                    if key not in identifiers))
        cap = 2 * self.config.top_k if limit is None else min(limit, 2 * self.config.top_k)
        selected = (identifiers + context)[:cap]
        return [{**self.chunks[key], "score": 0} for key in selected], {
            "mode": "source_context", "queries": [], "requested_ids": identifiers,
            "selected_ids": [key for key in selected if key in identifiers],
            "context_ids": [key for key in selected if key not in identifiers],
            "context_truncated": len(identifiers) + len(context) > len(selected),
            "test_pattern_ids": [], "repo": self.index["repo"], "commit": self.index["commit"]}

    def is_test_pattern(self, key):
        chunk = self.chunks[key]
        name = chunk["symbol"].rsplit(".", 1)[-1]
        return (chunk["kind"] == "tests" and chunk["path"].endswith(".py")
                and name.startswith("test") and bool(re.search(r"^\s*(?:async )?def test", chunk["text"], re.M)))


def cosine(left, right):
    if len(left) != len(right):
        raise ValueError("Embedding dimensions differ")
    denominator = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    return sum(x*y for x, y in zip(left, right)) / denominator if denominator else 0.0
