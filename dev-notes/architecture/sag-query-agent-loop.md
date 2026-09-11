# SAG Query Agent Loop

实施记录，2026-09-10，依据 SAG 的 r4 PRD。

## 宿主与会话

`HeadlessQueryExecutor` 复用 `TauWebRuntime` 和 `CodingSession`，不创建另一套
Agent 循环，也不启动 HTTP 服务。每个执行器仅处理一个问题，跨轮重新创建执行器，
恢复同一个 JSONL。worker 为每轮独立进程，四工具共用一个 DataQuestionService。
会话目录为 `TAU_QUERY_SESSION_ROOT/<tau_session_id>/`；内部使用原 SessionManager
索引布局，隔离不同会话的索引写入。默认目录为 `~/.tau/query-sessions`，部署时
设到运行时持久卷。provider/model 从服务端配置选择并持久化，凭据保持 Tau home。

基础工具为空，技能和外部扩展发现关闭，只从打包扩展路径装载四个问数工具。
工具不完整时拒绝启动。问句不执行斜杠命令、不展开提示词模板。普通 CLI/Web
保留默认行为。宿主使用可靠事件队列，不能套用浏览器有损 SSE 重同步机制。

`prompt()` 返回内部原生事件，`result` 为 PRD 最终结果结构；原生事件不允许
直接公开给浏览器。结果投影仅向 trace 写参数数量、工具名、状态及耗时，
SQL 参数和结果行只存在于受控结果及会话审计中。无 SQL 时 lastResult 为 null。

执行确认、免询问、工具预算在同一宿主钩子按取消、预算、授权顺序处理。
确认时间按整轮累计，不计入执行预算。步数耗尽后给模型最多 20 秒总结，
总执行时长仍受 MAX_SECONDS 限制。时间耗尽通过现有取消令牌中断，
不响应取消的异步任务在 2 秒后强制取消；无法形成总结时保留已有结论并显示预算说明。

## 迁移与备份

SAG 增加可空唯一 tau_session_id、默认空 trace 及固定 execution_mode。
映射使用服务端 UUID hex，在调用方事务内分配且不自行提交；历史不回填映射。
启动迁移和独立 `scripts/migrate_query_agent.py` 复用幂等迁移函数。

本机先备份实际源码 `.data` 和 `~/.tau`，再修改迁移代码；本机没有 sagdata 卷
和 tau/tau-runtime/.tau。SQLite backup API 副本 integrity_check=ok。
热重载应用迁移后对照备份核对 20 个会话、28 个查询，原字段未改变或删除。
备份位置在工作区 `tmp/backups/agent-loop-p1-20260910T064333Z`，不提交备份数据。

## 验证

fake provider 驱动真实 harness 与扩展，覆盖 SQL 失败修复、同实例 planner
对话、跨轮 JSONL 恢复及旧证据失效重搜、四工具隔离、确认取消、字面问句、
慢消费者、预算及超时。SAG 测试使用临时 SQLite。

全量检查已有基线问题：SAG 全量在中止前 115 passed / 16 failed；
从未修改 HEAD 导出副本复现 Agent 权限和解析器两个代表性失败。
Tau 原 `extension.py` identifiers 类型错误已通过局部变量收窄修复，
mypy 全部 119 个源文件通过；全库格式检查仍有未涉及文件的既有问题。
聚焦回归通过不等于 PRD 全量回归门槛通过。

## 结果展示补充

共享工具提示词要求中文业务分析与中文业务列名。网页在展示层去除重复表格，
不再追加改变查询决策的提示词，也不再提前拦截英文输出别名。
SQL 语法、只读策略和证据校验均由原服务负责。前端统一呈现分析、图表、
公司/日期/指标明细，旧英文字段只对已知名称作展示映射，不猜测单位。
轨迹使用 toolCallId 合并调用/结果，摘要区固定最高 160px 并支持滚动展开。

## CLI 问数能力对齐（2026-09-11）

09-11 同题日志显示两个问题：逐月拆分耗尽网页预算，以及仅含图表的规划回复
被外层模型误当完整方案后猜表。共享 search guideline、工具描述和参数说明改为
保留完整范围，全年趋势优先一次规划及集合查询；独立查询仍可拆分，不引入业务路由。
服务为纯图表 JSON 回复添加 incomplete 提示，保留原文与证据，允许模型补齐；
正常 SQL、澄清和证据不足回复不受这一窄检查影响，不新增“必须含 SQL”的硬闸。

真实验收首次虽返回 12 行，但上游选择了单体而非 CLI 成功记录的合并口径，未作为
通过。补充共享 PLANNING_SCOPE_RULES，同时进入外层工具提示词与上游规划请求：
显式用户口径优先，其次知识库简称映射，不允许以“未指定”直接默认单体。
再次通过网页同款 HeadlessQueryExecutor 和同题快照运行只读验收，约 103 秒、
1 次检索 + 2 次 prepare + 1 次 execute 返回 12 行，按月份用 Decimal 对比，
单月与累计金额均与 CLI 成功记录完全一致。此次未通过浏览器创建业务会话，
验收会话保存在工作区 tmp/query-parity-acceptance，不提交原始业务数据。

两入口仍共用既有 CodingSession 和扩展；网页独立用户、会话、授权以及有界预算
保持不变。fake provider 双入口回归覆盖同工具定义、同 SQL 接受行为、全年 12 行
结果及 chart-only 后再次规划，原会话恢复、隔离和确认取消测试继续运行。

## 2026-09-11 会话证据复用

会话记录与执行授权分开管理。真实 execute 工具结果的私有 details 保存
`reuseEvidence`，包括来源计划、SQL/参数、图表、原始证据 ID、会话和配置指纹。
每轮重置 ledger/plan store 后从当前分支的 ToolResultMessage 恢复可复用索引，
不读取用户文本或助手伪造的 JSON。工具正文仅公开 `reusablePlanId`。

`prepare_reused` 用 sqlglot scope 和 schema qualification 校验所有引用的表字段。
指标单位沿用时核对表达式；重新签发本轮证据及计划 ID，确认仍由同一 execute
流程执行。原始证据一小时失效，派生不续期，配置变化失效。压缩丢失真实工具
记录时回到 SAG，不新增另一份数据库或恢复旧文本的兼容路径。

search 在有会话证据而未声明新知识缺口时只返回本地索引；只有
`newEvidenceReason` 或 SQL 错误修复才触发上游请求。这避免单靠提示词要求模型
跳过检索，同时保留新指标、新表字段的取证能力。

确定性验证见 `test_query_reuse.py`、`test_query_reuse_session.py`，覆盖重建执行器、
新字段、作用域、有效期以及每次确认。SAG 图表允许 NULL 断点，不伪造零值。
psycopg 执行边界转义字面百分号，审计/展示仍保留原 SQL，不再因百分比表头触发
无意义的上游 SQL 修复。
