"""
KUVA AI Enterprise RDF Triple Store
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd
import rdflib
from prometheus_client import Counter, Gauge, Histogram
from pydantic import BaseModel, Field, validator
from redis import Redis
from sqlalchemy import (JSON, BigInteger, Column, DateTime, ForeignKey,
                        Index, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, and_, create_engine, delete, exists,
                        insert, select, text, update)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

# ======================
# Metrics Configuration
# ======================
METRICS = {
    "triple_inserts": Counter("rdf_triple_inserts", "Total triples inserted"),
    "triple_deletes": Counter("rdf_triple_deletes", "Total triples deleted"),
    "sparql_queries": Counter("rdf_sparql_queries", "SPARQL queries executed"),
    "query_duration": Histogram("rdf_query_duration", "Query latency distribution"),
    "cache_hits": Counter("rdf_cache_hits", "Triple pattern cache hits"),
    "cache_misses": Counter("rdf_cache_misses", "Triple pattern cache misses"),
    "active_transactions": Gauge("rdf_active_transactions", "Current transactions"),
}

# ====================
# Database Schema
# ====================
Base = declarative_base()
metadata = MetaData()

triples_table = Table(
    'triples', metadata,
    Column('id', BigInteger().with_variant(Integer, "sqlite"), primary_key=True),
    Column('subject', String(1024), nullable=False),
    Column('predicate', String(1024), nullable=False),
    Column('object', JSON, nullable=False),  # Stores both literals and URIs
    Column('context', ARRAY(String), default=[]),
    Column('datatype', String(256)),
    Column('language', String(8)),
    Column('version', Integer, default=1),
    Column('created_at', DateTime, default=datetime.utcnow),
    Column('deleted', Boolean, default=False),
    Column('tsvector_column', TSVECTOR),
    Index('idx_spo', 'subject', 'predicate', 'object', postgresql_using='gin'),
    Index('idx_fulltext', 'tsvector_column', postgresql_using='gin'),
    UniqueConstraint('subject', 'predicate', 'object', 'context',
                    name='uix_triple_unique')
)

namespaces_table = Table(
    'namespaces', metadata,
    Column('prefix', String(64), primary_key=True),
    Column('uri', String(1024), nullable=False),
    Column('last_used', DateTime, default=datetime.utcnow)
)

# ====================
# Data Models
# ====================
class RDFTriple(BaseModel):
    subject: str = Field(..., min_length=1, max_length=1024)
    predicate: str = Field(..., min_length=1, max_length=1024)
    object: Any
    context: List[str] = []
    datatype: Optional[str] = None
    language: Optional[str] = None

    @validator('subject', 'predicate')
    def validate_uri(cls, v):
        if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', v):
            raise ValueError(f"Invalid URI format: {v}")
        return v

class TriplePattern(BaseModel):
    subject: Optional[str] = None
    predicate: Optional[str] = None
    object: Optional[Any] = None
    context: Optional[List[str]] = None

# ====================
# Core Store Class
# ====================
class RDFTripleStore:
    """Enterprise-grade RDF triple store with ACID guarantees"""
    
    def __init__(self, 
                 db_uri: str,
                 redis_uri: str = "redis://localhost:6379/0",
                 cache_ttl: int = 3600,
                 bulk_size: int = 1000):
        self.engine = create_engine(db_uri, pool_size=20, max_overflow=0)
        self.Session = scoped_session(sessionmaker(bind=self.engine))
        self.redis = Redis.from_url(redis_uri)
        self.cache_ttl = cache_ttl
        self.bulk_size = bulk_size
        self.graph = rdflib.ConjunctiveGraph()
        self._setup_schema()
        self._lock_manager = DistributedLockManager(redis_uri)
        
    def _setup_schema(self):
        """Initialize database schema"""
        try:
            metadata.create_all(self.engine)
        except Exception as e:
            logging.error("Schema initialization failed: %s", e)
            raise

    @contextlib.contextmanager
    def transaction(self):
        """ACID transaction context manager"""
        session = self.Session()
        METRICS["active_transactions"].inc()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            raise RDFTransactionError(f"Transaction failed: {str(e)}") from e
        finally:
            self.Session.remove()
            METRICS["active_transactions"].dec()

    # ====================
    # CRUD Operations
    # ====================
    def insert_triple(self, triple: RDFTriple) -> None:
        """Insert a single triple with validation"""
        with self.transaction() as session:
            self._insert_triples([triple], session)
            
    def insert_bulk(self, triples: List[RDFTriple]) -> None:
        """Bulk insert with optimized batch processing"""
        with self.transaction() as session:
            for i in range(0, len(triples), self.bulk_size):
                batch = triples[i:i+self.bulk_size]
                self._insert_triples(batch, session)
                self._update_cache(batch)
                session.expire_all()

    def _insert_triples(self, triples: List[RDFTriple], session) -> None:
        """Low-level bulk insert implementation"""
        try:
            stmt = insert(triples_table).values(
                [self._triple_to_dict(t) for t in triples]
            )
            session.execute(stmt)
            METRICS["triple_inserts"].inc(len(triples))
        except IntegrityError as e:
            raise RDFConflictError("Duplicate triples detected") from e

    def delete_triple(self, pattern: TriplePattern) -> int:
        """Delete triples matching pattern"""
        with self.transaction() as session:
            where = self._build_where_clause(pattern)
            stmt = delete(triples_table).where(where)
            result = session.execute(stmt)
            METRICS["triple_deletes"].inc(result.rowcount)
            self._invalidate_cache(pattern)
            return result.rowcount

    # ====================
    # Query Operations
    # ====================
    def query(self, pattern: TriplePattern) -> List[RDFTriple]:
        """Basic triple pattern matching"""
        cache_key = self._cache_key(pattern)
        if cached := self.redis.get(cache_key):
            METRICS["cache_hits"].inc()
            return [RDFTriple.parse_raw(t) for t in cached]
            
        METRICS["cache_misses"].inc()
        with self.transaction() as session:
            where = self._build_where_clause(pattern)
            stmt = select([triples_table]).where(where)
            results = session.execute(stmt).fetchall()
            triples = [self._dict_to_triple(r) for r in results]
            self.redis.setex(cache_key, self.cache_ttl, 
                            [t.json() for t in triples])
            return triples

    def sparql_query(self, query: str) -> pd.DataFrame:
        """Optimized SPARQL query execution"""
        METRICS["sparql_queries"].inc()
        start_time = time.monotonic()
        
        try:
            parsed = self._parse_optimize_sparql(query)
            if self._is_cacheable(parsed):
                cache_key = self._sparql_cache_key(query)
                if cached := self.redis.get(cache_key):
                    return pd.read_json(cached, orient='split')
                    
            with self.transaction() as session:
                result = self._execute_sparql(parsed, session)
                df = self._format_results(result)
                
                if self._is_cacheable(parsed):
                    self.redis.setex(cache_key, self.cache_ttl, df.to_json(orient='split'))
                
                METRICS["query_duration"].observe(time.monotonic() - start_time)
                return df
                
        except Exception as e:
            raise RDFQueryError(f"SPARQL query failed: {str(e)}") from e

    # ====================
    # Cache Management
    # ====================
    def _cache_key(self, pattern: TriplePattern) -> str:
        """Generate consistent cache key for patterns"""
        key_data = pattern.dict(exclude_none=True)
        return f"rdf:cache:{hashlib.sha256(str(key_data).encode()).hexdigest()}"

    def _sparql_cache_key(self, query: str) -> str:
        """Generate cache key for SPARQL queries"""
        return f"rdf:sparql:{hashlib.sha256(query.encode()).hexdigest()}"

    def _update_cache(self, triples: List[RDFTriple]) -> None:
        """Invalidate affected cache entries on insert"""
        for t in triples:
            patterns = [
                TriplePattern(subject=t.subject),
                TriplePattern(predicate=t.predicate),
                TriplePattern(object=t.object)
            ]
            for p in patterns:
                self.redis.delete(self._cache_key(p))

    def _invalidate_cache(self, pattern: TriplePattern) -> None:
        """Delete all cache entries matching pattern"""
        keys = self.redis.keys(f"rdf:cache:*{self._cache_key(pattern)}*")
        if keys:
            self.redis.delete(*keys)

    # ====================
    # Utility Methods
    # ====================
    def _triple_to_dict(self, triple: RDFTriple) -> Dict:
        """Convert RDFTriple to database record format"""
        return {
            "subject": triple.subject,
            "predicate": triple.predicate,
            "object": triple.object,
            "context": triple.context,
            "datatype": triple.datatype,
            "language": triple.language
        }

    def _dict_to_triple(self, record: Dict) -> RDFTriple:
        """Convert database record to RDFTriple"""
        return RDFTriple(
            subject=record["subject"],
            predicate=record["predicate"],
            object=record["object"],
            context=record["context"],
            datatype=record["datatype"],
            language=record["language"]
        )

    def _build_where_clause(self, pattern: TriplePattern) -> Any:
        """Construct SQLAlchemy where clause from pattern"""
        clauses = []
        if pattern.subject:
            clauses.append(triples_table.c.subject == pattern.subject)
        if pattern.predicate:
            clauses.append(triples_table.c.predicate == pattern.predicate)
        if pattern.object is not None:
            clauses.append(triples_table.c.object == pattern.object)
        if pattern.context:
            clauses.append(triples_table.c.context.contains(pattern.context))
        return and_(*clauses)

    # ====================
    # Advanced Features
    # ====================
    def create_snapshot(self, snapshot_id: str) -> None:
        """Create point-in-time snapshot"""
        with self._lock_manager.global_lock():
            with self.engine.begin() as conn:
                conn.execute(text(f"CREATE DATABASE {snapshot_id} TEMPLATE current_db"))

    def export_data(self, format: str = "nquads") -> str:
        """Export data in specified format"""
        with self.transaction() as session:
            stmt = select([triples_table])
            result = session.execute(stmt)
            g = rdflib.ConjunctiveGraph()
            for row in result:
                subj = rdflib.URIRef(row.subject)
                pred = rdflib.URIRef(row.predicate)
                obj = self._parse_object(row)
                g.add((subj, pred, obj))
            return g.serialize(format=format)

    def _parse_object(self, row: Dict) -> Any:
        """Convert database object to RDFLib term"""
        if isinstance(row.object, dict) and '@type' in row.object:
            return rdflib.Literal(
                row.object['value'],
                datatype=row.object.get('datatype'),
                lang=row.object.get('language')
            )
        return rdflib.URIRef(row.object)

    # ====================
    # Security
    # ====================
    def apply_acl(self, pattern: TriplePattern, role: str) -> None:
        """Define access control rules"""
        acl_key = f"rdf:acl:{role}"
        self.redis.sadd(acl_key, pattern.json())

    def check_access(self, role: str, triple: RDFTriple) -> bool:
        """Verify access permissions"""
        acl_key = f"rdf:acl:{role}"
        patterns = [TriplePattern.parse_raw(p) for p in self.redis.smembers(acl_key)]
        return any(self._match_pattern(p, triple) for p in patterns)

    def _match_pattern(self, pattern: TriplePattern, triple: RDFTriple) -> bool:
        """Check if triple matches access pattern"""
        return all([
            pattern.subject in (None, triple.subject),
            pattern.predicate in (None, triple.predicate),
            pattern.object in (None, triple.object),
            pattern.context in (None, triple.context)
        ])

# ====================
# Exception Classes
# ====================
class RDFStoreError(Exception):
    """Base exception for store operations"""

class RDFTransactionError(RDFStoreError):
    """Transaction failure"""

class RDFQueryError(RDFStoreError):
    """Query execution failure"""

class RDFConflictError(RDFStoreError):
    """Data consistency violation"""

# ====================
# Distributed Locking
# ====================
class DistributedLockManager:
    """Redis-based distributed locking"""
    def __init__(self, redis_uri: str):
        self.redis = Redis.from_url(redis_uri)
        self.token = str(uuid.uuid4())

    @contextlib.contextmanager
    def global_lock(self, timeout=30):
        """Cluster-wide lock for critical operations"""
        lock = self.redis.lock("rdf:global_lock", timeout=timeout, token=self.token)
        if lock.acquire(blocking=True, blocking_timeout=timeout):
            try:
                yield
            finally:
                lock.release()
        else:
            raise RDFStoreError("Failed to acquire global lock")

# ====================
# Example Usage
# ====================
if __name__ == "__main__":
    store = RDFTripleStore(
        db_uri="postgresql://user:pass@localhost/kuva_rdf",
        redis_uri="redis://localhost:6379/1"
    )
    
    # Insert sample data
    triples = [
        RDFTriple(
            subject="urn:kuva:Device:123",
            predicate="http://schema.org/name",
            object="AI Sensor Array"
        ),
        RDFTriple(
            subject="urn:kuva:Device:123",
            predicate="http://schema.org/location",
            object={"@type": "Point", "coordinates": [34.05, -118.25]}
        )
    ]
    store.insert_bulk(triples)
    
    # Execute query
    pattern = TriplePattern(subject="urn:kuva:Device:123")
    results = store.query(pattern)
    print(f"Found {len(results)} matching triples")
    
    # SPARQL query
    query = """
        SELECT ?name WHERE {
            <urn:kuva:Device:123> <http://schema.org/name> ?name .
        }
    """
    df = store.sparql_query(query)
    print(df)
