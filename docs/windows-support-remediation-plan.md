# Windows 支持修复方案

## 1. 结论与范围

当前版本对 Windows 的支持停留在安装说明、前端构建和部分 Web 启动参数适配层面；新版本的仓库快照分析链路尚未达到可用标准。

本方案覆盖：

- Windows PowerShell 下的 Web 服务启动、停止、重启和状态检查；
- CodeGraph 子进程的启动、超时、取消和进程树清理；
- 源码冻结、Artifact 发布以及文件权限；
- Windows 路径、SQLite URI、服务发现、命令解析和 CI 回归；
- 源码安装与 wheel 安装两种交付方式的 Windows 冒烟验证。

本方案不改变分析结果格式、快照 identity 或 CodeGraph 数据模型。

本方案不要求 Web 服务在 `stop` / `restart` 时主动取消正在执行的 CodeGraph
分析任务；该行为不属于本轮 Windows 支持验收范围。CodeGraph 自身的用户取消、
执行超时和异常清理仍必须正确回收进程树。

## 2. 问题分级

### P0：阻断可用性

1. **服务进程状态检查可能误杀服务**

   [`scripts/service.py`](../scripts/service.py) 的 `is_running()` 使用 `os.kill(pid, 0)`。这个写法在 POSIX 上通常用于探测进程，但 Windows 的 `os.kill()` 对非控制台信号使用 `TerminateProcess` 语义，因此不能继续作为 Windows 的存活探测方式。

2. **Windows 服务停止方式与启动方式冲突**

   服务启动时使用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`，停止时发送 `CTRL_BREAK_EVENT`。Detached 进程没有与调用方共享控制台，不能依赖该控制事件完成停止；停止操作可能等待 5 秒后失败。

3. **源码冻结依赖 Unix-only 的 `dir_fd`**

   [`codeevolution/infrastructure/codegraph_capture.py`](../codeevolution/infrastructure/codegraph_capture.py) 的 `_open_beneath()` 使用 `os.open(..., dir_fd=...)`。Windows 不支持该参数。分析任务在 [`repository_attempt_worker.py`](../codeevolution/application/repository_attempt_worker.py) 中进入 `freeze_sources()` 后会触发该路径，导致快照分析失败。

4. **Artifact 发布依赖目录 fd 和目录 fsync**

   [`codeevolution/infrastructure/artifact_store_fs.py`](../codeevolution/infrastructure/artifact_store_fs.py) 会对目录调用 `os.open()` 和 `os.fsync()`。Windows CRT 不支持以普通 fd 方式打开目录，因此快照发布阶段仍会失败。

5. **CodeGraph 超时和取消使用 `os.killpg`**

   [`codeevolution/infrastructure/codegraph_command.py`](../codeevolution/infrastructure/codegraph_command.py) 的终止逻辑无条件调用 `os.killpg()` 和 `SIGKILL`。Windows 没有 `os.killpg`，超时或取消会变成内部错误，并可能留下 CodeGraph/Node 子进程。

### P1：兼容性和安全缺口

6. **CodeGraph 可执行文件解析和进程管理不统一**

   新的 `CodeGraphCommandRunner` 默认使用 `codegraph`，而其他旧入口已经有
   `codegraph.cmd` 的 Windows 分支。部分兼容入口仍直接使用
   `subprocess.run(..., timeout=...)`，即使解析到了正确命令，也不能保证 `.cmd`
   派生的 Node 进程在超时后被清理。所有仍可达的 CodeGraph 启动点都必须统一使用
   同一个 resolver 和 process-tree runner；不可达的遗留代码应直接删除。

7. **Unix 权限位在 Windows 上不等价**

   数据库、LLM 配置和 Artifact 多处调用 `chmod(0o600/0o700)`。Windows 的 `chmod` 主要只能控制只读属性，不能实现 Unix 风格的 owner-only ACL，敏感文件保护语义不完整。

8. **Windows 路径分类、校验和命名空间处理不完整**

   以 `PurePosixPath.is_absolute()` 判断路径安全时，`C:/...` 和 UNC 路径不能完全按
   Windows 绝对路径处理。同时必须区分三类路径：外部仓库/数据目录允许合法 Windows
   绝对路径；manifest/digest 路径必须是相对 POSIX 路径；CodeGraph 内绝对路径必须先
   验证位于仓库内，再规范化为相对路径。还需处理 NTFS ADS、DOS device name、组件尾部
   点/空格，以及大小写折叠后的别名碰撞。

9. **仓库发现依赖 Unix `find`**

   [`codeevolution/registry.py`](../codeevolution/registry.py) 的 `discover_repos()` 直接调用
   `find`，Windows 下会捕获 `FileNotFoundError` 后静默返回空列表。服务发现属于公开能力，
   不能以“无结果”掩盖平台不支持。

10. **SQLite URI 和存储文件系统边界未定义**

   多处以 `file:{path}?mode=ro` 手工拼接 SQLite URI。Windows 路径包含 `%`、`#`、`?`
   时可能被 URI 语义误解析。当前也没有说明 catalog WAL、Artifact 原子移动和 ACL 在
   UNC、网络盘、FAT/exFAT 上的支持等级。

