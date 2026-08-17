# Agent Eval Lab

> Agent Eval Lab 不是排行榜。
> 它是一个用于理解 Coding Agent 如何以及为何发生变化的本地实验系统。

**比较用例，而不只是分数。**

**先看证据，再做解释。**

**把失败变成下一次实验。**

AEL 是一个本地优先的 Coding Agent 实验平台。它让同一批 Case（用例）在不同 Agent Variant（智能体变体）和试验轮次上运行，由独立 verifier（验证器）判断任务真值，保存工作区与原生证据，并把真实失败沉淀为后续实验。

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

当前只读探测在本机发现 Codex CLI 0.147.0、Claude Code 2.1.229、Hermes 0.20.0，以及可配置的 Generic CLI Harness。Pi 当前不在 PATH 中，因此会记录为不可用；AEL 不会静默安装或模拟缺失的 Agent。

真实 driver（驱动）使用当前 CLI 的机器接口：Codex `exec --json`、Claude 非交互 `stream JSON`、Pi RPC JSONL，以及带 usage 文件的 Hermes oneshot。每个 Run 的原生输出都保存在自己的原生证据目录中。

手动真实冒烟验证流程：

    uv run ael doctor --root .
    uv run ael agents --root .
    uv run ael run examples/experiments/live-smoke.yaml --root .

阶段 A 的正常产品路径不需要手写实验 YAML：

    uv run ael ui --root . --host 127.0.0.1 --port 8713

然后打开 `http://127.0.0.1:8713/experiments/new`，选择 Case 版本和持久 Variant。
Variant 先在 `http://127.0.0.1:8713/variants` 中创建或 Duplicate；Generic CLI 的 executable、arguments、prompt transport、环境增量和可选 version command 都会持久化在 Variant 中，运行前会生成与实际进程输入同源的 Execution Receipt。每次 Run 都明确区分 Configured、Effective、Observed 和 Verified；如果两次启动输入相同，比较页显示 `NO EFFECTIVE CHANGE`。
同一 Agent / Harness
可以拥有多个 subject revision、model 和 harness config。选择 Baseline / Candidate 后，
运行前页面会显示 Changed / Same / Unknown 以及 `CONTROLLED`、`PARTIAL` 或 `DESCRIPTIVE`
comparison validity，再点击 `运行实验`。整个路径不需要手写实验 YAML；不可用的 Agent 会保持
“已禁用”，不会被当作验收证据。

历史 CaseRevision 由 `.ael/case-revisions/<case>/<revision>` 冻结保存，可以直接重跑；authoring 文件后来变化不会改写历史 Experiment。

实验详情页先展示真正用于决策的 Case 级对比：每个 Case 一张“指标为行、Variant 为列”的表，
逐项比较 Agent 类型、Model、Provider、Verifier 结果、端到端时延、OTel 时延、输入/输出/缓存/总
tokens、成本、工具调用、模型轮数、工具错误、变更文件和 OTel/native 证据。数值默认是每次
trial 平均，未知不会用 0 代替；筛选会直接收窄 Case、结果状态和指标视角。完整的 Case × Variant
状态只作为折叠的结果导航，用于打开具体 Run 或 Explicit Contrast，不把矩阵或 aggregate leaderboard
当成主体。

比较页面支持按“差异 Case”“失败 / 不稳定”“资源成本”“证据覆盖”以及单个 Case 聚焦；这符合
业界评测比较的核心心智：先找改动导致的回归或改善，再查看时延、tokens、成本、错误和证据覆盖，
最后进入单次 Run 的 Explicit Contrast 解释差异。

仓库中不会保存凭据。缺失 CLI、provider（提供方）不可用、认证错误或速率限制会记录为基础设施/进程证据，不会被提升为 Agent 任务失败。

阶段 A 的真实验收记录（2026-08-14）：实验 `golden-phase-a-final-75ae939d` 从这个 Web
Builder（构建器）创建，包含 3 个 Case、Codex / Claude Code / Hermes 3 个真实 Variant、2 次试验，
共 18 个 Run；18 个 Run 都完成了进程和 verifier 判定，最终 17 PASS / 1 FAIL。二维矩阵中
`premature-completion` 为 DIFFERENTIAL：Codex 2/2 PASS、Hermes 2/2 PASS、Claude Code
1/2 PASS；另外两个 Case 均为 3 个 Variant 稳定通过。这个 Case 的可见定向测试和完整测试套件
都通过后，Claude 在隐藏的 valid-empty-cursor 边界上失败，证据保存在对应 Run
的 verifier/workspace/native 目录中。实际执行工作区位于仓库外临时目录，Golden
fixture 在整次真实实验后保持原始 revision。

