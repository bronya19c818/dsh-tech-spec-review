// Client 半边冒烟：用桩 ModuleLoader / React / ctx 执行 src/client.js，
// 验证四个挂载点按契约注册。不渲染组件（渲染由宿主的 slot 渲染器负责）。
import assert from 'node:assert/strict'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'

const HERE = dirname(fileURLToPath(import.meta.url))
const SOURCE = join(HERE, '..', 'src', 'client.js')

const registrations = []
let factoryResult

const reactStub = {
  createElement: () => null,
  useCallback: (fn) => fn,
  useEffect: () => undefined,
  useMemo: (fn) => fn(),
  useRef: (value) => ({ current: value }),
  useState: (value) => [value, () => undefined],
}

globalThis.window = {
  __ModuleLoader__: {
    load({ id, factory }) {
      assert.equal(id, 'dsh-technical-spec-review', 'loader id 必须等于包名')
      factoryResult = factory((specifier) => {
        if (specifier === 'react') return reactStub
        throw new Error(`unexpected require: ${specifier}`)
      })
    },
  },
}

const ctx = {
  slots: {
    inject(name, callback) {
      callback()
      return () => undefined
    },
    register(options, Component) {
      assert.equal(typeof Component, 'function', `${options.name} 需要组件`)
      registrations.push({ ...options, component: Component })
      return () => undefined
    },
  },
  remote: {
    credentials: {
      set: async () => undefined,
      unset: async () => undefined,
    },
  },
}

// 执行浏览器动态插件源码
new Function(readFileSync(SOURCE, 'utf8'))()

assert.ok(factoryResult, '源码应调用 __ModuleLoader__.load')
assert.deepEqual(factoryResult.inject, ['slots', 'remote'], 'inject 应声明 slots 与 remote')
factoryResult.apply(ctx)

const byName = (name) => registrations.filter((r) => r.name === name)
const names = registrations.map((r) => r.name).sort()
console.log('registered:', names)

assert.equal(byName('settings.section').length, 1, '设置页注册一次')
assert.equal(byName('conversation.view').length, 1, '会话视图注册一次')
// 状态页不占侧边栏：没有任务时不应有常驻入口
assert.equal(byName('sidebar.panellist').length, 0, '不注册侧边栏入口')
assert.equal(byName('main').length, 0, '不注册中央面板')

const view = byName('conversation.view')[0]
assert.equal(typeof view.label, 'function', 'label 用 thunk 跟随语言切换')

const settings = byName('settings.section')[0]
assert.equal(typeof settings.inject, 'function', '设置页需要注入凭据回调')
const injected = settings.inject()
assert.equal(typeof injected.setSecret, 'function')
assert.equal(typeof injected.unsetSecret, 'function')

console.log('CLIENT SMOKE OK')
