#!/usr/bin/env python3
"""Read-only generation receipts; never infer missing charges as zero.

Only GET https://openrouter.ai/api/v1/generation is permitted. Credentials are
read from process environment, never persisted. Output excludes response text.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

GENERATION = re.compile(r"gen-[A-Za-z0-9_-]{1,180}\Z")
ENDPOINT = "https://openrouter.ai/api/v1/generation"


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def money(value):
    if value is None or isinstance(value, bool):
        raise ValueError("missing or invalid charge")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("invalid charge") from error
    if not result.is_finite() or result < 0:
        raise ValueError("charge must be finite and nonnegative")
    return result


def report_requests(report):
    """Final saved reader generations only; not proof all attempts were saved."""
    requests, seen_cases, owners = [], set(), {}
    for case in report.get("per_case", []):
        identity = (case.get("case_id"), case.get("model"))
        if not all(isinstance(v, str) and v for v in identity) or identity in seen_cases:
            raise ValueError("missing/duplicate case identity")
        seen_cases.add(identity)
        refs = case.get("data", {}).get("provider_responses", [])
        seen_ids = set()
        for ref in refs:
            generation = ref.get("ID")
            if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
                raise ValueError("invalid saved generation ID")
            if generation in seen_ids:
                continue  # Repeated stream ID is one charge, not another request.
            seen_ids.add(generation)
            if generation in owners and owners[generation] != identity:
                raise ValueError("generation attributed to multiple cases")
            owners[generation] = identity
            requests.append({"case_id": identity[0], "stage": "reader",
                             "model": ref.get("Model", identity[1]),
                             "generation_id": generation})
        if not seen_ids:
            requests.append({"case_id": identity[0], "stage": "reader",
                             "model": identity[1], "generation_id": None})
    if not seen_cases:
        raise ValueError("no cases")
    return requests


def journal_evidence(path):
    """Latest cumulative stream record per generation; retain failed-case calls.

    Input contract: passive backend checkpoint+'.provider-usage.jsonl'. It does
    not prove coverage of calls that failed before a provider ID was received.
    """
    latest, owners, unpriced = {}, {}, []
    with Path(path).open() as stream:
        for line in stream:
            row = json.loads(line)
            case, stage, generation = row.get("case_id"), row.get("stage"), row.get("generation_id")
            if not isinstance(case, str) or not case or stage not in ("reader", "judge"):
                raise ValueError("invalid journal attribution")
            request = {"case_id": case, "stage": stage, "generation_id": generation,
                       "model": row.get("model")}
            if not isinstance(request["model"], str) or not request["model"]:
                raise ValueError("journal model missing")
            if not generation:
                unpriced.append(request)
                continue
            if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
                raise ValueError("invalid journal generation ID")
            owner = (case, stage, request["model"], row.get("attempt_id"))
            if generation in owners and owners[generation] != owner:
                raise ValueError("journal generation ownership changed")
            owners[generation] = owner
            receipt = {"generation_id": generation, "status": "cost_missing", "model": request["model"]}
            if row.get("cost_status") == "reported_usage_cost":
                receipt.update(status="ok", total_cost_usd=str(money(row.get("cost_credits"))),
                               basis="openrouter_response.usage.cost", unit_conversion="USD-denominated OpenRouter credits")
            latest[generation] = (request, receipt)
    return [v[0] for v in latest.values()] + unpriced, [v[1] for v in latest.values()]


def combine_requests(saved, journal):
    combined = {}
    missing = []
    for request in saved + journal:
        generation = request.get("generation_id")
        if not generation:
            missing.append(request)
            continue
        if generation in combined and combined[generation] != request:
            raise ValueError("report/journal generation attribution mismatch")
        combined[generation] = request
    known = {(row["case_id"], row["stage"]) for row in combined.values()}
    # Saved-report placeholders are resolved by journal IDs; genuine unknown-ID
    # journal events must remain unpriced, even when other calls were captured.
    journal_missing = [row for row in journal if not row.get("generation_id")]
    missing_saved = [row for row in saved if not row.get("generation_id") and (row["case_id"], row["stage"]) not in known]
    return list(combined.values()) + missing_saved + journal_missing


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


def sanitize_receipt(generation, raw):
    data = raw.get("data", {})
    if data.get("id") != generation:
        raise ValueError("generation receipt identity mismatch")
    charge = money(data.get("total_cost"))
    result = {"generation_id": generation, "status": "ok",
              "total_cost_usd": str(charge), "basis": "openrouter_generation.total_cost"}
    for key in ("model", "provider_name", "created_at", "is_byok", "cancelled",
                "native_tokens_prompt", "native_tokens_completion", "native_tokens_reasoning",
                "native_tokens_cached", "tokens_prompt", "tokens_completion"):
        value = data.get(key)
        if value is not None:
            result[key] = value
    return result


def fetch_receipt(generation, key, opener=None):
    if not GENERATION.fullmatch(generation):
        raise ValueError("invalid generation ID")
    opener = opener or urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(ENDPOINT + "?" + urllib.parse.urlencode({"id": generation}),
                                     headers={"Authorization": "Bearer " + key})
    try:
        with opener.open(request, timeout=30) as response:
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise ValueError("oversized generation response")
            return sanitize_receipt(generation, json.loads(payload, parse_float=Decimal))
    except urllib.error.HTTPError as error:
        return {"generation_id": generation, "status": "http_error", "http_status": error.code}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"generation_id": generation, "status": "transport_error"}
    except (ValueError, TypeError):
        return {"generation_id": generation, "status": "invalid_receipt"}


def reconcile_receipts(requests, receipts, key, fetch=fetch_receipt):
    indexed = {}
    for receipt in receipts:
        generation = receipt.get("generation_id")
        if generation in indexed:
            raise ValueError("duplicate generation receipt")
        indexed[generation] = receipt
    for generation in sorted({row["generation_id"] for row in requests if row["generation_id"]}):
        prior = indexed.get(generation)
        if prior and prior.get("status") == "ok":
            money(prior.get("total_cost_usd"))
            continue
        receipt = fetch(generation, key)
        indexed[generation] = receipt
        if receipt.get("http_status") in (401, 402, 403, 429):
            break
    return list(indexed.values())


def summarize(requests, receipts):
    indexed = {}
    for receipt in receipts:
        generation = receipt.get("generation_id")
        if generation in indexed:
            raise ValueError("duplicate generation receipt")
        indexed[generation] = receipt
    totals, counts, missing = defaultdict(Decimal), defaultdict(int), defaultdict(int)
    stages, seen = set(), set()
    for request in requests:
        key = (request["case_id"], request["stage"])
        stages.add(key)
        generation = request.get("generation_id")
        if generation:
            if generation in seen:
                raise ValueError("duplicate request attribution")
            seen.add(generation)
        receipt = indexed.get(generation, {})
        if receipt.get("status") != "ok":
            missing[key] += 1
            continue
        if receipt.get("model") and receipt["model"] != request["model"]:
            raise ValueError("receipt model does not match attributed request")
        totals[key] += money(receipt.get("total_cost_usd"))
        counts[key] += 1
    rows = [{"case_id": case, "stage": stage,
             "recorded_cost_usd": str(totals[(case, stage)]),
             "priced_generations": counts[(case, stage)],
             "missing_generations": missing[(case, stage)],
             "saved_generation_coverage_complete": missing[(case, stage)] == 0}
            for case, stage in sorted(stages)]
    stage_totals = {stage: str(sum((value for (case, name), value in totals.items() if name == stage), Decimal(0)))
                    for stage in sorted({stage for _, stage in stages})}
    case_totals = defaultdict(Decimal)
    for case, stage in stages:
        case_totals[case] += totals[(case, stage)]
    captured_stats = None
    if not any(missing.values()) and case_totals:
        ordered = sorted(case_totals.values())
        count = len(ordered)
        midpoint = count // 2
        median = ordered[midpoint] if count % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2
        captured_stats = {"mean": str(sum(ordered, Decimal(0)) / count), "median": str(median),
                          "p95_nearest_rank": str(ordered[math.ceil(0.95 * count) - 1])}
    return {"per_case": rows, "recorded_cost_usd": str(sum(totals.values(), Decimal(0))),
            "recorded_cost_by_stage_usd": stage_totals,
            "question_denominator": len(case_totals),
            "captured_generation_cost_per_question_usd": [{"case_id": case, "recorded_cost_usd": str(value)}
                                                          for case, value in sorted(case_totals.items())],
            "captured_generation_cost_per_question_stats_usd": captured_stats,
            "stats_scope": "Captured reader/judge generations only; not all-attempt or full-lifecycle costs. Stats omitted when any captured generation is unpriced.",
            "priced_generations": sum(counts.values()), "missing_generations": sum(missing.values()),
            "saved_generation_coverage_complete": not any(missing.values()),
            "all_attempts_coverage_proven": False,
            "historical_preparation_cost_usd": None,
            "recorded_judge_cost_subtotal_usd": stage_totals.get("judge"), "full_judge_cost_usd": None,
            "embedding_cost_usd": None, "full_lifecycle_cost_usd": None,
            "full_lifecycle_cost_per_question_usd": None,
            "limitation": "Captured generations only. Missing attempts, uncaptured judge calls, embeddings and original preparation are not zero. No full-lifecycle total is established."}


def write_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--receipts", type=Path, help="Existing sanitized receipt JSON; offline by default")
    parser.add_argument("--journal", type=Path, help="Passive backend reader/judge usage journal")
    parser.add_argument("--fetch", action="store_true", help="Authorized read-only provider metadata lookup")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; preserve existing evidence")
    report = json.loads(args.report.read_text())
    requests = report_requests(report)
    receipts = json.loads(args.receipts.read_text())["receipts"] if args.receipts else []
    if args.journal:
        journal_requests, journal_receipts = journal_evidence(args.journal)
        requests = combine_requests(requests, journal_requests)
        provider_receipts = {row["generation_id"]: row for row in receipts}
        for row in journal_receipts:
            prior = provider_receipts.get(row["generation_id"])
            if prior and prior.get("status") == "ok" and row.get("status") == "ok":
                if money(prior["total_cost_usd"]) != money(row["total_cost_usd"]):
                    raise ValueError("response cost and generation metadata disagree")
            if not prior or prior.get("status") != "ok":
                provider_receipts[row["generation_id"]] = row
        receipts = list(provider_receipts.values())
    if args.fetch:
        key = os.environ.get("LOCAL_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            parser.error("key unavailable in process environment")
        receipts = reconcile_receipts(requests, receipts, key)
    result = {"schema": "openrouter-saved-reader-cost-audit-v1",
              "captured_at": datetime.now(timezone.utc).isoformat(),
              "report_sha256": digest(args.report), "report_run_id": report.get("run_id"),
              "helper_sha256": digest(__file__), "receipts": receipts,
              "summary": summarize(requests, receipts)}
    if args.journal:
        result["journal_sha256"] = digest(args.journal)
    write_new(args.output, result)
    print(json.dumps({"priced_generations": result["summary"]["priced_generations"],
                      "missing_generations": result["summary"]["missing_generations"],
                      "full_lifecycle_cost_usd": None}))


if __name__ == "__main__":
    main()
