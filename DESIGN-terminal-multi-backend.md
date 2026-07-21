# Terminal Multi-Backend: Per-Call Backend Selection

| | |
|---|---|
| **Status** | 📝 Draft / Proposal — **尚未实施** |
| **Date** | 2026-07-17 (rev.3 — 开放问题 Q1–Q5 拍板) |
| **Authors** | esp3j0 |
| **Related code** | `tools/terminal_tool.py`, `tools/environments/`, `tools/approval.py`, `tools/credential_files.py`, `tools/process_registry.py`, `tools/{read,close}_terminal_tool.py`, `tools/delegate_tool.py`, `tools/skills_guard.py`, `hermes_constants.py` |

---

## TL;DR

让 agent 在**每次调用** terminal 工具时选择命令运行的目标 backend(本机 / 多台 SSH 主机 / Docker / 云沙箱),从而**原生编排多台机器**,而不是在 `command` 字符串里手写 `ssh host "..."` 并忍受双层转义地狱。

工程成本低(现有 `BaseEnvironment` 抽象 + `credential_files` 校验 + `set_config_value` 持久化 + `is_inside_container()` 检测均已就位),核心工作量在**配置层重构、安全模型(allowlist + 字段级权限 + 写操作强制审批)、缓存 key 升级、运行环境过滤、一个管理 skill**。

> **安全性的限定前提**:本方案相对现状是净提升 —— **仅针对「原本就在命令里手写 ssh 跨机」的场景**。对**从不跨机**的用户,本方案是纯增攻击面(agent 可改 config),须靠 §4.5/§4.6 的审批/allowlist/字段权限对冲。

---

## 1. 背景与动机

### 1.1 现状

Terminal tool 的 backend 由**进程级环境变量**在启动前固定(`tools/terminal_tool.py:1256` `_get_env_config`):

- `TERMINAL_ENV`(env_type:local/docker/ssh/singularity/modal/daytona)
- `TERMINAL_SSH_HOST/USER/PORT/KEY`(**仅一组** SSH 目标)
- `TERMINAL_DOCKER_IMAGE` 等

整个进程只能有一个 active backend。要跨机,agent 只能在 `command` 里手写 `ssh ...`。

### 1.2 痛点

1. **转义地狱**:`ssh gpu "kubectl logs $(pod) 2>&1 | grep 'err'"` 要过 `agent → JSON arg → 本地 shell → 远程 shell` 四层解析。
2. **安全真空**:命令里嵌的 ssh **完全不经过 backend 安全栈** —— host/key 明文暴露给 LLM、无 per-host 审批、无凭据隔离。
3. **状态割裂**:本机 cwd 与远程 cwd 各自手工维护,agent 易混淆。

### 1.3 机会(现有基础设施)

| 能力 | 位置 | 说明 |
|------|------|------|
| 统一 backend 抽象 | `tools/environments/base.py:290` | 6 种 backend 同一接口 |
| 凭据**校验** | `tools/credential_files.py:57` | 防 `..` 穿越、HERMES_HOME 沙箱(挂载语义仅 sandbox,见 §4.6) |
| config 运行时持久化 | `hermes_cli/config.py:7803` `set_config_value`,`:6829` | 原子写(注:默认**无审批**,见 §4.6) |
| 多实例缓存雏形 | `tools/terminal_tool.py:982` | dict 已可存多实例 |
| per-task env override | `:1073` | 基础设施级锁定先例 |
| 危险命令审批 | `tools/approval.py` `check_all_command_guards` | 已按 `env_type` 分流 |
| background 进程注册 | `tools/process_registry.py` | 按 `session_id`/`pid`(read/close 走它,非缓存) |
| **运行环境检测** | `hermes_constants.py:858` `is_inside_container` | 查 `/.dockerenv`/`/run/.containerenv`;已有用法 `config.py:434` |
| **skill 静态审计** | `tools/skills_guard.py` | trust_level + 危险模式扫描,dangerous 不可 force |
| **subagent 受限 toolset** | `tools/delegate_tool.py:12` | delegate 可限制子代理工具集 |

---

## 2. 目标 / 非目标

