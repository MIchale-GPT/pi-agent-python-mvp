# PRD: 基于知识证据的 DWS 只读查询 Agent

状态: implemented · Phase 26 baseline · 方案: 方案 2（Evidence-bound Query Plan）

本 PRD 是已实现的证据绑定查询安全基线。SAG Agent 规划模式、规划会话、对应配置与可观测性由
后续文档 [`PRD-sag-agent-sql-planner.md`](PRD-sag-agent-sql-planner.md) 增量覆盖；证据账本、冻结
计划、授权与只读执行边界仍以本文为准。PRD 保留其原始写作语言，不维护逐行双语镜像。

## Problem Statement

Tau 当前可以让模型调用本地工具，但不能安全地完成下面这条业务链路：

1. 用户用自然语言提出数据问题；
2. Agent 先从指定 SAG 知识源查询表、字段、关联关系和业务口径；
3. Agent 根据知识证据生成 Huawei Cloud DWS SQL；
4. Tau 使用受控的只读数据库连接执行 SQL；
5. Agent 返回查询结论、实际执行的参数化 SQL、行数和截断状态。

如果只向模型暴露通用 MCP 调用和 `db_query(sql)`，系统无法可靠证明当前 SQL 是依据当前问题的
知识证据生成的，也无法在执行前冻结 SQL。模型还可能把 MCP 地址、数据库连接串或凭证带入工具
参数、日志和会话记录。单纯检查 SQL 是否以 `SELECT` 开头也不能构成安全边界，因为查询可能包含
修改型 CTE、锁定子句或有副作用的函数。

数据库连接还缺少用户可操作的配置入口。Tau Web 用户需要在不编辑源码或命令行环境变量的前提下
配置 DWS 主机、端口、数据库、账号、密码、SSL 模式和查询限制，同时保证密码不会被浏览器重新读取
或写入会话。

## Solution

在 `tau_coding` 中提供一个数据查询 Extension。Extension 对模型暴露四个按固定工作流衔接的
领域工具（顺序由服务端状态机强制），而不是暴露 SAG 的八个底层 MCP 工具或任意数据库管理能力：

```text
用户问题
  │
  ▼
data_knowledge_search ──► EvidenceBundle(bundle_id, evidence_ids)
  │                              │
  ├─ data_knowledge_read（可选） ┘
  │
  ▼
data_query_prepare ─────► QueryPlan(plan_id, frozen SQL, evidence, policy)
  │
  ▼
data_query_execute ─────► DWS read-only transaction
  │
  ▼
SQL + columns + ≤1000 rows + truncated + elapsed time + evidence
```

`bundle_id` 和 `plan_id` 是仅在当前 Agent run 中有效的不透明句柄。`data_query_prepare` 必须收到
当前 bundle 中至少一个具体证据 ID，并在服务端校验 SQL、绑定参数和策略，然后冻结 QueryPlan。
`data_query_execute` 只接受 `plan_id`，不再接受 SQL 或参数，因此模型不能在审核和执行之间替换语句。

生产环境由四层组成：

```text
Tau Web / CLI / TUI
        │
        ▼
tau_coding Data Query Extension
  ├─ DataQuestionService（工作流与状态门禁）
  ├─ EvidenceLedger / QueryPlanStore（run 内存状态）
  ├─ SagMcpKnowledgeBackend（Streamable HTTP MCP）
  └─ DwsPostgresQueryBackend（PostgreSQL wire protocol）
        │                         │
        ▼                         ▼
SAG knowledge MCP          Huawei Cloud DWS
```

`tau_agent` 继续只提供可移植的 AgentHarness、AgentTool、事件和执行协议，不依赖 MCP SDK、数据库
驱动、Web UI、凭证存储或 DWS 方言。CLI、TUI 和 Web 都通过同一个 Extension 工具契约获得能力。

Tau Web 增加全局“数据库连接”设置界面。它配置单个 DWS profile，默认使用
`sslmode=prefer`、查询超时 180 秒、最大返回 1000 行。密码只允许写入或替换，读取设置时只返回
`passwordConfigured`，绝不返回密码正文。设置界面提供“保存”和“测试连接”；测试连接只验证建立
连接、开启只读事务并执行轻量探测，不把“连接成功”描述成数据库权限已经被完全审计。

