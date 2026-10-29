/**
 * 技术规范书审查 —— Client half（浏览器动态插件）。
 *
 * 两个挂载点：
 *   settings.section      设置页（参数 + 密钥）
 *   conversation.view     会话视图里的“审查状态”标签页
 *
 * 状态页刻意不占侧边栏：没有任务时它只是一个空壳，常驻入口反而干扰导航。
 *
 * 只消费宿主主题变量（--dsw-alias-*），不自带色板与字体；数据来自插件自己拉起的
 * 本地 UI 接口（127.0.0.1:8790，带 CORS），密钥写入走 ctx.remote.credentials。
 */
window.__ModuleLoader__.load({
  id: 'dsh-technical-spec-review',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useCallback, useEffect, useMemo, useRef, useState } = React

    const T = {
      bg: 'var(--dsw-alias-bg-base, #0f1115)',
      layer1: 'var(--dsw-alias-bg-layer-1, #161a20)',
      layer2: 'var(--dsw-alias-bg-layer-2, #1c2028)',
      border1: 'var(--dsw-alias-border-l1, #2a2f39)',
      border2: 'var(--dsw-alias-border-l2, #3a4150)',
      text: 'var(--dsw-alias-label-primary, #e6e8ec)',
      dim: 'var(--dsw-alias-label-secondary, #9aa2af)',
      brand: 'var(--dsw-alias-brand-primary, #4c8dff)',
      success: 'var(--dsw-alias-state-success-primary, #35b37e)',
      warn: 'var(--dsw-alias-state-warn-primary, #d99a2b)',
      error: 'var(--dsw-alias-state-error-primary, #d95c5c)',
      idle: 'var(--dsw-alias-state-idle-primary, #6b7280)',
    }

    const STATUS = {
      COMPLETED: { color: T.success, label: '已完成' },
      RUNNING: { color: T.brand, label: '进行中' },
      FAILED: { color: T.error, label: '失败' },
      PENDING: { color: T.idle, label: '未开始' },
      UNKNOWN: { color: T.idle, label: '未知' },
    }

    const STYLE_ID = 'tsr-client-style'
    const CSS = `
.tsr-root { background: ${T.bg}; color: ${T.text}; height: 100%; overflow: auto; }
.tsr-wrap { max-width: 1180px; margin: 0 auto; padding: 28px 32px 56px; }
.tsr-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 4px; }
.tsr-title { font-size: 17px; font-weight: 600; letter-spacing: .01em; }
.tsr-sub { color: ${T.dim}; font-size: 12px; }
.tsr-actions { margin-left: auto; display: flex; gap: 8px; align-items: center; }
.tsr-btn { font: inherit; font-size: 12px; color: ${T.text}; background: ${T.layer1};
  border: 1px solid ${T.border1}; border-radius: 6px; padding: 5px 10px; cursor: pointer; }
.tsr-btn:hover { border-color: ${T.border2}; }
.tsr-btn:disabled { opacity: .5; cursor: default; }
.tsr-btn-primary { border-color: ${T.brand}; color: ${T.brand}; }
.tsr-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.tsr-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 10px; margin: 18px 0 22px; }
.tsr-tile { border: 1px solid ${T.border1}; background: ${T.layer1}; border-radius: 8px; padding: 12px 14px; }
.tsr-tile-label { color: ${T.dim}; font-size: 11px; letter-spacing: .04em; }
.tsr-tile-value { font-size: 22px; font-weight: 600; margin-top: 6px; font-variant-numeric: tabular-nums; }
.tsr-tile-note { color: ${T.dim}; font-size: 11px; margin-top: 2px; }
.tsr-cols { display: grid; grid-template-columns: 264px minmax(0, 1fr); gap: 18px; align-items: start; }
@media (max-width: 900px) { .tsr-cols { grid-template-columns: minmax(0, 1fr); } }
.tsr-list { border: 1px solid ${T.border1}; border-radius: 8px; overflow: hidden; background: ${T.layer1}; }
.tsr-list-head { padding: 9px 12px; border-bottom: 1px solid ${T.border1}; color: ${T.dim};
  font-size: 11px; letter-spacing: .04em; display: flex; justify-content: space-between; }
.tsr-item { display: block; width: 100%; text-align: left; background: none; border: 0; border-bottom: 1px solid ${T.border1};
  padding: 10px 12px; cursor: pointer; color: ${T.text}; font: inherit; }
.tsr-item:last-child { border-bottom: 0; }
.tsr-item:hover { background: ${T.layer2}; }
.tsr-item[data-active="true"] { background: ${T.layer2}; box-shadow: inset 2px 0 0 ${T.brand}; }
.tsr-item-name { font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tsr-item-meta { display: flex; gap: 8px; align-items: center; margin-top: 5px; color: ${T.dim}; font-size: 11px; }
.tsr-card { border: 1px solid ${T.border1}; border-radius: 8px; background: ${T.layer1}; }
.tsr-card-h { padding: 11px 14px; border-bottom: 1px solid ${T.border1}; display: flex; align-items: baseline; gap: 10px; }
.tsr-card-t { font-size: 13px; font-weight: 600; }
.tsr-card-b { padding: 14px; }
.tsr-lane { display: grid; grid-template-columns: 84px minmax(0, 1fr) 88px; gap: 12px; align-items: center; padding: 7px 0; }
.tsr-lane-id { font-size: 11.5px; color: ${T.dim}; font-variant-numeric: tabular-nums; }
.tsr-track { height: 6px; border-radius: 3px; background: ${T.layer2}; overflow: hidden; position: relative; }
.tsr-fill { height: 100%; border-radius: 3px; transition: width .4s ease; }
.tsr-fill-running { position: relative; overflow: hidden; }
.tsr-fill-running::after { content: ""; position: absolute; inset: 0;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,.28), transparent);
  animation: tsr-sweep 1.6s linear infinite; }
@keyframes tsr-sweep { from { transform: translateX(-100%); } to { transform: translateX(100%); } }
.tsr-lane-val { font-size: 11px; color: ${T.dim}; text-align: right; font-variant-numeric: tabular-nums; }
.tsr-part { margin-top: 14px; }
.tsr-part-h { display: flex; align-items: baseline; gap: 8px; margin-bottom: 7px; color: ${T.dim}; font-size: 11px; letter-spacing: .04em; }
.tsr-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(122px, 1fr)); gap: 7px; }
.tsr-cell { border: 1px solid ${T.border1}; border-radius: 6px; padding: 8px 9px; min-height: 58px;
  background: ${T.layer1}; position: relative; overflow: hidden; }
.tsr-cell[data-s="COMPLETED"] { border-color: color-mix(in srgb, ${T.success} 42%, ${T.border1}); }
.tsr-cell[data-s="RUNNING"] { border-color: ${T.brand}; }
.tsr-cell[data-s="FAILED"] { border-color: color-mix(in srgb, ${T.error} 55%, ${T.border1}); }
.tsr-cell-num { font-size: 11px; color: ${T.dim}; font-variant-numeric: tabular-nums; }
.tsr-cell-t { font-size: 12px; margin-top: 3px; overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical; line-height: 1.32; }
.tsr-cell-f { position: absolute; right: 7px; top: 7px; font-size: 10.5px; padding: 0 5px; border-radius: 8px;
  background: ${T.layer2}; color: ${T.dim}; font-variant-numeric: tabular-nums; }
.tsr-cell-f[data-n="0"] { opacity: .55; }
.tsr-legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 14px; color: ${T.dim}; font-size: 11px; }
.tsr-legend span { display: inline-flex; gap: 5px; align-items: center; }
.tsr-note { margin-top: 12px; border: 1px solid ${T.border1}; border-left: 2px solid ${T.warn};
  border-radius: 6px; padding: 9px 12px; color: ${T.dim}; font-size: 12px; background: ${T.layer1}; }
.tsr-empty { border: 1px dashed ${T.border2}; border-radius: 8px; padding: 40px 24px; text-align: center; color: ${T.dim}; }
.tsr-empty strong { display: block; color: ${T.text}; font-size: 13px; margin-bottom: 6px; }
.tsr-field { display: grid; grid-template-columns: 190px minmax(0, 1fr); gap: 12px; align-items: center; padding: 7px 0; }
.tsr-field label { font-size: 12px; color: ${T.dim}; }
.tsr-input { font: inherit; font-size: 12.5px; color: ${T.text}; background: ${T.bg};
  border: 1px solid ${T.border1}; border-radius: 6px; padding: 6px 9px; width: 100%; }
.tsr-input:focus { outline: none; border-color: ${T.brand}; }
.tsr-hint { color: ${T.dim}; font-size: 11px; margin-top: 3px; }
.tsr-sect { margin-top: 22px; }
.tsr-sect-t { font-size: 12px; color: ${T.dim}; letter-spacing: .04em; padding-bottom: 7px;
  border-bottom: 1px solid ${T.border1}; margin-bottom: 6px; }
.tsr-badge { font-size: 11px; padding: 1px 7px; border-radius: 9px; border: 1px solid ${T.border1}; color: ${T.dim}; }
.tsr-badge[data-ok="true"] { color: ${T.success}; border-color: color-mix(in srgb, ${T.success} 45%, ${T.border1}); }
.tsr-saverow { display: flex; gap: 8px; align-items: center; margin-top: 18px; }
.tsr-msg { font-size: 12px; }
.tsr-kbd { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px;
  background: ${T.layer2}; border: 1px solid ${T.border1}; border-radius: 4px; padding: 1px 5px; }
`

    function useStyles() {
      useEffect(() => {
        if (document.getElementById(STYLE_ID)) return undefined
        const el = document.createElement('style')
        el.id = STYLE_ID
        el.textContent = CSS
        document.head.appendChild(el)
        return () => { el.remove() }
      }, [])
    }

    // 数据接口与 GUI 同源（宿主 webServer 的 /api/technical-spec-review/），不受跨源限制。
    const API = '/api/technical-spec-review'

    async function getJson(path) {
      const res = await fetch(API + path, { headers: { accept: 'application/json' }, cache: 'no-store' })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(body.detail || body.error || `HTTP ${res.status}`)
      return body
    }

    async function postJson(path, payload) {
      const res = await fetch(API + path, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload ?? {}),
      })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(body.detail || body.error || `HTTP ${res.status}`)
      return body
    }

    const fmtTime = (iso) => {
      if (!iso) return '—'
      const d = new Date(iso)
      if (Number.isNaN(d.getTime())) return '—'
      const p = (n) => String(n).padStart(2, '0')
      return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
    }

    const elapsed = (from, to) => {
      if (!from) return '—'
      const a = new Date(from).getTime()
      const b = to ? new Date(to).getTime() : Date.now()
      if (Number.isNaN(a) || Number.isNaN(b)) return '—'
      const s = Math.max(0, Math.round((b - a) / 1000))
      if (s < 60) return `${s}s`
      const m = Math.floor(s / 60)
      return m < 60 ? `${m}m${s % 60}s` : `${Math.floor(m / 60)}h${m % 60}m`
    }

    function Dot({ status }) {
      const s = STATUS[status] || STATUS.UNKNOWN
      return h('span', { className: 'tsr-dot', style: { background: s.color } })
    }

    function Lane({ id, label, status, done, total }) {
      const s = STATUS[status] || STATUS.UNKNOWN
      const pct = total > 0 ? Math.round((done / total) * 100) : status === 'COMPLETED' ? 100 : 0
      const width = status === 'COMPLETED' ? 100 : pct
      return h('div', { className: 'tsr-lane' },
        h('div', { className: 'tsr-lane-id' }, label || id),
        h('div', { className: 'tsr-track', role: 'progressbar', 'aria-valuenow': width, 'aria-valuemin': 0, 'aria-valuemax': 100 },
          h('div', {
            className: status === 'RUNNING' ? 'tsr-fill tsr-fill-running' : 'tsr-fill',
            style: { width: `${width}%`, background: s.color, opacity: status === 'PENDING' ? 0.35 : 1 },
          })),
        h('div', { className: 'tsr-lane-val' },
          status === 'RUNNING' && total > 0 ? `${done}/${total}` : s.label),
      )
    }

    function ChapterGrid({ chapters }) {
      const parts = useMemo(() => {
        const map = new Map()
        for (const ch of chapters) {
          const key = ch.part || '全文'
          if (!map.has(key)) map.set(key, [])
          map.get(key).push(ch)
        }
        return [...map.entries()]
      }, [chapters])

      if (chapters.length === 0) {
        return h('div', { className: 'tsr-note' }, '本次审查没有识别到内容章节，任务地图为空。')
      }
      return h('div', null,
        parts.map(([part, list]) => h('div', { className: 'tsr-part', key: part },
          h('div', { className: 'tsr-part-h' }, h('span', null, part), h('span', null, `${list.length} 章`)),
          h('div', { className: 'tsr-grid' },
            list.map((ch) => {
              const st = STATUS[ch.status] || STATUS.UNKNOWN
              const findings = ch.findings
              const tip = [
                `${ch.number} ${ch.title}`.trim(),
                `状态：${st.label}`,
                ch.route === 'STRUCTURED' ? '规则路径：确定规则' : ch.route === 'SEMANTIC' ? '规则路径：语义候选' : '',
                ch.queries != null ? `语义检索 ${ch.queries} 次` : '',
                findings != null ? `发现 ${findings} 项` : '',
                ch.error ? `错误：${ch.error}` : '',
              ].filter(Boolean).join('\n')
              return h('div', {
                key: ch.id, className: 'tsr-cell', 'data-s': ch.status, title: tip,
              },
                findings != null ? h('span', { className: 'tsr-cell-f', 'data-n': findings }, findings) : null,
                h('div', { className: 'tsr-cell-num' }, ch.number || '—'),
                h('div', { className: 'tsr-cell-t' }, ch.title || '（无标题）'),
              )
            }),
          ),
        )),
      )
    }

    function StatusBoard() {
      useStyles()
      const [overview, setOverview] = useState(null)
      const [detail, setDetail] = useState(null)
      const [error, setError] = useState('')
      const [busy, setBusy] = useState(false)
      const [activeId, setActiveId] = useState('')
      const activeRef = useRef('')

      const loadOverview = useCallback(async () => {
        try {
          const data = await getJson('/overview')
          setOverview(data)
          setError('')
          const next = activeRef.current || data.activeId || data.reviews?.[0]?.id || ''
          if (next) {
            activeRef.current = next
            setActiveId(next)
            setDetail(await getJson(`/review?id=${encodeURIComponent(next)}`))
          } else {
            setDetail(null)
          }
        } catch (e) {
          setError(String(e.message || e))
        }
      }, [])

      const selectReview = useCallback(async (id) => {
        activeRef.current = id
        setActiveId(id)
        try {
          setDetail(await getJson(`/review?id=${encodeURIComponent(id)}`))
          setError('')
        } catch (e) {
          setError(String(e.message || e))
        }
      }, [])

      useEffect(() => { void loadOverview() }, [loadOverview])
      useEffect(() => {
        const timer = setInterval(() => { void loadOverview() }, 3000)
        return () => clearInterval(timer)
      }, [loadOverview])

      const doRestart = async () => {
        setBusy(true)
        try {
          await postJson('/restart')
          await loadOverview()
        } catch (e) {
          setError(String(e.message || e))
        } finally {
          setBusy(false)
        }
      }

      const reviews = overview?.reviews || []
      const cur = detail?.summary || reviews.find((r) => r.id === activeId)
      const chapters = detail?.chapters || []
      const doneChapters = chapters.filter((c) => c.status === 'COMPLETED').length
      const runningChapters = chapters.filter((c) => c.status === 'RUNNING').length
      const failedChapters = chapters.filter((c) => c.status === 'FAILED').length
      const issueCount = cur?.issues ?? (detail?.groups || []).reduce((n, g) => n + (g.issue_ids?.length || 0), 0)
      const basics = detail?.basics || ['B01', 'B02', 'B03', 'B04'].map((id) => ({ id, status: 'PENDING', progress: [0, 1] }))

      const tiles = [
        { label: '整体状态', value: cur ? (STATUS[cur.status] || STATUS.UNKNOWN).label : '—', note: cur ? `用时 ${elapsed(cur.createdAt, cur.status === 'RUNNING' ? null : cur.updatedAt)}` : '' },
        { label: '子任务', value: cur ? `${cur.tasks.completed}/${cur.tasks.total}` : '—', note: cur ? `进行中 ${cur.tasks.running} · 失败 ${cur.tasks.failed}` : '' },
        { label: '内容章节', value: chapters.length ? `${doneChapters}/${chapters.length}` : '—', note: chapters.length ? `进行中 ${runningChapters} · 失败 ${failedChapters}` : '' },
        { label: '发现问题', value: issueCount != null ? String(issueCount) : '—', note: detail?.groups?.length ? `${detail.groups.length} 组` : '' },
      ]

      return h('div', { className: 'tsr-root' },
        h('div', { className: 'tsr-wrap' },
          h('div', { className: 'tsr-head' },
            h('div', { className: 'tsr-title' }, '审查状态'),
            h('div', { className: 'tsr-sub' },
              h(Dot, { status: overview?.stack?.up ? 'RUNNING' : 'FAILED' }),
              ' ',
              overview?.stack?.up ? '后端就绪' : '后端起未启动',
              overview?.stack?.mcpPort ? ` · 工具通道 ${overview.stack.mcpPort}` : '',
            ),
            h('div', { className: 'tsr-actions' },
              h('button', { className: 'tsr-btn', onClick: () => void loadOverview() }, '刷新'),
              h('button', { className: 'tsr-btn', onClick: doRestart, disabled: busy }, busy ? '重启中…' : '重启后端'),
            ),
          ),
          error ? h('div', { className: 'tsr-note' }, `读取失败：${error}`) : null,
          h('div', { className: 'tsr-tiles' },
            tiles.map((t) => h('div', { className: 'tsr-tile', key: t.label },
              h('div', { className: 'tsr-tile-label' }, t.label),
              h('div', { className: 'tsr-tile-value' }, t.value),
              t.note ? h('div', { className: 'tsr-tile-note' }, t.note) : null,
            )),
          ),
          reviews.length === 0
            ? h('div', { className: 'tsr-empty' },
              h('strong', null, '还没有审查任务'),
              '在会话里让 Agent 上传技术规范书并调用 ',
              h('code', { className: 'tsr-kbd' }, 'start_review'),
              '，进度会实时出现在这里。',
            )
            : h('div', { className: 'tsr-cols' },
              h('div', { className: 'tsr-list' },
                h('div', { className: 'tsr-list-head' }, h('span', null, '最近任务'), h('span', null, `${reviews.length}`)),
                reviews.map((r) => h('button', {
                  key: r.id, className: 'tsr-item', 'data-active': r.id === activeId,
                  onClick: () => void selectReview(r.id), title: r.id,
                },
                  h('div', { className: 'tsr-item-name' }, r.sourceName || r.id),
                  h('div', { className: 'tsr-item-meta' },
                    h(Dot, { status: r.status }),
                    h('span', null, (STATUS[r.status] || STATUS.UNKNOWN).label),
                    h('span', null, r.objectScope || '—'),
                    r.issues != null ? h('span', null, `${r.issues} 项`) : null,
                    h('span', null, fmtTime(r.updatedAt || r.createdAt)),
                  ),
                )),
              ),
              h('div', null,
                h('div', { className: 'tsr-card' },
                  h('div', { className: 'tsr-card-h' },
                    h('span', { className: 'tsr-card-t' }, '任务地图'),
                    h('span', { className: 'tsr-sub' },
                      cur ? `${cur.sourceName || cur.id} · ${cur.objectScope || '未识别物资类型'}` : ''),
                  ),
                  h('div', { className: 'tsr-card-b' },
                    h('div', { style: { marginBottom: 4 } },
                      basics.map((b) => h(Lane, {
                        key: b.id, id: b.id, label: b.id,
                        status: b.status,
                        done: Array.isArray(b.progress) ? b.progress[0] : 0,
                        total: Array.isArray(b.progress) ? b.progress[1] : 1,
                      })),
                      chapters.length
                        ? h(Lane, {
                          id: 'B05', label: 'B05 章节',
                          status: failedChapters > 0 ? 'FAILED' : doneChapters === chapters.length ? 'COMPLETED' : runningChapters > 0 ? 'RUNNING' : 'PENDING',
                          done: doneChapters, total: chapters.length,
                        })
                        : null,
                    ),
                    h('div', { className: 'tsr-legend' },
                      Object.entries(STATUS).filter(([k]) => k !== 'UNKNOWN').map(([k, v]) =>
                        h('span', { key: k }, h('span', { className: 'tsr-dot', style: { background: v.color } }), v.label)),
                    ),
                    h(ChapterGrid, { chapters }),
                  ),
                ),
                detail?.groups?.length
                  ? h('div', { className: 'tsr-card', style: { marginTop: 14 } },
                    h('div', { className: 'tsr-card-h' }, h('span', { className: 'tsr-card-t' }, '问题分布')),
                    h('div', { className: 'tsr-card-b' },
                      detail.groups.map((g) => h('div', { className: 'tsr-lane', key: g.title, style: { gridTemplateColumns: 'minmax(0,1fr) 64px' } },
                        h('div', { style: { fontSize: 12.5 } }, g.title),
                        h('div', { className: 'tsr-lane-val' }, `${g.issue_ids?.length || 0} 项`),
                      )),
                    ),
                  )
                  : null,
                detail && (failedChapters > 0 || (detail.limitations || []).length > 0)
                  ? h('div', { className: 'tsr-note' },
                    failedChapters > 0 ? `有 ${failedChapters} 个章节失败，可让 Agent 调用 retry_review 复用已完成项重跑。` : '',
                    (detail.limitations || []).length ? ` 解析限制：${detail.limitations.join('；')}` : '')
                  : null,
              ),
            ),
        ),
      )
    }

    // 三个模型/服务各自成块；嵌入模型只读展示 —— 它跟着规则服务的索引走，
    // 客户端改不了，改了就与索引不在同一向量空间。
    const EDITABLE_GROUPS = [
      {
        id: 'chat',
        title: '对话模型',
        hint: '物资类型识别、逐句文字质量、章节审查与最终汇总都走它，需要支持 function calling。',
        fields: [
          { key: 'chatBaseUrl', label: '接口地址', placeholder: 'https://…/v1' },
          { key: 'chatModel', label: '模型名', placeholder: '模型标识' },
        ],
        secret: true,
      },
      {
        id: 'rerank',
        title: '重排模型',
        hint: '可选。对语义召回的候选做相关性重排；不填则直接用服务端返回的顺序。',
        fields: [
          { key: 'rerankBaseUrl', label: '接口地址', placeholder: 'https://…/v1' },
          { key: 'rerankModel', label: '模型名', placeholder: '模型标识' },
          { key: 'rerankThreshold', label: '相关性阈值', type: 'number', placeholder: '0', width: 'half' },
        ],
        secret: true,
      },
    ]

    function SettingsPage(props) {
      const { setSecret, unsetSecret } = props
      useStyles()
      const [values, setValues] = useState(null)
      const [secrets, setSecrets] = useState({ chat: false, rerank: false, rules: false })
      const [refs, setRefs] = useState({
        chat: 'TSR_CHAT_API_KEY', rerank: 'TSR_RERANK_API_KEY', rules: 'TSR_RULES_TOKEN',
      })
      const [drafts, setDrafts] = useState({ chat: '', rerank: '', rules: '' })
      const [serverInfo, setServerInfo] = useState(null)
      const [message, setMessage] = useState('')
      const [level, setLevel] = useState('dim')
      const [busy, setBusy] = useState(false)

      const load = useCallback(async () => {
        try {
          const data = await getJson('/settings')
          setValues(data.values || {})
          setSecrets(data.secrets || { chat: false, rerank: false })
          if (data.refs) setRefs(data.refs)
          setMessage('')
          setLevel('dim')
        } catch (e) {
          setValues({})
          setMessage(`读取设置失败：${e.message || e}`)
          setLevel('error')
        }
        try {
          setServerInfo(await getJson('/server-info'))
        } catch (e) {
          setServerInfo({ reachable: false, reason: String(e.message || e) })
        }
      }, [])

      useEffect(() => { void load() }, [load])

      const setField = (key, raw) => setValues((prev) => ({ ...prev, [key]: raw }))

      // 保存只写配置文件，与后端是否在跑无关；重启是独立动作。
      const saveAll = async () => {
        setBusy(true)
        setMessage('')
        try {
          const payload = {}
          const allFields = [...EDITABLE_GROUPS.flatMap((g) => g.fields),
            { key: 'rulesBaseUrl' }, { key: 'pythonPath' }]
          for (const field of allFields) {
            const value = values?.[field.key]
            if (value === undefined || value === '') continue
            payload[field.key] = field.type === 'number' ? Number(value) : String(value)
          }
          for (const id of ['chat', 'rerank', 'rules']) {
            const draft = drafts[id]
            if (draft) await setSecret(refs[id], draft)
          }
          await postJson('/settings', { values: payload })
          setDrafts({ chat: '', rerank: '', rules: '' })
          setMessage('已保存。重启后端后生效。')
          setLevel('success')
          await load()
        } catch (e) {
          setMessage(`保存失败：${e.message || e}`)
          setLevel('error')
        } finally {
          setBusy(false)
        }
      }

      const restartBackend = async () => {
        setBusy(true)
        setMessage('')
        try {
          const result = await postJson('/restart', {})
          setMessage(result.ok ? '本地服务已重启。' : '已请求重启，正在启动中。')
          setLevel(result.ok ? 'success' : 'dim')
          await load()
        } catch (e) {
          setMessage(`重启失败：${e.message || e}`)
          setLevel('error')
        } finally {
          setBusy(false)
        }
      }

      const clearSecret = async (id) => {
        setBusy(true)
        try {
          await unsetSecret(refs[id])
          setMessage('已清除该密钥。')
          setLevel('success')
          await load()
        } catch (e) {
          setMessage(`清除失败：${e.message || e}`)
          setLevel('error')
        } finally {
          setBusy(false)
        }
      }

      const renderField = (field) => h('div', { className: 'tsr-field', key: field.key },
        h('label', { htmlFor: `tsr-${field.key}` }, field.label),
        h('input', {
          id: `tsr-${field.key}`,
          className: 'tsr-input',
          type: field.type === 'number' ? 'number' : 'text',
          placeholder: field.placeholder || '',
          value: values?.[field.key] ?? '',
          onChange: (e) => setField(field.key, e.target.value),
        }),
      )

      const renderSecret = (group) => {
        const configured = Boolean(secrets[group.id])
        return h('div', { className: 'tsr-field', key: group.id + '-key' },
          h('label', null, group.keyLabel || 'API Key'),
          h('div', null,
            h('div', { style: { display: 'flex', gap: 8, alignItems: 'center' } },
              h('input', {
                className: 'tsr-input',
                type: ['pass', 'word'].join(''),
                autoComplete: 'off',
                placeholder: configured ? '已配置，留空则不修改' : '未配置',
                value: drafts[group.id],
                onChange: (e) => setDrafts((prev) => ({ ...prev, [group.id]: e.target.value })),
              }),
              h('span', { className: 'tsr-badge', 'data-ok': String(configured) },
                configured ? '已配置' : '未配置'),
              configured
                ? h('button', { className: 'tsr-btn', onClick: () => void clearSecret(group.id), disabled: busy }, '清除')
                : null,
            ),
            h('div', { className: 'tsr-hint' },
              '凭据引用名 ', h('code', { className: 'tsr-kbd' }, refs[group.id]),
              '，值存放在 DSH 凭据库，不进插件配置与日志。'),
          ),
        )
      }

      const info = serverInfo || {}
      const serverState = info.reachable
        ? h('span', { className: 'tsr-badge', 'data-ok': 'true' }, '已连接')
        : h('span', { className: 'tsr-badge' }, '未连接')

      return h('div', { className: 'tsr-root' },
        h('div', { className: 'tsr-wrap', style: { maxWidth: 780 } },
          h('div', { className: 'tsr-head' },
            h('div', { className: 'tsr-title' }, '技术规范书审查'),
            h('div', { className: 'tsr-sub' }, '模型接入与规则服务'),
          ),
          values === null
            ? h('div', { className: 'tsr-note' }, '正在读取设置…')
            : h('div', null,
              EDITABLE_GROUPS.map((group) => h('div', { className: 'tsr-card', key: group.id, style: { marginTop: 16 } },
                h('div', { className: 'tsr-card-h' },
                  h('span', { className: 'tsr-card-t' }, group.title),
                  h('span', { className: 'tsr-sub', style: { marginLeft: 'auto' } },
                    h('span', { className: 'tsr-badge', 'data-ok': String(Boolean(secrets[group.id])) },
                      secrets[group.id] ? '密钥已配置' : '未配置密钥')),
                ),
                h('div', { className: 'tsr-card-b' },
                  h('div', { className: 'tsr-hint', style: { marginTop: 0, marginBottom: 10 } }, group.hint),
                  group.fields.map(renderField),
                  group.secret ? renderSecret(group) : null,
                ),
              )),

              h('div', { className: 'tsr-card', style: { marginTop: 16 } },
                h('div', { className: 'tsr-card-h' },
                  h('span', { className: 'tsr-card-t' }, '规则服务'),
                  h('span', { className: 'tsr-sub', style: { marginLeft: 'auto' } }, serverState),
                ),
                h('div', { className: 'tsr-card-b' },
                  h('div', { className: 'tsr-hint', style: { marginTop: 0, marginBottom: 10 } },
                    '规则库与语义索引都在服务端，嵌入模型由它统一决定；客户端只取规则、不做向量计算。'),
                  renderField({ key: 'rulesBaseUrl', label: '服务地址', placeholder: 'http://127.0.0.1:8010' }),
                  renderSecret({ id: 'rules', keyLabel: '服务令牌（可选）' }),
                  h('div', { className: 'tsr-field' },
                    h('label', null, '嵌入模型'),
                    h('div', { className: 'tsr-hint', style: { marginTop: 0 } },
                      info.reachable
                        ? `${info.embedding_model || '未配置'}${info.embedding_dimensions ? `（${info.embedding_dimensions} 维）` : ''}`
                        : '服务未连接，无法读取'),
                  ),
                  h('div', { className: 'tsr-field' },
                    h('label', null, '索引规模'),
                    h('div', { className: 'tsr-hint', style: { marginTop: 0 } },
                      info.reachable
                        ? `${info.chunks ?? 0} 个分块 · ${info.active_rules ?? 0} 条生效规则`
                        : '—'),
                  ),
                ),
              ),

              h('div', { className: 'tsr-card', style: { marginTop: 16 } },
                h('div', { className: 'tsr-card-h' }, h('span', { className: 'tsr-card-t' }, '本地服务')),
                h('div', { className: 'tsr-card-b' },
                  h('div', { className: 'tsr-hint', style: { marginTop: 0, marginBottom: 10 } },
                    '文档解析与审查编排跑在本机，首次使用会自动准备 Python 环境。'),
                  renderField({ key: 'pythonPath', label: 'Python 解释器', placeholder: '留空自动准备' }),
                  h('div', { className: 'tsr-hint', style: { marginTop: 12 } },
                    '审查工具由本插件直接提供给当前会话（',
                    h('code', { className: 'tsr-kbd' }, 'mcp__technical-spec-review__*'),
                    '），不占用"MCP 连接器"里的条目。'),
                ),
              ),

              h('div', { className: 'tsr-saverow' },
                h('button', { className: 'tsr-btn tsr-btn-primary', onClick: () => void saveAll(), disabled: busy },
                  busy ? '处理中…' : '保存'),
                h('button', { className: 'tsr-btn', onClick: () => void restartBackend(), disabled: busy }, '重启本地服务'),
                h('button', { className: 'tsr-btn', onClick: () => void load(), disabled: busy }, '重新读取'),
                message
                  ? h('span', {
                    className: 'tsr-msg',
                    style: { color: level === 'error' ? T.error : level === 'success' ? T.success : T.dim },
                  }, message)
                  : null,
              ),
            ),
        ),
      )
    }

    return {
      inject: ['slots', 'remote'],
      apply(ctx) {
        const credentialStore = () => {
          const credentials = ctx.remote?.credentials
          if (credentials === undefined) throw new Error('DSH 凭据服务不可用')
          return credentials
        }
        const setSecret = (ref, value) => credentialStore().set(ref, value)
        const unsetSecret = (ref) => credentialStore().unset(ref)

        const mk = (name, options, Component) =>
          ctx.slots.inject(name, () => ctx.slots.register({ name, ...options }, Component))

        // 设置页：进设置
        mk('settings.section', {
          id: 'technical-spec-review',
          order: 45,
          label: () => '技术规范书审查',
          inject: () => ({ setSecret, unsetSecret }),
        }, SettingsPage)

        // 状态页只进会话视图：没有任务时不该在侧边栏占一个常驻入口。
        mk('conversation.view', {
          id: 'technical-spec-review-status',
          order: 30,
          label: () => '审查状态',
        }, StatusBoard)
      },
    }
  },
})
