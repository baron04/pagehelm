"""Three-model, non-thinking comparison with a frozen, difficulty-only subset.

Each lane cycles model order in five-question blocks. Every model sees the
same lane/case order, fixture, product executor and token ceilings. Provider
load and caches are observed, not controlled. Explicit env arguments are the
authorization boundary; this module is never called by ordinary tests.
"""
import argparse
import json
from pathlib import Path

from .run import main as run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qwen-env',type=Path,required=True)
    parser.add_argument('--deepseek-env',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--lane',default='1/1')
    args=parser.parse_args()
    ids=json.loads((Path(__file__).parents[1]/'v7/comparison_ids.json').read_text())['ids']
    arms=[('qwen3.8-flash',args.qwen_env),('deepseek-flash',args.deepseek_env),('deepseek-v4-pro',args.deepseek_env)]
    for block,start in enumerate(range(0,len(ids),5)):
        cases=ids[start:start+5]
        # Rotating small blocks reduces the systematic first/last-arm bias.
        for model,env in arms[block%3:]+arms[:block%3]:
            print(json.dumps({'block':block,'model':model,'lane':args.lane,'cases':cases}),flush=True)
            argv=['--model-env',str(env),'--model',model,'--profile','fast',
                  '--output',str(args.output/model),'--lane',args.lane,'--executor','hybrid']
            for case in cases:argv+=['--case',case]
            run(argv)


if __name__ == '__main__':
    main()
