// Runs a workflow script with stubbed agents so its control flow can be tested without a model.
import { readFileSync } from 'node:fs'

const [, , file, scenario] = process.argv
const source = readFileSync(file, 'utf8').replace(/^export const meta/m, 'const meta')
const { args, find, refute } = JSON.parse(scenario)
const calls = []

const agent = async (prompt, opts = {}) => {
  calls.push(opts.label)
  if (opts.label.startsWith('find:')) return find
  return { refuted: refute.some(parts => [].concat(parts).every(text => prompt.includes(text))), reason: 'stub' }
}
const parallel = thunks => Promise.all(thunks.map(t => t().catch(() => null)))
const pipeline = (items, ...stages) =>
  Promise.all(items.map(async (item, index) => {
    let value = item
    for (const stage of stages) {
      try {
        value = await stage(value, item, index)
      } catch {
        return null
      }
    }
    return value
  }))
const logs = []
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'log', 'phase', source)
const result = await run(args, agent, parallel, pipeline, m => logs.push(m), () => {})
process.stdout.write(JSON.stringify({ calls, logs, result }))
