# CodeEvolution 安装指南

## 依赖

### 系统依赖

| 依赖 | 用途 | 检查命令 |
|------|------|----------|
| Python 3.10+ | 后端引擎 | `python3 --version` |
| Node.js 20.19+（20.x 或 22.x） | CodeGraph + 前端构建 | `node --version` |
| Git | 代码仓分析 | `git --version` |

### CodeGraph（必须）

CodeEvolution 的代码解析完全委托给 CodeGraph。**每个要分析的目标仓库都需要先初始化 CodeGraph**。

```bash
npm i -g @colbymchenry/codegraph@0.9.x
cd /path/to/target/repo
codegraph init
```

CodeEvolution 通过直接读取 `.codegraph/codegraph.db`（SQLite WAL）获取代码图谱，不需要启动 CodeGraph 服务进程。所有语言自动支持（CodeGraph 覆盖 30+ 语言）。

### Python 包

```
fastapi + uvicorn     # Web API 服务器
fastmcp + mcp         # MCP 工具服务器
networkx              # 图算法（PageRank / Louvain 社区检测）
```

可选：
```
litellm               # LLM 支持（Phase 3 知识提取）
```

### Node 包（仅前端开发/构建时需要）

```
vue + vue-router      # 前端框架
mermaid               # 时序图渲染
vite                  # 构建工具
```

## 安装

```bash
cd services/codehistory

# 创建虚拟环境并安装
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 安装 LLM 支持（可选）
.venv/bin/pip install -e ".[llm]"
```

首次启动 Web 面板前需要安装前端依赖并生成 `web/dist/`：

```bash
cd web
npm ci
npm run build
cd ..
```

修改 `web/src/` 后需要重新执行 `cd web && npm run build`，否则后端仍会加载上一次构建的静态资源。

## 验证安装

```bash
.venv/bin/codeevolution --help
# 应显示: register / repos / web / serve
#         / knowledge / topology / impact / flow
#         / discover / check
```

## LLM 配置（可选）

Phase 3 知识提取（业务描述/规则/错误目录/状态机）需要 LLM：

```bash
export OPENAI_API_KEY="sk-..."
# 或
export ANTHROPIC_API_KEY="sk-ant-..."

# 可选：覆盖默认模型
export CODEEVOLUTION_LLM_MODEL="gpt-4o-mini"
```

## 快速开始

```bash
# 1. 初始化 CodeGraph
(cd /path/to/your/project && codegraph init)

# 2. 注册仓库
.venv/bin/codeevolution register -n demo -r /path/to/your/project

# 3. 查看 Web 面板并通过分析 API 创建快照
.venv/bin/codeevolution web --port 8765
# 浏览器打开 http://localhost:8765
```

`codeevolution web` 从 `web/dist/` 提供前端页面。如果访问根路径时只看到 `Frontend not built`，请回到 CodeEvolution 目录执行 `cd web && npm ci && npm run build`，再重启后端。
发布 wheel 会将构建后的静态资源嵌入 `codeevolution/web_dist`；源码运行则优先使用源码树中的 `web/dist`。

## Windows (PowerShell)

先安装 64 位 Python 3.10+、Node.js 20.19+（20.x 或 22.x）和 Git，并确认它们已加入 `PATH`：

```powershell
py --version
node --version
npm --version
git --version
```

在 CodeEvolution 目录中安装后端与前端依赖：

```powershell
cd services\codehistory

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

Push-Location web
npm ci
npm run build
Pop-Location

npm install --global @colbymchenry/codegraph@0.9.x
```

不需要执行 `Activate.ps1`，因此不会受 PowerShell 脚本执行策略影响。如果需要 LLM 支持，另行执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[llm]"
```

为目标仓库初始化 CodeGraph，然后注册服务并启动 Web 面板：

```powershell
$TargetRepo = "C:\path\to\your\repo"

Push-Location $TargetRepo
codegraph init
Pop-Location

.\.venv\Scripts\codeevolution.exe register --name demo --repo $TargetRepo
.\.venv\Scripts\python.exe scripts\service.py start --host 127.0.0.1 --port 8765
```

浏览器访问 `http://localhost:8765`。服务生命周期使用以下 PowerShell 命令；`build` 每次都会重新执行 `npm ci`，避免复用其他操作系统的 Rollup/esbuild 原生依赖：

```powershell
.\.venv\Scripts\python.exe scripts\service.py status
.\.venv\Scripts\python.exe scripts\service.py restart --host 127.0.0.1 --port 8765
.\.venv\Scripts\python.exe scripts\service.py stop
```

