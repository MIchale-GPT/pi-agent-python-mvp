# Tau Web TODO

这份清单跟踪 Tau Web（A / Trace Workbench）尚未开发的主要功能。

## 当前基础

Tau Web 目前已经支持：

- 浏览已索引的本地会话及其活动分支
- 向已有会话发送任务
- 通过 SSE 接收实时 `CodingSessionEvent`
- 取消当前运行
- 配置唯一可用的 OpenAI-compatible 连接（URL、Key、模型名称）
- 在新建和已有会话中选择受支持的 Thinking level
- 执行适用于 Web 的斜杠命令
- 在运行中发送 steering / follow-up 消息
- 在浏览器中确认或拒绝工具调用
- 断线重连并重新读取持久化会话记录

## P0：建立完整会话入口

- [x] **新建会话**
  - 选择项目目录
  - 编辑唯一 Provider 连接的 URL、Key 和模型名称
  - 选择会话级 Thinking level
  - 创建成功后自动进入新会话

- [x] **会话管理**
  - 重命名会话
  - 删除会话，并提供明确的危险操作确认
  - 导出会话

## P1：补齐 Agent 运行控制

- [x] **Provider、模型与 Thinking 配置**
  - 展示当前 Provider、模型和 Thinking level
  - Web 只展示默认或首个具有可用凭据的 OpenAI-compatible Provider
  - 切换结果写入会话状态

- [x] **斜杠命令**
  - 接入 `/help`
  - 接入 `/compact`
  - 逐步覆盖 Web 端适用的其他内置命令

- [x] **运行中消息控制**
  - 支持 steering 消息
  - 支持 follow-up 消息
  - 展示队列状态，并允许清理待处理消息

- [x] **工具授权与确认弹窗**
  - 在工具执行前展示授权请求
  - 支持允许、拒绝和取消
  - 断线时采用安全的默认行为

## P2：补齐工作区能力

- [ ] **文件浏览与 Diff 查看**
  - 浏览当前项目文件树
  - 查看文本文件
  - 查看 Agent 产生的 Diff

- [ ] **分支树切换与会话恢复**
  - 展示会话树和当前活动叶节点
  - 切换到历史分支
  - 恢复或继续已有会话

- [ ] **图片和附件**
  - 上传或粘贴图片
  - 将附件转换为 Tau 支持的消息内容
  - 在会话记录中正确展示附件

## P3：远程访问

- [ ] **远程访问认证**
  - 在允许非 loopback 绑定前提供身份认证
  - 定义会话、文件和命令接口的授权边界
  - 增加 CSRF、Origin、速率限制和安全审计
  - 在完成安全评审前继续默认绑定 `127.0.0.1`

## 完成标准

每项功能完成时应同时具备：

- 确定性的测试，优先使用 fake provider 和 fake tool
- `dev-notes/` 下的架构或实现说明
- `website/content/` 下的用户文档更新
- `uv run pytest`、Ruff 和 mypy 验证通过
