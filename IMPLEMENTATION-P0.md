# P0 实施手册:Terminal Per-Call Backend Selection

| | |
|---|---|
| **配套设计** | `DESIGN-terminal-multi-backend.md` (rev.3) |
| **范围** | P0 —— agent 能 `terminal(backend="gpu")` 端到端跑通;多 backend 实例并存;allowlist + 运行环境过滤 |
| **不含** | 风险模型/审批 UX/管理 skill/enum 热刷新(→ P1/P2) |
| **估时** | ~4.5 人天 |

---

## 0. 前置:隔离与验证策略

- **隔离**:在独立 worktree 或新分支实施,避免污染当前工作分支(已有未提交改动)。
- **验证分层**:
  1. 实施方:`python -m py_compile`(语法)+ 现有单元测试(`tests/tools/test_terminal_*.py`)。
  2. 集成方:在 docker 环境实测(`docker compose -f docker-compose.windows.yml build && up`),手测 `terminal(backend="gpu")`。
- **回退**:每步独立 commit,任一步回归可 `git revert`。

---

## 1. 改动总览

| 文件 | 改动 | 估时 |
|------|------|------|
| `tools/terminal_tool.py` | 缓存 key 升级 + 仲裁 + backend 参数 + 配置表 | 2.5d |
| `tools/process_registry.py` | `ProcessSession.backend` + spawn 记录 | 0.5d |
| `hermes_cli/config.py` | 暴露 `terminal.backends` 给 terminal_tool | 0.5d |
| `agent/prompt_builder.py` | backend 摘要注入 system prompt | 0.3d |
| 测试 | P0 子集 | 1d |

---

## 2. 步骤详解

### 步骤 1 — 缓存 key 升级(纯重构,必须先独立绿)

**目标**:`_active_environments` 等 dict 的 key 从 `str(task_id)` 升级为 `tuple(collapsed_task_id, backend_name)`,P0 先固定所有 backend 为 `"default"`,行为等价现状。

**改动点**(`tools/terminal_tool.py`):

| 行号 | 改动 |
|------|------|
| `:982-986` | 类型标注 `Dict[str, ...]` → `Dict[tuple, ...]` |
| `:1100-1111` | `register_task_env_overrides` cwd 同步:`_active_environments.get(task_id)` → `.get((task_id, "default"))`(P0) |
| `:1559-1565` | idle 回收:`for task_id in _last_activity` → tuple key |
| `:1638-1642` | `get_active_env`:返回 tuple 查找 |
| `:1664-1718` | `cleanup_all_environments` / per-task cleanup:tuple key |
| `:2152-2155` | execute `_existing_key` 查找 |
| `:2167` | `_creation_locks[effective_task_id]` |
| `:2173-2176` | 双查 |
| `:2241-2242` | `_active_environments[effective_task_id] = new_env` |

**验收**:全量 `tests/tools/test_terminal_*.py` 绿;行为等价现状。

### 步骤 2 — 配置层:命名 backends 表

新增(terminal_tool.py):
```python
def _load_backends_config() -> tuple[dict, str]:
    """Return (backends_map, default_backend_name). yaml > legacy env."""
    from hermes_cli.config import get_loaded_config
    raw = (get_loaded_config() or {}).get("terminal", {})
    by = raw.get("backends")
    if isinstance(by, dict) and by:
        return _normalize_backends(by), raw.get("default_backend", "default")
    return _legacy_env_as_backends(_get_env_config()), "default"

def _filter_by_runtime(backends: dict) -> dict:
    if not is_inside_container():       # hermes_constants.py:858
        return backends
    return {n: b for n, b in backends.items()
            if b["env"] not in ("docker", "singularity") or b.get("dinD_acknowledged")}
```
- `_normalize_backends`:校验 `env`、`key` 相对 HERMES_HOME、默认 `risk_level`/`approval`。
- `_legacy_env_as_backends`:把 `TERMINAL_ENV/SSH_HOST/...` 包成单条 `"default"`(现状兼容)。
- `_get_env_config`(`:1256`)**保留**,作降级路径。
- `hermes_cli/config.py`:确认 `get_loaded_config()` 入口;`terminal.backends` 子表已随 `terminal` 段加载(`:6639`)。

### 步骤 3 — 仲裁函数 `resolve_env_key`

