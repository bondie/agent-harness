export const meta = {
  name: 'upgrade-impact',
  description: 'Find which changes between two versions of a dependency hit your code: one agent per component group reads the changelog and searches the repositories, skeptics try to refute each risk',
  whenToUse: 'Before a patch or minor upgrade of a framework or library that one or more repositories use',
  phases: [
    { title: 'Find', detail: 'one agent per component group: changelog entries in range, usages in the repositories' },
    { title: 'Verify', detail: 'skeptics try to refute each risk: two for a high one, one for a medium one' },
  ],
}

// args: {
//   dependency: 'Framework 6.4', from: '6.4.10', to: '6.4.50',
//   changelog: '/abs/path/CHANGELOG.md',
//   repos: [{ name, path, ref: 'origin/dev', paths: ['src', 'config'] }],
//   groups: [{ name: 'http', components: ['http-foundation', 'http-kernel'] }],
//   versions: { 'http-foundation': '6.4.10' },  // optional: installed version per component
//   notes: 'optional context for every agent',
// }

const FINDINGS = {
  type: 'object',
  properties: {
    reviewed: { type: 'integer', description: 'changelog entries in range for these components' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          component: { type: 'string' },
          version: { type: 'string' },
          change: { type: 'string', description: 'the changelog entry, verbatim' },
          usage: { type: 'array', items: { type: 'string' }, description: 'repo:path:line' },
          risk: { type: 'string', enum: ['high', 'medium', 'low'] },
          why: { type: 'string' },
          test: { type: 'string', description: 'a concrete check before or after the upgrade' },
        },
        required: ['component', 'version', 'change', 'usage', 'risk', 'why', 'test'],
      },
    },
  },
  required: ['reviewed', 'findings'],
}

const VERDICT = {
  type: 'object',
  properties: { refuted: { type: 'boolean' }, reason: { type: 'string' } },
  required: ['refuted', 'reason'],
}

const a = args || {}
if (!a.changelog || !a.from || !a.to || !(a.repos || []).length || !(a.groups || []).length) {
  throw new Error('args need dependency, from, to, changelog, repos and groups')
}

const repoLines = a.repos
  .map(r => `- ${r.name}: \`git -C ${r.path} grep -n <pattern> ${r.ref || 'HEAD'} -- ${(r.paths || ['.']).join(' ')}\`, open a file with \`git -C ${r.path} show ${r.ref || 'HEAD'}:<file>\``)
  .join('\n')

const context = `Upgrade of ${a.dependency || 'a dependency'} from ${a.from} to ${a.to}.
Changelog: ${a.changelog} (read it from the file; entries name their component).
Repositories, read only (never check out, edit or run anything that changes state):
${repoLines}
${a.notes || ''}`

const findPrompt = g => `${context}

Your components: ${g.components.join(', ')}.

1. Take every changelog entry of a release after ${a.from} up to and including ${a.to} that concerns one of your components. Count them as "reviewed".${installed(g)}
2. Keep the entries that can change how our code behaves: a bug fix whose old behaviour our code may rely on, a security fix that changes behaviour (stricter parsing, headers, encoding, access checks), a changed default. Skip fixes for features we do not use, internal refactorings and deprecation-only entries.
3. For each kept entry, search the repositories for the API, option, configuration key or behaviour involved and open the hits. Configuration counts as usage (framework and package config files), and so does behaviour every request relies on (sessions, firewalls, trusted proxies, serialization of API responses); then cite the config file or the code that depends on it.
4. Return only findings with evidence: the entry verbatim, its version, the usages as repo:path:line, the risk (high: likely breaks or changes what users see; medium: could change behaviour, worth a test; low: the API is used but the change is benign for it), why, and a concrete test. An entry without a usage in our code is not a finding.
5. A medium or high risk must name the old and the new behaviour and the code path of ours that sees the difference. When you cannot name them, the risk is low: in a pilot, skeptics refuted 36 of 42 medium and high risks, mostly because the change did not apply to the way our code used the API.`

const installed = g => {
  const pinned = g.components.filter(c => a.versions && a.versions[c])
  if (!pinned.length) return ''
  return `\n   Installed versions differ per component; for these, start after the installed one instead: ${pinned.map(c => `${c} ${a.versions[c]}`).join(', ')}.`
}

const LENSES = [
  'Lens: the change itself. Read the changelog entry in the file. Is it in the version range, does it concern this component, and does it change behaviour the way the finding claims?',
  'Lens: our usage. Open every cited usage. Does our code or configuration really exercise the changed behaviour, so that the difference between the old and the new version can show up?',
]

const verifyPrompt = (f, lens) => `${context}

Try to refute this upgrade risk. Default to refuted=true when the evidence does not hold.
${lens}

Finding: ${JSON.stringify(f)}

Answer refuted true or false and the reason in at most two sentences.`

const results = await pipeline(
  a.groups,
  g => agent(findPrompt(g), { label: `find:${g.name}`, phase: 'Find', schema: FINDINGS, model: 'sonnet', effort: 'medium' }),
  (found, g) => {
    const all = (found && found.findings) || []
    const risky = all.filter(f => f.risk !== 'low')
    return parallel(
      risky.map(f => () =>
        parallel(
          (f.risk === 'high' ? LENSES : LENSES.slice(1)).map(lens => () =>
            agent(verifyPrompt(f, lens), { label: `verify:${g.name}:${lens === LENSES[0] ? 'change' : 'usage'}`, phase: 'Verify', schema: VERDICT, model: 'sonnet', effort: 'high' }),
          ),
        ).then(votes => {
          const cast = votes.filter(Boolean)
          return { ...f, votes: cast.length, refutations: cast.filter(v => v.refuted).map(v => v.reason) }
        }),
      ),
    ).then(checked => ({ group: g.name, reviewed: found ? found.reviewed : 0, low: all.filter(f => f.risk === 'low'), checked: checked.filter(Boolean) }))
  },
)

const done = results.filter(Boolean)
const failed = a.groups.filter(g => !done.some(r => r.group === g.name)).map(g => g.name)
if (failed.length) log(`No result for groups: ${failed.join(', ')}`)

const order = { high: 0, medium: 1 }
const confirmed = done
  .flatMap(r => r.checked.filter(f => f.votes === 0 || f.refutations.length < f.votes).map(f => ({ ...f, group: r.group, disputed: f.refutations.length > 0 })))
  .sort((x, y) => order[x.risk] - order[y.risk])
const refuted = done.flatMap(r => r.checked.filter(f => f.votes > 0 && f.refutations.length === f.votes).map(f => ({ group: r.group, change: f.change, reasons: f.refutations })))
const low = done.flatMap(r => r.low.map(f => ({ ...f, group: r.group })))
const reviewed = done.reduce((sum, r) => sum + (r.reviewed || 0), 0)

log(`${reviewed} entries reviewed, ${confirmed.length} risks kept, ${refuted.length} refuted, ${low.length} low`)
return { reviewed, confirmed, refuted, low, failedGroups: failed }