## User Stories

1. 作为 Tau 用户，我希望用自然语言提问业务数据，以便不需要先掌握具体表名和字段名。
2. 作为 Tau 用户，我希望 Agent 在生成 SQL 前查询指定知识库，以便 SQL 使用组织认可的业务口径。
3. 作为 Tau 用户，我希望 Agent 在知识证据不足时停止查询，以便它不会猜测表名、字段或关联关系。
4. 作为 Tau 用户，我希望 Agent 能展开某条检索证据的上下文，以便处理摘要不足以确定 SQL 的问题。
5. 作为 Tau 用户，我希望最终回答展示实际执行的参数化 SQL，以便我核对和复用查询逻辑。
6. 作为 Tau 用户，我希望 SQL 参数默认不被拼接进展示文本，以便敏感过滤值不被意外泄露。
7. 作为 Tau 用户，我希望查询结果标明返回行数和是否截断，以便我不会把前 1000 行误认为完整结果。
8. 作为 Tau 用户，我希望看到查询耗时，以便判断查询性能是否符合预期。
9. 作为 Tau 用户，我希望超过三分钟的查询被数据库端终止，以便一次错误 SQL 不会长期占用资源。
10. 作为 Tau 用户，我希望取消 Agent run 时正在执行的数据库查询也被取消，以便停止操作真正释放资源。
11. 作为 Tau 用户，我希望数据库错误以可理解的形式返回，以便我能修正问题而不会看到密码或完整 DSN。
12. 作为 Tau Web 用户，我希望从界面打开数据库连接设置，以便无需修改源码完成部署配置。
13. 作为 Tau Web 用户，我希望配置数据库主机、端口、数据库名和用户名，以便连接目标 DWS 实例。
14. 作为 Tau Web 用户，我希望通过密码输入框写入或轮换数据库密码，以便密钥不出现在普通配置文件中。
15. 作为 Tau Web 用户，我希望重新打开设置时只看到“密码已配置”，以便服务端不会把秘密回传给浏览器。
16. 作为 Tau Web 用户，我希望不填写密码直接保存其他字段时保留原密码，以便修改超时不会意外清除凭证。
17. 作为 Tau Web 用户，我希望将 SSL 模式配置为 `prefer`，以便匹配当前 DWS 连接要求。
18. 作为 Tau Web 用户，我希望超时和最大行数有明确默认值和输入校验，以便配置错误能在保存前被发现。
19. 作为 Tau Web 用户，我希望保存后测试连接，以便在发起 Agent 查询前发现网络、账号或 SSL 配置错误。
20. 作为安全管理员，我希望数据库账号本身只有允许对象的 `SELECT` 权限，以便模型生成的 SQL 不能写数据。
21. 作为安全管理员，我希望每次查询都运行在数据库强制的只读事务中，以便形成第二层写保护。
22. 作为安全管理员，我希望系统拒绝多语句、DDL、DML、修改型 CTE 和锁定查询，以便减少只读账号配置错误带来的风险。
23. 作为安全管理员，我希望模型不能传入 MCP URL、source ID、DSN 或凭证，以便连接边界由可信配置控制。
24. 作为安全管理员，我希望知识查询固定在已批准的 SAG source，以便 Agent 不能用无关知识解锁数据库查询。
25. 作为安全管理员，我希望 MCP 返回正文被当作不可信数据，以便知识文档不能覆盖系统和工具规则。
26. 作为审计人员，我希望一条数据库查询可以追溯到具体知识证据和冻结后的 SQL 计划，以便复盘生成依据。
27. 作为审计人员，我希望审计摘要记录 plan、SQL 指纹、耗时、行数、截断和结果状态且不包含秘密或参数值；参数值只作为 tool-call arguments 保存在本地 session JSONL、Web trace 和会话详情中，以便明确不同持久化表面的隐私边界。
28. 作为开发者，我希望真实 SAG 和 DWS 都位于窄适配器后面，以便单元测试使用 deterministic fake。
29. 作为开发者，我希望安全不变量由 run-scoped ledger 对 bundle、evidence 和 plan 的校验保证，以便即使模型在一次回复中并行发出多个 tool call、或将来 loop 支持真正并行执行，也无法绕过“先检索、后准备、再执行”的状态机（当前 loop 顺序执行只是实现现状，不构成安全保证）。
30. 作为开发者，我希望新 run 自动使旧 bundle 和 plan 失效，以便上一个问题的证据不能授权当前查询。
31. 作为开发者，我希望 session shutdown 和 Extension reload 关闭 MCP 与数据库资源，以便不会泄漏连接或后台任务。
32. 作为维护者，我希望该能力不修改 `tau_agent` 的可移植契约，以便其他宿主不被迫安装 MCP 或数据库依赖。
33. 作为维护者，我希望用户文档解释配置、查询流程和安全限制，以便首次使用者能正确部署和验收。

