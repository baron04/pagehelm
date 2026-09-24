/**
 * What the stand-in application declares about itself, as a real adapter would on its
 * descriptor. The dev page and the stories both play this one application.
 */
import type {Suggestion} from '../src/contract/panel'

export const SAMPLE_SUGGESTIONS: Suggestion[] = [
  {text: '列出所有待开票的销售订单', kind: '列举', workflow: 'enumerate'},
  {text: 'Acme 有哪些草稿报价单', kind: '条件查询', workflow: 'filter'},
  {text: '把报价单 S00002 确认成销售订单', kind: '写入', workflow: 'write'},
]