### Goals
- G1 agent 在 per-call 粒度选择命名 backend,同一会话可并发操作多机。
- G2 彻底消除跨机命令的转义问题(命令传输由 backend 标准化)。
- G3 每台远程主机的凭据收进 HERMES_HOME 沙箱,**agent 运行时读不到明文**(对用户审批仍可见,见 §4.5)。
- G4 提供管理 skill:用户口述主机/key → hermes 登记并持久化(**写操作强制审批**)。
- G5 不破坏 RL/benchmark 的环境锁定语义。
- G6 容器内运行时自动过滤不可用的 backend(docker/singularity)。

### Non-Goals
- ❌ 跨 backend 的分布式事务/原子性。
- ❌ 连接池/会话复用优化(MVP 完全隔离)。
- ❌ 把 hermes 变成凭据保管库(私钥仍由用户持有)。

---

## 3. 现状架构(基线)

### 3.1 执行模型
`BaseEnvironment`(`base.py:290`):**Spawn-per-call** + **session snapshot**(`base.py:353`)+ **CWD 持久**(`base.py:837`)+ 统一 `execute()`(`base.py:889`)与 `_wait_for_process()`(`base.py:543`)。

### 3.2 六种 backend
- **子进程型**(`subprocess.Popen`):local / docker / ssh / singularity。
- **SDK 型**(`_ThreadedProcessHandle`,`base.py:207`):modal / daytona / managed-modal。

### 3.3 缓存与隔离
`_active_environments: Dict[str, env]`(`terminal_tool.py:982`),key 是折叠后 task_id。`_resolve_container_task_id`(`:1123`):top-level/subagent → `"default"` 共享;含 isolation key(`:1147-1150`)override 的 task → 独立沙箱。idle 回收(`:1559`);`get_active_env`(`:1638`);`cleanup_all_environments`(`:1664`);`_creation_locks`(`:985`)。

### 3.4 安全栈
`check_all_command_guards(command, env_type, approval_callback)`(`approval.py`)—— 已接受 `env_type` 维度;hardline block(`:451`)、gateway lifecycle 阻断(`terminal_tool.py:2246`)、tirith pre-exec、三态审批(`:2277`)、辅助 LLM auto-approve(`:7`)、per-session 审批(`:122`)。

### 3.5 background 命令定位
`terminal(background=true)` 返回 `session_id`,登记在 `process_registry`。`close_terminal_tool.py:30` 用 `process_registry.request_close_terminal(pid)`,`:54` 用 `session_id` —— **read/close 走 process_registry,不走 `_active_environments`**。

### 3.6 subagent(delegate)
子代理 = 全新会话(无父历史)+ 独立 task_id + **受限 toolset**(`delegate_tool.py:12`)+ 聚焦 system prompt;**父只看 summary,看不到中间调用**(`:15-16`);`delegation.subagent_auto_approve` 可配(`:68`)。

### 3.7 skill 执行模型
skill = `SKILL.md` **指令文档**(非可执行脚本,`skills_tool.py:1-67`),经 `skills_guard.py` 静态审计(trust_level:`builtin`/`trusted`/`community`;`builtin` 总信任,其余扫描 `eval`/`subprocess`/`sudo`/injection 话术;**dangerous 的 community/trusted 不可 `--force`**,`skills_guard.py:717`)。skill 让 agent **调工具**执行,本身不是特权代码。

---

## 4. 提议设计

### 4.1 架构概览

```
agent: terminal(command="...", backend="gpu")
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ 1. Backend 解析 + 仲裁  resolve_env_key()                    │
│    → (collapsed_task, backend_name)                         │
│    优先级: override 锁定 > delegate 授权子集 > agent 选择 > default │
│    allowlist + 运行环境过滤(§4.8)                          │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. 缓存查/建  _active_environments[(task, backend)]         │
│    SSH → 本地 ssh -i 引用;sandbox → 挂载                   │
│    background 活跃的 backend → pin,不被回收                 │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. 安全栈  check_all_guards(command, backend_ctx)            │
│    risk_level 作为 auto-approve 输入维度(P1 档)            │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. 统一 execute()(BaseEnvironment,无需改动)                │
│    返回值携带 {backend, cwd, output, returncode}            │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 配置 Schema

```yaml
terminal:
  default_backend: this
  allowed_host_patterns: ["*.lan", "192.168.*", "10.*"]   # 辅助层(§4.5)
  max_concurrent_backends_per_session: 5
  backends:
    this:
      env: local
      description: "本机"
      risk_level: 1                          # 有 env_type/host 下限,§4.5
      approval: high_risk
    gpu:
      env: ssh
      host: 192.168.6.171
      user: yxy
      port: 22
      key: keys/gpu_ed25519                  # 相对 HERMES_HOME
      description: "GPU 训练服务器(双2080Ti)"
      risk_level: 2
      approval: high_risk
      deny: ["systemctl", "shutdown"]
      force_allowed: true
    prod-db:
      env: ssh
      host: db.internal
      user: hermes_ro
      key: keys/prod_db_ed25519
      description: "生产只读查询"
      risk_level: 4
      approval: always
      allow_only: ["pg_dump", "psql"]
      force_allowed: false
    # docker/singularity backend 仅当 hermes 跑在宿主机 + dinD_acknowledged 时可用(§4.8)
    sandbox:
      env: docker
      image: nikolaik/python-nodejs:python3.11-nodejs20
      description: "本地隔离沙箱"
      risk_level: 1
      approval: never
      dinD_acknowledged: false               # 容器内运行时必须 true 才启用
  credential_files:                          # sandbox 凭据挂载(SSH key 不走这里)
    - keys/gcp_token.json
