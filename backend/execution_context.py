"""Server-owned identity and policy scope. Never deserialize this from a query body."""
from dataclasses import dataclass, field
from hashlib import sha256
from time import monotonic
import json
import secrets

class AuthenticationError(Exception): pass
class BudgetExceeded(Exception): pass

@dataclass
class RequestBudget:
    max_submissions: int = 3
    max_rows: int = 500
    max_bytes: int = 10 * 1024 * 1024
    statement_seconds: int = 30
    queue_seconds: int = 10
    deadline_seconds: int = 90
    started: float = field(default_factory=monotonic)
    submissions: int = 0

    def remaining(self):
        remaining = self.deadline_seconds - (monotonic() - self.started)
        if remaining <= 0: raise BudgetExceeded('Request deadline exceeded')
        return remaining

    def submit(self):
        self.remaining()
        if self.submissions >= self.max_submissions:
            raise BudgetExceeded('Analytical submission budget exceeded')
        self.submissions += 1

@dataclass(frozen=True)
class ExecutionContext:
    principal: str
    tenant: str
    connection_profile: str
    role: str
    database: str
    schema: str
    policy_version: str
    allowed_objects: frozenset[str]
    schema_version: str = 'unknown'
    semantic_version: str = '0'
    dialect: str = 'snowflake'
    budget: RequestBudget = field(default_factory=RequestBudget, compare=False)
    request_id: str = field(default_factory=lambda: secrets.token_hex(16), compare=False)

    @property
    def scope(self):
        return sha256(json.dumps([self.principal, self.tenant, self.connection_profile,
            self.role, self.database, self.schema, self.policy_version, self.schema_version,
            self.semantic_version, sorted(self.allowed_objects)], separators=(',', ':')).encode()).hexdigest()

    def conversation_key(self, conversation_id):
        # Stable across schema/definition changes; identity and connection remain isolated.
        return sha256(json.dumps([self.principal, self.tenant, self.connection_profile,
                                  self.role, self.database, self.schema, self.policy_version,
                                  sorted(self.allowed_objects), conversation_id]).encode()).hexdigest()


def cache_key(context, text):
    # No lowercasing or whitespace rewriting inside literals.
    return sha256((context.scope + '\0' + text).encode()).hexdigest()


def authenticate(api_key, registry, *, allow_anonymous=False):
    if api_key:
        tenant = registry.get_by_api_key(api_key)
        if tenant is None: raise AuthenticationError('Invalid credentials')
        return tenant
    if not allow_anonymous: raise AuthenticationError('Credentials required')
    from tenant import TenantConfig
    return TenantConfig('local', 'local', principal='local')


def authorized_schema(schema, context):
    return {name: info for name, info in schema.items() if name in context.allowed_objects}
