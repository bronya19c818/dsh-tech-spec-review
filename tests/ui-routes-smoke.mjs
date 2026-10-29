// Host 侧 UI 路由逻辑冒烟：直接调用 handleUiRequest，验证状态聚合与设置读写。
// 不经过 HTTP，因此不依赖 GUI 鉴权。
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
const { handleUiRequest, UI_PREFIX, validateSettings } = await import(
  pathToFileURL(join(ROOT, 'src', 'ui-routes.js')).href
)

const dataDir = mkdtempSync(join(tmpdir(), 'tsr-ui-'))
const runsDir = join(dataDir, 'runs')
const reviewDir = join(runsDir, 'review-a')
mkdirSync(reviewDir, { recursive: true })

writeFileSync(join(reviewDir, 'state.json'), JSON.stringify({
  review_id: 'review-a',
  status: 'COMPLETED',
  source_name: '净水器技术规范书.docx',
  object_scope: '净水器',
  document_role: '采购技术规范书',
  created_at: '2026-10-29T01:00:00Z',
  updated_at: '2026-10-29T01:12:00Z',
  tasks: {
    INIT: { status: 'COMPLETED' },
    B01: { status: 'COMPLETED', progress: [1, 1] },
    B02: { status: 'COMPLETED', progress: [108, 108] },
    B03: { status: 'COMPLETED', progress: [1, 1] },
    B04: { status: 'COMPLETED', progress: [1, 1] },
    'B05-CH001': { status: 'COMPLETED' },
    'B05-CH002': { status: 'FAILED', error: 'SEMANTIC_SEARCH_REQUIRED' },
    SUMMARY: { status: 'COMPLETED' },
  },
}))
writeFileSync(join(reviewDir, 'document.json'), JSON.stringify({
  chapters: [
    { id: 'CH001', part: '通用部分', number: '1', title: '总则' },
    { id: 'CH002', part: '专用部分', number: '2', title: '技术参数' },
  ],
  limitations: ['TEXT_EXTRACTOR_ONLY'],
}))
writeFileSync(join(reviewDir, 'summary.json'), JSON.stringify({
  summary: '共发现 3 项问题。',
  groups: [
    { title: '版面与格式', issue_ids: ['B01-F0001'] },
    { title: '章节结构', issue_ids: ['B03-F0001', 'B03-F0002'] },
  ],
}))
writeFileSync(join(reviewDir, 'B05-CH001.json'), JSON.stringify({ findings: [{}, {}], route: 'STRUCTURED', queries: [] }))
writeFileSync(join(reviewDir, 'B05-CH002.json'), JSON.stringify({ findings: [], route: 'SEMANTIC', queries: [{}, {}] }))

const state = { dataDir, mcpPort: '8775' }
const settingsPath = join(dataDir, 'settings.json')
const deps = {
  CHAT_KEY_REF: 'TSR_CHAT_API_KEY',
  RERANK_KEY_REF: 'TSR_RERANK_API_KEY',
  RULES_TOKEN_REF: 'TSR_RULES_TOKEN',
  loadSettings: () => (existsSync(settingsPath) ? JSON.parse(readFileSync(settingsPath, 'utf8')) : {}),
  settingsToEnv: () => ({}),
  readCredential: async (ref) => (ref === 'TSR_CHAT_API_KEY' ? 'x' : undefined),
  restart: async () => true,
}

// 宿主把 Node 原生 req/res 交给 handler，这里按同样语义造一对假对象。
function makeReq({ path, method = 'GET', body, headers = {} }) {
  const payload = body === undefined ? '' : JSON.stringify(body)
  const req = {
    url: `${UI_PREFIX}${path.startsWith('/') ? path : '/' + path}`,
    method,
    headers: { host: '127.0.0.1:57511', ...headers },
  }
  req[Symbol.asyncIterator] = async function* () {
    if (payload) yield Buffer.from(payload)
  }
  return req
}

function makeRes() {
  const res = { statusCode: 0, headers: {}, text: '' }
  res.writeHead = (status, headers) => {
    res.statusCode = status
    Object.assign(res.headers, headers || {})
  }
  res.end = (chunk) => {
    if (chunk !== undefined) {
      res.text += typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')
    }
  }
  return res
}

