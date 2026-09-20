import {describe, expect, it} from 'vitest'
import {sseBatches} from '../src/runtime/transport'

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
}

async function frames(chunks: string[]) {
  const out: {id: string; data: string}[] = []
  for await (const batch of sseBatches(streamOf(chunks))) out.push(...batch)
  return out
}

describe('sseBatches follows the spec last-event-ID buffer', () => {
  it('inherits the id of the last frame that carried one', async () => {
    const got = await frames(['id: 7\ndata: a\n\n', 'data: b\n\n'])
    expect(got).toEqual([{id: '7', data: 'a'}, {id: '7', data: 'b'}])
  })

  it('carries the buffer across chunk boundaries, not just frames', async () => {
    const got = await frames(['id: 9\ndata: a\n', '\ndata: b\n\n'])
    expect(got).toEqual([{id: '9', data: 'a'}, {id: '9', data: 'b'}])
  })

  it('an empty id line resets the buffer rather than inheriting', async () => {
    const got = await frames(['id: 4\ndata: a\n\n', 'id:\ndata: b\n\n', 'data: c\n\n'])
    expect(got).toEqual([{id: '4', data: 'a'}, {id: '', data: 'b'}, {id: '', data: 'c'}])
  })

  it('a frame with only an id still moves the buffer for the frames after it', async () => {
    const got = await frames(['id: 5\n\n', 'data: a\n\n'])
    expect(got).toEqual([{id: '5', data: 'a'}])
  })

  it('a stream that never sends an id yields empty ids, not stale ones', async () => {
    const got = await frames(['data: a\n\n', 'data: b\n\n'])
    expect(got).toEqual([{id: '', data: 'a'}, {id: '', data: 'b'}])
  })
})
