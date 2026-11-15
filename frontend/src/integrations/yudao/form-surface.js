/** Yudao/Element Plus form surface: the read sources the application vouches for and its
 * own confirmation and submit-busy indicators. These conventions belong to the
 * application; the core Runtime holds no default for them. */

const FORM_SOURCE = /^\/admin-api\/(?:[A-Za-z0-9_-]+\/)+(?:get|page|list)(?:-[a-z0-9-]+)?$/
const ROW_SOURCE = /^\/admin-api\/(?:[A-Za-z0-9_-]+\/)+(?:page|list)(?:-[a-z0-9-]+)?$/

/** Whether this application produced `source` from one of its own admin-api reads:
 * a form's single record or page list, a row only ever a list. */
export function acceptReadSource(source, kind) {
  return typeof source === 'string' &&
    (kind === 'row' ? ROW_SOURCE : FORM_SOURCE).test(source)
}

const BOX = '.el-message-box'

const shown = node => {
  const rect = node.getBoundingClientRect()
  return rect.width > 0 && rect.height > 0
}

/** The open Element Plus message box, if one is on the page. Raw read; the Runtime
 * bounds the text like any other page fact. */
export function readConfirmBox(doc = document) {
  const box = Array.from(doc.querySelectorAll(BOX)).find(shown)
  if (!box) return {open: false}
  return {open: true,
    title: (box.querySelector('.el-message-box__title')?.textContent ?? '').trim(),
    text: (box.querySelector('.el-message-box__message')?.textContent ?? '').trim(),
    buttons: Array.from(box.querySelectorAll('.el-message-box__btns button'))
      .map(button => (button.textContent ?? '').trim()).filter(Boolean)}
}

/** Press one button of the open message box by its exact label; false if not offered. */
export function pressConfirmButton(label, doc = document) {
  const box = Array.from(doc.querySelectorAll(BOX)).find(shown)
  const matches = Array.from(box?.querySelectorAll('.el-message-box__btns button') ?? [])
    .filter(button => (button.textContent ?? '').replace(/\s+/g, ' ').trim() === label && !button.disabled)
  if (matches.length !== 1) return false
  matches[0].click()
  return true
}

/** Element Plus's own submit-in-flight indicator. */
export function readSubmitBusy(doc = document) {
  return Array.from(doc.querySelectorAll('.el-loading-mask')).some(shown)
}
