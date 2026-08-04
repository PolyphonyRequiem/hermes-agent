import { Box, Text } from '@hermes/ink'
import React, { memo } from 'react'

import type { Theme } from '../theme.js'

/** One cell: [top, bottom] RGBA pixel pair. */
export type ImageCell = [number[], number[]]

interface ImageCanvasProps {
  caption?: string
  cells: ImageCell[][]
  t: Theme
  title?: string
}

const UPPER_HALF = '\u2580'
const ALPHA_FLOOR = 8

const rgb = (px: number[] | undefined) =>
  px && px.length >= 3 ? `#${px.slice(0, 3).map((v) => Math.max(0, Math.min(255, v | 0)).toString(16).padStart(2, '0')).join('')}` : undefined

const visible = (px: number[] | undefined) => Boolean(px && (px[3] ?? 255) >= ALPHA_FLOOR)

/**
 * Renders a half-block image thumbnail inside an Ink layout.
 *
 * Each glyph is U+2580 (upper half block): the foreground paints the TOP pixel
 * and the background paints the BOTTOM one, so a single text row carries two
 * rows of image data at full terminal width accounting. This is why the
 * payload ships cell data rather than a kitty/sixel escape — Ink must be able
 * to measure what it draws.
 *
 * A fully transparent cell renders as a space so the surrounding transcript
 * background shows through instead of a black hole.
 */
export const ImageCanvas = memo(function ImageCanvas({ caption, cells, t, title }: ImageCanvasProps) {
  if (!cells?.length) {
    return null
  }

  return (
    <Box borderColor={t.color.border} borderStyle="round" flexDirection="column" paddingX={1}>
      {title ? (
        <Text bold color={t.color.primary}>
          {title}
        </Text>
      ) : null}

      {cells.map((row, y) => (
        <Text key={y}>
          {row.map(([top, bottom], x) => {
            const showTop = visible(top)
            const showBottom = visible(bottom)

            if (!showTop && !showBottom) {
              return <Text key={x}> </Text>
            }

            return (
              <Text backgroundColor={showBottom ? rgb(bottom) : undefined} color={showTop ? rgb(top) : undefined} key={x}>
                {showTop ? UPPER_HALF : ' '}
              </Text>
            )
          })}
        </Text>
      ))}

      {caption ? <Text color={t.color.muted}>{caption}</Text> : null}
    </Box>
  )
})