## Implementation Decisions

1. **采用 Extension，不新增插件运行时。** 数据查询能力属于 `tau_coding` 的应用集成；
   `tau_agent` 保持 provider、UI 和基础设施无关。Skill 可补充业务提示，但不能拥有连接或权限。
2. **模型侧固定为四个领域工具。** `data_knowledge_search` 查询当前问题；
   `data_knowledge_read` 只展开已经发现的证据；`data_query_prepare` 校验并冻结 SQL；
   `data_query_execute` 只执行冻结计划。MVP 不提供通用 `mcp_call` 或 `db_execute`。
3. **工具参数不包含基础设施配置。** MCP URL、Bearer token、source ID、数据库 host、port、
   database、username、password 和 SSL 设置只来自可信配置，模型不能选择或覆盖。
4. **知识源固定。** SAG 后端通过 Streamable HTTP 连接现有 MCP 服务，并将所有检索限定到
   source `19d09d3733c34716bcdf906738d10b03`。适配器内部把语义检索、精确检索和内容展开映射到
   SAG 的只读工具，不把八个远端工具直接注册给模型。MVP 搜索最多返回 5 条证据，每条搜索摘要
   最多 8 KiB；单次展开的证据正文最多 64 KiB，同一 bundle 的模型可见证据总量最多 256 KiB。
5. **证据账本按 run 隔离。** 每次 `agent_start` 创建新的 run 范围并清空旧状态。
   `bundle_id`、`evidence_id` 和 `plan_id` 必须由服务端生成并能在当前 ledger/store 中解析；
   新 run、reload 和 shutdown 后全部失效。句柄使用 `secrets.token_hex(16)` 或等价至少 128-bit
   的不可枚举随机值；ledger/store 校验必须同时绑定 session、run、状态机阶段和句柄有效期，
   任何一项不匹配即拒绝。
6. **准备阶段是执行前冻结点。** QueryPlan 绑定用户问题、run、知识 source、bundle、具体证据、
   参数化 SQL、参数摘要、策略版本和 SQL 摘要。准备成功后不能修改；执行阶段不接受替代 SQL。
   参数摘要只包含参数数量和通用类型（如 `integer`、`text`），不保存值、值前缀或普通哈希——
   低熵值（如账号、日期）的普通哈希可被枚举；若审计确实需要关联具体参数值，仅由审计系统使用
   带密钥的 HMAC 进行。同一 plan 在所属 run 和有效期内允许重复执行；每次执行都重新授权，并作为
   独立审计事件记录。
7. **SQL 使用位置参数绑定。** 用户值与 SQL 文本分离传给数据库驱动。动态标识符不能使用值
   占位符，必须来自知识证据和允许对象策略，不能由用户输入直接拼接。所有表和视图引用必须使用
   schema-qualified 名称；prepare 阶段拒绝无 schema 的关系引用。

   prepare 还必须解析并校验 SQL 中全部 schema、table/view 和 column 引用，使其符合部署时配置的
   对象 allowlist；不在清单内一律拒绝，不等待数据库权限错误兜底。只有关系策略明确允许全部列时
   才接受 `table_alias.*`，否则必须逐列校验。知识证据应提供生成 SQL 所需的完整限定名。
