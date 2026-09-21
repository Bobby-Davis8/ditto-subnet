import { describe, expect, it } from "vitest";

import { parkedReading } from "./pipeline";
import type { PipelineEntryExt } from "./pipeline";

function entry(overrides: Partial<PipelineEntryExt>): PipelineEntryExt {
  return { agent_id: "a", status: "waiting_validator", ...overrides } as PipelineEntryExt;
}

describe("parkedReading", () => {
  it("tells a miner an operator hold is not their failure", () => {
    const read = parkedReading(
      entry({ retry_state: "exhausted", retry_disposition: "operator_hold" }),
    );
    expect(read?.tone).toBe("hold");
    expect(read?.label).toBe("On hold · Ditto-side failure");
    expect(read?.title).toContain("not");
    expect(read?.title).toContain("operator");
  });

  it("names the code and the next step on a terminal artifact failure", () => {
    const read = parkedReading(
      entry({
        retry_state: "exhausted",
        retry_disposition: "terminal_artifact_failure",
        terminal_failure_code: "inference_request_rejected",
      }),
    );
    expect(read?.tone).toBe("terminal");
    expect(read?.label).toContain("inference_request_rejected");
    expect(read?.title).toContain("cannot finish scoring");
    expect(read?.title).toContain("submit a new version");
  });

  it("never implies a refund or a payment outcome", () => {
    for (const disposition of ["operator_hold", "terminal_artifact_failure"]) {
      const read = parkedReading(
        entry({
          retry_state: "exhausted",
          retry_disposition: disposition,
          terminal_failure_code: "inference_allowance_exhausted",
        }),
      );
      const text = (read?.label || "") + " " + (read?.title || "");
      for (const word of ["refund", "fee", "TAO", "credit", "reimburse"]) {
        expect(text.toLowerCase()).not.toContain(word.toLowerCase());
      }
    }
  });

  it("states the terminal outcome without inventing a cause it was not given", () => {
    const read = parkedReading(
      entry({
        retry_state: "exhausted",
        retry_disposition: "terminal_artifact_failure",
        terminal_failure_code: null,
      }),
    );
    expect(read?.title).toContain("cannot finish scoring");
    expect(read?.label).toBe("Cannot finish scoring");
  });

  it("falls back to the no-fault reading when the wire carries no disposition", () => {
    const read = parkedReading(entry({ retry_state: "exhausted" }));
    expect(read?.tone).toBe("hold");
  });

  it("says nothing about a row that is still advancing", () => {
    expect(parkedReading(entry({ retry_state: "queued" }))).toBeNull();
    expect(parkedReading(entry({ retry_state: "cooling_down" }))).toBeNull();
    expect(parkedReading(entry({ retry_state: "running" }))).toBeNull();
  });
});
