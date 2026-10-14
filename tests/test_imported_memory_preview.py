from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge.handlers import (
    _KnowledgeSearchState, _parse_knowledge_search_request, _serialize_search_discoveries,
)
from src.mcp_handlers.knowledge.imported_preview import annotate_imported_read
from src.mcp_handlers.middleware.envelope_step import _append_more_results, _memory_suggestions


def row(body, tags=None):
    return DiscoveryNode(id='memory-1', agent_id='writer', type='note', summary='Snapshot',
                         details=body, tags=['source-any-memory'] if tags is None else tags,
                         timestamp=(datetime.now(timezone.utc) - timedelta(days=12)).isoformat())


@pytest.mark.parametrize('body', ['', 'small', 'é' * 2000, '🙂' * 20000])
def test_imported_search_bounds_utf8_and_preserves_stored_evidence(body):
    document = row(body)
    state = _KnowledgeSearchState(request=_parse_knowledge_search_request({}), graph=AsyncMock())
    state.results = [document]
    result = _serialize_search_discoveries(state, include_details=True)[0]
    assert len(result.get('details', '').encode()) <= 2048
    assert document.details == body
    assert result['imported_memory']['trust'] == 'unreviewed'
    assert result['imported_memory']['age_days'] == 12
    if len(body.encode()) > 2048:
        assert result['details_bytes'] == len(body.encode())
        assert result['details_length'] == len(body)
        assert result['details_retrieval'] == {
            'action': 'details', 'discovery_id': 'memory-1', 'offset': 0, 'length': 2000}
        assert result['has_more_details'] is True


def test_native_findings_are_unchanged():
    document = row('🙂' * 2000, tags=[])
    item = {'id': document.id, 'details': document.details}
    annotate_imported_read(item, document)
    assert item == {'id': document.id, 'details': document.details}


def test_sync_metadata_update_does_not_claim_reverification():
    document = row('body')
    document.updated_at = datetime.now(timezone.utc).isoformat()
    item = {'id': document.id}
    annotate_imported_read(item, document)
    assert item['imported_memory'] == {'trust': 'unreviewed', 'age_basis': 'created_at', 'age_days': 12}


def test_markers_survive_first_and_additional_digest_rows():
    document = row('body')
    item = {'id': document.id, 'summary': document.summary}
    annotate_imported_read(item, document)
    payload = {'discoveries': [item] * 6}
    digests = _memory_suggestions(payload)
    assert all(d['imported_memory']['trust'] == 'unreviewed' for d in digests)
    envelope = {'memory_suggestions': digests, 'state_summary': {}}
    _append_more_results(envelope, payload, {'limit': 6})
    assert all(d['imported_memory']['age_days'] == 12 for d in envelope['more_results'])


def test_invalid_date_stays_unknown():
    document = row('body')
    document.timestamp = 'unknown'
    item = {}
    annotate_imported_read(item, document)
    assert item['imported_memory']['age_days'] is None