11. **Artifact 操作没有处理 Windows sharing violation**

   即使所有应用句柄已经关闭，杀毒软件、索引器或并发读取仍可能令 `rename`、`replace`
   或 `rmtree` 短暂返回 sharing violation。目录 fsync 直接 no-op 也会降低现有 CAS 的
   crash-durability 语义，不能被描述为与 POSIX 等价。

12. **Web 生命周期状态和 PID 文件并发模型不完整**

   PID 的检查、端口探测、进程启动和 PID 文件写入没有跨进程互斥。并发 `start` 可能
   覆盖 PID；子进程提前退出或 PID 写入失败可能留下孤儿进程。只校验 Python 映像也不足以
   防止 PID 重用，因为其他项目命令可能使用同一个解释器。

13. **没有 Windows CI，且现有测试包含 POSIX-only 假设**

   当前 CI 只运行 `ubuntu-latest`，现有相关测试在 Linux 通过不能证明 Windows 的进程、目录 fd、权限和命令解析行为正确。

   此外，测试中仍有 `/bin/echo`、`#!/bin/sh`、POSIX executable bit、无条件 symlink 和
   `0600/0700` 权限位断言。直接加入 `windows-latest` 会先被测试夹具阻断，必须同步迁移。

14. **Node 版本、安装文档和实际 CLI 契约不一致**

   当前前端锁文件中的 Vitest/jsdom 等依赖要求 Node 20/22，而方案仍计划验证 Node 18，
   安装文档也宣称 Node 18+。PowerShell 示例仍包含已经移除或参数契约已经改变的 CLI
   用法。仅执行 `python -m build` 也不能证明 wheel 安装后能够找到 `web/dist`。此外，
   `service.py build` 只在 `node_modules` 不存在时执行 `npm ci`，从 WSL/Linux 切换到
   Windows 时可能复用错误平台的 Rollup/esbuild 原生依赖。

15. **Git/CodeGraph 输出编码策略不明确**

   多处 `subprocess.run(..., text=True)` 依赖 Windows 当前代码页。Unicode 分支名、作者、
   路径或非本地代码页的 stderr 可能解码失败或乱码。实施顺序中提到“统一编码”还不够，
   需要明确协议和错误处理方式。

16. **开发命令仍以 POSIX shell 为主**

    `Makefile` 使用 `.venv/bin/python`，README 的快速开始主要使用 Bash 命令。PowerShell 安装段落已经存在，但多仓命令、开发测试和日常启停仍缺少完整的 Windows 对照命令。

## 3. 推荐的实现方案

### 3.1 建立平台适配层