Windows 下运行后端检查和前端测试/构建：

```powershell
.\.venv\Scripts\python.exe -m ruff check codeevolution tests scripts
.\.venv\Scripts\python.exe -m pytest -q
Push-Location web
npm test
npm run build
Pop-Location
```

多仓发现和注册使用当前 CLI 契约：

```powershell
.\.venv\Scripts\codeevolution.exe discover --dir C:\work
.\.venv\Scripts\codeevolution.exe register --name order-svc --repo C:\work\order-svc
.\.venv\Scripts\codeevolution.exe repos
.\.venv\Scripts\codeevolution.exe check
```

快照分析的完整 API 流程（创建 scope/member、提交分析、轮询完成、查询快照）如下；将路径替换为已执行 `codegraph init` 的仓库：

```powershell
$Server = "http://127.0.0.1:8765"
$JsonHeaders = @{ "Content-Type" = "application/json" }
$Scope = Invoke-RestMethod -Method Post -Uri "$Server/api/scopes" `
  -Headers $JsonHeaders -Body (@{ name = "demo-scope" } | ConvertTo-Json -Compress)
$Member = Invoke-RestMethod -Method Post -Uri "$Server/api/scopes/$($Scope.scope.id)/members" `
  -Headers $JsonHeaders -Body (@{ display_name = "demo"; registered_path = $TargetRepo } | ConvertTo-Json -Compress)
$Run = Invoke-RestMethod -Method Post -Uri "$Server/api/analysis-runs" `
  -Headers $JsonHeaders -Body (@{ member_ids = @($Member.member.id) } | ConvertTo-Json -Compress)
do {
  Start-Sleep -Seconds 2
  $Run = Invoke-RestMethod -Method Get -Uri "$Server/api/analysis-runs/$($Run.run.id)"
} while ($Run.run.status -in @("pending", "running"))
if ($Run.run.status -ne "completed") { throw "analysis failed: $($Run.run.status)" }
$Snapshots = Invoke-RestMethod -Method Get -Uri "$Server/api/repository-members/$($Member.member.id)/snapshots"
$SnapshotId = $Snapshots.items[0].id
Invoke-RestMethod -Method Get -Uri "$Server/api/repository-snapshots/$SnapshotId?include=facts"
```

### Windows 文件系统支持矩阵

- 本地 NTFS：支持 SQLite WAL、Artifact 原子发布、受限 ACL 和有限 sharing-violation 重试。
- `CODEEVOLUTION_DATA_DIR` 应放在目标仓库之外；若 staging/artifact 位于被分析仓库内部，会被明确拒绝，避免冻结文件重新进入输入扫描。
- UNC 路径、网络盘、FAT/exFAT/ReFS 等非 NTFS 文件系统：当前不宣称支持；如需使用，先完成锁、WAL、ACL 和原子替换集成验证。
- Windows 目录没有可直接 `fsync` 的普通 CRT fd，因此文件内容仍执行 flush/fsync，目录元数据持久性不等价于 POSIX；异常退出后应保留 staging 目录并运行 scavenge。
- `llm-config.json` 使用 `icacls` 去除继承并限制当前用户和 SYSTEM；若 ACL 工具不可用，密钥保存会失败。普通 Artifact 的权限降级会产生警告。
- Windows 路径中的 ADS、DOS device name、尾部点/空格、reparse point 和大小写折叠碰撞会被拒绝；仓库目录本身可以使用盘符绝对路径。
- Windows 服务停止先校验 PID、启动时间、映像和实例 nonce，再使用 `taskkill /PID <pid> /T` 作为 detached 进程的强制关闭 fallback；不依赖 `CTRL_BREAK_EVENT`，也不会在 stop/restart 时主动取消分析任务。

可选的 LLM 环境变量可在当前 PowerShell 会话中设置：

```powershell
$env:OPENAI_API_KEY = "sk-..."
$env:CODEEVOLUTION_LLM_MODEL = "gpt-4o-mini"
```

## 多仓微服务设置

```bash
# 注册多个服务
.venv/bin/codeevolution register -n order-svc -r /repos/order-service
.venv/bin/codeevolution register -n user-svc  -r /repos/user-service

# 也可将前端和后端等多个独立仓库归入同一逻辑服务
.venv/bin/codeevolution register -n mall -r /repos/mall -r /repos/mall-admin-web

# 查看已注册服务和健康状态
.venv/bin/codeevolution repos
.venv/bin/codeevolution check
```