async function call(path, method = 'GET', body, headers) {
  const res = makeRes()
  await handleUiRequest(makeReq({ path, method, body, headers }), res, state, deps)
  let parsed
  try {
    parsed = JSON.parse(res.text)
  } catch {
    parsed = undefined
  }
  return { status: res.statusCode, body: parsed, text: res.text }
}

const get = (path) => call(path).then((r) => ({ status: r.status, json: async () => r.body }))
const post = (path, body, headers) =>
  call(path, 'POST', body, headers).then((r) => ({ status: r.status, json: async () => r.body }))

// overview
const ov = await (await get('overview')).json()
assert.equal(ov.reviews.length, 1, '应列出 1 条任务')
assert.equal(ov.activeId, 'review-a')
assert.equal(ov.reviews[0].tasks.total, 6, 'B01-B04 + 2 章')
assert.equal(ov.reviews[0].tasks.completed, 5)
assert.equal(ov.reviews[0].tasks.failed, 1)
assert.equal(ov.reviews[0].issues, 3, '问题数来自 summary groups')
assert.equal(ov.stack.mcpPort, '8775')

// review detail
const detail = await (await get('review?id=review-a')).json()
assert.equal(detail.chapters.length, 2)
assert.equal(detail.chapters[0].findings, 2, '章节问题数来自 B05 结果')
assert.equal(detail.chapters[0].route, 'STRUCTURED')
assert.equal(detail.chapters[1].status, 'FAILED')
assert.equal(detail.chapters[1].queries, 2)
assert.equal(detail.groups.length, 2)
assert.deepEqual(detail.basics.map((b) => b.id), ['B01', 'B02', 'B03', 'B04'])

// 非法 id 必须被拒
assert.equal((await get('review?id=../etc')).status, 400, '非法 review id 应 400')

// settings 读：只暴露对话与重排两把密钥，嵌入在服务端
const st = await (await get('settings')).json()
assert.equal(st.secrets.chat, true)
assert.equal(st.secrets.rerank, false)
assert.equal(st.secrets.rules, false)
assert.equal(st.secrets.embed, undefined, '客户端不再持有嵌入密钥')
assert.equal(st.refs.chat, 'TSR_CHAT_API_KEY')
assert.equal(st.refs.rerank, 'TSR_RERANK_API_KEY')
assert.equal(st.refs.rules, 'TSR_RULES_TOKEN')

// 规则服务未配置时 server-info 应明确返回 not-configured
const info = await (await get('server-info')).json()
assert.equal(info.reachable, false)
assert.equal(info.reason, 'not-configured')

// settings 写 + 回读
const saved = await (await post('settings', {
  values: { chatModel: 'mimo-v2.6-flash', rulesBaseUrl: 'http://127.0.0.1:8010' },
})).json()
assert.equal(saved.ok, true)
assert.equal(saved.values.chatModel, 'mimo-v2.6-flash')
const onDisk = JSON.parse(readFileSync(settingsPath, 'utf8'))
assert.equal(onDisk.rulesBaseUrl, 'http://127.0.0.1:8010')

// 非法值被拒：未知键、越界阈值
assert.equal((await post('settings', { values: { nope: 1 } })).status, 400)
assert.equal((await post('settings', { values: { rerankThreshold: 5 } })).status, 400)
assert.throws(() => validateSettings({ nope: 1 }), /unknown setting/)

// server-info 只回答可达性：嵌入模型与索引规模属于服务端，客户端不读取
const probed = await (await get('server-info')).json()
assert.equal(typeof probed.reachable, 'boolean')
assert.equal(probed.embedding_model, undefined, '客户端不应读取服务端嵌入模型')
assert.equal(probed.chunks, undefined, '客户端不应读取服务端索引规模')
if (!probed.reachable) {
  assert.ok(typeof probed.reason === 'string' && probed.reason.length > 0, '不可达要给原因')
}

// restart
const restart = await (await post('restart', {})).json()
assert.equal(restart.ok, true)

// 未知路径 404
assert.equal((await get('nope')).status, 404)

console.log('UI ROUTES SMOKE OK')
