# Agent Protocol

每个 agent 是一个目录，包含三个文件：

```
agents/{name}/
├── default.nix    # Nix 打包
├── runner.py      # 运行协议
└── build.sh       # 构建脚本
```

## runner.py

必须导出一个 `run` 函数：

```python
async def run(agent_input: dict) -> dict
```

- 在沙箱内执行，可以直接访问文件系统和运行命令
- `agent_input` 内容由 agent 自定义，runtime server 透传不解析
- 返回 `dict`，格式由 agent 自定义
- API key 等敏感信息通过 `agent_input` 传入，不依赖环境变量

Runtime server 启动时从 `/opt/hnix/agent/runner.py` 加载，orchestrator 通过 `POST /run` 调用。

## default.nix

Nix 打包定义，接受参数化构建：

```nix
{ pkgs ? import <nixpkgs> {}
, version ? "1.0.0"
, hash ? ""
}:
```

产出一个 closure，包含：
- `bin/` — agent 可执行文件
- `runner.py` — 运行协议
- 全部依赖（runtime, libs, etc.）

## build.sh

构建脚本，封装 nix-build + export：

```bash
./build.sh                              # 构建默认版本
./build.sh --version 2.2.0             # 构建指定版本 (自动发现 hash)
./build.sh --version 2.2.0 --export    # 构建 + 导出 tarball
```

参数：
- `--version` — agent 版本
- `--hash` — Nix output hash (省略则自动发现)
- `--export` — 导出 closure 为 tarball
- `--out-dir` — 导出目录 (默认 `./out/`)

导出产物：
```
out/
├── {name}-{version}.tar.gz    # closure tarball
└── {name}-{version}.json      # metadata
```

## 添加新 agent

1. 创建目录 `agents/{name}/`
2. 写 `runner.py`:
   ```python
   async def run(agent_input: dict) -> dict:
       # 你的 agent 逻辑
       ...
       return {"exit_code": 0, "stdout": "...", "stderr": ""}
   ```
3. 写 `default.nix`（参考 `agents/claude-code/default.nix`）
4. 写 `build.sh`（参考 `agents/claude-code/build.sh`）
5. 注册到 `flake.nix`
6. 测试: `./build.sh --version x.y.z`