```

> **路径约束**:`key` 必须相对 HERMES_HOME;复用 `register_credential_file`(`credential_files.py:57`)校验。

### 4.3 工具 Schema 变更

**只有 `terminal` 加 `backend` 参数**:

```jsonc
{
  "properties": {
    "command":   { /* 不变 */ },
    "background":{ /* 不变 */ },
    "timeout":   { /* 不变 */ },
    "backend": {
      "type": "string",
      "enum": ["this", "gpu", "prod-db"],     // 从 config 动态生成 + 运行环境过滤(§4.7/4.8)
      "description": "Named backend to run on. Omit for default."
    }
  },
  "required": ["command"]
}
```

**`read_terminal` / `close_terminal` 不加 `backend` 参数**:它们按现有 `session_id`/`pid` 经 `process_registry` 定位(`close_terminal_tool.py:30,54`)。多 backend 下,让 `process_registry` 登记 background session 时**记录所属 backend 名**,read/close 沿用 session_id 路由(不变量 12)。

### 4.4 缓存 Key 与仲裁

**Key 升级**:`task_id` → `(collapsed_task_id, backend_name)`。

```python
_active_environments: Dict[tuple[str, str], BaseEnvironment]
_last_activity:       Dict[tuple[str, str], float]
_creation_locks:      Dict[tuple[str, str], Lock]

_ISOLATION_KEYS = frozenset({
    "docker_image", "modal_image", "singularity_image",
    "daytona_image", "env_type", "backend",
})

def resolve_env_key(task_id, backend_arg, *, allowlist, delegate_backends=None):
    collapsed = _resolve_container_task_id(task_id)
    overrides = resolve_task_overrides(task_id)
    # L1: 基础设施锁定(RL/benchmark)— backend 维度塌缩
    if overrides and (set(overrides) & _ISOLATION_KEYS):
        locked = overrides.get("backend") or _derive_from_env_type(overrides)
        if backend_arg and backend_arg != locked:
            logger.warning("task %s locked to %s; ignoring %r", task_id, locked, backend_arg)
        return (collapsed, locked), locked
    # L1.5: delegate 授权子集(subagent 场景)
    if delegate_backends is not None:
        backend = backend_arg or get_default_backend()
        if backend not in delegate_backends:
            raise BackendRejected(backend, delegate_backends, reason="not authorized by parent")
        return (collapsed, backend), backend
    # L2: agent 选择
    backend = backend_arg or get_default_backend()
    if backend not in allowlist:
        raise BackendRejected(backend, allowlist)
    return (collapsed, backend), backend
