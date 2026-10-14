import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from starlette.testclient import TestClient

from knowvia.admin import AdministrationError, SqliteAuthority
from knowvia.employee_directory import EmployeeRecord
from knowvia.runtime_paths import RuntimePaths, PROJECT_ROOT
from knowvia.knowledge_management import KnowledgeManagement, MAX_DOCUMENT_BYTES, REFERENCE_DOCUMENTS
from knowvia.embeddings import DEFAULT_LOCAL_MODEL
from knowvia.deployment import DeploymentConfig, ApplicationEnvironment
from knowvia.web import create_web_app, ProductionRuntimeFactory

PASSWORD = "Knowledge-testing-phrase!"
ORIGIN = {"origin": "http://testserver"}
CONTENT = "# Managed procedure\n\n## Connection help\nIf the connection fails, restart the client."


def employee(name):
    return EmployeeRecord(name, "Test", name, name + "@novatech.example", "IT", "Employee")


class Embedder:
    model_name = DEFAULT_LOCAL_MODEL
    def embed(self, texts): return [[1.0] + [0.0] * 383 for _ in texts]


@pytest.fixture
def manager(tmp_path):
    authority = SqliteAuthority(RuntimePaths(tmp_path))
    authority.bootstrap(employee("admin"), PASSWORD, PASSWORD)
    authority.provision("admin", employee("reader"), PASSWORD, PASSWORD, ["hr-reader"])
    authority.provision("admin", employee("other"), PASSWORD, PASSWORD, ["knowledge-reader"])
    shutil.copyfile(PROJECT_ROOT / "data/knowvia_index.json", authority.paths.index)
    return KnowledgeManagement(authority, Embedder)


def stage(manager, name="managed.md", content=CONTENT, skill="it_support", roles=None):
    manager.stage("admin", name, content, skill, roles or ["it-reader"], manager.listing("admin")["revision"])


def publish(manager): return manager.publish("admin", manager.listing("admin")["revision"])


def test_staging_is_private_publication_and_rollback_preserve_reference_and_restart(manager):
    before = manager.authority.paths.index.read_bytes()
    originals = {p.name: p.read_bytes() for p in REFERENCE_DOCUMENTS.glob('*.md')}
    stage(manager)
    assert manager.snapshot() is None
    assert KnowledgeManagement(SqliteAuthority(manager.authority.paths), Embedder).listing("admin")["staged"]
    first = publish(manager)
    snapshot = KnowledgeManagement(SqliteAuthority(manager.authority.paths), Embedder).snapshot()
    assert snapshot.version == first and "managed.md" in snapshot.documents
    assert snapshot.index != manager.authority.paths.index
    assert manager.authority.paths.index.read_bytes() == before
    assert originals == {p.name: p.read_bytes() for p in REFERENCE_DOCUMENTS.glob('*.md')}
    stage(manager, content=CONTENT.replace("restart", "close and restart"))
    second = publish(manager)
    assert first != second
    manager.rollback("admin", "clean")
    assert manager.snapshot().version == first
    assert snapshot.index.read_bytes() == manager.snapshot().index.read_bytes()
    manager.rollback("admin", "clean")
    assert manager.snapshot().version == second
    events = manager.authority.events("admin")
    assert any(e['operation'] == 'KNOWLEDGE_PUBLISH' for e in events)
    assert not any(s in json.dumps(events) for s in [CONTENT, PASSWORD, "embedding"])


def test_rollback_first_publication_restores_reference(manager):
    stage(manager); publish(manager)
    manager.rollback("admin", "clean")
    assert manager.snapshot() is None and manager.authority.paths.index.exists()


@pytest.mark.parametrize("name", ["../escape.md", "C:\\escape.md", "/escape.md", "a/b.md", "a\\b.md", "%2e%2e.md", "CON.md", "readme.md", "secret.env", "x.md:stream", ".hidden.md"])
def test_path_traversal_and_non_markdown_are_rejected(manager, name):
    with pytest.raises(AdministrationError): stage(manager, name)
    assert not manager.listing("admin")["staged"]


