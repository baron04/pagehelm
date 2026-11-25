/**
 * The confirmation gate judges what a person can see at the instant they click.
 *
 * A tall write card can be taller than its scrollport; a fast honest answer can outrun
 * the observer's records. Both used to keep an approvable card unapprovable: the gate
 * trusted only IntersectionObserver reports, which arrive late and measure against a
 * height the card can never fill. These pin the synchronous coverage check and the
 * dwell floor against mount, and keep covered, hidden and untrusted clicks refused.
 */
import presentation from '../src/presentation.json' with {type: 'json'}
import {expect, labels, snapshot, test} from './harness'

const ROWS = (count: number) => Array.from({length: count}, (_, i) => ({
  kind: 'change' as const,
  label: `明细行 ${i + 1}`,
  text: `物料 M${100 + i} 数量 ${i + 1} → ${i + 2}，单价 12.${i}0 → 13.${i}0`,
}))

test.describe('approval visibility', () => {
  test('an answer before the dwell is refused, said on the card, and can be retried', async ({open}) => {
    const panel = await open(snapshot())
    await panel.raiseApproval({details: ROWS(10)})
    await panel.page.getByTestId('woa-approve').click()
    await expect(panel.page.locator('.woa-approval__blocked')).toContainText(labels.approvalObscured)
    expect(await panel.calls()).toEqual([])
    await panel.page.waitForTimeout(presentation.approval_visibility.min_visible_ms + 150)
    await panel.page.getByTestId('woa-approve').click()
    expect(await panel.calls()).toContainEqual(['decide', 'a-1', 'approved'])
    expect(panel.errors).toEqual([])
  })

  test('a card the observer never reported on still answers after its dwell', async ({open}) => {
    const panel = await open(snapshot())
    // IntersectionObserver defined but dead: the gate must not depend on its records.
    await panel.page.evaluate(() => {
      const Original = window.IntersectionObserver
      window.IntersectionObserver = class extends Original {
        constructor(_callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
          super(() => undefined, options)
        }
      } as typeof IntersectionObserver
    })
    await panel.raiseApproval({details: ROWS(4)})
    await panel.page.waitForTimeout(presentation.approval_visibility.min_visible_ms + 150)
    await panel.page.getByTestId('woa-approve').click()
    expect(await panel.calls()).toContainEqual(['decide', 'a-1', 'approved'])
    expect(panel.errors).toEqual([])
  })

  test('a covered or scrolled-out card is refused at click time even without observer records', async ({open}) => {
    const panel = await open(snapshot())
    await panel.page.evaluate(() => {
      const Original = window.IntersectionObserver
      window.IntersectionObserver = class extends Original {
        constructor(_callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
          super(() => undefined, options)
        }
      } as typeof IntersectionObserver
    })
    await panel.raiseApproval({details: ROWS(10)})
    await panel.page.evaluate(() => {
      const root = (document.querySelector('#woa-assistant') as HTMLElement).shadowRoot!
      const card = root.querySelector('.woa-approval') as HTMLElement
      const thread = root.querySelector('.woa-thread') as HTMLElement
      const spacer = document.createElement('div')
      spacer.style.height = '600px'
      card.after(spacer)
      thread.scrollTop = card.offsetTop + 480
    })
    await panel.page.waitForTimeout(presentation.approval_visibility.min_visible_ms + 150)
    const box = await panel.page.getByTestId('woa-approve').boundingBox()
    expect(box).not.toBeNull()
    await panel.page.mouse.click(box!.x + box!.width / 2, box!.y + box!.height / 2)
    await expect(panel.page.locator('.woa-approval__blocked'))
      .toContainText(labels.approvalObscured)
    expect(await panel.calls()).toEqual([])
    expect(panel.errors).toEqual([])
  })
})
