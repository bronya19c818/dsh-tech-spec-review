# 开发与部署说明

## 目录

```
src/index.js            Host 半边：拉起本地 MCP 服务、UI 同源路由、设置读写
src/client.js           浏览器半边：设置页与「审查状态」标签页
src/ui-routes.js        /api/technical-spec-review/* 的状态与设置接口
src/index.js 的 ui-routes 只读本地 runs/ 与 settings.json

py/technical_spec_review/
  parser.py numbering.py checks.py structure.py   文档解析与确定性检查（B01/B03/B04）
  language.py chapter.py engine.py                模型判断（B02/B05）与任务编排
  rules.py                                        规则服务客户端（目录 / 精确规则 / 语义检索）
  model.py prompts.py                             OpenAI 兼容对话模型
  rerank.py                                       客户端可选重排
  mcp_server.py                                   审查 MCP 工具
  local_stack.py                                  本地 MCP 服务入口
  pyenv.py                                        插件私有 Python 环境准备
```

## 依赖边界

客户端**不需要** PostgreSQL、pgvector、嵌入模型。依赖只有：`python-docx`、`httpx`、
`pydantic`、`mcp`、`python-multipart`、`uvicorn`、`python-dotenv`。

规则库与索引在服务端（见 rule-server 仓库）。客户端要连不上规则服务时，先确认
`规则服务 → 服务地址` 配置正确、服务端 `status.bat` 正常。

## 开发

```powershell
uv venv --python 3.12 .venv
uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt
$env:PYTHONPATH="py"
.\.venv\Scripts\python.exe -m technical_spec_review.cli stack   # 前台起 MCP 服务
```

冒烟：

```powershell
node tests/client-smoke.mjs      # 客户端四个挂载点注册契约
node tests/ui-routes-smoke.mjs   # Host 侧状态聚合与设置读写
node tests/plugin-apply.smoke.mjs  # 插件 Host 半边拉起/回收（需要真实解释器）
```

端到端：

```bat
run-e2e.bat
```

生成一份小样本 DOCX（含样式不一致与编号跳号），跑完整审查，校验全部子任务完成、
过程文件齐全、报告已生成、问题分组非空、编号跳号被检出。每次在 `artifacts/`
留一份 JSON 报告。需要规则服务在跑且对话模型可用，否则以退出码 2 跳过。

小样本一次约 1.5 分钟；用真实规范书（84 句）约 9 分钟，不适合做回归。

## 发布

```powershell
pnpm pack
plugin_manager(action="install_bundle", target="<绝对路径>.tgz")
```

改完 `src/` 需要重新安装才加载新代码；只改 `py/` 则重启本地服务即可（子进程每次重新导入）。

## 配置项

| 位置 | 用途 |
|------|------|
| 插件设置页 | 对话/重排/规则服务地址与模型名 → `$DSH_HOME/technical-spec-review/settings.json` |
| DSH 凭据库 | 三把密钥，引用名见 README |
| `keys.env`（仅开发） | 本地回退，正式安装不存在 |

`keys.env` 只用于开发调试：`local_stack` 会读它，但 `settings.json` 的值优先。

## 常见问题

**工具报 `CATALOG_HTTP_*` / 语义检索失败**
规则服务没起或地址不对。`rule-server\status.bat` 确认状态；设置页「规则服务」卡片应显示"已连接"。

**改设置后不生效**
保存只写文件。点「重启本地服务」，或等 20 秒自愈定时器。

**首次启动很慢**
在准备 Python 环境（建 venv + 装依赖，需联网一次）。之后离线可用。

**MCP 工具消失 / `Session not found`**
本地服务重启导致会话失效。插件面板关再开 `dsh-technical-spec-review`。