新增 `codeevolution/platform.py`，或将同等职责放入 `scripts/service.py` 的内部适配模块，禁止业务代码直接散落调用平台专用 API。

建议提供以下接口：

```text
resolve_executable(name) -> str
inspect_process(identity) -> running | stopped | unknown
start_process(argv, cwd, stdout, stderr, purpose) -> ProcessHandle
terminate_process(handle_or_pid, tree=True, grace_seconds=5) -> None
fsync_file(path_or_fd) -> None
fsync_directory(path) -> None
set_private_permissions(path, kind) -> None
normalize_manifest_path(value) -> str
sqlite_readonly_uri(path) -> str
atomic_replace(source, target, retry_policy) -> None
```

规则：

- POSIX 保留 `start_new_session`、进程组终止、目录 fsync 和 chmod；
- Windows 使用 `creationflags`、Windows 进程句柄和进程树终止；
- 业务层只依赖平台接口，不再直接引用 `os.killpg`、`dir_fd` 或目录 fd；
- 平台能力不可用时返回明确的 capability error，而不是抛出 `AttributeError` 或 `NotImplementedError`。
- Windows 瞬时 sharing violation 只对明确的 WinError 做有限次数、带上限的退避重试，
  不得 catch-all 后无限重试。

### 3.2 修复 Web 服务生命周期

#### 状态检查

Windows 下不要再使用 `os.kill(pid, 0)`。建议：

