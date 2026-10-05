import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from tests.test_trusted_execution import context, SCHEMA

@pytest.mark.parametrize('condition',['o.id=x.id OR TRUE','NOT(o.id=x.id)','o.id=x.id OR o.id<>x.id'])
def test_reviewed_key_cannot_hide_inside_a_cross_product(condition):
    from trusted_validator import validate,ValidationError
    schema={**SCHEMA,'ANALYTICS.PUBLIC.OTHER':SCHEMA['ANALYTICS.PUBLIC.ORDERS']}
    ctx=context(allowed_objects=frozenset(schema))
    joins=[{'keys':['ANALYTICS.PUBLIC.ORDERS.ID','ANALYTICS.PUBLIC.OTHER.ID'],'cardinality':'one_to_one'}]
    with pytest.raises(ValidationError):
        validate('SELECT o.id FROM orders o JOIN other x ON '+condition,ctx,schema,approved_joins=joins)


def test_reviewed_template_can_preserve_preaggregation_bridge_keys():
    from trusted_validator import validate
    schema={**SCHEMA,'ANALYTICS.PUBLIC.ITEMS':{'columns':[{'name':'ORDER_ID','type':'INT'}]}}
    ctx=context(allowed_objects=frozenset(schema))
    sql='WITH i AS (SELECT ORDER_ID FROM ITEMS GROUP BY ORDER_ID) SELECT SUM(o.AMOUNT) FROM orders o JOIN i ON o.ID=i.ORDER_ID'
    joins=[{'keys':['ANALYTICS.PUBLIC.ORDERS.ID','ANALYTICS.PUBLIC.ITEMS.ORDER_ID'],'cardinality':'one_to_many'}]
    assert validate(sql,ctx,schema,approved_joins=joins,reviewed_template=True)['joins_reviewed']