@pytest.mark.parametrize("content", ["", "No title", "# Title\nNothing indexed", "# Title\n## Empty", "# Title\n## Section\n\x00bad", "x" * (MAX_DOCUMENT_BYTES + 1)], ids=['empty', 'no-title', 'no-sections', 'empty-section', 'nul', 'oversized'])
def test_invalid_and_oversized_uploads_rejected(manager, content):
    with pytest.raises((AdministrationError, ValueError)): stage(manager, content=content)
    assert manager.snapshot() is None


@pytest.mark.parametrize("roles", [[], ["employee"], ["administrator"], ["made-up"], ["hr-reader"]])
def test_explicit_compatible_read_roles_required(manager, roles):
    with pytest.raises(AdministrationError):
        manager.stage("admin", "managed.md", CONTENT, "it_support", roles, "clean")


def test_non_admin_cannot_list_or_mutate_and_cannot_spoof_roles(manager):
    for operation in [lambda: manager.listing("reader"), lambda: manager.stage("reader", "x.md", CONTENT, "hr", ["hr-reader"], "clean"),
                      lambda: manager.remove("reader", "managed.md", "clean"), lambda: manager.publish("reader", "clean"),
                      lambda: manager.rollback("reader", "clean"), lambda: manager.discard("reader", "clean")]:
        with pytest.raises(AdministrationError): operation()
    assert not manager.root.exists()


def test_source_acl_requires_both_current_trusted_role_and_domain_permission(manager):
    stage(manager, skill="hr", roles=["hr-reader"]); publish(manager)
    reader = manager.authority.resolve("reader")
    other = manager.authority.resolve("other")
    snapshot, allowed = manager.request_snapshot(reader)
    assert "managed.md" in allowed
    assert "managed.md" not in manager.request_snapshot(other)[1]
    assert "managed.md" not in snapshot.allowed_sources(reader, ["it-reader"])
    assert "managed.md" not in manager.request_snapshot(manager.authority.resolve("admin"))[1]
    manager.authority.assign_roles("admin", "reader", ["employee"])
    assert not manager.request_snapshot(manager.authority.resolve("reader"))[1]


def test_remove_is_staged_then_published_and_rollback_restores(manager):
    stage(manager); first = publish(manager)
    manager.remove("admin", "managed.md", "clean")
    assert "managed.md" in manager.snapshot().documents
    publish(manager)
    assert "managed.md" not in manager.snapshot().documents
    manager.rollback("admin", "clean")
    assert manager.snapshot().version == first


def test_stale_drafts_and_case_conflicts_rejected_without_lost_updates(manager):
    stage(manager)
    with pytest.raises(AdministrationError): manager.stage("admin", "new.md", CONTENT, "it_support", ["it-reader"], "clean")
    with pytest.raises(AdministrationError): stage(manager, "Managed.md")
    with pytest.raises(AdministrationError): manager.rollback("admin", manager.listing("admin")["revision"])
    manager.discard("admin", manager.listing("admin")["revision"])
    assert not manager.listing("admin")["staged"]


@pytest.mark.parametrize("bad", ["failure", "wrong-model", "invalid-vector", "wrong-count"])
def test_failed_index_publication_keeps_previous_working_snapshot_and_draft(manager, bad):
    stage(manager); first = publish(manager)
    stage(manager, "next.md")
    class BadEmbedder(Embedder):
        model_name = "another-model" if bad == "wrong-model" else DEFAULT_LOCAL_MODEL
        def embed(self, texts):
            if bad == "failure": raise RuntimeError("private internal exception")
            if bad == "wrong-count": return []
            if bad == "invalid-vector": return [[float('nan')] * 384 for _ in texts]
            return super().embed(texts)
    manager.embedder_factory = BadEmbedder
    with pytest.raises((AdministrationError, ValueError, RuntimeError)): publish(manager)
    assert manager.snapshot().version == first and manager.listing("admin")["staged"]
    assert "next.md" not in manager.snapshot().documents
    assert manager.authority.events('admin')[0]['operation'] == 'KNOWLEDGE_PUBLISH_FAILED'


