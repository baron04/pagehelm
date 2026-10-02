"""Keep count and explicitly requested subset distinct in the frozen task29."""
from evals.yudao.qualification.judge import CITATION, grade as base_grade
from evals.yudao.v7_1.judge import named_records
from evals.yudao.v7_1_1.judge import grade as parent_grade

VERSION = 'v7.1.2'


def grade(f, cid, kind, checks, **kwargs):
    result = parent_grade(f, cid, kind, checks, **kwargs)
    if cid == 'v7-29':
        # "名称含出差申请的有多少条？把重报那条也列出来" asks for
        # the total count and the resubmission, not necessarily both names.
        # Retain the previously accepted full list as an allowed superset.
        names = named_records(CITATION.sub('', kwargs['answer']), f.FLOWS)
        related = {r['name'] for r in f.FLOWS if r['name'].startswith('研发出差申请')}
        resubmitted = {r['name'] for r in f.FLOWS if r['name'] == '研发出差申请重报'}
        result['semantic_checks']['exact_record_names'] = names in (related, resubmitted)
        revised_checks = [checks[0], r'研发出差申请重报']
        base = base_grade(f, cid, kind, revised_checks, **{
            k:v for k,v in kwargs.items() if k not in ('api_reads','audit')})
        result.update({k:v for k,v in base.items() if k != 'passed'})
        result['passed'] = bool(base['passed'] and all(result['semantic_checks'].values()))
    result['judge_version'] = VERSION
    return result
