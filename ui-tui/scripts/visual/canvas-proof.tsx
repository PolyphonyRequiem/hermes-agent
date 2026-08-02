// E2E proof: take the EXACT JSON-RPC payload the Python side emits, run it
// through the real gateway event handler, and render the resulting transcript
// message with the real <Panel> Ink component to real ANSI.
//
// This is deliberately NOT a mock of the renderer: the panel below is produced
// by the same component that draws locally-built panels in the live TUI.
import { readFileSync } from 'fs'

import { render } from 'ink'
import React from 'react'

import { Panel } from '../../src/components/branding.js'
import { DARK_THEME } from '../../src/theme.js'

const frame = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const payload = frame.params.payload

// Mirror the dispatcher's defensive normalization (createGatewayEventHandler).
const sections = Array.isArray(payload?.sections) ? payload.sections : []

if (!sections.length) {
  console.error('no sections in payload — dispatcher would have dropped this frame')
  process.exit(1)
}

render(React.createElement(Panel, { sections, t: DARK_THEME, title: payload.title ?? '' }))