1. 读取 PID 文件；
2. 通过 Windows 进程句柄执行非破坏性的存在性检查，例如 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)`；
3. 可行时校验进程映像路径确实是当前虚拟环境的 Python 可执行文件；
4. 同时校验进程创建时间、完整命令摘要和实例 nonce，不能只校验 Python 映像；
5. 无法确认身份时返回 `unknown`，禁止删除 PID、启动第二个实例或终止该 PID，并输出诊断。

POSIX 继续使用 `os.kill(pid, 0)`，但也应增加 PID 文件 stale 和 PID 重用保护。

#### 停止方式

Windows 跨独立 CLI 的进程句柄不能假定仍然存在。本轮选择一个可落地的控制模型并固定下来：

1. 推荐由常驻 supervisor 持有服务进程和命名 Job Object，并通过具备随机 nonce 的本地控制通道
   接收 stop 请求；不得依赖启动 CLI 退出后仍持有 `Popen` handle；
2. 先请求 Uvicorn 优雅退出并等待 lifespan 完成；超时后才调用 `TerminateJobObject`；
3. 若首期采用 `taskkill /PID <pid> /T`，必须明确这是强制关闭 fallback，并在身份校验成功后
   才能执行，随后重新枚举进程树；
4. 不再把 `CTRL_BREAK_EVENT` 作为 detached 服务的唯一停止方式。

启动时建议使用 `CREATE_NO_WINDOW`，设置 `stdin=DEVNULL`、`close_fds=True`，并明确允许继承的
handle 列表。PID 文件应保存 PID、Windows process creation FILETIME、命令摘要、实例 nonce、
host、port 和平台信息。

`start`、`stop`、`restart` 对生命周期状态的修改必须持有跨进程锁；PID 元数据使用同目录
临时文件加原子替换。所有 `Popen` 之后的失败路径，包括 child early-exit、PID 写入失败和
ready timeout，都必须执行 terminate + wait，再决定是否删除 PID 文件。

端口探测使用 `getaddrinfo()` 按实际 IPv4/IPv6 地址族检查，先验证端口范围为 1–65535；
Windows 不设置会削弱独占判断的 `SO_REUSEADDR`，必要时使用 `SO_EXCLUSIVEADDRUSE`。
IPv6 readiness URL 必须使用方括号 host。`status` 同时报告 process state 和 HTTP readiness，
区分 `running/ready`、`running/unhealthy`、`stopped` 和 `unknown`。

#### 重启异常处理

启动超时后必须等待终止动作完成，再清理 PID 文件；如果进程仍存在，应在错误信息中明确提示 PID 和日志路径，而不是假设服务已经结束。

### 3.3 修复 CodeGraph 子进程管理

将 [`CodeGraphCommandRunner`](../codeevolution/infrastructure/codegraph_command.py) 改为使用平台适配层：

- POSIX：`start_new_session=True`，终止整个进程组；
- Windows：若需要保证 `.cmd`/Node 派生进程无残留，必须使用 Job Object，或使用已经通过
  派生进程测试验证的 `taskkill /T`；`CREATE_NEW_PROCESS_GROUP` 不能单独满足这一要求；
- Windows Job Object 必须在子进程开始派生 worker 前完成 assignment；
- Windows 首次终止等待 grace period，之后对整个 Job 强制终止；
- 如果 CodeGraph 会派生 Node worker，必须验证子进程也被清理；
- `CommandResult` 保持现有 `cancelled`、`timed_out` 和 `returncode` 契约不变；
- 终止失败通过专用异常表达，并由 worker 映射为结构化
  `codegraph_termination_failed` Attempt 错误，避免与 `CommandResult` 契约冲突；
- API、CLI、Evolution 兼容入口和 `RepositoryAttemptWorker` 中所有仍可达的 CodeGraph
  调用都使用同一个 runner；不可达的旧实现删除，不保留第二套 subprocess 行为。

### 3.4 修复安全源码冻结

保留 POSIX 上基于目录 fd 的防 TOCTOU 实现；Windows 使用独立实现，不能简单把 `dir_fd` 参数删掉后直接拼接路径。

Windows 实现至少应包含：

1. 将输入规范化为相对 POSIX manifest 路径；
2. 拒绝盘符路径、UNC 路径、`.`、`..` 和 NUL；
3. 逐级检查 symlink、junction 和其他 reparse point；
4. `resolve()` 后再次确认目标仍在 repository root 内；
5. 打开和复制前后比较文件 identity、大小和修改时间；
6. 发现路径类型变化或文件变化时返回现有的 `source_changed_during_capture` / `unsafe_or_unreadable` 语义；
7. 在 Windows 测试中覆盖普通文件、symlink、junction 和路径大小写变化。

上述安全校验同样应用于 `WorkspaceInputScanner` 的观察阶段，不能只修复
`freeze_sources()`。Windows 上 `O_NOFOLLOW` 不存在，扫描阶段也必须识别父目录中的
junction/reparse point，并生成与冻结阶段一致的 canonical collision key。

如果需要抗高权限并发攻击的严格保证，应使用 Windows `CreateFile` 的 reparse-point 相关标志实现真正的句柄级校验，而不是只依赖字符串路径 containment。

### 3.5 修复 Artifact 发布和持久化

调整 [`FileSystemArtifactStore`](../codeevolution/infrastructure/artifact_store_fs.py)：

- 文件仍然执行 `flush` + `fsync`；
- POSIX 继续执行目录 fsync；
- Windows 不再尝试 `os.open(directory)`；若 crash durability 属于 CAS 契约，优先使用
  `CreateFile(FILE_FLAG_BACKUP_SEMANTICS)` + `FlushFileBuffers`；若选择 no-op，必须在支持矩阵
  中标记持久性降级，并增加异常退出后的恢复/scavenge 验收；
- 原子发布继续要求 staging、artifact 和 trash 位于同一文件系统；
- Windows 上用 `os.replace`/`os.rename` 前确保所有文件句柄已关闭；
- 对 `winerror` 5/32/33 等已经确认的瞬时 sharing violation 做有限重试和退避；
- 发布失败必须保留可诊断错误，并由调用方安全回收 staging。

### 3.6 统一 CodeGraph 命令解析

`resolve_executable("codegraph")` 的建议顺序：

1. Windows 优先查找 `codegraph.cmd`、`codegraph.exe`，再查找裸名称；
2. POSIX 查找 `codegraph`；
3. 使用 `shutil.which` 返回绝对路径；
4. 明确 `.cmd` npm shim 的调用方式和 quoting；优先解析到绝对 `node.exe + JS entrypoint`，
   或使用经过 shell 元字符路径测试的封装，不允许拼接命令字符串；
5. 在启动前执行一次带 timeout、缓存和同等进程树清理的 `--version` 检查；
6. 错误信息输出实际解析候选和脱敏后的搜索路径，不直接泄露完整 PATH。

所有 CLI、API、Evolution 兼容入口和 `RepositoryAttemptWorker` 都必须使用同一个解析函数。

### 3.7 处理权限和路径语义

#### 权限

新增 `set_private_permissions()`：

- POSIX 使用现有 chmod 语义；
- Windows 优先在数据根设置 protected inheritable DACL，保留当前用户、SYSTEM，按部署策略
  决定是否保留 Administrators；优先采用成熟 Windows 安全 API 封装，避免手写不完整的
  `ctypes` ACL 替换；
- API Key 等密钥存储权限失败必须 fail closed；普通 snapshot/artifact 在允许降级时必须
  给出明确警告，不能静默认为 `chmod` 已生效；
- 在不支持 ACL 的受限环境中，至少拒绝把 API Key 写入公共目录，并给出 `CODEEVOLUTION_DATA_DIR` 建议。
- ACL 测试覆盖临时文件、SQLite `-wal/-shm`、migration backup 和最终 Artifact，并验证其他
  普通用户不能读取密钥，而不只是检查“不可公开写”。

#### 路径

统一使用两种明确的路径类型：

- 外部文件系统路径：`Path` / `os.path`，按当前平台解析；
- manifest、CodeGraph 相对路径和 digest 输入：规范化为 POSIX 相对路径。

不要用 `PurePosixPath.is_absolute()` 单独判断 Windows 输入是否绝对路径；应配合
`ntpath.splitdrive()`、UNC 判断和 root containment 检查。规则如下：

- 外部 `repository_root` / `data_dir`：允许盘符绝对路径；UNC 仅在存储支持矩阵声明支持时允许；
- manifest 和 CAS 相对路径：拒绝盘符、UNC、ADS、`.`、`..`、NUL、DOS device name、尾点和尾空格；
- CodeGraph 内绝对路径：先按当前平台解析并确认位于 repository root，再转为相对 POSIX 路径；
- 对所有目标路径计算 Windows canonical collision key，拒绝大小写、尾点/尾空格折叠后的碰撞。

### 3.8 修复仓库发现

用受 `max_depth` 限制的 `os.walk`/`Path` 遍历替换外部 `find`：

- 不跟随 symlink、junction 或其他 reparse point；
- 跳过 `.git`、`.codegraph`、`.codeevolution`、`node_modules` 等内部目录；
- 单个目录无权限时记录 warning 并继续，根目录不可读时返回明确错误；
- 已注册仓库的比较使用平台规范化 identity，不使用字符串 `/` 拆分目录名。

### 3.9 统一 SQLite URI 和存储边界

- 使用集中 URI builder（例如基于 `Path.resolve().as_uri()`）正确转义 `%`、`#`、`?` 和 Unicode；
- 不允许调用点手工拼接 `file:{path}?mode=ro`；
- 首期正式支持本地 NTFS；UNC、网络盘、FAT/exFAT 必须经 WAL、锁、ACL 和原子 rename 集成测试
  后才能标记为支持，否则启动时给出明确 capability error；
