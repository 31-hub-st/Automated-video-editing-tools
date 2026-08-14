# StoryForge v1.0.8 发布与回归门禁

StoryForge 使用三层 Windows 门禁。Actions 只需要仓库读取权限，不读取私密 Token，也不会上传或发布 EXE/ZIP。

| 门禁 | 触发方式 | 内容 | 摘要 Artifact |
| --- | --- | --- | --- |
| `fast-pr` | Pull Request；`main`、`release/**` 推送 | Python 编译、前端 JavaScript 语法、空白检查、发布/稳定性/生产/UI 关键契约测试 | `fast-pr-summary-*`，保留 14 天 |
| `nightly-full` | 每日定时；可手动运行 | Python/JavaScript 语法和 `unittest discover` 全量回归 | `nightly-full-summary-*`，保留 30 天 |
| `release-stable-gate` | `v*` Tag；人工确认的手动运行 | 全量回归、轻量 onedir 冻结构建、311.05 秒与 600 秒真实渲染、发布证明、独立包体冒烟 | `release-stable-evidence-*`，保留 90 天 |

每个测试摘要目录至少包含 `unittest.log`、`summary.json` 和 `summary.md`。Release 摘要另外包含构建日志、`stability-acceptance.json`、`package-smoke.json` 和总览 Markdown。Artifact 是门禁证据，不是可分发安装包。

## 本地快速回归

```powershell
python -m compileall -q storyforge scripts tests
node --check ui/app.js
python scripts/run_test_gate.py `
  --name fast-local `
  --output-dir test-results/fast-local `
  -- tests.test_release_gate tests.test_stability_acceptance `
     tests.test_production_workflow tests.test_ui_contract
```

全量回归：

```powershell
python scripts/run_test_gate.py `
  --name full-local `
  --output-dir test-results/full-local `
  -- discover -s tests -p test_*.py
```

## 构建模式

普通本地开发、界面检查或候选包不传发布开关，保持原有快速路径：

```powershell
& '.\scripts\build_exe.ps1' `
  -Standalone `
  -OutputDirectory 'D:\StoryForgeBuildTemp\dev-dist' `
  -WorkDirectory 'D:\StoryForgeBuildTemp\dev-work'
```

正式发布必须显式声明两个开关：

```powershell
& '.\scripts\build_exe.ps1' `
  -ReleaseBuild `
  -RequireStableAcceptance `
  -WithLocalAI `
  -OutputDirectory 'D:\StoryForgeBuildTemp\release-dist' `
  -WorkDirectory 'D:\StoryForgeBuildTemp\release-work' `
  -HubEndpoint 'http://10.0.0.225:8765'
```

`-ReleaseBuild` 不会暗中打开稳定验收；调用者必须同时明确写出 `-RequireStableAcceptance`。这样普通本地构建不会意外执行长时间渲染，而任何被标记为 Release 的构建都无法省略稳定门禁。稳定验收要求 `-StableStressSeconds` 不少于 600 秒。

面向员工制作电脑的正式完整包还必须带 `-WithLocalAI`，以收集 Kokoro/PyTorch 和离线多语种资源。CI 中不携带本地模型的独立门禁属于轻量契约验证，不能冒充员工完整包。

Release 构建依次生成并核对：

1. `BUILD_STARTUP_VALIDATION.json`：冻结程序版本、Python.NET/WebView 导入、UI 文件、FFmpeg 和本机 Worker 启动。
2. `BUILD_STABILITY_ACCEPTANCE.json`：冻结 EXE 真实执行 311.05 秒和 600 秒场景，并绑定 EXE 的大小与 SHA-256。
3. `BUILD_RELEASE_VALIDATION.json`：冻结发布目录逐文件证明。
4. `<WorkDirectory>\release-gate\package-smoke.json`：在发布证明生成后重新启动精确包体，复核版本、导入、UI，并实际执行包内 FFmpeg 的 `-version` 探针。

## 单独复核已有包体

包体冒烟报告必须写到已证明发布目录之外，否则会改变目录 Manifest：

```powershell
python scripts/package_smoke.py `
  --package-root 'D:\StoryForgeBuildTemp\release-dist\StoryForge Studio' `
  --expected-version '1.0.8' `
  --report 'D:\StoryForgeBuildTemp\reports\package-smoke.json' `
  --require-stable-acceptance
```

稳定发布不允许跳过冻结运行。仅在非 Windows 的元数据诊断中，可以用 `--skip-runtime-reason "具体原因"`；报告会把运行时导入和 FFmpeg 标为 `skipped` 并保存原因，而不是伪装为通过。
