/**
 * What the Odoo addon's page declares for a new conversation (`assistant_entry.xml`), for
 * the dev page, the stories and the e2e harness, which play that page. Development only.
 */
import type {PanelExample} from '../src/bridge/host'

export const SAMPLE_EXAMPLES: PanelExample[] = [
  {kind: 'query', text: '列出所有待开票的销售订单'},
  {kind: 'find', text: '在哪里新建报价单'},
  {kind: 'operate', text: '打开待开票的销售订单列表'},
]
