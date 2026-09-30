"""The standard chart of accounts an account set is initialized from, as SQL.

Initializing an accounting set builds its subjects from `fms_subject_template` and then its
closing templates and schemes, which name the subjects they post to. Upstream publishes that
reference table outside the MIT repositories — like the business modules' schemas — so a build
made from them alone has an empty chart and cannot initialize: `initialize` fails on the first
closing template whose subject is missing.

This is a **synthetic** chart, not the published one: it follows the small-business standard's
("小企业会计准则") coding and naming, and carries every subject the closing templates and
schemes reference, so an account set initializes and a person can post a voucher. It is not a
statement of what the standard contains.

    python3 subject_chart.py > $YUDAO_CACHE/subject-chart.sql
    docker exec -i woa-yudao-mysql-1 sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" \
        --default-character-set=utf8mb4 ruoyi-vue-pro' < $YUDAO_CACHE/subject-chart.sql

The client's character set matters: without `--default-character-set=utf8mb4` the subjects are
stored double-encoded and every page shows them mojibake. Instead of this second command, the
file can be mounted for a fresh database the way the schema scripts are (compose.yaml).

Re-applying it replaces the rows it owns (ids 900001-909999) and touches no other row, so it is
safe on a database that already holds a chart.
"""
from __future__ import annotations

import sys

#: The ids this chart owns: above anything upstream ships, so one insert can be repeated
#: without overwriting a different chart.
BASE_ID = 900000

ASSET, LIABILITY, EQUITY, COST, PROFIT_LOSS = 1, 2, 3, 4, 5
CURRENT, NON_CURRENT, EQUITY_CATEGORY, COST_CATEGORY = 1, 2, 1, 1
OPERATING_INCOME, OTHER_INCOME, PERIOD_EXPENSE = 1, 2, 3
OTHER_LOSS, OPERATING_COST_AND_TAX, PRIOR_YEAR_ADJUSTMENT, INCOME_TAX = 4, 5, 6, 7
DEBIT, CREDIT = 1, 2

