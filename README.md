# Agent Eval Lab

> Agent Eval Lab 不是排行榜。
> 它是一个用于理解 Coding Agent 如何以及为何发生变化的本地实验系统。

**比较用例，而不只是分数。**

**先看证据，再做解释。**

**把失败变成下一次实验。**

AEL 是一个本地优先的 Coding Agent 实验平台。它让同一批 Case（用例）在不同 Agent Variant（智能体变体）和 Trial（试验轮次）上运行，由独立 verifier（验证器）判断任务真值，保存工作区与原生证据，并把真实失败沉淀为后续实验。

## 开发

项目使用 `uv` 和 Python 3.11 及以上版本。

执行：

    uv sync --extra test
    uv run pytest
    uv run ael doctor

测试套件不会调用付费模型。

## 本地证据

AEL 将原生输出和归一化事件与验证器真值分开保存：

- verifier（验证器）：任务是否通过；
- workspace（工作区）：实际发生了哪些文件变化；
- native/telemetry（原生输出/遥测）：Agent 的行为证据。

主机本地文件系统复制只是工作区隔离，不等于操作系统或安全沙箱。Agent 自身的 sandbox（沙箱）和 approval policy（审批策略）会记录在对应的 variant/run fingerprint（变体/运行指纹）中。

## 当前机器探测

当前只读 M1 探测在本机发现 Codex CLI 0.147.0、Claude Code 2.1.229 和 Hermes 0.20.0。Pi 当前不在 PATH 中，因此会记录为不可用；AEL 不会静默安装或模拟缺失的 Agent。

真实 driver（驱动）使用当前 CLI 的机器接口：Codex `exec --json`、Claude 非交互 `stream JSON`、Pi RPC JSONL，以及带 usage 文件的 Hermes oneshot。每个 Run 的原生输出都保存在自己的原生证据目录中。

手动真实冒烟验证流程：

    uv run ael doctor --root .
    uv run ael agents --root .
    uv run ael run examples/experiments/live-smoke.yaml --root .

Phase A 的正常产品路径不需要手写 Experiment YAML：

    uv run ael ui --root . --host 127.0.0.1 --port 8713

然后打开 `http://127.0.0.1:8713/experiments/new`，选择 `parser-boundary`、
`premature-completion`、`state-reset`，勾选当前可用的真实 Agent，填写实际
Model/Provider（或保留 `Default configured`），设置 trials/concurrency，点击
`Run experiment`。实验详情页的主体是 Case × Variant 矩阵；每个 cell 都链接到
对应真实 Run。不可用的 Agent 会保持 disabled，不会被当作验收证据。

仓库中不会保存凭据。缺失 CLI、provider（提供方）不可用、认证错误或速率限制会记录为基础设施/进程证据，不会被提升为 Agent 任务失败。

Phase A 的真实验收记录（2026-08-14）：实验 `golden-phase-a-final-75ae939d` 从这个 Web
Builder 创建，包含 3 个 Case、Codex / Claude Code / Hermes 3 个真实 Variant、2 trials，
共 18 个 Run；18 个 Run 都完成了进程和 verifier 判定，最终 17 PASS / 1 FAIL。二维矩阵中
`premature-completion` 为 DIFFERENTIAL：Codex 2/2 PASS、Hermes 2/2 PASS、Claude Code
1/2 PASS；另外两个 Case 均为 3 个 Variant 稳定通过。这个 Case 的 visible targeted/full
suite 都通过后，Claude 在 hidden valid-empty-cursor boundary 上失败，证据保存在对应 Run
的 verifier/workspace/native 目录中。实际 execution workspace 位于仓库外临时目录，Golden
fixture 在整次真实实验后保持原始 revision。

## ObservationProfile 与 OTel

Run 默认使用 `minimal` 观察配置。`minimal` 和 `telemetry` 保留归一化行为摘要，但会省略结构化 prompt（提示词）、tool arguments（工具参数）、tool results（工具结果）和 transcript（转录）字段；只有显式选择 `deep` 才会保留这些字段，并继续进行凭据脱敏。每个 Run 都会在 fingerprint 中记录 ObservationProfile。

Phase B 的 Claude Code OTel vertical slice 已在本机真实验证：Claude Code 2.1.229 → OTLP
HTTP → local-only Collector → `.ael/otel/{logs,metrics,traces}.jsonl` → 按 `ael.run.id`
ingest → Run Evidence / Failure Explorer。正式 Phase A 的 Run
`f0dab3c6f17a4447b392513d2ccc26a4` 实际收到了 2 个 log records、1 个 metric record、35
个关联事件，包含 6 model calls、5 tool calls、15,794 input tokens、3,273 output tokens；
Run 页面和 Explorer 都可见这些结果。这里的 35 个事件来自真实 Collector 输出，不是环境变量、
空文件或 debug exporter 的存在性证明。

