import {useMemo, useState} from 'react'
import {ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight, Download, Search} from 'lucide-react'
import type {Labels, StructuredTable} from '../contract/panel'
import {parseInline, type Block, type Inline} from '../markdown/parse'
import {inlineText} from '../markdown/plain'
import {CopyButton} from './CopyButton'
import {iconProps} from './icon'
import {renderStaticInline} from './inline'

type TableBlock = Extract<Block, {type: 'table'}>
type PageSize = 10 | 25 | 50 | 'all'
type SortState = {col: number; dir: 'asc' | 'desc'} | null

const PAGE_SIZES: readonly (10 | 25 | 50)[] = [10, 25, 50]
const PAGINATION_THRESHOLD = 10

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

/** Escape a single cell per RFC 4180 CSV rules. */
export function escapeCsvCell(value: string): string {
  if (/[",\r\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`
  }
  return value
}

/** Format header + rows as UTF-8 BOM + RFC 4180 CSV (CRLF line endings). */
export function formatCsv(head: string[], rows: string[][]): string {
  const lines = [head, ...rows].map(row => row.map(escapeCsvCell).join(','))
  return `\uFEFF${lines.join('\r\n')}`
}

/** Format header + rows as tab-separated values suitable for spreadsheet paste. */
export function formatTsv(head: string[], rows: string[][]): string {
  const clean = (cell: string) => cell.replace(/[\t\r\n]+/g, ' ')
  return [head, ...rows].map(row => row.map(clean).join('\t')).join('\n')
}

function downloadCsv(csv: string, filename = 'table.csv'): void {
  const blob = new Blob([csv], {type: 'text/csv;charset=utf-8'})
  if (typeof URL.createObjectURL !== 'function') return
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.style.display = 'none'
  document.body.appendChild(link)
  try {
    link.click()
  } finally {
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL?.(url), 1000)
  }
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
}

export function DataTable({block, labels, allow}: DataTableProps): React.JSX.Element {
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<SortState>(null)
  const [pageSize, setPageSize] = useState<PageSize>(10)
  const [page, setPage] = useState(1)

  const columnCount = useMemo(
    () => Math.max(block.head.length, ...block.rows.map(row => row.length), 0),
    [block],
  )

  const headCells = useMemo(
    () => Array.from({length: columnCount}, (_, col) => block.head[col] ?? []),
    [block.head, columnCount],
  )

  const headText = useMemo(
    () => headCells.map(cell => inlineText(cell)),
    [headCells],
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

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return indexedRows
    return indexedRows.filter(row => row.text.some(cell => cell.toLowerCase().includes(needle)))
  }, [indexedRows, query])

  const sorted = useMemo(() => {
    if (!sort) return filtered
    const {col, dir} = sort
    const factor = dir === 'asc' ? 1 : -1
    const copy = [...filtered]
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
  }, [filtered, labels.locale, sort])

  const paginated = block.rows.length > PAGINATION_THRESHOLD
  const totalPages = paginated && pageSize !== 'all'
    ? Math.max(1, Math.ceil(sorted.length / pageSize))
    : 1
  const safePage = Math.min(page, totalPages)

  const visibleRows = useMemo(() => {
    if (!paginated || pageSize === 'all') return sorted
    const start = (safePage - 1) * pageSize
    return sorted.slice(start, start + pageSize)
  }, [pageSize, paginated, safePage, sorted])

  const cycleSort = (col: number) => {
    setSort(prev => {
      if (!prev || prev.col !== col) return {col, dir: 'asc'}
      if (prev.dir === 'asc') return {col, dir: 'desc'}
      return null
    })
    setPage(1)
  }

  return (
    <div className="woa-datatable">
      <div className="woa-datatable__toolbar">
        <label className="woa-datatable__filter">
          <Search {...iconProps(12)} aria-hidden="true" />
          <input
            type="search"
            value={query}
            placeholder={labels.tableFilterPlaceholder}
            aria-label={labels.tableFilterPlaceholder}
            onChange={event => {
              setQuery(event.target.value)
              setPage(1)
            }}
          />
        </label>
        <div className="woa-datatable__actions">
          <CopyButton
            text={() => formatTsv(headText, sorted.map(row => row.text))}
            label={labels.tableCopyTsv}
            copiedLabel={labels.copied}
            className="woa-datatable__action"
            showLabel
          />
          <button
            type="button"
            className="woa-datatable__action"
            onClick={() => downloadCsv(formatCsv(headText, sorted.map(row => row.text)))}
          >
            <Download {...iconProps(12)} />
            <span>{labels.tableDownloadCsv}</span>
          </button>
        </div>
      </div>

      <div className="woa-md__tablewrap">
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
              visibleRows.map(row => (
                <tr key={row.index}>
                  {row.cells.map((cell, col) => {
                    const isRight = block.align[col] === 'right' || row.numeric[col] !== null
                    return (
                      <td key={col} data-align={isRight ? 'right' : 'left'}>
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

      {paginated && (
        <div className="woa-datatable__footer">
          <label className="woa-datatable__size">
            <span>{labels.tablePageSize}</span>
            <select
              aria-label={labels.tablePageSize}
              value={String(pageSize)}
              onChange={event => {
                const raw = event.target.value
                setPageSize(raw === 'all' ? 'all' : (Number(raw) as 10 | 25 | 50))
                setPage(1)
              }}
            >
              {PAGE_SIZES.map(size => (
                <option key={size} value={String(size)}>{size}</option>
              ))}
              <option value="all">{labels.tableShowAll}</option>
            </select>
          </label>
          <div className="woa-datatable__pager">
            <button
              type="button"
              className="woa-datatable__pagebtn"
              disabled={pageSize === 'all' || safePage <= 1}
              onClick={() => setPage(current => Math.max(1, current - 1))}
            >
              <ChevronLeft {...iconProps(12)} aria-hidden="true" />
              <span>{labels.tablePrevPage}</span>
            </button>
            <span className="woa-datatable__pageinfo">
              {pageSize === 'all' ? labels.tableShowAll : `${safePage} / ${totalPages}`}
            </span>
            <button
              type="button"
              className="woa-datatable__pagebtn"
              disabled={pageSize === 'all' || safePage >= totalPages}
              onClick={() => setPage(current => Math.min(totalPages, current + 1))}
            >
              <span>{labels.tableNextPage}</span>
              <ChevronRight {...iconProps(12)} aria-hidden="true" />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