```

**仲裁优先级**(rev.3 四层):

| 场景 | 锁定源 | agent `backend=` | 结果 |
|------|--------|------------------|------|
| RL/benchmark | override isolation key | 任意 | override 锁定,拒绝改 |
| subagent | 父 delegate `allowed_backends` 子集 | 必须在子集内 | 否则拒绝(不变量 13) |
| 普通会话 | 无 | `"gpu"` | gpu |
| 普通会话 | 无 | 无 | default |

**Subagent backend 委派(Q1 决策,rev.3)**:subagent **不自由选 backend**。父 agent 在 delegate 时通过 `allowed_backends` 参数授权一个子集(类比现有受限 toolset,`delegate_tool.py:12`)。理由:子代理处理不可信输入且父看不到其中间调用(`:15-16`),自由选会导致 ① 不可信输入放大攻击面 ② 审计断链 ③ capability 越权 ④ 实例资源爆炸。未授权子代理用 default。

**连锁升级**:`_creation_locks`/idle 回收/`get_active_env`/`cleanup_all_environments` 全部 tuple key;cwd-only override 同步(`:1100-1111`)**仅 default backend**。

**Background pin(防 LRU 误杀)**:回收器淘汰 `(task, backend)` 前查 `process_registry` 是否有活跃 background session;有则跳过(不变量 11)。

### 4.5 安全模型(纵深防御 4 层)

#### L1 — Backend 选择层
- **Allowlist 枚举 + 运行时双校验**:结构性拒绝,不靠 LLM。
- **凭据可见性边界**:`host/user/port/key` 对 **agent/LLM 不可见**(不进 schema 默认值/system prompt/tool result/日志明文),对 **用户可见**(审批 UI、审计日志)。两通道分离(不变量 2/10)。

#### L2 — 命令层
`check_all_command_guards(command, env_type, ...)` → 泛化 `backend_ctx`(`env_type + risk_level + deny + allow_only`)。per-backend `approval`/`deny`/`allow_only`;hardline 与 gateway lifecycle 跨所有 backend 保留。

#### 风险量化:P1 档(Q5 决策,rev.3 定档)
**既定方案 P1**(改动小):不做乘积运算。`backend.risk_level` 作为**辅助 LLM auto-approve 的输入维度**(auto-approve prompt 告知"目标 backend risk=N"),由 LLM 综合判断;`approval: always` 的 backend 直接绕过 auto-approve 强制人工。`risk_level` 有 **env_type/host 推断下限**(如 ssh 到非 RFC1918 地址 → ≥3),防低报。

| command \ backend | this (1) | sandbox (1) | gpu (2) | prod (4) |
|-------------------|----------|-------------|---------|----------|
| `ls/cat` | auto | auto | auto | auto |
| `pip install` | auto | auto | 人工 | 人工 |
| `rm/build` | 人工 | auto | 人工+确认主机 | hardline/双确认 |

(P2 数值化 `command_risk` 做乘积,列为 P4 后续。)

#### L3 — 连接/凭据层
SSH 专用 key + restricted shell / `ForceCommand`;Docker/Singularity 沿用 `docker.py:571`;可选 bastion。

#### L4 — 审计与审批 UX
审批提示标注目标主机:「将运行在 `gpu`(ssh://yxy@192.168.6.171,risk=2),非本机」。per-backend 审计日志。

#### `allowed_host_patterns` 是辅助层非强隔离
IP 直连/DNS rebinding/短名解析可绕过;真正边界是 L1 allowlist + L2 审批 + L3 restricted shell。

### 4.6 管理 Skill(读写分离 + 写操作强制审批)

**核心原则:agent 是「配置登记员」,不是「凭据保管员」。**

**私钥处理(SSH)**:用户自备 key,skill 负责「复制进 `~/.hermes/keys/` + 校验 0600 + 登记相对路径」。SSH backend 用 `ssh -i <HERMES_HOME/keys/...>` **本地引用,不挂载**(挂载是给 docker/modal sandbox 凭据的)。复用 `register_credential_file` 的**校验逻辑**(防穿越/沙箱/权限),不是挂载动作。

**写操作强制审批(Q3 决策,rev.3 关键)**:
skill 是被 `skills_guard` 审计的**指令文档**,不是特权代码(§3.7)。它让 agent 调工具执行。**但 `set_config_value`(`config.py:7803`)默认无审批** —— 因此 multi-backend 的**每一步写操作(复制 key、写 config、删 backend)必须显式触发 `pending_approval`**,在 `set_config_value` 之上叠加审批门。**不能因"管理 skill 是 builtin 可信"就放行**(安全靠审批闸,不靠 skill 可信度)。否则 prompt-injection 诱导加载一个 skill 即可绕过 L1。

**工作流**(每步写操作过审批):

| 步骤 | 动作 | 安全关卡 |
|------|------|----------|
| 1 | 校验用户提供 key 存在 + 权限 0600 | 不合规拒绝 |
| 2 | 复制进 `~/.hermes/keys/<name>` | `validate_within_dir` |
| 3 | host allowlist + risk_level 下限校验 | 不匹配/低于下限拒绝 |
| 4 | **审批**:「将添加 backend `gpu` → ssh://yxy@192.168.6.171?」 | `pending_approval`,显示完整 host |
| 5 | 用户批准 → `set_config_value` 原子写 | 审计 |
| 6 | 注册进 allowlist,热刷新 enum(§4.7) | — |
| 7 | 连通性探测(`init_session`) | 失败给清晰错误 |

配套 `/remove-backend`(清 config + 安全删 key 副本 + 销毁实例,**同样过审批**)、`/test-backend`。

**配置字段权限矩阵**:

| 字段 | 程序化可写(经审批) | 说明 |
|------|--------------------------|------|
| `name, description, host, user, port, key路径` | ✅ | 登记信息 |
| `risk_level` | ✅ 但不低于 env_type/host 下限 | §4.5 |
| `approval, force_allowed` | ✅ **单调收紧** | `never→always`/`true→false` OK,反向拒绝 |
| `default_backend` | ⚠️ 仅同级或更低 risk | — |
| `dinD_acknowledged` | ⚠️ 仅 `false→true` 且需风险声明 | §4.8 |
| 全局安全开关、其他 backend 的 key | ❌ | — |

> **单调收紧仅程序化路径有效**:API/skill 写入时强制;用户手编 `config.yaml` 是信任边界,启动检查仅告警。

### 4.7 Agent 可见性与状态反馈

**Enum 热刷新(Q2 决策,rev.3 定)**:管理 skill 增删 backend 后**立即重发 tool schema** 刷新 `terminal.backend` 的 enum,**接受 prompt cache 失效代价**。只在增删时触发,不每条命令刷。

**System prompt 集成(必需)**:各 backend 的 `name + description + risk` 摘要注入 system prompt,教育 agent 何时用哪个。无此集成 agent 倾向只用 default。

**返回值携带 backend**:每条结果附 `{"backend": "gpu", "cwd": "/data", ...}`。

### 4.8 运行环境与 backend 可用性过滤(Q4 决策,rev.3 新增)

hermes 检测自身运行环境(`hermes_constants.py:858` `is_inside_container`,查 `/.dockerenv`/`/run/.containerenv`;已有用法 `config.py:434` 「容器内不视为 docker」)。

**容器内运行时**:
- `docker` / `singularity` backend 默认**隐藏 + 启动告警**。容器内跑这些 backend 需 Docker-in-Docker 或挂载 `docker.sock` —— 后者**等同宿主 root**(能起任意容器、挂任意宿主目录),严重风险,**默认禁**。
- 显式启用需 `dinD_acknowledged: true`(用户书面接受 docker.sock 风险)。
- **你的部署正是此场景**:hermes 跑在 `docker-compose.windows.yml` 容器内,天然只用 `local`(容器内 bash)+ `ssh`(连 gpu);`backends` 表里即使写了 docker,容器内也被过滤、enum 不含它。

过滤发生在 schema 生成时:enum 只列「运行环境允许 + allowlist 内」的 backend(不变量 15)。

---

## 5. 数据流

### 5.1 添加 backend(管理 skill)
```
用户口述 → 校验 key 权限 → 复制进 HERMES_HOME
  → host allowlist + risk 下限 → 审批(pending_approval,显示 host)   [强制,§4.6]
  → 用户批准 → set_config_value 原子写 → 热刷新 enum
  → 运行环境过滤(§4.8)→ 连通性探测 → 回执