```python
_ISOLATION_KEYS = frozenset({
    "docker_image","modal_image","singularity_image","daytona_image","env_type","backend",
})

def resolve_env_key(task_id, backend_arg, *, backends, default_backend,
                    delegate_backends=None) -> tuple[tuple, str]:
    collapsed = _resolve_container_task_id(task_id)
    overrides = resolve_task_overrides(task_id)
    if overrides and (set(overrides) & _ISOLATION_KEYS):           # L1 锁定
        locked = overrides.get("backend") or _derive_from_env_type(overrides)
        return (collapsed, locked), locked
    if delegate_backends is not None:                               # L1.5 delegate
        chosen = backend_arg or default_backend
        if chosen not in delegate_backends:
            raise BackendRejected(chosen, delegate_backends, "not authorized by parent")
        return (collapsed, chosen), chosen
    chosen = backend_arg or default_backend                         # L2 agent + allowlist
    if chosen not in backends:
        raise BackendRejected(chosen, list(backends))
    return (collapsed, chosen), chosen
```
`_ISOLATION_KEYS`(`:1147`)加 `"backend"`。新增 `class BackendRejected(Exception)`。

### 步骤 4 — execute 接入

**签名**(`terminal_tool`,`:2020` 附近):加 `backend=None, delegate_backends=None`。

**主体**(`:2064`):
```python
backends, default_backend = _filter_by_runtime(_load_backends_config())
try:
    (cache_key, backend_name) = resolve_env_key(
        task_id, backend, backends=backends, default_backend=default_backend,
        delegate_backends=delegate_backends)
except BackendRejected as e:
    return json.dumps({"output":"", "exit_code":-1, "error": str(e), "status":"error"})
bcfg = backends[backend_name]
env_type = bcfg["env"]
```
- 所有 `effective_task_id`(缓存 key)→ `cache_key`。
- `ssh_config`/`container_config`/`image`(`:2082-2213`)从 `bcfg` 取,而非全局 `config.get`。

**里程碑**:此步后 `terminal(backend="gpu")` 端到端跑通。

### 步骤 5 — process_registry 记录 backend

- `ProcessSession`(`:91`):加 `backend: str = "default"`。
- `spawn_via_env`(`:821`)/ `spawn_local`(`:682`):签名加 `backend_name`,写入 session。
- terminal_tool 调 spawn 时传当前 `backend_name`。
- read/close 无逻辑改动(session 按 session_id 路由,backend 字段供审计)。

### 步骤 6 — schema + dispatch

- `TERMINAL_SCHEMA`(`:2927`):加 `backend` property,`enum` 启动时从 `_filter_by_runtime(backends)` 静态生成(P0;热刷新 P2)。
- `_handle_terminal`(`:2972`):传 `backend=args.get("backend")`、`delegate_backends=kw.get("delegate_backends")`。

### 步骤 7 — system prompt 摘要

`agent/prompt_builder.py`(已有 platform hints 机制,`:860,1137`):注入 backend 名 + description + risk 列表,教育 agent 何时用哪个。

---

## 3. 实施顺序(依赖链)

```
1. key 升级 → 回归必须绿
2. _load_backends_config + _filter_by_runtime
3. resolve_env_key + BackendRejected
4. execute 接入(2+3 合进)→ 手测 terminal(backend=...) 跑通
5. process_registry 记 backend
6. schema + _handle_terminal
7. prompt 摘要
8. P0 测试子集
```

每步独立 commit。**步骤 1 必须先绿**。

---

## 4. P0 测试子集

- [ ] 仅旧环境变量(无 `backends` 表)→ 行为同现状(回归)
- [ ] `terminal(backend="evil")` → `BackendRejected`,不连主机
- [ ] RL override 锁定 → agent `backend` 被忽略
- [ ] `(default,gpu)` 与 `(default,this)` 缓存独立(各自 cwd)
- [ ] read/close 跨 backend 按 session_id 路由
- [ ] 容器内运行 → docker/singularity 从 enum 移除
- [ ] delegate `delegate_backends=["gpu"]`,选 `prod-db` → 拒绝(P0.5 接入 delegate 后)
- [ ] 返回值含 `backend` + `cwd`

---

## 5. 风险

1. **key 升级面积最大** —— 必须先独立 + 回归绿,再叠 backend。
2. `_get_env_config` 多处调用 —— 保留它,新增 `_load_backends_config`,避免大爆炸。
3. **delegate 层**:P0 可先返回 `None`(不限制),`delegate_backends` 接入放 P0.5,避免阻塞主干。
4. **enum 静态**:P0 新 backend 需新会话可见。
5. **无法在实施机跑 hermes 集成测试** —— py_compile + 单测 + 集成方 docker 实测。
