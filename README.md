# dsh-tech-spec-review

技术规范书审查的 DeepSeek Harness 插件。把 DOCX 技术规范书拆成确定性检查与
章节内容审查，产出带证据的审查报告。

配套的规则服务端是 [`tech-spec-review-rule-server`](https://github.com/bronya19c818/tech-spec-review-rule-server)，
本插件只通过 HTTP 访问它。

## 分工

| 本插件（客户端） | 规则服务端 |
|------------------|-----------|
| 读本地 DOCX 的 OOXML（样式、编号、页边距） | PostgreSQL + pgvector 规则库 |
| B01 版面 / B03 结构 / B04 交叉引用（确定性） | 语义索引（原 Dify 知识库的替代） |
| B02 文字质量 / B05 章节内容（模型判断） | 嵌入模型（钉在服务端） |
| 对话模型调用、可选重排 | 规则发布与检索接口 |
| 审查 MCP 服务与 UI | 规则入库管道 |

为什么这么切：DOCX 解析必须读用户本机那份文件；规则库是共享资产、索引与模型必须
全网一致，所以放服务端。对话模型放客户端，用哪家由用户自己决定。

## 安装

插件是普通 npm 包，`dsh.bundle.patch` 声明配置层：

```powershell
pnpm pack
plugin_manager(action="install_bundle", target="D:\\...\\dsh-tech-spec-review-<ver>.tgz")
```

装好后在侧栏 **Plugins** 里可见；审查工具以 `mcp__technical-spec-review__*` 直接提供给
当前会话，不占用「MCP 连接器」的条目。

## 配置

设置 → 技术规范书审查：

| 分组 | 字段 | 说明 |
|------|------|------|
| 对话模型 | 接口地址、模型名、API Key | 需要支持 function calling |
| 重排模型 | 接口地址、模型名、阈值、API Key | 可选；不填则用服务端返回顺序 |
| 规则服务 | 服务地址、服务令牌（可选）、嵌入模型（只读）、索引规模（只读） | 嵌入模型由服务端决定，客户端改不了 |
| 本地服务 | Python 解释器 | 留空自动准备 |

密钥写入 DSH 凭据库（`$DSH_HOME/.credentials.yaml`），不进插件配置与日志：

| 引用名 | 用途 |
|--------|------|
| `TSR_CHAT_API_KEY` | 对话模型 |
| `TSR_RERANK_API_KEY` | 重排模型 |
| `TSR_RULES_TOKEN` | 规则服务令牌（服务端对外监听时才需要） |

保存只写配置文件，与后端是否在跑无关；「重启本地服务」是独立动作。

## 运行形态

- 插件自己拉起本地 MCP 服务（默认 `127.0.0.1:8775`），首次使用会用宿主解释器建一个
  插件私有 venv（`$DSH_HOME/technical-spec-review/pyenv`），不污染宿主 runtime。
- 运行数据也在 `$DSH_HOME/technical-spec-review/`（`runs/`、`workspace/`、`logs/`），
  不写包目录——包目录升级时会被整批替换。
- 规则服务端请先启动，否则工具会报连不上规则服务。

## 审查一本规范书

```text
1) 上传 DOCX：POST http://127.0.0.1:8775/upload_review_document（Bearer，multipart）
2) mcp__technical-spec-review__start_review(file_id, object_scope?, document_role?)
3) mcp__technical-spec-review__get_review_status(review_id)
4) mcp__technical-spec-review__get_review_results(review_id)
5) 失败重试 mcp__technical-spec-review__retry_review(review_id)
```

引擎对模型依从/传输类错误自动重试一次；单章失败不阻断其他章节，汇总阶段会被
`DEPENDENCY_FAILED` 挡住并如实标失败。

## 附带 Agent 预设

安装本插件同时注册预设「技术规范书审查专家」：限定审查流程、区分确定性结论与需人工
确认的候选、要求每条问题给出章节与原文依据。

## 已知边界

- 本机若配置了全局代理，插件内部所有回环请求都强制 `trust_env=False`。
- 本地 MCP 服务重启后，DSH 侧会话会失效（`Session not found`），在插件面板关再开即可。
- 只做「文件是否写明要求」的审查，不判断产品是否履约或验收合格。

开发与目录说明见 [`docs/deployment.md`](docs/deployment.md)。