```

### 5.2 执行命令
```
terminal(backend="gpu")
  → resolve_env_key: (default, gpu)        [仲裁:override > delegate > agent > default]
  → 运行环境过滤(gpu=ssh,允许)
  → _active_environments[(default, gpu)] hit?
       miss → _create_environment + (SSH: ssh -i / sandbox: 挂载) → 存入
  → check_all_guards(cmd, gpu_ctx)          [P1:risk 作 auto-approve 输入]
  → env.execute(cmd)
  → 更新 _last_activity[(default, gpu)]
  → 返回 {backend:"gpu", cwd, output, returncode}
```

---

## 6. 不变量(验收基线)

1. **Allowlist 不可被指令绕过**:`backend` 不在 enum → 结构性拒绝。
2. **凭据对 agent 不可见**:`host/key` 不进 agent 的 system prompt / tool result;对用户审批 UI 可见(两通道)。
3. **override 锁定优先**:含 isolation key 的 task,agent `backend` 被忽略/拒绝。
4. **default_backend 最低 risk**。
5. **高风险 backend 不可 force 跳过 hardline**。
6. **读写分离**:agent 登记路径但读不到私钥明文。
7. **approval/force 单调收紧(程序化路径)**。
8. **私钥强制 0600 + HERMES_HOME 沙箱**。
9. **新增 backend 默认最高风险**(`approval: always`)。
10. **审批决策携带目标主机**(对用户通道)。
11. **Background pin**:活跃 background session 的 backend 不被回收。
12. **process_registry 记录 backend**:read/close 按 session_id 正确路由。
13. **Delegate backend 委派**(rev.3):subagent 只能选父 `allowed_backends` 子集;越权拒绝。
14. **写操作强制审批**(rev.3):backend 增删改 + key 复制/删除必经 `pending_approval`,**含 builtin skill**。
15. **运行环境过滤**(rev.3):`is_inside_container()` 为真时 docker/singularity 默认从 enum 移除(除非 `dinD_acknowledged: true`)。

---

## 7. 风险与缓解

| 风险 | 严重度 | 缓解 |
|------|--------|------|
| Injection 添加恶意 backend | 高 | 写必审批 + host allowlist + 字段权限 |
| Injection 放宽安全(`always→never`) | 高 | 字段单调收紧 |
| **skill 绕过审批**(builtin 信任滥用) | 高 | 写操作强制审批(不变量 14),不信任 builtin |
| 私钥泄漏 | 高 | HERMES_HOME 沙箱 + 读写分离 |
| 误审批(prod 当本机) | 中 | 审批 UX 标注主机 |
| 并发改 config lost-update | 中 | 配置级锁/版本号 |
| backend 实例过多 | 中 | cap + LRU |
| LRU 误杀 background | 中 | Background pin |
| **subagent 越权选 backend** | 中 | delegate `allowed_backends` 子集(不变量 13) |
| **docker.sock 挂载 = 宿主 root** | 高 | 容器内默认禁 docker/singularity(不变量 15) |
| enum 漂移破坏 prompt cache | 中 | 接受(Q2 决策),仅增删时刷 |
| risk_level 低报 | 中 | env_type/host 下限 + 新增默认 always |

---

## 8. 迁移与兼容性

- **环境变量降级**:仅设旧变量、无 `backends` 表 → 合成单 backend `default`,行为同现状。
- **现有 task override 不变**:新增 `backend` 作 isolation key。
- **RL/benchmark 零改动**:L1 仲裁保护。
- **容器内部署**:docker/singularity 自动过滤,不影响 local/ssh(你的场景)。
- **config schema 版本号**:`terminal.schema_version`,旧 config 自动迁移。
- **回归**:加 backend 维度后单 backend 全套测试须回归(省略 `backend` 时等价现状)。

---

## 9. 分阶段实施

| 阶段 | 范围 |
|------|------|
| **P0** | config schema + `terminal.backend` 参数 + `(task,backend)` 缓存 + 四层仲裁(含 delegate `allowed_backends`)+ allowlist + process_registry 记 backend + 运行环境过滤 |
| **P1** | 风险模型 P1 档 + per-backend 审批策略 + 字段权限 + 审批 UX 标注主机 + Background pin |
| **P2** | 管理 skill(`/add|remove|test-backend`)+ credential 校验接入 + 读写分离 + **写操作强制审批**(叠加在 set_config_value 上)+ system prompt 集成 + enum 热刷新 |
| **P3** | SSH restricted shell 指南 + key 0600 强制 + host allowlist + risk 下限推断 |
| **P4**(后续) | 风险模型 P2 档、跨 task 连接池、多机原子操作 |

---

## 10. 替代方案

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| **本提案** | 原生多机、根治转义、(对跨机用户)安全净提升 | 配置重构 + 攻击面对冲 | ✅ |
| MCP:每台一个 server | 零侵入 terminal | 每台部署;同循环多机体验差 | ❌ G1 |
| 单 SSH + `/switch` | 改动小 | 不能同循环多机 | ❌ G1 |
| 维持现状 | 零改动 | 转义地狱 + 安全真空 | ❌ |

---

## 11. 开放问题

- **Q1**:Docker/Singularity 多实例(仅宿主机部署场景)是否复用同一 image 的容器层,还是各自起容器?影响资源占用。
  > (原 Q1 subagent / Q2 热刷新 / Q3 skill 特权 / Q5 风险档位 均已在 rev.3 拍板,见 §4.4/4.5/4.6/4.7。)

---

## 附录 A:验收测试清单

- [ ] `backend="evil.com"`(不在 enum)→ 结构性拒绝。
- [ ] RL task 注册 `env_type=docker` override 后,agent 传 `backend="gpu"` → 忽略并用 docker。
- [ ] `(default, gpu)` 与 `(default, this)` 缓存独立,cwd 互不影响。
- [ ] prod backend 上 `rm` 即使 `force=True` 仍 hardline 阻断。
- [ ] skill 对 key 路径 `../../.ssh/id_rsa` → 被 `validate_within_dir` 拒绝。
- [ ] 审批弹窗含 `ssh://user@host` + `risk_level`(对**用户**通道)。
- [ ] dump agent 的 system prompt + tool result,无私钥头、无 `host:` 明文(对**agent**通道)。
- [ ] `approval: always→never` 程序化写入被拒;`never→always` 通过。
- [ ] 仅设旧环境变量(无 `backends` 表)→ 行为同现状(回归)。
- [ ] `(default, gpu)` 有活跃 background session 时,idle/LRU 不回收(pin)。
- [ ] `read_terminal`/`close_terminal` 用 `session_id` 正确路由到所属 backend(无需 backend 参数)。
- [ ] 命令返回值含 `backend` + `cwd`。
- [ ] ssh 到非 RFC1918 host,skill 设 `risk_level:1` → 被下限拒绝。
- [ ] **delegate `allowed_backends=["gpu"]`,子代理选 `prod-db` → 拒绝**(不变量 13)。
- [ ] **builtin 管理 skill 加 backend 不触发 `pending_approval` → 测试失败**(不变量 14,强制审批)。
- [ ] **hermes 在容器内运行,`backends` 含 docker → 启动告警 + `terminal.backend` enum 不含 docker**(不变量 15)。
- [ ] 容器内设 `sandbox.dinD_acknowledged: true` → docker backend 启用(enum 含),但启动告警 docker.sock 风险。

