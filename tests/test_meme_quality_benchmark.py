from scripts import evaluate_meme_retrieval as benchmark


def test_candidate_stage_benchmark_holds_its_recorded_floor():
    value, stickers = benchmark.load(benchmark.DEFAULT_CASES)
    metrics = benchmark.evaluate(value, stickers)["metrics"]
    rate = {key: item["rate"] for key, item in metrics.items()}
    assert rate["explicit_request_candidate_rate"] == 1.0
    assert rate["refused_context_offer_rate"] == 0.0
    assert rate["negated_or_ironic_misfire_rate"] == 0.0
    assert rate["offers_with_unacceptable_candidate"] == 0.0
    # Lexical-only recall; the miss is a third-party case that needs semantic similarity.
    assert metrics["candidate_recall_at_5"]["numerator"] >= 21


def test_benchmark_uses_the_production_parser_not_injected_labels():
    value, stickers = benchmark.load(benchmark.DEFAULT_CASES)
    query, offered = benchmark.candidates("自嘲把工作搞砸了", stickers)
    assert query["target"] == "self" and offered == ["self_fail"]
    assert benchmark.candidates("我一点也不开心", stickers) == (
        {"text": "我一点也不开心", "intent": None, "target": "recipient", "request": None}, [])
