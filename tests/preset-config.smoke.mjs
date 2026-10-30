// 预设配置冒烟：预设里每个子插件的 config 必须满足该插件的 Config schema。
//
// 背景：技术规范书审查专家预设曾因 tool-fs-search / tool-todo 两个必填项缺失而
// 「加载失败」，在会话预设选择器里被静默隐藏。本脚本把这类机械错误变成确定性检查。
//
// 用法：node tests/preset-config.smoke.mjs [应用 node_modules 目录]
import { readFileSync } from 'node:fs'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const patchPath = resolve(here, '..', 'cordis.patch.yml')

const appModules =
  process.argv[2] ||
  process.env.DSH_APP_NODE_MODULES ||
  'C:/Users/Lenovo/AppData/Local/Programs/DeepSeek Harness/resources/app/dsh/node_modules'

// 只为结构校验：把 !!js 表达式换成中性布尔值。
const source = readFileSync(patchPath, 'utf8').replace(/!!js\s+[^\n]+/g, 'true')

let YAML
for (const candidate of [
  pathToFileURL(join(appModules, 'yaml', 'dist', 'index.js')).href,
  pathToFileURL(join(appModules, 'js-yaml', 'index.js')).href,
  'yaml',
  'js-yaml',
]) {
  try {
    YAML = await import(candidate)
    break
  } catch {
    // 换下一个候选
  }
}
if (!YAML) {
  console.error('找不到 YAML 解析库（yaml / js-yaml）')
  process.exit(1)
}
const doc = YAML.parse ? YAML.parse(source) : YAML.load(source)

const presetRows = doc
  .flatMap((chunk) => chunk.insert || [])
  .filter((row) => row.name === '@deepseek-ai/dsh-agent-preset')

if (presetRows.length === 0) {
  console.error('没找到任何 agent preset 声明')
  process.exit(1)
}

function configSchema(mod) {
  const candidates = [mod.Config, mod.config, mod.default?.Config, mod.default?.config]
  return candidates.find((item) => typeof item === 'function')
}

function validate(Schema, value) {
  try {
    Schema(value)
    return null
  } catch (error) {
    return String(error?.message || error).replace(/\n/g, ' | ')
  }
}

const failures = []
for (const row of presetRows) {
  const presetId = row.config?.id || row.id
  for (const plugin of row.config?.plugins || []) {
    if (!plugin.name || !plugin.name.startsWith('@deepseek-ai/')) continue
    const packageName = plugin.name.split('/')[1]
    const entry = join(appModules, '@deepseek-ai', packageName, 'lib', 'index.js')
    let mod
    try {
      mod = await import(pathToFileURL(entry).href)
    } catch (error) {
      failures.push(`${presetId}/${plugin.id}: 无法导入 ${packageName}：${String(error.message).slice(0, 120)}`)
      continue
    }
    const Schema = configSchema(mod)
    if (!Schema) continue
    const problem = validate(Schema, plugin.config || {})
    if (problem) failures.push(`${presetId}/${plugin.id} (${packageName}): ${problem}`)
  }
}

if (failures.length) {
  console.error(`预设配置不合法 ${failures.length} 处：`)
  for (const item of failures) console.error(' - ' + item)
  process.exit(1)
}
console.log(`预设配置检查通过：${presetRows.length} 个预设，全部子插件 config 合法。`)
