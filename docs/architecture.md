# Agentix 架构

## 定位

Coding Agent SDK：run ANY agent on ANY environment, collect trajectories for training.

打包用 llm-agents.nix，Agentix 是上面的中间件层：plugin 协议 + trajectory 采集 + sandbox 接口。

## 系统架构

```
┌─ Host (Slime / Verl / 任意 orchestrator) ────────────────────┐
│                                                                │
│  RuntimeClient                                                 │
│  ├── exec("python -m agentix.eval --agent ... --dataset ...")  │
│  ├── download("/output/result.json")                           │
│  └── health() / upload() / exec()                              │
│                                                                │
│  不知道 agent 和 dataset 的细节                                  │
│  只知道: 触发 eval, 拿结果                                      │
│                                                                │
└──────────────────────────┬─────────────────────────────────────┘
                           │ HTTP
                           ▼
┌─ Sandbox ────────────────────────────────────────────────────────┐
│                                                                   │
│  agentix-server (:8000)          ← 纯管道, 不知道 agent/dataset   │
│  ├── POST /exec                                                  │
│  ├── POST /upload                                                │
│  ├── GET  /download                                              │
│  └── GET  /health                                                │
│                                                                   │
│  agentix.eval CLI                ← 编排逻辑                       │
│  python -m agentix.eval --agent <plugin> --dataset <plugin>      │
│  │                                                                │
│  │  1. dataset.setup()          → agent_input                    │
│  │  2. runner.run(agent_input)  → RunResult (output + trajectory)│
│  │  3. dataset.verify()         → metrics                        │
│  │  4. write /output/result.json                                 │
│  │                                                                │
│  ┌─ Agent Plugin ──────────┐  ┌─ Dataset Plugin ───────────────┐ │
│  │ Nix closure             │  │ Nix closure                    │ │
│  │ ├── bin/ (llm-agents)   │  │ └── dataset.py                 │ │
│  │ └── runner.py           │  │     setup() → agent_input      │ │
│  │     run(input) → Result │  │     verify() → metrics         │ │
│  └─────────────────────────┘  └────────────────────────────────┘ │
│                                                                   │
│  Task Environment (Dockerfile)                                    │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘

Deployment 层 (Docker / K8s / Daytona / Modal)
  create: 建沙箱 + 注入 runtime/agent/dataset closures + 启动 server
  delete: 销毁
```

## 三层分离

| 层 | 关注 | 不关注 |
|---|------|--------|
| **Host** | 触发 eval, 拿结果, 重试/并发/聚合 | agent 怎么跑, dataset 怎么验证 |
| **Sandbox** | exec/upload/download | eval 逻辑, plugin 内容 |
| **Eval CLI** | setup → run → verify, 写 result.json | 自己在哪, 谁在调自己 |

## 协议

### Runtime Server (沙箱接口)

纯管道。不知道 agent 和 dataset 是什么。

```
GET  /health              → {"status": "ok", "version": "0.1.0"}
POST /exec                → {"exit_code": 0, "stdout": "...", "stderr": ""}
POST /upload (multipart)  → {"path": "...", "size": 1024}
GET  /download?path=...   → file content
```

### Agent Plugin

Nix closure = llm-agents.nix binary + runner.py

```
agents/{name}/
├── default.nix       # symlinkJoin(llm-agents binary + runner.py)
└── runner.py         # 沙箱内执行
```

runner.py:
```python
async def run(agent_input: dict) -> RunResult:
    # RunResult = { output: dict, trajectory: Trajectory | None }
```

### Dataset Plugin

Nix closure = dataset.py + 任意资源

```
datasets/{name}/
├── default.nix
└── dataset.py        # 沙箱内执行
```

dataset.py:
```python
async def setup() -> dict:     # 初始化环境, 返回 agent_input
async def verify() -> dict:    # agent 跑完后, 采集 metrics
```

### Eval CLI

沙箱内命令，编排 setup → run → verify:

```bash
python -m agentix.eval --agent /opt/agent --dataset /opt/dataset --output /output/result.json
```

输出 result.json:
```json
{
  "output": { ... },
  "trajectory": { "schema_version": "ATIF-v1.4", "steps": [...], ... },
  "metrics": { "reward": 1.0, "passed": true }
}
```

### Deployment

沙箱 CRUD:

```python
class Deployment(ABC):
    async def create(self, config: SandboxConfig) -> SandboxInfo: ...
    async def get(self, sandbox_id: str) -> SandboxInfo: ...
    async def update(self, sandbox_id: str, config: SandboxConfig) -> SandboxInfo: ...
    async def delete(self, sandbox_id: str) -> None: ...
```

SandboxConfig:
```python
class SandboxConfig(BaseModel):
    task_image: str           # Dockerfile for environment
    runtime_closure: str      # agentix-server
    agent_closure: str        # agent plugin
    dataset_closure: str      # dataset plugin (optional)
```

## 典型流程

```
Host                          Deployment              Sandbox
 │                                │                       │
 │  deployment.create(config) ───►│                       │
 │                                │── 建沙箱               │
 │                                │── 注入 closures ─────►│
 │                                │── 启动 agentix-server ►│ :8000
 │  ◄── SandboxInfo               │                       │
 │                                │                       │
 │  exec("python -m agentix.eval  │                       │
 │    --agent /opt/agent           ─────────────────────►│
 │    --dataset /opt/dataset")    │                       │
 │                                │         setup()       │
 │                                │         run()         │
 │                                │         verify()      │
 │  ◄── {"exit_code": 0}         │                       │
 │                                │                       │
 │  download("/output/result.json")─────────────────────►│
 │  ◄── {output, trajectory, metrics}                    │
 │                                │                       │
 │  deployment.delete(id) ───────►│── 销毁 ──────────────►│ ✗
```

## 版本管理

| 需求 | 方案 |
|------|------|
| Agent 打包 | llm-agents.nix (40+ agents, 自动更新) |
| Agent plugin | Nix wrap: llm-agents binary + runner.py |
| Dataset plugin | Nix closure: dataset.py + resources |
| 版本锁定 | flake.lock + git commit |
| 分发 | Nix binary cache (S3) 或 tarball |