8. **DWS 使用 PostgreSQL wire adapter。** 连接配置的当前默认值为 host `localhost`、port
   `35432`、database `exchange_service`、`sslmode=prefer`。用户名和密码作为部署配置输入，
   不固化在源码、PRD、工具 schema 或日志中。
9. **只读采取纵深防御。** 首要边界是数据库层只读账号及最小对象权限；每次执行还必须开启
   `READ ONLY` 事务、拒绝多语句和已知写/锁定结构，并在成功或失败后回滚。SQL 文本检查不能
   代替数据库授权。
10. **执行策略固定。** 数据库 statement timeout 为 180 秒；最多返回 1000 行（读取第 1001 行
    仅用于判断截断）；MVP 默认限制总结果 1 MiB、单元格 64 KiB，避免少量超大字段挤爆模型上下文。
    截断结果携带 `truncationReasons`：

    - 行数达到上限 → `row_limit`
    - 下一完整行会使总结果超过 1 MiB → `byte_limit`（不返回半行）
    - 单元格超过 64 KiB → 返回明确标记的预览并记录 `cell_limit`

    任一限制触发都设置 `truncated=true`。处理一行时先逐格应用 `cell_limit`，再计算裁剪后完整行的
    序列化大小；若加入该行仍会超过总上限，则整行不返回并记录 `byte_limit`。

    **SQL 策略检查采用 `sqlglot` 的 PostgreSQL dialect 解析为 AST，fail-closed**：解析失败、
    语法无法确认或出现无法确认的 DWS 扩展语法时一律拒绝；以 DWS SELECT 语法文档建立兼容测试集。
    拒绝多语句、DDL、DML、`SELECT INTO`、修改型 CTE 以及 `FOR UPDATE`/`FOR SHARE` 等锁定子句。

    函数采用静态 allowlist 优先策略：只允许明确批准的聚合、数值、文本、日期和窗口函数；未知函数、
    用户自定义函数及未经批准的 schema-qualified 函数一律拒绝。另维护危险函数 denylist，至少覆盖
    `lo_*`、`pg_read_file`、`pg_ls_dir`、`dblink_*`、`pg_sleep`、`pg_terminate_backend` 等类别。
    数据库连接固定安全 `search_path`，数据库角色不得创建可遮蔽内置函数的对象。

    解析器和函数列表只是提前拦截，最终边界仍是数据库层只读账号、最小权限、危险函数 EXECUTE
    权限回收和只读事务；注意 PostgreSQL 只读事务并不禁止 `SELECT ... FOR UPDATE`，锁定查询必须
    由应用层策略拒绝。函数副作用不依赖运行时读取 `pg_proc` 判断。

    工具是否顺序执行不构成安全保证：当前 loop 对单次回复中的多个 tool call 总是顺序执行，但
    安全不变量由 run-scoped ledger 对 bundle、evidence 和 plan 的校验保证，即使将来并行执行
    也不能绕过。
11. **结果和 SQL 同时进入模型可见内容。** 查询结果包含参数化 SQL、列、行、返回行数、
    `truncationReasons`、耗时和证据引用。参数值默认不回显；错误返回经过脱敏，不包含密码、
    Bearer token 或完整 DSN。MVP 为 `data_query_prepare` 提供最小化 `render_call`，默认只显示参数
    数量而不显示参数值；其他工具先使用默认渲染。`data_query_execute` 的模型可见内容必须包含上述
    全部字段；富 `render_call`/`render_result` 作为后续 UI 增强。
12. **数据库配置是用户级、跨宿主共享，按实例生效。** CLI、TUI 和 Web 共享同一套用户级配置
    与凭据存储。环境变量用于部署覆盖，优先级为：环境变量 → 用户配置 → 默认值；被环境变量
    接管的字段在 Web 界面中标记为只读并禁用输入。修改连接配置不写入会话 JSONL；运行中的查询
    继续使用其启动时快照，新查询使用最新已保存配置。
