// Tests for the add-on's JavaScript, run in a sandbox standing in for FDM's script engine:
// node --test tests/addon.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const here = new URL('../addon/', import.meta.url)
const manifest = JSON.parse(readFileSync(new URL('manifest.json', here), 'utf8'))

function loadAddon(bridgeAnswer, proxy = '') {
  const calls = []
  const tmpFiles = []
  const context = {
    console: { log() {} },
    Promise,
    JSON,
    qtJsSystem: { defaultUserAgent: 'FDM-Test/6' },
    qtJsNetworkProxyMgr: { proxyForUrl: () => ({ url: () => proxy }) },
    qtJsTools: {
      createTmpFile(name) {
        const file = { path: '/tmp/' + name, text: null, writeText(t) { this.text = t; return true } }
        tmpFiles.push(file)
        return file
      }
    },
    launchPythonScript(requestId, interactive, script, args) {
      // Copied out of the sandbox, whose arrays aren't this realm's.
      calls.push({ requestId, interactive, script, args: JSON.parse(JSON.stringify(args)) })
      const answer = typeof bridgeAnswer === 'function' ? bridgeAnswer(args) : bridgeAnswer
      return Promise.resolve({ output: 'some log line\n' + JSON.stringify(answer) + '\n', errorOutput: '', exitCode: 0 })
    }
  }
  vm.createContext(context)
  for (const script of manifest.scripts) {
    vm.runInContext(readFileSync(new URL(script, here), 'utf8'), context, { filename: script })
  }
  return { parser: vm.runInContext('msParser', context), calls, tmpFiles }
}

const okAnswer = {
  ok: true,
  url: 'http://127.0.0.1:47913/d/abc/ubuntu.iso',
  filename: 'ubuntu.iso',
  size: 5_000_000_000,
  networks: ['Wi-Fi', 'iPhone USB'],
  ui: 'http://127.0.0.1:47913/'
}

test('manifest lists files that exist and asks only for Python', () => {
  for (const file of [...manifest.scripts, manifest.icon]) readFileSync(new URL(file, here))
  readFileSync(new URL('bridge/plexo_fdm.py', here))
  assert.deepEqual(manifest.permissions, ['launchPython'])
  assert.equal(manifest.mediaParser, true)
})

test('offers large files and skips pages, small files and its own links', () => {
  const { parser } = loadAddon(okAnswer)
  const file = { url: 'https://example.com/big.iso', contentType: 'application/octet-stream', resourceSize: 900e6 }
  assert.equal(parser.isPossiblySupportedSource(file), true)
  assert.equal(parser.isPossiblySupportedSource({ ...file, resourceSize: -1 }), true)
  assert.equal(parser.isPossiblySupportedSource({ ...file, resourceSize: 1000 }), false)
  assert.equal(parser.isPossiblySupportedSource({ ...file, contentType: 'text/html; charset=utf-8' }), false)
  assert.equal(parser.isPossiblySupportedSource({ ...file, url: 'ftp://example.com/big.iso' }), false)
  assert.equal(parser.isPossiblySupportedSource({ ...file, url: 'http://127.0.0.1:47913/d/x/big.iso' }), false)
})

test('a #plexo link is always taken', () => {
  const { parser } = loadAddon(okAnswer)
  assert.equal(parser.isSupportedSource('https://example.com/a.zip#plexo'), true)
  assert.equal(parser.isSupportedSource('https://example.com/a.zip'), false)
  assert.equal(parser.isSupportedSource('http://127.0.0.1:47913/d/x/a.zip#plexo'), false)
})

test('parse hands FDM the bridge link as the only format', async () => {
  const { parser, calls, tmpFiles } = loadAddon(okAnswer)
  const info = await parser.parse({
    url: 'https://example.com/ubuntu.iso',
    requestId: 7,
    interactive: true,
    resourceSize: 5_000_000_000,
    cookies: [{ domain: '.example.com', name: 'sid', value: '1', isSecure: true, expirationDate: new Date(0) }]
  })
  assert.equal(info.title, 'ubuntu')
  assert.equal(info.formats.length, 1)
  assert.equal(info.formats[0].url, okAnswer.url)
  assert.equal(info.formats[0].ext, 'iso')
  assert.equal(info.formats[0].filesize, okAnswer.size)
  assert.match(info.formats[0].format_note, /Wi-Fi \+ iPhone USB/)

  const { script, args, requestId, interactive } = calls[0]
  assert.equal(script, 'bridge/plexo_fdm.py')
  assert.equal(requestId, 7)
  assert.equal(interactive, true)
  assert.deepEqual(args.slice(0, 3), ['register', '--url', 'https://example.com/ubuntu.iso'])
  assert.equal(args[args.indexOf('--size') + 1], '5000000000')
  assert.equal(args[args.indexOf('--user-agent') + 1], 'FDM-Test/6')
  // Cookies go through a file, never the command line.
  assert.ok(!args.join(' ').includes('sid'))
  assert.equal(args[args.indexOf('--cookies-file') + 1], tmpFiles[0].path)
  assert.deepEqual(JSON.parse(tmpFiles[0].text)[0], {
    domain: '.example.com', path: '/', name: 'sid', value: '1', isSecure: true, expires: 0
  })
})

test('a declined link goes back to FDM as an ordinary download', async () => {
  const { parser } = loadAddon({ ok: false, reason: 'small', message: 'Smaller than 16 MB' })
  await assert.rejects(parser.parse({ url: 'https://example.com/a.zip', requestId: 1 }), (e) => {
    assert.equal(e.error, 'Unsupported URL')
    assert.equal(e.isParseError, false)
    return true
  })
})

test('a bridge failure on a #plexo link is shown to the person', async () => {
  const { parser } = loadAddon({ ok: false, reason: 'error', message: 'Port in use' })
  await assert.rejects(parser.parse({ url: 'https://example.com/a.zip#plexo', requestId: 1 }), (e) => {
    assert.match(e.error, /Port in use/)
    assert.equal(e.isParseError, true)
    return true
  })
})

test('a file without an extension keeps its name', async () => {
  const { parser } = loadAddon({ ...okAnswer, filename: 'README' })
  const info = await parser.parse({ url: 'https://example.com/README', requestId: 1 })
  assert.equal(info.title, 'README')
})

test('with a proxy set in FDM, links are left to FDM unless marked', async () => {
  const { parser, calls } = loadAddon(okAnswer, 'http://proxy.local:3128')
  await assert.rejects(parser.parse({ url: 'https://example.com/big.iso', requestId: 1 }), (e) => {
    assert.equal(e.error, 'Unsupported URL')
    return true
  })
  assert.equal(calls.length, 0)
  const info = await parser.parse({ url: 'https://example.com/big.iso#plexo', requestId: 2 })
  assert.equal(info.formats[0].url, okAnswer.url)
})
