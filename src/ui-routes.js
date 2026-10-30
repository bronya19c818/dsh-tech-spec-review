/**
 * 状态页/设置页的数据接口（Host 侧同源路由）。
 *
 * 宿主 webServer 的 handler 契约是 Node 原生 (req, res)：自己写状态码和响应体，
 * 返回 Response 没有任何作用。这里统一用 writeJson(res, ...)。
 * 挂在 /api/technical-spec-review 前缀下，与 GUI 同源，因此不受跨源限制。
 * 只读状态 + 非敏感设置；密钥仍由 DSH 凭据库存放，这里只报告"是否已配置"。
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { request as httpRequest } from 'node:http'
import { join } from 'node:path'

import { TASK_LABELS } from './task-labels.js'

// 前缀不带尾斜杠：宿主按 pathname === prefix 或 startsWith(prefix + '/') 匹配。
export const UI_PREFIX = '/api/technical-spec-review'

const REVIEW_ID_RE = /^[A-Za-z0-9_-]{1,64}$/
const REVIEW_LIMIT = 30
const MAX_BODY_BYTES = 1 << 20

const SETTINGS_INT_RANGES = {}
const SETTINGS_FLOAT_RANGES = { rerankThreshold: [0, 1], gateThreshold: [0, 1] }
const SETTINGS_STRINGS = new Set([
  'chatBaseUrl', 'chatModel', 'rerankBaseUrl', 'rerankModel', 'rulesBaseUrl',
  'gateBaseUrl', 'pythonPath',
])
// 嵌入模型与向量索引在规则服务端，客户端不保存这两类配置。
const SETTINGS_ENV = {
  chatBaseUrl: 'TSR_MODEL_BASE_URL',
  chatModel: 'TSR_MODEL',
  rerankBaseUrl: 'TSR_RERANK_BASE_URL',
  rerankModel: 'TSR_RERANK_MODEL',
  rerankThreshold: 'TSR_RERANK_THRESHOLD',
  rulesBaseUrl: 'TSR_REGISTRY_URL',
  // B02 前置筛句：地址与阈值走环境变量，密钥仍由凭据库提供。
  gateBaseUrl: 'TSR_GATE_BASE_URL',
  gateThreshold: 'TSR_GATE_THRESHOLD',
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return undefined
  }
}

export function validateSettings(patch) {
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) {
    throw new Error('settings must be an object')
  }
  const accepted = {}
  for (const [key, raw] of Object.entries(patch)) {
    if (key in SETTINGS_INT_RANGES) {
      const [min, max] = SETTINGS_INT_RANGES[key]
      const value = Number(raw)
      if (!Number.isInteger(value) || value < min || value > max) {
        throw new Error(`${key} must be an integer in [${min}, ${max}]`)
      }
      accepted[key] = value
      continue
    }
    if (key in SETTINGS_FLOAT_RANGES) {
      const [min, max] = SETTINGS_FLOAT_RANGES[key]
      const value = Number(raw)
      if (!Number.isFinite(value) || value < min || value > max) {
        throw new Error(`${key} must be a number in [${min}, ${max}]`)
      }
      accepted[key] = value
      continue
    }
    if (SETTINGS_STRINGS.has(key)) {
      if (typeof raw !== 'string' || raw.length > 500) {
        throw new Error(`${key} must be a string of at most 500 characters`)
      }
      accepted[key] = raw.trim()
      continue
    }
    throw new Error(`unknown setting: ${key}`)
  }
  return accepted
}

function currentValues(stored) {
  const values = {}
  for (const key of Object.keys(SETTINGS_ENV)) {
    const value = stored?.[key]
    if (value !== undefined && value !== '') values[key] = value
  }
  if (stored?.pythonPath) values.pythonPath = stored.pythonPath
  return values
}

function summarizeReview(runsDir, name) {
  const dir = join(runsDir, name)
  const state = readJson(join(dir, 'state.json'))
  if (!state) return undefined
  const document = readJson(join(dir, 'document.json')) || {}
  const summary = readJson(join(dir, 'summary.json')) || {}
  const tasks = state.tasks || {}
  const chapterIds = (document.chapters || []).map((chapter) => chapter.id)
  let total = 0
  let completed = 0
  let failed = 0
  for (const [taskId, task] of Object.entries(tasks)) {
    if (taskId === 'INIT' || taskId === 'SUMMARY') continue
    if (taskId.startsWith('B05-')) continue
    total += 1
    if (task.status === 'COMPLETED') completed += 1
    if (task.status === 'FAILED') failed += 1
  }
  for (const chapterId of chapterIds) {
    total += 1
    const task = tasks[`B05-${chapterId}`]
    if (task?.status === 'COMPLETED') completed += 1
    if (task?.status === 'FAILED') failed += 1
  }
  const groups = summary.groups || []
  const issues = groups.reduce((sum, group) => sum + (group.issue_ids || []).length, 0)
  return {
    id: state.review_id,
    name: state.source_name || name,
    status: state.status,
    objectScope: state.object_scope || '',
    documentRole: state.document_role || '',
    createdAt: state.created_at,
    updatedAt: state.updated_at,
    tasks: { total, completed, failed },
    issues,
    error: state.error || '',
  }
}

function overview(state) {
  const runsDir = join(state.dataDir, 'runs')
  let reviews = []
  if (existsSync(runsDir)) {
    const names = readdirSync(runsDir, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
    for (const name of names) {
      const summary = summarizeReview(runsDir, name)
      if (summary) reviews.push(summary)
    }
    reviews.sort((a, b) => String(b.updatedAt || '').localeCompare(String(a.updatedAt || '')))
  }
  const limited = reviews.slice(0, REVIEW_LIMIT)
  return {
    reviews: limited,
    activeId: limited[0]?.id || '',
    stack: { up: Boolean(state.mcpPort), mcpPort: state.mcpPort },
  }
}

function reviewDetail(state, id) {
  if (!REVIEW_ID_RE.test(id)) throw new Error('invalid review id')
  const dir = join(state.dataDir, 'runs', id)
  const stateJson = readJson(join(dir, 'state.json'))
  if (!stateJson) throw new Error('review not found')
  const document = readJson(join(dir, 'document.json')) || {}
  const summary = readJson(join(dir, 'summary.json')) || {}
  const tasks = stateJson.tasks || {}
  const chapters = (document.chapters || []).map((chapter) => {
    const task = tasks[`B05-${chapter.id}`] || {}
    const result = readJson(join(dir, `B05-${chapter.id}.json`)) || {}
    return {
      id: chapter.id,
      part: chapter.part,
      number: chapter.number,
      title: chapter.title,
      status: task.status || 'PENDING',
      error: task.error || '',
      route: result.route || '',
      findings: (result.findings || []).length,
      queries: (result.queries || []).length,
    }
  })
  const basics = ['B01', 'B02', 'B03', 'B04'].map((taskId) => {
    const task = tasks[taskId] || {}
    const result = readJson(join(dir, `${taskId}.json`)) || {}
    return {
      id: taskId,
      label: TASK_LABELS[taskId] || taskId,
      status: task.status || 'PENDING',
      error: task.error || '',
      findings: (result.findings || []).length,
      stats: result.stats || {},
    }
  })
  return {
    id,
    status: stateJson.status,
    error: stateJson.error || '',
    chapters,
    basics,
    groups: summary.groups || [],
    summaryText: summary.summary || '',
    limitations: document.limitations || [],
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

function writeJson(res, status, body) {
  const payload = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'referrer-policy': 'no-referrer',
  })
  res.end(payload)
}

async function readJsonBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY_BYTES) return undefined
    chunks.push(chunk)
  }
  if (chunks.length === 0) return {}
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    return undefined
  }
}

/** 只接受同源请求：拒绝跨站发起，Origin 存在时必须与 Host 一致。 */
function isSameOrigin(req) {
  if (req.headers['sec-fetch-site'] === 'cross-site') return false
  const host = req.headers.host
  if (typeof host !== 'string' || host.length === 0) return false
  const origin = req.headers.origin
  if (origin !== undefined) {
    try {
      if (new URL(origin).host !== host) return false
    } catch {
      return false
    }
  }
  return true
}