13. **Web 配置 API 是唯一前端持久化边界。** 读取操作返回非秘密字段、
    `passwordConfigured: boolean` 以及每个字段的 `source`（`env`/`config`/`default`）；
    `source=env` 的字段标记为只读，前端禁用对应输入框。写入操作校验字段，空 password 表示保留，
    显式的凭证清除需要独立动作，避免误删。API 不接受 JDBC URL，以减少凭证嵌入和解析歧义。
14. **秘密与普通设置分开存储。** 非秘密字段存入 Tau 用户配置；数据库密码进入现有权限受限的
    credential store 或等价秘密存储，文件权限为仅当前用户可读写。浏览器响应、日志、trace、
    session 和错误信息均不得包含数据库密码或 MCP token。

    MVP 接受查询参数值作为 assistant tool-call arguments 保存在本地 session JSONL、Web trace 和
    会话详情中，其文件权限与现有会话记录一致。参数“不回显”仅指最终自然语言回答、execute 结果
    和默认折叠渲染；用户主动展开 trace、查看会话详情或导出 JSONL 时可以看到参数原值。
15. **连接测试不是权限认证。** “测试连接”验证配置格式、建立连接、开启只读事务和轻量查询，
    返回成功、耗时或脱敏错误。它不声称证明账号对所有数据库对象都无写权限；只读角色必须由
    DWS 管理侧配置和审计。
16. **授权策略按工具分级，并由宿主适配器统一执行。** 固定可信只读 SAG 下，
    `data_knowledge_search`/`data_knowledge_read` 自动执行；`data_query_prepare` 只做校验与冻结；
    `data_query_execute` 每次执行只确认一次，展示参数化 SQL、证据数量、180 秒超时和 1000 行限制。
    取消或拒绝执行时不返回部分结果。

    Web 的 `before_tool_call` 按工具名分类：前三个数据工具直接放行，只有 `data_query_execute`
    创建授权请求。确认内容不能来自模型只提供 `{plan_id}` 的 tool arguments；Extension 通过窄的
    只读接口生成 `QueryPlanApprovalView`，包含冻结后的参数化 SQL、证据数量、超时和最大行数。
    Web 授权层只消费该展示负载，不直接访问 QueryPlanStore 内部结构。

    Web 的 `tool_authorization_requested` 为 execute 附加 `dataQueryApproval` 展示负载；这属于本 PRD
    对 `tau_coding` Web 事件的增量。用户批准后，授权 broker 签发仅内存存在、一次性消费并绑定
    session、run、tool-call ID 和 plan ID 的 approval receipt。execute 检测并消费有效 receipt 后
    不再调用 `context.ui.confirm`，因此 Web 不会因 NullUiBridge 发生第二次必然失败的确认。

    TUI 没有 Web receipt 时通过 Extension `context.ui.confirm` 确认；非交互 print CLI 默认
    fail-closed。部署方只有显式设置 `TAU_DATA_AUTO_APPROVE_EXECUTE=true` 才能在非交互模式自动
    批准，且必须向 stderr 输出执行提示。不得安装对所有扩展恒真返回的 Web UiBridge；授权适配均
    位于 `tau_coding`，不改变 `tau_agent` 契约。
17. **生命周期由 Extension 管理。** `setup` 同步注册工具和事件；真实连接延迟到异步调用或
    session start；shutdown/reload 关闭 MCP client、数据库池和未完成任务。数据库查询在独立任务中
    执行，另起取消监视任务轮询 `ToolCancellationToken`；检测到取消后调用数据库 driver 的 cancel，
    并回滚或废弃该连接，不能只等待 loop 自行中断。
18. **依赖保持在应用层。** MCP SDK 和 PostgreSQL/DWS 驱动只由数据查询 Extension/adapter
    引入，不成为 `tau_agent` 依赖。具体库版本在实现阶段按 DWS 兼容性验证后固定。
19. **不持久化完整查询结果。** 默认 session 和审计日志只保存有界摘要、SQL/指纹、证据定位、
    行数、耗时和状态；完整数据行仅存在于当前工具结果和模型上下文。若未来需要持久化，另行设计
    数据分类、保留期和访问控制。