- 测试仓库目录和数据目录中包含空格、中文、`%`、`#` 的场景。

### 3.10 统一 Git/CodeGraph 编码

- 文件名枚举优先使用 byte-oriented、NUL-delimited 协议，并通过 `os.fsdecode` 还原；
- 结构化 Git 输出显式规定 UTF-8 配置和 `encoding`，错误输出使用可审计的
  `errors="replace"` 或 `surrogateescape` 策略；
- 测试中文仓库路径、分支名、作者名、提交消息，以及非 UTF-8 stderr；
- Windows 控制台输出失败不能令分析任务从成功变为 internal error。

### 3.11 修复安装、前端构建和文档契约

- 在 `web/package.json` 声明与 lockfile 一致的 `engines.node`；安装文档、CI 和错误提示使用
  同一个最低 Node 版本；
- `service.py build` 默认执行 `npm ci`，或使用包含 lockfile digest、OS、CPU 架构和 Node ABI
  的 sentinel 决定是否可复用 `node_modules`，不能仅检查目录是否存在；
- 更新 PowerShell 示例，以当前 `codeevolution --help` 和 CLI parser 为准，删除已移除命令，
  给出注册仓库、创建 analysis run、等待完成和查询 snapshot 的完整流程；
- 明确选择“wheel 内嵌 Web 静态资源”或“仅支持源码部署”。若支持 wheel，将 `web/dist`
  作为 package data，并在全新虚拟环境中验证，而不是依赖源码树相对路径。

