from backend.services import shopping_metrics as metrics


def test_stage_and_transport_snapshots_are_bounded():
    before = metrics.snapshot()
    with metrics.stage('http'):
        metrics.record_request('arbitrary-email@example.test', False, kind='/private-path', status=429)
    metrics.record_stage('untrusted product description', 42)
    metrics.increment('untrusted shopper query')

    after = metrics.snapshot()
    metric_key = 'http_other_other_4xx'
    assert after[metric_key] == before.get(metric_key, 0) + 1
    histograms = after['stage_histograms']['bucket_counts']
    assert sum(histograms['http']) == sum(before['stage_histograms']['bucket_counts'].get('http', [])) + 1
    assert 'untrusted product description' not in histograms
    assert not any('example.test' in key or 'private-path' in key or 'shopper query' in key for key in after)
    histograms['http'][0] = -10
    assert metrics.snapshot()['stage_histograms']['bucket_counts']['http'][0] >= 0


def test_cached_and_verified_progress_have_distinct_observations():
    before = metrics.snapshot()
    card = {'supplier': 'spacenet', 'verified_this_turn': False}
    token = metrics.begin()
    metrics.retrieved([card])
    metrics.first([card])
    metrics.increment('shared_lookups')
    metrics.first([{**card, 'verified_this_turn': True}])
    metrics.model_started()
    metrics.model_usage({'input_tokens': 14})  # Incomplete provider usage remains unknown.
    result = metrics.finish(token, [card], True)

    after = metrics.snapshot()
    assert result['first_result_ms'] is not None
    assert result['first_verified_ms'] >= result['first_result_ms']
    assert result['estimated_cost_usd'] is None
    assert after['initial_local_hits'] == before.get('initial_local_hits', 0) + 1
    assert after['fully_local_hits'] == before.get('fully_local_hits', 0)
    assert after['verified_live_hits'] == before.get('verified_live_hits', 0) + 1
    assert after['unknown_usage'] == before.get('unknown_usage', 0) + 1
