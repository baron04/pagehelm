import {useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState} from 'react'
import {
  ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Ellipsis,
} from 'lucide-react'
import type {Labels, StructuredTable} from '../contract/panel'
import {parseInline, type Block, type Inline} from '../markdown/parse'
import {inlineText} from '../markdown/plain'
import {iconProps} from './icon'
import {renderStaticInline} from './inline'

type TableBlock = Extract<Block, {type: 'table'}>
type SortState = {col: number; dir: 'asc' | 'desc'} | null
export type PageItem = number | 'ellipsis-prev' | 'ellipsis-next'

const PAGE_SIZE = 10
const PAGE_JUMP = 5
const TOKEN_CELL_RE = /^[A-Za-z0-9_./:+%#-]+$/

type ColumnWrap = 'nowrap' | 'wrap'

/** Estimate visual width in half-width character units (CJK ~ 2, ASCII ~ 1). */
function displayUnits(text: string): number {
  let units = 0
  for (const ch of text) {
    units += ch.charCodeAt(0) > 0x7f ? 2 : 1
  }
  return units
}

/** Build a compact numeric page list with ellipsis for large page counts. */
function buildPageItems(current: number, total: number): PageItem[] {
  if (total <= 7) {
    return Array.from({length: total}, (_, index) => index + 1)
  }
  if (current <= 4) {
    return [1, 2, 3, 4, 5, 'ellipsis-next', total]
  }
  if (current >= total - 3) {
    return [1, 'ellipsis-prev', total - 4, total - 3, total - 2, total - 1, total]
  }
  return [1, 'ellipsis-prev', current - 1, current, current + 1, 'ellipsis-next', total]
}

interface IndexedRow {
  index: number
  cells: Inline[][]
  text: string[]
  numeric: (number | null)[]
}

/** Parse numbers formatted with optional currency symbols, commas, or trailing %. */
export function parseNumericCell(raw: string): number | null {
  const trimmed = raw.trim()
  if (!trimmed) return null
  const withoutUnit = trimmed.replace(/^[¥￥$€£]\s*/, '').replace(/\s*%$/, '')
  const normalized = withoutUnit.replace(/^([+-]?)\s*[¥￥$€£]\s*/, '$1')
  if (!/^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$/.test(normalized)) {
    return null
  }
  const value = Number(normalized.replace(/,/g, ''))
  return Number.isFinite(value) ? value : null
}

/** Convert a server-projected StructuredTable into a DataTable block using inline text parsing. */
export function structuredTableToBlock(table: StructuredTable): TableBlock {
  return {
    type: 'table',
    head: table.columns.map(column => parseInline(String(column ?? ''))),
    rows: table.rows.map(row =>
      table.columns.map((_, colIndex) => parseInline(String(row[colIndex] ?? ''))),
    ),
    align: table.columns.map(() => 'left'),
  }
}

export interface DataTableProps {
  block: TableBlock
  labels: Labels
  allow: (href: string) => boolean
  totalRows?: number | undefined
}

export function DataTable({block, labels, allow, totalRows}: DataTableProps): React.JSX.Element {
  const [sort, setSort] = useState<SortState>(null)
  const [page, setPage] = useState(1)
  const [canScrollRight, setCanScrollRight] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  const columnCount = useMemo(
    () => Math.max(block.head.length, ...block.rows.map(row => row.length), 0),
    [block],
  )

  const headCells = useMemo(
    () => Array.from({length: columnCount}, (_, col) => block.head[col] ?? []),
    [block.head, columnCount],
  )

  const indexedRows = useMemo<IndexedRow[]>(() => block.rows.map((row, index) => {
    const cells = Array.from({length: columnCount}, (_, col) => row[col] ?? [])
    const text = cells.map(cell => inlineText(cell))
    const numeric = text.map(parseNumericCell)
    return {index, cells, text, numeric}
  }), [block.rows, columnCount])

  const columnNumeric = useMemo(() => Array.from({length: columnCount}, (_, col) => {
    if (block.align[col] === 'right') return true
    let sawNumeric = false
    for (const row of indexedRows) {
      const value = (row.text[col] ?? '').trim()
      if (!value || value === '-' || value === '—') continue
      if (row.numeric[col] === null) return false
      sawNumeric = true
    }
    return sawNumeric
  }), [block.align, columnCount, indexedRows])

  const columnWrap = useMemo<ColumnWrap[]>(() => Array.from({length: columnCount}, (_, col) => {
    if (columnNumeric[col]) return 'nowrap'
    let sawLongText = false
    for (const row of indexedRows) {
      const value = (row.text[col] ?? '').trim()
      if (!value) continue
      if (value.length <= 24 && TOKEN_CELL_RE.test(value)) continue
      if (displayUnits(value) > 10) {
        sawLongText = true
        break
      }
    }
    return sawLongText ? 'wrap' : 'nowrap'
  }), [columnCount, columnNumeric, indexedRows])

  const paginated = block.rows.length > PAGE_SIZE

  const sorted = useMemo(() => {
    if (!sort) return indexedRows
    const {col, dir} = sort
    const factor = dir === 'asc' ? 1 : -1
    const copy = [...indexedRows]
    copy.sort((a, b) => {
      const aText = (a.text[col] ?? '').trim()
      const bText = (b.text[col] ?? '').trim()
      if (!aText && !bText) return a.index - b.index
      if (!aText) return 1
      if (!bText) return -1
      const aNum = a.numeric[col] ?? null
      const bNum = b.numeric[col] ?? null
      let cmp = 0
      if (aNum !== null && bNum !== null) {
        cmp = aNum - bNum
      } else {
        cmp = aText.localeCompare(bText, labels.locale || undefined, {numeric: true, sensitivity: 'base'})
      }
      return cmp !== 0 ? cmp * factor : a.index - b.index
    })
    return copy
  }, [indexedRows, labels.locale, sort])

  const totalPages = paginated ? Math.max(1, Math.ceil(sorted.length / PAGE_SIZE)) : 1
  const safePage = Math.min(page, totalPages)

  const visibleRows = useMemo(() => {
    if (!paginated) return sorted
    const start = (safePage - 1) * PAGE_SIZE
    return sorted.slice(start, start + PAGE_SIZE)
  }, [paginated, safePage, sorted])

  const pageItems = useMemo(
    () => (totalPages > 1 ? buildPageItems(safePage, totalPages) : []),
    [safePage, totalPages],
  )

  const syncScrollEdge = useCallback(() => {
    const node = wrapRef.current
    if (!node) return
    const right = node.scrollLeft + node.clientWidth < node.scrollWidth - 1
    setCanScrollRight(prev => (prev === right ? prev : right))
  }, [])

  useEffect(() => {
    setPage(1)
    setSort(null)
  }, [block])

  useEffect(() => {
    const node = wrapRef.current
    if (!node || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(syncScrollEdge)
    observer.observe(node)
    return () => observer.disconnect()
  }, [syncScrollEdge])

  useLayoutEffect(() => {
    syncScrollEdge()
  }, [syncScrollEdge, visibleRows, columnCount])

  const goToPage = (next: number) => {
    setPage(next)
    if (wrapRef.current) wrapRef.current.scrollTop = 0
  }

  const cycleSort = (col: number) => {
    setSort(prev => {
      if (!prev || prev.col !== col) return {col, dir: 'asc'}
      if (prev.dir === 'asc') return {col, dir: 'desc'}
      return null
    })
    goToPage(1)
  }

  return (
    <div className="woa-datatable">
      <div
        className="woa-datatable__frame"
        data-scroll-right={canScrollRight ? 'true' : undefined}
      >
        <div ref={wrapRef} className="woa-md__tablewrap" onScroll={syncScrollEdge}>
          <table className="woa-datatable__table">
            <thead>
              <tr>
                {headCells.map((cell, col) => {
                  const active = sort?.col === col ? sort.dir : null
                  const ariaSort = active === 'asc' ? 'ascending' : active === 'desc' ? 'descending' : 'none'
                  const align = columnNumeric[col] ? 'right' : 'left'
                  return (
                    <th key={col} scope="col" aria-sort={ariaSort} data-align={align}>
                      <button
                        type="button"
                        className="woa-datatable__sort"
                        data-sort={active ?? 'none'}
                        onClick={() => cycleSort(col)}
                      >
                        <span>{renderStaticInline(cell, allow)}</span>
                        {active === 'asc'
                          ? <ArrowUp {...iconProps(12)} aria-hidden="true" />
                          : active === 'desc'
                            ? <ArrowDown {...iconProps(12)} aria-hidden="true" />
                            : <ArrowUpDown {...iconProps(12)} aria-hidden="true" />}
                      </button>
                    </th>
                  )
                })}
              </tr>
            </thead>
            <tbody>
              {visibleRows.length === 0 ? (
                <tr>
                  <td colSpan={Math.max(1, columnCount)} className="woa-datatable__empty">
                    {labels.tableNoMatch}
                  </td>
                </tr>
              ) : (
                visibleRows.map((row, slotIndex) => (
                  <tr key={slotIndex}>
                    {row.cells.map((cell, col) => {
                      const isRight = block.align[col] === 'right' || row.numeric[col] !== null
                      const wrap = isRight ? 'nowrap' : columnWrap[col]
                      return (
                        <td
                          key={col}
                          data-align={isRight ? 'right' : 'left'}
                          data-wrap={wrap}
                        >
                          {renderStaticInline(cell, allow)}
                        </td>
                      )
                    })}
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {paginated && (
        <div className="woa-datatable__footer">
          <span className="woa-datatable__count">
            {labels.tableTotalRows.replace('{count}', String(Math.max(indexedRows.length, totalRows ?? 0)))}
          </span>
          <nav className="woa-datatable__pager" aria-label={labels.tablePagination}>
            <button
              type="button"
              className="woa-datatable__pagebtn woa-datatable__pagebtn--nav"
              aria-label={labels.tablePrevPage}
              disabled={safePage <= 1}
              onClick={() => goToPage(Math.max(1, safePage - 1))}
            >
              <ChevronLeft {...iconProps(12)} aria-hidden="true" />
            </button>
            {pageItems.map(item => {
              if (item === 'ellipsis-prev' || item === 'ellipsis-next') {
                const isPrev = item === 'ellipsis-prev'
                const label = (isPrev ? labels.tableJumpPrev : labels.tableJumpNext)
                  .replace('{count}', String(PAGE_JUMP))
                const target = isPrev
                  ? Math.max(1, safePage - PAGE_JUMP)
                  : Math.min(totalPages, safePage + PAGE_JUMP)
                return (
                  <button
                    key={item}
                    type="button"
                    className="woa-datatable__pagebtn woa-datatable__pagebtn--nav woa-datatable__ellipsis"
                    aria-label={label}
                    title={label}
                    onClick={() => goToPage(target)}
                  >
                    <Ellipsis {...iconProps(12)} className="woa-datatable__dots" aria-hidden="true" />
                    {isPrev
                      ? <ChevronsLeft {...iconProps(12)} className="woa-datatable__jumpicon" aria-hidden="true" />
                      : <ChevronsRight {...iconProps(12)} className="woa-datatable__jumpicon" aria-hidden="true" />}
                  </button>
                )
              }
              return (
                <button
                  key={item}
                  type="button"
                  className="woa-datatable__pagebtn"
                  data-active={item === safePage ? '' : undefined}
                  aria-current={item === safePage ? 'page' : undefined}
                  onClick={() => goToPage(item)}
                >
                  {item}
                </button>
              )
            })}
            <button
              type="button"
              className="woa-datatable__pagebtn woa-datatable__pagebtn--nav"
              aria-label={labels.tableNextPage}
              disabled={safePage >= totalPages}
              onClick={() => goToPage(Math.min(totalPages, safePage + 1))}
            >
              <ChevronRight {...iconProps(12)} aria-hidden="true" />
            </button>
          </nav>
        </div>
      )}
    </div>
  )
}
