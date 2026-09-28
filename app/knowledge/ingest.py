"""Ingestion: parse -> heading-aware chunking -> metadata + ACL enrichment -> embeddings -> index.

Production mapping: Document Intelligence layout extraction replaces the markdown parser, the
SharePoint indexer (or security-filter push) replaces the file loop, and Azure AI Search stores
the index. Permission metadata (allowed_groups) travels with every chunk.
Run:  python -m app.knowledge.ingest            (local index)
      python -m app.knowledge.ingest --azure    (push to Azure AI Search; untested adapter)
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import yaml

from ..config import settings
from ..llm import embed

MAX_CHARS = 2400  # ~600 tokens


def parse(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    meta, body = yaml.safe_load(m.group(1)), m.group(2)
    return meta, body


def chunk(meta: dict, body: str) -> list[dict]:
    sections: list[tuple[str, str]] = []
    current_h2, current_title, buf = "", "", []
    for line in body.splitlines():
        h = re.match(r"^(#{1,3})\s+(.*)", line)
        if h:
            if buf and "".join(buf).strip():
                sections.append((current_title, "\n".join(buf).strip()))
            buf = []
            level, text = len(h.group(1)), h.group(2).strip()
            if level == 1:
                current_title = ""
            elif level == 2:
                current_h2, current_title = text, text
            else:
                current_title = f"{current_h2} > {text}"
        else:
            buf.append(line)
    if buf and "".join(buf).strip():
        sections.append((current_title, "\n".join(buf).strip()))

    chunks = []
    for i, (sec, text) in enumerate(sections):
        parts = [text] if len(text) <= MAX_CHARS else [text[j:j + MAX_CHARS] for j in range(0, len(text), MAX_CHARS - 240)]
        for k, part in enumerate(parts):
            num = re.match(r"^(\d+(?:\.\d+)?)", sec.split(">")[-1].strip())
            chunks.append({
                "chunk_id": f"{meta['doc_id']}#{i + 1}.{k + 1}",
                "doc_id": meta["doc_id"], "title": meta["title"], "section": sec,
                "section_no": num.group(1) if num else "",
                # contextual header improves retrieval of short sections
                "text": f"{meta['title']} - {sec}\n{part}" if sec else f"{meta['title']}\n{part}",
                "domain": meta["domain"], "owner": meta["owner"], "effective_date": str(meta["effective_date"]),
                "version": str(meta["version"]), "language": meta["language"],
                "allowed_groups": list(meta["allowed_groups"]), "sensitivity": meta["sensitivity"],
            })
    return chunks


def build_local_index() -> Path:
    chunks = []
    for p in sorted(settings.corpus_dir.glob("*.md")):
        meta, body = parse(p)
        chunks.extend(chunk(meta, body))
    t0 = time.time()
    vecs = embed([c["text"] for c in chunks])
    for c, v in zip(chunks, vecs):
        c["vector"] = v
    out = settings.data_dir / "index.json"
    out.write_text(json.dumps({"embed_model": settings.embed_model, "chunks": chunks}, ensure_ascii=False))
    print(f"Indexed {len(chunks)} chunks from {len({c['doc_id'] for c in chunks})} documents "
          f"with {settings.embed_model} in {time.time() - t0:.1f}s -> {out}")
    return out


def push_azure() -> None:  # pragma: no cover - requires Azure
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import (HnswAlgorithmConfiguration, SearchableField, SearchField,
        SearchFieldDataType, SearchIndex, SemanticConfiguration, SemanticField, SemanticPrioritizedFields,
        SemanticSearch, SimpleField, VectorSearch, VectorSearchProfile)

    local = json.loads(build_local_index().read_text())["chunks"]
    dims = len(local[0]["vector"])
    cred = AzureKeyCredential(settings.azure_search_key)
    idx = SearchIndex(
        name=settings.azure_search_index,
        fields=[
            SimpleField(name="id", type=SearchFieldDataType.String, key=True),
            SimpleField(name="chunk_id", type=SearchFieldDataType.String),
            SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True),
            SearchableField(name="title", type=SearchFieldDataType.String),
            SearchableField(name="section", type=SearchFieldDataType.String),
            SearchableField(name="text_en", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
            SearchableField(name="text_ar", type=SearchFieldDataType.String, analyzer_name="ar.microsoft"),
            SimpleField(name="language", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="domain", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="owner", type=SearchFieldDataType.String),
            SimpleField(name="effective_date", type=SearchFieldDataType.String),
            SimpleField(name="version", type=SearchFieldDataType.String),
            SimpleField(name="sensitivity", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="section_no", type=SearchFieldDataType.String),
            SearchField(name="allowed_groups", type=SearchFieldDataType.Collection(SearchFieldDataType.String), filterable=True),
            SearchField(name="vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single), searchable=True,
                        vector_search_dimensions=dims, vector_search_profile_name="hnsw"),
        ],
        vector_search=VectorSearch(algorithms=[HnswAlgorithmConfiguration(name="hnsw-alg")],
                                   profiles=[VectorSearchProfile(name="hnsw", algorithm_configuration_name="hnsw-alg")]),
        semantic_search=SemanticSearch(configurations=[SemanticConfiguration(name="default", prioritized_fields=
            SemanticPrioritizedFields(title_field=SemanticField(field_name="title"),
                                      content_fields=[SemanticField(field_name="text_en"), SemanticField(field_name="text_ar")]))]),
    )
    SearchIndexClient(settings.azure_search_endpoint, cred).create_or_update_index(idx)
    docs = [{**{k: c[k] for k in ("chunk_id", "doc_id", "title", "section", "language", "domain", "owner", "effective_date",
                                    "version", "sensitivity", "section_no", "allowed_groups", "vector")},
             "id": re.sub(r"[^A-Za-z0-9_-]", "_", c["chunk_id"]),
             "text_en": c["text"] if c["language"] == "en" else "", "text_ar": c["text"] if c["language"] == "ar" else ""}
            for c in local]
    SearchClient(settings.azure_search_endpoint, settings.azure_search_index, cred).upload_documents(docs)
    print(f"Uploaded {len(docs)} chunks to Azure AI Search index '{settings.azure_search_index}'")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--azure", action="store_true")
    a = ap.parse_args()
    push_azure() if a.azure else build_local_index()