20. **配置完整性决定工具是否注册。** SAG endpoint 采用环境变量 → 用户配置 → 本机默认地址
    `http://localhost:8100/mcp/` 的优先级；SAG token 采用环境变量 → credential store 的优先级且
    没有默认值。MVP 不提供 SAG endpoint/token 的 Web UI，固定 source 也不可配置。DWS 或 SAG
    必需配置缺失时不注册四个工具，而是产生脱敏诊断；用户保存配置后需要新建 session 或 reload
    才能启用工具。Web 设置界面保存成功后必须明确提示“新建会话或 reload 后生效”，不能让用户在
    当前会话中误以为工具已经动态出现。

## Testing Decisions

测试只验证外部可观察行为，不断言内部类调用顺序。主要测试 seam 是真实 Extension 注册后的四个
`AgentTool.execute()`；Web 配置使用 HTTP API 作为第二个必要 seam。真实网络和数据库只用于独立的
选择性集成测试。

1. **Extension 工作流测试。** 使用 fake knowledge backend、fake query backend、deterministic
   ID factory 和 fake clock，通过真正的工具入口验证：成功检索签发 bundle；空证据不能准备；
   无效或过期 bundle/evidence/plan 被拒绝；prepare 后 SQL 不可替换；新 run 使旧句柄失效；工具
   结果包含 SQL、证据、行数、截断和耗时；同一 run 重复执行同一 plan 会分别授权和审计；approval
   receipt 只能由匹配的 session/run/tool-call/plan 消费一次，伪造、回放和交叉使用均被拒绝。
2. **SQL 策略测试。** 表驱动覆盖普通参数化 SELECT、CTE SELECT、多语句、DDL、DML、修改型
   CTE、`SELECT INTO`、`FOR UPDATE`/`FOR SHARE`、允许函数、未知函数、危险函数、schema-qualified
   函数、无 schema 的关系、关系/列 allowlist 命中与越界、受限 `*`、注释和字符串中的关键字，
   以及解析失败、语法无法确认和未知 DWS 扩展语法（一律拒绝）。另按 DWS SELECT 语法文档建立
   兼容用例集，防止合法查询被误杀。测试期望是“允许或拒绝”行为，不绑定 sqlglot 内部节点结构。
3. **DWS adapter 测试。** fake driver 验证只读事务、180 秒 statement timeout、参数独立绑定、
   fetch 1001/return 1000、三种截断原因（`row_limit`/`byte_limit`/`cell_limit`）与 `truncated`
   标记、先裁剪单元格再判断完整行字节上限、回滚、取消（监视任务 → driver cancel）和错误脱敏。
   可选真实 DWS 测试只使用专用测试账号并通过显式环境开关启用，默认测试套件不得连接当前数据库。
4. **MCP adapter 测试。** 使用内存 fake MCP server 验证 initialize、Bearer header 注入、固定
   source、search/read 映射、超时、错误转换、search top 5、摘要 8 KiB、展开正文 64 KiB、
   bundle 256 KiB 总上限和 shutdown。测试输出不得打印 token。
5. **Web 配置 API 测试。** 沿用现有 Tau Web handler 测试模式，验证默认值、字段校验、
   `source` 标记与只读字段、保存后读取、`passwordConfigured`、空密码保留、credential store
   权限、测试连接的成功与脱敏失败。**密码泄露回归**：API 响应、session JSONL、Web trace 文件、
   日志和错误文本中均不得出现密码正文或 MCP token。
6. **前端测试。** 将“表单值 ↔ API payload/validation state”提取为无 DOM 依赖的纯函数，使用
   Node 内置测试能力验证默认值、错误映射、密码保留提示、保存状态以及“新建会话或 reload 后
   生效”的成功提示；不为 CSS 细节写测试。
