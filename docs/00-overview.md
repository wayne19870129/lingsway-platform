# Platform overview

Lingsway Platform 是一个 provider 可插拔的网络订阅分发平台。分层模型、
目录边界、插件接口与安全铁律见根目录 [`ARCHITECTURE.md`](../ARCHITECTURE.md)；
凡与已接受的 ADR（`80-decisions/`）冲突处以 ADR 为准。

新会话的阅读顺序见 [`../CLAUDE.md`](../CLAUDE.md)；当前进度、角色分工与
未完成工作见 [`83-project-continuity.md`](83-project-continuity.md)。

## 本机 Python 测试与 lint

**统一入口是 `Makefile`，不是某一台机器上的解释器绝对路径。**

```sh
make lint         # ruff check backend + mypy backend/app backend/tests + 前端 lint/typecheck
make test-unit    # 全 mock，无需 DB / Docker / 网络
make test         # 需要 TEST_DATABASE_URL（真实 MySQL 8.4），跑 unit + integration + guards
```

只跑后端某一项时，直接用这些命令（CI 的 `lint` job 跑的就是 `make lint`）：

```sh
python -m ruff check backend
python -m mypy backend/app backend/tests
python -m pytest backend/tests/unit backend/tests/guards
```

### 解释器漂移怎么防

本节此前写死了 `F:\Codex\lingsway-platform\.venv\Scripts\python.exe`，
并要求"不得使用裸 `python`"。该要求已**作废**——它只在一台特定 Windows
机器上成立，对 CI runner、Claude Code 云端 session、以及任何换了机器的
执行者都是错误指令，且与 `Makefile`（`PYTHON ?= python`）和
`.github/workflows/ci.yml` 实际执行的命令直接冲突。

正确的防漂移做法是**每台机器各自建一个虚拟环境并激活它**，让 `python`
在该 shell 内就指向正确的解释器：

```sh
python -m venv .venv
. .venv/bin/activate          # Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

`pyproject.toml` 要求 `requires-python = ">=3.12"`，CI 固定跑 3.12；
在低于 3.12 的解释器上 `pip install -e .` 会直接失败，这本身就是防漂移的闸门。

汇报测试结果时给出 `python -V` 与 `sys.executable` 作为证据仍然有价值，
保留这条要求——但证据的作用是**证明跑在哪个解释器上**，不是证明跑在某个
特定绝对路径上。
