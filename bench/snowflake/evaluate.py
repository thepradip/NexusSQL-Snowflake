"""Opt-in staging evaluation. Never trains on the frozen holdout or claims offline certification."""
import argparse
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sqlas.correctness import result_set_similarity


def normalize(value):
    if isinstance(value,list): return tuple(normalize(v) for v in value)
    try: return Decimal(str(value)).normalize()
    except (InvalidOperation,ValueError): return value


def report(cases, responses):
    answerable = [c for c in cases if c['expected_status']=='answered']
    answered = [(c,r) for c,r in zip(cases,responses) if c['expected_status']=='answered' and r.get('status')=='answered']
    correct = 0
    scores = []
    for case,response in answered:
        rows=response.get('data',{}).get('rows',[])
        # SQLAS uses precomputed results, so scoring never re-executes analytical SQL.
        score,_=result_set_similarity('prediction','gold',execute_fn=lambda name:
            [tuple(row) for row in (rows if name=='prediction' else case['expected_rows'])])
        scores.append(score)
        correct += normalize(rows)==normalize(case['expected_rows']) and not response.get('data',{}).get('truncated',False)
    ambiguous=[(c,r) for c,r in zip(cases,responses) if c['expected_status']=='needs_clarification']
    accuracy=correct/len(answered) if answered else 0
    coverage=len(answered)/len(answerable) if answerable else 0
    handling=sum(r.get('status')=='needs_clarification' for c,r in ambiguous)/len(ambiguous) if ambiguous else 0
    reviewed=all(c.get('adjudication')=='approved' for c in cases)
    return {'answered_accuracy':accuracy,'coverage':coverage,'ambiguity_handling':handling,
        'answered':len(answered),'answerable':len(answerable),'sqlas_result_similarity':sum(scores)/len(scores) if scores else 0,
        'business_adjudicated':reviewed,'correctness_gate':reviewed and accuracy>=.9 and coverage>=.8 and handling>=.95,
        'production_certified':False,'isolation_and_cost_gates':'require separate live acceptance'}

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    manifest=json.loads((Path(__file__).parent/'holdout_v1.json').read_text())
    key=os.environ['NEXUS_EVAL_API_KEY']
    responses=[]
    for case in manifest['cases']:
        conversation=None
        for question in [*case.get('prior_questions',[]),case['question']]:
            request=Request(args.base_url.rstrip('/')+'/query',data=json.dumps({'query':question,'conversation_id':conversation}).encode(),
                headers={'Content-Type':'application/json','X-API-Key':key})
            with urlopen(request,timeout=100) as response: result=json.load(response)
            conversation=result['conversation_id']
        responses.append(result)
    outcome=report(manifest['cases'],responses)
    Path(args.output).write_text(json.dumps({'report':outcome,'responses':responses},indent=2))
    print(json.dumps(outcome,indent=2))