def test_publication_and_concurrent_edit_are_atomic_and_old_requests_keep_snapshot(manager):
    stage(manager); old_version = publish(manager)
    snapshot = manager.snapshot()
    stage(manager, "next.md")
    entered, release = Event(), Event()
    class Waiting(Embedder):
        def embed(self, texts):
            entered.set(); assert release.wait(15); return super().embed(texts)
    manager.embedder_factory = Waiting
    with ThreadPoolExecutor() as pool:
        future = pool.submit(publish, manager)
        assert entered.wait(10)
        assert manager.snapshot().version == old_version
        stage(manager, "concurrent.md")
        release.set()
        with pytest.raises(AdministrationError, match="Concurrent"): future.result()
    assert manager.snapshot().version == old_version
    manager.embedder_factory = Embedder
    publish(manager)
    assert snapshot.version == old_version and "next.md" not in snapshot.documents
    assert snapshot.index.exists()


def test_corrupted_published_index_fails_closed_and_can_rollback(manager):
    stage(manager); publish(manager)
    snapshot = manager.snapshot()
    snapshot.index.write_text('{}', encoding='utf-8')
    with pytest.raises(AdministrationError, match="integrity"): manager.snapshot()
    assert manager.listing('admin')['integrity_error']
    # Rollback must not need to load the broken current index.
    manager.rollback("admin", "clean")
    assert manager.snapshot() is None


def test_source_acl_manifest_corruption_fails_closed(manager):
    stage(manager, skill='hr', roles=['hr-reader']); publish(manager)
    snapshot = manager.snapshot()
    manifest_path = snapshot.index.parent / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['documents']['managed.md']['roles'] = ['knowledge-reader']
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(AdministrationError, match='manifest integrity'):
        manager.request_snapshot(manager.authority.resolve('other'))
    manager.rollback('admin', 'clean')
    assert manager.snapshot() is None


def test_revoked_administrator_cannot_finish_publication(manager):
    stage(manager)
    manager.authority.provision('admin', employee('second-admin'), PASSWORD, PASSWORD, ['administrator'])
    class Revoking(Embedder):
        def embed(self, texts):
            manager.authority.assign_roles('second-admin', 'admin', ['employee'])
            return super().embed(texts)
    manager.embedder_factory = Revoking
    with pytest.raises(AdministrationError): manager.publish('admin', manager.listing('admin')['revision'])
    assert manager.snapshot() is None
    assert manager.listing('second-admin')['staged']


@pytest.fixture
def client(manager):
    app = create_web_app(runtime_paths=manager.authority.paths, admin_authority=manager.authority, knowledge_manager=manager,
                         deployment_config=DeploymentConfig(environment=ApplicationEnvironment.TEST, secure_cookie=False))
    with TestClient(app) as client: yield client


def login(client, name="admin"):
    assert client.post("/api/auth/login", json={"work_email": name + "@novatech.example", "password": PASSWORD}).status_code == 200


def test_web_knowledge_admin_requires_auth_authority_origin_and_bounded_body(client, manager):
    assert client.get('/api/admin/knowledge').status_code == 401
    login(client, 'reader')
    assert client.get('/api/admin/knowledge').status_code == 403
    assert client.post('/api/admin/knowledge/publish', json={'revision': 'clean'}, headers=ORIGIN).status_code == 403
    login(client)
    assert client.post('/api/admin/knowledge/publish', json={'revision': 'clean'}).status_code == 403
    assert client.post('/api/admin/knowledge/stage', content=b'x' * 800001, headers={**ORIGIN, 'content-type': 'application/json'}).status_code == 400
    assert client.get('/api/admin/knowledge').headers['cache-control'] == 'no-store'


