# Platform overview

Documentation content is delivered in T7. The authoritative contract is `ARCHITECTURE.md`.

## 本机 Python 测试与 lint 解释器

本机测试统一使用 `F:\Codex\lingsway-platform\.venv\Scripts\python.exe`，不得使用裸 `python`，以避免解释器漂移。

每次汇报测试结果时，必须同时给出 `sys.executable` 的实际值作为证据。lint 同样使用该解释器：

```powershell
Set-Location F:\Codex\lingsway-platform
& 'F:\Codex\lingsway-platform\.venv\Scripts\python.exe' -m ruff check backend
& 'F:\Codex\lingsway-platform\.venv\Scripts\python.exe' -m mypy backend/app backend/tests
```
