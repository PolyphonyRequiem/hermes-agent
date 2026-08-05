import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { ImageCanvas } from '../components/imageCanvas.js'
import type { ImageCell } from '../components/imageCanvas.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const t = DEFAULT_THEME
const UPPER_HALF = '\u2580'

/** One cell: [top, bottom] RGBA pixel pair. */
const cell = (top: number[], bottom: number[]): ImageCell => [top, bottom]

const OPAQUE: ImageCell = cell([255, 0, 0, 255], [0, 0, 255, 255])
const TRANSPARENT: ImageCell = cell([0, 0, 0, 0], [0, 0, 0, 0])

/** Mount an ImageCanvas via renderSync + PassThrough, returning its frame. */
function render(props: Partial<React.ComponentProps<typeof ImageCanvas>> & { cells: ImageCell[][] }) {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  let output = ''

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 40 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(React.createElement(ImageCanvas, { t, ...props }), {
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  instance.unmount()
  instance.cleanup()

  return stripAnsi(output)
}

const glyphLines = (frame: string) => frame.split('\n').filter(line => line.includes(UPPER_HALF))

describe('ImageCanvas', () => {
  it('renders one upper-half-block glyph per opaque cell', () => {
    expect(render({ cells: [[OPAQUE]] })).toContain(UPPER_HALF)
  })

  it('renders one text row per cell row', () => {
    const row = [OPAQUE, OPAQUE, OPAQUE]

    expect(glyphLines(render({ cells: [row, row, row] }))).toHaveLength(3)
  })

  it('renders fully transparent cells blank so the transcript shows through', () => {
    expect(render({ cells: [[TRANSPARENT]] })).not.toContain(UPPER_HALF)
  })

  it('renders nothing at all when there are no cells', () => {
    expect(render({ cells: [] }).trim()).toBe('')
  })

  it('renders the caption so a user can identify the image', () => {
    expect(render({ caption: 'map.png 2048x2048', cells: [[OPAQUE]] })).toContain('map.png')
  })

  it('renders the title when provided', () => {
    expect(render({ cells: [[OPAQUE]], title: 'Latency' })).toContain('Latency')
  })

  it('survives malformed cell data without throwing', () => {
    // Short / non-RGBA arrays can arrive from a buggy producer; a cosmetic
    // surface must degrade rather than crash the transcript.
    const malformed = [[[[1, 2], []] as unknown as ImageCell]]

    expect(() => render({ cells: malformed })).not.toThrow()
  })
})
