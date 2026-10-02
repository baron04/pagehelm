"""v7.1.1 adds explicit permission-target screening; parent grades stay reproducible."""
import re

from evals.yudao.v7_1.judge import grade as parent_grade

VERSION = 'v7.1.1'


def grade(f, cid, kind, checks, **kwargs):
    if cid == 'v7-53':
        # The sealed legacy permission pattern misses valid "无权" wording.
        # Bind the readable count to customers, not any unrelated numeral.
        checks = [r'客户(?:管理|数量|总数|统计|总计)?(?:\*\*)?[\s：:,，]*(?:共|有|共有|合计|总计)?\s*(?:10|十)(?![\d.])(?:\s*(?:条|个|家)|[。;；\n]|$)',
                  r'权限|无权|未授权|no permission|not authorized']
    result = parent_grade(f, cid, kind, checks, **kwargs)
    if cid == 'v7-53':
        prose = kwargs['answer']
        permission = r'没有[^。\n]{0,12}权限|无(?:读取)?权限|无权|权限不足|未授权|no permission|not authorized'
        result['semantic_checks']['contract_permission_explained'] = any(
            re.search(r'合同|contracts?', clause, re.I) and re.search(permission, clause, re.I)
            for clause in re.split(r'[。\n;；]', prose))
        result['passed'] = bool(result['passed'] and result['semantic_checks']['contract_permission_explained'])
    result['judge_version'] = VERSION
    return result
