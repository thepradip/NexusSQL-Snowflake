import importlib.util
from pathlib import Path
import sys
import sqlglot
ROOT=Path(__file__).resolve().parents[1]

def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'bench'/'snowflake'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_fixture_is_reproducible_and_holdout_has_200_questions():
    import json
    builder=load('build_fixture'); sql,manifest=builder.build()
    assert sql==(ROOT/'bench/snowflake/fixture_v1.sql').read_text()
    assert manifest==json.loads((ROOT/'bench/snowflake/holdout_v1.json').read_text())
    assert len(manifest['cases'])==200 and len({c['id'] for c in manifest['cases']})==200
    assert {c['category'] for c in manifest['cases']} >= {'metrics','join','fanout','dates','fiscal','window','nulls','semi_structured','ambiguity','follow_up','bridge'}
    for case in manifest['cases']:
        if 'reference_sql' in case: assert sqlglot.parse_one(case['reference_sql'],read='snowflake')


def test_abstention_cannot_inflate_accuracy_or_certification():
    evaluator=load('evaluate')
    cases=[{'expected_status':'answered','expected_rows':[[12]],'adjudication':'pending_business_review'},
           {'expected_status':'answered','expected_rows':[[13]],'adjudication':'approved'},
           {'expected_status':'needs_clarification','adjudication':'approved'}]
    out=evaluator.report(cases,[{'status':'answered','data':{'rows':[[12]]}}, {'status':'blocked'}, {'status':'needs_clarification'}])
    assert out['answered_accuracy']==1 and out['coverage']==.5 and out['ambiguity_handling']==1
    assert not out['correctness_gate'] and not out['production_certified']


def test_synthetic_reference_results_match_independent_expected_values():
    """Cross-dialect sanity check, not live Snowflake acceptance."""
    import json
    import sqlite3
    from sqlglot import exp
    conn=sqlite3.connect(':memory:')
    for statement in sqlglot.parse((ROOT/'bench/snowflake/fixture_v1.sql').read_text(),read='snowflake'):
        if statement is not None: conn.execute(statement.sql(dialect='sqlite'))
    manifest=json.loads((ROOT/'bench/snowflake/holdout_v1.json').read_text())
    for case in manifest['cases']:
        if 'reference_sql' not in case: continue
        tree=sqlglot.parse_one(case['reference_sql'],read='snowflake')
        # Snowflake VARIANT scalar cast strips JSON quotes; SQLite JSONExtractScalar
        # supplies that behavior for this offline fixture check only.
        def variant_scalar(node):
            if isinstance(node,exp.JSONExtract): return exp.JSONExtractScalar(this=node.this,expression=node.expression)
            return node
        sql=tree.transform(variant_scalar).sql(dialect='sqlite')
        rows=[list(row) for row in conn.execute(sql)]
        assert rows==case['expected_rows'],case['id']
    conn.close()
