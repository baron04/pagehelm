/**
 * Starter examples for the dev page, Storybook and e2e harness, aligned with
 * `examples/yudao/deploy/cloud/yudao-loader.js`. Development only.
 */
import type {PanelExample} from '../src/bridge/host'

export const SAMPLE_EXAMPLES: PanelExample[] = [
  {kind: 'query', text: 'CRM 联系人一共有多少个？'},
  {kind: 'operate', text: '打开合同管理页面'},
  {kind: 'edit', text: '新建一个 CRM 客户，名称叫星辰科技'},
]