/**
 * 处理 /api/technical-spec-review/* 的请求。
 * @param req - Node IncomingMessage（宿主原样转交）。
 * @param res - Node ServerResponse，由本函数负责写响应。
 * @param state - 插件运行状态（dataDir、mcpPort）。
 * @param deps - 宿主能力（设置读写、凭据查询、重启）。
 */
export async function handleUiRequest(req, res, state, deps) {
  const url = new URL(req.url ?? '/', 'http://localhost')
  const path = url.pathname.slice(UI_PREFIX.length)
  const method = req.method ?? 'GET'
  try {
    if (path === '/overview' && method === 'GET') {
      writeJson(res, 200, overview(state))
      return
    }

    if (path === '/review' && method === 'GET') {
      writeJson(res, 200, reviewDetail(state, url.searchParams.get('id') || ''))
      return
    }

    if (path === '/settings' && method === 'GET') {
      const stored = deps.loadSettings(state.dataDir)
      const chat = await deps.readCredential(deps.CHAT_KEY_REF)
      const rerank = await deps.readCredential(deps.RERANK_KEY_REF)
      const rules = await deps.readCredential(deps.RULES_TOKEN_REF)
      const gate = await deps.readCredential(deps.GATE_KEY_REF)
      writeJson(res, 200, {
        values: currentValues(stored),
        stored,
        secrets: {
          chat: Boolean(chat), rerank: Boolean(rerank),
          rules: Boolean(rules), gate: Boolean(gate),
        },
        refs: {
          chat: deps.CHAT_KEY_REF,
          rerank: deps.RERANK_KEY_REF,
          rules: deps.RULES_TOKEN_REF,
          gate: deps.GATE_KEY_REF,
        },
        stack: { up: Boolean(state.mcpPort), mcpPort: state.mcpPort },
      })
      return
    }

    if (path === '/server-info' && method === 'GET') {
      // 只回答"规则服务能不能连上"。嵌入模型、索引规模是服务端自身的事，
      // 客户端不读取也不展示。
      const stored = deps.loadSettings(state.dataDir)
      const base = (stored.rulesBaseUrl || process.env.TSR_REGISTRY_URL || '').replace(/\/+$/, '')
      if (!base) {
        writeJson(res, 200, { reachable: false, reason: 'not-configured' })
        return
      }
      try {
        await getJsonFrom(base + '/health')
        writeJson(res, 200, { reachable: true })
      } catch (error) {
        writeJson(res, 200, { reachable: false, reason: String((error && error.message) || error) })
      }
      return
    }

    if (path === '/settings' && method === 'POST') {
      if (!isSameOrigin(req)) {
        writeJson(res, 403, { error: 'cross-site request rejected' })
        return
      }
      const body = await readJsonBody(req)
      if (body === undefined) {
        writeJson(res, 400, { error: 'malformed JSON body' })
        return
      }
      const patch = validateSettings((body && body.values) || body || {})
      const merged = { ...deps.loadSettings(state.dataDir), ...patch }
      mkdirSync(state.dataDir, { recursive: true })
      writeFileSync(join(state.dataDir, 'settings.json'), JSON.stringify(merged, null, 2), 'utf8')
      writeJson(res, 200, { ok: true, values: currentValues(merged) })
      return
    }

    if (path === '/restart' && method === 'POST') {
      if (!isSameOrigin(req)) {
        writeJson(res, 403, { error: 'cross-site request rejected' })
        return
      }
      // 请求体可以不传；有就消费掉，避免连接悬挂。
      await readJsonBody(req)
      const ok = await deps.restart()
      writeJson(res, 200, { ok, stack: { up: Boolean(state.mcpPort), mcpPort: state.mcpPort } })
      return
    }

    writeJson(res, 404, { error: 'not found' })
  } catch (error) {
    writeJson(res, 400, { error: String((error && error.message) || error) })
  }
}
