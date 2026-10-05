"""Reproducible synthetic holdout. Reference expectations are hand-derived; review is pending."""
from pathlib import Path
import json

ROOT = Path(__file__).parent

def build():
    sql = ['-- Fixture v1: run only in a dedicated staging database/schema as administrator.',
        '-- Provision roles, row access/masking policies, and warehouse controls separately.',
        'CREATE TABLE IF NOT EXISTS ORDERS (TENANT_ID VARCHAR, COHORT INTEGER, ID INTEGER, CUSTOMER_ID INTEGER, AMOUNT NUMBER(12,2), DISCOUNT NUMBER(12,2), BOOKED_AT DATE);',
        'CREATE TABLE IF NOT EXISTS PAYMENTS (TENANT_ID VARCHAR, COHORT INTEGER, ORDER_ID INTEGER, AMOUNT NUMBER(12,2));',
        'CREATE TABLE IF NOT EXISTS ORDER_ITEMS (TENANT_ID VARCHAR, COHORT INTEGER, ORDER_ID INTEGER, LINE_ID INTEGER);',
        'CREATE TABLE IF NOT EXISTS EVENTS (TENANT_ID VARCHAR, COHORT INTEGER, PAYLOAD VARIANT);',
        'CREATE TABLE IF NOT EXISTS CUSTOMER_SEGMENTS (TENANT_ID VARCHAR, COHORT INTEGER, CUSTOMER_ID INTEGER, SEGMENT VARCHAR);',
        '-- Do not rerun inserts into an existing populated fixture. Use a new versioned schema.']
    cases = []
    for cohort in range(1,11):
        for tenant in ('acme','beta'):
            # Beta has deliberately different totals to detect isolation leaks.
            bump = 0 if tenant == 'acme' else 10000
            for identifier, customer, amount, discount, booked in [(1,1,100+cohort,'NULL','2025-01-01'),(2,2,200+cohort,'10','2025-01-31'),(3,1,50+cohort,'NULL','2025-02-01')]:
                sql.append(f"INSERT INTO ORDERS VALUES ('{tenant}',{cohort},{identifier},{customer},{amount+bump},{discount},'{booked}');")
                sql.append(f"INSERT INTO ORDER_ITEMS VALUES ('{tenant}',{cohort},{identifier},1),('{tenant}',{cohort},{identifier},2);")
            sql.append(f"INSERT INTO PAYMENTS VALUES ('{tenant}',{cohort},1,{80+cohort+bump}),('{tenant}',{cohort},2,{200+cohort+bump});")
            for payload in ('{"type":"purchase","device":"mobile"}','{"type":"view","device":"mobile"}','{"type":"view","device":"desktop"}'):
                sql.append(f"INSERT INTO EVENTS SELECT '{tenant}',{cohort},PARSE_JSON('{payload}');")
            sql.append(f"INSERT INTO CUSTOMER_SEGMENTS VALUES ('{tenant}',{cohort},1,'premium'),('{tenant}',{cohort},1,'active'),('{tenant}',{cohort},2,'active');")
        where = f"TENANT_ID='acme' AND COHORT={cohort}"
        scenarios = [
            ('metrics','What is booked revenue?',f'SELECT SUM(AMOUNT) FROM ORDERS WHERE {where}',[[350+3*cohort]]),
            ('metrics','What is collected revenue?',f'SELECT SUM(AMOUNT) FROM PAYMENTS WHERE {where}',[[280+2*cohort]]),
            ('count','How many orders?',f'SELECT COUNT(*) FROM ORDERS WHERE {where}',[[3]]),
            ('distinct','How many distinct customers booked?',f'SELECT COUNT(DISTINCT CUSTOMER_ID) FROM ORDERS WHERE {where}',[[2]]),
            ('dates','What is booked revenue in January 2025?',f"SELECT SUM(AMOUNT) FROM ORDERS WHERE {where} AND BOOKED_AT >= '2025-01-01' AND BOOKED_AT < '2025-02-01'",[[300+2*cohort]]),
            ('fiscal','What is booked revenue in fiscal 2025 (April start)?',f"SELECT SUM(AMOUNT) FROM ORDERS WHERE {where} AND BOOKED_AT >= '2024-04-01' AND BOOKED_AT < '2025-04-01'",[[350+3*cohort]]),
            ('nulls','How many orders have unknown discounts?',f'SELECT COUNT(*) FROM ORDERS WHERE {where} AND DISCOUNT IS NULL',[[2]]),
            ('nulls','What is the mean known discount?',f'SELECT AVG(DISCOUNT) FROM ORDERS WHERE {where}',[[10]]),
            ('denominator','What percent of orders have a payment?',f'SELECT ROUND(100.0*COUNT(DISTINCT ORDER_ID)/3,2) FROM PAYMENTS WHERE {where}',[[66.67]]),
            ('fanout','What is booked revenue without double-counting item lines?',f"WITH ITEM_ORDERS AS (SELECT TENANT_ID,COHORT,ORDER_ID FROM ORDER_ITEMS GROUP BY TENANT_ID,COHORT,ORDER_ID) SELECT SUM(O.AMOUNT) FROM ORDERS O JOIN ITEM_ORDERS I ON O.TENANT_ID=I.TENANT_ID AND O.COHORT=I.COHORT AND O.ID=I.ORDER_ID WHERE O.TENANT_ID='acme' AND O.COHORT={cohort}",[[350+3*cohort]]),
            ('grain','Show booked revenue by customer.',f'SELECT CUSTOMER_ID,SUM(AMOUNT) FROM ORDERS WHERE {where} GROUP BY CUSTOMER_ID ORDER BY CUSTOMER_ID',[[1,150+2*cohort],[2,200+cohort]]),
            ('window','Rank orders by amount.',f'SELECT ID,ROW_NUMBER() OVER (ORDER BY AMOUNT DESC) FROM ORDERS WHERE {where} ORDER BY AMOUNT DESC',[[2,1],[1,2],[3,3]]),
            ('semi_structured','Count purchase events.',f"SELECT COUNT(*) FROM EVENTS WHERE {where} AND PAYLOAD:type::VARCHAR='purchase'",[[1]]),
            ('semi_structured','Count mobile events.',f"SELECT COUNT(*) FROM EVENTS WHERE {where} AND PAYLOAD:device::VARCHAR='mobile'",[[2]]),
            ('empty','Show orders from 2026.',f"SELECT ID FROM ORDERS WHERE {where} AND BOOKED_AT >= '2026-01-01'",[]),
            ('bridge','What is booked revenue for premium customers?',f"SELECT SUM(O.AMOUNT) FROM ORDERS O WHERE O.TENANT_ID='acme' AND O.COHORT={cohort} AND EXISTS (SELECT 1 FROM CUSTOMER_SEGMENTS S WHERE S.TENANT_ID=O.TENANT_ID AND S.COHORT=O.COHORT AND S.CUSTOMER_ID=O.CUSTOMER_ID AND S.SEGMENT='premium')",[[150+2*cohort]]),
            ('join','Show paid totals by order.',f"SELECT O.ID,SUM(P.AMOUNT) FROM ORDERS O JOIN PAYMENTS P ON O.TENANT_ID=P.TENANT_ID AND O.COHORT=P.COHORT AND O.ID=P.ORDER_ID WHERE O.TENANT_ID='acme' AND O.COHORT={cohort} GROUP BY O.ID ORDER BY O.ID",[[1,80+cohort],[2,200+cohort]]),
        ]
        for index, (category,question,reference,expected) in enumerate(scenarios):
            cases.append({'id':f'v1-{cohort:02}-{index:02}','category':category,'question':question+f' For cohort {cohort}.',
                'reference_sql':reference,'expected_rows':expected,'expected_status':'answered',
                'adjudication':'pending_business_review','tenant':'acme'})
        cases.append({'id':f'v1-{cohort:02}-17','category':'ambiguity','question':f'What is revenue for cohort {cohort}?',
            'expected_status':'needs_clarification','adjudication':'pending_business_review','tenant':'acme'})
        cases.append({'id':f'v1-{cohort:02}-18','category':'ambiguity','question':f'Show fiscal performance for cohort {cohort}.',
            'expected_status':'needs_clarification','adjudication':'pending_business_review','tenant':'acme'})
        cases.append({'id':f'v1-{cohort:02}-19','category':'follow_up','question':'And February?',
            'prior_questions':[f'What is booked revenue in January 2025 for cohort {cohort}?'],
            'reference_sql':f"SELECT SUM(AMOUNT) FROM ORDERS WHERE {where} AND BOOKED_AT >= '2025-02-01' AND BOOKED_AT < '2025-03-01'",
            'expected_rows':[[50+cohort]],'expected_status':'answered','adjudication':'pending_business_review','tenant':'acme'})
    return '\n'.join(sql)+'\n', {'fixture_version':'1','split':'holdout','certified':False,'cases':cases}

if __name__ == '__main__':
    sql, manifest = build()
    (ROOT/'fixture_v1.sql').write_text(sql)
    (ROOT/'holdout_v1.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(f"Wrote {len(manifest['cases'])} cases; business adjudication pending.")
