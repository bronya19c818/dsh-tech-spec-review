// 插件 Host 半边的冒烟测试：用桩 ctx 驱动 apply()，验证解释器解析、
// 本地栈拉起、工具注册与卸载回收。不进 DSH profile，不碰用户运行环境。
// 用法：node tests/plugin-apply.smoke.mjs
import assert from 'node:assert/strict'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')

const { apply, resolvePython } = await import(pathToFileURL(join(ROOT, 'src', 'index.js')).href)

const registered = []
const disposers = []
const logs = []

const ctx = {
  effect(setup) {
    const dispose = typeof setup === 'function' ? setup() : undefined
    if (typeof dispose === 'function') disposers.push(dispose)
    return () => dispose?.()
  },
  inject(_names, fn) {
    fn(ctx)
  },
  tools: {
    register(definition) {
      registered.push(definition)
      return () => {}
    },
  },
  timer: {
    interval(cb, ms) {
      const id = setInterval(cb, ms)
      return () => clearInterval(id)
    },
  },
  logger: {
    warn: (m) => logs.push(String(m)),
  },
}

const python = resolvePython({}, ROOT)
console.log('resolved python:', python)
assert.ok(python.endsWith('python.exe') || python.endsWith('python') || !python.includes('/'), '解释器应被解析')

apply(ctx, {})

// 等本地栈起来（首次 pgserver 启动可能到 2 分钟）
const deadline = Date.now() + 180_000
let up = false
while (Date.now() < deadline) {
  try {
    await fetch('http://127.0.0.1:8775/mcp', { method: 'GET' })
    up = true
    break
  } catch {
    await new Promise((r) => setTimeout(r, 2000))
  }
}

console.log('stack up:', up)
console.log('registered tools:', registered.map((t) => t.name))
assert.ok(up, '本地栈应被拉起')
assert.equal(registered.length, 1, '应注册一个自检工具')

const status = await registered[0].execute()
console.log('stack_status:', status)
assert.equal(status.ready, true)

console.log('warnings:', logs)

// 卸载：跑 disposer，确认子进程与 postgres 退出
for (const dispose of disposers) dispose()
await new Promise((r) => setTimeout(r, 6000))
let stillUp = false
try {
  await fetch('http://127.0.0.1:8775/mcp', { method: 'GET' })
  stillUp = true
} catch {
  stillUp = false
}
console.log('after dispose, stack still listening:', stillUp)
assert.equal(stillUp, false, '卸载后本地栈应停止')
console.log('SMOKE OK')
