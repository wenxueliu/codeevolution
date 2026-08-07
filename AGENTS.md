@CLAUDE.md

# Agent 自动打包与重启

修改 codehistory 代码后，Agent 自动执行打包和重启。

## 触发条件

以下变更应触发自动打包+重启：

- **后端代码变更**: `codehistory/*.py` 任意 Python 源文件
- **前端代码变更**: `web/src/**` 任意 Vue/JS/CSS 文件
- **依赖变更**: `pyproject.toml` 或 `web/package.json`

纯文档变更（`*.md`）和 CI 配置（`.github/`）不需要打包重启。

## 自动打包

```bash
# 在服务根目录执行
cd services/codehistory

# 构建前端（npm ci + npm run build → web/dist/）
.venv/bin/python scripts/service.py build
```

`build` 做了两件事：
1. `npm ci`（如果 `web/node_modules` 不存在）
2. `npm run build`（Vite 构建到 `web/dist/`）

后端是 Python 源码直读 (`python -m codehistory.cli web`)，无需额外打包步骤。

## 自动重启

```bash
# 在服务根目录执行
cd services/codehistory

# 重启服务（stop → 构建 → start）
.venv/bin/python scripts/service.py restart --host 0.0.0.0 --port 8765
```

`restart` 流程：
1. 停止当前运行的 codehistory 进程（SIGTERM，5s 超时等待）
2. 重新构建前端
3. 启动新进程，轮询 `/api/repos` 直到就绪
4. PID 写入 `.run/codehistory.pid`，日志写入 `.run/codehistory.log`

## 验证

重启后验证服务正常：

```bash
# 检查状态
.venv/bin/python scripts/service.py status

# 快速冒烟：API 健康检查
curl -s http://127.0.0.1:8765/api/repos | head -c 200
```

## 手动操作

| 命令 | 用途 |
|------|------|
| `make build` | 仅构建前端 |
| `make start` | 构建 + 启动 |
| `make stop` | 停止服务 |
| `make restart` | 停止 + 构建 + 启动 |
| `make status` | 查看运行状态 |