## 4. 测试与 CI 方案

### 4.1 GitHub Actions

后端和前端 CI 分开，避免 Python × Node × OS 的笛卡尔积：

```yaml
backend:
  os: [ubuntu-latest, windows-latest]
  python: ["3.10", "3.12", "3.14"]

frontend:
  os: [ubuntu-latest, windows-latest]
  node: ["20.19", "22.13"]
```

如果项目不准备支持当前稳定 Python，则必须在 `requires-python` 和安装文档中声明上限，
不能继续无条件宣称 `Python 3.10+`。前端最低 Node 版本必须与 lockfile 和
`package.json#engines` 一致；不得继续验证当前依赖不支持的 Node 18。

Windows job 使用 PowerShell 原生命令，不依赖 Make 或 Bash。至少执行：

- `python -m ruff check codeevolution tests scripts`；
- `python -m pytest -q`；
- Python package build；
- `npm ci`、前端测试和生产构建。

Python build 后必须在干净临时环境安装 wheel，并验证 CLI、API import 和 Web 静态资源。
需要在打包配置中包含 `web/dist`，或明确产品只支持源码部署；不能只验证 editable install。

在启用 Windows job 前，先迁移现有 POSIX-only 测试夹具：用
`sys.executable -c ...` 代替 `/bin/echo`/shell script；symlink/junction 测试按 runner
能力显式 skip；Windows 权限测试验证 ACL，不硬断言 POSIX mode bits。

### 4.2 必须新增的 Windows 回归测试

1. `service.py` 的 `is_running()` 不得终止被探测进程；
2. start → status → stop → status 完整流程可重复执行；
3. stale PID、PID 重用和端口占用能得到可读错误；
4. 并发 start、child early-exit、PID 写入失败和 readiness timeout 不会覆盖 PID 或留下孤儿；
5. IPv4、IPv6、wildcard host、非法 port 和端口占用探测正确；Windows 不使用会削弱
   独占判断的 `SO_REUSEADDR`，并按实际地址族探测；
6. CodeGraph 正常执行、超时、用户取消和终止失败都会得到正确 Attempt 状态，并回收
   子进程和派生进程；
7. scanner + `freeze_sources()` 在 Windows 普通仓库上成功，并一致处理 reparse point；
8. Artifact publish、reuse、trash、scavenging、sharing violation 重试和崩溃恢复成功；
9. `codegraph.cmd` 或解析后的 Node entrypoint 可被统一、安全地启动；
10. 仓库路径包含空格、中文、`%`、`#` 和 shell 元字符时可用；
11. manifest 中的 drive path、UNC、ADS、DOS device name、尾点/尾空格、symlink/junction、
    大小写别名碰撞和路径逃逸会被正确拒绝；
