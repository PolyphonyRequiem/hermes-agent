/** Render ImageCanvas with real cell data to a PNG, via the real Ink renderer. */
import '../../src/lib/forceTruecolor.js'

import { mkdirSync, readFileSync, writeFileSync } from 'fs'
import { join } from 'path'
import { PassThrough } from 'stream'

import { Box, renderSync } from '@hermes/ink'
import React, { type ReactElement } from 'react'

import { ImageCanvas } from '../../src/components/imageCanvas.js'
import { DARK_THEME } from '../../src/theme.js'
import { visualOutDir } from './paths.mjs'

const payload = JSON.parse(readFileSync(process.argv[2], 'utf8')).params.payload

const out = new PassThrough()
let buf = ''

out.on('data', (c: Buffer) => (buf += c.toString()))

const inst = renderSync(
  React.createElement(
    Box,
    { flexDirection: 'column' },
    React.createElement(ImageCanvas, {
      caption: payload.caption,
      cells: payload.cells,
      t: DARK_THEME,
      title: payload.title
    })
  ) as ReactElement,
  { stdout: out as never }
)

inst.unmount()

const escapeHtml = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

function ansiToHtml(raw: string): string {
  let fg = '#c0caf5'
  let bg = 'transparent'
  let html = ''
  const open = () => `<span style="color:${fg};background:${bg}">`

  html += open()

  for (const part of raw.split(/(\x1b\[[0-9;]*m)/)) {
    if (!part) continue

    if (!part.startsWith('\x1b[')) {
      html += escapeHtml(part.replace(/\x1b\[[^m]*[A-Za-z]/g, ''))
      continue
    }

    const codes = part.slice(2, -1).split(';').map(Number)

    for (let i = 0; i < codes.length; i++) {
      const c = codes[i]

      if (c === 0) {
        fg = '#c0caf5'
        bg = 'transparent'
      } else if (c === 38 && codes[i + 1] === 2) {
        fg = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`
        i += 4
      } else if (c === 48 && codes[i + 1] === 2) {
        bg = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`
        i += 4
      } else if (c === 39) {
        fg = '#c0caf5'
      } else if (c === 49) {
        bg = 'transparent'
      }
    }

    html += `</span>${open()}`
  }

  return html + '</span>'
}

const dir = visualOutDir()

mkdirSync(dir, { recursive: true })
writeFileSync(
  join(dir, 'tui-visual.html'),
  '<!doctype html><meta charset="utf-8"><body style="margin:0;background:#101014;font:13px/1.0 monospace">' +
    `<pre style="margin:0;padding:14px;white-space:pre;line-height:1.0">${ansiToHtml(buf)}</pre></body>`
)
console.log('wrote', join(dir, 'tui-visual.html'))
