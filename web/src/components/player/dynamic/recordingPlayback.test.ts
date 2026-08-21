import { describe, expect, it } from "vitest";
import { resolveRecordingPlayback } from "./recordingPlayback";

describe("resolveRecordingPlayback", () => {
  it("falls back to main recordings when the requested sub recordings are empty", () => {
    const mainRecordings = [{ id: "main-segment" }];

    expect(resolveRecordingPlayback("sub", [], mainRecordings)).toEqual({
      fallbackQuality: "main",
      quality: "main",
      recordings: mainRecordings,
    });
  });

  it("keeps recordings unresolved while the fallback request is pending", () => {
    expect(resolveRecordingPlayback("sub", [])).toEqual({
      fallbackQuality: "main",
      quality: "main",
      recordings: undefined,
    });
  });

  it("does not request a fallback while the requested recordings are pending", () => {
    expect(resolveRecordingPlayback("sub")).toEqual({
      quality: "sub",
      recordings: undefined,
    });
  });

  it("uses the requested recordings without requesting a fallback", () => {
    const subRecordings = [{ id: "sub-segment" }];

    expect(resolveRecordingPlayback("sub", subRecordings)).toEqual({
      quality: "sub",
      recordings: subRecordings,
    });
  });

  it("falls back to sub recordings when the requested main recordings are empty", () => {
    const subRecordings = [{ id: "sub-segment" }];

    expect(resolveRecordingPlayback("main", [], subRecordings)).toEqual({
      fallbackQuality: "sub",
      quality: "sub",
      recordings: subRecordings,
    });
  });

  it("reports no recordings after both quality requests return empty", () => {
    expect(resolveRecordingPlayback("sub", [], [])).toEqual({
      fallbackQuality: "main",
      quality: "main",
      recordings: [],
    });
  });
});
