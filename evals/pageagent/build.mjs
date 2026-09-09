import { createHash } from 'node:crypto'
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const ROOT = dirname(fileURLToPath(import.meta.url))
const EXPECTED = {
  'node_modules/@page-agent/core': ['1.12.4', 'sha512-Ixiv3xtjBF9ystUadNtHcmt4DDPbIis9gphxe3DB36a3Kt93K8lxDMyVltIJx986KkBJ4RGjNZ4/Dv63hJdDlg=='],
  'node_modules/@page-agent/page-controller': ['1.12.4', 'sha512-I8se6/QMhg1wMZp52fkX37xbv/lkUnBihtadcoZKOQ9xvL562/sd141mu5ANPGvaCosPlXxJQ3HGIwA2VM4bwQ=='],
  'node_modules/esbuild': ['0.28.2', 'sha512-HKVLS8dvII+xoKW9kmqxbRKrnWEXfJJr/FZhhJmiqIB0e053QNYFqOBouTMO/k5sID4MvCiUCvv8b9M4h32wIA=='],
  'node_modules/zod': ['4.5.4', 'sha512-sC95tT5iHHH9gtpj6A81kh+NEaRAUFN+qlUPDUbRfOMvNf5QCBqsb3WgvnpVtK5Y+4UfA6KqufotuTvMGiTlsA=='],
}

function sha256(bytes) {
  return `sha256:${createHash('sha256').update(bytes).digest('hex')}`
}

async function atomicWrite(path, bytes) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 })
  const temporary = `${path}.${process.pid}.tmp`
  await writeFile(temporary, bytes, { mode: 0o600, flag: 'wx' })
  await rename(temporary, path)
}

export async function buildBaseline(outputRoot = resolve(ROOT, '../../.artifacts/evals/pageagent')) {
  const lockBytes = await readFile(resolve(ROOT, 'package-lock.json'))
  const lock = JSON.parse(lockBytes.toString('utf8'))
  if (lock.lockfileVersion !== 3 || lock.name !== 'web-operation-agent-pageagent-baseline') throw new Error('unexpected PageAgent lockfile header')
  const packages = {}
  for (const [name, [version, integrity]] of Object.entries(EXPECTED)) {
    const entry = lock.packages?.[name]
    if (!entry || entry.version !== version || entry.integrity !== integrity || entry.license !== 'MIT') throw new Error(`locked package mismatch: ${name}`)
    packages[name.replace('node_modules/', '')] = { version, integrity, license: entry.license }
  }
  const bridgeBytes = await readFile(resolve(ROOT, 'bridge.js'))
  const result = await build({
    absWorkingDir: ROOT,
    entryPoints: ['bridge.js'],
    bundle: true,
    format: 'iife',
    globalName: 'WoaPageAgentBaselineBundle',
    legalComments: 'eof',
    minify: false,
    platform: 'browser',
    sourcemap: false,
    target: ['es2022'],
    write: false,
    metafile: true,
  })
  if (result.outputFiles.length !== 1) throw new Error('PageAgent build produced an unexpected output set')
  const bundle = result.outputFiles[0].contents
  const manifest = {
    artifact_version: 1,
    kind: 'pageagent_baseline_bundle',
    upstream_release: 'pageagent@1.12.4',
    package_lock_digest: sha256(lockBytes),
    bridge_digest: sha256(bridgeBytes),
    bundle_digest: sha256(bundle),
    bundle_bytes: bundle.length,
    bundled_input_count: Object.keys(result.metafile.inputs).length,
    packages,
  }
  await atomicWrite(resolve(outputRoot, 'pageagent-baseline.js'), bundle)
  await atomicWrite(resolve(outputRoot, 'pageagent-baseline.manifest.json'), Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`))
  return manifest
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const manifest = await buildBaseline()
  process.stdout.write(`${JSON.stringify(manifest)}\n`)
}
