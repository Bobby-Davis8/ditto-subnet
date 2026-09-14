import copy
import hashlib
import json
import unittest
import analyze_slices_pilot as pilot


def report(mode):
    meta = {key: "true" for key in ("lme_complete", "lme_subject_graph", "lme_require_graph", "lme_prepared_snapshot_unchanged")}
    meta.update(lme_seed_context_mode=mode, lme_hydration_preflight_users="60", lme_reasoning_effort="medium",
                lme_prompt_clock="question-date", lme_prepared_snapshot_sha256="same", lme_prepared_snapshot_after_sha256="same",
                lme_cases_sha256="same", lme_manifest_sha256="same", lme_subject_graph_version="same", lme_condition_sha256=mode)
    rows = []
    for i in range(60):
        mem = {"pairID": str(i), "summary": "summary"}
        if mode != "summary":
            mem["user"] = "verbatim source"
        text = json.dumps({"memories": [mem]})
        rows.append(dict(case_id=str(i), query="q", gold_answer="a", category=str(i % 6), model="openai/gpt-5.6-luna",
                         lme_correct=i % 2 == 0, data=dict(query_status="ok", judge_status="ok", seed_context_mode=mode,
                         seed_pair_count=1, seed_pair_ids=[str(i)], seed_context=dict(text=text, bytes=len(text),
                         sha256=hashlib.sha256(text.encode()).hexdigest(), truncated=False), prompt_tokens=10, session_recall=1)))
    return dict(meta=meta, per_case=rows, run_id=mode, git_sha="same", prompt_sha="same", tools_sha="same",
                weights_sha="same", judge_model="google/gemini-3.1-flash-lite")


class PilotAuditTests(unittest.TestCase):
    def test_additive_matched_payload_and_paired_verdicts(self):
        a, b = report("summary"), report("source-slices-v1")
        b["per_case"][1]["lme_correct"] = True
        result = pilot.analyze(a, b, [str(i) for i in range(60)])
        self.assertEqual(result["slices_correct"], 31)
        self.assertEqual(result["wins"], ["1"])
        self.assertEqual(result["baseline_payload_preserved_cases"], 60)

    def test_retrieval_drift_is_reported_not_silently_matched(self):
        a, b = report("summary"), report("source-slices-v1")
        b["per_case"][0]["data"]["seed_pair_ids"] = ["different"]
        result = pilot.analyze(a, b, [str(i) for i in range(60)])
        self.assertEqual(result["retrieval_or_payload_drift_case_ids"], ["0"])

    def test_identity_failure_truncation_and_corruption_fail_closed(self):
        a, b = report("summary"), report("source-slices-v1")
        for mutation in (lambda r: r.update(git_sha="other"),
                         lambda r: r["per_case"][0]["data"]["seed_context"].update(truncated=True),
                         lambda r: r["per_case"][0]["data"]["seed_context"].update(text="corrupt"),
                         lambda r: r["meta"].update(lme_complete="false")):
            bad = copy.deepcopy(b)
            mutation(bad)
            with self.assertRaises(ValueError):
                pilot.analyze(a, bad, [str(i) for i in range(60)])


if __name__ == "__main__":
    unittest.main()
