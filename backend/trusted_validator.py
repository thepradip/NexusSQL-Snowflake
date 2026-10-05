"""Fail-closed AST checks shared by every governed execution path."""
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope, Scope

# Positive allowlist of scalar/aggregate/window AST identities corresponding to
# approved Snowflake builtins. Parser recognition alone never grants permission.
_APPROVED_FUNCTIONS = frozenset({
    'And','Or','Exists',  # SQL boolean/subquery operators, not callable UDFs
    'Count','Sum','Avg','Min','Max','Abs','Round','Ceil','Floor','Sqrt','Pow','Mod',
    'Stddev','StddevPop','StddevSamp','Variance','VariancePop','VarianceSamp',
    'PercentileCont','PercentileDisc','Median','Corr',
    'RowNumber','Rank','DenseRank','Lag','Lead','FirstValue','LastValue','NthValue',
    'Ntile','CumeDist','PercentRank',
    'Cast','TryCast','Coalesce','Nullif','If','Case','Greatest','Least',
    'Lower','Upper','Length','Trim','LTrim','RTrim','Substring','Concat','ConcatWs',
    'Replace','RegexpReplace','RegexpLike','RegexpExtract',
    'CurrentDate','CurrentTimestamp','DateAdd','DateSub','DateDiff','DateTrunc',
    'TimestampTrunc','Extract','TimeToStr','StrToDate','TsOrDsToDate','TsOrDsToTimestamp',
    'ToNumber','ToChar','ToVariant','ParseJSON','JSONExtract','JSONExtractScalar',
})

class ValidationError(Exception):
    def __init__(self, message, *, code='policy', repairable=False):
        super().__init__(message)
        self.code, self.repairable = code, repairable


def _ident(identifier, dialect):
    if identifier is None: return ''
    if dialect == 'snowflake':
        return identifier.name if identifier.args.get('quoted') else identifier.name.upper()
    return identifier.name.lower()


def object_name(table, context):
    parts = [_ident(table.args.get(k), context.dialect) for k in ('catalog', 'db', 'this')]
    if not parts[2] or not isinstance(table.this, exp.Identifier):
        raise ValidationError('Stage and table function access is prohibited')
    if context.dialect == 'snowflake':
        return '.'.join([parts[0] or context.database, parts[1] or context.schema, parts[2]])
    if parts[0] or parts[1]: raise ValidationError('Cross-database access is prohibited')
    return parts[2]


def _column_lineage(column, scope, context):
    source = scope.sources.get(column.table) if column.table else None
    if source is None and len(scope.sources) == 1: source = next(iter(scope.sources.values()))
    if isinstance(source, exp.Table): return object_name(source, context) + '.' + _ident(column.this, context.dialect)
    if isinstance(source, Scope):
        matches = [projection for projection in source.expression.selects
                   if _ident(projection.args.get('alias') if isinstance(projection,exp.Alias) else projection.this if isinstance(projection,exp.Column) else None, context.dialect) == _ident(column.this,context.dialect)]
        if len(matches) == 1:
            projection = matches[0].this if isinstance(matches[0],exp.Alias) else matches[0]
            if isinstance(projection,exp.Column): return _column_lineage(projection,source,context)
    raise ValidationError('Join column lineage is unresolved', code='semantic')


def validate(sql, context, schema, *, approved_joins=(), reviewed_template=False):
    try:
        statements = [s for s in sqlglot.parse(sql, read=context.dialect) if s is not None]
    except sqlglot.errors.ParseError:
        raise ValidationError('SQL could not be parsed', code='syntax', repairable=True)
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.SetOperation)):
        raise ValidationError('Exactly one read-only query is required')
    tree = statements[0]
    forbidden = ('Insert', 'Update', 'Delete', 'Drop', 'Create', 'Alter', 'Command',
                 'Into', 'Lock', 'Copy', 'Grant', 'Revoke', 'Transaction', 'Use')
    if any(isinstance(node, (exp.TableSample, exp.HistoricalData)) for node in tree.walk()):
        raise ValidationError('Sampling and time-travel require a separately reviewed execution contract', code='semantic')
    if any(type(node).__name__ in forbidden for node in tree.walk()):
        raise ValidationError('Forbidden SQL operation')
    # Unknown functions may be external/UDFs; qualified builtins also fail closed.
    for node in tree.walk():
        if (isinstance(node, exp.Func) and type(node).__name__ not in _APPROVED_FUNCTIONS) or (isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func)):
            raise ValidationError('Unapproved function')
    physical = []
    for scope in traverse_scope(tree):
        for _, source in scope.sources.items():
            if isinstance(source, exp.Table):
                name = object_name(source, context)
                if name not in context.allowed_objects or name not in schema:
                    raise ValidationError('Object is not authorized')
                physical.append(name)
    # Normalize identifiers using the dialect rules before checking column scopes.
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
    normalized = normalize_identifiers(tree.copy(), dialect=context.dialect)
    nested = {}
    for name, info in schema.items():
        if name not in context.allowed_objects: continue
        columns = {c['name']: c.get('type', 'UNKNOWN') for c in info['columns']}
        target = nested
        components = name.split('.')
        for part in components[:-1]: target = target.setdefault(part, {})
        target[components[-1]] = columns
    try:
        qualify(normalized, dialect=context.dialect, schema=nested,
                db=context.schema if context.dialect == 'snowflake' else None,
                catalog=context.database if context.dialect == 'snowflake' else None,
                validate_qualify_columns=True, identify=False)
    except Exception:
        raise ValidationError('Column or alias could not be resolved', code='identifier', repairable=True)
    # Join conditions must be reviewed, never inferred from matching names.
    for scope in traverse_scope(tree):
        for join in scope.expression.args.get('joins', []):
            on = join.args.get('on')
            if not on or join.args.get('using'):
                raise ValidationError('Join requires reviewed explicit keys', code='semantic')
            def predicates(node):
                if isinstance(node, exp.Paren): return predicates(node.this)
                if isinstance(node, exp.And): return predicates(node.this) + predicates(node.expression)
                if isinstance(node, exp.EQ): return [node]
                raise ValidationError('Join must use positive reviewed equality predicates', code='semantic')
            equalities = predicates(on)
            if not equalities or any(not isinstance(eq.this, exp.Column) or
                                     not isinstance(eq.expression, exp.Column) for eq in equalities):
                raise ValidationError('Unreviewed join', code='semantic')
            for eq in equalities:
                endpoints = []
                for col in (eq.this, eq.expression):
                    source = scope.sources.get(col.table)
                    if not isinstance(source,exp.Table) and not reviewed_template:
                        raise ValidationError('Derived joins require a reviewed semantic template',code='semantic')
                    endpoints.append(_column_lineage(col,scope,context))
                if frozenset(endpoints) not in {frozenset(j['keys']) for j in approved_joins}:
                    raise ValidationError('Join relationship is not reviewed', code='semantic')
                reviewed = next(j for j in approved_joins if frozenset(j['keys']) == frozenset(endpoints))
                if reviewed.get('cardinality') not in ('one_to_one', 'many_to_one', 'one_to_many'):
                    raise ValidationError('Join cardinality is unresolved', code='semantic')
                if reviewed.get('cardinality') != 'one_to_one' and any(tree.find_all(exp.AggFunc)) and not reviewed_template:
                    raise ValidationError('Aggregation over a fanout join requires a reviewed query template', code='semantic')
    return {'readonly': True, 'objects': sorted(set(physical)), 'columns_resolved': True,
            'joins_reviewed': True, 'sql_unchanged': True}
