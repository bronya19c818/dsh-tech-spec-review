/**
 * 技术规范书审查 —— Host half。
 *
 * 职责只有三件：
 *   1) 按需拉起本地后端栈（嵌入式 PostgreSQL + 规则服务 + 审查 MCP + UI 数据接口）
 *   2) 栈被强杀后按 30 秒节奏自愈重拉
 *   3) 给 Agent 暴露一个后端自检工具
 *
 * 边界都放在别处，这里只做生命周期：
 *   - MCP 工具由 cordis.patch.yml 的静态行提供（profile 安装的插件无法 import
 *     宿主的 @deepseek-ai/dsh-mcp-client，动态挂载在这条安装形态下不可用）
 *   - 状态页/设置页的数据接口由本地栈自己的 HTTP 服务提供，带 CORS 供 GUI 直连
 *   - 凭据不从配置文件读：对话 key 与嵌入 token 由 DSH 凭据存储解析后只注入子进程 env
 *   - 运行数据不写包目录：profile 安装的 bundle 升级时整个目录会被替换
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, openSync, appendFileSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { UI_PREFIX, handleUiRequest } from './ui-routes.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')

/** 凭据引用名（DSH 凭据存储里的 env key），不是凭据值本身。
 *  嵌入模型与它的密钥都在规则服务端，客户端只持有对话与重排两把。 */
export const CHAT_KEY_REF = 'TSR_CHAT_API_KEY'
export const RERANK_KEY_REF = 'TSR_RERANK_API_KEY'
/** 规则服务的访问令牌：仅当服务端对外监听并设置了 RULE_SERVER_TOKEN 时才需要。 */
export const RULES_TOKEN_REF = 'TSR_RULES_TOKEN'

/** 设置文件里的字段到子进程环境变量的映射；设置文件本身由本地栈读取。 */
const SETTINGS_ENV = {
  chatBaseUrl: 'TSR_MODEL_BASE_URL',
  chatModel: 'TSR_MODEL',
  rerankBaseUrl: 'TSR_RERANK_BASE_URL',
  rerankModel: 'TSR_RERANK_MODEL',
  rerankThreshold: 'TSR_RERANK_THRESHOLD',
  rulesBaseUrl: 'TSR_REGISTRY_URL',
}

function pluginLog(dataDir, line) {
  if (!dataDir) return
  try {
    const dir = join(dataDir, 'logs')
    mkdirSync(dir, { recursive: true })
    appendFileSync(join(dir, 'plugin.log'), `${new Date().toISOString()} ${line}\n`, 'utf8')
  } catch {
    // 日志是诊断辅助，写不进去不能影响功能
  }
}

/** 解释器解析顺序：显式配置 → 环境变量 → 插件自带 venv → 包内 .venv → 随包解释器 → PATH。 */
export function resolvePython(config = {}, root = ROOT, dataDir = '') {
  const win = process.platform === 'win32'
  const candidates = [
    config.pythonPath,
    process.env.TSR_PYTHON,
    dataDir ? join(dataDir, 'pyenv', win ? 'Scripts' : 'bin', win ? 'python.exe' : 'python') : '',
    join(root, '.venv', win ? 'Scripts' : 'bin', win ? 'python.exe' : 'python'),
    join(root, 'python', win ? 'python.exe' : 'bin/python'),
  ].filter(Boolean)
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate
  }
  return candidates[0] || (win ? 'python' : 'python3')
}

/** 数据目录：包目录会被升级整批替换，因此默认落 DSH_HOME。 */
async function resolveDataDir(config) {
  const explicit = config.dataDir || process.env.TSR_DATA_DIR
  if (explicit) return explicit
  try {
    const mod = await import('@deepseek-ai/dsh-home-paths')
    if (typeof mod.dshHomePath === 'function') return mod.dshHomePath('technical-spec-review')
  } catch {
    // 非 profile 启动（仓库内测试）时该包可能不可见
  }
  const home = process.env.DSH_HOME || join(process.env.HOME || process.env.USERPROFILE || '.', '.dsh')
  return join(home, 'technical-spec-review')
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return undefined
  }
}

function loadSettings(dataDir) {
  const stored = dataDir ? readJson(join(dataDir, 'settings.json')) : undefined
  return stored && typeof stored === 'object' ? stored : {}
}

function settingsToEnv(settings) {
  const env = {}
  for (const [key, name] of Object.entries(SETTINGS_ENV)) {
    const value = settings[key]
    if (value === undefined || value === '' || value === null) continue
    env[name] = String(value)
  }
  return env
}

/**
 * 准备插件自带的 Python 环境：用宿主解释器建 venv 并装依赖。
 * 走子进程调用 pyenv 模块，避免在 Host 进程里跑 pip。失败只记日志。
 */
function preparePythonEnv(dataDir) {
  const launcher = resolvePython({}, ROOT, dataDir)
  const bare = !launcher.includes('/') && !launcher.includes('\\')
  if (!bare && !existsSync(launcher)) {
    pluginLog(dataDir, `python env: no launcher (${launcher})`)
    return
  }
  const code = 'from technical_spec_review.pyenv import ensure_environment;'
    + 'import pathlib,sys;'
    + 'p=ensure_environment(pathlib.Path(sys.argv[1]), log=lambda m: print(m, flush=True));'
    + 'print("PYENV=" + (str(p) if p else ""))'
  try {
    const child = spawn(launcher, ['-c', code, dataDir], {
      cwd: ROOT,
      env: { ...process.env, PYTHONPATH: join(ROOT, 'py'), PYTHONIOENCODING: 'utf-8' },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    let output = ''
    child.stdout.on('data', (chunk) => { output += String(chunk) })
    child.stderr.on('data', (chunk) => { output += String(chunk) })
    child.on('exit', (code_) => {
      for (const line of output.trim().split('\n').slice(-6)) pluginLog(dataDir, `python env: ${line}`)
      pluginLog(dataDir, `python env preparation exit=${code_}`)
    })
  } catch (error) {
    pluginLog(dataDir, `python env preparation failed: ${error?.message}`)
  }
}

export function apply(ctx, config = {}) {
  const state = { dataDir: '', child: null, starting: null, mcpPort: '' }

  const stopChild = () => {
    if (!state.child) return
    try {
      if (process.platform === 'win32') {
        // pgserver 会拉起一组 postgres 子进程，需要整树结束
        spawnSync('taskkill', ['/PID', String(state.child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true })
      } else {
        state.child.kill('SIGTERM')
      }
    } catch {}
    state.child = null
  }

  async function readCredential(ref) {
    const credentials = typeof ctx.get === 'function' ? ctx.get('credentials') : undefined
    if (credentials === undefined) return undefined
    try {
      return (await credentials.resolve(ref))?.value
    } catch {
      return undefined
    }
  }

  async function isMcpUp(port, timeoutMs = 1500) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    try {
      await fetch(`http://127.0.0.1:${port}/mcp`, { method: 'GET', signal: controller.signal })
      return true
    } catch {
      return false
    } finally {
      clearTimeout(timer)
    }
  }

  function stackPorts(fallbackMcp) {
    const info = state.dataDir ? readJson(join(state.dataDir, 'stack.json')) : undefined
    const portOf = (url, dflt) => {
      const m = /:(\d+)/.exec(url || '')
      return m ? Number(m[1]) : dflt
    }
    return { mcp: portOf(info?.mcp_url, fallbackMcp) }
  }

  async function waitStackUp(expectMcp, totalMs = 240000) {
    const deadline = Date.now() + totalMs
    while (Date.now() < deadline) {
      const { mcp } = stackPorts(expectMcp)
      if (mcp && await isMcpUp(mcp)) return mcp
      await new Promise((r) => setTimeout(r, 1000))
    }
    return 0
  }

  async function ensureStack() {
    const settings = loadSettings(state.dataDir)
    const expectMcp = Number(settings.mcpPort || process.env.TSR_MCP_PORT || 8775)
    const known = stackPorts(expectMcp)
    if (known.mcp && await isMcpUp(known.mcp)) {
      state.mcpPort = String(known.mcp)
      return true
    }
    if (state.starting) return state.starting
    state.starting = (async () => {
      const python = resolvePython(
        { ...config, pythonPath: settings.pythonPath || config.pythonPath }, ROOT, state.dataDir,
      )
      const bare = !python.includes('/') && !python.includes('\\')
      if (!bare && !existsSync(python)) {
        pluginLog(state.dataDir, `python not found: ${python}`)
        ctx.logger?.warn?.(`[technical-spec-review] python 未找到：${python}`)
        return false
      }
      const env = { ...process.env, PYTHONPATH: join(ROOT, 'py') }
      env.PYTHONIOENCODING = 'utf-8'
      env.TSR_DATA_DIR = state.dataDir
      Object.assign(env, settingsToEnv(settings))
      const chatKey = await readCredential(CHAT_KEY_REF)
      const rerankKey = await readCredential(RERANK_KEY_REF)
      const rulesToken = await readCredential(RULES_TOKEN_REF)
      if (chatKey) env.TSR_MODEL_API_KEY = chatKey
      if (rerankKey) env.TSR_RERANK_API_KEY = rerankKey
      if (rulesToken) env.TSR_REGISTRY_API_KEY = rulesToken
      // 本地回环请求必须绕开系统代理
      for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']) {
        delete env[k]
      }
      const logDir = join(state.dataDir, 'logs')
      mkdirSync(logDir, { recursive: true })
      const out = openSync(join(logDir, 'stack.out'), 'a')
      const err = openSync(join(logDir, 'stack.err'), 'a')
      pluginLog(
        state.dataDir,
        `spawning: ${python} -m technical_spec_review.local_stack `
        + `(keys: chat=${chatKey ? 'set' : 'missing'} rerank=${rerankKey ? 'set' : 'missing'})`,
      )
      state.child = spawn(python, ['-m', 'technical_spec_review.local_stack'], {
        cwd: ROOT, env, stdio: ['ignore', out, err], windowsHide: true,
      })
      state.child.on('error', (e) => pluginLog(state.dataDir, `spawn error: ${e?.message}`))
      state.child.on('exit', (code, signal) => {
        pluginLog(state.dataDir, `child exit code=${code} signal=${signal}`)
        state.child = null
      })
      const port = await waitStackUp(expectMcp)
      if (port === 0) {
        ctx.logger?.warn?.('[technical-spec-review] 本地栈启动超时')
        return false
      }
      state.mcpPort = String(port)
      pluginLog(state.dataDir, `stack ready; mcp 127.0.0.1:${port}`)
      return true
    })().finally(() => { state.starting = null })
    return state.starting
  }

  void (async () => {
    state.dataDir = await resolveDataDir(config)
    mkdirSync(state.dataDir, { recursive: true })
    const has = (name) => Boolean(typeof ctx.get === 'function' ? ctx.get(name) : undefined)
    pluginLog(
      state.dataDir,
      `apply invoked; dataDir=${state.dataDir} services timer=${has('timer')} `
      + `tools=${has('tools')} credentials=${has('credentials')} webServer=${has('webServer')}`,
    )
    // 首次运行准备插件自带的 Python 环境（用宿主解释器建 venv，不污染宿主 runtime）。
    // 失败不阻断：ensureStack 会回退到当前可用的解释器。
    preparePythonEnv(state.dataDir)
    void ensureStack()
  })()

  // 自愈：栈被强杀、首次初始化超时，或设置变更需要重启时，按固定节奏重新拉起。
  ctx.inject(['timer'], (scope) => {
    scope.effect(() => {
      const stop = scope.timer.interval(() => { void ensureStack() }, 20000)
      return () => stop()
    }, 'technical-spec-review.stack-retry')
  })

  // 状态页/设置页的数据接口走宿主 webServer 的同源路由。跨源 fetch 在桌面端会被拦，
  // 同源没有这个问题；第三方插件（如 dsh-free-search）同样用 ctx.inject(['webServer']) 注册 /api/。
  ctx.inject(['webServer'], (scope) => {
    scope.effect(() => scope.webServer.register({
      kind: 'prefix',
      path: UI_PREFIX,
      handler: (request, response) => handleUiRequest(request, response, state, {
        loadSettings, settingsToEnv, readCredential,
        CHAT_KEY_REF, RERANK_KEY_REF, RULES_TOKEN_REF,
        // 重启不阻塞响应：栈起得慢（首次 initdb 可达一分钟），
        // 请求方只关心"已受理"，进度由状态页轮询体现。
        restart: async () => {
          stopChild()
          state.mcpPort = ''
          void (async () => {
            await new Promise((r) => setTimeout(r, 600))
            await ensureStack()
          })()
          return true
        },
      }),
    }), 'technical-spec-review.ui-routes')
  })

  ctx.effect(() => stopChild, 'technical-spec-review.local-stack')
  process.once('exit', stopChild)

  ctx.inject(['tools'], (scope) => {
    scope.tools.register({
      name: 'technical_spec_review_stack_status',
      description: '检查技术规范书审查本地后端栈（PostgreSQL + 规则服务 + MCP）是否就绪；未就绪时尝试拉起。',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      execute: async () => ({
        ready: await ensureStack(),
        dataDir: state.dataDir,
        mcp: state.mcpPort ? `http://127.0.0.1:${state.mcpPort}/mcp` : '',
        ui: UI_PREFIX,
      }),
    })
  })
}
