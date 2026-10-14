"""Versioned, admin-authorized runtime knowledge; reference resources stay immutable."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from knowvia.admin import AdministrationError, ROLE_PERMISSIONS
from knowvia.embeddings import DEFAULT_LOCAL_MODEL
from knowvia.parsing import parse_markdown
from knowvia.permissions import KNOWLEDGE_PERMISSION_BY_SKILL, KNOWLEDGE_SOURCES_BY_PERMISSION
from knowvia.retrieval import build_index
from knowvia.skills import SkillRegistry, initial_skill_definitions
from knowvia.vector_store import JsonVectorStore


REFERENCE_DOCUMENTS = Path(__file__).resolve().parents[2] / "knowvia_m1_dataset"
MAX_DOCUMENT_BYTES = 128 * 1024
MAX_CORPUS_BYTES = 2 * 1024 * 1024
MAX_DOCUMENTS = 100
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,90}\.md\Z")


def document_name(value):
    if (not isinstance(value, str) or not _NAME.fullmatch(value)
            or value.casefold() == "readme.md"
            or value.split('.')[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}):
        raise AdministrationError("Invalid Markdown filename")
    return value


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class KnowledgeSnapshot:
    version: str
    index: Path
    documents: dict

    def allowed_sources(self, identity, roles):
        if not identity.authenticated:
            return ()
        return tuple(name for name, doc in self.documents.items()
                     if KNOWLEDGE_PERMISSION_BY_SKILL[doc["skill"]] in identity.permissions
                     and set(doc["roles"]).intersection(roles))

    def registry(self):
        definitions = []
        for definition in initial_skill_definitions():
            sources = tuple(name for name, doc in self.documents.items()
                            if definition.skill_id == "general_company_knowledge" or doc["skill"] == definition.skill_id)
            # Empty domains retain their capability definitions (including tools),
            # but the request's source ACL yields no retrievable evidence.
            definitions.append(replace(definition, knowledge_sources=sources or definition.knowledge_sources))
        return SkillRegistry(definitions)


class SnapshotSourcePolicy:
    def __init__(self, allowed):
        self.allowed = tuple(allowed)

    def authorized_sources(self, identity):
        return self.allowed if identity.authenticated else ()


class EmptyKnowledgeRetriever:
    def search(self, question, top_k=3):
        return []


class KnowledgeManagement:
    """Stage in SQLite; publish immutable files via one transactional pointer.

    The pointer, previous version, draft and sanitized audit event commit in the
    same authority transaction. Index construction never holds that transaction.
    Readers retain a complete old snapshot during publication and rollback.
    """
    def __init__(self, authority, embedder_factory=None, reference_documents=None):
        self.authority = authority
        self.root = authority.paths.data_dir / "knowledge"
        from knowvia.deployment_bootstrap import DeploymentResources
        self.reference_documents = reference_documents or DeploymentResources.from_environment().documents_dir
        self.embedder_factory = embedder_factory

    @staticmethod
    def _get(db, name, default=None):
        row = db.execute("SELECT value FROM authority_meta WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _put(db, name, value):
        db.execute("INSERT OR REPLACE INTO authority_meta VALUES (?, ?)", (name, json.dumps(value)))

    def _reference(self):
        docs = {}
        for skill, permission in KNOWLEDGE_PERMISSION_BY_SKILL.items():
            for name in KNOWLEDGE_SOURCES_BY_PERMISSION[permission]:
                docs[name] = {"skill": skill, "roles": [role for role, grants in ROLE_PERMISSIONS.items() if permission in grants]}
        return KnowledgeSnapshot("reference", self.authority.paths.index, docs)

    def _snapshot(self, version, unpublished=False):
        if version is None:
            return self._reference()
        if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{32}", version):
            raise AdministrationError("Invalid knowledge version")
        folder = self.root / "versions" / version
        manifest_path = folder / "manifest.json"
        if not unpublished:
            # A read-only connection also works while a caller holds the
            # authority's reserved write lock. Bind ACL metadata to publication.
            db = sqlite3.connect(self.authority.paths.authority)
            try:
                db.execute("PRAGMA query_only = ON")
                expected = self._get(db, "knowledge_manifest_" + version)
            finally:
                db.close()
            if expected is None or _digest(manifest_path) != expected:
                raise AdministrationError("Knowledge manifest integrity failure")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        index = folder / "index.json"
        if _digest(index) != manifest["index_sha256"]:
            raise AdministrationError("Knowledge index integrity failure")
        for name, doc in manifest["documents"].items():
            document_name(name)
            if _digest(folder / "documents" / name) != doc["sha256"]:
                raise AdministrationError("Knowledge document integrity failure")
        return KnowledgeSnapshot(version, index, manifest["documents"])

    def snapshot(self):
        with self.authority.transaction() as db:
            version = self._get(db, "knowledge_active")
        return self._snapshot(version) if version else None

    def request_snapshot(self, identity):
        with self.authority.transaction() as db:
            version = self._get(db, "knowledge_active")
            # Roles are server-derived, never accepted from the requesting employee.
            roles = tuple(r[0] for r in db.execute("SELECT role FROM role_assignments WHERE employee_id = ?", (identity.subject_id,)))
        snapshot = self._snapshot(version) if version else None
        return snapshot, snapshot.allowed_sources(identity, roles) if snapshot else ()

    def _draft(self, db):
        active = self._get(db, "knowledge_active")
        draft = self._get(db, "knowledge_draft")
        if draft is not None:
            return draft
        snapshot = self._snapshot(active)
        folder = self.root / "versions" / active / "documents" if active else self.reference_documents
        docs = {name: {"content": (folder / name).read_text(encoding="utf-8-sig"), "skill": doc["skill"], "roles": doc["roles"]}
                for name, doc in snapshot.documents.items()}
        return {"base": active, "revision": "clean", "documents": docs}

    def listing(self, actor):
        with self.authority.transaction() as db:
            self.authority._require(db, actor, "admin.knowledge.manage")
            try:
                draft = self._draft(db)
            except (AdministrationError, ValueError, OSError):
                # Recovery metadata only. Never substitute corrupted evidence in
                # employee retrieval; keep authorized rollback available in UI.
                return {"active_version": self._get(db, "knowledge_active", "reference"),
                        "revision": "clean", "staged": False, "documents": [],
                        "integrity_error": True,
                        "rollback_available": self._get(db, "knowledge_previous") is not None}
            return {"active_version": self._get(db, "knowledge_active", "reference"),
                    "revision": draft["revision"], "staged": draft["revision"] != "clean",
                    "rollback_available": self._get(db, "knowledge_previous") is not None,
                    "documents": [{"name": name, "skill": doc["skill"], "roles": doc["roles"], "bytes": len(doc["content"].encode('utf-8'))}
                                  for name, doc in sorted(draft["documents"].items())]}

    def _revision(self, draft, revision):
        if not isinstance(revision, str) or revision != draft["revision"]:
            raise AdministrationError("Staged knowledge changed; refresh before editing")

    def stage(self, actor, name, content, skill, roles, revision):
        with self.authority.transaction() as db:
            self.authority._require(db, actor, "admin.knowledge.manage")
        name = document_name(name)
        if (not isinstance(content, str) or not content.strip() or '\x00' in content
                or len(content.encode('utf-8')) > MAX_DOCUMENT_BYTES):
            raise AdministrationError("Invalid or oversized Markdown document")
        if not isinstance(skill, str) or skill not in KNOWLEDGE_PERMISSION_BY_SKILL:
            raise AdministrationError("Invalid knowledge domain")
        permission = KNOWLEDGE_PERMISSION_BY_SKILL[skill]
        if (not isinstance(roles, list) or not roles or not all(isinstance(r, str) and r in ROLE_PERMISSIONS
                and permission in ROLE_PERMISSIONS[r] for r in roles)):
            raise AdministrationError("Select explicit roles with domain read permission")
        # Validate with the existing parser in a private, unreferenced stage.
        from tempfile import TemporaryDirectory
        self.root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=self.root, prefix="validate-") as temporary:
            path = Path(temporary) / name
            path.write_text(content, encoding="utf-8")
            if not parse_markdown(path):
                raise AdministrationError("Markdown requires a title and nonempty level-two sections")
        with self.authority.transaction() as db:
            self.authority._require(db, actor, "admin.knowledge.manage")
            draft = self._draft(db)
            self._revision(draft, revision)
            if any(n.casefold() == name.casefold() and n != name for n in draft["documents"]):
                raise AdministrationError("Case-insensitive filename conflict")
            draft["documents"][name] = {"content": content, "skill": skill, "roles": sorted(set(roles))}
            if len(draft["documents"]) > MAX_DOCUMENTS or sum(len(d['content'].encode('utf-8')) for d in draft["documents"].values()) > MAX_CORPUS_BYTES:
                raise AdministrationError("Knowledge corpus limit exceeded")
            draft["revision"] = uuid4().hex
            self._put(db, "knowledge_draft", draft)
            self.authority._event(db, actor, name, "KNOWLEDGE_STAGE", roles)

    def remove(self, actor, name, revision):
        document_name(name)
        with self.authority.transaction() as db:
            self.authority._require(db, actor, "admin.knowledge.manage")
            draft = self._draft(db)
            self._revision(draft, revision)
            if name not in draft["documents"]: raise AdministrationError("Unknown document")
            del draft["documents"][name]
            draft["revision"] = uuid4().hex
            self._put(db, "knowledge_draft", draft)
            self.authority._event(db, actor, name, "KNOWLEDGE_REMOVE_STAGED")

    @staticmethod
    def _validate_index(index, documents, directory):
        from knowvia.chunking import chunk_sections
        expected = {c.id: c for name in sorted(documents) for c in chunk_sections(parse_markdown(directory / name))}
        value = json.loads(index.read_text(encoding="utf-8"))
        if value.get("version") != 1 or value.get("embedding_model") != DEFAULT_LOCAL_MODEL:
            raise AdministrationError("Incompatible embedding model")
        chunks = value.get("chunks")
        if not isinstance(chunks, list) or len(chunks) > 1000:
            raise AdministrationError("Invalid chunk count")
        seen = set()
        sources = set()
        for chunk in chunks:
            vector = chunk.get("embedding")
            original = expected.get(chunk.get("id"))
            if (chunk.get("id") in seen or chunk.get("source") not in documents or not chunk.get("text")
                    or original is None or any(chunk.get(field) != getattr(original, field) for field in ('source', 'title', 'section', 'text'))
                    or not isinstance(vector, list) or len(vector) != 384
                    or not all(type(v) in (int, float) and math.isfinite(v) for v in vector)
                    or abs(math.sqrt(sum(v*v for v in vector)) - 1) > 1e-5):
                raise AdministrationError("Invalid embedded chunk")
            seen.add(chunk["id"])
            sources.add(chunk["source"])
        if sources != set(documents) or seen != set(expected): raise AdministrationError("Missing indexed document")

    def publish(self, actor, revision):
        with self.authority.transaction() as db:
            self.authority._require(db, actor, "admin.knowledge.manage")
        try:
            return self._publish(actor, revision)
        except Exception:
            # No exception body, document text or partial generated output.
            try:
                with self.authority.transaction() as db:
                    self.authority._require(db, actor, "admin.knowledge.manage")
                    self.authority._event(db, actor, "staged-corpus", "KNOWLEDGE_PUBLISH_FAILED")
            except AdministrationError:
                pass
            raise

    def _publish(self, actor, revision):
        with self.authority.transaction() as db:
            self.authority._require(db, actor, "admin.knowledge.manage")
            draft = self._draft(db)
            self._revision(draft, revision)
            if revision == "clean": raise AdministrationError("No staged changes")
        version = uuid4().hex
        folder = self.root / "versions" / version
        documents = folder / "documents"
        documents.mkdir(parents=True)
        for name, doc in draft["documents"].items():
            (documents / document_name(name)).write_text(doc["content"], encoding="utf-8")
        if self.embedder_factory is None:
            from knowvia.cli import create_embedder
            from knowvia.deployment import DeploymentConfig
            embedder = create_embedder(DeploymentConfig.from_environment().embedding_provider)
        else:
            embedder = self.embedder_factory()
        index = folder / "index.json"
        build_index(documents, embedder, JsonVectorStore(index))
        self._validate_index(index, draft["documents"], documents)
        manifest = {"index_sha256": _digest(index), "documents": {
            name: {"skill": doc["skill"], "roles": doc["roles"], "sha256": _digest(documents / name)}
            for name, doc in draft["documents"].items()}}
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        # Flush immutable files before the durable, atomic publication transaction.
        import os
        for path in [index, folder / "manifest.json", *documents.glob('*.md')]:
            with path.open('r+b') as stream: os.fsync(stream.fileno())
        if os.name != 'nt':
            # Persist directory entries before SQLite can expose their version.
            for directory in [documents, folder, folder.parent, self.root, self.root.parent]:
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(descriptor)
                finally: os.close(descriptor)
        self._snapshot(version, unpublished=True)
        with self.authority.transaction() as db:
            self.authority._require(db, actor, "admin.knowledge.manage")
            current = self._get(db, "knowledge_draft")
            if current != draft or self._get(db, "knowledge_active") != draft["base"]:
                raise AdministrationError("Concurrent knowledge update; publication rejected")
            # 'reference' is represented explicitly for rollback to the untouched seed.
            self._put(db, "knowledge_previous", draft["base"] or "reference")
            self._put(db, "knowledge_manifest_" + version, _digest(folder / "manifest.json"))
            self._put(db, "knowledge_active", version)
            db.execute("DELETE FROM authority_meta WHERE name = 'knowledge_draft'")
            self.authority._event(db, actor, version, "KNOWLEDGE_PUBLISH")
        return version

    def rollback(self, actor, revision):
        with self.authority.transaction() as db:
            self.authority._require(db, actor, "admin.knowledge.manage")
            draft = self._get(db, "knowledge_draft", {"revision": "clean"})
            self._revision(draft, revision)
            if revision != "clean": raise AdministrationError("Discard staged changes before rollback")
            previous = self._get(db, "knowledge_previous")
            if previous is None: raise AdministrationError("No previous knowledge version")
            self._snapshot(None if previous == "reference" else previous)
            active = self._get(db, "knowledge_active")
            self._put(db, "knowledge_active", None if previous == "reference" else previous)
            self._put(db, "knowledge_previous", active or "reference")
            self.authority._event(db, actor, previous, "KNOWLEDGE_ROLLBACK")

    def discard(self, actor, revision):
        with self.authority.transaction() as db:
            self.authority._require(db, actor, "admin.knowledge.manage")
            self._revision(self._draft(db), revision)
            db.execute("DELETE FROM authority_meta WHERE name = 'knowledge_draft'")
            self.authority._event(db, actor, "draft", "KNOWLEDGE_DISCARD")