#: (code, name, parent code, type, category, direction, cash) — level follows the parent.
CHART: list[tuple[str, str, str, int, int, int, bool]] = [
    # 资产
    ("1001", "库存现金", "", ASSET, CURRENT, DEBIT, True),
    ("1002", "银行存款", "", ASSET, CURRENT, DEBIT, True),
    ("1012", "其他货币资金", "", ASSET, CURRENT, DEBIT, True),
    ("1101", "短期投资", "", ASSET, CURRENT, DEBIT, False),
    ("1121", "应收票据", "", ASSET, CURRENT, DEBIT, False),
    ("1122", "应收账款", "", ASSET, CURRENT, DEBIT, False),
    ("1123", "预付账款", "", ASSET, CURRENT, DEBIT, False),
    ("1131", "应收股利", "", ASSET, CURRENT, DEBIT, False),
    ("1132", "应收利息", "", ASSET, CURRENT, DEBIT, False),
    ("1221", "其他应收款", "", ASSET, CURRENT, DEBIT, False),
    ("1401", "材料采购", "", ASSET, CURRENT, DEBIT, False),
    ("1402", "在途物资", "", ASSET, CURRENT, DEBIT, False),
    ("1403", "原材料", "", ASSET, CURRENT, DEBIT, False),
    ("1405", "库存商品", "", ASSET, CURRENT, DEBIT, False),
    ("1407", "周转材料", "", ASSET, CURRENT, DEBIT, False),
    ("1408", "委托加工物资", "", ASSET, CURRENT, DEBIT, False),
    ("1471", "存货跌价准备", "", ASSET, CURRENT, CREDIT, False),
    ("1501", "长期债券投资", "", ASSET, NON_CURRENT, DEBIT, False),
    ("1511", "长期股权投资", "", ASSET, NON_CURRENT, DEBIT, False),
    ("1601", "固定资产", "", ASSET, NON_CURRENT, DEBIT, False),
    ("1602", "累计折旧", "", ASSET, NON_CURRENT, CREDIT, False),
    ("1604", "在建工程", "", ASSET, NON_CURRENT, DEBIT, False),
    ("1605", "工程物资", "", ASSET, NON_CURRENT, DEBIT, False),
    ("1606", "固定资产清理", "", ASSET, NON_CURRENT, DEBIT, False),
    ("1701", "无形资产", "", ASSET, NON_CURRENT, DEBIT, False),
    ("1702", "累计摊销", "", ASSET, NON_CURRENT, CREDIT, False),
    ("1801", "长期待摊费用", "", ASSET, NON_CURRENT, DEBIT, False),
    # 负债
    ("2001", "短期借款", "", LIABILITY, CURRENT, CREDIT, False),
    ("2201", "应付票据", "", LIABILITY, CURRENT, CREDIT, False),
    ("2202", "应付账款", "", LIABILITY, CURRENT, CREDIT, False),
    ("2203", "预收账款", "", LIABILITY, CURRENT, CREDIT, False),
    ("2211", "应付职工薪酬", "", LIABILITY, CURRENT, CREDIT, False),
    ("2221", "应交税费", "", LIABILITY, CURRENT, CREDIT, False),
    ("222101", "应交增值税", "2221", LIABILITY, CURRENT, CREDIT, False),
    ("22210101", "进项税额", "222101", LIABILITY, CURRENT, DEBIT, False),
    ("22210104", "转出未交增值税", "222101", LIABILITY, CURRENT, DEBIT, False),
    ("22210107", "销项税额", "222101", LIABILITY, CURRENT, CREDIT, False),
    ("222102", "未交增值税", "2221", LIABILITY, CURRENT, CREDIT, False),
    ("222111", "应交所得税", "2221", LIABILITY, CURRENT, CREDIT, False),
    ("222113", "应交教育费附加", "2221", LIABILITY, CURRENT, CREDIT, False),
    ("222114", "应交地方教育费附加", "2221", LIABILITY, CURRENT, CREDIT, False),
    ("222117", "应交城市维护建设税", "2221", LIABILITY, CURRENT, CREDIT, False),
    ("222121", "应交消费税", "2221", LIABILITY, CURRENT, CREDIT, False),
    ("2231", "应付利息", "", LIABILITY, CURRENT, CREDIT, False),
    ("2232", "应付利润", "", LIABILITY, CURRENT, CREDIT, False),
    ("2241", "其他应付款", "", LIABILITY, CURRENT, CREDIT, False),
    ("2401", "递延收益", "", LIABILITY, NON_CURRENT, CREDIT, False),
    ("2501", "长期借款", "", LIABILITY, NON_CURRENT, CREDIT, False),
    ("2701", "长期应付款", "", LIABILITY, NON_CURRENT, CREDIT, False),
    # 所有者权益
    ("3001", "实收资本", "", EQUITY, EQUITY_CATEGORY, CREDIT, False),
    ("3002", "资本公积", "", EQUITY, EQUITY_CATEGORY, CREDIT, False),
    ("3101", "盈余公积", "", EQUITY, EQUITY_CATEGORY, CREDIT, False),
    ("3103", "本年利润", "", EQUITY, EQUITY_CATEGORY, CREDIT, False),
    ("3104", "利润分配", "", EQUITY, EQUITY_CATEGORY, CREDIT, False),
    ("310415", "未分配利润", "3104", EQUITY, EQUITY_CATEGORY, CREDIT, False),
    # 成本
    ("4001", "生产成本", "", COST, COST_CATEGORY, DEBIT, False),
    ("4101", "制造费用", "", COST, COST_CATEGORY, DEBIT, False),
    ("4301", "研发支出", "", COST, COST_CATEGORY, DEBIT, False),
    ("4401", "工程施工", "", COST, COST_CATEGORY, DEBIT, False),
    # 损益
    ("5001", "主营业务收入", "", PROFIT_LOSS, OPERATING_INCOME, CREDIT, False),
    ("5051", "其他业务收入", "", PROFIT_LOSS, OTHER_INCOME, CREDIT, False),
    ("5111", "投资收益", "", PROFIT_LOSS, OTHER_INCOME, CREDIT, False),
    ("5301", "营业外收入", "", PROFIT_LOSS, OTHER_INCOME, CREDIT, False),
    ("5401", "主营业务成本", "", PROFIT_LOSS, OPERATING_COST_AND_TAX, DEBIT, False),
    ("5402", "其他业务成本", "", PROFIT_LOSS, OPERATING_COST_AND_TAX, DEBIT, False),
    ("5403", "税金及附加", "", PROFIT_LOSS, OPERATING_COST_AND_TAX, DEBIT, False),
    ("540303", "城市维护建设税", "5403", PROFIT_LOSS, OPERATING_COST_AND_TAX, DEBIT, False),
    ("540310", "教育费附加", "5403", PROFIT_LOSS, OPERATING_COST_AND_TAX, DEBIT, False),
    ("540313", "地方教育费附加", "5403", PROFIT_LOSS, OPERATING_COST_AND_TAX, DEBIT, False),
    ("5601", "销售费用", "", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("560101", "电话费", "5601", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("560102", "房租水电费", "5601", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("560107", "差旅费", "5601", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("560110", "工资", "5601", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("5602", "管理费用", "", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("560201", "办公费", "5602", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("560202", "折旧费", "5602", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("560203", "差旅费", "5602", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("560209", "工资", "5602", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("5603", "财务费用", "", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("560301", "利息支出", "5603", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("560302", "利息收入", "5603", PROFIT_LOSS, PERIOD_EXPENSE, CREDIT, False),
    ("560303", "银行手续费", "5603", PROFIT_LOSS, PERIOD_EXPENSE, DEBIT, False),
    ("5711", "营业外支出", "", PROFIT_LOSS, OTHER_LOSS, DEBIT, False),
    ("5801", "所得税费用", "", PROFIT_LOSS, INCOME_TAX, DEBIT, False),
    ("6000", "以前年度损益调整", "", PROFIT_LOSS, PRIOR_YEAR_ADJUSTMENT, DEBIT, False),
]

#: 状态：开启（CommonStatusEnum）
ENABLED = 0

_COLUMNS = ("id", "code", "name", "parent_id", "type", "category", "balance_direction",
            "quantity_unit", "cash", "status", "level", "quantity_accounting", "deleted",
            "create_time", "update_time", "creator", "updater")


def rows() -> list[dict]:
    """Every subject, its level counted from its parent, numbered from BASE_ID."""
    numbered = sorted(CHART, key=lambda row: row[0])
    ids = {code: BASE_ID + index + 1 for index, (code, *_rest) in enumerate(numbered)}
    levels: dict[str, int] = {}
    out = []
    for code, name, parent, kind, category, direction, cash in numbered:
        assert parent == "" or parent in ids, f"{code}: unknown parent {parent}"
        levels[code] = 1 if not parent else levels[parent] + 1
        out.append({"id": ids[code], "code": code, "name": name,
                    "parent_id": ids[parent] if parent else 0, "type": kind,
                    "category": category, "balance_direction": direction, "quantity_unit": None,
                    "cash": int(cash), "status": ENABLED, "level": levels[code],
                    "quantity_accounting": 0, "deleted": 0})
    return out


def _literal(value) -> str:
    return "NULL" if value is None else ("'%s'" % str(value).replace("'", "''") if isinstance(value, str)
                                         else str(value))


def sql() -> str:
    """The chart as one statement that replaces the rows it owns.

    The ids are the generator's own (BASE_ID upwards) and one code's id follows its place in
    the chart, so re-applying an edited chart must replace rather than merge: merging by id
    would leave an edited subject's old code beside its new name.
    """
    body = ",\n  ".join("(" + ", ".join(
        [_literal(row[column]) for column in _COLUMNS[:-4]] + ["NOW()", "NOW()", "'seed'", "'seed'"]) + ")"
        for row in rows())
    return (f"-- Synthetic chart of accounts for the small-business standard ({len(CHART)} subjects).\n"
            f"-- Upstream publishes the real one outside the MIT repositories; see subject_chart.py.\n"
            f"START TRANSACTION;\n"
            f"DELETE FROM fms_subject_template WHERE id BETWEEN {BASE_ID + 1} AND {BASE_ID + 9999};\n"
            f"INSERT INTO fms_subject_template\n  ({', '.join(_COLUMNS)})\nVALUES\n  {body};\n"
            f"COMMIT;\n")


if __name__ == "__main__":
    sys.stdout.write(sql())