## ObservationProfile 与 OTel

Run 默认使用 `minimal` 观察配置。`minimal` 和 `telemetry` 保留归一化行为摘要，但会省略结构化 prompt（提示词）、tool arguments（工具参数）、tool results（工具结果）和 transcript（转录）字段；只有显式选择 `deep` 才会保留这些字段，并继续进行凭据脱敏。每个 Run 都会在 fingerprint 中记录 ObservationProfile。

Claude Code OTel 的端到端路径是：Claude Code → OTLP HTTP → 仅本机 Collector →
`.ael/otel/{logs,metrics,traces}.jsonl` → 按 `ael.run.id` 摄取 → Run 证据 / Explicit Contrast。
每个 telemetry / deep Managed Run 还会由官方 OpenTelemetry API/SDK 产生 `ael.run`、`workspace.prepare`、
`agent.execute`、`verifier.execute`、`workspace.capture` lifecycle spans；AEL trace 与 vendor
Agent trace 不建立伪造 parent，只通过 `ael.run.id` 关联。

历史 Claude proof Run `d0b926f4b18b4be482a6f7dd57053e1d`（Claude Code 2.1.229，1 个本地
Case，最终 PASS）在 Run evidence 中保存了 5 个 AEL lifecycle spans；Collector 按同一
`ael.run.id` 收到 38 个 OTel log、15 个 OTel metric 和 5 个真实 AEL trace span；这是历史证据，当前验收仍需重新检查新 Managed Run。证据同时记录
`ael-lifecycle.json` 的官方 OTLP exporter receipt 和 `telemetry/summary.json` 的 signal counts，不是
环境变量、空文件或 debug exporter 存在的证明。

Collector 只绑定 loopback，使用 Docker 启动（不会启动 Grafana/Tempo/Prometheus/Jaeger）：

    docker run -d --name ael-otel \
      -p 127.0.0.1:4317:4317 -p 127.0.0.1:4318:4318 \
      -v "$PWD/infra/otel-collector.yaml:/etc/otelcol-contrib/config.yaml:ro" \
      -v "$PWD/.ael/otel:/output" \
      otel/opentelemetry-collector-contrib:0.133.0 \
      --config=/etc/otelcol-contrib/config.yaml

    AEL_OTEL_ENDPOINT=http://127.0.0.1:4318 \
      uv run ael ui --root . --host 127.0.0.1 --port 8713

OTel 是 AEL 的一个 Evidence Source（证据来源），不取代 Verifier（task truth）、Workspace
（environment truth）或 Agent native trace；没有真实 telemetry 时 UI 会显示“证据不足”。对于
Claude Code，只有提供 `AEL_OTEL_ENDPOINT` 时，`telemetry` 才会通过单次运行环境启用 metrics/logs；
`deep` 还会显式打开 prompt 和 tool payload 字段。Codex 当前 CLI 不提供可用 OTel 输出，Run 页面会
明确显示“Agent 未提供 OTel”，并回退到 native / usage 证据；没有真实 trace span 时显示 Event
Timeline，不把 logs / metrics / native event 推断成 span hierarchy。只有真实 span 才显示 Trace
Waterfall，并使用真实的 `trace_id`、`span_id`、`parent_span_id`、start/end 和 duration。
默认仍是 `minimal`。

Run 页面会把这些证据分成几个可互相核对的视角：

- `AEL trajectory`：只按真实观察到的 READ / MUTATE / TOOL / VERIFY / COMPLETE 强锚点归纳行为；不把
  隐藏推理当成事实；
- `Telemetry` 摘要：显示 Model 调用、工具调用、输入/输出/总 tokens、缓存、成本、工具错误、活跃时长、Model、Collector
  记录和 signal 分布；没有 OTel 时改为显示 Agent 原生证据分布；
- `OTel trace / event` 瀑布：按时间偏移、耗时、operation、状态和安全属性展开每条 log、metric 或真实
  span；点击事件可以查看属性，原始 JSONL 只在页面底部按需展开；
- 文件行为统计：按文件显示可观察的 `C / R / U / D`（创建 / 读取 / 更新 / 删除）次数；如果只有
  Workspace 变更而没有明确文件事件，会单独标注为回退估计，不冒充 Agent 操作；
- Explicit Contrast：只在用户明确选择 Reference 后，把候选与 Reference 的强行为组局部对齐，并单独显示 Verifier、
  Workspace、native 和 OTel 的证据覆盖；顶部另有逐项指标表，直接比较时延、token、成本、工具调用和证据覆盖。

## External Session

