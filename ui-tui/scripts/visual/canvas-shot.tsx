/**
 * Canvas proof shot: renders the REAL <Panel> component driven by the REAL
 * JSON-RPC payload the Python side emits, across dark/light themes, to an
 * HTML page the Electron shooter turns into a PNG.
 *
 * Mirrors scripts/visual/render.tsx's ansiToHtml approach so what you see is
 * genuine Ink output, not a hand-drawn mockup.
 */
import '../../src/lib/forceTruecolor.js'

import { mkdirSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'
import { PassThrough } from 'stream'

import { Box, renderSync, Text } from '@hermes/ink'
import React, { type ReactElement } from 'react'

import { Panel } from '../../src/components/branding.js'
import { fromSkin, type Theme } from '../../src/theme.js'
import { visualOutDir } from './paths.mjs'

const frame = JSON.parse(readFileSync(process.argv[2] ?? '/tmp/canvas-frame.json', 'utf8'))
const payload = frame.params.payload
const sections = Array.isArray(payload?.sections) ? payload.sections : []

if (!sections.length) {
  console.error('no sections — the dispatcher would have dropped this frame')
  process.exit(1)
}

const renderAnsi = (node: ReactElement): string => {
  const out = new PassThrough()
  let buf = ''

  out.on('data', (c: Buffer) => (buf += c.toString()))

  const inst = renderSync(node, { stdout: out as never })

  inst.unmount()

  return buf
}

const escapeHtml = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

// Minimal SGR -> HTML (truecolor + reset only; enough for Panel output).
function ansiToHtml(raw: string, defaultFg: string): string {
  let fg = defaultFg
  let bold = false
  let html = ''
  const open = () => `<span style="color:${fg};font-weight:${bold ? 700 : 400}">`

  html += open()

  const parts = raw.split(/(\x1b\[[0-9;]*m)/)

  for (const part of parts) {
    if (!part) continue

    if (!part.startsWith('\x1b[')) {
      html += escapeHtml(part.replace(/\x1b\[[^m]*[A-Za-z]/g, ''))
      continue
    }

    const codes = part.slice(2, -1).split(';').map(Number)

    for (let i = 0; i < codes.length; i++) {
      const c = codes[i]

      if (c === 0) {
        fg = defaultFg
        bold = false
      } else if (c === 1) {
        bold = true
      } else if (c === 38 && codes[i + 1] === 2) {
        fg = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`
        i += 4
      } else if (c === 39) {
        fg = defaultFg
      }
    }

    html += `</span>${open()}`
  }

  return html + '</span>'
}

const scenes = [
  { bg: '#101014', label: 'dark terminal', skin: {} },
  { bg: '#ffffff', label: 'light terminal', skin: {} }
]

let page =
  '<!doctype html><meta charset="utf-8">' +
  '<body style="margin:0;background:#2a2a30;font:14px/1.35 \'Hack Nerd Font\',Menlo,monospace">' +
  '<div style="padding:20px">' +
  '<div style="color:#c0caf5;font:600 15px sans-serif;padding-bottom:14px">' +
  'canvas.render — real &lt;Panel&gt; driven by the real Python JSON-RPC payload' +
  '</div>'

for (const scene of scenes) {
  process.env.HERMES_TUI_BACKGROUND = scene.bg

  const theme: Theme = fromSkin(scene.skin, {})
  const ansi = renderAnsi(
    React.createElement(
      Box,
      { flexDirection: 'column', width: 76 },
      React.createElement(Panel, { sections, t: theme, title: payload.title ?? '' })
    ) as ReactElement
  )

  page +=
    `<div style="color:#8b93a7;font:12px sans-serif;padding:10px 0 6px">${scene.label}</div>` +
    `<pre style="margin:0;padding:12px;white-space:pre;background:${scene.bg}">` +
    ansiToHtml(ansi, theme.color.text) +
    '</pre>'
}

page += '</div></body>'

const outDir = visualOutDir()

mkdirSync(outDir, { recursive: true })
writeFileSync(join(outDir, 'tui-visual.html'), page)
console.log('wrote', join(outDir, 'tui-visual.html'))