---

## 附录 B:关键代码引用(基线)

| 关注点 | 位置 |
|--------|------|
| 配置读取(进程级) | `tools/terminal_tool.py:1256` |
| env 工厂 | `:1389` |
| 全局缓存 | `:982-986` |
| task override | `:1073` `:1158` `:1123` |
| isolation keys | `:1147-1150` |
| idle 回收 / cleanup | `:1559` `:1664` |
| 危险命令审批入口 | `:2269-2287` |
| 审批实现 | `tools/approval.py` `check_all_command_guards` |
| 凭据校验 | `tools/credential_files.py:57`(挂载仅 sandbox) |
| config 持久化 | `hermes_cli/config.py:7803`(默认无审批) |
| background 定位 | `tools/process_registry.py`;`tools/close_terminal_tool.py:30,54` |
| **subagent 受限 toolset** | `tools/delegate_tool.py:12`,`:15-16`(父不可见),`:68`(auto_approve) |
| **skill 审计** | `tools/skills_guard.py:12-13,717`;`tools/skills_tool.py:1-67` |
| **运行环境检测** | `hermes_constants.py:858`;`hermes_cli/config.py:434` |
| BaseEnvironment | `tools/environments/base.py:290` |

---

## 修订记录

- **rev.1**(初稿):架构、仲裁、安全模型、管理 skill。
- **rev.2**(自审 15 条):修正 read/close 机制(process_registry)、credential_files 语义(SSH 本地引用)、不变量 2/10 边界;风险模型分 P1/P2;新增 §4.7;Background pin;subagent 移至开放问题;补单调收紧/risk 下限/allowlist 辅助/净提升前提。
- **rev.3**(开放问题拍板):
  - **Q1 → §4.4**:subagent 不自由选;父 delegate 授权 `allowed_backends` 子集(不变量 13)。
  - **Q2 → §4.7**:enum 热刷新(接受 prompt cache 失效)。
  - **Q3 → §4.6**:写操作强制审批,叠加在 `set_config_value` 上,不信任 builtin skill(不变量 14)。
  - **Q4 → §4.8(新增)**:运行环境过滤,容器内默认禁 docker/singularity(复用 `is_inside_container`),`dinD_acknowledged` 显式启用(不变量 15)。
  - **Q5 → §4.5**:风险模型定 P1 档(P2 移 P4)。
  - 仲裁升级为四层(override > delegate > agent > default);新增 §3.6/3.7(subagent/skill 基线);附录 A +3 条测试;附录 B +3 项引用。