Collector 只绑定 loopback，使用 Docker 启动（不会启动 Grafana/Tempo/Prometheus/Jaeger）：

    docker run -d --name ael-otel \
      -p 127.0.0.1:4317:4317 -p 127.0.0.1:4318:4318 \
      -v "$PWD/infra/otel-collector.yaml:/etc/otelcol-contrib/config.yaml:ro" \
      -v "$PWD/.ael/otel:/output" \
      otel/opentelemetry-collector-contrib:0.133.0 \
      --config=/etc/otelcol-contrib/config.yaml

    AEL_OTEL_ENDPOINT=http://127.0.0.1:4318 \
      uv run ael ui --root . --host 127.0.0.1 --port 8713

OTel 是 AEL 的一个 Evidence Source，不取代 Verifier（task truth）、Workspace
（environment truth）或 Agent native trace；没有真实 telemetry 时 UI 会显示 insufficient
evidence。对于 Claude Code，只有提供 `AEL_OTEL_ENDPOINT` 时，`telemetry` 才会通过单次运行
环境启用 metrics/logs；`deep` 还会显式打开 prompt 和 tool payload 字段。默认仍是 `minimal`。

## 示例

一次比较应当能读成：

    Minimal v0.3       PASS
    Minimal v0.4       FAIL

    相同：
    模型
    任务
    运行时

    变化：
    compaction=true

    下一次实验：
    在上下文回归套件上比较 compaction off/on

报告应当区分已观察到的事实和未知信息，而不是声称某个模型承担了精确百分比的责任。

## 产品边界

AEL 聚焦于矩阵执行、差异比较、证据融合、失败调查、失败到实验和失败到回归。它不是分布式运行器、Agent runtime、通用插件框架、OTel backend 或云服务。

## 差异证据

确定性的 Failure Explorer（失败分析器）会先检查 Case revision，再选择最接近的 PASS 参考运行，并展示 SAME/CHANGED/UNKNOWN 变量、`CONTROLLED`/`PARTIAL`/`DESCRIPTIVE` 置信度、verifier/workspace 证据、归一化锚点时间线和首次有意义的分歧。如果参考运行或锚点证据不足，系统会明确说明，不会编造因果根因。

Diagnosis（诊断）使用这个紧凑证据包，而不是不受限的完整轨迹。未配置 endpoint 时，系统仍会生成确定性的假设和未知项。配置 `AEL_DIAGNOSIS_BASE_URL`、`AEL_DIAGNOSIS_API_KEY` 与 `AEL_DIAGNOSIS_MODEL` 后，可以调用一个 OpenAI-compatible chat-completions endpoint；API key 只会放在请求 header 中。Failure Explorer 的 Follow-up Builder 会在同一 Case revision 上生成可编辑的 baseline/candidate 两个 Variant，真正写入 `verification_gate`、`run_mode` 或 Agent 变化后再运行，不需要用户离开 AEL 修改 YAML。

## 失败记录簿（Failure Book）

只有进程已完成且 verifier 返回 FAIL 的记录才会进入失败记录簿，并以 `OBSERVED` 开始；相同 Case revision 和 verifier signature 的重复失败会归并到同一 Failure Pattern，关联多个 Runs，并在重复出现后变为 `REPRODUCED`。

Phase C 的真实 Web 验收记录（2026-08-14）：从 Failure Explorer 打开 Follow-up Builder，保持
Claude Code、Model、3 个 Golden Case 和 `controlled` observation 不变，生成
`verification_gate=false` 的 baseline 与 `verification_gate=true` 的 candidate。第一次真实复验实验
`follow-up-golden-phase-a-final-75ae939d-f0dab3c6-6ede65` 完成 12 个 Claude Code Runs；baseline
全部 PASS，candidate 在 `premature-completion` 上出现 1 PASS / 1 FAIL，因此系统没有虚假标记为修复。
第二次将 trials 编辑为 1 的真实复验实验
`follow-up-golden-phase-a-final-75ae939d-f0dab3c6-18582b` 完成 6 个 Runs，两个 Variant 的 3 个
Case 均 PASS；原始 Failure `failure-f0dab3c6f17a4447b392513d2ccc26a4` 随之显示
`FIXED`，再通过页面按钮 pin 后显示 `REGRESSION_GUARDED`。Regression Suite 直接保存
`case_id=premature-completion + revision=b71c21621ce0a54f59774ab63251ecc9947973f150a42a145ab81d48575ed7d5`，没有复制 fixture。

## 可运行示例

仓库包含一个不调用模型的确定性矩阵：

    uv run ael run examples/experiments/fake-matrix.yaml --root .

然后启动本地 Web UI：

    uv run ael ui --root .

该示例会产生稳定通过和稳定失败两类结果。

对于已经持久化的实验，可以执行 `uv run ael compare <experiment-a> <experiment-b> --root .`，再从本地 UI 打开对应 Run 的 Failure Explorer。AEL 将数据库和大型证据写入 `.ael/`，该目录已被 Git 忽略。

## 许可

内部全新原型项目。