12. 外部盘符仓库路径可用；UNC/网络盘按支持矩阵成功或返回明确 capability error；
13. `discover` 在 Windows 正确遵守 depth，并安全处理 junction 循环和无权限目录；
14. 权限适配器验证非 owner 用户不能读取密钥，并覆盖 SQLite sidecar 和临时文件；
15. 中文 Git 元数据和非 UTF-8 子进程错误输出不会造成解码崩溃；
16. 从干净 wheel 安装启动 CLI/Web 的冒烟测试通过；
17. PowerShell 文档中的命令由 CI 执行，确保与当前 CLI 参数契约一致。

没有 symlink/junction 权限的 runner 可以跳过对应测试，但必须在测试报告中显式标记，而不能静默通过。

## 5. 实施顺序

### 阶段 A：解除核心阻断

1. 建立平台适配层；
2. 确定 Windows Web 的 supervisor/control-channel 设计，修复 `service.py` 的状态检查、
   生命周期锁、停止和失败回滚；
3. 修复 CodeGraph 子进程终止与命令解析；
4. 为源码冻结增加 Windows 实现；
5. 修复扫描阶段的 reparse point/路径校验；
6. 禁止 Windows 走 POSIX 目录 fd fsync，并明确 crash durability 策略；
7. 用跨平台遍历替换仓库发现中的 Unix `find`；
8. 修复 SQLite URI 转义。

### 阶段 B：补齐安全和路径语义

1. 增加 Windows ACL 适配；
2. 统一盘符、UNC、ADS、DOS device name、reparse point、collision key 和相对路径校验；
3. 统一 Git/CodeGraph 输出编码和错误信息；
4. 增加 Windows sharing violation 有限重试；
5. 修正 Node/Python 支持范围和 PowerShell CLI 文档；
6. 确定本地 NTFS、UNC、网络盘和 FAT/exFAT 的支持矩阵。

### 阶段 C：持续验证

1. 加入 `windows-latest` CI；
2. 迁移 POSIX-only 测试夹具，执行真实 CodeGraph CLI 的 Windows 集成测试；
3. 验证前端构建、服务重启和快照发布；
4. 构建 wheel，在干净 Windows 环境执行安装后 CLI/Web 冒烟；
5. 将 Windows 支持状态从“初步适配”更新为“受支持”，前提是所有 P0 验收项和支持矩阵内
   的验收项通过。

## 6. 验收标准

Windows 支持修复完成的最低标准：

- PowerShell 下首次启动、状态查询、停止和重启连续执行 20 次无误杀、无残留进程；
- 两个并发 start 只能产生一个受管理实例，PID 文件不会被覆盖；
- 一个包含 Python/JavaScript/配置文件的真实 Git 仓库能够成功创建并发布 Repository Analysis Snapshot；
- CodeGraph 正常、超时、用户取消和终止失败场景均能得到正确 Attempt 状态；
- Artifact 发布和读取在 Windows 上通过完整回归测试；
- `discover`、注册、创建分析、查询快照的 PowerShell 真实流程可用；
- 包含空格、中文、`%`、`#`、shell 元字符和盘符的仓库路径可用；
- Windows 文件名命名空间攻击和 canonical collision 均被拒绝；
- Windows CI 全部通过；
- wheel 安装后的 CLI、API 和 Web 静态资源冒烟通过；
- 现有 Linux 测试和行为不回归；
- Node/Python 最低版本与依赖元数据、CI 和安装文档一致；
- 所有权限、文件系统和 crash-durability 差异都在安装文档中明确说明。

## 7. 参考依据

- [Python `os` 文档：Windows 信号、`killpg`、`dir_fd` 和 `chmod`](https://docs.python.org/3.12/library/os.html)
- [Python `subprocess` 文档：Windows `Popen`、进程组和终止方式](https://docs.python.org/3/library/subprocess.html)
- [Microsoft CRT `_open` 文档：目录不能按普通文件打开](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-wopen?view=msvc-170)
- [Microsoft Job Objects 文档：进程树归属和生命周期](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [SQLite URI 文档：Windows 路径和 percent-encoding](https://www.sqlite.org/uri.html)