def test_web_stage_publish_replace_remove_rollback_and_restart(client, manager):
    login(client)
    def send(operation, **fields):
        revision = client.get('/api/admin/knowledge').json()['revision']
        response = client.post('/api/admin/knowledge/' + operation, json={'revision': revision, **fields}, headers=ORIGIN)
        assert response.status_code == 200, response.text
    send('stage', name='managed.md', content=CONTENT, skill='hr', roles=['hr-reader'])
    assert manager.snapshot() is None
    send('publish')
    first = manager.snapshot().version
    send('stage', name='managed.md', content=CONTENT + '\nUse approved support.', skill='hr', roles=['hr-reader'])
    send('publish')
    send('remove', name='managed.md'); send('publish')
    assert 'managed.md' not in manager.snapshot().documents
    send('rollback')
    assert 'managed.md' in KnowledgeManagement(manager.authority, Embedder).snapshot().documents
    assert manager.snapshot().version != first
    assert 'Company knowledge' in client.get('/admin').text


def test_web_publication_failure_is_sanitized_and_preserves_draft(client, manager):
    login(client); stage(manager)
    def fail(): raise RuntimeError('private-provider-detail-never-display')
    manager.embedder_factory = fail
    revision = manager.listing('admin')['revision']
    response = client.post('/api/admin/knowledge/publish', json={'revision': revision}, headers=ORIGIN)
    assert response.status_code == 400
    assert 'private-provider' not in response.text
    assert manager.snapshot() is None and manager.listing('admin')['staged']


def test_admin_knowledge_frontend_only_uses_safe_dom_and_explicit_role_selection():
    script = (PROJECT_ROOT / 'src/knowvia/static/admin.js').read_text(encoding='utf-8')
    html = (PROJECT_ROOT / 'src/knowvia/static/admin.html').read_text(encoding='utf-8')
    assert 'innerHTML' not in script and 'localStorage' not in script
    assert 'new TextDecoder("utf-8", {fatal: true})' in script
    assert 'const choices = roles([])' in script
    assert 'name: file.name' in script and 'revision: knowledge.revision' in script
    for name in ['knowledge-file', 'knowledge-publish', 'knowledge-discard', 'knowledge-rollback']:
        assert 'id="' + name + '"' in html


def test_knowledge_management_reuses_packaged_document_resource_configuration(manager, monkeypatch, tmp_path):
    packaged = tmp_path / 'packaged-resources'
    packaged.mkdir()
    for path in REFERENCE_DOCUMENTS.glob('*.md'): shutil.copyfile(path, packaged / path.name)
    monkeypatch.setenv('KNOWVIA_DOCUMENTS_DIR', str(packaged))
    configured = KnowledgeManagement(manager.authority, Embedder)
    assert configured.reference_documents == packaged.resolve()
    assert len(configured.listing('admin')['documents']) == 8


def test_runtime_factory_applies_snapshot_acl_to_all_skills_without_live_models(manager, monkeypatch):
    from knowvia.retrieval import Retriever
    from knowvia.vector_store import JsonVectorStore
    stage(manager, skill='hr', roles=['hr-reader']); publish(manager)
    config = DeploymentConfig(environment=ApplicationEnvironment.TEST, router_provider='deterministic', secure_cookie=False)
    factory = ProductionRuntimeFactory(config, manager.authority.paths, manager)
    factory._knowledge_components = (Retriever(Embedder(), JsonVectorStore(manager.authority.paths.index)), object(), object())
    reader_bundle = factory.build(manager.authority.resolve('reader'))
    other_bundle = factory.build(manager.authority.resolve('other'))
    for bundle in [reader_bundle, other_bundle]:
        skill = bundle.skill_agent.registry.load('hr')
        retriever = bundle.skill_agent.knowledge_factory(skill).retriever
        sources = [r.chunk.source for r in retriever.search('question', top_k=100)]
        assert ('managed.md' in sources) is (bundle is reader_bundle)
        assert 'managed.md' in skill.knowledge_sources
    old = reader_bundle.skill_agent.knowledge_factory(reader_bundle.skill_agent.registry.load('hr')).retriever
    manager.remove('admin', 'managed.md', 'clean'); publish(manager)
    assert 'managed.md' in [r.chunk.source for r in old.search('question', top_k=100)]
    new = factory.build(manager.authority.resolve('reader'))
    assert 'managed.md' not in [r.chunk.source for r in new.skill_agent.knowledge_factory(new.skill_agent.registry.load('hr')).retriever.search('question', top_k=100)]
