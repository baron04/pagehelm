import {describe, expect, it} from 'vitest'
import {
  listboxCombobox, ownedTree, selectedTreeOption, treeItemChoices, treeItemPath,
} from '../src/runtime/combobox'

describe('tree-backed combobox helpers (ElTreeSelect / ARIA tree)', () => {
  it('recognizes a tree combobox and builds hierarchical Parent / Child paths', () => {
    document.body.innerHTML = `
      <div class="el-select__wrapper">
        <input id="dept-combo" role="combobox" aria-haspopup="tree" aria-controls="dept-pop" />
      </div>
      <div id="dept-pop">
        <div role="tree">
          <div role="treeitem" class="el-tree-node" aria-expanded="true">
            <div class="el-tree-node__content"><span class="el-tree-node__label">总公司</span></div>
            <div role="group">
              <div role="treeitem" class="el-tree-node is-current" aria-selected="true">
                <div class="el-tree-node__content"><span class="el-tree-node__label">研发部</span></div>
              </div>
              <div role="treeitem" class="el-tree-node">
                <div class="el-tree-node__content"><span class="el-tree-node__label">财务部</span></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `
    const input = document.getElementById('dept-combo') as HTMLInputElement
    expect(listboxCombobox(input)).toBe(true)
    const tree = ownedTree(input)
    expect(tree).not.toBeNull()
    const choices = treeItemChoices(tree!)
    expect(choices.map(c => c.path)).toEqual(['总公司', '总公司 / 研发部', '总公司 / 财务部'])
    expect(treeItemPath(tree!, choices[1]!.item)).toBe('总公司 / 研发部')
    expect(selectedTreeOption(input)).toBe('研发部')
  })

  it('disambiguates duplicate leaf names using full Parent / Child path', () => {
    document.body.innerHTML = `
      <input id="cat-combo" role="combobox" aria-haspopup="listbox" aria-controls="cat-pop" />
      <div id="cat-pop">
        <div role="tree">
          <div role="treeitem">
            <div class="el-tree-node__content"><span class="el-tree-node__label">华东区</span></div>
            <div role="group">
              <div role="treeitem" class="el-tree-node is-current">
                <div class="el-tree-node__content"><span class="el-tree-node__label">综合部</span></div>
              </div>
            </div>
          </div>
          <div role="treeitem">
            <div class="el-tree-node__content"><span class="el-tree-node__label">华南区</span></div>
            <div role="group">
              <div role="treeitem" class="el-tree-node">
                <div class="el-tree-node__content"><span class="el-tree-node__label">综合部</span></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `
    const input = document.getElementById('cat-combo') as HTMLInputElement
    expect(selectedTreeOption(input)).toBe('华东区 / 综合部')
  })
})