7. **端到端验收。** 在受控测试数据上输入自然语言问题，观察 trace 顺序必为检索、可选阅读、
   prepare、execute；核对最终回答展示参数化 SQL、最多 1000 行和截断状态；核对新 session/run
   不能复用旧 plan。分别验证 Web execute 单次弹窗、TUI confirm、print CLI 默认拒绝及显式
   auto-approve 提示。Web 的 search/read/prepare 不得弹窗，execute 弹窗必须显示从冻结 plan
   生成的 `dataQueryApproval`，不能显示模型伪造的展示字段，放行后不得再次调用 NullUiBridge。
8. **回归检查。** 运行完整 Python 测试、类型检查、lint 和格式检查；确认未安装或未配置数据
   Extension 时，四个工具不会注册且现有 CLI、TUI、Tau Web 和 provider 配置行为保持不变；
   配置完成并 reload 或新建 session 后工具才出现。
9. **宿主授权适配测试。** Web handler 测试验证按工具名自动放行前三步、只为 execute 发布一次
   授权事件、拒绝无效 plan、展示负载来自 `QueryPlanApprovalView`、批准后签发并消费一次性 receipt。
   TUI 使用 fake UiBridge 验证 confirm；print CLI 使用 Null/Stderr bridge 验证默认拒绝和显式
   auto-approve。其他 Extension 的 confirm 不得被数据工具的 Web 授权机制自动放行。

## Out of Scope

- 任何 INSERT、UPDATE、DELETE、MERGE、DDL、COPY 写入或数据库管理操作
- 让模型动态添加数据库连接、切换任意 DSN、提交用户名密码或选择 SAG source
- 把 SAG 的八个 MCP 工具全部直接暴露为 Tau 工具
- 通用 MCP host 的 resources、prompts、sampling、elicitation、roots 或动态 tool refresh
- 多数据库 profile、租户级路由、跨数据库 join 和写库/读库自动选择
- 自动证明数据库账号绝对只读；该属性由 DWS 权限配置和外部审计保证
- 在界面中展示、复制或导出已保存的数据库密码和 MCP token
- 默认展开并展示 SQL 参数值
- 持久化完整查询结果、结果导出、图表、分页和后台批量任务
- 自动重写失败 SQL 或无限重试；MVP 失败后由 Agent 解释并由用户决定是否继续
- 将方案发布为 GitHub Issue 或 PR；当前 PRD 只保存在仓库文档中

## Further Notes

- 当前已确认的运行参数是：DWS 只读账号、`sslmode=prefer`、statement timeout 180 秒、最大
  1000 行，并在结果中展示参数化 SQL。
- 当前 SAG 接入为 Streamable HTTP MCP，知识 source 固定为
  `19d09d3733c34716bcdf906738d10b03`。MCP endpoint 是可信部署配置；token 是秘密配置。
- SAG endpoint/token 在 MVP 中没有 Web 配置界面：endpoint 可由环境变量或用户配置提供，token
  只能来自环境变量或 credential store；固定 source 不允许用户或模型覆盖。
- 已经出现在聊天或文档中的数据库密码和长期 JWT 应视为已暴露；联调前应轮换，之后只通过秘密
  配置提供。
- 上线前仍需由数据库管理员给出允许访问的 schema、表或只读视图，并确认是否禁止系统目录、
  `EXPLAIN`、存储过程和用户自定义函数。MVP 默认不开放这些能力。
- 多轮追问要求重新检索知识并重新 prepare 是预期安全行为：新的 agent run 会使上一 run 的 bundle
  和 plan 全部失效，用户追问同一问题时 Agent 必须重新走“检索 → 准备 → 执行”链路，这不是缺陷。
- 证据绑定保证查询可溯源、计划不可变，不自动证明 SQL 与证据语义一致；一致性由模型判断，审计
  可通过 plan 中的证据引用回溯复核。这是 LLM Agent 方案的固有边界。
- 模型应优先生成标准 PostgreSQL SELECT 子集。合法的 DWS/GaussDB 特有语法在加入兼容用例集前
  可能被 fail-closed 拒绝，这是 MVP 的预期安全取舍。
- 实现按 Tau 项目纪律拆分为小阶段：配置模型与 Web UI、工作流与 fake adapters、SAG adapter、
  DWS adapter、端到端联调；每个阶段同步更新 `dev-notes/` 和 `website/content/`。
