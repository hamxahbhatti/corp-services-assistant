"""Permission-aware hybrid retrieval.

Security trimming happens *inside* the search call, before ranking, exactly like an Azure AI Search
filter on `allowed_groups` (the GA security-filter pattern). Chunks the caller cannot see are never
scored, never returned and never reach the model.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from ..config import settings
from ..llm import embed
from .arabic import tokens


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    section_no: str
    text: str
    language: str
    owner: str
    version: str
    effective_date: str
    sensitivity: str
    score: float
    ranks: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    hits: list[Hit]
    trimmed_docs: list[str]  # doc ids removed by security trimming (debug/trace only - never shown to the user)
    candidates: int


def _visible(chunk: dict, groups: set[str]) -> bool:
    return bool(set(chunk["allowed_groups"]) & groups)


class LocalHybridIndex:
    def __init__(self, path=None):
        path = path or settings.data_dir / "index.json"
        if not path.exists():
            raise FileNotFoundError("Index not found. Run: python -m app.knowledge.ingest")
        data = json.loads(path.read_text())
        self.chunks: list[dict] = data["chunks"]
        self.toks = [tokens(c["text"]) for c in self.chunks]
        self.df = Counter(t for ts in self.toks for t in set(ts))
        self.N = len(self.chunks)
        self.avgdl = sum(len(t) for t in self.toks) / max(self.N, 1)
        self.norms = [math.sqrt(sum(x * x for x in c["vector"])) or 1.0 for c in self.chunks]

    def _bm25(self, q: list[str], i: int, k1=1.4, b=0.75) -> float:
        tf = Counter(self.toks[i])
        dl = len(self.toks[i])
        s = 0.0
        for t in q:
            if t not in tf:
                continue
            idf = math.log(1 + (self.N - self.df[t] + 0.5) / (self.df[t] + 0.5))
            s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * dl / self.avgdl))
        return s

    def search(self, queries: list[str], groups: list[str], top_k: int | None = None) -> SearchResult:
        top_k = top_k or settings.top_k
        g = set(groups)
        allowed = [i for i, c in enumerate(self.chunks) if _visible(c, g)]
        trimmed = sorted({c["doc_id"] for c in self.chunks if not _visible(c, g)})
        qvecs = embed(queries)
        fused: dict[int, float] = {}
        ranks: dict[int, dict] = {}
        for q, qv in zip(queries, qvecs):
            qt = tokens(q)
            qn = math.sqrt(sum(x * x for x in qv)) or 1.0
            kw = sorted(((self._bm25(qt, i), i) for i in allowed), reverse=True)
            vec = sorted(((sum(a * b for a, b in zip(qv, self.chunks[i]["vector"])) / (qn * self.norms[i]), i) for i in allowed), reverse=True)
            for name, lst in (("bm25", kw), ("vector", vec)):
                for r, (s, i) in enumerate(lst[:20]):
                    if name == "bm25" and s <= 0:
                        continue
                    fused[i] = fused.get(i, 0.0) + 1.0 / (60 + r + 1)  # reciprocal rank fusion
                    ranks.setdefault(i, {})[name] = min(ranks.get(i, {}).get(name, 99), r + 1)
        best = sorted(fused.items(), key=lambda x: -x[1])[:top_k]
        # debug/trace only: which restricted docs WOULD have ranked if the caller had access
        hidden = [i for i, c in enumerate(self.chunks) if not _visible(c, g)]
        would = set()
        if hidden:
            floor = best[-1][1] if best else 0
            for q, qv in zip(queries, qvecs):
                qt = tokens(q)
                qn = math.sqrt(sum(x * x for x in qv)) or 1.0
                for i in hidden:
                    cos = sum(a * b for a, b in zip(qv, self.chunks[i]["vector"])) / (qn * self.norms[i])
                    if self._bm25(qt, i) > 2.0 or cos > 0.55:
                        would.add(self.chunks[i]["doc_id"])
        trimmed = sorted(would)
        hits = []
        for i, s in best:
            c = self.chunks[i]
            hits.append(Hit(c["chunk_id"], c["doc_id"], c["title"], c["section"], c["section_no"], c["text"], c["language"],
                            c["owner"], c["version"], c["effective_date"], c["sensitivity"], round(s, 4), ranks.get(i, {})))
        return SearchResult(hits, trimmed, len(allowed))


class AzureSearchIndex:  # pragma: no cover - requires Azure; adapter untested in this repo
    def __init__(self):
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
        self.client = SearchClient(settings.azure_search_endpoint, settings.azure_search_index,
                                   AzureKeyCredential(settings.azure_search_key))

    def search(self, queries: list[str], groups: list[str], top_k: int | None = None) -> SearchResult:
        from azure.search.documents.models import VectorizedQuery
        top_k = top_k or settings.top_k
        flt = "allowed_groups/any(g: search.in(g, '%s', ','))" % ",".join(groups)
        seen: dict[str, Hit] = {}
        for q, qv in zip(queries, embed(queries)):
            res = self.client.search(search_text=q, vector_queries=[VectorizedQuery(vector=qv, k_nearest_neighbors=20, fields="vector")],
                                     filter=flt, query_type="semantic", semantic_configuration_name="default", top=top_k)
            for d in res:
                score = d.get("@search.reranker_score") or d.get("@search.score") or 0
                h = Hit(d["chunk_id"], d["doc_id"], d["title"], d["section"], d.get("section_no", ""), d.get("text_en") or d.get("text_ar"),
                        d["language"], d["owner"], d["version"], d["effective_date"], d["sensitivity"], float(score), {"semantic": 1})
                if h.chunk_id not in seen or seen[h.chunk_id].score < h.score:
                    seen[h.chunk_id] = h
        hits = sorted(seen.values(), key=lambda h: -h.score)[:top_k]
        return SearchResult(hits, [], -1)


@lru_cache(maxsize=1)
def get_index():
    return AzureSearchIndex() if settings.search_backend == "azure" else LocalHybridIndex()
