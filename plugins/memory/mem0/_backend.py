"""Backend abstraction for Mem0 Platform and OSS modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Mem0Backend(ABC):
    """Unified interface over Platform (MemoryClient) and OSS (Memory) backends."""

    @abstractmethod
    def search(self, query: str, *, filters: dict, top_k: int = 10, rerank: bool = True) -> list[dict]:
        ...

    @abstractmethod
    def get_all(self, *, filters: dict, page: int = 1, page_size: int = 100) -> dict:
        ...

    @abstractmethod
    def add(
        self,
        messages: list,
        *,
        user_id: str,
        agent_id: str,
        infer: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        ...

    @abstractmethod
    def update(self, memory_id: str, text: str) -> dict:
        ...

    @abstractmethod
    def delete(self, memory_id: str) -> dict:
        ...

    def close(self) -> None:
        pass


def _unwrap_results(response: Any) -> list:
    """Normalize API response — extract results list from dict or pass through."""
    if isinstance(response, dict):
        return response.get("results", [])
    if isinstance(response, list):
        return response
    return []


class PlatformBackend(Mem0Backend):
    """Wraps mem0.MemoryClient for Mem0 Platform (cloud API)."""

    def __init__(self, api_key: str):
        from mem0 import MemoryClient
        self._client = MemoryClient(api_key=api_key)

    def search(self, query: str, *, filters: dict, top_k: int = 10, rerank: bool = True) -> list[dict]:
        response = self._client.search(query, filters=filters, top_k=top_k, rerank=rerank)
        return _unwrap_results(response)

    def get_all(self, *, filters: dict, page: int = 1, page_size: int = 100) -> dict:
        response = self._client.get_all(filters=filters, page=page, page_size=page_size)
        results = response.get("results", []) if isinstance(response, dict) else response
        count = response.get("count", len(results)) if isinstance(response, dict) else len(results)
        return {"results": results, "count": count}

    def add(
        self,
        messages: list,
        *,
        user_id: str,
        agent_id: str,
        infer: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {"user_id": user_id, "agent_id": agent_id, "infer": infer}
        if metadata:
            kwargs["metadata"] = metadata
        return self._client.add(messages, **kwargs)

    def update(self, memory_id: str, text: str) -> dict:
        self._client.update(memory_id=memory_id, text=text)
        return {"result": "Memory updated.", "memory_id": memory_id}

    def delete(self, memory_id: str) -> dict:
        self._client.delete(memory_id=memory_id)
        return {"result": "Memory deleted.", "memory_id": memory_id}


class OSSBackend(Mem0Backend):
    """Wraps mem0.Memory for self-hosted (OSS) mode."""

    def __init__(self, oss_config: dict):
        import os
        from mem0 import Memory

        vector_store = dict(oss_config["vector_store"])
        vs_config = dict(vector_store.get("config", {}))

        if "path" in vs_config:
            vs_config["path"] = os.path.expanduser(vs_config["path"])

        embedder_config = oss_config.get("embedder", {}).get("config", {})
        dims = embedder_config.get("embedding_dims")
        if not dims:
            from ._oss_providers import KNOWN_DIMS
            model = embedder_config.get("model", "")
            dims = KNOWN_DIMS.get(model)
        if dims:
            vs_config["embedding_model_dims"] = dims
            self._recreate_collection_if_dims_changed(
                vector_store.get("provider", "qdrant"), vs_config, dims,
            )

        vector_store["config"] = vs_config

        config = {
            "vector_store": vector_store,
            "llm": oss_config["llm"],
            "embedder": oss_config["embedder"],
            "version": "v1.1",
        }
        self._memory = Memory.from_config(config)

    @staticmethod
    def _recreate_collection_if_dims_changed(provider: str, vs_config: dict, expected_dims: int) -> None:
        """Delete stale vector collection when embedding dimensions change."""
        collection_name = vs_config.get("collection_name", "mem0")
        if provider == "qdrant":
            try:
                from qdrant_client import QdrantClient
                path = vs_config.get("path")
                url = vs_config.get("url")
                if path:
                    client = QdrantClient(path=path)
                elif url:
                    client = QdrantClient(url=url, api_key=vs_config.get("api_key"))
                else:
                    return
                try:
                    if not client.collection_exists(collection_name):
                        return
                    info = client.get_collection(collection_name)
                    vectors = info.config.params.vectors
                    # Named-vector collections expose a dict; unnamed expose an object with .size.
                    if isinstance(vectors, dict):
                        first = next(iter(vectors.values()), None)
                        current_dims = first.size if first else None
                    else:
                        current_dims = getattr(vectors, "size", None)
                    if current_dims is not None and current_dims != expected_dims:
                        client.delete_collection(collection_name)
                finally:
                    client.close()
            except Exception:
                pass
        elif provider == "pgvector":
            try:
                import psycopg2
                from psycopg2 import sql as pgsql
                conn_params = {}
                for k in ("host", "port", "user", "password", "dbname"):
                    if vs_config.get(k):
                        conn_params[k] = vs_config[k]
                if vs_config.get("sslmode"):
                    conn_params["sslmode"] = vs_config["sslmode"]
                conn = psycopg2.connect(**conn_params)
                conn.autocommit = True
                try:
                    cur = conn.cursor()
                    try:
                        cur.execute(
                            "SELECT atttypmod FROM pg_attribute "
                            "WHERE attrelid = %s::regclass AND attname = 'vector'",
                            (collection_name,),
                        )
                        row = cur.fetchone()
                        if row and row[0] > 0 and row[0] != expected_dims:
                            cur.execute(pgsql.SQL("DROP TABLE IF EXISTS {}").format(
                                pgsql.Identifier(collection_name)
                            ))
                    finally:
                        cur.close()
                finally:
                    conn.close()
            except Exception:
                pass

    def search(self, query: str, *, filters: dict, top_k: int = 10, rerank: bool = True) -> list[dict]:
        response = self._memory.search(query, filters=filters, top_k=top_k)
        return _unwrap_results(response)

    def get_all(self, *, filters: dict, page: int = 1, page_size: int = 100) -> dict:
        response = self._memory.get_all(filters=filters)
        all_results = _unwrap_results(response)
        total = len(all_results)
        start = (page - 1) * page_size
        results = all_results[start : start + page_size]
        return {"results": results, "count": total}

    def add(
        self,
        messages: list,
        *,
        user_id: str,
        agent_id: str,
        infer: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {"user_id": user_id, "agent_id": agent_id, "infer": infer}
        if metadata:
            kwargs["metadata"] = metadata
        return self._memory.add(messages, **kwargs)

    def update(self, memory_id: str, text: str) -> dict:
        self._memory.update(memory_id, data=text)
        return {"result": "Memory updated.", "memory_id": memory_id}

    def delete(self, memory_id: str) -> dict:
        self._memory.delete(memory_id)
        return {"result": "Memory deleted.", "memory_id": memory_id}

    def close(self):
        try:
            telemetry = getattr(self._memory, "telemetry", None)
            if telemetry and hasattr(telemetry, "posthog"):
                try:
                    telemetry.posthog.shutdown()
                except Exception:
                    pass
            if hasattr(self._memory, "close"):
                self._memory.close()
            vs = getattr(self._memory, "vector_store", None)
            if vs and hasattr(vs, "close"):
                vs.close()
            client = getattr(vs, "client", None)
            if client and hasattr(client, "close"):
                client.close()
        except Exception:
            pass


class SelfHostBackend(Mem0Backend):
    """Talks to a self-hosted mem0 REST server (mem0/server/main.py) over HTTP.

    PlatformBackend uses mem0.MemoryClient, which speaks the Mem0 Cloud
    protocol (``Authorization: Token`` header plus a key-validation round
    trip) that a self-hosted OSS server does not implement. This backend
    instead speaks the OSS server's native REST API — ``X-API-Key`` header
    against the ``/memories`` and ``/search`` endpoints — so hermes uses a
    self-hosted mem0 server directly.

    Activate with ``mode: "selfhost"`` plus ``host`` and ``api_key`` in
    mem0.json (or MEM0_HOST / MEM0_API_KEY env). The api_key is an X-API-Key
    issued by the self-hosted server (m0sk_...), NOT a Mem0 Platform key.
    """

    def __init__(self, host: str, api_key: str, timeout: float = 30.0):
        if not host:
            raise ValueError(
                "selfhost mode requires 'host' (the mem0 server base URL, e.g. "
                "http://host.docker.internal:9112) in mem0.json or MEM0_HOST."
            )
        if not api_key:
            raise ValueError(
                "selfhost mode requires 'api_key' — an X-API-Key issued by the "
                "self-hosted mem0 server (m0sk_...), set in mem0.json or MEM0_API_KEY."
            )
        import httpx

        self._http = httpx.Client(
            base_url=host.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    def _check(self, resp) -> None:
        """Raise on HTTP errors; surface status + detail so hermes's
        client-error detection (404 / 'not found') keeps benign not-found
        responses from tripping the circuit breaker."""
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise RuntimeError(f"mem0 server returned {resp.status_code}: {detail}")

    def search(self, query: str, *, filters: dict, top_k: int = 10, rerank: bool = True) -> list[dict]:
        # rerank is a Platform-only feature; the OSS server has no such step.
        resp = self._http.post("/search", json={"query": query, "filters": filters, "top_k": top_k})
        self._check(resp)
        return _unwrap_results(resp.json())

    def get_all(self, *, filters: dict, page: int = 1, page_size: int = 100) -> dict:
        params = {k: v for k, v in filters.items() if v}
        resp = self._http.get("/memories", params=params)
        self._check(resp)
        data = resp.json()
        results = _unwrap_results(data)
        count = data.get("count", len(results)) if isinstance(data, dict) else len(results)
        return {"results": results, "count": count}

    def add(
        self,
        messages: list,
        *,
        user_id: str,
        agent_id: str,
        infer: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "messages": messages,
            "user_id": user_id,
            "agent_id": agent_id,
            "infer": infer,
        }
        if metadata:
            body["metadata"] = metadata
        resp = self._http.post("/memories", json=body)
        self._check(resp)
        return resp.json()

    def update(self, memory_id: str, text: str) -> dict:
        resp = self._http.put(f"/memories/{memory_id}", json={"text": text})
        self._check(resp)
        return {"result": "Memory updated.", "memory_id": memory_id}

    def delete(self, memory_id: str) -> dict:
        resp = self._http.delete(f"/memories/{memory_id}")
        self._check(resp)
        return {"result": "Memory deleted.", "memory_id": memory_id}

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass
