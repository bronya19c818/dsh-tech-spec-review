/**
 * 状态页/设置页的数据接口（Host 侧同源路由）。
 *
 * 挂在宿主 webServer 的 /api/technical-spec-review/ 前缀下，与 GUI 同源，
 * 因此不受跨源限制。只读状态 + 非敏感设置；密钥仍由 DSH 凭据库存放，
 * 这里只报告"是否已配置"。
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { request as httpRequest } from 'node:http'
import { join } from 'node:path'

export const UI_PREFIX = '/api/technical-spec-review/'

const REVIEW_ID_RE = /^[A-Za-z0-9_-]{1,64}$/
const REVIEW_LIMIT = 30

const SETTINGS_INT_RANGES = {}
const SETTINGS_FLOAT_RANGES = { rerankThreshold: [0, 1] }
const SETTINGS_STRINGS = new Set([
  'chatBaseUrl', 'chatModel', 'rerankBaseUrl', 'rerankModel', 'rulesBaseUrl', 'pythonPath',
])
// 嵌入模型与向量索引在规则服务端，客户端不保存这两类配置。
const SETTINGS_ENV = {
  chatBaseUrl: 'TSR_MODEL_BASE_URL',
  chatModel: 'TSR_MODEL',
  rerankBaseUrl: 'TSR_RERANK_BASE_URL',
  rerankModel: 'TSR_RERANK_MODEL',
  rerankThreshold: 'TSR_RERANK_THRESHOLD',
  rulesBaseUrl: 'TSR_REGISTRY_URL',
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return undefined
  }
}

/** 校验一份设置补丁；非法字段抛错，绝不静默丢弃。 */
export function validateSettings(patch) {
  if (patch === null || typeof patch !== 'object' || Array.isArray(patch)) {
    throw new Error('settings must be an object')
  }
  const out = {}
  for (const [key, value] of Object.entries(patch)) {
    if (SETTINGS_STRINGS.has(key)) {
      if (typeof value !== 'string') throw new Error(`${key} must be a string`)
      out[key] = value.trim()
    } else if (SETTINGS_INT_RANGES[key] !== undefined) {
      const [low, high] = SETTINGS_INT_RANGES[key]
      const n = Number(value)
      if (!Number.isInteger(n)) throw new Error(`${key} must be an integer`)
      if (n < low || n > high) throw new Error(`${key} out of range [${low}, ${high}]`)
      out[key] = n
    } else if (SETTINGS_FLOAT_RANGES[key] !== undefined) {
      const [low, high] = SETTINGS_FLOAT_RANGES[key]
      const n = Number(value)
      if (!Number.isFinite(n)) throw new Error(`${key} must be a number`)
      if (n < low || n > high) throw new Error(`${key} out of range [${low}, ${high}]`)
      out[key] = n
    } else {
      throw new Error(`unknown setting: ${key}`)
    }
  }
  return out
}

/** 界面回显"当前生效值"：环境变量（含 keys.env）打底，设置文件覆盖。 */
function currentValues(stored) {
  const out = {}
  for (const [key, name] of Object.entries(SETTINGS_ENV)) {
    const raw = process.env[name]
    if (raw === undefined || raw === '') continue
    if (SETTINGS_INT_RANGES[key] !== undefined) {
      const n = Number(raw)
      if (Number.isInteger(n)) out[key] = n
    } else if (SETTINGS_FLOAT_RANGES[key] !== undefined) {
      const n = Number(raw)
      if (Number.isFinite(n)) out[key] = n
    } else {
      out[key] = raw
    }
  }
  for (const [key, value] of Object.entries(stored)) {
    if (SETTINGS_ENV[key] !== undefined || SETTINGS_STRINGS.has(key)) out[key] = value
  }
  return out
}

function summarizeReview(runsDir, name) {
  const dir = join(runsDir, name)
  const state = readJson(join(dir, 'state.json'))
  if (state === undefined || state === null) return undefined
  const tasks = state.tasks || {}
  const ids = Object.keys(tasks).filter((k) => k !== 'INIT' && k !== 'SUMMARY')
  const count = (status) => ids.filter((k) => (tasks[k] || {}).status === status).length
  const summary = readJson(join(dir, 'summary.json'))
  const groups = (summary && summary.groups) || []
  const issues = groups.length > 0
    ? groups.reduce((n, g) => n + ((g.issue_ids || []).length), 0)
    : undefined
  return {
    id: state.review_id || name,
    sourceName: state.source_name || '',
    objectScope: state.object_scope || state.requested_object || '',
    status: state.status || 'UNKNOWN',
    error: state.error || null,
    createdAt: state.created_at || null,
    updatedAt: state.updated_at || null,
    tasks: {
      total: ids.length,
      completed: count('COMPLETED'),
      running: count('RUNNING'),
      failed: count('FAILED'),
      pending: count('PENDING'),
    },
    issues,
    hasReport: existsSync(join(dir, 'report.docx')),
  }
}

function overview(state) {
  const runsDir = join(state.dataDir, 'runs')
  const reviews = []
  if (existsSync(runsDir)) {
    for (const entry of readdirSync(runsDir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue
      const row = summarizeReview(runsDir, entry.name)
      if (row !== undefined) reviews.push(row)
    }
  }
  reviews.sort((a, b) => String(b.updatedAt || b.createdAt || '').localeCompare(String(a.updatedAt || a.createdAt || '')))
  const limited = reviews.slice(0, REVIEW_LIMIT)
  const stack = readJson(join(state.dataDir, 'stack.json')) || {}
  const active = limited.find((r) => r.status === 'RUNNING') || limited[0]
  return {
    dataDir: state.dataDir,
    stack: {
      up: Boolean(stack.mcp_url),
      mcpUrl: stack.mcp_url || '',
      registryUrl: stack.registry_url || '',
      mcpPort: state.mcpPort || '',
    },
    reviews: limited,
    activeId: active ? active.id : '',
    checkedAt: new Date().toISOString(),
  }
}

function reviewDetail(state, id) {
  if (!REVIEW_ID_RE.test(id || '')) throw new Error('invalid review id')
  const dir = join(state.dataDir, 'runs', id)
  const st = readJson(join(dir, 'state.json'))
  if (st === undefined || st === null) throw new Error('review not found')
  const document = readJson(join(dir, 'document.json')) || {}
  const summary = readJson(join(dir, 'summary.json')) || {}
  const tasks = st.tasks || {}
  const chapters = (document.chapters || []).map((ch) => {
    const task = tasks['B05-' + ch.id] || {}
    const result = readJson(join(dir, `B05-${ch.id}.json`)) || {}
    const findings = result.findings
    return {
      id: ch.id,
      part: ch.part || '全文',
      number: ch.number || '',
      title: ch.title || '',
      status: task.status || 'PENDING',
      error: task.error || null,
      findings: Array.isArray(findings) ? findings.length : undefined,
      route: result.route,
      queries: Array.isArray(result.queries) ? result.queries.length : undefined,
    }
  })
  const basics = ['B01', 'B02', 'B03', 'B04'].map((key) => {
    const task = tasks[key] || {}
    return {
      id: key,
      status: task.status || 'PENDING',
      error: task.error || null,
      progress: task.progress || [0, 1],
    }
  })
  return {
    summary: summarizeReview(join(state.dataDir, 'runs'), id),
    sourceName: st.source_name || '',
    objectScope: st.object_scope || '',
    documentRole: st.document_role || '',
    limitations: document.limitations || [],
    basics,
    chapters,
    groups: summary.groups || [],
    summaryText: summary.summary || '',
  }
}

/**
 * 直连本地 HTTP 取 JSON。
 *
 * 不用全局 fetch：应用里配了系统代理，fetch 会把 127.0.0.1 的请求也交给代理，
 * 结果就是规则服务明明在跑却探测失败。node:http 不走代理。
 */
function getJsonFrom(url, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const target = new URL(url)
    if (target.protocol !== 'http:') {
      reject(new Error(`unsupported-protocol-${target.protocol}`))
      return
    }
    const req = httpRequest(
      {
        hostname: target.hostname,
        port: target.port || 80,
        path: `${target.pathname}${target.search}`,
        method: 'GET',
        timeout: timeoutMs,
        headers: { accept: 'application/json' },
      },
      (res) => {
        const chunks = []
        res.on('data', (chunk) => chunks.push(chunk))
        res.on('end', () => {
          if (res.statusCode !== 200) {
            reject(new Error(`http-${res.statusCode}`))
            return
          }
          try {
            resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')))
          } catch {
            reject(new Error('invalid-json'))
          }
        })
      },
    )
    req.on('timeout', () => req.destroy(new Error('timeout')))
    req.on('error', (error) => reject(error))
    req.end()
  })
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  })
}

/**
 * 处理 /api/technical-spec-review/* 的请求。
 * @param request - 宿主转发的 HTTP 请求。
 * @param state - 插件运行状态（dataDir、mcpPort）。
 * @param deps - 设置读写、凭据读取与重启能力。
 * @returns 同源 JSON 响应。
 */
export async function handleUiRequest(request, state, deps) {
  const url = new URL(request.url)
  const path = url.pathname.slice(UI_PREFIX.length - 1)
  try {
    if (path === '/overview' && request.method === 'GET') return json(overview(state))

    if (path === '/review' && request.method === 'GET') {
      return json(reviewDetail(state, url.searchParams.get('id') || ''))
    }

    if (path === '/settings' && request.method === 'GET') {
      const stored = deps.loadSettings(state.dataDir)
      const chat = await deps.readCredential(deps.CHAT_KEY_REF)
      const rerank = await deps.readCredential(deps.RERANK_KEY_REF)
      const rules = await deps.readCredential(deps.RULES_TOKEN_REF)
      return json({
        values: currentValues(stored),
        stored,
        secrets: { chat: Boolean(chat), rerank: Boolean(rerank), rules: Boolean(rules) },
        refs: {
          chat: deps.CHAT_KEY_REF,
          rerank: deps.RERANK_KEY_REF,
          rules: deps.RULES_TOKEN_REF,
        },
        stack: { up: Boolean(state.mcpPort) },
      })
    }

    if (path === '/server-info' && request.method === 'GET') {
      // 规则服务的只读信息：嵌入模型、维度与索引规模都由服务端决定。
      const stored = deps.loadSettings(state.dataDir)
      const base = (stored.rulesBaseUrl || process.env.TSR_REGISTRY_URL || '').replace(/\/+$/, '')
      if (!base) return json({ reachable: false, reason: 'not-configured' })
      try {
        return json({ reachable: true, ...(await getJsonFrom(base + '/v1/service-info')) })
      } catch (error) {
        return json({ reachable: false, reason: String((error && error.message) || error) })
      }
    }

    if (path === '/settings' && request.method === 'POST') {
      const body = await request.json()
      const patch = validateSettings((body && body.values) || body || {})
      const merged = { ...deps.loadSettings(state.dataDir), ...patch }
      mkdirSync(state.dataDir, { recursive: true })
      writeFileSync(join(state.dataDir, 'settings.json'), JSON.stringify(merged, null, 2), 'utf8')
      return json({ ok: true, values: currentValues(merged) })
    }

    if (path === '/restart' && request.method === 'POST') {
      const ok = await deps.restart()
      return json({ ok, stack: { up: Boolean(state.mcpPort), mcpPort: state.mcpPort } })
    }

    return json({ error: 'not found' }, 404)
  } catch (error) {
    return json({ error: String((error && error.message) || error) }, 400)
  }
}