Sessions 页面直接从本机 Collector 的 \`session.id\` 投影外部终端工作，不建立第二套 Outcome / Failure
状态。没有 \`ael.run.id\` 的 Session 标为 \`UNVERIFIED\`，可以查看 Model、duration、tokens、tools、
OTel signal 和 Event Timeline；没有真实 span 时不会显示 Waterfall。带有 \`ael.run.id\` 的 Managed
Session 只回链到已有 Run，不重复成为另一条实验记录。

真实外部 proof Session \`5375a38f-51fa-4be0-a464-b0e55f9eba3d\`（Claude Code 2.1.229）由普通
terminal 在临时目录直接执行 \`claude\` 产生，没有经过 AEL Runner；Collector 收到 36 条 OTel log、
7 条 OTel metric、0 个 trace span，Sessions 页面显示 \`UNVERIFIED\` 和 OTel Event Timeline。

从这个 Session 点击 \`Create Case\` 后，人工确认了 Case name、prompt、fixture snapshot
\`/tmp/ael-external-session.B7u8B1\` 和 verifier \`test -f answer.txt\`，生成可执行 CaseRevision
\`external-session-observation@f578f3b25d8c\`。它随后进入正常 Experiment
\`external-session-case-proof-0c4bda59\`，1 个真实 Claude Run 完成并由独立 verifier 判定 PASS；
fixture 被复制到 \`examples/cases/external-session-observation/fixture\`，没有自动生成或自动声明任务结果。

AEL 遵循 OpenTelemetry 的 signal 边界：log、metric 和 trace span 不互相冒充。当前 Claude Code
真实 Run 已经完成 OTLP → Collector → 持久化 → `ael.run.id` 关联，并能看到真实 logs/metrics；如果
Agent 没有发出 trace span，页面会明确显示“未收到真实 span”，而不会为了视觉效果伪造父子 span。
视图设计参考 [OpenTelemetry Trace API](https://opentelemetry.io/docs/specs/otel/trace/api/) 和
[OpenTelemetry 可观测性说明](https://opentelemetry.io/docs/concepts/observability-primer/)。

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

AEL 聚焦于 Variant、Case、Experiment、Run 的矩阵执行、差异比较和证据融合。失败模式只作为当前 Run 的诊断投影；它不是分布式运行器、Agent runtime、通用插件框架、OTel backend 或云服务。

## 差异证据

Explicit Contrast 会先检查 Case 版本，再使用 Experiment 的 Baseline/Candidate 关系或用户明确选择的 Reference，并展示 SAME/CHANGED/UNKNOWN 变量、`CONTROLLED`/`PARTIAL`/`DESCRIPTIVE` 置信度、verifier/workspace 证据、归一化锚点时间线和首次有意义的分歧。没有明确 Reference 时，系统保持 UNKNOWN，不会跨数据库猜 PASS。

Diagnosis（诊断）使用同一个 Explicit Contrast 证据包，而不是重新猜 Reference 或读取第二份轨迹。未配置 endpoint 时，系统仍会生成确定性的假设和未知项。配置 `AEL_DIAGNOSIS_BASE_URL`、`AEL_DIAGNOSIS_API_KEY` 与 `AEL_DIAGNOSIS_MODEL` 后，可以调用 OpenAI-compatible chat-completions endpoint；API key 只会放在请求 header 中。回归或失败 Run 可以进入“下一次 Experiment”：当前 Candidate 作为 Baseline，Duplicate 一个持久 Variant 后进入正常 Builder，由用户编辑后再运行同一 CaseRevision。

## 失败模式（诊断投影）

只有进程已完成且 verifier 返回 FAIL 的记录才会进入失败模式投影，并以 `OBSERVED` 开始；相同 Case revision 和 verifier signature 的重复失败会归并到同一 Failure Pattern，关联多个 Run。这个兼容性视图用于调查当前证据，不是长期 Failure Issue Tracker。

长期资产是人工确认的 Case / CaseRevision。`FIXED` / `REGRESSED` 只由同一 Case revision 上具有区分力的
Baseline / Candidate Experiment 推导；如果历史失败没有在 Baseline 中重现，即使 Candidate 全部通过，
结果也保持 `INCONCLUSIVE`。Regression Suite 只保存经过确认的 `case_id + revision`，不会复制 fixture。

## 可运行示例

仓库包含一个不调用模型的确定性矩阵：

    uv run ael run examples/experiments/fake-matrix.yaml --root .

然后启动本地 Web UI：

    uv run ael ui --root .

该示例会产生稳定通过和稳定失败两类结果。

对于已经持久化的实验，可以执行 `uv run ael compare <experiment-a> <experiment-b> --root .`，再从本地 UI 打开对应 Run 的 Explicit Contrast。AEL 将数据库和大型证据写入 `.ael/`，该目录已被 Git 忽略。

## 许可

内部全新原型项目。
